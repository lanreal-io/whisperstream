"""A retired trial-policy key in an existing config loads and is dropped."""
from whisperstream.config_manager import ConfigManager, WhisperstreamConfig


def test_retired_trial_policy_key_is_ignored_and_not_rewritten(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("trial_policy_checks_enabled = false\n", encoding="utf-8")
    manager = ConfigManager(path, models_root=tmp_path / "models")
    config = manager.load()
    assert not hasattr(config, "trial_policy_checks_enabled")
    manager.save(config)
    assert "trial_policy_checks_enabled" not in path.read_text(encoding="utf-8")
    assert not hasattr(WhisperstreamConfig.from_dict({"trial_policy_checks_enabled": "x"}), "trial_policy_checks_enabled")
