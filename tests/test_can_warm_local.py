"""Unit tests for WhisperstreamApp._can_warm_local().

Each test covers one False branch or the True (all-guards-pass) branch.
No hardware, no model loading: all heavy dependencies are mocked out.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from whisperstream.config_manager import WhisperstreamConfig

# ---------------------------------------------------------------------------
# Fixture: a minimal patched WhisperstreamApp
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    """Construct a WhisperstreamApp with all hardware/model touchpoints mocked."""
    with (
        patch("whisperstream.main.make_stt_manager"),
        patch("whisperstream.main.KeyboardShortcutManager"),
        patch("whisperstream.main.AudioCapture"),
        patch("whisperstream.main.AudioFeedback"),
        patch("whisperstream.main.LicenseManager"),
        patch("whisperstream.main.TextInjector"),
        patch("whisperstream.speech.controller.SpeechController"),
        patch("whisperstream.main.model_dir", return_value=tmp_path),
        patch(
            "whisperstream.main.ConfigManager.load",
            return_value=WhisperstreamConfig(),
        ),
    ):
        from whisperstream.main import WhisperstreamApp

        instance = WhisperstreamApp(headless=True)
        yield instance


@pytest.fixture
def local_cfg(tmp_path):
    """Return a WhisperstreamConfig pointing at local provider with a known model id."""
    return WhisperstreamConfig(
        enhancement_provider="local",
        enhancement_local_model="gemma4-e2b-instruct",  # must exist in LOCAL_BUNDLED_MODELS
        enhancement_local_endpoint="",                   # no override
    )


@pytest.fixture
def gguf_file(tmp_path, local_cfg):
    """Write a non-empty fake GGUF file in tmp_path and return its Path."""
    from whisperstream.enhancement_models import get_local_bundled_model
    entry = get_local_bundled_model(local_cfg.enhancement_local_model)
    assert entry is not None, "test model id not found in LOCAL_BUNDLED_MODELS"
    p = tmp_path / entry["gguf_filename"]
    p.write_bytes(b"\x00" * 16)  # non-empty
    return p


# ---------------------------------------------------------------------------
# Helper to patch get_local_llm_dir for a given tmp_path
# ---------------------------------------------------------------------------

def _patch_llm_dir(monkeypatch, tmp_path: Path):
    # The helper does an inline `from .config_manager import get_local_llm_dir`.
    # Patching at the source module ensures the inline import picks up our mock.
    monkeypatch.setattr(
        "whisperstream.config_manager.get_local_llm_dir",
        lambda: tmp_path,
    )


def _enable_candidate_overlay(tmp_path, monkeypatch):
    pytest.importorskip("whisperstream.dev_candidate_models")  # omitted from source releases
    registry = tmp_path / "candidate-models.json"
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()
    available_path = candidate_dir / "available.gguf"
    available_path.write_bytes(b"candidate")
    registry.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "candidates": [
                    {
                        "id": "candidate-available",
                        "label": "candidate-available",
                        "artifact_filename": "available.gguf",
                        "parameter_size": "350M",
                        "role": "Review candidate",
                        "status": "Evaluation candidate",
                        "serving_contract": "wrap-v2",
                        "trained_modes": ["chat", "code"],
                        "prompt_key": "stream",
                    },
                    {
                        "id": "candidate-missing",
                        "label": "candidate-missing",
                        "artifact_filename": "missing.gguf",
                        "parameter_size": "1.2B",
                        "role": "Review candidate",
                        "status": "Evaluation candidate",
                        "serving_contract": "wrap-v2",
                        "trained_modes": ["chat", "code"],
                        "prompt_key": "stream",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "whisperstream._build_flags.DEV_TOOLS_AVAILABLE", True
    )
    monkeypatch.setenv(
        "WHISPERSTREAM_DEV_CANDIDATE_REGISTRY", str(registry)
    )
    monkeypatch.setenv("WHISPERSTREAM_DEV_CANDIDATE_DIR", str(candidate_dir))
    return available_path


# ---------------------------------------------------------------------------
# Tests: False branches
# ---------------------------------------------------------------------------

def test_false_when_provider_is_not_local(app, tmp_path, monkeypatch, gguf_file, local_cfg):
    """Guard 1: provider != 'local' → False."""
    _patch_llm_dir(monkeypatch, tmp_path)
    app.llama_server = MagicMock()
    app._server_loop = object()  # sentinel

    cfg = replace(local_cfg, enhancement_provider="openai")
    result = app._can_warm_local(cfg)
    assert result is False


def test_false_when_endpoint_override_set(app, tmp_path, monkeypatch, gguf_file, local_cfg):
    """Guard 2: endpoint override set → False."""
    _patch_llm_dir(monkeypatch, tmp_path)
    app.llama_server = MagicMock()
    app._server_loop = object()

    cfg = replace(local_cfg, enhancement_local_endpoint="http://localhost:9999")
    result = app._can_warm_local(cfg)
    assert result is False


def test_false_when_model_id_unknown(app, tmp_path, monkeypatch, local_cfg):
    """Guard 3: model id not in catalog → False."""
    _patch_llm_dir(monkeypatch, tmp_path)
    app.llama_server = MagicMock()
    app._server_loop = object()

    cfg = replace(local_cfg, enhancement_local_model="not-a-real-model-id")
    result = app._can_warm_local(cfg)
    assert result is False


def test_false_when_gguf_missing(app, tmp_path, monkeypatch, local_cfg):
    """Guard 4a: GGUF file does not exist → False."""
    _patch_llm_dir(monkeypatch, tmp_path)
    app.llama_server = MagicMock()
    app._server_loop = object()

    # tmp_path exists but GGUF was never created
    result = app._can_warm_local(local_cfg)
    assert result is False


def test_false_when_gguf_is_zero_bytes(app, tmp_path, monkeypatch, local_cfg):
    """Guard 4b: GGUF file exists but is empty → False."""
    from whisperstream.enhancement_models import get_local_bundled_model
    entry = get_local_bundled_model(local_cfg.enhancement_local_model)
    assert entry is not None
    (tmp_path / entry["gguf_filename"]).write_bytes(b"")  # zero-byte file

    _patch_llm_dir(monkeypatch, tmp_path)
    app.llama_server = MagicMock()
    app._server_loop = object()

    result = app._can_warm_local(local_cfg)
    assert result is False


def test_false_when_llama_server_is_none(app, tmp_path, monkeypatch, gguf_file, local_cfg):
    """Guard 5: self.llama_server is None → False."""
    _patch_llm_dir(monkeypatch, tmp_path)
    app.llama_server = None
    app._server_loop = object()

    result = app._can_warm_local(local_cfg)
    assert result is False


def test_false_when_server_loop_is_none(app, tmp_path, monkeypatch, gguf_file, local_cfg):
    """Guard 6: self._server_loop is None → False (kick_warm needs the loop)."""
    _patch_llm_dir(monkeypatch, tmp_path)
    app.llama_server = MagicMock()
    app._server_loop = None

    result = app._can_warm_local(local_cfg)
    assert result is False


# ---------------------------------------------------------------------------
# Test: True branch — all guards satisfied
# ---------------------------------------------------------------------------

def test_true_when_all_guards_pass(app, tmp_path, monkeypatch, gguf_file, local_cfg):
    """All six guards satisfied → True."""
    _patch_llm_dir(monkeypatch, tmp_path)
    app.llama_server = MagicMock()
    app._server_loop = object()

    result = app._can_warm_local(local_cfg)
    assert result is True


# ---------------------------------------------------------------------------
# Test: helper is side-effect free (does not mutate app state)
# ---------------------------------------------------------------------------

def test_no_side_effects(app, tmp_path, monkeypatch, gguf_file, local_cfg):
    """_can_warm_local must not mutate any app attributes."""
    _patch_llm_dir(monkeypatch, tmp_path)
    mock_supervisor = MagicMock()
    app.llama_server = mock_supervisor
    app._server_loop = object()

    app._can_warm_local(local_cfg)

    # supervisor must not have been called
    mock_supervisor.kick_warm.assert_not_called()
    mock_supervisor.start.assert_not_called()


def test_true_for_available_candidate_at_external_artifact_path(
    app, tmp_path, monkeypatch, local_cfg
):
    _enable_candidate_overlay(tmp_path, monkeypatch)
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    _patch_llm_dir(monkeypatch, managed_dir)
    app.llama_server = MagicMock()
    app._server_loop = object()
    cfg = replace(
        local_cfg,
        enhancement_local_model="candidate-available",
    )

    assert app._can_warm_local(cfg) is True


def test_false_for_registered_candidate_with_missing_artifact(
    app, tmp_path, monkeypatch, local_cfg
):
    _enable_candidate_overlay(tmp_path, monkeypatch)
    _patch_llm_dir(monkeypatch, tmp_path / "managed")
    app.llama_server = MagicMock()
    app._server_loop = object()
    cfg = replace(
        local_cfg,
        enhancement_local_model="candidate-missing",
    )

    assert app._can_warm_local(cfg) is False
