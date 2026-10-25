"""Always-available Agent setup demonstrations and owned plugin detail routes."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...config_manager import WhisperstreamConfig
from ...eula import AcceptanceGrant
from ..middleware import _EULA_BLOCK_CODE


def register_agent_setup_routes(
    api: FastAPI, *, current_config: Callable[[], WhisperstreamConfig],
    current_grant: Callable[[], AcceptanceGrant],
    acceptance_is_current: Callable[[AcceptanceGrant], bool],
    handle_hook: Callable[[dict[str, Any]], Awaitable[Any]],
    blocked_response: Callable[[str], JSONResponse],
    arm_preview: Callable[[bool, bool], None], teardown: Callable[[], dict],
    get_app_dir: Callable[[], Path], logger: logging.Logger,
) -> None:
    # -- /api/setup/agent-speak/* -- Agent Speak setup flow, Task 1.3 ------
    # Always registered in EVERY build and deliberately NOT dev-gated:
    # step 4 of the setup flow proves the feature with a deterministic,
    # locally-fired question and response, so it can never depend on the
    # dev-tools env var or on a live Claude session choosing to ask
    # something. The payload constants therefore live in the shipped
    # ``agent_setup_payloads`` module (``hud_simulator.py`` is excluded
    # from the Nuitka build).
    #
    # The existing hook gates are unchanged and still apply: the payloads
    # go through ``_handle_claude_hook_body``, the same entry point
    # ``POST /api/claude-hook`` uses, so Lockdown, ``claude_hud_enabled``
    # and EULA acceptance all behave exactly as they do for a real hook.
    #
    # Logging here stays content-free: never the payloads, the question,
    # the option labels, the reply, or any path.

    async def _drive_setup_payloads(payloads, grant) -> None:
        # Sequential, never concurrent: an AskUserQuestion needs its
        # PreToolUse surface to land before the PermissionRequest attaches
        # the answer hook. Only the LAST payload may block (a question or
        # response hook deliberately holds for its whole grace window), so
        # every earlier payload is awaited and the last is left running.
        for payload in payloads[:-1]:
            if not acceptance_is_current(grant):
                return
            await handle_hook(payload)

        async def _drive_last() -> None:
            if acceptance_is_current(grant):
                await handle_hook(payloads[-1])

        asyncio.ensure_future(_drive_last())

    def _fire_setup_payloads(kind: str, payloads):
        """Fire one setup sequence in the background and return at once."""
        if (
            not getattr(current_config(), "claude_hud_enabled", False)
            or getattr(current_config(), "agents_enabled", True) is False
        ):
            logger.info(
                "[ClaudeHUD] setup fire skipped kind=%s reason=hud_disabled",
                kind,
            )
            return {
                "ok": False,
                "detail": "claude_hud_enabled is off; the hook path returns {}.",
            }
        grant = current_grant()
        if not acceptance_is_current(grant):
            logger.info(
                "[ClaudeHUD] setup fire blocked kind=%s reason=%s",
                kind,
                _EULA_BLOCK_CODE,
            )
            return blocked_response(
                f"/api/setup/agent-speak/{kind}"
            )
        asyncio.ensure_future(_drive_setup_payloads(payloads, grant))
        logger.info(
            "[ClaudeHUD] setup fire kind=%s payloads=%d", kind, len(payloads)
        )
        return {"ok": True}

    @api.post("/api/setup/agent-speak/question")
    async def setup_agent_speak_question():
        from ...agent_setup_payloads import (  # noqa: PLC0415 — lazy: cold setup-only path
            setup_question_payloads,
        )

        return _fire_setup_payloads("question", setup_question_payloads())

    @api.post("/api/setup/agent-speak/response")
    async def setup_agent_speak_response():
        from ...agent_setup_payloads import (  # noqa: PLC0415 — lazy: cold setup-only path
            setup_response_payloads,
        )

        return _fire_setup_payloads("response", setup_response_payloads())

    @api.post("/api/setup/agent-speak/delivery-preview")
    @api.post("/api/setup/agent-speak/preview")
    async def setup_agent_speak_delivery_preview(request: Request):
        """Arm a setup-only delivery preview override for step 4 testing.

        Ephemerally overrides agent_summary_text_enabled and agent_speech_enabled
        while setup step 4 is active. Cleared by teardown, lockdown, shutdown,
        or setup completion. Never persists to config.
        """
        grant = current_grant()
        if not acceptance_is_current(grant):
            return blocked_response(
                "/api/setup/agent-speak/delivery-preview"
            )
        try:
            data = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "detail": "Invalid JSON body."},
            )
        if not isinstance(data, dict):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "detail": "Payload must be a JSON object."},
            )

        summary_val = None
        for key in (
            "show_summary_text",
            "show_summaries",
            "agent_summary_text_enabled",
        ):
            if key in data:
                summary_val = data[key]
                break

        speech_val = None
        for key in ("agent_speech_enabled", "speak_replies", "read_aloud"):
            if key in data:
                speech_val = data[key]
                break

        if summary_val is None and speech_val is None:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "detail": "Missing delivery mode parameters."},
            )

        if summary_val is not None and not isinstance(summary_val, bool):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "detail": "show_summary_text must be a boolean."},
            )

        if speech_val is not None and not isinstance(speech_val, bool):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "detail": "agent_speech_enabled must be a boolean."},
            )

        summary_enabled = bool(summary_val) if summary_val is not None else False
        speech_enabled = bool(speech_val) if speech_val is not None else False

        if not summary_enabled and not speech_enabled:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "detail": "At least one delivery mode must be enabled.",
                },
            )

        # Body reads can yield while revocation clears the setup override.
        # Only the same still-current grant may arm it again.
        if not acceptance_is_current(grant):
            return blocked_response("/api/setup/agent-speak/delivery-preview")
        arm_preview(summary_enabled, speech_enabled)

        logger.info(
            "[ClaudeHUD] setup delivery preview armed summary=%s speech=%s",
            summary_enabled,
            speech_enabled,
        )
        return {
            "ok": True,
            "agent_summary_text_enabled": summary_enabled,
            "agent_speech_enabled": speech_enabled,
        }

    @api.post("/api/setup/agent-speak/teardown")
    async def setup_agent_speak_teardown():
        """Release whatever the setup fire routes may have left live.

        Idempotent and never raising, by design: with nothing live this is
        a no-op that still returns 200. Without it, a synthetic Stop leaves
        a broker request blocked for its whole grace window (up to 120
        seconds) and a live compose lease that swallows the user's next
        push-to-talk.
        """
        return teardown()

    @api.get("/api/setup/agent-speak/plugin-detail")
    async def setup_agent_speak_plugin_detail():
        # Task 1.4 / design decision 1: ``_public_claude_plugin_status``
        # deliberately strips ``plugin_dir`` and ``marketplace_dir`` from
        # every other ``/api/claude-hud/*`` response. This route
        # consciously reverses that, ONLY here, so setup step 1 can offer
        # Show in Explorer and an expandable view of the installed hook
        # definition. Both values are exposed as data to the frontend:
        # they must never be logged and never enter a diagnostic bundle.
        # ``_public_claude_plugin_status``'s allowlist is untouched by
        # this route.
        from ...claude_hud import (  # noqa: PLC0415 — lazy: cold setup-only path
            _has_owner_marker,
            _owned_plugin_paths,
        )

        _marketplace_dir, plugin_dir = _owned_plugin_paths(get_app_dir())
        if not _has_owner_marker(plugin_dir):
            return {"plugin_dir": None, "hook_definition": None}

        hook_definition: str | None
        try:
            hook_definition = (
                plugin_dir / "hooks" / "hooks.json"
            ).read_text(encoding="utf-8")
        except OSError:
            hook_definition = None

        return {
            "plugin_dir": str(plugin_dir),
            "hook_definition": hook_definition,
        }

    @api.get("/api/setup/agent-speak/codex-plugin-detail")
    async def setup_agent_speak_codex_plugin_detail():
        """Return the owned Codex hook receipt only to the setup surface."""

        from ...codex_plugin_lifecycle import (
            _has_owner_marker,
            _owned_codex_plugin_paths,
        )

        marketplace_dir, plugin_dir = _owned_codex_plugin_paths(get_app_dir())
        if not _has_owner_marker(marketplace_dir):
            return {"plugin_dir": None, "hook_definition": None}

        try:
            hook_definition = (
                plugin_dir / "hooks" / "hooks.json"
            ).read_text(encoding="utf-8")
        except OSError:
            hook_definition = None
        return {
            "plugin_dir": str(plugin_dir),
            "hook_definition": hook_definition,
        }
