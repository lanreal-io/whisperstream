"""Lazy platform selection for the frozen TTS playback contract."""

from __future__ import annotations

import platform
from collections.abc import Iterable
from typing import Any

from .speech.selection import SelectionResolution, normalize_selection

from .tts_playback_contract import (
    SpeechFailure,
    SpeechPlaybackAdapter,
    SpeechReceipt,
    SpeechRequest,
    SpeechStage,
    SpeechSynthesisAdapter,
    SpeechTerminalReason,
)


def speech_engine_supported(engine_key: str, *, platform_name: str | None = None) -> bool:
    """Static runtime support, separate from installed assets and health."""
    system = platform_name or platform.system()
    return system in {"Darwin", "Windows"} and engine_key in {"kokoro", "supertonic"}


class UnavailableSpeechSynthesisAdapter:
    """Truthful synthesis boundary for platforms without a TTS adapter."""

    class _UnavailableSelectionPolicy:
        @staticmethod
        def resolve(
            requested: object,
            *,
            supertonic_voice: object,
            kokoro_voice: object,
        ) -> SelectionResolution:
            del supertonic_voice, kokoro_voice
            return SelectionResolution(
                normalize_selection(requested),
                (),
                "unsupported",
            )

    selection_policy = _UnavailableSelectionPolicy()

    def synthesize(self, request: SpeechRequest) -> Iterable[object]:
        del request
        return ()

    def close(self) -> None:
        return None

    def shutdown(self, timeout: float = 2.0) -> bool:
        del timeout
        return True

    @staticmethod
    def acquire(engine_key: str) -> object:
        del engine_key
        raise RuntimeError("speech synthesis is unsupported")

    @staticmethod
    def mark_unhealthy(engine_key: str, reason: str) -> None:
        del engine_key, reason

    @staticmethod
    def mark_healthy(engine_key: str) -> None:
        del engine_key

    @staticmethod
    def health_reason(engine_key: str) -> None:
        del engine_key
        return None


class UnavailableSpeechPlaybackAdapter:
    """Truthful output boundary for platforms without playback support."""

    def play(
        self,
        request: SpeechRequest,
        buffers: Iterable[object],
    ) -> SpeechReceipt:
        del buffers
        return SpeechReceipt(
            request.completion_id,
            request.generation,
            SpeechStage.FAILED,
            SpeechTerminalReason.UNAVAILABLE_OUTPUT,
            SpeechFailure.OUTPUT_UNAVAILABLE,
        )

    def interrupt(
        self,
        generation: int,
        reason: SpeechTerminalReason,
    ) -> None:
        del generation, reason

    def close(self) -> None:
        return None

    @staticmethod
    def abort_stream(stream: object | None) -> None:
        del stream

    @staticmethod
    def open_output_stream(sample_rate: int) -> object:
        del sample_rate
        raise RuntimeError("speech output is unsupported")

    @staticmethod
    def claim_stream(generation: int, stream: object) -> bool:
        del generation, stream
        return False

    @staticmethod
    def release_stream(generation: int, stream: object) -> None:
        del generation, stream

    @staticmethod
    def finish_generation(generation: int) -> None:
        del generation


def _is_windows_x64(system: str | None, machine: str | None) -> bool:
    return (system or platform.system()) == "Windows" and (
        machine or platform.machine()
    ).lower() in {"amd64", "x86_64"}


def _is_darwin_arm64(system: str | None, machine: str | None) -> bool:
    return (system or platform.system()) == "Darwin" and (
        machine or platform.machine()
    ).lower() in {"arm64", "aarch64"}


def create_speech_synthesis_adapter(
    *,
    engine_manager: object,
    logger: object | None = None,
    system: str | None = None,
    machine: str | None = None,
) -> SpeechSynthesisAdapter:
    """Select the platform model owner without importing native adapters eagerly."""

    if _is_windows_x64(system, machine):
        from .windows_tts_playback import WindowsSpeechSynthesisAdapter

        return WindowsSpeechSynthesisAdapter(
            engine_manager=engine_manager,
            logger=logger,
        )
    if _is_darwin_arm64(system, machine):
        from .macos_tts_playback import MacOSSpeechSynthesisAdapter

        return MacOSSpeechSynthesisAdapter(
            engine_manager=engine_manager,
            logger=logger,
        )
    return UnavailableSpeechSynthesisAdapter()


def create_speech_playback_adapter(
    *,
    logger: object | None = None,
    system: str | None = None,
    machine: str | None = None,
    **kwargs: Any,
) -> SpeechPlaybackAdapter:
    """Select the platform current-output owner lazily."""

    platform_provider = kwargs.get("platform_provider")
    runtime_platform = None
    if system is None and callable(platform_provider):
        runtime_platform = str(platform_provider()).casefold()
    is_windows = (
        runtime_platform == "win32"
        if runtime_platform is not None
        else _is_windows_x64(system, machine)
    )
    is_darwin = (
        runtime_platform == "darwin"
        if runtime_platform is not None
        else _is_darwin_arm64(system, machine)
    )

    if is_windows:
        from .windows_tts_playback import WindowsSpeechPlaybackAdapter

        return WindowsSpeechPlaybackAdapter(logger=logger, **kwargs)
    if is_darwin:
        from .macos_tts_playback import MacOSSpeechPlaybackAdapter

        # PlaybackOwner supplies Windows-only compatibility seams. Keep them out
        # of the Darwin adapter so native endpoint objects never cross owners.
        macos_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in {"sounddevice_module", "current_output_resolver"}
        }
        return MacOSSpeechPlaybackAdapter(logger=logger, **macos_kwargs)
    if runtime_platform is not None:
        # PlaybackOwner's legacy seam deliberately retains the generic
        # sounddevice path used by non-Windows tests and unsupported hosts.
        from .windows_tts_playback import WindowsSpeechPlaybackAdapter

        return WindowsSpeechPlaybackAdapter(logger=logger, **kwargs)
    return UnavailableSpeechPlaybackAdapter()


assert isinstance(UnavailableSpeechSynthesisAdapter(), SpeechSynthesisAdapter)
assert isinstance(UnavailableSpeechPlaybackAdapter(), SpeechPlaybackAdapter)


__all__ = [
    "UnavailableSpeechPlaybackAdapter",
    "UnavailableSpeechSynthesisAdapter",
    "create_speech_playback_adapter",
    "create_speech_synthesis_adapter",
]
