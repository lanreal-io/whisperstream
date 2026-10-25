"""Focused tests for Qwen's R2-only download shim."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


from whisperstream.model_downloader import DownloadCancelled, ModelId
from whisperstream.qwen_download import download_qwen_model
from whisperstream.qwen_models import get_qwen_model


_ENTRY = get_qwen_model(ModelId.QWEN3_ASR_06B_INT8.value)
assert _ENTRY is not None
_FILES = tuple(_ENTRY["files"])
_R2_BASE = "https://huggingface.co/test/qwen/resolve/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"


def _manifest(payloads: dict[str, bytes]) -> dict:
    return {
        "models": {
            ModelId.QWEN3_ASR_06B_INT8.value: {
                "int8": {
                    "files": [
                        {
                            "name": path,
                            "url": f"{_R2_BASE}{path}",
                            "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                        for path, payload in payloads.items()
                    ]
                }
            }
        }
    }


def _client_for(payloads: dict[str, bytes]) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    manifest_response = MagicMock()
    manifest_response.raise_for_status = MagicMock()
    manifest_response.json = MagicMock(return_value=_manifest(payloads))
    client.get = MagicMock(return_value=manifest_response)

    def stream(_method: str, url: str) -> MagicMock:
        rel_path = url.removeprefix(_R2_BASE)
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.iter_bytes = MagicMock(return_value=iter([payloads[rel_path]]))
        context = MagicMock()
        context.__enter__ = MagicMock(return_value=response)
        context.__exit__ = MagicMock(return_value=False)
        return context

    client.stream = MagicMock(side_effect=stream)
    return client


def test_shim_delegates_to_qwen_registry_model(tmp_path: Path) -> None:
    progress = MagicMock()
    cancel_event = threading.Event()

    with patch("whisperstream.qwen_download.download") as unified_download:
        download_qwen_model(
            tmp_path,
            progress_callback=progress,
            cancel_event=cancel_event,
        )

    unified_download.assert_called_once_with(
        ModelId.QWEN3_ASR_06B_INT8,
        tmp_path,
        progress_callback=progress,
        cancel_event=cancel_event,
    )


def test_downloads_every_required_file_from_r2_with_nested_layout(
    tmp_path: Path,
) -> None:
    payloads = {path: f"payload:{path}".encode() for path in _FILES}
    client = _client_for(payloads)

    with patch("whisperstream.model_downloader.new_http_client", return_value=client):
        download_qwen_model(tmp_path)

    for path, payload in payloads.items():
        assert (tmp_path / path).read_bytes() == payload
    assert (tmp_path / "tokenizer").is_dir()
    assert client.stream.call_count == len(_FILES)


def test_present_nonempty_file_is_skipped_but_zero_byte_file_is_downloaded(
    tmp_path: Path,
) -> None:
    cached = _FILES[0]
    empty = _FILES[1]
    (tmp_path / cached).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / cached).write_bytes(b"cached")
    (tmp_path / empty).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / empty).write_bytes(b"")
    payloads = {path: f"payload:{path}".encode() for path in _FILES}
    client = _client_for(payloads)

    with patch("whisperstream.model_downloader.new_http_client", return_value=client):
        download_qwen_model(tmp_path)

    requested = {call.args[1].removeprefix(_R2_BASE) for call in client.stream.call_args_list}
    assert cached not in requested
    assert empty in requested
    assert (tmp_path / cached).read_bytes() == b"cached"
    assert (tmp_path / empty).read_bytes() == payloads[empty]


def test_all_files_present_skips_network(tmp_path: Path) -> None:
    for path in _FILES:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"cached")

    with patch("whisperstream.model_downloader.new_http_client") as client_factory:
        download_qwen_model(tmp_path)

    client_factory.assert_not_called()


def test_progress_is_cumulative_across_manifest_files(tmp_path: Path) -> None:
    payloads = {path: f"payload:{path}".encode() for path in _FILES}
    client = _client_for(payloads)
    reports: list[tuple[int, int | None]] = []

    with patch("whisperstream.model_downloader.new_http_client", return_value=client):
        download_qwen_model(tmp_path, progress_callback=lambda done, total: reports.append((done, total)))

    expected_total = sum(map(len, payloads.values()))
    assert reports
    assert reports[-1] == (expected_total, expected_total)
    assert [done for done, _total in reports] == sorted(done for done, _total in reports)


def test_cancel_is_terminal_and_cleans_atomic_scratch(tmp_path: Path) -> None:
    first = _FILES[0]
    payloads = {path: f"payload:{path}".encode() for path in _FILES}
    cancel_event = threading.Event()
    client = _client_for(payloads)

    def chunks():
        yield b"first chunk"
        cancel_event.set()
        yield b"second chunk"

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.iter_bytes = MagicMock(return_value=chunks())
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=response)
    context.__exit__ = MagicMock(return_value=False)
    client.stream = MagicMock(return_value=context)

    with patch("whisperstream.model_downloader.new_http_client", return_value=client):
        with pytest.raises(DownloadCancelled):
            download_qwen_model(tmp_path, cancel_event=cancel_event)

    target = tmp_path / first
    assert not target.exists()
    assert not target.with_suffix(target.suffix + ".tmp").exists()
    assert client.stream.call_count == 1


def test_r2_failure_names_manual_qwen_archive(tmp_path: Path) -> None:
    with patch(
        "whisperstream.model_downloader._download_from_hf",
        side_effect=RuntimeError("r2 unavailable"),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            download_qwen_model(tmp_path)

    message = str(exc_info.value)
    assert "Check your connection" in message
    assert "verified model files locally" in message


@pytest.fixture(autouse=True)
def pinned_test_source(monkeypatch):
    from whisperstream.model_downloader import MODEL_SOURCES
    payloads = {path: f"payload:{path}".encode() for path in _FILES}
    monkeypatch.setitem(MODEL_SOURCES, ModelId.QWEN3_ASR_06B_INT8.value, {
        "repo": "test/qwen", "revision": "a" * 40,
        "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name,data in payloads.items()}
    })
