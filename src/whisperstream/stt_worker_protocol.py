"""Versioned private protocol for supervised speech-to-text workers.

The protocol deliberately uses only anonymous byte streams, UTF-8 JSON, and
little-endian float32 audio.  It contains no Python pickles, Windows handles,
native-engine objects, or platform paths, so the later macOS worker can
implement the same boundary without embedding Python-specific transport code.

DO NOT LOG request/reply bodies or binary payloads.  They may contain audio,
transcripts, hotwords, or language hints.  Request IDs, operation names,
backend IDs, byte/sample counts, timings, and error classifications are the
only protocol metadata safe for routine diagnostics.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from numbers import Integral
from typing import Any, BinaryIO, Mapping

import numpy as np

from .stt_contracts import InvalidSttAudio, normalize_stt_audio


PROTOCOL_NAME = "whisperstream-stt"
PROTOCOL_VERSION = 1
FRAME_MAGIC = b"WST1"

# A v1 frame is: magic (4 bytes), JSON header length (u32 big endian), binary
# payload length (u32 big endian), compact UTF-8 JSON, then the raw payload.
_FRAME_PREFIX = struct.Struct(">4sII")
FRAME_PREFIX_BYTES = _FRAME_PREFIX.size

MAX_HEADER_BYTES = 256 * 1024
MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_AUDIO_SAMPLES = MAX_PAYLOAD_BYTES // 4
MAX_REQUEST_ID_BYTES = 128

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_BACKEND_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class SttWorkerProtocolError(RuntimeError):
    """A frame or envelope violates the private worker protocol."""

    code = "protocol_error"


class SttWorkerEof(EOFError):
    """The peer closed the stream at a frame boundary."""


class SttWorkerProtocolMismatch(SttWorkerProtocolError):
    """The peer requested a protocol name or version we cannot speak."""

    code = "protocol_mismatch"


class SttWorkerValidationError(SttWorkerProtocolError):
    """A syntactically valid envelope contains invalid request data."""

    code = "validation_error"


class SttWorkerCapabilityError(SttWorkerProtocolError):
    """A valid optional operation is unsupported by this engine."""

    code = "capability_unsupported"


@dataclass(frozen=True, slots=True)
class SttWorkerFrame:
    """One decoded frame.  ``payload`` is opaque until operation validation."""

    header: dict[str, Any]
    payload: bytes


@dataclass(frozen=True, slots=True)
class ValidatedRequest:
    """Content-free routing metadata from a validated request envelope."""

    request_id: str
    operation: str
    backend: str
    body: dict[str, Any]


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        part = stream.read(length - len(chunks))
        if not part:
            if not chunks:
                raise SttWorkerEof("worker stream reached EOF")
            raise SttWorkerProtocolError("unexpected EOF inside worker frame")
        chunks.extend(part)
    return bytes(chunks)


def encode_frame(
    header: Mapping[str, Any], payload: bytes | bytearray | memoryview = b""
) -> bytes:
    """Encode one bounded frame for a byte stream."""

    if not isinstance(header, Mapping):
        raise SttWorkerProtocolError("frame header must be an object")
    try:
        header_bytes = json.dumps(
            dict(header),
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SttWorkerProtocolError("frame header is not valid JSON") from exc

    payload_bytes = bytes(payload)
    if not header_bytes or len(header_bytes) > MAX_HEADER_BYTES:
        raise SttWorkerProtocolError(
            f"frame header must be 1..{MAX_HEADER_BYTES} bytes"
        )
    if len(payload_bytes) > MAX_PAYLOAD_BYTES:
        raise SttWorkerProtocolError(
            f"frame payload exceeds {MAX_PAYLOAD_BYTES} bytes"
        )
    return (
        _FRAME_PREFIX.pack(FRAME_MAGIC, len(header_bytes), len(payload_bytes))
        + header_bytes
        + payload_bytes
    )


def write_frame(
    stream: BinaryIO,
    header: Mapping[str, Any],
    payload: bytes | bytearray | memoryview = b"",
) -> None:
    """Write and flush one complete frame."""

    stream.write(encode_frame(header, payload))
    stream.flush()


def read_frame(stream: BinaryIO) -> SttWorkerFrame:
    """Read one complete bounded frame from *stream*."""

    prefix = _read_exact(stream, FRAME_PREFIX_BYTES)
    magic, header_length, payload_length = _FRAME_PREFIX.unpack(prefix)
    if magic != FRAME_MAGIC:
        raise SttWorkerProtocolError("invalid worker frame magic")
    if not 0 < header_length <= MAX_HEADER_BYTES:
        raise SttWorkerProtocolError("invalid worker frame header length")
    if payload_length > MAX_PAYLOAD_BYTES:
        raise SttWorkerProtocolError("invalid worker frame payload length")

    header_bytes = _read_exact(stream, header_length)
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SttWorkerProtocolError("worker frame header is not valid UTF-8 JSON") from exc
    if not isinstance(header, dict):
        raise SttWorkerProtocolError("worker frame header must be a JSON object")
    payload = _read_exact(stream, payload_length) if payload_length else b""
    return SttWorkerFrame(header=header, payload=payload)


def request_header(
    *,
    request_id: str,
    operation: str,
    backend: str,
    body: Mapping[str, Any] | None = None,
    version: int = PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Build a request envelope.  Validation still occurs at the receiver."""

    return {
        "protocol": PROTOCOL_NAME,
        "version": version,
        "kind": "request",
        "request_id": request_id,
        "operation": operation,
        "backend": backend,
        "body": dict(body or {}),
    }


