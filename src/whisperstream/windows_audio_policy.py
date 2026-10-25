"""Windows Core Audio implementation of recording audio-policy schema v1."""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .audio_policy_contract import (
    WINDOWS_REDUCE_TARGET_SCALAR,
    AudioOutputSnapshot,
    AudioPolicyFailure,
    AudioPolicyMode,
    AudioPolicyResult,
    AudioPolicyStage,
    AudioPolicyTransaction,
    ChimeKind,
    OutputEndpointRef,
    RecordingAudioPolicyAdapter,
)

try:
    import comtypes
    from pycaw.pycaw import AudioUtilities

    _WINDOWS_AUDIO_AVAILABLE = True
except Exception:  # pragma: no cover - exercised by unavailable-host tests
    comtypes = None  # type: ignore[assignment]
    AudioUtilities = None  # type: ignore[assignment]
    _WINDOWS_AUDIO_AVAILABLE = False


@dataclass(frozen=True)
class _ResolvedEndpoint:
    reference: OutputEndpointRef
    interface: Any


def _failure_for_exception(exc: Exception) -> AudioPolicyFailure:
    message = str(exc).casefold()
    if "endpoint" in message or "getdefaultaudioendpoint" in message:
        return AudioPolicyFailure.ENDPOINT_UNAVAILABLE
    return AudioPolicyFailure.OBSERVATION_FAILED


def _endpoint_token(endpoint_id: object) -> str:
    if not isinstance(endpoint_id, str) or not endpoint_id:
        endpoint_id = "windows-default-output"
    digest = hashlib.sha256(endpoint_id.encode("utf-8", errors="replace")).hexdigest()
    return f"windows-output:{digest[:24]}"


def _default_endpoint_provider() -> tuple[str, Any]:
    if not _WINDOWS_AUDIO_AVAILABLE:
        raise RuntimeError("windows audio policy unavailable")
    comtypes.CoInitialize()
    speakers = AudioUtilities.GetSpeakers()
    return str(getattr(speakers, "id", "windows-default-output")), speakers.EndpointVolume


