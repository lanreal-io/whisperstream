"""Speech HTTP contracts with live configuration and shared download admission.

Concrete synthesis engines stay lazy behind the existing speech controller.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...agent_plugin_runtime import plugin_identity
from ...config_manager import WhisperstreamConfig
from ...model_downloader import DOWNLOAD_REGISTRY, is_present
from ...tts_playback import speech_engine_supported

if TYPE_CHECKING:
    from ...speech.controller import SpeechController


class SpeechDownloadBody(BaseModel):
    engine: Literal["kokoro", "supertonic"]


class SpeechSampleBody(BaseModel):
    engine: Literal["kokoro", "supertonic"] | None = None
    voice: int | str | None = None


# Onboarding voice-speed (Task 1.2): caller-submitted test text is
# transcript-class, unlike the fixed SAMPLE_TEXT line, so it is capped and
# validated explicitly. Keep this a named constant rather than a magic number.
SPEAK_TEXT_MAX_CHARS = 500


class SpeechTextBody(BaseModel):
    # TRANSCRIPT DATA - DO NOT LOG. `text` is user-typed content, never a
    # fixed product string. See SpeechController.speak_text's privacy banner.
    text: str
    engine: Literal["kokoro", "supertonic"] | None = None
    voice: int | str | None = None


class SpeechMemoryStatus(BaseModel):
    total_mb: int | None
    available_mb: int | None
    eligible: bool | None


class SpeechEngineStatus(BaseModel):
    key: Literal["kokoro", "supertonic"]
    label: str
    installed: bool
    supported: bool
    available: bool
    unavailable_reason: str | None
    downloading: bool
    download_bytes: int
    installed_bytes: int


SpeechSelectionReason = Literal[
    "kokoro",
    "supertonic",
    "low_memory",
    "unknown_memory",
    "missing_kokoro",
    "missing_supertonic",
    "unhealthy_kokoro",
    "slow_kokoro",
    "runtime_unavailable",
]


class SpeechStatusResponse(BaseModel):
    plugin_variant: Literal["prod", "dev"]
    requested: Literal["auto", "kokoro", "supertonic"]
    preferred_engine: Literal["kokoro", "supertonic"]
    resolved_engine: Literal["kokoro", "supertonic"] | None
    active_engine: Literal["kokoro", "supertonic"] | None
    reason: SpeechSelectionReason
    memory: SpeechMemoryStatus
    kokoro_memory_tier: Literal[
        "recommended",
        "caution",
        "blocked",
        "unknown",
    ]
    engines: dict[str, SpeechEngineStatus]
    current_download: Literal["kokoro", "supertonic"] | None
    recommended_downloads: list[Literal["kokoro", "supertonic"]]
    # Rolling compatibility for the original Supertonic-only frontend.
    installed: bool
    downloading: bool
    engine: Literal["kokoro", "supertonic"]


def register_speech_routes(
    api: FastAPI,
    *,
    current_config: Callable[[], WhisperstreamConfig],
    current_controller: Callable[[], SpeechController | None],
    current_download_engine: Callable[[], str | None],
    try_start_download: Callable[[str], bool],
) -> None:
    """Expose the existing controller while composition owns the download slot."""
    @api.get("/api/speech/status", response_model=SpeechStatusResponse)
    async def speech_status():
        from ...speech.engine_specs import ENGINE_SPECS
        from ...speech.availability import kokoro_available, legacy_kokoro_cached
        engine_specs = {k: v for k, v in ENGINE_SPECS.items() if k != "kokoro" or kokoro_available()}
        from ...speech.selection import (
            AVAILABLE_RAM_MIN_MB,
            MemorySnapshot,
            SelectionPolicy,
            classify_kokoro_memory,
            normalize_selection,
        )
        from ...system_info import get_available_ram_mb, get_total_ram_mb

        total_mb = get_total_ram_mb()
        available_mb = get_available_ram_mb()
        memory_eligible = (
            None
            if available_mb is None
            else available_mb >= AVAILABLE_RAM_MIN_MB
        )
        kokoro_memory_tier = classify_kokoro_memory(
            total_mb,
            available_mb,
        )

        installed_by_engine = {
            key: (is_present(spec.model_id) or (key == "kokoro" and legacy_kokoro_cached()))
            for key, spec in engine_specs.items()
        }
        current_download = current_download_engine()
        config = current_config()
        requested = normalize_selection(
            getattr(config, "agent_speech_engine", "auto")
        )

        # Reuse the live controller's process-health facts when present,
        # but use the static presence map for installation checks. Status
        # must never construct an engine wrapper or import a concrete TTS
        # implementation just to explain the next selection.
        controller = current_controller()
        health_manager = getattr(
            controller, "__dict__", {}
        ).get("_engine_manager")

        class _StatusManager:
            @staticmethod
            def is_installed(engine_key: str) -> bool:
                return installed_by_engine.get(engine_key, False)

            @classmethod
            def is_available(cls, engine_key: str) -> bool:
                return (
                    cls.is_installed(engine_key)
                    and cls.is_healthy(engine_key)
                )

            @staticmethod
            def is_healthy(engine_key: str) -> bool:
                if not speech_engine_supported(engine_key):
                    return False
                return (
                    True
                    if health_manager is None
                    else bool(health_manager.is_healthy(engine_key))
                )

            @staticmethod
            def is_resident(engine_key: str) -> bool:
                probe = getattr(health_manager, "is_resident", None)
                return bool(probe(engine_key)) if callable(probe) else False

            @staticmethod
            def low_memory_hold_active() -> bool:
                probe = getattr(health_manager, "low_memory_hold_active", None)
                return bool(probe()) if callable(probe) else False

            @staticmethod
            def health_reason(engine_key: str) -> str | None:
                if not speech_engine_supported(engine_key):
                    return "runtime_unavailable"
                return (
                    None
                    if health_manager is None
                    else health_manager.health_reason(engine_key)
                )

        policy = SelectionPolicy(
            _StatusManager(),
            memory_probe=lambda: MemorySnapshot(total_mb, available_mb),
            hold_on_fallback=False,
        )
        resolution = policy.resolve(
            requested,
            supertonic_voice=getattr(config, "agent_speech_voice", 1),
            kokoro_voice=getattr(
                config, "agent_speech_kokoro_voice", "af_heart"
            ),
        )
        resolved_engine = (
            resolution.candidates[0].engine_key
            if resolution.candidates
            else None
        )
        preferred_engine = (
            requested
            if requested in {"kokoro", "supertonic"}
            else ("kokoro" if memory_eligible is True or not speech_engine_supported("supertonic") else "supertonic")
        )
        reason = resolution.reason
        if requested == "supertonic" and resolved_engine is None:
            reason = (
                "runtime_unavailable"
                if (
                    not _StatusManager.is_healthy("supertonic")
                    and _StatusManager.health_reason("supertonic")
                    == "runtime_unavailable"
                )
                else "missing_supertonic"
            )

        # Automatic downloads both models, Supertonic first so speech can
        # start sooner (owner decision, 2026-10-25); an explicit choice
        # downloads only itself.
        desired_downloads = (
            ["supertonic", "kokoro"]
            if requested == "auto"
            else [requested]
        )
        recommended_downloads = [
            key
            for key in desired_downloads
            if key in installed_by_engine
            and not installed_by_engine[key]
            and speech_engine_supported(key)
        ]

        engines = {}
        for key, spec in engine_specs.items():
            model_spec = DOWNLOAD_REGISTRY[spec.model_id]
            engines[key] = {
                "key": key,
                "label": spec.label,
                "installed": installed_by_engine[key],
                "supported": speech_engine_supported(key),
                "available": _StatusManager.is_available(key),
                "unavailable_reason": (
                    None
                    if _StatusManager.is_available(key)
                    else (
                        _StatusManager.health_reason(key)
                        if installed_by_engine[key] or not speech_engine_supported(key)
                        else "model_unavailable"
                    )
                ),
                "downloading": current_download == key,
                "download_bytes": model_spec.download_bytes or 0,
                "installed_bytes": model_spec.installed_bytes or 0,
            }

        compatibility_engine = resolved_engine or preferred_engine
        active_engine = None
        active_engine_probe = getattr(controller, "active_engine", None)
        if callable(active_engine_probe):
            try:
                probed_engine = active_engine_probe()
                if probed_engine in {"kokoro", "supertonic"}:
                    active_engine = probed_engine
            except Exception:  # noqa: BLE001 -- status route is never-raise
                active_engine = None
        return {
            "requested": requested,
            "plugin_variant": "dev" if plugin_identity().dev else "prod",
            "preferred_engine": preferred_engine,
            "resolved_engine": resolved_engine,
            # Actual audio ownership, not the candidate for a future utterance.
            "active_engine": active_engine,
            "reason": reason,
            "memory": {
                "total_mb": total_mb,
                "available_mb": available_mb,
                "eligible": memory_eligible,
            },
            "kokoro_memory_tier": kokoro_memory_tier,
            "engines": engines,
            "current_download": current_download,
            "recommended_downloads": recommended_downloads,
            "installed": installed_by_engine[compatibility_engine],
            "downloading": current_download is not None,
            "engine": compatibility_engine,
        }

    @api.get("/api/speech/voices")
    async def speech_voices(
        engine: Literal["kokoro", "supertonic"] = "supertonic",
    ):
        # Static engine metadata only. Voice labels are product metadata,
        # not transcript data, and this path never loads model weights.
        from ...speech.engine_specs import get_engine_spec

        from ...speech.availability import kokoro_available
        if engine == "kokoro" and not kokoro_available():
            return JSONResponse(status_code=404, content={"error": "engine_unavailable"})
        spec = get_engine_spec(engine)

        return {
            "engine": spec.key,
            "engine_label": spec.label,
            "voices": [
                {
                    "id": voice.id,
                    "sid": voice.id,
                    "label": voice.label,
                    "group": voice.group,
                    **(
                        {"reference_label": voice.reference_label}
                        if voice.reference_label is not None
                        else {}
                    ),
                }
                for voice in spec.voices
            ],
        }

    @api.post("/api/speech/download/start")
    async def speech_download_start(body: SpeechDownloadBody):
        if not speech_engine_supported(body.engine):
            return JSONResponse(status_code=409, content={"status": "unavailable", "reason": "runtime_unavailable"})
        from ...speech.availability import kokoro_available
        if (body.engine) == "kokoro" and not kokoro_available():
            return JSONResponse(status_code=404, content={"error": "engine_unavailable"})
        # Shares the single-download guard with the Parakeet/local-LLM
        # download endpoints through composition-owned admission
        # so a voice-model download concurrent with any other download is
        # rejected the same way a second Parakeet/LLM download would be.
        if not try_start_download(body.engine):
            return JSONResponse(status_code=409, content={"status": "busy"})
        return {"status": "started", "engine": body.engine}

    @api.post("/api/speech/stop")
    async def speech_stop():
        # Fire-and-forget shared stop chokepoint for the HUD stop button and
        # the Controls "Hear a sample" -> "Stop" toggle. Always {"ok": True},
        # even with no controller (e.g. gate on but voice model not yet
        # installed) -- there is simply nothing to stop.
        controller = current_controller()
        if controller is not None:
            controller.stop()
        return {"ok": True}

    @api.post("/api/speech/speak-sample")
    async def speech_speak_sample(body: SpeechSampleBody | None = None):
        from ...speech.availability import kokoro_available
        if (body.engine if body else None) == "kokoro" and not kokoro_available():
            return JSONResponse(status_code=404, content={"error": "engine_unavailable"})
        # speak_sample() is synchronous but non-blocking: it only resolves
        # config + spawns PlaybackOwner's own daemon worker before returning
        # (see speech/controller.py + speech/playback.py), so no
        # asyncio.create_task / thread wrapper is needed to keep this
        # handler fast. SAMPLE_TEXT is a static product string, not
        # transcript data.
        explicit_engine = body.engine if body is not None else None
        voice: int | str | None = None
        if explicit_engine is not None:
            from ...speech.engine_specs import get_engine_spec

            spec = get_engine_spec(explicit_engine)
            voice = (
                (
                    getattr(
                        current_config(),
                        "agent_speech_voice",
                        spec.default_voice_id,
                    )
                    if explicit_engine == "supertonic"
                    else getattr(
                        current_config(),
                        "agent_speech_kokoro_voice",
                        spec.default_voice_id,
                    )
                )
                if body.voice is None
                else body.voice
            )
            if not any(item.id == voice for item in spec.voices):
                return JSONResponse(
                    status_code=422,
                    content={"error": "invalid_voice"},
                )

        controller = current_controller()
        if controller is not None:
            if explicit_engine is None:
                controller.speak_sample()
            else:
                controller.speak_sample(
                    engine=explicit_engine,
                    voice=voice,
                )
        return {"ok": True}

    @api.post("/api/speech/speak-text")
    async def speech_speak_text(body: SpeechTextBody):
        from ...speech.availability import kokoro_available
        if (body.engine) == "kokoro" and not kokoro_available():
            return JSONResponse(status_code=404, content={"error": "engine_unavailable"})
        # ====================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # `body.text` is user-typed content (the onboarding voice-speed
        # custom test box), never a fixed product string like SAMPLE_TEXT.
        # Never log it, or any string derived from it. This handler
        # deliberately has no logging call that could touch it.
        # ====================================================================
        #
        # speak_text() is synchronous but non-blocking, same contract as
        # speak_sample() above -- it only resolves config + spawns
        # PlaybackOwner's own daemon worker before returning.
        text = body.text.strip()
        if not text:
            return JSONResponse(
                status_code=422,
                content={"error": "empty_text"},
            )
        if len(text) > SPEAK_TEXT_MAX_CHARS:
            return JSONResponse(
                status_code=422,
                content={"error": "text_too_long"},
            )

        explicit_engine = body.engine
        voice: int | str | None = None
        if explicit_engine is not None:
            from ...speech.engine_specs import get_engine_spec

            spec = get_engine_spec(explicit_engine)
            voice = (
                (
                    getattr(
                        current_config(),
                        "agent_speech_voice",
                        spec.default_voice_id,
                    )
                    if explicit_engine == "supertonic"
                    else getattr(
                        current_config(),
                        "agent_speech_kokoro_voice",
                        spec.default_voice_id,
                    )
                )
                if body.voice is None
                else body.voice
            )
            if not any(item.id == voice for item in spec.voices):
                return JSONResponse(
                    status_code=422,
                    content={"error": "invalid_voice"},
                )

        controller = current_controller()
        if controller is not None:
            if explicit_engine is None:
                controller.speak_text(text)
            else:
                controller.speak_text(
                    text,
                    engine=explicit_engine,
                    voice=voice,
                )
        return {"ok": True}