def success_reply(
    request: ValidatedRequest,
    *,
    body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a correlated success reply."""

    return {
        "protocol": PROTOCOL_NAME,
        "version": PROTOCOL_VERSION,
        "kind": "reply",
        "request_id": request.request_id,
        "operation": request.operation,
        "backend": request.backend,
        "ok": True,
        "body": dict(body or {}),
    }


def error_reply(
    *,
    request_id: str | None,
    operation: str | None,
    backend: str | None,
    code: str,
    category: str,
    message: str,
    exception_type: str | None = None,
    traceback_text: str | None = None,
) -> dict[str, Any]:
    """Build a structured failure reply without request content."""

    error: dict[str, Any] = {
        "code": code,
        "category": category,
        "message": _bounded_single_line(message, 2_048),
    }
    if exception_type is not None:
        error["exception_type"] = _bounded_single_line(exception_type, 256)
    if traceback_text is not None:
        # The frame itself remains bounded.  192 KiB retains complete ordinary
        # tracebacks while reserving headroom for the envelope.
        error["traceback"] = traceback_text[-(192 * 1024) :]
    return {
        "protocol": PROTOCOL_NAME,
        "version": PROTOCOL_VERSION,
        "kind": "reply",
        "request_id": request_id,
        "operation": operation,
        "backend": backend,
        "ok": False,
        "error": error,
    }


def validate_request(header: Mapping[str, Any]) -> ValidatedRequest:
    """Validate routing and correlation fields without inspecting content."""

    if header.get("protocol") != PROTOCOL_NAME:
        raise SttWorkerProtocolMismatch("unsupported STT worker protocol")
    version = header.get("version")
    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise SttWorkerProtocolMismatch(
            f"unsupported STT worker protocol version; supported={PROTOCOL_VERSION}"
        )
    if header.get("kind") != "request":
        raise SttWorkerValidationError("worker envelope kind must be request")

    request_id = header.get("request_id")
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id.encode("utf-8")) > MAX_REQUEST_ID_BYTES
        or _IDENTIFIER_RE.fullmatch(request_id) is None
    ):
        raise SttWorkerValidationError("invalid request_id")
    operation = header.get("operation")
    if (
        not isinstance(operation, str)
        or not operation
        or len(operation) > 64
        or _IDENTIFIER_RE.fullmatch(operation) is None
    ):
        raise SttWorkerValidationError("invalid operation")
    backend = header.get("backend")
    if (
        not isinstance(backend, str)
        or not backend
        or len(backend) > 64
        or _BACKEND_RE.fullmatch(backend) is None
    ):
        raise SttWorkerValidationError("invalid backend")
    body = header.get("body", {})
    if not isinstance(body, dict):
        raise SttWorkerValidationError("request body must be an object")
    return ValidatedRequest(
        request_id=request_id,
        operation=operation,
        backend=backend,
        body=body,
    )


def validate_reply(header: Mapping[str, Any], *, request: ValidatedRequest) -> None:
    """Reject malformed, stale, or cross-operation replies."""

    if header.get("protocol") != PROTOCOL_NAME:
        raise SttWorkerProtocolMismatch("reply protocol mismatch")
    if header.get("version") != PROTOCOL_VERSION:
        raise SttWorkerProtocolMismatch("reply protocol version mismatch")
    if header.get("kind") != "reply":
        raise SttWorkerProtocolError("worker response is not a reply")
    if header.get("request_id") != request.request_id:
        raise SttWorkerProtocolError(
            "worker reply request_id mismatch "
            f"expected={request.request_id} actual={header.get('request_id')}"
        )
    if header.get("operation") != request.operation:
        raise SttWorkerProtocolError("worker reply operation mismatch")
    if header.get("backend") != request.backend:
        raise SttWorkerProtocolError("worker reply backend mismatch")
    if not isinstance(header.get("ok"), bool):
        raise SttWorkerProtocolError("worker reply is missing boolean ok")
    if header["ok"]:
        if not isinstance(header.get("body", {}), dict):
            raise SttWorkerProtocolError("worker success body must be an object")
    elif not isinstance(header.get("error"), dict):
        raise SttWorkerProtocolError("worker error reply is missing error object")


def encode_audio_request(
    audio: np.ndarray,
    *,
    sample_rate: int,
) -> tuple[dict[str, Any], bytes]:
    """Normalize audio and return v1 metadata plus raw little-endian bytes."""

    waveform = normalize_stt_audio(audio, sample_rate=sample_rate)
    if waveform.size > MAX_AUDIO_SAMPLES:
        raise InvalidSttAudio(
            f"audio exceeds protocol limit of {MAX_AUDIO_SAMPLES} samples"
        )
    little_endian = waveform.astype("<f4", copy=False)
    payload = little_endian.tobytes(order="C")
    return (
        {
            "audio": {
                "dtype": "float32-le",
                "shape": [int(waveform.size)],
                "sample_count": int(waveform.size),
                "sample_rate": int(sample_rate),
                "byte_length": len(payload),
            }
        },
        payload,
    )


def decode_audio_request(
    request: ValidatedRequest,
    payload: bytes,
) -> tuple[np.ndarray, int]:
    """Validate declared array metadata before exposing audio to native code."""

    audio = request.body.get("audio")
    if not isinstance(audio, dict):
        raise SttWorkerValidationError("audio metadata must be an object")
    if audio.get("dtype") != "float32-le":
        raise SttWorkerValidationError("audio dtype must be float32-le")
    shape = audio.get("shape")
    if (
        not isinstance(shape, list)
        or len(shape) != 1
        or isinstance(shape[0], bool)
        or not isinstance(shape[0], Integral)
        or shape[0] < 0
    ):
        raise SttWorkerValidationError("audio shape must be one non-negative dimension")
    sample_count = audio.get("sample_count")
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, Integral)
        or sample_count < 0
        or sample_count != shape[0]
        or sample_count > MAX_AUDIO_SAMPLES
    ):
        raise SttWorkerValidationError("audio sample_count does not match shape")
    sample_rate = audio.get("sample_rate")
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, Integral)
        or sample_rate <= 0
    ):
        raise SttWorkerValidationError("audio sample_rate must be a positive integer")
    byte_length = audio.get("byte_length")
    expected_length = int(sample_count) * 4
    if (
        isinstance(byte_length, bool)
        or not isinstance(byte_length, Integral)
        or byte_length != expected_length
        or len(payload) != expected_length
    ):
        raise SttWorkerValidationError("audio byte length does not match metadata")

    # Copy before returning so the native engine never retains a view backed by
    # a temporary protocol frame.
    waveform = np.frombuffer(payload, dtype="<f4", count=int(sample_count)).copy()
    return waveform, int(sample_rate)


def _bounded_single_line(value: str, limit: int) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:limit]


__all__ = [
    "FRAME_MAGIC",
    "FRAME_PREFIX_BYTES",
    "MAX_AUDIO_SAMPLES",
    "MAX_HEADER_BYTES",
    "MAX_PAYLOAD_BYTES",
    "PROTOCOL_NAME",
    "PROTOCOL_VERSION",
    "SttWorkerEof",
    "SttWorkerCapabilityError",
    "SttWorkerFrame",
    "SttWorkerProtocolError",
    "SttWorkerProtocolMismatch",
    "SttWorkerValidationError",
    "ValidatedRequest",
    "decode_audio_request",
    "encode_audio_request",
    "encode_frame",
    "error_reply",
    "read_frame",
    "request_header",
    "success_reply",
    "validate_reply",
    "validate_request",
    "write_frame",
]
