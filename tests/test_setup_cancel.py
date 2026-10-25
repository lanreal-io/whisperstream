"""Cancellation contracts for verified Hugging Face model downloads."""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


from whisperstream.model_downloader import (
    DOWNLOAD_REGISTRY,
    DownloadCancelled,
    ModelId,
    _download_from_hf,
    download_file_with_sha256,
)


def _stream_context(chunks) -> MagicMock:  # noqa: ANN001
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.iter_bytes = MagicMock(return_value=iter(chunks))
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=response)
    context.__exit__ = MagicMock(return_value=False)
    return context


def test_download_cancelled_exception_exists() -> None:
    assert issubclass(DownloadCancelled, Exception)


def test_verified_stream_cancel_cleans_atomic_scratch(tmp_path: Path) -> None:
    cancel_event = threading.Event()

    def chunks():
        yield b"first"
        cancel_event.set()
        yield b"second"

    client = MagicMock()
    client.stream = MagicMock(return_value=_stream_context(chunks()))
    dest = tmp_path / "model.bin"

    with pytest.raises(DownloadCancelled):
        download_file_with_sha256(
            client,
            "https://huggingface.co/test/model.bin",
            dest,
            hashlib.sha256(b"firstsecond").hexdigest(),
            on_chunk=lambda _size: None,
            cancel_event=cancel_event,
        )

    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".tmp").exists()


def test_hf_path_raises_download_cancelled_when_event_set(tmp_path: Path) -> None:
    spec = DOWNLOAD_REGISTRY[ModelId.PARAKEET_V3_INT8]
    payload = b"firstsecond"
    cancel_event = threading.Event()
    manifest = {
        "models": {
            spec.id: {
                "int8": {
                    "files": [
                        {
                            "name": "encoder.int8.onnx",
                            "url": "https://huggingface.co/test/encoder.int8.onnx",
                            "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    ]
                }
            }
        }
    }
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    manifest_response = MagicMock()
    manifest_response.raise_for_status = MagicMock()
    manifest_response.json = MagicMock(return_value=manifest)
    client.get = MagicMock(return_value=manifest_response)

    def chunks():
        yield b"first"
        cancel_event.set()
        yield b"second"

    client.stream = MagicMock(return_value=_stream_context(chunks()))

    with patch("whisperstream.model_downloader.new_http_client", return_value=client):
        with pytest.raises(DownloadCancelled):
            _download_from_hf(
                spec,
                tmp_path,
                ["encoder.int8.onnx"],
                None,
                cancel_event,
                logging.getLogger(__name__),
            )

    dest = tmp_path / "encoder.int8.onnx"
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".tmp").exists()
    assert client.stream.call_count == 1


def test_download_llm_model_cancel_cleans_up_tmp(tmp_path: Path) -> None:
    from whisperstream.setup import download_llm_model

    spec = DOWNLOAD_REGISTRY[ModelId.GEMMA_4_E2B_Q4]
    filename = spec.files[0]
    payload = b"firstsecond"
    cancel_event = threading.Event()
    manifest = {
        "models": {
            spec.id: {
                "q4_k_m": {
                    "files": [
                        {
                            "name": filename,
                            "url": f"https://huggingface.co/test/{filename}",
                            "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    ]
                }
            }
        }
    }
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    manifest_response = MagicMock()
    manifest_response.raise_for_status = MagicMock()
    manifest_response.json = MagicMock(return_value=manifest)
    client.get = MagicMock(return_value=manifest_response)

    def chunks():
        yield b"first"
        cancel_event.set()
        yield b"second"

    client.stream = MagicMock(return_value=_stream_context(chunks()))
    gguf_path = tmp_path / "local-llm" / filename

    with patch("whisperstream.model_downloader.new_http_client", return_value=client):
        with pytest.raises(DownloadCancelled):
            download_llm_model(
                ModelId.GEMMA_4_E2B_Q4.value,
                gguf_path,
                cancel_event=cancel_event,
            )

    assert not gguf_path.exists()
    assert not gguf_path.with_suffix(gguf_path.suffix + ".tmp").exists()
