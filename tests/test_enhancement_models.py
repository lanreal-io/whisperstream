"""Unit tests for the local enhancement-model catalog.

Cloud catalog tests live in test_cloud_model_manifest.py. This module must not
grow provider model-list clients again.
"""
from __future__ import annotations

from unittest.mock import patch

from whisperstream import enhancement_models


def test_module_contains_no_cloud_discovery_surface():
    for name in (
        "BUNDLED_MODELS",
        "MODEL_CATALOG_DATE",
        "fetch_provider_models",
        "filter_to_recent",
        "_fetch_gemini",
        "_fetch_openai",
        "_fetch_anthropic",
    ):
        assert not hasattr(enhancement_models, name)


def test_local_bundled_catalog_has_one_default_and_stable_ids():
    models = enhancement_models.LOCAL_BUNDLED_MODELS
    assert [row["id"] for row in models] == [
        "stream-1-mini",
        "gemma4-e2b-instruct",
    ]
    assert [row["id"] for row in models if row.get("recommended")] == [
        "stream-1-mini"
    ]


def test_local_catalog_returns_fresh_production_rows(tmp_path):
    with (
        patch("whisperstream.enhancement_models._load_dev_candidates", return_value=[]),
        patch("whisperstream.config_manager.get_local_llm_dir", return_value=tmp_path),
    ):
        first = enhancement_models.get_local_model_catalog(active_id="gemma4-e2b-instruct")
        second = enhancement_models.get_local_model_catalog(active_id="gemma4-e2b-instruct")

    assert first == second
    assert first is not second
    assert all(row["source"] == "production" for row in first)
    assert next(row for row in first if row["id"] == "gemma4-e2b-instruct")["active"] is True
    assert enhancement_models.LOCAL_BUNDLED_MODELS[0].get("source") is None


def test_get_local_bundled_model_handles_known_and_unknown_ids():
    assert enhancement_models.get_local_bundled_model("stream-1-mini")["id"] == "stream-1-mini"
    assert enhancement_models.get_local_bundled_model("missing") is None


def test_get_local_model_is_case_insensitive(tmp_path):
    with (
        patch("whisperstream.enhancement_models._load_dev_candidates", return_value=[]),
        patch("whisperstream.config_manager.get_local_llm_dir", return_value=tmp_path),
    ):
        row = enhancement_models.get_local_model("GEMMA4-E2B-INSTRUCT")
    assert row is not None
    assert row["id"] == "gemma4-e2b-instruct"


def test_resolve_local_model_path_uses_managed_directory(tmp_path):
    with patch("whisperstream.config_manager.get_local_llm_dir", return_value=tmp_path):
        path = enhancement_models.resolve_local_model_path("gemma4-e2b-instruct")
    assert path == tmp_path / "gemma-4-E2B-it-Q4_K_M.gguf"


def test_candidate_row_uses_registry_artifact_path(tmp_path):
    artifact = tmp_path / "candidate.gguf"
    row = {"source": "candidate", "_artifact_path": artifact}
    assert enhancement_models.resolve_local_model_path(row) == artifact


def test_nonempty_file_requires_content(tmp_path):
    path = tmp_path / "model.gguf"
    assert enhancement_models._nonempty_file(path) is False
    path.write_bytes(b"x")
    assert enhancement_models._nonempty_file(path) is True
