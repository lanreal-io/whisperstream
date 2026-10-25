"""Enhancement execution and public catalog HTTP contracts."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from ...config_manager import WhisperstreamConfig
from ...eula import AcceptanceGrant
from ...enhancement_security import require_enhancement_admission, EnhancementAdmissionError
from ..callbacks import EventBroadcaster

if TYPE_CHECKING:
    from ...llama_server import LlamaServerSupervisor
    from ...llm_enhancer import LLMEnhancer

class EnhancementPreviewBody(BaseModel):
    text: str
    system_prompt: str
    profile_id: str | None = None


def register_enhancement_execution_routes(
    api: FastAPI, *, current_config: Callable[[], WhisperstreamConfig],
    current_enhancer: Callable[[], LLMEnhancer],
    request_grant: Callable[[Request], AcceptanceGrant],
    acceptance_is_current: Callable[[AcceptanceGrant], bool],
    blocked_response: Callable[[str], JSONResponse],
    broadcast: EventBroadcaster,
) -> None:
    @api.get("/api/enhancement/thinking")
    async def enhancement_thinking():
        from ...enhancement_thinking import resolve_thinking
        return resolve_thinking(current_config())

    @api.post("/api/enhancement/check")
    async def enhancement_check(request: Request):
        grant = request_grant(request)
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        try:
            require_enhancement_admission(current_config())
        except EnhancementAdmissionError as exc:
            return JSONResponse(
                status_code=403,
                content={"connected": False, "message": str(exc), "error_code": str(exc)},
            )
        check = await current_enhancer().check_connection()
        connected, message = check
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        result = {"connected": connected, "message": message}
        error_code = getattr(check, "error_code", None)
        if error_code in {"workspace_required", "workspace_invalid", "workspace_unavailable"}:
            result["error_code"] = error_code
        broadcast(
            {"type": "enhancement.status", **result},
            acceptance_grant=grant,
        )
        return result

    @api.post("/api/enhancement/preview")
    async def enhancement_preview(
        body: EnhancementPreviewBody, request: Request
    ):
        """Run the configured provider on the given sample text.

        Powers the per-profile "Try it" tester. Editable-prompt models use
        the client-supplied prompt. Fixed-prompt local models resolve the
        optional profile id through the selected registry contract.

        Returns a structured 400 (``error_code: "not_configured"``) when no
        provider/key is configured so the frontend can surface the gate hint
        instead of typing raw text. NEVER logs the sample text or output.
        """
        grant = request_grant(request)
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        cfg = current_config()
        # Use effective_provider() for the dispatch/key-required decision so
        # that under lockdown (effective_provider()="local") the gate never
        # rejects the call for a missing cloud key (Task 4.6).
        provider = (cfg.effective_provider() or "").lower()

        try:
            require_enhancement_admission(cfg)
        except EnhancementAdmissionError as exc:
            return JSONResponse(
                status_code=403,
                content={"error": str(exc), "error_code": str(exc)},
            )

        # Gate: a cloud provider with no stored key cannot run the test.
        # Mirrors check_connection()'s requires-key logic (local + ollama
        # need no cloud key). Surface a clear, structured error so the UI
        # shows the "Connect a model" hint rather than echoing raw text.
        requires_key = provider not in ("local", "ollama")
        if requires_key and not (cfg.active_api_key() or "").strip():
            return JSONResponse(
                status_code=400,
                content={
                    "error": "No cleanup model configured.",
                    "error_code": "not_configured",
                },
            )

        if body.profile_id:
            output = await current_enhancer().preview(
                body.text, body.system_prompt, profile_id=body.profile_id
            )
        else:
            output = await current_enhancer().preview(
                body.text, body.system_prompt
            )
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        return {"output": output}

def register_enhancement_catalog_routes(
    api: FastAPI, *, current_config: Callable[[], WhisperstreamConfig],
    current_llama: Callable[[], LlamaServerSupervisor | None],
    diagnostics_locked: Callable[[], bool],
    software_version: str,
) -> None:
    @api.get("/api/enhancement/models")
    async def get_enhancement_models(provider: str = ""):
        """Return the curated cloud or merged local model catalog.

        Cloud providers use the validated remote, disk-cache, and bundled
        manifest chain. The catalog request never reads or transmits a
        provider API key.

        For provider="local": returns the merged production/development
        catalog with per-row source, artifact, selection, download, and
        active metadata. The legacy installed_ids/available_ids fields
        remain production-only so older frontends preserve the existing
        hide-until-published and managed-download behavior.
        """
        from ...enhancement_models import (
            get_local_model_catalog,
            resolve_local_model_path,
        )
        from ...enhancement_policy import model_capabilities

        normalized = provider.strip().lower()
        if normalized not in ("gemini", "openai", "anthropic", "local", "custom"):
            return JSONResponse(
                status_code=400,
                content={"error": "unknown provider"},
            )

        # Lockdown makes every cloud/custom provider unavailable. The local
        # catalog remains available.
        if normalized != "local" and diagnostics_locked():
            return JSONResponse(
                status_code=403,
                content={
                    "error_code": "lockdown_provider_unavailable",
                    "message": (
                        "Cloud and remote providers are unavailable in "
                        "Lockdown Mode."
                    ),
                },
            )

        # Local provider: return the merged GGUF catalog. Legacy disk and
        # remote-availability ID arrays intentionally remain limited to
        # production rows: older frontends interpret them as permission to
        # enter managed download/benchmark flows, which candidates must
        # never reach.
        if normalized == "local":
            import asyncio

            from ...model_downloader import local_gguf_available
            from ...resident_llama_pool import estimate_resident_ram_mb

            cfg = current_config()
            catalog = get_local_model_catalog(
                active_id=cfg.enhancement_local_model
            )
            production_entries = [
                entry
                for entry in catalog
                if entry["source"] == "production"
            ]
            installed_ids = [
                str(entry["id"])
                for entry in production_entries
                if entry["artifact_available"]
            ]

            # Only surface a model the user can actually download now: a
            # production GGUF is available when it has a pinned download
            # source, or if already installed on disk.
            # Offloaded to a thread so availability checks never block the
            # event loop.
            installed_set = set(installed_ids)

            def _compute_available() -> list[str]:
                return [
                    entry["id"]
                    for entry in production_entries
                    if local_gguf_available(
                        entry["id"], installed=entry["id"] in installed_set
                    )
                ]

            available_ids = await asyncio.to_thread(_compute_available)
            available_set = set(available_ids)
            public_catalog: list[dict] = []
            for entry in catalog:
                resident_ram_mb = None
                try:
                    artifact_path = resolve_local_model_path(entry)
                    if artifact_path is not None:
                        resident_ram_mb = estimate_resident_ram_mb(
                            artifact_path,
                            catalog_ram_mb=entry.get("ram_mb", 0),
                        )
                except (OSError, TypeError, ValueError):
                    # Unavailable rows stay visible in the merged catalog,
                    # but no fictional memory number is attached.
                    resident_ram_mb = None
                row = {
                    key: value
                    for key, value in entry.items()
                    if not key.startswith("_")
                }
                row["capabilities"] = model_capabilities(normalized, entry).public_dict()
                if resident_ram_mb is not None:
                    row["resident_ram_mb"] = resident_ram_mb
                    row["resident_ram_estimated"] = (
                        row["source"] == "candidate"
                    )
                if row["source"] == "production":
                    row["selectable"] = row["id"] in available_set
                public_catalog.append(row)

            llama_server = current_llama()
            running = llama_server is not None and llama_server.is_running
            return {
                "provider": "local",
                "bundled": public_catalog,
                "installed_ids": installed_ids,
                "available_ids": available_ids,
                "active_id": cfg.enhancement_local_model,
                "running": running,
            }

        if normalized == "custom":
            return {"provider": "custom", "bundled": []}

        from ...cloud_model_manifest import resolve_cloud_model_catalog

        catalog = await resolve_cloud_model_catalog(
            app_version=software_version
        )
        return {
            "provider": normalized,
            "bundled": [
                {**row, "capabilities": model_capabilities(normalized).public_dict()}
                for row in catalog[normalized]
            ],
        }

    @api.get("/api/enhancement/builtin-prompt")
    async def get_builtin_prompt(mode: str = "chat"):
        """Return the bundled system prompt for ``mode``.

        Profile editors use this endpoint for one-shot template prefills.
        Chat intentionally returns the simpler editable template instead
        of the larger production cloud prompt.
        """
        from ...llm_enhancer import CHAT_TEMPLATE_PROMPT, CODE_PROMPT, EMAIL_PROMPT

        mode_normalized = mode.strip().lower()
        prompts = {
            "chat": CHAT_TEMPLATE_PROMPT,
            "code": CODE_PROMPT,
            "email": EMAIL_PROMPT,
        }
        if mode_normalized not in prompts:
            return JSONResponse(
                status_code=400,
                content={"error": "unknown mode"},
            )
        return {"mode": mode_normalized, "prompt": prompts[mode_normalized]}
