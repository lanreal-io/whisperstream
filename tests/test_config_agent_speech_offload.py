"""Persistence and validation of the shared Agent Speak residency preference."""

import pytest

from whisperstream.config_manager import WhisperstreamConfig


@pytest.mark.parametrize("value", [0, 300, 600, 900, 1800, 3600])
def test_supported_timeout_round_trip(value):
    config = WhisperstreamConfig.from_dict({"agent_speech_idle_timeout_seconds": value})
    config.validate()
    assert config.agent_speech_idle_timeout_seconds == value
    assert WhisperstreamConfig.from_dict(config.to_dict()).agent_speech_idle_timeout_seconds == value


@pytest.mark.parametrize("value", [True, False, -1, 60, 300.0, "300", None])
def test_invalid_timeout_loads_default_but_direct_validation_rejects(value):
    assert WhisperstreamConfig.from_dict({"agent_speech_idle_timeout_seconds": value}).agent_speech_idle_timeout_seconds == 1800
    config = WhisperstreamConfig(agent_speech_idle_timeout_seconds=value)
    with pytest.raises(ValueError, match="agent_speech_idle_timeout_seconds"):
        config.validate()


def test_old_config_gets_thirty_minutes():
    assert WhisperstreamConfig.from_dict({}).agent_speech_idle_timeout_seconds == 1800


def test_timeout_persists_to_isolated_toml(tmp_path):
    from whisperstream.config_manager import ConfigManager

    manager = ConfigManager(config_path=tmp_path / "config.toml")
    config = WhisperstreamConfig(agent_speech_idle_timeout_seconds=900)
    manager.save(config)
    assert manager.load().agent_speech_idle_timeout_seconds == 900
