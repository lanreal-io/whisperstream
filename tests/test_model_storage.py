"""Tests for the model_storage_dir config field (Tasks 1.1, 1.2, and 1.3).

Task 1.1 — verifies that model_storage_dir:
- defaults to "" on a fresh WhisperstreamConfig
- round-trips through ConfigManager.save() / ConfigManager.load()
- loads as "" when the key is absent from config.toml
- is preserved by from_dict() / to_dict() without any special handling

Task 1.2 — verifies that _model_storage_base() / get_models_root() /
get_local_llm_dir() re-base onto model_storage_dir when set, and fall back
to get_app_dir() when the value is empty, the key is absent, or the TOML is
malformed.

Task 1.3 — verifies that ConfigManager.refresh_models_root():
- re-reads get_models_root() and assigns the result to self._models_root
- creates the new directory (mkdir parents=True, exist_ok=True)
- returns the new path
- causes subsequent model_dir() calls to resolve under the new root
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from whisperstream.config_manager import (
    ConfigManager,
    WhisperstreamConfig,
    get_local_llm_dir,
    get_models_root,
)


def _make_manager(tmp_path: Path) -> ConfigManager:
    config_path = tmp_path / "config.toml"
    models_path = tmp_path / "models"
    return ConfigManager(config_path=config_path, models_root=models_path)


class TestModelStorageDirField:
    def test_default_is_empty_string(self) -> None:
        """model_storage_dir defaults to empty string."""
        cfg = WhisperstreamConfig()
        assert cfg.model_storage_dir == ""

    def test_roundtrip_via_save_and_load(self) -> None:
        """model_storage_dir survives a ConfigManager save + load cycle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(Path(tmpdir))
            cfg = WhisperstreamConfig(model_storage_dir="C:/somewhere")
            mgr.save(cfg)
            loaded = mgr.load()
        assert loaded.model_storage_dir == "C:/somewhere"

    def test_missing_key_in_toml_loads_as_empty_string(self) -> None:
        """A config.toml that lacks model_storage_dir loads with ""."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            models_path = Path(tmpdir) / "models"
            # Write a minimal config.toml without model_storage_dir
            config_path.write_text('audio_feedback = true\n', encoding="utf-8")
            mgr = ConfigManager(config_path=config_path, models_root=models_path)
            loaded = mgr.load()
        assert loaded.model_storage_dir == ""

    def test_from_dict_round_trip(self) -> None:
        """model_storage_dir passes through from_dict / to_dict unchanged."""
        cfg = WhisperstreamConfig(model_storage_dir="D:/models-dir")
        payload = cfg.to_dict()
        assert payload["model_storage_dir"] == "D:/models-dir"
        reloaded = WhisperstreamConfig.from_dict(payload)
        assert reloaded.model_storage_dir == "D:/models-dir"

    def test_empty_string_roundtrip(self) -> None:
        """Empty string (default state) also round-trips correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(Path(tmpdir))
            cfg = WhisperstreamConfig(model_storage_dir="")
            mgr.save(cfg)
            loaded = mgr.load()
        assert loaded.model_storage_dir == ""


