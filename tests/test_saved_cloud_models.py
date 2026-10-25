from whisperstream.config_manager import ConfigManager, WhisperstreamConfig


def test_migrates_each_existing_provider_without_changing_selection():
    data = {
        "enhancement_provider": "custom",
        "enhancement_gemini_model": "future-gemini",
        "enhancement_openai_model": "future-openai",
        "enhancement_anthropic_model": "future-claude",
        "enhancement_custom_model": "vendor/new-model",
    }
    config = WhisperstreamConfig.from_dict(data)
    assert config.enhancement_provider == "custom"
    for provider in ("gemini", "openai", "anthropic", "custom"):
        selected = data[f"enhancement_{provider}_model"]
        assert getattr(config, f"enhancement_{provider}_model") == selected
        assert config.enhancement_saved_models[provider] == [selected]


def test_history_round_trip_and_explicit_deletion(tmp_path):
    config = WhisperstreamConfig.from_dict({
        "enhancement_openai_model": "second",
        "enhancement_saved_models": {"openai": ["first", "second"]},
    })
    manager = ConfigManager(config_path=tmp_path / "config.toml", models_root=tmp_path / "models")
    manager.save(config)
    restored = manager.load()
    assert restored.enhancement_saved_models["openai"] == ["first", "second"]
    data = restored.to_dict()
    data["enhancement_openai_model"] = ""
    data["enhancement_saved_models"]["openai"] = []
    deleted = WhisperstreamConfig.from_dict(data)
    assert deleted.enhancement_saved_models["openai"] == []
    assert deleted.enhancement_openai_model == ""
    manager.save(deleted)
    reloaded = manager.load()
    assert reloaded.enhancement_saved_models["openai"] == []
    assert reloaded.enhancement_openai_model == ""


def test_invalid_history_is_normalized_without_losing_valid_ids():
    config = WhisperstreamConfig.from_dict({
        "enhancement_saved_models": {
            "openai": [" first ", "first", "", None, "a" * 129, "bad\nname", "Second"],
            "gemini": "not-a-list",
            "unknown": ["ignored"],
        },
    })
    assert config.enhancement_saved_models["openai"] == ["first", "Second"]
    assert config.enhancement_saved_models["gemini"] == []
    assert "unknown" not in config.enhancement_saved_models
