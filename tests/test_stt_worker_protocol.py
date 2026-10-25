from __future__ import annotations

import io
import struct

import numpy as np
import pytest

import whisperstream.stt_worker_protocol as protocol
from whisperstream.stt_worker_protocol import (
    FRAME_MAGIC,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    SttWorkerEof,
    SttWorkerProtocolError,
    SttWorkerProtocolMismatch,
    SttWorkerValidationError,
    decode_audio_request,
    encode_audio_request,
    encode_frame,
    read_frame,
    request_header,
    success_reply,
    validate_reply,
    validate_request,
    write_frame,
)


def test_binary_frame_round_trip_preserves_header_and_payload() -> None:
    header = request_header(
        request_id="request-1",
        operation="transcribe_chunk",
        backend="prototype",
        body={"metadata_only": True},
    )
    payload = bytes(range(32))
    stream = io.BytesIO()

    write_frame(stream, header, payload)
    stream.seek(0)
    decoded = read_frame(stream)

    assert decoded.header == header
    assert decoded.payload == payload


def test_frame_rejects_bad_magic_and_partial_eof() -> None:
    bad_magic = struct.pack(">4sII", b"BAD!", 2, 0) + b"{}"
    with pytest.raises(SttWorkerProtocolError, match="magic"):
        read_frame(io.BytesIO(bad_magic))

    partial = struct.pack(">4sII", FRAME_MAGIC, 4, 0) + b"{}"
    with pytest.raises(SttWorkerProtocolError, match="unexpected EOF"):
        read_frame(io.BytesIO(partial))

    with pytest.raises(SttWorkerEof):
        read_frame(io.BytesIO())


def test_frame_enforces_header_and_payload_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "MAX_HEADER_BYTES", 16)
    with pytest.raises(SttWorkerProtocolError, match="header"):
        encode_frame({"value": "longer than cap"})

    monkeypatch.setattr(protocol, "MAX_PAYLOAD_BYTES", 3)
    with pytest.raises(SttWorkerProtocolError, match="payload"):
        encode_frame({}, b"four")


def test_request_and_reply_validate_protocol_and_correlation() -> None:
    request = validate_request(
        request_header(
            request_id="request-2",
            operation="health",
            backend="prototype",
        )
    )
    reply = success_reply(request, body={"state": "idle"})

    validate_reply(reply, request=request)
    stale_reply = dict(reply, request_id="request-stale")
    with pytest.raises(SttWorkerProtocolError, match="request_id"):
        validate_reply(stale_reply, request=request)


def test_protocol_mismatch_is_typed_before_operation_dispatch() -> None:
    header = request_header(
        request_id="request-3",
        operation="hello",
        backend="prototype",
        version=PROTOCOL_VERSION + 1,
    )

    with pytest.raises(SttWorkerProtocolMismatch, match="supported"):
        validate_request(header)

    wrong_name = dict(header, protocol="another-protocol", version=PROTOCOL_VERSION)
    with pytest.raises(SttWorkerProtocolMismatch):
        validate_request(wrong_name)


def test_request_rejects_unbounded_or_nonportable_identifiers() -> None:
    invalid = request_header(
        request_id="contains spaces",
        operation="health",
        backend="prototype",
    )
    with pytest.raises(SttWorkerValidationError, match="request_id"):
        validate_request(invalid)

    invalid_backend = dict(invalid, request_id="valid", backend="Windows\\handle")
    with pytest.raises(SttWorkerValidationError, match="backend"):
        validate_request(invalid_backend)


def test_audio_payload_round_trip_is_little_endian_float32_copy() -> None:
    audio = np.arange(12, dtype=np.int16).reshape(6, 2)
    body, payload = encode_audio_request(audio, sample_rate=np.int64(16_000))
    request = validate_request(
        request_header(
            request_id="request-4",
            operation="transcribe",
            backend="prototype",
            body=body,
        )
    )

    decoded, sample_rate = decode_audio_request(request, payload)

    assert sample_rate == 16_000
    assert decoded.dtype == np.dtype("float32")
    assert decoded.flags.owndata
    np.testing.assert_array_equal(decoded, np.arange(12, dtype=np.float32))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("dtype", "float64-le", "dtype"),
        ("shape", [2, 2], "shape"),
        ("sample_count", 99, "sample_count"),
        ("sample_rate", 0, "sample_rate"),
        ("byte_length", 1, "byte length"),
    ],
)
def test_audio_payload_rejects_malformed_array_metadata(
    field: str,
    value: object,
    message: str,
) -> None:
    body, payload = encode_audio_request(
        np.zeros(4, dtype=np.float32), sample_rate=16_000
    )
    body["audio"][field] = value
    request = validate_request(
        request_header(
            request_id="request-5",
            operation="transcribe",
            backend="prototype",
            body=body,
        )
    )

    with pytest.raises(SttWorkerValidationError, match=message):
        decode_audio_request(request, payload)


def test_protocol_module_exposes_no_pickle_or_platform_transport() -> None:
    source = open(protocol.__file__, encoding="utf-8").read().lower()

    assert PROTOCOL_NAME in source
    for forbidden in (
        "import pickle",
        "multiprocessing.connection",
        "namedpipe",
        "createfilew",
        "af_pipe",
        "import socket",
        "import sherpa",
        "import onnxruntime",
    ):
        assert forbidden not in source
