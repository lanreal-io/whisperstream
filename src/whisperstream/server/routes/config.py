"""Live config patch and capability query HTTP contracts."""
from __future__ import annotations

import asyncio
import platform
from collections.abc import Awaitable, Callable
from typing import Any, Dict
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from ...config_manager import ConfigManager, WhisperstreamConfig, get_app_dir
from ...managed_policy import HISTORY_DISABLED_MESSAGE
from ..config_payload import (
    _client_config_dict, _config_from_client_dict,
    _inject_key_fingerprints, _coerce_enhancement_under_lockdown,
)


def register_config_routes(
    api: FastAPI, *, current_config: Callable[[], WhisperstreamConfig],
    current_config_manager: Callable[[], ConfigManager],
    reload_config: Callable[[WhisperstreamConfig], None],
    reconcile_input_selection: Callable[
        [], tuple[WhisperstreamConfig, list[dict[str, Any]] | None]
    ],
    managed_lockdown_enabled: Callable[[], bool],
    config_saved: Callable[[WhisperstreamConfig, bool, bool], None],
    history_disabled: Callable[[], bool] = lambda: False,
    screen_reader_detected: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    async def _screen_reader_detected() -> bool:
        # The server passes its monitor-backed read (cached while the monitor
        # runs, else one live check). Standalone registrations use the probe.
        if screen_reader_detected is not None:
            return await screen_reader_detected()
        from ...main import screen_reader_active  # noqa: PLC0415

        return screen_reader_active()

    @api.get("/api/config")
    async def get_config():
        from ...config_manager import get_app_dir
        config, input_devices = await asyncio.to_thread(reconcile_input_selection)
        payload = _client_config_dict(
            config,
            current_config_manager(),
        )
        if input_devices is not None:
            from ...input_device_selection import (  # noqa: PLC0415
                macos_input_selection_available,
            )

            payload["audio_input_device_available"] = (
                macos_input_selection_available(
                    config.audio_input_device,
                    input_devices,
                )
            )
        payload["screen_reader_detected"] = await _screen_reader_detected()
        # Resolved absolute path of the default app data directory.
        # The frontend uses this to display the default storage path and
        # as the destination for "Reset to default" in RelocateModelsDialog.
        if platform.system() == "Darwin":
            payload.pop("data_dir", None)
            payload["model_storage_dir"] = ""
        else:
            payload["data_dir"] = str(get_app_dir())
        _inject_key_fingerprints(payload, current_config())
        return payload

    @api.get("/api/capabilities/daily-use")
    async def get_daily_use_capabilities():
        """Return implementation support plus passive volatile capability."""
        import platform

        from ...daily_use_capabilities import create_daily_use_capability_report

        system = platform.system()
        audio_policy_available: bool | None = None
        if system == "Darwin":
            try:
                from ...macos_audio_policy import (
                    probe_current_output_policy_support,
                )

                audio_policy_available = probe_current_output_policy_support()
            except Exception:  # noqa: BLE001 - capability query fails closed
                audio_policy_available = False
        return create_daily_use_capability_report(
            system,
            audio_policy_available=audio_policy_available,
        ).as_dict()

    @api.get("/api/capabilities/advanced")
    async def get_advanced_capabilities():
        """Return authoritative schema-v1 Phase 5 presentation support."""

        from ...advanced_capabilities import (  # noqa: PLC0415
            create_advanced_capability_report,
        )

        return create_advanced_capability_report(platform.system()).as_dict()

    def _history_disabled_response() -> JSONResponse:
        return JSONResponse(status_code=403, content={
            "ok": False, "error_code": "history_disabled_by_organization",
            "message": HISTORY_DISABLED_MESSAGE,
        })

    @api.put("/api/config")
    async def put_config(body: Dict[str, Any]):
        from ...speech.availability import kokoro_available
        if str(body.get("agent_speech_engine", "")).strip().lower() == "kokoro" and not kokoro_available():
            return JSONResponse(status_code=422, content={"error": "engine_unavailable"})
        hud_was_enabled = bool(
            getattr(current_config(), "claude_hud_enabled", False)
        )
        codex_was_enabled = bool(
            getattr(current_config(), "codex_hud_enabled", False)
        )
        # PUT is the app's config-patch boundary. Build from the live
        # server state so a screen holding an older config snapshot cannot
        # overwrite unrelated settings when it changes one control.
        merged_body = current_config().to_dict()
        # Organization policy refuses an explicit enable request before any
        # consent gate or platform coercion can hide it (Mac drops
        # history_enabled without opt-in, which would otherwise turn a
        # refusal into a silent 200).
        if history_disabled() and (
            body.get("history_enabled") is True or body.get("save_audio") is True
        ):
            return _history_disabled_response()
        if platform.system() == "Darwin":
            opted_in = getattr(current_config(), "mac_history_opt_in", False) is True
            if body.get("history_enabled") is True and not opted_in:
                return JSONResponse(status_code=409, content={"error": "history_consent_required"})
            body = {k: v for k, v in body.items() if k != "mac_history_opt_in"}
        merged_body.update(body)
        try:
            new_config = _config_from_client_dict(merged_body, current=current_config(), config_manager=current_config_manager())
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "invalid_config", "detail": "Invalid configuration."})
        if history_disabled():
            if new_config.history_enabled or new_config.save_audio:
                return _history_disabled_response()
            new_config.history_enabled = False
            new_config.save_audio = False
        previous = current_config()
        previous_anthropic_key = previous.enhancement_anthropic_api_key or (
            previous.enhancement_api_key if previous.enhancement_provider == "anthropic" else ""
        )
        new_anthropic_key = new_config.enhancement_anthropic_api_key or (
            new_config.enhancement_api_key if new_config.enhancement_provider == "anthropic" else ""
        )
        if new_anthropic_key != previous_anthropic_key:
            # Credential replacement must never inherit hidden account routing,
            # even if an older client echoes a stale workspace in its full PUT.
            new_config.enhancement_anthropic_workspace_id = ""
        # Older frontend builds do not know this additive provider flag.
        # Preserve it across their full-config-shaped saves until every
        # active client includes the field explicitly.
        if "codex_hud_enabled" not in body:
            new_config.codex_hud_enabled = codex_was_enabled
        if "agents_enabled" not in body:
            new_config.agents_enabled = (
                getattr(current_config(), "agents_enabled", True) is not False
            )
        # Server-authored reply-hint counter; clients cannot write it.
        current_hint_replies = getattr(current_config(), "claude_reply_hint_dictated_replies", 0)
        new_config.claude_reply_hint_dictated_replies = (
            current_hint_replies if isinstance(current_hint_replies, int) else 0
        )
        # Server-authored connection evidence; clients cannot write it.
        current_record = getattr(current_config(), "claude_connected_plugin_version", "")
        new_config.claude_connected_plugin_version = (
            current_record if isinstance(current_record, str) else ""
        )
        # Preserve the rollout mode defensively when an older client omits
        # the field. The live-state merge above normally supplies it.
        if "enhancement_output_guard_mode" not in body:
            current_guard_mode = getattr(
                current_config(),
                "enhancement_output_guard_mode",
                None,
            )
            if isinstance(current_guard_mode, str):
                current_guard_mode = current_guard_mode.lower()
            if current_guard_mode in ("enforce", "log", "off"):
                new_config.enhancement_output_guard_mode = current_guard_mode
        # The GPU benchmark identity fields are benchmark-authored and
        # server-authoritative. The client never sets them, but its
        # full-config PUT echoes (usually stale, often empty) values that
        # would clobber what the benchmark just wrote. Preserve the server's
        # values so the selected GPU name/index + the all-GPU fingerprint
        # survive ordinary client config saves. (The verdict itself stays
        # client-writable: the frontend caches it for immediate render and
        # the failure path writes "unviable".)
        new_config.enhancement_local_gpu_device = (
            current_config().enhancement_local_gpu_device
        )
        new_config.enhancement_local_gpu_device_index = (
            current_config().enhancement_local_gpu_device_index
        )
        new_config.enhancement_local_gpu_fingerprint = (
            current_config().enhancement_local_gpu_fingerprint
        )
        new_config.enhancement_local_gpu_devices = dict(current_config().enhancement_local_gpu_devices)
        new_config.enhancement_local_gpu_device_names = dict(current_config().enhancement_local_gpu_device_names)
        new_config.enhancement_local_gpu_artifacts = dict(current_config().enhancement_local_gpu_artifacts)
        selected = new_config.enhancement_local_model
        if selected in new_config.enhancement_local_gpu_devices:
            new_config.enhancement_local_gpu_device_index = new_config.enhancement_local_gpu_devices[selected]
            new_config.enhancement_local_gpu_device = new_config.enhancement_local_gpu_device_names.get(selected, "")
        # F1 (CRITICAL): lockdown_mode is server-authoritative. A full-config
        # PUT must never turn it on or off. Machine policy wins even if some
        # earlier in-memory corruption changed the current config; otherwise
        # preserve the live state and leave ordinary transitions to the
        # dedicated enable/disable endpoints.
        new_config.lockdown_mode = (
            True
            if managed_lockdown_enabled()
            else current_config().lockdown_mode
        )
        # While lockdown is ON the enhancement provider is also
        # server-authoritative: a config PUT must not re-point AI off "local"
        # (which would re-enable cloud/Ollama egress under lockdown). Keep
        # whatever the server holds. This is keyed on lockdown_mode (which we
        # just preserved above), NOT on the envelope file — the history-off
        # ("don't save") enable path turns lockdown on WITHOUT writing an
        # envelope, so gating on _is_lockdown_enabled_on_disk() left the
        # provider un-coerced there. That let the frontend's post-enable
        # full-config PUT (which still carries the prior cloud provider from
        # its cached state) trip validate() -> 400 "Save failed". Coercing to
        # the server's stored "local" instead makes the save succeed and
        # returns a sanitized config the client adopts. The provider stays
        # freely writable when lockdown is off, so normal changes still work.
        if new_config.lockdown_mode:
            _coerce_enhancement_under_lockdown(new_config)
        try:
            new_config.validate()
        except ValueError as exc:
            # A rejected config (e.g. F5: lockdown + history + no envelope)
            # must surface as a 400, not an unhandled 500. Message is a short
            # field:reason token with no secret/transcript content.
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_config", "detail": str(exc)},
            )
        current_config_manager().save(new_config)
        reload_config(new_config)
        config_saved(new_config, hud_was_enabled, codex_was_enabled)
        payload = _client_config_dict(new_config, current_config_manager())
        # Mirror GET /api/config: data_dir is a computed field the frontend
        # relies on to display the default model-storage path. Without it on
        # the PUT response, every saveConfig() round-trip drops data_dir and
        # the Home storage row blanks to "Default location" (and the relocate
        # dialog's "From" goes empty) after any settings save.
        if platform.system() == "Darwin":
            payload.pop("data_dir", None)
            payload["model_storage_dir"] = ""
            if isinstance(body.get("audio_input_device_available"), bool):
                payload["audio_input_device_available"] = body[
                    "audio_input_device_available"
                ]
        else:
            payload["data_dir"] = str(get_app_dir())
        _inject_key_fingerprints(payload, new_config)
        return payload