class WindowsRecordingAudioPolicyAdapter:
    """Own one exact output endpoint and restore transaction per generation."""

    def __init__(
        self,
        *,
        feedback: object,
        logger: logging.Logger,
        start_path: Callable[[], str | None],
        stop_path: Callable[[], str | None],
        mode: Callable[[], str] | None = None,
        endpoint_provider: Callable[[], tuple[str, Any]] | None = None,
    ) -> None:
        self._feedback = feedback
        self._logger = logger
        self._start_path = start_path
        self._stop_path = stop_path
        self._mode = mode or (lambda: AudioPolicyMode.OFF.value)
        self._endpoint_provider = endpoint_provider or _default_endpoint_provider
        self._lock = threading.RLock()
        self._active: AudioPolicyTransaction | None = None
        self._active_endpoint: _ResolvedEndpoint | None = None
        self._attempted_generation: int | None = None
        self._attempted_mode = AudioPolicyMode.OFF
        self._restored = False
        self._closed: set[int] = set()

    def _resolve_and_observe(
        self, generation: int, mode: AudioPolicyMode, phase: str
    ) -> tuple[_ResolvedEndpoint | None, AudioPolicyResult | None]:
        try:
            endpoint_id, interface = self._endpoint_provider()
            resolved = _ResolvedEndpoint(
                OutputEndpointRef(_endpoint_token(endpoint_id)), interface
            )
            volume = float(interface.GetMasterVolumeLevelScalar())
            muted = bool(interface.GetMute())
            self._logger.info(
                "playback snapshot phase=%s mode=%s volume=%.3f muted=%s",
                phase,
                mode.value,
                volume,
                str(muted).lower(),
            )
            return resolved, None
        except Exception as exc:
            failure = _failure_for_exception(exc)
            self._logger.warning(
                "playback snapshot failed phase=%s mode=%s reason=%s",
                phase,
                mode.value,
                failure.value,
            )
            return None, AudioPolicyResult(
                generation,
                AudioPolicyStage.CAPTURE,
                failure,
            )

    def play_chime(self, generation: int, kind: ChimeKind) -> AudioPolicyResult:
        if generation < 1:
            raise ValueError("generation must be positive")
        with self._lock:
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
                    # Keep the legacy diagnostic label. Capture is now open in
                    # discard mode; this snapshot precedes capture arming and
                    # the separate post-capture volume transaction.
                    try:
                        configured_mode = AudioPolicyMode(self._mode())
                    except ValueError:
                        configured_mode = AudioPolicyMode.OFF
                    self._resolve_and_observe(
                        generation, configured_mode, "pre_capture"
                    )
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
    ) -> tuple[AudioPolicyTransaction | None, AudioPolicyResult]:
        if generation < 1:
            raise ValueError("generation must be positive")
        with self._lock:
            if generation in self._closed:
                return None, AudioPolicyResult(
                    generation,
                    AudioPolicyStage.CLOSED,
                    AudioPolicyFailure.CLOSED,
                )
            if self._active is not None:
                failure = (
                    AudioPolicyFailure.STALE_GENERATION
                    if self._active.generation != generation
                    else AudioPolicyFailure.MUTATION_FAILED
                )
                return None, AudioPolicyResult(
                    generation,
                    AudioPolicyStage.CAPTURE,
                    failure,
                )
            self._attempted_generation = generation
            self._attempted_mode = mode
            resolved, failure_result = self._resolve_and_observe(
                generation, mode, "post_capture"
            )
            if resolved is None:
                return None, failure_result or AudioPolicyResult(
                    generation,
                    AudioPolicyStage.CAPTURE,
                    AudioPolicyFailure.ENDPOINT_UNAVAILABLE,
                )
            try:
                snapshot = AudioOutputSnapshot(
                    endpoint=resolved.reference,
                    scalar_volume=float(
                        resolved.interface.GetMasterVolumeLevelScalar()
                    ),
                    muted=bool(resolved.interface.GetMute()),
                )
                transaction = AudioPolicyTransaction(generation, mode, snapshot)
                changed = False
                if mode is AudioPolicyMode.MUTE:
                    resolved.interface.SetMute(1, None)
                    changed = not snapshot.muted
                elif mode is AudioPolicyMode.REDUCE:
                    target = snapshot.scalar_volume * WINDOWS_REDUCE_TARGET_SCALAR
                    if target < snapshot.scalar_volume:
                        resolved.interface.SetMasterVolumeLevelScalar(target, None)
                        changed = True
                self._active = transaction
                self._active_endpoint = resolved
                self._restored = False
                self._logger.info(
                    "volume policy applied mode=%s changed=%s",
                    mode.value,
                    str(changed).lower(),
                )
                return transaction, AudioPolicyResult(
                    generation,
                    AudioPolicyStage.MUTATED if changed else AudioPolicyStage.CAPTURE,
                    changed=changed,
                )
            except Exception:
                # A partially completed native mutation must be rolled back
                # immediately; failure cannot leave changed synthetic state.
                rollback_failed = False
                try:
                    if "snapshot" in locals():
                        resolved.interface.SetMute(1 if snapshot.muted else 0, None)
                        resolved.interface.SetMasterVolumeLevelScalar(
                            snapshot.scalar_volume, None
                        )
                except Exception:
                    rollback_failed = True
                self._active = None
                self._active_endpoint = None
                return None, AudioPolicyResult(
                    generation,
                    AudioPolicyStage.CAPTURE,
                    (
                        AudioPolicyFailure.RESTORE_FAILED
                        if rollback_failed
                        else AudioPolicyFailure.MUTATION_FAILED
                    ),
                )

    def restore(self, transaction: AudioPolicyTransaction) -> AudioPolicyResult:
        with self._lock:
            if transaction.generation in self._closed:
                return AudioPolicyResult(
                    transaction.generation,
                    AudioPolicyStage.CLOSED,
                    AudioPolicyFailure.CLOSED,
                )
            if self._active != transaction or self._active_endpoint is None:
                return AudioPolicyResult(
                    transaction.generation,
                    AudioPolicyStage.RESTORED,
                    AudioPolicyFailure.STALE_GENERATION,
                )
            if self._restored:
                return AudioPolicyResult(
                    transaction.generation, AudioPolicyStage.RESTORED
                )

            # Observe the current default without replacing the exact endpoint
            # captured after microphone open.
            self._resolve_and_observe(
                transaction.generation, transaction.mode, "pre_close"
            )
            try:
                endpoint = self._active_endpoint.interface
                snapshot = transaction.snapshot
                if transaction.mode is AudioPolicyMode.MUTE:
                    endpoint.SetMute(1 if snapshot.muted else 0, None)
                if transaction.mode in {AudioPolicyMode.MUTE, AudioPolicyMode.REDUCE}:
                    endpoint.SetMasterVolumeLevelScalar(
                        snapshot.scalar_volume, None
                    )
                self._restored = True
                self._logger.info(
                    "volume policy restored mode=%s", transaction.mode.value
                )
                return AudioPolicyResult(
                    transaction.generation,
                    AudioPolicyStage.RESTORED,
                    changed=transaction.mode is not AudioPolicyMode.OFF,
                )
            except Exception:
                return AudioPolicyResult(
                    transaction.generation,
                    AudioPolicyStage.RESTORED,
                    AudioPolicyFailure.RESTORE_FAILED,
                )

    def close(self, generation: int) -> AudioPolicyResult:
        if generation < 1:
            raise ValueError("generation must be positive")
        with self._lock:
            if generation in self._closed:
                return AudioPolicyResult(
                    generation,
                    AudioPolicyStage.CLOSED,
                    AudioPolicyFailure.CLOSED,
                )
            if self._active is not None and self._active.generation != generation:
                return AudioPolicyResult(
                    generation,
                    AudioPolicyStage.CLOSED,
                    AudioPolicyFailure.STALE_GENERATION,
                )
            if (
                self._active is None
                and self._attempted_generation is not None
                and self._attempted_generation != generation
            ):
                return AudioPolicyResult(
                    generation,
                    AudioPolicyStage.CLOSED,
                    AudioPolicyFailure.STALE_GENERATION,
                )
            if self._active is not None and not self._restored:
                restored = self.restore(self._active)
                if restored.failure is not AudioPolicyFailure.NONE:
                    return AudioPolicyResult(
                        generation,
                        AudioPolicyStage.CLOSED,
                        AudioPolicyFailure.RESTORE_FAILED,
                    )
            observed_lifecycle = (
                self._active is not None or self._attempted_generation == generation
            )
            mode = self._active.mode if self._active is not None else self._attempted_mode
            if self._active is None and self._attempted_generation == generation:
                self._resolve_and_observe(generation, mode, "pre_close")
            if observed_lifecycle:
                self._resolve_and_observe(generation, mode, "post_close")
            self._active = None
            self._active_endpoint = None
            self._restored = False
            self._attempted_generation = None
            self._attempted_mode = AudioPolicyMode.OFF
            self._closed.add(generation)
            return AudioPolicyResult(generation, AudioPolicyStage.CLOSED)


assert isinstance(
    WindowsRecordingAudioPolicyAdapter(
        feedback=object(),
        logger=logging.getLogger(__name__),
        start_path=lambda: None,
        stop_path=lambda: None,
        mode=lambda: AudioPolicyMode.OFF.value,
        endpoint_provider=lambda: ("synthetic", object()),
    ),
    RecordingAudioPolicyAdapter,
)
