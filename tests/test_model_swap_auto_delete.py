"""Switching recognizers retains every downloaded STT artifact on disk."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from whisperstream.model_downloader import ModelId


# ---------------------------------------------------------------------------
# Shared helpers (mirror test_server_endpoints.py patterns)
# ---------------------------------------------------------------------------


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.parakeet_quantization = "int8"
    cfg.enhancement_provider = "none"
    cfg.word_boosts = []
    cfg.threads = None
    cfg.model_timeout = 0.0
    cfg.hotword_score = 1.5
    cfg.beam_width = 4
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _make_server(*, config=None):
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.config = config or _make_mock_config()
    app.stt_engine = MagicMock()
    app.config_manager = MagicMock()
    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    from whisperstream.windows_model_lifecycle import WindowsModelLifecycleAdapter
    server._model_lifecycle = WindowsModelLifecycleAdapter()
    return server


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestModelSwapRetention:
    """A successful switch changes runtime ownership without deleting files."""

    def test_parakeet_v3_swap_does_not_run_automatic_cleanup(self):
        server = _make_server()
        previous = server._app.stt_engine
        replacement = MagicMock()
        with (
            patch("whisperstream.stt_factory._resolve_backend", return_value=("parakeet", "config")),
            patch("whisperstream.setup.download_model"),
            patch("whisperstream.stt_factory.make_stt_manager", return_value=MagicMock()),
            patch("whisperstream.server.app.delete_model") as mock_cleanup,
        ):
            server._run_model_download()
        mock_cleanup.assert_not_called()

    @pytest.mark.parametrize("fail", [False, True])
    def test_worker_replacement_is_serial_and_restores_on_failure(self, fail):
        import logging
        import threading
        from whisperstream.main import WhisperstreamApp

        events = []
        class Previous:
            def unload(self): events.append("unload")
            def load(self): events.append("restore")
            def shutdown(self): events.append("shutdown")

        app = WhisperstreamApp.__new__(WhisperstreamApp)
        app._lock = threading.Lock()
        app.logger = logging.getLogger(__name__)
        app.stt_activation_busy = lambda: False
        previous = Previous()
        app.stt_engine = previous
        replacement = object()

        def replace():
            with app.stt_replacement():
                assert app.stt_engine is None
                assert app._stt_swap_in_progress
                assert events == ["unload"]
                events.append("construct")
                if fail:
                    raise RuntimeError("construction failed")
                app.stt_engine = replacement

        if fail:
            with pytest.raises(RuntimeError, match="construction failed"):
                replace()
            assert app.stt_engine is previous
            assert events == ["unload", "construct", "restore"]
        else:
            replace()
            assert app.stt_engine is replacement
            assert events == ["unload", "construct", "shutdown"]
        assert not app._stt_swap_in_progress

    def test_parakeet_v2_swap_does_not_run_automatic_cleanup(self):
        server = _make_server(config=_make_mock_config(parakeet_model="nemo-parakeet-tdt-0.6b-v2"))
        with (
            patch("whisperstream.stt_factory._resolve_backend", return_value=("parakeet", "config")),
            patch("whisperstream.setup.download_model"),
            patch("whisperstream.stt_factory.make_stt_manager", return_value=MagicMock()),
            patch("whisperstream.server.app.delete_model") as mock_cleanup,
        ):
            server._run_model_download()
        mock_cleanup.assert_not_called()

    def test_qwen_swap_does_not_run_automatic_cleanup(self, tmp_path: Path):
        server = _make_server()
        with (
            patch("whisperstream.stt_factory._resolve_backend", return_value=("qwen", "config")),
            patch("whisperstream.qwen_download.download_qwen_model"),
            patch("whisperstream.server.app.model_dir", return_value=tmp_path / "qwen3-asr-0.6b-int8"),
            patch("whisperstream.stt_factory.make_stt_manager", return_value=MagicMock()),
            patch("whisperstream.server.app.delete_model") as mock_cleanup,
        ):
            server._run_model_download()
        mock_cleanup.assert_not_called()

    def test_cohere_swap_does_not_run_automatic_cleanup(self, tmp_path: Path):
        server = _make_server()
        with (
            patch("whisperstream.stt_factory._resolve_backend", return_value=("cohere", "config")),
            patch("whisperstream.cohere_download.download_cohere_model"),
            patch("whisperstream.server.app.model_dir", return_value=tmp_path / "cohere"),
            patch("whisperstream.stt_factory.make_stt_manager", return_value=MagicMock()),
            patch("whisperstream.server.app.delete_model") as mock_cleanup,
        ):
            server._run_model_download()
        mock_cleanup.assert_not_called()

    def test_explicit_unused_cleanup_preserves_gguf_entries(self, tmp_path: Path) -> None:
        """Regression: explicit STT cleanup must not delete downloaded GGUFs.

        Before the ``kind`` discriminator, the registry sweep
        iterated the ENTIRE ``DOWNLOAD_REGISTRY`` and rmtree'd every
        non-active entry -- which would wipe the local-LLM picker's
        downloaded GGUFs (a separate keep-all-downloaded family) on every
        STT model switch. This drives the REAL (unmocked) function to prove
        a GGUF file survives.
        """
        from whisperstream.model_downloader import delete_unused_stt_models, model_dir

        with patch("whisperstream.config_manager.get_models_root", return_value=tmp_path):
            gguf_dir = model_dir(ModelId.GEMMA_4_E2B_Q4)  # "local-llm", shared by all GGUF entries
            gguf_dir.mkdir(parents=True, exist_ok=True)
            gguf_file = gguf_dir / "stream-1-mini-vocabulary-q4_k_m.gguf"
            gguf_file.write_bytes(b"gguf-stub-bytes")

            removed = delete_unused_stt_models({ModelId.PARAKEET_V3_INT8})

            assert gguf_file.exists(), "GGUF file must survive an STT model switch"
            assert ModelId.STREAM_1_MINI_Q4 not in removed
            assert ModelId.GEMMA_4_E2B_Q4 not in removed

    def test_switch_never_calls_cleanup_even_if_it_would_fail(self):
        """Retention means a cleanup failure cannot affect activation."""
        server = _make_server()
        server.broadcast = MagicMock()
        with (
            patch("whisperstream.stt_factory._resolve_backend", return_value=("parakeet", "config")),
            patch("whisperstream.setup.download_model"),
            patch("whisperstream.stt_factory.make_stt_manager", return_value=MagicMock()),
            patch(
                "whisperstream.server.app.delete_model",
                side_effect=OSError("file locked"),
            ) as mock_cleanup,
        ):
            server._run_model_download()
        types = [c.args[0]["type"] for c in server.broadcast.call_args_list if c.args]
        mock_cleanup.assert_not_called()
        assert "download.complete" in types
        assert "download.error" not in types
