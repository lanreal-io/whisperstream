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


class MacosCaptureAdapter:
    """Generation-bound CoreAudio/PortAudio capture for Apple Silicon."""

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
        fully_injected = _bridge is not None and _permission_state is not None
        if not native_host and not fully_injected:
            raise RuntimeError("macOS capture adapter requires Darwin arm64")
        CaptureFormat(sample_rate=sample_rate, channels=channels, dtype=dtype)
        self.sample_rate = sample_rate
        self.channels = channels
        self.dtype = dtype
        self._device_name = device
        self._status_callback = status_callback
        self._terminal_callback = terminal_callback
        self._owner_epoch = allocate_capture_owner_epoch()
        self._level_callback: Callable[[float], None] | None = None
        self._chunk_callback = chunk_callback
        self._chunk_duration = chunk_duration
        self._chunk_overlap = chunk_overlap
        self._bridge = _bridge or _NativeCoreAudioBridge()
        self._permission_state = _permission_state or _query_microphone_permission_state

        self._stream: object | None = None
        self._capture_armed = threading.Event()
        self._capture_armed.set()
        self._frames: list[np.ndarray] = []
        self._frames_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_session: CaptureSession | None = None
        self._active_failure: CaptureFailure | None = None
        self._stopping = False
        self._generation = 0
        self._legacy_session: CaptureSession | None = None

        self._chunk_buffer: ChunkBuffer | None = None
        self._chunk_queue: queue.Queue[np.ndarray | None] = queue.Queue()
        self._chunk_worker: threading.Thread | None = None
        self._preview_callback: Callable[[], None] | None = None
        self._preview_max_duration = 0.0
        self._preview_ring: _PreviewAudioRing | None = None

    @property
    def stream_open(self) -> bool:
        return self._stream is not None

    @property
    def chunk_worker_alive(self) -> bool:
        worker = self._chunk_worker
        return worker is not None and worker.is_alive()

    @property
    def active_session(self) -> CaptureSession | None:
        with self._state_lock:
            return self._active_session

    @property
    def owner_epoch(self) -> int:
        return self._owner_epoch

    def _native_devices(self) -> tuple[_NativeInputDevice, ...]:
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

    def set_level_callback(self, callback: Callable[[float], None] | None) -> None:
        self._level_callback = callback

    def set_preview_callback(
        self,
        callback: Callable[[], None] | None,
        *,
        max_duration: float = 0.0,
    ) -> None:
        self._preview_callback = callback
        self._preview_max_duration = max(0.0, float(max_duration))
        if callback is None and self._preview_ring is not None:
            self._preview_ring.reset()
            self._preview_ring = None

    def preview_snapshot(self) -> PreviewAudioSnapshot:
        ring = self._preview_ring
        if ring is None:
            return PreviewAudioSnapshot(
                np.empty(0, dtype=np.float32),
                0,
                0,
            )
        return ring.snapshot()

    def _chunk_dispatch_loop(self) -> None:
        while True:
            chunk = self._chunk_queue.get()
            if chunk is None:
                return
            callback = self._chunk_callback
            if callback is None:
                continue
            try:
                callback(chunk)
            except Exception:
                logger.exception("audio chunk callback failed origin=stream")

    def _prepare_stream_work(self) -> None:
        self._chunk_queue = queue.Queue()
        if self._chunk_duration > 0 and self._chunk_callback is not None:
            self._chunk_buffer = ChunkBuffer(
                sample_rate=self.sample_rate,
                chunk_duration=self._chunk_duration,
                chunk_overlap=self._chunk_overlap,
            )
            self._chunk_worker = threading.Thread(
                target=self._chunk_dispatch_loop,
                daemon=True,
                name="macos-chunk-dispatch",
            )
            self._chunk_worker.start()
        else:
            self._chunk_buffer = None
            self._chunk_worker = None

        preview_samples = int(round(self.sample_rate * self._preview_max_duration))
        self._preview_ring = (
            _PreviewAudioRing(preview_samples)
            if self._preview_callback is not None and preview_samples > 0
            else None
        )
        with self._frames_lock:
            self._frames.clear()

    def _record_failure(self, failure: CaptureFailure) -> None:
        with self._state_lock:
            if self._active_session is not None and self._active_failure is None:
                self._active_failure = failure

    def _on_stream_finished(self) -> None:
        callback: Callable[[str], None] | None = None
        terminal_callback: CaptureTerminalCallback | None = None
        terminal_event: CaptureTerminalEvent | None = None
        with self._state_lock:
            if (
                self._active_session is not None
                and not self._stopping
                and self._active_failure is None
            ):
                self._active_failure = CaptureFailure.DEVICE_LOST
                callback = self._status_callback
                terminal_callback = self._terminal_callback
                terminal_event = CaptureTerminalEvent(
                    request_id=self._active_session.request_id,
                    generation=self._active_session.generation,
                    failure=CaptureFailure.DEVICE_LOST,
                    owner_epoch=self._owner_epoch,
                )
        if terminal_callback is not None and terminal_event is not None:
            try:
                terminal_callback(terminal_event)
            except Exception:  # noqa: BLE001 - cleanup must continue
                logger.warning("audio terminal callback failed origin=device_loss")
        if callback is not None:
            try:
                callback(CaptureFailure.DEVICE_LOST.value)
            except Exception:  # noqa: BLE001 - cleanup must continue
                logger.warning("audio status callback failed origin=device_loss")

    def _stream_callback(
        self,
        indata: np.ndarray,
        _frame_count: int,
        _time_info: object,
        status: object,
    ) -> None:
        try:
            if status and self._status_callback is not None:
                self._status_callback("stream-status")
            if not self._capture_armed.is_set():
                return
            data = np.asarray(indata, dtype=np.float32)
            if data.ndim > 1 and data.shape[1] > 1:
                mono = np.mean(data, axis=1, dtype=np.float32)
            else:
                mono = data.reshape(-1).copy()
            mono = np.ascontiguousarray(mono, dtype=np.float32)
            with self._frames_lock:
                self._frames.append(mono)

            if self._preview_ring is not None:
                self._preview_ring.append(mono)
                if self._preview_callback is not None:
                    try:
                        self._preview_callback()
                    except Exception:
                        logger.exception("audio preview callback failed origin=stream")

            if self._chunk_buffer is not None:
                for chunk in self._chunk_buffer.append(mono):
                    self._chunk_queue.put(chunk)

            if self._level_callback is not None:
                rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
                self._level_callback(min(rms * 10.0, 1.0))
        except Exception as exc:
            self._record_failure(CaptureFailure.STREAM_FAILED)
            logger.warning("audio callback failed reason=%s", type(exc).__name__)
            raise sd.CallbackAbort from exc

    @staticmethod
    def _coreaudio_settings():
        return sd.CoreAudioSettings(
            change_device_parameters=False,
            fail_if_conversion_required=False,
            conversion_quality="max",
        )

    def _check_canonical_format(self, device_index: int | None) -> None:
        sd.check_input_settings(
            device=device_index,
            channels=CAPTURE_CHANNELS,
            dtype=CAPTURE_DTYPE,
            samplerate=CAPTURE_SAMPLE_RATE,
            extra_settings=self._coreaudio_settings(),
        )

    def _close_partial_stream(self, stream: object | None) -> None:
        if stream is None:
            return
        try:
            stream.close()
        except Exception:  # noqa: BLE001 - retain uncertain ownership
            self._stream = stream

    def _open_one_stream(
        self,
        device_index: int | None,
        *,
        expected_uid: str | None,
    ) -> tuple[object, str]:
        stream: object | None = None
        try:
            self._check_canonical_format(device_index)
            stream = sd.InputStream(
                samplerate=CAPTURE_SAMPLE_RATE,
                channels=CAPTURE_CHANNELS,
                dtype=CAPTURE_DTYPE,
                device=device_index,
                callback=self._stream_callback,
                finished_callback=self._on_stream_finished,
                extra_settings=self._coreaudio_settings(),
            )
            stream.start()
            if int(round(float(stream.samplerate))) != CAPTURE_SAMPLE_RATE:
                raise RuntimeError("PortAudio did not provide canonical sample rate")
            actual_uid = self._bridge.stream_input_uid(stream)
            if expected_uid is not None and actual_uid != expected_uid:
                raise RuntimeError("PortAudio opened a different CoreAudio device")
            return stream, actual_uid
        except Exception:
            self._close_partial_stream(stream)
            raise

    def _open_selected_stream(
        self, selected: _NativeInputDevice
    ) -> tuple[object, str]:
        last_error: Exception | None = None
        for device_index in selected.candidate_indices:
            try:
                return self._open_one_stream(
                    device_index,
                    expected_uid=selected.uid,
                )
            except Exception as exc:  # noqa: BLE001 - try same-name candidates
                if self._stream is not None:
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("selected PortAudio device unavailable")

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

    def start_capture(
        self,
        request: CaptureRequest,
        *,
        on_level: Callable[[float], None] | None = None,
        on_chunk: Callable[[np.ndarray], None] | None = None,
    ) -> CaptureStartOutcome:
        with self._state_lock:
            if self._active_session is not None:
                return CaptureStartOutcome(failure=CaptureFailure.NATIVE_ERROR)
        try:
            selected, provisional_id, resolution = self._resolve_request(request)
        except AudioCaptureError as exc:
            return CaptureStartOutcome(failure=exc.failure)
        except Exception:
            return CaptureStartOutcome(failure=CaptureFailure.NATIVE_ERROR)

        permission_state = self._permission_state()
        if permission_state in {
            PermissionState.NOT_DETERMINED,
            PermissionState.RESTRICTED,
            PermissionState.DENIED,
        }:
            return CaptureStartOutcome(failure=CaptureFailure.OPEN_FAILED)
        if permission_state is not PermissionState.GRANTED:
            return CaptureStartOutcome(failure=CaptureFailure.NATIVE_ERROR)

        session = CaptureSession(
            request_id=request.request_id,
            generation=request.generation,
            device_id=provisional_id,
            selection_resolution=resolution,
        )
        with self._state_lock:
            self._active_session = session
            self._active_failure = None
            self._stopping = False
        self._level_callback = on_level
        self._chunk_callback = on_chunk

        try:
            self._prepare_stream_work()
            try:
                if selected is None:
                    stream, actual_uid = self._open_one_stream(
                        None,
                        expected_uid=None,
                    )
                else:
                    stream, actual_uid = self._open_selected_stream(selected)
            except Exception as selected_error:
                if selected is None or self._stream is not None:
                    raise selected_error
                logger.warning(
                    "audio device open failed selection=selected fallback=system_default reason=%s",
                    type(selected_error).__name__,
                )
                stream, actual_uid = self._open_one_stream(
                    None,
                    expected_uid=None,
                )
                resolution = CaptureSelectionResolution.FALLBACK_OPEN_FAILED
            self._stream = stream
            session = CaptureSession(
                request_id=request.request_id,
                generation=request.generation,
                device_id=_logical_device_id(actual_uid),
                selection_resolution=resolution,
            )
            with self._state_lock:
                self._active_session = session
        except Exception as exc:
            logger.warning("audio capture open failed reason=%s", type(exc).__name__)
            with self._state_lock:
                self._stopping = True
            _, cleanup_failure = self._finish_stream(
                discard=True, flush_tail=False
            )
            if cleanup_failure is None:
                with self._state_lock:
                    self._active_session = None
                    self._active_failure = None
                    self._stopping = False
            return CaptureStartOutcome(
                failure=cleanup_failure or CaptureFailure.OPEN_FAILED
            )

        logger.info(
            "audio device opened platform=macos resolution=%s sample_rate=%d channels=%d",
            session.selection_resolution.value,
            CAPTURE_SAMPLE_RATE,
            CAPTURE_CHANNELS,
        )
        return CaptureStartOutcome(session=session)

    def _stop_chunk_worker(self, *, discard_pending: bool) -> bool:
        worker = self._chunk_worker
        if worker is None:
            return True
        if discard_pending:
            while True:
                try:
                    self._chunk_queue.get_nowait()
                except queue.Empty:
                    break
        self._chunk_queue.put(None)
        worker.join(timeout=CAPTURE_STOP_TIMEOUT_SECONDS)
        if worker.is_alive():
            return False
        self._chunk_worker = None
        return True

    def _finish_stream(
        self,
        *,
        discard: bool,
        flush_tail: bool,
    ) -> tuple[np.ndarray, CaptureFailure | None]:
        """Release exact native and worker state under the app owner lock."""
        stream = self._stream
        cleanup_failure: CaptureFailure | None = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                cleanup_failure = CaptureFailure.CLEANUP_FAILED
            try:
                stream.close()
            except Exception:
                cleanup_failure = CaptureFailure.CLEANUP_FAILED
            else:
                if self._stream is stream:
                    self._stream = None

        if self._chunk_buffer is not None:
            if flush_tail and not discard:
                tail = self._chunk_buffer.flush_tail()
                if tail is not None and self._chunk_callback is not None:
                    self._chunk_queue.put(tail)
            self._chunk_buffer.reset()
            self._chunk_buffer = None

        if not self._stop_chunk_worker(discard_pending=discard):
            cleanup_failure = CaptureFailure.TIMEOUT
        if self._preview_ring is not None:
            self._preview_ring.reset()
            self._preview_ring = None

        with self._frames_lock:
            frames = tuple(self._frames)
            self._frames.clear()
        if discard or not frames:
            audio = np.empty(0, dtype=np.float32)
        elif len(frames) == 1:
            audio = frames[0].copy()
        else:
            audio = np.concatenate(frames).astype(np.float32, copy=False)
        return np.ascontiguousarray(audio.reshape(-1), dtype=np.float32), cleanup_failure

    def _matches_active(self, session: CaptureSession) -> bool:
        with self._state_lock:
            return self._active_session == session

    def stop_capture(self, session: CaptureSession) -> CaptureCompletion:
        if not self._matches_active(session):
            return CaptureCompletion(
                request_id=session.request_id,
                generation=session.generation,
                status=CaptureCompletionStatus.FAILED,
                failure=CaptureFailure.STALE_GENERATION,
            )
        with self._state_lock:
            self._stopping = True
            failure = self._active_failure
        audio, cleanup_failure = self._finish_stream(
            discard=False,
            flush_tail=True,
        )
        failure = cleanup_failure or failure
        if cleanup_failure is None:
            with self._state_lock:
                self._active_session = None
                self._active_failure = None
                self._stopping = False
            self._legacy_session = None
        if failure is not None:
            return CaptureCompletion(
                request_id=session.request_id,
                generation=session.generation,
                status=CaptureCompletionStatus.FAILED,
                failure=failure,
            )
        return CaptureCompletion(
            request_id=session.request_id,
            generation=session.generation,
            status=CaptureCompletionStatus.STOPPED,
            audio=audio,
        )

    def cancel_capture(self, session: CaptureSession) -> CaptureCompletion:
        if not self._matches_active(session):
            return CaptureCompletion(
                request_id=session.request_id,
                generation=session.generation,
                status=CaptureCompletionStatus.FAILED,
                failure=CaptureFailure.STALE_GENERATION,
            )
        with self._state_lock:
            self._stopping = True
            failure = self._active_failure
        _, cleanup_failure = self._finish_stream(
            discard=True,
            flush_tail=False,
        )
        failure = cleanup_failure or failure
        if cleanup_failure is None:
            with self._state_lock:
                self._active_session = None
                self._active_failure = None
                self._stopping = False
            self._legacy_session = None
        if failure is not None:
            return CaptureCompletion(
                request_id=session.request_id,
                generation=session.generation,
                status=CaptureCompletionStatus.FAILED,
                failure=failure,
            )
        return CaptureCompletion(
            request_id=session.request_id,
            generation=session.generation,
            status=CaptureCompletionStatus.CANCELLED,
        )

    def arm_capture(self) -> None:
        """Admit audio after the recording start cue has completed."""
        self._capture_armed.set()

    def start(self, *, discard_until_armed: bool = False) -> None:
        if self.active_session is not None:
            raise AudioCaptureError(CaptureFailure.NATIVE_ERROR)
        if discard_until_armed:
            self._capture_armed.clear()
        else:
            self._capture_armed.set()
        self._generation += 1
        selection = (
            CaptureSelection(preferred_id=self._device_name)
            if self._device_name.startswith("macos-input-")
            else CaptureSelection(legacy_name=self._device_name or None)
        )
        outcome = self.start_capture(
            CaptureRequest(
                request_id=f"macos-capture-{self._generation}",
                generation=self._generation,
                selection=selection,
            ),
            on_level=self._level_callback,
            on_chunk=self._chunk_callback,
        )
        if outcome.failure is not None:
            raise AudioCaptureError(outcome.failure)
        self._legacy_session = outcome.session

    def stop(self) -> np.ndarray:
        session = self._legacy_session or self.active_session
        if session is None:
            return np.empty(0, dtype=np.float32)
        completion = self.stop_capture(session)
        if completion.status is CaptureCompletionStatus.FAILED:
            raise AudioCaptureError(completion.failure or CaptureFailure.NATIVE_ERROR)
        assert completion.audio is not None
        return completion.audio

    def cancel(self) -> None:
        session = self._legacy_session or self.active_session
        if session is None:
            self._finish_stream(discard=True, flush_tail=False)
            return
        completion = self.cancel_capture(session)
        if completion.status is CaptureCompletionStatus.FAILED:
            raise AudioCaptureError(completion.failure or CaptureFailure.NATIVE_ERROR)

    def close(self) -> bool:
        session = self.active_session
        if session is not None:
            completion = self.cancel_capture(session)
            if self.active_session is not None:
                logger.warning(
                    "audio capture close incomplete reason=%s",
                    completion.failure.value if completion.failure else "unknown",
                )
                return False
        else:
            _, cleanup_failure = self._finish_stream(
                discard=True,
                flush_tail=False,
            )
            if cleanup_failure is not None:
                logger.warning(
                    "audio capture close incomplete reason=%s",
                    cleanup_failure.value,
                )
                return False
        return not self.stream_open and not self.chunk_worker_alive


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
