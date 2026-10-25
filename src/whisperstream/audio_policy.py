"""Lazy platform selector for the frozen recording audio-policy contract."""

from __future__ import annotations

import logging
import platform
from collections.abc import Callable

from .audio_policy_contract import (
    AudioPolicyFailure,
    AudioPolicyMode,
    AudioPolicyResult,
    AudioPolicyStage,
    ChimeKind,
    RecordingAudioPolicyAdapter,
    RecordingAudioPolicyFactory,
)


class UnavailableRecordingAudioPolicyAdapter:
    """Keep shared chimes available while volume mutation is unsupported."""

    def __init__(
        self,
        *,
        feedback: object,
        start_path: Callable[[], str | None],
        stop_path: Callable[[], str | None],
        mode: Callable[[], str] | None = None,
    ) -> None:
        self._feedback = feedback
        self._start_path = start_path
        self._stop_path = stop_path
        self._mode = mode or (lambda: AudioPolicyMode.OFF.value)
        self._closed: set[int] = set()

    def play_chime(self, generation: int, kind: ChimeKind) -> AudioPolicyResult:
        if generation < 1:
            raise ValueError("generation must be positive")
        if generation in self._closed:
            return AudioPolicyResult(
                generation,
                AudioPolicyStage.CLOSED,
                AudioPolicyFailure.CLOSED,
            )
        stage = (
            AudioPolicyStage.START_CHIME
            if kind is ChimeKind.START
            else AudioPolicyStage.STOP_CHIME
        )
        try:
            if kind is ChimeKind.START:
                played = self._feedback.play_start_blocking(self._start_path())
            else:
                played = self._feedback.play_stop(self._stop_path())
        except Exception:
            return AudioPolicyResult(
                generation,
                stage,
                AudioPolicyFailure.MUTATION_FAILED,
            )
        if played is False:
            return AudioPolicyResult(
                generation,
                stage,
                AudioPolicyFailure.UNSUPPORTED,
            )
        return AudioPolicyResult(generation, stage)

    def begin(
        self, generation: int, mode: AudioPolicyMode
    ) -> tuple[None, AudioPolicyResult]:
        del mode
        return None, AudioPolicyResult(
            generation,
            AudioPolicyStage.CAPTURE,
            AudioPolicyFailure.UNSUPPORTED,
        )

    def restore(self, transaction) -> AudioPolicyResult:
        return AudioPolicyResult(
            transaction.generation,
            AudioPolicyStage.RESTORED,
            AudioPolicyFailure.UNSUPPORTED,
        )

    def close(self, generation: int) -> AudioPolicyResult:
        if generation < 1:
            raise ValueError("generation must be positive")
        if generation in self._closed:
            return AudioPolicyResult(
                generation,
                AudioPolicyStage.CLOSED,
                AudioPolicyFailure.CLOSED,
            )
        self._closed.add(generation)
        return AudioPolicyResult(generation, AudioPolicyStage.CLOSED)


class PlatformRecordingAudioPolicyFactory:
    """Select a policy owner without eagerly resolving an output endpoint."""

    def __init__(
        self,
        *,
        feedback: object,
        logger: logging.Logger,
        start_path: Callable[[], str | None],
        stop_path: Callable[[], str | None],
        mode: Callable[[], str] | None = None,
        system: str | None = None,
        machine: str | None = None,
    ) -> None:
        self._feedback = feedback
        self._logger = logger
        self._start_path = start_path
        self._stop_path = stop_path
        self._mode = mode or (lambda: AudioPolicyMode.OFF.value)
        self._system = system
        self._machine = machine

    def create(self) -> RecordingAudioPolicyAdapter:
        system = self._system or platform.system()
        machine = (self._machine or platform.machine()).lower()
        if system == "Windows" and machine in {"amd64", "x86_64"}:
            from .windows_audio_policy import WindowsRecordingAudioPolicyAdapter

            return WindowsRecordingAudioPolicyAdapter(
                feedback=self._feedback,
                logger=self._logger,
                start_path=self._start_path,
                stop_path=self._stop_path,
                mode=self._mode,
            )
        if system == "Darwin" and machine == "arm64":
            from .macos_audio_policy import MacosRecordingAudioPolicyAdapter

            return MacosRecordingAudioPolicyAdapter(
                feedback=self._feedback,
                logger=self._logger,
                start_path=self._start_path,
                stop_path=self._stop_path,
                mode=self._mode,
            )
        return UnavailableRecordingAudioPolicyAdapter(
            feedback=self._feedback,
            start_path=self._start_path,
            stop_path=self._stop_path,
            mode=self._mode,
        )


assert isinstance(
    PlatformRecordingAudioPolicyFactory(
        feedback=object(),
        logger=logging.getLogger(__name__),
        start_path=lambda: None,
        stop_path=lambda: None,
    ),
    RecordingAudioPolicyFactory,
)
