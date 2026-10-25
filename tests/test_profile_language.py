from unittest.mock import MagicMock

import pytest

from whisperstream.config_manager import ConfigManager, WhisperstreamConfig
from whisperstream.foreground_window import RoutingTarget
from whisperstream.main import WhisperstreamApp
from whisperstream.model_downloader import ModelId
from whisperstream.supervised_stt_manager import SupervisedSttManager


def config():
    return WhisperstreamConfig(
        language="en", stt_backend="cohere", stt_model_selection_mode="manual",
        enhancement_enabled=False, default_profile_id="default",
        profiles=[
            {"id": "default", "name": "Default", "prompt": "", "language": "de"},
            {"id": "app", "name": "App", "prompt": "", "language": "fr"},
            {"id": "site", "name": "Site", "prompt": "", "language": "ja"},
        ], power_mode_map={"browser.exe": "app"}, site_map={"example.com": "site"},
    )


def app_for(cfg, target=None):
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.config = cfg
    app.parakeet = MagicMock(spec=SupervisedSttManager)
    app._loaded_stt_artifact_id = ModelId.COHERE_TRANSCRIBE_INT8
    app._dictation_app = target
    app._manual_profile_id = None
    app._override_anchor = None
    return app


@pytest.mark.parametrize("target,expected", [(None, "de"), (RoutingTarget(process_name="browser.exe"), "fr"), (RoutingTarget(process_name="browser.exe", domain="example.com"), "ja")])
def test_profile_hint_routes_with_enhancement_off(target, expected):
    app = app_for(config(), target)
    app._bind_recording_language()
    app.parakeet.for_language.assert_called_once_with(expected)


def test_manual_profile_wins_and_unsupported_override_inherits_global():
    app = app_for(config(), RoutingTarget(process_name="browser.exe", domain="example.com"))
    app._manual_profile_id = "app"
    app._override_anchor = "browser.exe"
    app._bind_recording_language()
    app.parakeet.for_language.assert_called_with("fr")
    app.config.profiles[1]["language"] = "ru"
    app._bind_recording_language()
    app.parakeet.for_language.assert_called_with("en")


def test_no_profile_hint_always_binds_cohere_default():
    cfg = config()
    cfg.profiles[0].pop("language")
    app = app_for(cfg)
    app._bind_recording_language()
    app.parakeet.for_language.assert_called_once_with("en")


def test_profile_language_roundtrip_and_removal(tmp_path):
    manager = ConfigManager(config_path=tmp_path / "config.toml", models_root=tmp_path / "models")
    cfg = config()
    cfg.profiles[0]["language"] = " DE "
    manager.save(cfg)
    loaded = manager.load()
    assert loaded.profiles[0]["language"] == "de"
    loaded.profiles[0].pop("language")
    manager.save(loaded)
    assert "language" not in manager.load().profiles[0]


@pytest.mark.parametrize("language", ["", "xx", 7, [], False])
def test_invalid_profile_language_rejected(language):
    cfg = config()
    cfg.profiles[0]["language"] = language
    with pytest.raises(ValueError, match="dictation language"):
        cfg.validate()
