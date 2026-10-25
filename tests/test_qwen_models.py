"""Unit tests for qwen_models: catalog entry + get_qwen_model() lookup helper."""
from __future__ import annotations

import pytest

from whisperstream import qwen_models


# ---------------------------------------------------------------------------
# Catalog completeness
# ---------------------------------------------------------------------------


def test_qwen_catalog_has_one_entry():
    assert len(qwen_models.QWEN_MODELS) == 1


def test_qwen_catalog_entry_has_required_fields():
    entry = qwen_models.QWEN_MODELS[0]
    assert "id" in entry
    assert "hf_repo" in entry
    assert "files" in entry


def test_qwen_catalog_entry_id():
    entry = qwen_models.QWEN_MODELS[0]
    assert entry["id"] == "qwen3-asr-0.6b-int8"


def test_qwen_catalog_hf_repo():
    entry = qwen_models.QWEN_MODELS[0]
    # Official maintainer export (Fangjun Kuang), not the third-party pantinor mirror.
    assert entry["hf_repo"] == "csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"


def test_qwen_catalog_lists_all_required_files():
    entry = qwen_models.QWEN_MODELS[0]
    files = entry["files"]
    required = {
        "conv_frontend.onnx",
        "encoder.int8.onnx",
        "decoder.int8.onnx",
        "tokenizer/vocab.json",
        "tokenizer/merges.txt",
        "tokenizer/tokenizer_config.json",
    }
    assert required.issubset(set(files)), (
        f"Missing required files: {required - set(files)}"
    )


def test_qwen_catalog_files_are_strings():
    entry = qwen_models.QWEN_MODELS[0]
    for f in entry["files"]:
        assert isinstance(f, str), f"Expected str, got {type(f)!r} for {f!r}"


# ---------------------------------------------------------------------------
# get_qwen_model() lookup helper
# ---------------------------------------------------------------------------


def test_get_qwen_model_returns_entry_by_id():
    entry = qwen_models.get_qwen_model("qwen3-asr-0.6b-int8")
    assert entry is not None
    assert entry["id"] == "qwen3-asr-0.6b-int8"


def test_get_qwen_model_returns_none_for_unknown_id():
    assert qwen_models.get_qwen_model("nonexistent-model") is None


def test_get_qwen_model_returns_none_for_empty_string():
    assert qwen_models.get_qwen_model("") is None


def test_get_qwen_model_returns_same_object_as_catalog():
    """Lookup returns the actual catalog entry dict, not a copy."""
    entry = qwen_models.get_qwen_model("qwen3-asr-0.6b-int8")
    assert entry is qwen_models.QWEN_MODELS[0]


# ---------------------------------------------------------------------------
# model_dir(ModelId.QWEN3_ASR_06B_INT8) — the unified destination resolver
# (Qwen now lives UNDER models/, renamed from the old top-level qwen3-asr/).
# ---------------------------------------------------------------------------


def test_qwen_model_dir_name(tmp_path, monkeypatch):
    """model_dir(QWEN) must return a path named 'qwen3-asr-0.6b-int8' under models/."""
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    from whisperstream.model_downloader import ModelId, model_dir  # noqa: PLC0415

    p = model_dir(ModelId.QWEN3_ASR_06B_INT8)
    assert p.name == "qwen3-asr-0.6b-int8"
    # Now a child of models/, not a top-level sibling.
    assert p.parent.name == "models"


def test_qwen_model_dir_honors_data_dir_override(tmp_path, monkeypatch):
    """model_dir(QWEN) must respect WHISPERSTREAM_DATA_DIR."""
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    from whisperstream.model_downloader import ModelId, model_dir  # noqa: PLC0415

    p = model_dir(ModelId.QWEN3_ASR_06B_INT8)
    assert str(p).startswith(str(tmp_path))
