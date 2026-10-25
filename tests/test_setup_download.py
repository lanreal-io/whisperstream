"""Tests for the unified registry-driven download() and the setup shims.

``setup.download_model`` is now a thin shim over
``model_downloader.download(model_id_for_parakeet(name), ...)``. The Parakeet
download flow (per-file pending, verified R2-only delivery, terminal cancel,
cumulative+milestone progress,
``bpe.vocab`` post-download)
lives once in ``model_downloader``. These tests assert that unified behavior
through the shim; patch points target ``model_downloader`` where the loop now
lives.

``setup.download_llm_model`` (GGUF) is now ALSO a thin shim over the same
``model_downloader.download()`` (mirrors ``qwen_download.download_qwen_model``'s
shape) -- it resolves the catalog id to a ``ModelId`` and delegates with
``dest_dir = gguf_path.parent``. The tests below exercise that delegation
directly and drive a couple of end-to-end flows through the real ``download()``
to prove the shim didn't regress idempotent, progress, or unknown-id behavior.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest



def test_download_shim_delegates_to_unified_download() -> None:
    """``setup.download_model`` resolves the ModelId and calls download()."""
    from whisperstream.model_downloader import ModelId
    from whisperstream.setup import download_model

    with patch("whisperstream.setup.download") as mock_download:
        download_model(
            "nemo-parakeet-tdt-0.6b-v3",
            Path("/fake/model/dir"),
            quantization="int8",
            progress_callback=lambda downloaded, total: None,
        )

    assert mock_download.call_count == 1
    args, kwargs = mock_download.call_args
    assert args[0] is ModelId.PARAKEET_V3_INT8
    assert args[1] == Path("/fake/model/dir")
    assert "progress_callback" in kwargs


def test_download_shim_resolves_v2_vs_v3() -> None:
    """The shim maps the v2 model name to PARAKEET_V2_INT8."""
    from whisperstream.model_downloader import ModelId
    from whisperstream.setup import download_model

    with patch("whisperstream.setup.download") as mock_download:
        download_model("nemo-parakeet-tdt-0.6b-v2", Path("/fake/v2"), quantization="int8")

    assert mock_download.call_args.args[0] is ModelId.PARAKEET_V2_INT8


def test_progress_callback_invoked_with_cumulative_shape() -> None:
    """The user callback is invoked with (downloaded, total) through download().

    download() wraps the user callback with a milestone-logging closure, then
    hands that wrapper to _download_from_hf. We capture the wrapper and drive
    it directly to confirm the user callback receives the expected pairs.
    """
    from whisperstream.setup import download_model

    calls: list[tuple[int, int | None]] = []

    def my_callback(downloaded: int, total: int | None) -> None:
        calls.append((downloaded, total))

    with (
        patch("whisperstream.model_downloader._download_from_hf") as mock_r2,
        patch(
            "whisperstream.model_downloader._compute_pending",
            return_value=["encoder.int8.onnx"],
        ),
        patch("whisperstream.model_downloader.generate_bpe_vocab"),
        patch.object(Path, "mkdir"),
        patch.object(Path, "rglob", return_value=[]),
    ):
        mock_r2.return_value = None
        download_model(
            "nemo-parakeet-tdt-0.6b-v3",
            Path("/fake/model/dir"),
            quantization="int8",
            progress_callback=my_callback,
        )

    assert mock_r2.called
    # _download_from_hf positional args:
    #   (spec, dest_dir, pending, progress_callback, cancel_event, logger)
    args = mock_r2.call_args.args
    wrapped_callback = args[3]
    assert callable(wrapped_callback)

    wrapped_callback(25, 100)
    wrapped_callback(100, 100)

    assert calls == [(25, 100), (100, 100)]


def test_no_progress_callback_still_wraps_milestones() -> None:
    """With no user callback, download() still hands the helper a callable."""
    from whisperstream.setup import download_model

    with (
        patch("whisperstream.model_downloader._download_from_hf") as mock_r2,
        patch(
            "whisperstream.model_downloader._compute_pending",
            return_value=["encoder.int8.onnx"],
        ),
        patch("whisperstream.model_downloader.generate_bpe_vocab"),
        patch.object(Path, "mkdir"),
        patch.object(Path, "rglob", return_value=[]),
    ):
        mock_r2.return_value = None
        download_model(
            "nemo-parakeet-tdt-0.6b-v3",
            Path("/fake/model/dir"),
        )

    assert mock_r2.called
    wrapped_callback = mock_r2.call_args.args[3]
    wrapped_callback(10, 100)  # no-op, no exception


def test_download_skips_when_no_pending_files() -> None:
    """When every file is already present, download() never touches the network."""
    from whisperstream.setup import download_model

    with (
        patch("whisperstream.model_downloader._download_from_hf") as mock_r2,
        patch("whisperstream.model_downloader._compute_pending", return_value=[]),
        patch("whisperstream.model_downloader.generate_bpe_vocab") as mock_bpe,
        patch.object(Path, "mkdir"),
    ):
        download_model("nemo-parakeet-tdt-0.6b-v3", Path("/fake/model/dir"))

    mock_r2.assert_not_called()
    mock_bpe.assert_not_called()


def test_parakeet_post_download_runs_bpe_vocab(tmp_path: Path) -> None:
    """A successful Parakeet download runs its post_download hook (bpe.vocab).

    The hook is bound on the frozen ModelSpec, so we drive the real
    generate_bpe_vocab end to end: the R2 helper is faked to drop a real
    tokens.txt, and we assert bpe.vocab is produced afterwards.
    """
    from whisperstream.setup import download_model

    def fake_r2(spec, dest_dir, pending, progress_callback, cancel_event, logger):
        # Simulate the download landing tokens.txt so the post-hook has input.
        (dest_dir / "tokens.txt").write_text("<blk> 0\nhello 1\n", encoding="utf-8")

    with (
        patch("whisperstream.model_downloader._download_from_hf", side_effect=fake_r2),
        patch("whisperstream.model_downloader._compute_pending", return_value=["tokens.txt"]),
    ):
        download_model("nemo-parakeet-tdt-0.6b-v3", tmp_path)

    assert (tmp_path / "bpe.vocab").exists(), "post_download hook did not generate bpe.vocab"


def test_qwen_post_download_is_none(tmp_path: Path) -> None:
    """The Qwen spec has no post_download hook (no bpe.vocab for Qwen)."""
    from whisperstream.qwen_download import download_qwen_model

    with (
        patch("whisperstream.model_downloader._download_from_hf"),
        patch("whisperstream.model_downloader._compute_pending", return_value=["conv_frontend.onnx"]),
    ):
        download_qwen_model(tmp_path)

    assert not (tmp_path / "bpe.vocab").exists(), "Qwen must not generate bpe.vocab"

# ---------------------------------------------------------------------------
# TestDownloadLLMModel: Task 1.4
# ---------------------------------------------------------------------------


class TestDownloadLLMModel:
    """Tests for the download_llm_model() thin shim in setup.py."""

    def test_shim_delegates_to_unified_download_with_dest_dir_and_forwards_callbacks(
        self, tmp_path: Path
    ) -> None:
        """download_llm_model resolves the catalog id to its ModelId and calls
        the unified download() with dest_dir = gguf_path.parent, forwarding
        progress_callback and cancel_event unchanged (Task 1.3 shim contract)."""
        import threading

        from whisperstream.model_downloader import ModelId
        from whisperstream.setup import download_llm_model

        some_dir = tmp_path / "local-llm"
        gguf_path = some_dir / "stream-1-Q4_K_M.gguf"
        progress_cb = lambda downloaded, total: None
        cancel_event = threading.Event()

        with patch("whisperstream.setup.download") as mock_download:
            download_llm_model(
                "gemma4-e2b-instruct",
                gguf_path,
                progress_callback=progress_cb,
                cancel_event=cancel_event,
            )

        mock_download.assert_called_once()
        args, kwargs = mock_download.call_args
        assert args[0] is ModelId.GEMMA_4_E2B_Q4
        assert args[1] == some_dir
        assert kwargs["progress_callback"] is progress_cb
        assert kwargs["cancel_event"] is cancel_event

    def test_successful_download_calls_progress_callback(self, tmp_path: Path) -> None:
        """download_llm_model preserves the unified downloader's progress path."""
        from whisperstream.setup import download_llm_model

        payload = b"GGUF" * 1024
        calls: list[tuple[int, int | None]] = []
        gguf_path = tmp_path / "local-llm" / "gemma-4-E2B-it-Q4_K_M.gguf"

        def fake_r2(spec, dest_dir, pending, progress_callback, cancel_event, logger):
            (dest_dir / pending[0]).write_bytes(payload)
            progress_callback(len(payload), len(payload))

        with patch(
            "whisperstream.model_downloader._download_from_hf",
            side_effect=fake_r2,
        ):
            download_llm_model(
                "gemma4-e2b-instruct",
                gguf_path,
                progress_callback=lambda d, t: calls.append((d, t)),
            )

        assert gguf_path.exists()
        assert gguf_path.read_bytes() == payload
        assert calls == [(len(payload), len(payload))]

    def test_idempotent_returns_immediately_if_file_exists(self, tmp_path: Path) -> None:
        """download_llm_model returns immediately when the GGUF is already present."""
        from unittest.mock import patch

        from whisperstream.setup import download_llm_model

        gguf_path = tmp_path / "local-llm" / "gemma-4-E2B-it-Q4_K_M.gguf"
        gguf_path.parent.mkdir(parents=True, exist_ok=True)
        gguf_path.write_bytes(b"existing content")

        with patch("whisperstream.model_downloader.httpx.Client") as mock_client_cls:
            download_llm_model("gemma4-e2b-instruct", gguf_path)

        # httpx.Client should NOT have been constructed at all.
        mock_client_cls.assert_not_called()
        assert gguf_path.read_bytes() == b"existing content"

    def test_raises_value_error_on_unknown_model_id(self, tmp_path: Path) -> None:
        """download_llm_model raises ValueError for an unrecognized model id."""
        import pytest

        from whisperstream.setup import download_llm_model

        gguf_path = tmp_path / "no-such-model.gguf"
        with pytest.raises(ValueError, match="unknown"):
            download_llm_model("nonexistent-model-xyz", gguf_path)