class TestModelStorageBase:
    """Task 1.2: _model_storage_base() rebasing and soft-fail behaviour."""

    def test_models_root_rebased_when_dir_set(self) -> None:
        """get_models_root() returns <model_storage_dir>/models when the field is set."""
        with (
            tempfile.TemporaryDirectory() as data_dir,
            tempfile.TemporaryDirectory() as storage_dir,
        ):
            # Write a config.toml pointing model_storage_dir at storage_dir.
            config_path = Path(data_dir) / "config.toml"
            # Use forward slashes so TOML doesn't need escaping.
            storage_posix = Path(storage_dir).as_posix()
            config_path.write_text(
                f'model_storage_dir = "{storage_posix}"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"WHISPERSTREAM_DATA_DIR": data_dir}):
                result = get_models_root()
            assert result == Path(storage_dir) / "models"

    def test_local_llm_dir_rebased_when_dir_set(self) -> None:
        """get_local_llm_dir() returns <model_storage_dir>/local-llm when the field is set."""
        with (
            tempfile.TemporaryDirectory() as data_dir,
            tempfile.TemporaryDirectory() as storage_dir,
        ):
            config_path = Path(data_dir) / "config.toml"
            storage_posix = Path(storage_dir).as_posix()
            config_path.write_text(
                f'model_storage_dir = "{storage_posix}"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"WHISPERSTREAM_DATA_DIR": data_dir}):
                result = get_local_llm_dir()
            assert result == Path(storage_dir) / "local-llm"

    def test_fallback_when_model_storage_dir_empty(self) -> None:
        """Empty model_storage_dir in config.toml falls back to get_app_dir()."""
        with tempfile.TemporaryDirectory() as data_dir:
            config_path = Path(data_dir) / "config.toml"
            config_path.write_text('model_storage_dir = ""\n', encoding="utf-8")
            with patch.dict(os.environ, {"WHISPERSTREAM_DATA_DIR": data_dir}):
                models_root = get_models_root()
                llm_dir = get_local_llm_dir()
            assert models_root == Path(data_dir) / "models"
            assert llm_dir == Path(data_dir) / "local-llm"

    def test_fallback_when_key_absent(self) -> None:
        """Missing model_storage_dir key in config.toml falls back to get_app_dir()."""
        with tempfile.TemporaryDirectory() as data_dir:
            config_path = Path(data_dir) / "config.toml"
            config_path.write_text("audio_feedback = true\n", encoding="utf-8")
            with patch.dict(os.environ, {"WHISPERSTREAM_DATA_DIR": data_dir}):
                models_root = get_models_root()
                llm_dir = get_local_llm_dir()
            assert models_root == Path(data_dir) / "models"
            assert llm_dir == Path(data_dir) / "local-llm"

    def test_fallback_when_config_file_missing(self) -> None:
        """Missing config.toml file falls back to get_app_dir() (no error raised)."""
        with tempfile.TemporaryDirectory() as data_dir:
            # Do NOT write a config.toml — the file is absent.
            with patch.dict(os.environ, {"WHISPERSTREAM_DATA_DIR": data_dir}):
                models_root = get_models_root()
                llm_dir = get_local_llm_dir()
            assert models_root == Path(data_dir) / "models"
            assert llm_dir == Path(data_dir) / "local-llm"

    def test_fallback_when_toml_malformed(self) -> None:
        """Malformed config.toml fails soft and falls back to get_app_dir()."""
        with tempfile.TemporaryDirectory() as data_dir:
            config_path = Path(data_dir) / "config.toml"
            config_path.write_text("this is [[[not valid toml\n", encoding="utf-8")
            with patch.dict(os.environ, {"WHISPERSTREAM_DATA_DIR": data_dir}):
                models_root = get_models_root()
                llm_dir = get_local_llm_dir()
            assert models_root == Path(data_dir) / "models"
            assert llm_dir == Path(data_dir) / "local-llm"


class TestRefreshModelsRoot:
    """Task 1.3: ConfigManager.refresh_models_root() cache-refresh primitive."""

    def test_refresh_models_root_updates_cache_and_creates_dir(self, tmp_path: Path) -> None:
        """refresh_models_root() re-reads get_models_root(), caches the new path,
        creates the directory, and returns it."""
        config_path = tmp_path / "config.toml"
        old_models = tmp_path / "old_models"
        cm = ConfigManager(config_path=config_path, models_root=old_models)

        new_models = tmp_path / "new_models"
        assert not new_models.exists()

        with patch(
            "whisperstream.config_manager.get_models_root",
            return_value=new_models,
        ):
            result = cm.refresh_models_root()

        assert result == new_models
        assert cm.models_root == new_models
        assert new_models.exists()

    def test_refresh_models_root_model_dir_resolves_under_new_root(self, tmp_path: Path) -> None:
        """After refresh_models_root(), model_dir() returns a path under the new root."""
        config_path = tmp_path / "config.toml"
        old_models = tmp_path / "old_models"
        cm = ConfigManager(config_path=config_path, models_root=old_models)

        new_models = tmp_path / "new_storage" / "models"
        with patch(
            "whisperstream.config_manager.get_models_root",
            return_value=new_models,
        ):
            cm.refresh_models_root()

        model_path = cm.model_dir("nemo-parakeet-tdt-0.6b-v3", "int8")
        assert model_path.is_relative_to(new_models)

    def test_refresh_models_root_constructor_caching_unchanged(self, tmp_path: Path) -> None:
        """Constructor still caches _models_root at construction time (no change)."""
        config_path = tmp_path / "config.toml"
        initial_models = tmp_path / "initial_models"
        cm = ConfigManager(config_path=config_path, models_root=initial_models)

        # Without calling refresh, models_root is still the constructor value
        assert cm.models_root == initial_models
