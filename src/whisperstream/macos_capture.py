"""macOS CoreAudio identity and PortAudio microphone capture adapter.

CoreAudio device IDs and UIDs, PortAudio indices, C structures, stream objects,
and native failures remain private to this module.  Shared orchestration sees
only the schema-v1 values from :mod:`whisperstream.capture_contract`.

Enumeration and permission checks are read-only.  This module never requests
microphone access or changes a CoreAudio device parameter.  Opening a stream is
allowed only when the separate passive permission report already says access is
granted.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import platform
import queue
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np
import sounddevice as sd

from whisperstream.capture_contract import (
    CAPTURE_CHANNELS,
    CAPTURE_DTYPE,
    CAPTURE_SAMPLE_RATE,
    CAPTURE_STOP_TIMEOUT_SECONDS,
    CAPTURE_START_TIMEOUT_SECONDS,
    CaptureCompletion,
    CaptureCompletionStatus,
    CaptureDevice,
    CaptureFailure,
    CaptureFormat,
    CaptureInventory,
    CaptureRequest,
    CaptureSelection,
    CaptureSelectionResolution,
    CaptureSession,
    CaptureStartOutcome,
    CaptureTerminalCallback,
    CaptureTerminalEvent,
    allocate_capture_owner_epoch,
)
from whisperstream.chunking import ChunkBuffer
from whisperstream.permission_contract import PermissionKind, PermissionState


logger = logging.getLogger(__name__)

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
_SELECTOR_DEVICES = _fourcc("dev#")
_SELECTOR_DEFAULT_INPUT = _fourcc("dIn ")
_SELECTOR_UID = _fourcc("uid ")
_SELECTOR_NAME = _fourcc("lnam")
_SELECTOR_NOMINAL_RATE = _fourcc("nsrt")
_SELECTOR_AVAILABLE_RATES = _fourcc("nsr#")


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = (
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    )


class _AudioValueRange(ctypes.Structure):
    _fields_ = (
        ("minimum", ctypes.c_double),
        ("maximum", ctypes.c_double),
    )


@dataclass(frozen=True, slots=True)
class _CoreAudioDevice:
    object_id: int
    uid: str = field(repr=False)
    name: str
    nominal_sample_rate: float
    available_sample_rate_ranges: tuple[tuple[float, float], ...]
    is_default: bool


@dataclass(frozen=True, slots=True)
class _NativeInputDevice:
    index: int
    candidate_indices: tuple[int, ...]
    uid: str = field(repr=False)
    name: str
    max_input_channels: int
    default_sample_rate: float
    logical_id: str = field(repr=False)
    is_default: bool
    native_sample_rate_ranges: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class MacCaptureFormatSupport:
    """Content-safe format discovery for one opaque logical input."""

    logical_id: str = field(repr=False)
    native_sample_rate: float
    native_sample_rate_ranges: tuple[tuple[float, float], ...]
    canonical_format_supported: bool | None


@dataclass(frozen=True, slots=True)
class PreviewAudioSnapshot:
    """One immutable, bounded view of preview-only captured audio."""

    audio: np.ndarray
    total_samples: int
    start_sample: int


class _CoreAudioBridge(Protocol):
    def enumerate_devices(self) -> tuple[_CoreAudioDevice, ...]: ...

    def stream_input_uid(self, stream: object) -> str: ...


class _NativeCoreAudioBridge:
    """Minimal direct bridge for stable identity and format metadata."""

    def __init__(self) -> None:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("native CoreAudio adapter requires Darwin arm64")
        self._audio = ctypes.CDLL(str(_CORE_AUDIO_FRAMEWORK))
        self._cf = ctypes.CDLL(str(_CORE_FOUNDATION_FRAMEWORK))
        self._portaudio: ctypes.CDLL | None = None

        self._audio.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_AudioObjectPropertyAddress),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self._audio.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        self._audio.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_AudioObjectPropertyAddress),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self._audio.AudioObjectGetPropertyData.restype = ctypes.c_int32

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
    def _address(selector: int) -> _AudioObjectPropertyAddress:
        return _AudioObjectPropertyAddress(selector, _SCOPE_GLOBAL, 0)

    def _data_size(self, object_id: int, selector: int) -> int:
        address = self._address(selector)
        size = ctypes.c_uint32()
        status = self._audio.AudioObjectGetPropertyDataSize(
            object_id,
            ctypes.byref(address),
            0,
            None,
            ctypes.byref(size),
        )
        if status != 0:
            raise RuntimeError("CoreAudio property size unavailable")
        return int(size.value)

    def _read(self, object_id: int, selector: int, value: object) -> object:
        address = self._address(selector)
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
            raise RuntimeError("CoreAudio property unavailable")
        return value

    def _string(self, object_id: int, selector: int) -> str:
        reference = self._read(object_id, selector, ctypes.c_void_p()).value
        if not reference:
            raise RuntimeError("CoreAudio string unavailable")
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
                raise RuntimeError("CoreAudio string conversion failed")
            value = buffer.value.decode("utf-8")
            if not value:
                raise RuntimeError("CoreAudio string is empty")
            return value
        finally:
            self._cf.CFRelease(reference)

    def _device_ids(self) -> tuple[int, ...]:
        size = self._data_size(_AUDIO_OBJECT_SYSTEM, _SELECTOR_DEVICES)
        if size == 0 or size % ctypes.sizeof(ctypes.c_uint32):
            return ()
        values_type = ctypes.c_uint32 * (size // ctypes.sizeof(ctypes.c_uint32))
        values = self._read(
            _AUDIO_OBJECT_SYSTEM,
            _SELECTOR_DEVICES,
            values_type(),
        )
        return tuple(int(value) for value in values)

    def _default_input_id(self) -> int | None:
        try:
            value = self._read(
                _AUDIO_OBJECT_SYSTEM,
                _SELECTOR_DEFAULT_INPUT,
                ctypes.c_uint32(),
            )
        except Exception:  # noqa: BLE001 - read-only default probe is best effort
            return None
        return int(value.value) if int(value.value) > 0 else None

    def _sample_rate_ranges(
        self, object_id: int
    ) -> tuple[tuple[float, float], ...]:
        try:
            size = self._data_size(object_id, _SELECTOR_AVAILABLE_RATES)
            item_size = ctypes.sizeof(_AudioValueRange)
            if size == 0 or size % item_size:
                return ()
            values_type = _AudioValueRange * (size // item_size)
            values = self._read(
                object_id,
                _SELECTOR_AVAILABLE_RATES,
                values_type(),
            )
            return tuple(
                (float(value.minimum), float(value.maximum)) for value in values
            )
        except Exception:  # noqa: BLE001 - optional format metadata
            return ()

    def enumerate_devices(self) -> tuple[_CoreAudioDevice, ...]:
        default_id = self._default_input_id()
        devices: list[_CoreAudioDevice] = []
        for object_id in self._device_ids():
            try:
                nominal = self._read(
                    object_id,
                    _SELECTOR_NOMINAL_RATE,
                    ctypes.c_double(),
                )
                devices.append(
                    _CoreAudioDevice(
                        object_id=object_id,
                        uid=self._string(object_id, _SELECTOR_UID),
                        name=self._string(object_id, _SELECTOR_NAME),
                        nominal_sample_rate=float(nominal.value),
                        available_sample_rate_ranges=self._sample_rate_ranges(
                            object_id
                        ),
                        is_default=object_id == default_id,
                    )
                )
            except Exception:  # noqa: BLE001 - one invalid device cannot hide peers
                continue
        return tuple(devices)

    def stream_input_uid(self, stream: object) -> str:
        pointer = getattr(stream, "_ptr", None)
        if pointer is None:
            raise RuntimeError("PortAudio stream pointer unavailable")
        if self._portaudio is None:
            library_name = getattr(sd, "_libname", None)
            if not isinstance(library_name, str) or not library_name:
                raise RuntimeError("PortAudio library path unavailable")
            self._portaudio = ctypes.CDLL(library_name)
            query = self._portaudio.PaMacCore_GetStreamInputDevice
            query.argtypes = [ctypes.c_void_p]
            query.restype = ctypes.c_uint32
        raw_pointer = int(sd._ffi.cast("uintptr_t", pointer))
        object_id = int(
            self._portaudio.PaMacCore_GetStreamInputDevice(
                ctypes.c_void_p(raw_pointer)
            )
        )
        if object_id <= 0:
            raise RuntimeError("CoreAudio stream input unavailable")
        return self._string(object_id, _SELECTOR_UID)


class _PreviewAudioRing:
    def __init__(self, max_samples: int) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        self._max_samples = max_samples
        self._chunks: deque[np.ndarray] = deque()
        self._stored_samples = 0
        self._total_samples = 0
        self._lock = threading.Lock()

    def append(self, audio: np.ndarray) -> None:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return
        with self._lock:
            self._total_samples += samples.size
            if samples.size >= self._max_samples:
                self._chunks.clear()
                kept = samples[-self._max_samples :].copy()
                self._chunks.append(kept)
                self._stored_samples = kept.size
                return
            self._chunks.append(samples)
            self._stored_samples += samples.size
            excess = self._stored_samples - self._max_samples
            while excess > 0 and self._chunks:
                first = self._chunks[0]
                if first.size <= excess:
                    self._chunks.popleft()
                    self._stored_samples -= first.size
                    excess -= first.size
                else:
                    self._chunks[0] = first[excess:].copy()
                    self._stored_samples -= excess
                    excess = 0

    def snapshot(self) -> PreviewAudioSnapshot:
        with self._lock:
            chunks = tuple(self._chunks)
            total = self._total_samples
            stored = self._stored_samples
        if not chunks:
            audio = np.empty(0, dtype=np.float32)
        elif len(chunks) == 1:
            audio = chunks[0].copy()
        else:
            audio = np.concatenate(chunks).astype(np.float32, copy=False)
        return PreviewAudioSnapshot(audio, total, total - stored)

    def reset(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._stored_samples = 0
            self._total_samples = 0


def _logical_device_id(uid: str) -> str:
    digest = hashlib.sha256(uid.encode("utf-8", errors="strict")).hexdigest()[:24]
    return f"macos-input-{digest}"


def _portaudio_inputs() -> tuple[dict[str, object], ...]:
    hostapis = tuple(sd.query_hostapis())
    core_api_indices = {
        index
        for index, api in enumerate(hostapis)
        if str(api.get("name", "")).casefold() == "core audio"
    }
    if not core_api_indices:
        return ()
    default_indices = {
        int(hostapis[index].get("default_input_device", -1))
        for index in core_api_indices
        if isinstance(hostapis[index].get("default_input_device"), int)
    }
    try:
        configured_default = sd.default.device[0]
        if isinstance(configured_default, int) and not isinstance(
            configured_default, bool
        ):
            default_indices.add(configured_default)
    except Exception:  # noqa: BLE001 - read-only fallback remains best effort
        pass

    records: list[dict[str, object]] = []
    for fallback_index, info in enumerate(tuple(sd.query_devices())):
        try:
            channels = int(info["max_input_channels"])
            hostapi_index = int(info["hostapi"])
            native_index = int(info.get("index", fallback_index))
        except (KeyError, TypeError, ValueError):
            continue
        if channels < 1 or hostapi_index not in core_api_indices:
            continue
        records.append(
            {
                "index": native_index,
                "name": str(info["name"]),
                "max_input_channels": channels,
                "default_sample_rate": float(info["default_samplerate"]),
                "is_portaudio_default": native_index in default_indices,
            }
        )
    return tuple(records)


def _query_native_input_devices(
    bridge: _CoreAudioBridge,
) -> tuple[_NativeInputDevice, ...]:
    core_by_name: dict[str, list[_CoreAudioDevice]] = defaultdict(list)
    for device in bridge.enumerate_devices():
        core_by_name[device.name.casefold()].append(device)
    for devices in core_by_name.values():
        devices.sort(key=lambda device: device.object_id)

    used_uids: set[str] = set()
    portaudio = _portaudio_inputs()
    records: list[_NativeInputDevice] = []
    for info in portaudio:
        candidates = [
            device
            for device in core_by_name.get(str(info["name"]).casefold(), ())
            if device.uid not in used_uids
        ]
        if not candidates:
            continue
        core_device = next(
            (
                device
                for device in candidates
                if device.is_default and bool(info["is_portaudio_default"])
            ),
            candidates[0],
        )
        used_uids.add(core_device.uid)
        records.append(
            _NativeInputDevice(
                index=int(info["index"]),
                candidate_indices=tuple(
                    int(candidate["index"])
                    for candidate in portaudio
                    if str(candidate["name"]).casefold()
                    == core_device.name.casefold()
                ),
                uid=core_device.uid,
                name=core_device.name,
                max_input_channels=int(info["max_input_channels"]),
                default_sample_rate=core_device.nominal_sample_rate,
                logical_id=_logical_device_id(core_device.uid),
                is_default=core_device.is_default,
                native_sample_rate_ranges=core_device.available_sample_rate_ranges,
            )
        )
    return tuple(records)


def _query_microphone_permission_state() -> PermissionState:
    try:
        from .permission_status import query_permissions

        report = query_permissions()
        return next(
            snapshot.state
            for snapshot in report.permissions
            if snapshot.kind is PermissionKind.MICROPHONE
        )
    except Exception:  # noqa: BLE001 - capture fails closed on unknown status
        return PermissionState.UNKNOWN


_devices_enumerated_logged = False


def enumerate_input_devices() -> CaptureInventory:
    """Return prompt-free CoreAudio inputs with opaque UID-derived IDs."""
    records = _query_native_input_devices(_NativeCoreAudioBridge())
    return CaptureInventory(
        devices=tuple(
            CaptureDevice(
                logical_id=record.logical_id,
                display_name=record.name,
                max_input_channels=record.max_input_channels,
                default_sample_rate=record.default_sample_rate,
                is_default=record.is_default,
            )
            for record in records
        )
    )


def list_input_devices() -> list[dict[str, Any]]:
    global _devices_enumerated_logged
    inventory = enumerate_input_devices()
    devices = [
        {
            "id": device.logical_id,
            "name": device.display_name,
            "channels": device.max_input_channels,
            "sample_rate": device.default_sample_rate,
            "is_default": device.is_default,
        }
        for device in inventory.devices
    ]
    if not _devices_enumerated_logged:
        logger.debug("audio devices enumerated platform=macos count=%d", len(devices))
        _devices_enumerated_logged = True
    return devices


def has_input_device() -> bool:
    try:
        return bool(list_input_devices())
    except Exception as exc:  # noqa: BLE001 - availability probe never raises
        logger.warning("audio device probe failed reason=%s", type(exc).__name__)
        return False


class AudioCaptureError(RuntimeError):
    """Compatibility exception facade for a typed capture failure."""

    def __init__(self, failure: CaptureFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


@dataclass(eq=False)
class _CaptureContext:
    """Resources belong to one admission, including after a caller times out."""

    request: CaptureRequest
    level: Callable[[float], None] | None
    chunk: Callable[[np.ndarray], None] | None
    preview: Callable[[], None] | None
    armed: bool
    session: CaptureSession | None = None
    stream: Any = None
    token: object = field(default_factory=object)
    frames: list[np.ndarray] = field(default_factory=list)
    frames_lock: threading.Lock = field(default_factory=threading.Lock)
    ring: _PreviewAudioRing | None = None
    chunk_buffer: ChunkBuffer | None = None
    chunk_queue: queue.Queue[np.ndarray | None] = field(default_factory=queue.Queue)
    chunk_worker: threading.Thread | None = None
    failure: CaptureFailure | None = None
    terminal_reported: bool = False
    terminal_submitted: bool = False
    cancelled: bool = False
    abandoned: bool = False
    retired: bool = False
    release_uncertain: bool = False
    open_done: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    released: threading.Event = field(default_factory=threading.Event)
    start_result: CaptureStartOutcome | None = None
    result: CaptureCompletion | None = None


class MacosCaptureAdapter:
    """One native owner; callbacks never initiate PortAudio shutdown."""

    terminal_operations_owned = True

    def __init__(
        self,
        *,
        sample_rate: int = CAPTURE_SAMPLE_RATE,
        channels: int = CAPTURE_CHANNELS,
        dtype: str = CAPTURE_DTYPE,
        device: str = "",
        status_callback: Callable[[str], None] | None = None,
        terminal_callback: CaptureTerminalCallback | None = None,
        chunk_callback: Callable[[np.ndarray], None] | None = None,
        chunk_duration: float = 0.0,
        chunk_overlap: float = 0.0,
        _bridge: _CoreAudioBridge | None = None,
        _permission_state: Callable[[], PermissionState] | None = None,
    ) -> None:
        native_host = platform.system() == "Darwin" and platform.machine() == "arm64"
        if not native_host and not (_bridge is not None and _permission_state is not None):
            raise RuntimeError("macOS capture adapter requires Darwin arm64")
        CaptureFormat(sample_rate=sample_rate, channels=channels, dtype=dtype)
        self.sample_rate, self.channels, self.dtype = sample_rate, channels, dtype
        self._device_name = device
        self._status_callback = status_callback
        self._terminal_callback = terminal_callback
        self._owner_epoch = allocate_capture_owner_epoch()
        self._level_callback = None
        self._chunk_callback = chunk_callback
        self._chunk_duration, self._chunk_overlap = chunk_duration, chunk_overlap
        self._preview_callback = None
        self._preview_max_duration = 0.0
        self._bridge = _bridge or _NativeCoreAudioBridge()
        self._permission_state = _permission_state or _query_microphone_permission_state
        self._state_lock = threading.Lock()
        self._context = None
        self._quarantined = False
        self._closed = False
        self._generation = 0
        self._legacy_session = None
        self._discard_until_armed = False
        self._operations = queue.Queue()
        self._worker = None
        self._shutdown_queued = False

    @property
    def capture_available(self) -> bool:
        with self._state_lock:
            return not self._quarantined and not self._closed

    @property
    def stream_open(self) -> bool:
        ctx = self._context
        return ctx is not None and ctx.stream is not None

    @property
    def _stream(self):
        ctx = self._context
        return ctx.stream if ctx else None

    @property
    def chunk_worker_alive(self) -> bool:
        ctx = self._context
        return bool(ctx and ctx.chunk_worker and ctx.chunk_worker.is_alive())

    @property
    def active_session(self) -> CaptureSession | None:
        with self._state_lock:
            ctx = self._context
            return ctx.session if ctx and (not ctx.retired or self._quarantined) else None

    @property
    def owner_epoch(self) -> int:
        return self._owner_epoch

    def _submit_locked(self, operation, ctx):
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, daemon=True,
                                            name="macos-capture-owner")
            self._worker.start()
        self._operations.put((operation, ctx))

    def _work(self):
        while True:
            operation, ctx = self._operations.get()
            if operation == "shutdown":
                return
            try:
                if operation == "open":
                    self._open(ctx)
                else:
                    self._finish(ctx)
            except Exception as exc:
                # Keep uncertain resources alive. Never allow another owner to
                # retry native work after an unexpected owner failure.
                logger.warning("audio owner failed reason=%s", type(exc).__name__)
                with self._state_lock:
                    self._quarantined = True
                    ctx.abandoned = True
                    ctx.start_result = ctx.start_result or CaptureStartOutcome(failure=CaptureFailure.NATIVE_ERROR)
                    ctx.result = ctx.result or self._completion(ctx, CaptureFailure.CLEANUP_FAILED)
                    ctx.open_done.set()
                    ctx.done.set()

    @staticmethod
    def _completion(ctx, failure=None, audio=None):
        return CaptureCompletion(
            request_id=ctx.request.request_id, generation=ctx.request.generation,
            status=(CaptureCompletionStatus.FAILED if failure else
                    CaptureCompletionStatus.CANCELLED if ctx.cancelled else CaptureCompletionStatus.STOPPED),
            failure=failure, audio=audio if not failure and not ctx.cancelled else None)

    @staticmethod
    def _notify(callback, *args):
        if callback is not None:
            try:
                callback(*args)
            except Exception as exc:
                logger.warning("audio notification failed reason=%s", type(exc).__name__)

    def set_level_callback(self, callback: Callable[[float], None] | None) -> None:
        with self._state_lock:
            self._level_callback = callback
            if self._context:
                self._context.level = callback

    def set_preview_callback(self, callback, *, max_duration=0.0):
        with self._state_lock:
            self._preview_callback = callback
            self._preview_max_duration = max(0.0, float(max_duration))
            if self._context:
                self._context.preview = callback

    def preview_snapshot(self) -> PreviewAudioSnapshot:
        with self._state_lock:
            ctx = self._context
            ring = ctx.ring if ctx and ctx.preview and not ctx.retired and not ctx.abandoned else None
        return ring.snapshot() if ring else PreviewAudioSnapshot(np.empty(0, dtype=np.float32), 0, 0)

    def _failure(self, ctx, token, failure):
        event = None
        with self._state_lock:
            if self._context is not ctx or ctx.token is not token or ctx.retired or ctx.abandoned:
                return
            ctx.failure = ctx.failure or failure
            ctx.armed = False
            if not ctx.terminal_reported:
                ctx.terminal_reported = True
                event = CaptureTerminalEvent(ctx.request.request_id, ctx.request.generation,
                                             ctx.failure, self._owner_epoch)
        # This callback enqueues the pre-cleanup app request. It must never wait
        # for the app lock. App restoration precedes its terminal submission.
        if event is not None:
            self._notify(self._terminal_callback, event)

    def _finished(self, ctx, token):
        with self._state_lock:
            expected = ctx.terminal_submitted or ctx.abandoned or ctx.retired or ctx.token is not token
        if not expected:
            self._failure(ctx, token, CaptureFailure.DEVICE_LOST)

    def _stream_callback(self, ctx, token, indata, _frames, _time, status):
        try:
            with self._state_lock:
                if self._context is not ctx or ctx.token is not token or ctx.retired or ctx.abandoned or not ctx.armed or ctx.failure:
                    return
                level, preview, ring = ctx.level, ctx.preview, ctx.ring
            if status:
                self._notify(self._status_callback, "stream-status")
            data = np.asarray(indata, dtype=np.float32)
            mono = np.mean(data, axis=1, dtype=np.float32) if data.ndim > 1 and data.shape[1] > 1 else data.reshape(-1).copy()
            mono = np.ascontiguousarray(mono, dtype=np.float32)
            with ctx.frames_lock:
                ctx.frames.append(mono)
            if ctx.chunk_buffer is not None:
                for chunk in ctx.chunk_buffer.append(mono):
                    ctx.chunk_queue.put(chunk)
            # Preview and levels are optional. Their failures cannot abort
            # authoritative capture or initiate callback-side native shutdown.
            try:
                if ring is not None and preview is not None:
                    ring.append(mono)
                    self._notify(preview)
            except Exception as exc:
                logger.warning("audio preview failed reason=%s", type(exc).__name__)
            try:
                if level is not None:
                    rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
                    self._notify(level, min(rms * 10.0, 1.0))
            except Exception as exc:
                logger.warning("audio level failed reason=%s", type(exc).__name__)
        except Exception as exc:
            logger.warning("audio callback failed reason=%s", type(exc).__name__)
            self._failure(ctx, token, CaptureFailure.STREAM_FAILED)
            # Return normally: PortAudio callback abort can race explicit stop.

    def _chunks(self, ctx):
        while True:
            chunk = ctx.chunk_queue.get()
            if chunk is None:
                return
            with self._state_lock:
                callback = ctx.chunk if not (ctx.abandoned or ctx.cancelled or ctx.failure or ctx.retired) else None
            self._notify(callback, chunk)

    @staticmethod
    def _coreaudio_settings():
        return sd.CoreAudioSettings(change_device_parameters=False,
                                   fail_if_conversion_required=False, conversion_quality="max")

    def _check_canonical_format(self, device_index):
        sd.check_input_settings(device=device_index, channels=CAPTURE_CHANNELS,
                                dtype=CAPTURE_DTYPE, samplerate=CAPTURE_SAMPLE_RATE,
                                extra_settings=self._coreaudio_settings())

    def _close_native(self, ctx, *, stop):
        stream = ctx.stream
        if stream is None:
            return None
        if stop:
            try:
                stream.stop(ignore_errors=False)
            except Exception as exc:
                # Already-stopped and other stop errors do not establish release.
                # Checked close below is the final resource ownership authority.
                already_stopped = (
                    isinstance(exc, sd.PortAudioError)
                    and len(exc.args) > 1
                    and exc.args[1] == sd._lib.paStreamIsStopped
                )
                if not already_stopped:
                    logger.warning("audio native stop failed reason=%s", type(exc).__name__)
                    ctx.failure = ctx.failure or CaptureFailure.CLEANUP_FAILED
        try:
            stream.close(ignore_errors=False)
        except Exception as exc:
            logger.warning("audio native close failed reason=%s", type(exc).__name__)
            with self._state_lock:
                self._quarantined = True
                ctx.release_uncertain = True
                ctx.abandoned = True
            return CaptureFailure.CLEANUP_FAILED
        ctx.stream = None
        return None

    def _open_one(self, ctx, index, expected_uid):
        if ctx.abandoned or self._quarantined:
            raise AudioCaptureError(CaptureFailure.TIMEOUT)
        self._check_canonical_format(index)
        token = object()
        ctx.token = token
        ctx.stream = sd.InputStream(
            samplerate=CAPTURE_SAMPLE_RATE, channels=CAPTURE_CHANNELS,
            dtype=CAPTURE_DTYPE, device=index,
            callback=lambda *args: self._stream_callback(ctx, token, *args),
            finished_callback=lambda: self._finished(ctx, token),
            extra_settings=self._coreaudio_settings())
        if ctx.abandoned:
            raise AudioCaptureError(CaptureFailure.TIMEOUT)
        ctx.stream.start()
        if ctx.abandoned:
            raise AudioCaptureError(CaptureFailure.TIMEOUT)
        if int(round(float(ctx.stream.samplerate))) != CAPTURE_SAMPLE_RATE:
            raise RuntimeError("PortAudio did not provide canonical sample rate")
        actual = self._bridge.stream_input_uid(ctx.stream)
        if expected_uid is not None and actual != expected_uid:
            raise RuntimeError("PortAudio opened a different CoreAudio device")
        return actual

    def _open(self, ctx):
        failure = None
        try:
            selected, provisional, resolution = self._resolve_request(ctx.request)
            permission = self._permission_state()
            if permission is not PermissionState.GRANTED:
                raise AudioCaptureError(CaptureFailure.OPEN_FAILED if permission in {
                    PermissionState.NOT_DETERMINED, PermissionState.RESTRICTED, PermissionState.DENIED
                } else CaptureFailure.NATIVE_ERROR)
            if self._chunk_duration > 0 and ctx.chunk:
                ctx.chunk_buffer = ChunkBuffer(sample_rate=self.sample_rate,
                    chunk_duration=self._chunk_duration, chunk_overlap=self._chunk_overlap)
                ctx.chunk_worker = threading.Thread(target=self._chunks, args=(ctx,), daemon=True, name="macos-chunk-dispatch")
                ctx.chunk_worker.start()
            samples = int(round(self.sample_rate * self._preview_max_duration))
            ctx.ring = _PreviewAudioRing(samples) if ctx.preview and samples > 0 else None
            candidates = [(i, selected.uid) for i in selected.candidate_indices] if selected else []
            candidates.append((None, None))
            for index, uid in candidates:
                try:
                    actual = self._open_one(ctx, index, uid)
                    if selected and index is None:
                        resolution = CaptureSelectionResolution.FALLBACK_OPEN_FAILED
                    break
                except Exception:
                    # Retire this candidate's callbacks before partial close.
                    ctx.token = object()
                    cleanup = self._close_native(ctx, stop=False)
                    if cleanup or ctx.abandoned or (index, uid) == candidates[-1]:
                        raise
            with self._state_lock:
                if ctx.abandoned:
                    raise AudioCaptureError(CaptureFailure.TIMEOUT)
                ctx.session = CaptureSession(ctx.request.request_id, ctx.request.generation,
                                             _logical_device_id(actual), resolution)
                ctx.start_result = CaptureStartOutcome(session=ctx.session)
                ctx.open_done.set()
            return
        except AudioCaptureError as exc:
            failure = exc.failure
        except Exception as exc:
            logger.warning("audio capture open failed reason=%s", type(exc).__name__)
            failure = CaptureFailure.OPEN_FAILED
        ctx.cancelled = True
        # Failed close clears sounddevice's pointer, so it must not be retried.
        if not ctx.release_uncertain:
            cleanup = self._close_native(ctx, stop=False)
            failure = cleanup or failure
        else:
            failure = CaptureFailure.CLEANUP_FAILED
        if not ctx.release_uncertain:
            self._drain(ctx)
        with self._state_lock:
            ctx.retired = True
            ctx.start_result = ctx.start_result or CaptureStartOutcome(failure=failure)
            ctx.result = ctx.result or self._completion(ctx, failure)
            ctx.open_done.set()
            ctx.done.set()
            if not ctx.release_uncertain:
                ctx.released.set()

    def start_capture(
        self,
        request: CaptureRequest,
        *,
        on_level: Callable[[float], None] | None = None,
        on_chunk: Callable[[np.ndarray], None] | None = None,
    ) -> CaptureStartOutcome:
        with self._state_lock:
            if self._quarantined or self._closed or (self._context and not self._context.done.is_set()):
                return CaptureStartOutcome(failure=CaptureFailure.NATIVE_ERROR)
            ctx = _CaptureContext(request, on_level, on_chunk, self._preview_callback,
                                  not self._discard_until_armed)
            self._context = ctx
            self._submit_locked("open", ctx)
        if not ctx.open_done.wait(CAPTURE_START_TIMEOUT_SECONDS):
            with self._state_lock:
                if not ctx.open_done.is_set():
                    ctx.abandoned = True
                    ctx.armed = False
                    self._quarantined = True
                    ctx.start_result = CaptureStartOutcome(failure=CaptureFailure.TIMEOUT)
        return ctx.start_result

    def _drain(self, ctx):
        if ctx.chunk_buffer is not None:
            if not (ctx.cancelled or ctx.failure or ctx.abandoned):
                tail = ctx.chunk_buffer.flush_tail()
                if tail is not None:
                    ctx.chunk_queue.put(tail)
            ctx.chunk_buffer = None
        if ctx.chunk_worker:
            ctx.chunk_queue.put(None)
            # The public deadline bounds the entire operation. This owner keeps
            # resources until dispatch really exits, even after caller timeout.
            ctx.chunk_worker.join()
        if ctx.ring:
            ctx.ring.reset()

    def _finish(self, ctx: _CaptureContext) -> None:
        cleanup = self._close_native(ctx, stop=True)
        if not ctx.release_uncertain:
            self._drain(ctx)
        with self._state_lock:
            ctx.retired = True
            collect = not (cleanup or ctx.failure or ctx.cancelled or ctx.abandoned)
        # Allocation is not a state transition. Keep it outside the admission
        # lock so availability reads and the whole-operation deadline stay live.
        audio = np.empty(0, dtype=np.float32)
        if not ctx.release_uncertain:
            with ctx.frames_lock:
                frames = tuple(ctx.frames) if collect else ()
                ctx.frames.clear()
            if frames:
                audio = np.concatenate(frames).astype(np.float32, copy=False)
        with self._state_lock:
            if ctx.result is None:
                ctx.result = self._completion(ctx, cleanup or ctx.failure, audio)
            ctx.done.set()
            if not ctx.release_uncertain:
                ctx.released.set()

    def _terminal(self, session, *, cancel):
        with self._state_lock:
            ctx = self._context
            if ctx is None or ctx.session != session:
                return CaptureCompletion(session.request_id, session.generation,
                    CaptureCompletionStatus.FAILED, failure=CaptureFailure.STALE_GENERATION)
            if ctx.result is not None:
                return ctx.result
            ctx.cancelled = ctx.cancelled or cancel
            if not ctx.terminal_submitted:
                ctx.terminal_submitted = True
                self._submit_locked("finish", ctx)
        if not ctx.done.wait(CAPTURE_STOP_TIMEOUT_SECONDS):
            with self._state_lock:
                if ctx.result is None:
                    self._quarantined = True
                    ctx.abandoned = True
                    ctx.armed = False
                    ctx.result = self._completion(ctx, CaptureFailure.TIMEOUT)
                    ctx.done.set()
        return ctx.result

    def stop_capture(self, session: CaptureSession) -> CaptureCompletion:
        return self._terminal(session, cancel=False)

    def cancel_capture(self, session: CaptureSession) -> CaptureCompletion:
        return self._terminal(session, cancel=True)

    def arm_capture(self) -> None:
        with self._state_lock:
            ctx = self._context
            if ctx and not (ctx.abandoned or ctx.retired or ctx.failure):
                ctx.armed = True

    def start(self, *, discard_until_armed: bool = False) -> None:
        self._discard_until_armed = discard_until_armed
        self._generation += 1
        selection = (CaptureSelection(preferred_id=self._device_name)
                     if self._device_name.startswith("macos-input-") else
                     CaptureSelection(legacy_name=self._device_name or None))
        outcome = self.start_capture(CaptureRequest(f"macos-capture-{self._generation}", self._generation, selection),
                                     on_level=self._level_callback, on_chunk=self._chunk_callback)
        if outcome.failure:
            raise AudioCaptureError(outcome.failure)
        self._legacy_session = outcome.session

    def stop(self) -> np.ndarray:
        session = self._legacy_session or self.active_session
        if session is None:
            return np.empty(0, dtype=np.float32)
        result = self.stop_capture(session)
        if result.failure:
            raise AudioCaptureError(result.failure)
        return result.audio if result.audio is not None else np.empty(0, dtype=np.float32)

    def cancel(self) -> None:
        session = self._legacy_session or self.active_session
        if session is not None:
            result = self.cancel_capture(session)
            if result.failure:
                raise AudioCaptureError(result.failure)

    def close(self) -> bool:
        with self._state_lock:
            ctx = self._context
            self._closed = True
            if ctx and not ctx.open_done.is_set():
                ctx.abandoned = True
                ctx.armed = False
                self._quarantined = True
            unavailable = self._quarantined
        if ctx and ctx.session and not unavailable:
            self.cancel_capture(ctx.session)
        with self._state_lock:
            if self._worker is not None and not self._shutdown_queued:
                self._shutdown_queued = True
                self._operations.put(("shutdown", None))
            if self._quarantined:
                return False
            complete = not self.stream_open and not self.chunk_worker_alive
            if complete:
                self._context = None
                self._legacy_session = None
            return complete

    def _native_devices(self) -> tuple[_NativeInputDevice, ...]:
        if not self.capture_available:
            return ()
        return _query_native_input_devices(self._bridge)

    def enumerate_input_devices(self) -> CaptureInventory:
        records = self._native_devices()
        return CaptureInventory(
            devices=tuple(
                CaptureDevice(
                    logical_id=record.logical_id,
                    display_name=record.name,
                    max_input_channels=record.max_input_channels,
                    default_sample_rate=record.default_sample_rate,
                    is_default=record.is_default,
                )
                for record in records
            )
        )

    def supported_capture_formats(self) -> tuple[MacCaptureFormatSupport, ...]:
        """Discover native rate ranges and canonical PortAudio conversion."""
        permission_granted = self._permission_state() is PermissionState.GRANTED
        results: list[MacCaptureFormatSupport] = []
        for record in self._native_devices():
            canonical_supported: bool | None = None
            if permission_granted:
                canonical_supported = False
                for device_index in record.candidate_indices:
                    try:
                        self._check_canonical_format(device_index)
                        canonical_supported = True
                        break
                    except Exception:  # noqa: BLE001 - independent candidate
                        continue
            results.append(
                MacCaptureFormatSupport(
                    logical_id=record.logical_id,
                    native_sample_rate=record.default_sample_rate,
                    native_sample_rate_ranges=record.native_sample_rate_ranges,
                    canonical_format_supported=canonical_supported,
                )
            )
        return tuple(results)

    def _resolve_request(
        self, request: CaptureRequest
    ) -> tuple[_NativeInputDevice | None, str, CaptureSelectionResolution]:
        records = self._native_devices()
        if not records:
            raise AudioCaptureError(CaptureFailure.NO_INPUT_DEVICE)
        default = next((record for record in records if record.is_default), None)
        default_id = (
            default.logical_id if default is not None else "macos-system-default"
        )
        selection = request.selection
        if selection.uses_system_default:
            return None, default_id, CaptureSelectionResolution.SYSTEM_DEFAULT

        selected: _NativeInputDevice | None = None
        if selection.preferred_id is not None:
            selected = next(
                (
                    record
                    for record in records
                    if record.logical_id == selection.preferred_id
                ),
                None,
            )
        elif selection.legacy_name is not None:
            matches = [
                record
                for record in records
                if record.name.casefold() == selection.legacy_name.casefold()
            ]
            if len(matches) == 1:
                selected = matches[0]
            elif matches:
                selected = next(
                    (record for record in matches if record.is_default),
                    None,
                )
        if selected is None:
            return None, default_id, CaptureSelectionResolution.FALLBACK_MISSING
        return selected, selected.logical_id, CaptureSelectionResolution.SELECTED



AudioCapture = MacosCaptureAdapter


__all__ = [
    "AudioCapture",
    "AudioCaptureError",
    "MacCaptureFormatSupport",
    "MacosCaptureAdapter",
    "PreviewAudioSnapshot",
    "enumerate_input_devices",
    "has_input_device",
    "list_input_devices",
]
