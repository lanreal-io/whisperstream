"""Windows PortAudio device and microphone capture adapter.

The shared schema lives in :mod:`whisperstream.capture_contract`. PortAudio
indices, host API records, stream objects, and native failures remain private
to this module. ``AudioCapture`` is retained as the Windows compatibility name.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import sounddevice as sd

from whisperstream.chunking import ChunkBuffer
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

logger = logging.getLogger(__name__)

# Track whether the first enumeration log has been emitted (module-level, resets on import).
_devices_enumerated_logged = False


class AudioCaptureError(RuntimeError):
    """Legacy exception facade for a typed capture-contract failure."""

    def __init__(self, failure: CaptureFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


class _OwnedInputStream(sd.InputStream):
    """Retain the wrapper before its fallible post-open initialization.

    sounddevice 0.5.3 can raise after Pa_OpenStream but before the constructor
    returns. The owner must retain its CFFI callbacks even on that path.
    """

    def __init__(self, *, owner, **kwargs):
        owner._stream = self
        super().__init__(**kwargs)


@dataclass(frozen=True, slots=True)
class _NativeInputDevice:
    """Adapter-private PortAudio record; never returned to shared callers."""

    index: int
    name: str
    hostapi_index: int
    hostapi_name: str
    max_input_channels: int
    default_sample_rate: float
    logical_id: str
    is_default: bool


@dataclass(frozen=True, slots=True)
class PreviewAudioSnapshot:
    """One immutable, bounded view of preview-only captured audio."""

    audio: np.ndarray
    total_samples: int
    start_sample: int


class _PreviewAudioRing:
    """Bounded append-only audio tail independent from ``ChunkBuffer``."""

    def __init__(self, max_samples: int) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        self._max_samples = max_samples
        self._chunks: deque[np.ndarray] = deque()
        self._stored_samples = 0
        self._total_samples = 0
        self._lock = threading.Lock()

    def append(self, audio: np.ndarray) -> None:
        samples = audio.reshape(-1)
        if samples.size == 0:
            return
        with self._lock:
            self._total_samples += samples.size
            if samples.size >= self._max_samples:
                self._chunks.clear()
                kept = samples[-self._max_samples :].astype(np.float32, copy=True)
                self._chunks.append(kept)
                self._stored_samples = kept.size
                return

            # AudioCapture owns ``samples`` through its authoritative frame
            # list, so retaining this immutable view adds no callback-time copy.
            self._chunks.append(samples)
            self._stored_samples += samples.size
            excess = self._stored_samples - self._max_samples
            while excess > 0 and self._chunks:
                first = self._chunks[0]
                if first.size <= excess:
                    self._chunks.popleft()
                    self._stored_samples -= first.size
                    excess -= first.size
                    continue
                # Copy a partially retained tail so its discarded prefix does
                # not remain alive through a NumPy view.
                self._chunks[0] = first[excess:].astype(np.float32, copy=True)
                self._stored_samples -= excess
                excess = 0

    def snapshot(self) -> PreviewAudioSnapshot:
        # Copy chunk references under the lock, then concatenate away from the
        # sounddevice callback's short append critical section.
        with self._lock:
            chunks = tuple(self._chunks)
            total_samples = self._total_samples
            stored_samples = self._stored_samples
        if not chunks:
            audio = np.empty(0, dtype=np.float32)
        elif len(chunks) == 1:
            audio = chunks[0].astype(np.float32, copy=True)
        else:
            audio = np.concatenate(chunks).astype(np.float32, copy=False)
        return PreviewAudioSnapshot(
            audio=audio,
            total_samples=total_samples,
            start_sample=total_samples - stored_samples,
        )

    def reset(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._stored_samples = 0
            self._total_samples = 0


def _logical_device_id(hostapi_name: str, name: str, ordinal: int) -> str:
    """Return an opaque runtime identity without exposing a native index."""
    material = "\0".join(
        (hostapi_name.casefold().strip(), name.casefold().strip(), str(ordinal))
    ).encode("utf-8", errors="replace")
    digest = hashlib.sha256(material).hexdigest()[:24]
    return f"windows-input-{digest}"


def _query_native_input_devices(*, display_only: bool) -> list[_NativeInputDevice]:
    """Enumerate adapter-private input records.

    The display inventory is WASAPI-only on Windows. Open resolution uses all
    host APIs so the existing MME preference can remain private to this module.
    """
    hostapis = tuple(sd.query_hostapis())
    wasapi_idx = next(
        (
            index
            for index, api in enumerate(hostapis)
            if "WASAPI" in str(api["name"])
        ),
        None,
    )
    raw_devices = tuple(sd.query_devices())

    default_index: int | None = None
    if display_only and wasapi_idx is not None:
        candidate = hostapis[wasapi_idx].get("default_input_device")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            default_index = candidate
    if default_index is None:
        try:
            candidate = sd.default.device[0]
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                default_index = candidate
        except Exception:  # noqa: BLE001 - read-only probe remains best effort
            pass

    duplicate_ordinals: dict[tuple[str, str], int] = {}
    records: list[_NativeInputDevice] = []
    for fallback_index, info in enumerate(raw_devices):
        channels = int(info["max_input_channels"])
        if channels <= 0:
            continue
        hostapi_index = int(info["hostapi"])
        if display_only and wasapi_idx is not None and hostapi_index != wasapi_idx:
            continue
        hostapi_name = str(hostapis[hostapi_index]["name"])
        name = str(info["name"])
        key = (hostapi_name.casefold().strip(), name.casefold().strip())
        ordinal = duplicate_ordinals.get(key, 0)
        duplicate_ordinals[key] = ordinal + 1
        native_index = int(info.get("index", fallback_index))
        records.append(
            _NativeInputDevice(
                index=native_index,
                name=name,
                hostapi_index=hostapi_index,
                hostapi_name=hostapi_name,
                max_input_channels=channels,
                default_sample_rate=float(info["default_samplerate"]),
                logical_id=_logical_device_id(hostapi_name, name, ordinal),
                is_default=native_index == default_index,
            )
        )
    return records


def enumerate_input_devices() -> CaptureInventory:
    """Return the typed WASAPI display inventory with opaque logical IDs."""
    records = _query_native_input_devices(display_only=True)
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


def list_input_devices() -> List[Dict[str, Any]]:
    """Return a list of available audio input devices.

    On Windows, filters to WASAPI host API only to avoid duplicate entries
    across MME/DirectSound/WDM-KS. Native PortAudio indices never leave the
    adapter; the ``id`` field is an opaque runtime logical identity. Device
    names remain the persisted Windows compatibility seam.
    """
    global _devices_enumerated_logged
    inventory = enumerate_input_devices()
    devices: List[Dict[str, Any]] = [
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
        logger.debug("audio devices enumerated count=%d", len(devices))
        _devices_enumerated_logged = True
    return devices


def _reinit_portaudio() -> None:
    """Force PortAudio to re-enumerate devices.

    PortAudio caches its device list at the first call to sd.query_devices().
    Devices plugged in or enabled after that point are invisible to
    sd.query_devices() until PortAudio is terminated and reinitialised. This
    helper exists so tests can monkeypatch it to a no-op.
    """
    sd._terminate()
    sd._initialize()
    global _devices_enumerated_logged
    _devices_enumerated_logged = False


def has_input_device() -> bool:
    """Return True if at least one audio input device is available.

    Fast path: returns True immediately when the cached enumeration shows at
    least one input device. Slow path: when the cached list is empty, force
    PortAudio to re-enumerate so devices plugged in or enabled after process
    start become visible without restarting the backend. Returns False if both
    checks come back empty or if any check raises.
    """
    try:
        if list_input_devices():
            return True
        # Cached list is empty. Force PortAudio to re-enumerate so a mic the
        # user just plugged in / enabled becomes visible to subsequent presses.
        try:
            _reinit_portaudio()
            logger.debug("audio device probe portaudio reinit")
        except Exception as exc:
            logger.warning("audio device portaudio reinit failed reason=%s", type(exc).__name__)
            return False
        return bool(list_input_devices())
    except Exception as exc:
        logger.warning("audio device probe failed reason=%s", type(exc).__name__)
        return False


def _resolve_device(name: str) -> Optional[int]:
    """Resolve a device name to its PortAudio index.

    Prefers MME host API over WASAPI because MME handles sample-rate
    conversion transparently and works from any thread (WASAPI shared
    mode requires COM initialization on the calling thread, which
    breaks when start() is called from the pynput hotkey thread).

    Falls back to any host API if MME isn't found.
    """
    if not name:
        return None
    name_lower = name.lower()
    mme_match: Optional[int] = None
    any_match: Optional[int] = None
    chosen_record: _NativeInputDevice | None = None
    for record in _query_native_input_devices(display_only=False):
        if name_lower not in record.name.lower():
            continue
        if "MME" in record.hostapi_name and mme_match is None:
            mme_match = record.index
        if any_match is None:
            any_match = record.index
    chosen = mme_match if mme_match is not None else any_match
    if chosen is not None:
        for record in _query_native_input_devices(display_only=False):
            if record.index == chosen:
                chosen_record = record
                break
        logger.debug(
            "audio device resolved selection=selected hostapi=%s",
            chosen_record.hostapi_name if chosen_record is not None else "unknown",
        )
    return chosen


class WindowsCaptureAdapter:
    """Windows PortAudio capture adapter implementing contract schema v1.

    ``start``/``stop``/``cancel`` remain as the compatibility surface used by
    the current Windows orchestrator. For the canonical 16 kHz format they
    delegate to the typed, generation-bound contract methods below.
    """

    def __init__(
        self,
        *,
        sample_rate: int = CAPTURE_SAMPLE_RATE,
        channels: int = CAPTURE_CHANNELS,
        dtype: str = CAPTURE_DTYPE,
        device: str = "",
        status_callback: Optional[Callable[[str], None]] = None,
        terminal_callback: CaptureTerminalCallback | None = None,
        chunk_callback: Optional[Callable[[np.ndarray], None]] = None,
        chunk_duration: float = 0.0,
        chunk_overlap: float = 0.0,
    ) -> None:
        CaptureFormat(
            sample_rate=sample_rate,
            channels=channels,
            dtype=dtype,
        )
        self.sample_rate = sample_rate
        self.channels = channels
        self.dtype = dtype
        self._device_name = device
        self._status_callback = status_callback
        self._terminal_callback = terminal_callback
        self._owner_epoch = allocate_capture_owner_epoch()
        self._level_callback: Optional[Callable[[float], None]] = None
        self._chunk_callback = chunk_callback
        self._chunk_duration = chunk_duration
        self._chunk_overlap = chunk_overlap
        self._stream: Optional[sd.InputStream] = None
        self._native_close_unconfirmed = False
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._chunk_buffer: Optional[ChunkBuffer] = None
        # Daemon worker that dispatches chunk callbacks off the audio thread.
        self._chunk_queue: queue.Queue[Optional[np.ndarray]] = queue.Queue()
        self._chunk_worker: Optional[threading.Thread] = None
        self._preview_callback: Optional[Callable[[], None]] = None
        self._preview_max_duration = 0.0
        self._preview_ring: Optional[_PreviewAudioRing] = None
        self._state_lock = threading.Lock()
        self._active_session: CaptureSession | None = None
        self._active_failure: CaptureFailure | None = None
        self._terminal_failure: CaptureFailure | None = None
        self._terminal_reported = False
        self._native_callback_token: object | None = None
        self._stopping = False
        self._generation = 0
        self._legacy_session: CaptureSession | None = None
        # The stream may be opened before the user-facing start cue. While this
        # gate is clear, callbacks discard input before it can reach any
        # authoritative, preview, chunking, or level consumer.
        self._capture_armed = threading.Event()
        self._capture_armed.set()

    @property
    def stream_open(self) -> bool:
        """Content-free lifecycle state used by cleanup verification."""
        return self._stream is not None

    @property
    def capture_available(self) -> bool:
        """Unknown native lifetime requires process exit, never pointer retry."""
        return not self._native_close_unconfirmed

    @property
    def cleanup_retryable(self) -> bool:
        """Only a worker drain after confirmed native release can be retried."""
        return not self._native_close_unconfirmed and self._stream is None

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

    def enumerate_input_devices(self) -> CaptureInventory:
        return enumerate_input_devices()

    def set_level_callback(self, callback: Optional[Callable[[float], None]]) -> None:
        """Set or clear the audio level callback for visual indicators."""
        self._level_callback = callback

    def set_preview_callback(
        self,
        callback: Optional[Callable[[], None]],
        *,
        max_duration: float = 0.0,
    ) -> None:
        """Configure bounded preview-only capture for the next recording."""
        self._preview_callback = callback
        self._preview_max_duration = max(0.0, float(max_duration))
        if callback is None and self._preview_ring is not None:
            self._preview_ring.reset()
            self._preview_ring = None

    def preview_snapshot(self) -> PreviewAudioSnapshot:
        """Return the latest preview tail without touching authoritative audio."""
        ring = self._preview_ring
        if ring is None:
            return PreviewAudioSnapshot(
                audio=np.empty(0, dtype=np.float32),
                total_samples=0,
                start_sample=0,
            )
        return ring.snapshot()

    def arm_capture(self) -> None:
        """Allow subsequent input callbacks into the recording pipeline."""
        self._capture_armed.set()

    def _chunk_dispatch_loop(self) -> None:
        """Daemon worker that drains the chunk queue and invokes chunk_callback.

        Runs on a dedicated thread so the sounddevice callback is never blocked
        by potentially slow consumer code.  A ``None`` sentinel stops the loop.
        """
        while True:
            chunk = self._chunk_queue.get()
            if chunk is None:
                break
            cb = self._chunk_callback
            if cb is not None:
                try:
                    cb(chunk)
                except Exception as exc:
                    self._record_failure(CaptureFailure.STREAM_FAILED)
                    logger.error(
                        "audio chunk callback failed origin=stream error_type=%s",
                        type(exc).__name__,
                    )

    def _stop_chunk_worker(self, *, discard_pending: bool = False) -> bool:
        """Signal the chunk dispatch worker to exit and report bounded cleanup."""
        if self._chunk_worker is not None and self._chunk_worker.is_alive():
            if discard_pending:
                while True:
                    try:
                        self._chunk_queue.get_nowait()
                    except queue.Empty:
                        break
            self._chunk_queue.put(None)  # sentinel
            self._chunk_worker.join(timeout=CAPTURE_STOP_TIMEOUT_SECONDS)
            if self._chunk_worker.is_alive():
                return False
        self._chunk_worker = None
        return True

    def _prepare_stream_work(self) -> None:
        if self._chunk_duration > 0 and self._chunk_callback is not None:
            self._chunk_buffer = ChunkBuffer(
                sample_rate=self.sample_rate,
                chunk_duration=self._chunk_duration,
                chunk_overlap=self._chunk_overlap,
            )
            while not self._chunk_queue.empty():
                try:
                    self._chunk_queue.get_nowait()
                except queue.Empty:
                    break
            self._chunk_worker = threading.Thread(
                target=self._chunk_dispatch_loop,
                daemon=True,
                name="chunk-dispatch",
            )
            self._chunk_worker.start()
        else:
            self._chunk_buffer = None

        preview_samples = int(round(self.sample_rate * self._preview_max_duration))
        if self._preview_callback is not None and preview_samples > 0:
            self._preview_ring = _PreviewAudioRing(preview_samples)
        else:
            self._preview_ring = None
        with self._lock:
            self._frames.clear()

    def _record_failure(self, failure: CaptureFailure) -> None:
        with self._state_lock:
            if self._active_session is not None and self._active_failure is None:
                self._active_failure = failure

    def _on_stream_finished(self, token: object) -> None:
        callback: Callable[[str], None] | None = None
        terminal_callback: CaptureTerminalCallback | None = None
        terminal_event: CaptureTerminalEvent | None = None
        with self._state_lock:
            if (
                self._active_session is not None
                and self._native_callback_token is token
                and not self._stopping
                and not self._terminal_reported
            ):
                self._active_failure = self._active_failure or CaptureFailure.DEVICE_LOST
                self._terminal_reported = True
                callback = self._status_callback
                terminal_callback = self._terminal_callback
                terminal_event = CaptureTerminalEvent(
                    request_id=self._active_session.request_id,
                    generation=self._active_session.generation,
                    failure=self._active_failure,
                    owner_epoch=self._owner_epoch,
                )
        if terminal_callback is not None and terminal_event is not None:
            try:
                terminal_callback(terminal_event)
            except Exception:  # noqa: BLE001 - native finish cleanup must continue
                logger.warning("audio terminal callback failed origin=device_loss")
        if callback is not None:
            try:
                assert terminal_event is not None
                callback(terminal_event.failure.value)
            except Exception:  # noqa: BLE001 - native finish cleanup must continue
                logger.warning("audio status callback failed origin=device_loss")

    def _stream_callback(
        self, indata: np.ndarray, _frames: int, _time, status, *, token: object
    ) -> None:
        with self._state_lock:
            if self._native_callback_token is not token:
                return
        try:
            status_cb = self._status_callback
            if status and status_cb is not None:
                try:
                    status_cb(str(status))
                except Exception:
                    logger.warning("audio status callback failed origin=stream")
            if not self._capture_armed.is_set():
                return
            data = np.asarray(indata, dtype=np.float32)
            if data.ndim > 1:
                if data.shape[1] == 1:
                    mono = data.reshape(-1).copy()
                else:
                    mono = np.mean(data, axis=1, dtype=np.float32)
            else:
                mono = data.reshape(-1).copy()
            mono = np.ascontiguousarray(mono, dtype=np.float32)
            with self._lock:
                self._frames.append(mono)
            preview_ring = self._preview_ring
            if preview_ring is not None:
                preview_ring.append(mono)
                preview_cb = self._preview_callback
                if preview_cb is not None:
                    try:
                        preview_cb()
                    except Exception as exc:
                        logger.error(
                            "audio preview callback failed origin=stream error_type=%s",
                            type(exc).__name__,
                        )
            chunk_buf = self._chunk_buffer
            if chunk_buf is not None:
                for chunk in chunk_buf.append(mono):
                    self._chunk_queue.put(chunk)
            cb = self._level_callback
            if cb is not None:
                rms = float(np.sqrt(np.mean(mono ** 2))) if mono.size else 0.0
                try:
                    cb(min(rms * 10.0, 1.0))
                except Exception:
                    logger.warning("audio level callback failed origin=stream")
        except Exception as exc:
            self._record_failure(CaptureFailure.STREAM_FAILED)
            logger.warning("audio callback failed error_type=%s", type(exc).__name__)
            raise sd.CallbackAbort from exc

    def _open_one_stream(self, device_index: int | None):
        stream = None
        token = object()
        with self._state_lock:
            self._native_callback_token = token
        try:
            stream = _OwnedInputStream(
                owner=self,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype=self.dtype,
                device=device_index,
                callback=lambda data, frames, timing, status: self._stream_callback(
                    data, frames, timing, status, token=token
                ),
                finished_callback=lambda: self._on_stream_finished(token),
            )
            stream.start()
            return stream
        except Exception:
            with self._state_lock:
                self._stopping = True
            owned = stream if stream is not None else self._stream
            if owned is not None:
                # Before successful Pa_OpenStream the wrapper has no _ptr, or
                # a PaStream ** output slot. Only PaStream * owns an open.
                ptr = getattr(owned, "_ptr", None)
                opened = stream is not None or (
                    ptr is not None and sd._ffi.typeof(ptr) == sd._ffi.typeof("PaStream *")
                    and ptr != sd._ffi.NULL
                )
                self._stream = owned
                if opened:
                    self._close_native_stream()
                else:
                    self._stream = None
            raise

    def _close_native_stream(self) -> bool:
        if self._native_close_unconfirmed:
            return False
        stream = self._stream
        if stream is None:
            return True
        try:
            stream.close(ignore_errors=False)
        except Exception:
            # The real wrapper clears _ptr even when Pa_CloseStream fails.
            # Retain the whole wrapper; neither retry nor replacement is safe.
            self._native_close_unconfirmed = True
            self._capture_armed.clear()
            return False
        self._stream = None
        with self._state_lock:
            self._native_callback_token = None
        return True

    def _resolve_request(
        self, request: CaptureRequest
    ) -> tuple[int | None, str, CaptureSelectionResolution, str]:
        display_records = _query_native_input_devices(display_only=True)
        all_records = _query_native_input_devices(display_only=False)
        if not all_records:
            raise AudioCaptureError(CaptureFailure.NO_INPUT_DEVICE)
        default_record = next(
            (record for record in display_records if record.is_default),
            None,
        )
        default_id = (
            default_record.logical_id
            if default_record is not None
            else "windows-system-default"
        )
        selection = request.selection
        if selection.uses_system_default:
            return (
                None,
                default_id,
                CaptureSelectionResolution.SYSTEM_DEFAULT,
                "default",
            )

        selected_display: _NativeInputDevice | None = None
        selected_name: str | None = None
        if selection.preferred_id is not None:
            selected_display = next(
                (
                    record
                    for record in display_records
                    if record.logical_id == selection.preferred_id
                ),
                None,
            )
            if selected_display is not None:
                selected_name = selected_display.name
        if selected_name is None and selection.legacy_name is not None:
            selected_name = selection.legacy_name
            name_lower = selected_name.casefold()
            selected_display = next(
                (
                    record
                    for record in display_records
                    if name_lower in record.name.casefold()
                ),
                None,
            )
        if selected_name is None:
            return (
                None,
                default_id,
                CaptureSelectionResolution.FALLBACK_MISSING,
                "default",
            )
        device_index = _resolve_device(selected_name)
        if device_index is None:
            logger.warning(
                "audio device not found selection=selected fallback=system_default"
            )
            return (
                None,
                default_id,
                CaptureSelectionResolution.FALLBACK_MISSING,
                "default",
            )
        selected_id = (
            selected_display.logical_id
            if selected_display is not None
            else _logical_device_id("windows-compatible", selected_name, 0)
        )
        selected_record = next(
            (record for record in all_records if record.index == device_index),
            None,
        )
        hostapi_name = (
            selected_record.hostapi_name if selected_record is not None else "unknown"
        )
        return (
            device_index,
            selected_id,
            CaptureSelectionResolution.SELECTED,
            hostapi_name,
        )

    def start_capture(
        self,
        request: CaptureRequest,
        *,
        on_level: Callable[[float], None] | None = None,
        on_chunk: Callable[[np.ndarray], None] | None = None,
    ) -> CaptureStartOutcome:
        with self._state_lock:
            if self._active_session is not None or not self.capture_available:
                return CaptureStartOutcome(failure=CaptureFailure.NATIVE_ERROR)
        try:
            device_index, device_id, resolution, hostapi_name = self._resolve_request(
                request
            )
        except AudioCaptureError as exc:
            return CaptureStartOutcome(failure=exc.failure)
        except Exception:
            return CaptureStartOutcome(failure=CaptureFailure.NATIVE_ERROR)

        session = CaptureSession(
            request_id=request.request_id,
            generation=request.generation,
            device_id=device_id,
            selection_resolution=resolution,
        )
        with self._state_lock:
            self._active_session = session
            self._active_failure = None
            self._terminal_failure = None
            self._terminal_reported = False
            self._stopping = False
        self._level_callback = on_level
        self._chunk_callback = on_chunk
        try:
            self._prepare_stream_work()
        except Exception as exc:
            logger.warning("audio capture prepare failed error_type=%s", type(exc).__name__)
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
                failure=cleanup_failure or CaptureFailure.NATIVE_ERROR
            )
        try:
            try:
                self._stream = self._open_one_stream(device_index)
            except Exception as selected_exc:
                if device_index is None or self._stream is not None:
                    raise selected_exc
                # A released first stream can still have queued/running chunk
                # work. Drain and discard it before admitting fallback audio.
                _, cleanup_failure = self._finish_stream(discard=True, flush_tail=False)
                if cleanup_failure is not None:
                    raise AudioCaptureError(cleanup_failure) from selected_exc
                with self._state_lock:
                    self._active_failure = None
                    self._terminal_reported = False
                    self._stopping = False
                self._prepare_stream_work()
                logger.warning(
                    "audio device open failed selection=selected error_type=%s fallback=system_default",
                    type(selected_exc).__name__,
                )
                self._stream = self._open_one_stream(None)
                default_record = next(
                    (
                        record
                        for record in _query_native_input_devices(display_only=True)
                        if record.is_default
                    ),
                    None,
                )
                session = CaptureSession(
                    request_id=request.request_id,
                    generation=request.generation,
                    device_id=(
                        default_record.logical_id
                        if default_record is not None
                        else "windows-system-default"
                    ),
                    selection_resolution=CaptureSelectionResolution.FALLBACK_OPEN_FAILED,
                )
                with self._state_lock:
                    self._active_session = session
                hostapi_name = "default"
            actual_rate = int(round(float(self._stream.samplerate)))
            if actual_rate != CAPTURE_SAMPLE_RATE:
                raise RuntimeError("capture stream did not accept canonical sample rate")
        except Exception as exc:
            logger.warning("audio capture open failed error_type=%s", type(exc).__name__)
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
            "audio device opened resolution=%s hostapi=%s sample_rate=%d channels=%d",
            session.selection_resolution.value,
            hostapi_name,
            CAPTURE_SAMPLE_RATE,
            CAPTURE_CHANNELS,
        )
        return CaptureStartOutcome(session=session)

    def start(self, *, discard_until_armed: bool = False) -> None:
        """Compatibility start used by the current orchestrator."""
        if self.active_session is not None:
            raise AudioCaptureError(CaptureFailure.NATIVE_ERROR)
        if discard_until_armed:
            self._capture_armed.clear()
        else:
            self._capture_armed.set()
        self._generation += 1
        request = CaptureRequest(
            request_id=f"windows-capture-{self._generation}",
            generation=self._generation,
            selection=CaptureSelection(legacy_name=self._device_name or None),
        )
        outcome = self.start_capture(
            request,
            on_level=self._level_callback,
            on_chunk=self._chunk_callback,
        )
        if outcome.failure is not None:
            raise AudioCaptureError(outcome.failure)
        self._legacy_session = outcome.session

    def _finish_stream(
        self,
        *,
        discard: bool,
        flush_tail: bool,
    ) -> tuple[np.ndarray, CaptureFailure | None]:
        """Release exact native and worker state under the app owner lock."""
        stream = self._stream
        cleanup_failure: CaptureFailure | None = None
        if self._native_close_unconfirmed:
            return np.empty(0, dtype=np.float32), CaptureFailure.CLEANUP_FAILED
        if stream is not None:
            try:
                stream.stop(ignore_errors=False)
            except sd.PortAudioError as exc:
                # A callback-aborted stream may already be stopped.
                if len(exc.args) < 2 or exc.args[1] != sd._lib.paStreamIsStopped:
                    cleanup_failure = CaptureFailure.CLEANUP_FAILED
            except Exception:
                cleanup_failure = CaptureFailure.CLEANUP_FAILED
            if not self._close_native_stream():
                # Callback/worker/buffer references remain owned until exit.
                return np.empty(0, dtype=np.float32), CaptureFailure.CLEANUP_FAILED

        with self._state_lock:
            discard = (discard or self._active_failure is not None
                       or self._terminal_failure is not None or cleanup_failure is not None)

        chunk_buf = self._chunk_buffer
        if chunk_buf is not None:
            try:
                if flush_tail and not discard:
                    tail = chunk_buf.flush_tail()
                    if tail is not None and self._chunk_callback is not None:
                        self._chunk_callback(tail)
            except Exception as exc:
                self._record_failure(CaptureFailure.STREAM_FAILED)
                discard = True
                logger.error(
                    "audio chunk callback failed origin=tail_flush error_type=%s",
                    type(exc).__name__,
                )
            finally:
                self._chunk_buffer = None

        if not self._stop_chunk_worker(discard_pending=discard):
            cleanup_failure = CaptureFailure.TIMEOUT
        if self._preview_ring is not None:
            self._preview_ring.reset()
            self._preview_ring = None

        with self._lock:
            frames = tuple(self._frames)
            self._frames.clear()
        if discard or not frames:
            audio = np.empty(0, dtype=np.float32)
        elif len(frames) == 1:
            audio = frames[0].astype(np.float32, copy=True)
        else:
            audio = np.concatenate(frames).astype(np.float32, copy=False)
        return np.ascontiguousarray(audio.reshape(-1), dtype=np.float32), cleanup_failure

    def _matches_active_session(self, session: CaptureSession) -> bool:
        with self._state_lock:
            return self._active_session == session

    def stop_capture(self, session: CaptureSession) -> CaptureCompletion:
        if not self._matches_active_session(session):
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
        with self._state_lock:
            failure = cleanup_failure or self._terminal_failure or self._active_failure or failure
            self._terminal_failure = failure
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
        if not self._matches_active_session(session):
            return CaptureCompletion(
                request_id=session.request_id,
                generation=session.generation,
                status=CaptureCompletionStatus.FAILED,
                failure=CaptureFailure.STALE_GENERATION,
            )
        with self._state_lock:
            self._stopping = True
            failure = self._active_failure
        _, cleanup_failure = self._finish_stream(discard=True, flush_tail=False)
        with self._state_lock:
            failure = cleanup_failure or self._terminal_failure or self._active_failure or failure
            self._terminal_failure = failure
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

    def stop(self) -> np.ndarray:
        """Compatibility stop returning the authoritative float32 waveform."""
        session = self._legacy_session or self.active_session
        if session is None:
            return np.empty(0, dtype=np.float32)
        completion = self.stop_capture(session)
        if completion.status is CaptureCompletionStatus.FAILED:
            raise AudioCaptureError(completion.failure or CaptureFailure.NATIVE_ERROR)
        assert completion.audio is not None
        return completion.audio

    def cancel(self) -> None:
        """Compatibility cancellation that closes and discards exact capture."""
        session = self._legacy_session or self.active_session
        if session is None:
            self._finish_stream(discard=True, flush_tail=False)
            return
        completion = self.cancel_capture(session)
        if completion.status is CaptureCompletionStatus.FAILED:
            raise AudioCaptureError(completion.failure or CaptureFailure.NATIVE_ERROR)

    def close(self) -> bool:
        """Idempotently release capture and report bounded teardown truth."""
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


# Keep the long-standing import/class name while making the Windows boundary
# explicit for the later platform selector.
AudioCapture = WindowsCaptureAdapter
