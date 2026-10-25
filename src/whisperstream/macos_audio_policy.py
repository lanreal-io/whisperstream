"""Darwin CoreAudio recording-output policy for schema v1.

The native bridge reads only the current default output. It does not enumerate
outputs or resolve their names. CoreAudio object IDs, UIDs, property addresses,
and writable controls remain private to this adapter. One post-capture output
and its scalar/mute snapshot stay attached to one positive generation until an
exact restoration succeeds or the adapter reports a typed failure.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import platform
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .audio_policy_contract import (
    AUDIO_REDUCE_RATIO,
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


_CORE_AUDIO_FRAMEWORK = Path(
    "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
)
_CORE_FOUNDATION_FRAMEWORK = Path(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
_CF_STRING_ENCODING_UTF8 = 0x08000100
_AUDIO_OBJECT_SYSTEM = 1


def _fourcc(value: str) -> int:
    if len(value) != 4 or not value.isascii():
        raise ValueError("CoreAudio property codes must contain four ASCII bytes")
    return int.from_bytes(value.encode("ascii"), "big")


_SCOPE_GLOBAL = _fourcc("glob")
_SCOPE_OUTPUT = _fourcc("outp")
_SELECTOR_DEFAULT_OUTPUT = _fourcc("dOut")
_SELECTOR_UID = _fourcc("uid ")
_SELECTOR_SCALAR_VOLUME = _fourcc("volm")
_SELECTOR_MUTE = _fourcc("mute")
_ELEMENT_MAIN = 0


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = (
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    )


@dataclass(frozen=True, slots=True)
class _MacosOutputObservation:
    handle: object = field(repr=False)
    reference: OutputEndpointRef = field(repr=False)
    scalar_volume: float | None
    muted: bool | None
    volume_writable: bool
    mute_writable: bool


class _CoreAudioOutputBridge(Protocol):
    def resolve_current_output(self) -> _MacosOutputObservation: ...

    def observe(
        self, observation: _MacosOutputObservation
    ) -> tuple[float | None, bool | None]: ...

    def set_scalar(
        self, observation: _MacosOutputObservation, scalar: float
    ) -> None: ...

    def set_muted(
        self, observation: _MacosOutputObservation, muted: bool
    ) -> None: ...


class _NativeCoreAudioOutputBridge:
    """Minimal passive/current-output bridge plus exact property writes."""

    def __init__(self) -> None:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("native CoreAudio output policy requires Darwin arm64")
        self._audio = ctypes.CDLL(str(_CORE_AUDIO_FRAMEWORK))
        self._cf = ctypes.CDLL(str(_CORE_FOUNDATION_FRAMEWORK))
        self._audio.AudioObjectHasProperty.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_AudioObjectPropertyAddress),
        ]
        self._audio.AudioObjectHasProperty.restype = ctypes.c_ubyte
        self._audio.AudioObjectIsPropertySettable.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_AudioObjectPropertyAddress),
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        self._audio.AudioObjectIsPropertySettable.restype = ctypes.c_int32
        self._audio.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_AudioObjectPropertyAddress),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self._audio.AudioObjectGetPropertyData.restype = ctypes.c_int32
        self._audio.AudioObjectSetPropertyData.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_AudioObjectPropertyAddress),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self._audio.AudioObjectSetPropertyData.restype = ctypes.c_int32
        self._cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        self._cf.CFStringGetLength.restype = ctypes.c_long
        self._cf.CFStringGetMaximumSizeForEncoding.argtypes = [
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        self._cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._cf.CFStringGetCString.restype = ctypes.c_bool
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._cf.CFRelease.restype = None

    @staticmethod
    def _address(
        selector: int,
        *,
        scope: int = _SCOPE_OUTPUT,
    ) -> _AudioObjectPropertyAddress:
        return _AudioObjectPropertyAddress(selector, scope, _ELEMENT_MAIN)

    def _read(
        self,
        object_id: int,
        selector: int,
        value: object,
        *,
        scope: int = _SCOPE_OUTPUT,
    ) -> object:
        address = self._address(selector, scope=scope)
        size = ctypes.c_uint32(ctypes.sizeof(value))
        status = self._audio.AudioObjectGetPropertyData(
            object_id,
            ctypes.byref(address),
            0,
            None,
            ctypes.byref(size),
            ctypes.byref(value),
        )
        if status != 0:
            raise RuntimeError("CoreAudio output property unavailable")
        return value

    def _write(self, object_id: int, selector: int, value: object) -> None:
        address = self._address(selector)
        status = self._audio.AudioObjectSetPropertyData(
            object_id,
            ctypes.byref(address),
            0,
            None,
            ctypes.sizeof(value),
            ctypes.byref(value),
        )
        if status != 0:
            raise RuntimeError("CoreAudio output property write failed")

    def _is_writable(self, object_id: int, selector: int) -> bool:
        address = self._address(selector)
        if not self._audio.AudioObjectHasProperty(object_id, ctypes.byref(address)):
            return False
        writable = ctypes.c_ubyte()
        status = self._audio.AudioObjectIsPropertySettable(
            object_id,
            ctypes.byref(address),
            ctypes.byref(writable),
        )
        return status == 0 and bool(writable.value)

    def _uid(self, object_id: int) -> str:
        reference = self._read(
            object_id,
            _SELECTOR_UID,
            ctypes.c_void_p(),
            scope=_SCOPE_GLOBAL,
        ).value
        if not reference:
            raise RuntimeError("CoreAudio output identity unavailable")
        try:
            length = self._cf.CFStringGetLength(reference)
            capacity = (
                self._cf.CFStringGetMaximumSizeForEncoding(
                    length, _CF_STRING_ENCODING_UTF8
                )
                + 1
            )
            buffer = ctypes.create_string_buffer(capacity)
            if not self._cf.CFStringGetCString(
                reference,
                buffer,
                capacity,
                _CF_STRING_ENCODING_UTF8,
            ):
                raise RuntimeError("CoreAudio output identity conversion failed")
            value = buffer.value.decode("utf-8")
            if not value:
                raise RuntimeError("CoreAudio output identity unavailable")
            return value
        finally:
            self._cf.CFRelease(reference)

    def resolve_current_output(self) -> _MacosOutputObservation:
        object_id = int(
            self._read(
                _AUDIO_OBJECT_SYSTEM,
                _SELECTOR_DEFAULT_OUTPUT,
                ctypes.c_uint32(),
                scope=_SCOPE_GLOBAL,
            ).value
        )
        if object_id <= 0:
            raise RuntimeError("CoreAudio default output unavailable")
        uid = self._uid(object_id)
        digest = hashlib.sha256(uid.encode("utf-8", errors="strict")).hexdigest()
        scalar = self._read_optional(
            object_id, _SELECTOR_SCALAR_VOLUME, ctypes.c_float()
        )
        muted = self._read_optional(object_id, _SELECTOR_MUTE, ctypes.c_uint32())
        scalar_value = float(scalar.value) if scalar is not None else None
        muted_value = bool(muted.value) if muted is not None else None
        if scalar_value is not None and not 0.0 <= scalar_value <= 1.0:
            scalar_value = None
        return _MacosOutputObservation(
            handle=object_id,
            reference=OutputEndpointRef(f"macos-output:{digest[:24]}"),
            scalar_volume=scalar_value,
            muted=muted_value,
            volume_writable=self._is_writable(object_id, _SELECTOR_SCALAR_VOLUME),
            mute_writable=self._is_writable(object_id, _SELECTOR_MUTE),
        )

    def _read_optional(
        self, object_id: int, selector: int, value: object
    ) -> object | None:
        address = self._address(selector)
        if not self._audio.AudioObjectHasProperty(object_id, ctypes.byref(address)):
            return None
        try:
            return self._read(object_id, selector, value)
        except Exception:  # noqa: BLE001 - optional output control
            return None

    def observe(
        self, observation: _MacosOutputObservation
    ) -> tuple[float | None, bool | None]:
        object_id = int(observation.handle)
        scalar = self._read_optional(
            object_id, _SELECTOR_SCALAR_VOLUME, ctypes.c_float()
        )
        muted = self._read_optional(object_id, _SELECTOR_MUTE, ctypes.c_uint32())
        scalar_value = float(scalar.value) if scalar is not None else None
        if scalar_value is not None and not 0.0 <= scalar_value <= 1.0:
            scalar_value = None
        return scalar_value, (bool(muted.value) if muted is not None else None)

    def set_scalar(
        self, observation: _MacosOutputObservation, scalar: float
    ) -> None:
        if not 0.0 <= scalar <= 1.0:
            raise ValueError("scalar volume must be between zero and one")
        self._write(
            int(observation.handle),
            _SELECTOR_SCALAR_VOLUME,
            ctypes.c_float(scalar),
        )

    def set_muted(
        self, observation: _MacosOutputObservation, muted: bool
    ) -> None:
        self._write(
            int(observation.handle),
            _SELECTOR_MUTE,
            ctypes.c_uint32(1 if muted else 0),
        )


class MacosRecordingAudioPolicyAdapter:
    """Own one exact CoreAudio output transaction per recording generation."""

    def __init__(
        self,
        *,
        feedback: object,
        logger: logging.Logger,
        start_path: Callable[[], str | None],
        stop_path: Callable[[], str | None],
        mode: Callable[[], str] | None = None,
        _bridge: _CoreAudioOutputBridge | None = None,
    ) -> None:
        self._feedback = feedback
        self._logger = logger
        self._start_path = start_path
        self._stop_path = stop_path
        self._mode = mode or (lambda: AudioPolicyMode.OFF.value)
        self._bridge = _bridge or _NativeCoreAudioOutputBridge()
        self._lock = threading.RLock()
        self._active_transaction: AudioPolicyTransaction | None = None
        self._active_output: _MacosOutputObservation | None = None
        self._attempted_generation: int | None = None
        self._attempted_mode = AudioPolicyMode.OFF
        self._restored = False
        self._changed = False
        self._closed: set[int] = set()

    def play_chime(self, generation: int, kind: ChimeKind) -> AudioPolicyResult:
        if generation < 1:
            raise ValueError("generation must be positive")
        with self._lock:
            if generation in self._closed:
                return AudioPolicyResult(
                    generation, AudioPolicyStage.CLOSED, AudioPolicyFailure.CLOSED
                )
            stage = (
                AudioPolicyStage.START_CHIME
                if kind is ChimeKind.START
                else AudioPolicyStage.STOP_CHIME
            )
            try:
                played = (
                    self._feedback.play_start_blocking(self._start_path())
                    if kind is ChimeKind.START
                    else self._feedback.play_stop(self._stop_path())
                )
            except Exception:  # noqa: BLE001 - recording path never raises
                return AudioPolicyResult(
                    generation, stage, AudioPolicyFailure.MUTATION_FAILED
                )
            if played is False:
                return AudioPolicyResult(
                    generation, stage, AudioPolicyFailure.UNSUPPORTED
                )
            return AudioPolicyResult(generation, stage)

    @staticmethod
    def _supports(
        mode: AudioPolicyMode, output: _MacosOutputObservation
    ) -> bool:
        if mode is AudioPolicyMode.OFF:
            return True
        if mode is AudioPolicyMode.REDUCE:
            return output.scalar_volume is not None and output.volume_writable
        return (
            output.scalar_volume is not None
            and output.muted is not None
            and output.volume_writable
            and output.mute_writable
        )

    def begin(
        self, generation: int, mode: AudioPolicyMode
    ) -> tuple[AudioPolicyTransaction | None, AudioPolicyResult]:
        if generation < 1:
            raise ValueError("generation must be positive")
        with self._lock:
            if generation in self._closed:
                return None, AudioPolicyResult(
                    generation, AudioPolicyStage.CLOSED, AudioPolicyFailure.CLOSED
                )
            if self._active_transaction is not None:
                failure = (
                    AudioPolicyFailure.STALE_GENERATION
                    if self._active_transaction.generation != generation
                    else AudioPolicyFailure.MUTATION_FAILED
                )
                return None, AudioPolicyResult(
                    generation, AudioPolicyStage.CAPTURE, failure
                )
            self._attempted_generation = generation
            self._attempted_mode = mode
            try:
                output = self._bridge.resolve_current_output()
            except Exception:  # noqa: BLE001 - endpoint absence is typed
                return None, AudioPolicyResult(
                    generation,
                    AudioPolicyStage.CAPTURE,
                    AudioPolicyFailure.ENDPOINT_UNAVAILABLE,
                )
            if not self._supports(mode, output):
                return None, AudioPolicyResult(
                    generation,
                    AudioPolicyStage.CAPTURE,
                    AudioPolicyFailure.UNSUPPORTED,
                )
            snapshot = AudioOutputSnapshot(
                output.reference, output.scalar_volume, output.muted
            )
            transaction = AudioPolicyTransaction(generation, mode, snapshot)
            changed = False
            try:
                if mode is AudioPolicyMode.REDUCE:
                    assert snapshot.scalar_volume is not None
                    target = snapshot.scalar_volume * AUDIO_REDUCE_RATIO
                    if target < snapshot.scalar_volume:
                        self._bridge.set_scalar(output, target)
                        changed = True
                elif mode is AudioPolicyMode.MUTE and snapshot.muted is False:
                    self._bridge.set_muted(output, True)
                    changed = True
            except Exception:  # noqa: BLE001 - rollback has its own typed result
                rollback_failed = not self._restore_snapshot(output, transaction)
                if rollback_failed:
                    # A setter may report failure after partially changing the
                    # endpoint. Keep the exact private handle and snapshot so
                    # close can retry instead of losing ownership of the state.
                    self._active_transaction = transaction
                    self._active_output = output
                    self._restored = False
                    self._changed = True
                return None, AudioPolicyResult(
                    generation,
                    AudioPolicyStage.CAPTURE,
                    (
                        AudioPolicyFailure.RESTORE_FAILED
                        if rollback_failed
                        else AudioPolicyFailure.MUTATION_FAILED
                    ),
                )
            self._active_transaction = transaction
            self._active_output = output
            self._restored = False
            self._changed = changed
            self._logger.info(
                "[AudioPolicy] mutation applied platform=macos mode=%s changed=%s",
                mode.value,
                str(changed).lower(),
            )
            return transaction, AudioPolicyResult(
                generation,
                AudioPolicyStage.MUTATED if changed else AudioPolicyStage.CAPTURE,
                changed=changed,
            )

    def _restore_snapshot(
        self,
        output: _MacosOutputObservation,
        transaction: AudioPolicyTransaction,
    ) -> bool:
        try:
            snapshot = transaction.snapshot
            if transaction.mode is AudioPolicyMode.MUTE:
                assert snapshot.muted is not None
                self._bridge.set_muted(output, snapshot.muted)
            if transaction.mode in {AudioPolicyMode.REDUCE, AudioPolicyMode.MUTE}:
                assert snapshot.scalar_volume is not None
                self._bridge.set_scalar(output, snapshot.scalar_volume)
            return True
        except Exception:  # noqa: BLE001 - caller returns RESTORE_FAILED
            return False

    def restore(self, transaction: AudioPolicyTransaction) -> AudioPolicyResult:
        with self._lock:
            if transaction.generation in self._closed:
                return AudioPolicyResult(
                    transaction.generation,
                    AudioPolicyStage.CLOSED,
                    AudioPolicyFailure.CLOSED,
                )
            if (
                transaction != self._active_transaction
                or self._active_output is None
            ):
                return AudioPolicyResult(
                    transaction.generation,
                    AudioPolicyStage.RESTORED,
                    AudioPolicyFailure.STALE_GENERATION,
                )
            if self._restored:
                return AudioPolicyResult(
                    transaction.generation, AudioPolicyStage.RESTORED
                )
            restored = self._restore_snapshot(self._active_output, transaction)
            if not restored:
                # Sleep/wake and endpoint reconnects can make the first write
                # transiently unavailable. Retry this retained endpoint once,
                # never the process's possibly changed current default.
                restored = self._restore_snapshot(self._active_output, transaction)
            if not restored:
                return AudioPolicyResult(
                    transaction.generation,
                    AudioPolicyStage.RESTORED,
                    AudioPolicyFailure.RESTORE_FAILED,
                )
            self._restored = True
            self._logger.info(
                "[AudioPolicy] snapshot restored platform=macos mode=%s",
                transaction.mode.value,
            )
            return AudioPolicyResult(
                transaction.generation,
                AudioPolicyStage.RESTORED,
                changed=self._changed,
            )

    def close(self, generation: int) -> AudioPolicyResult:
        if generation < 1:
            raise ValueError("generation must be positive")
        with self._lock:
            if generation in self._closed:
                return AudioPolicyResult(
                    generation, AudioPolicyStage.CLOSED, AudioPolicyFailure.CLOSED
                )
            if (
                self._active_transaction is not None
                and self._active_transaction.generation != generation
            ):
                return AudioPolicyResult(
                    generation,
                    AudioPolicyStage.CLOSED,
                    AudioPolicyFailure.STALE_GENERATION,
                )
            if (
                self._active_transaction is None
                and self._attempted_generation is not None
                and self._attempted_generation != generation
            ):
                return AudioPolicyResult(
                    generation,
                    AudioPolicyStage.CLOSED,
                    AudioPolicyFailure.STALE_GENERATION,
                )
            if self._active_transaction is not None and not self._restored:
                restored = self.restore(self._active_transaction)
                if restored.failure is not AudioPolicyFailure.NONE:
                    return AudioPolicyResult(
                        generation,
                        AudioPolicyStage.CLOSED,
                        AudioPolicyFailure.RESTORE_FAILED,
                    )
            self._active_transaction = None
            self._active_output = None
            self._attempted_generation = None
            self._attempted_mode = AudioPolicyMode.OFF
            self._restored = False
            self._changed = False
            self._closed.add(generation)
            return AudioPolicyResult(generation, AudioPolicyStage.CLOSED)


assert isinstance(
    MacosRecordingAudioPolicyAdapter(
        feedback=object(),
        logger=logging.getLogger(__name__),
        start_path=lambda: None,
        stop_path=lambda: None,
        _bridge=object(),  # type: ignore[arg-type]
    ),
    RecordingAudioPolicyAdapter,
)


def probe_current_output_policy_support(
    *, _bridge: _CoreAudioOutputBridge | None = None
) -> bool:
    """Passively confirm the current output supports exact reduce and mute.

    Capability hydration may call this query, but it never writes a property,
    registers a listener, opens a stream, enumerates outputs, or retains the
    opaque observation after return.
    """

    try:
        output = (_bridge or _NativeCoreAudioOutputBridge()).resolve_current_output()
        return (
            output.scalar_volume is not None
            and output.muted is not None
            and output.volume_writable
            and output.mute_writable
        )
    except Exception:  # noqa: BLE001 - capability probe fails closed
        return False
