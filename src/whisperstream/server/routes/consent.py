"""Target-bound consent and credential removal HTTP contracts."""
from __future__ import annotations

from collections.abc import Callable
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from ...config_manager import WhisperstreamConfig
from ...enhancement_security import EnhancementAdmissionError
from ...byok_consent import list_records, record_consent, remove_provider

class BYOKConsentContextBody(BaseModel):
    provider: str
    endpoint: str | None = None
    model: str | None = None


class BYOKConsentBody(BYOKConsentContextBody):
    dialog_body_text: str
    context_token: str = ""


def register_consent_status_route(
    api: FastAPI, *, current_config: Callable[[], WhisperstreamConfig],
) -> None:
    @api.get("/api/byok-consent")
    async def get_byok_consent():
        from ...enhancement_security import consent_target
        from ...byok_consent import has_target_consent
        current = []
        for provider in ("gemini", "openai", "anthropic", "ollama", "custom"):
            try:
                if has_target_consent(consent_target(current_config(), provider=provider)):
                    current.append(provider)
            except EnhancementAdmissionError:
                pass
        return {
            "acknowledged_providers": current,
            "records": list_records(),
        }

def register_consent_mutation_routes(
    api: FastAPI, *, current_config: Callable[[], WhisperstreamConfig],
    save_config: Callable[[WhisperstreamConfig], None],
    reload_config: Callable[[WhisperstreamConfig], None],
) -> None:
    def resolve_consent_context(body: BYOKConsentContextBody):
        from copy import copy
        from ...enhancement_security import consent_target
        from ...consent_disclosure import consent_context, LABELS
        provider = body.provider.strip().lower()
        if provider not in LABELS:
            raise EnhancementAdmissionError("unknown_provider")
        cfg = copy(current_config())
        cfg.enhancement_provider = provider
        if body.model is not None:
            setattr(cfg, f"enhancement_{provider}_model", body.model)
        if body.endpoint is not None and provider in {"custom", "ollama"}:
            field = "enhancement_custom_base_url" if provider == "custom" else "enhancement_ollama_endpoint"
            setattr(cfg, field, body.endpoint)
        target = consent_target(cfg, provider=provider)
        context = consent_context(target)
        try:
            context["matches_config"] = target == consent_target(current_config())
        except EnhancementAdmissionError:
            context["matches_config"] = False
        return target, context

    @api.post("/api/byok-consent/context")
    async def get_byok_consent_context(body: BYOKConsentContextBody):
        try:
            _, context = resolve_consent_context(body)
            return context
        except EnhancementAdmissionError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc), "error_code": str(exc)})

    @api.post("/api/byok-consent")
    async def post_byok_consent(body: BYOKConsentBody):
        try:
            target, context = resolve_consent_context(body)
        except EnhancementAdmissionError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc), "error_code": str(exc)})
        if body.context_token != context["context_token"] or body.dialog_body_text != context["dialog_body_text"]:
            return JSONResponse(status_code=409, content={"error": "consent_changed", "error_code": "consent_changed"})
        return record_consent(
            target.provider, context["dialog_body_text"], target=target,
            legal_identity=context["legal_identity"],
        )

    @api.post("/api/enhancement/remove-key")
    async def remove_enhancement_key():
        # Clears the encrypted credential and revokes consent for the
        # currently configured provider. Re-enabling the same provider
        # must produce a fresh click-through (PRIVACY.md §6.5 bullet 3).
        # Scoped to the active provider's per-provider slot so credentials
        # for other providers stay intact. The legacy active-mirror is
        # also cleared so a stale value can't leak back via the fallback.
        from ...config_manager import _PROVIDER_KEY_SLOT  # noqa: PLC0415
        provider = (current_config().enhancement_provider or "").lower()
        from copy import deepcopy
        cfg = deepcopy(current_config())
        slot_attr = _PROVIDER_KEY_SLOT.get(provider)
        if slot_attr is not None:
            setattr(cfg, slot_attr, "")
        if provider == "anthropic":
            cfg.enhancement_anthropic_workspace_id = ""
        cfg.enhancement_api_key = ""
        cfg.enhancement_enabled = False
        cfg.validate()
        save_config(cfg)
        reload_config(cfg)
        removed = remove_provider(provider) if provider else 0
        return {
            "status": "ok",
            "provider": provider,
            "consent_records_removed": removed,
        }
