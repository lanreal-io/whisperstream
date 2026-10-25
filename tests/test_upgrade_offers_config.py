"""Config round-trip + normalization for the upgrade-offer dismissal state.

Covers the two list fields added for the feature-spotlight / upgrade-offer
overlay: `dismissed_offer_ids` and `shown_offer_ids`. Both persist which offers
the user has already seen / dismissed so the launch overlay never re-nags.
"""

from whisperstream.config_manager import WhisperstreamConfig


def test_offer_id_lists_default_empty():
    cfg = WhisperstreamConfig()
    assert cfg.dismissed_offer_ids == []
    assert cfg.shown_offer_ids == []


def test_offer_ids_normalize_garbage():
    # Non-strings dropped, blanks dropped, whitespace trimmed, order-preserving dedup.
    cfg = WhisperstreamConfig.from_dict(
        {
            "dismissed_offer_ids": [1, "", "  ", "gemini-3-5-flash", "gemini-3-5-flash", " cohere-transcribe "],
            "shown_offer_ids": ["a", "a", None, "b"],
        }
    )
    assert cfg.dismissed_offer_ids == ["gemini-3-5-flash", "cohere-transcribe"]
    assert cfg.shown_offer_ids == ["a", "b"]


def test_offer_ids_missing_keys_tolerated():
    cfg = WhisperstreamConfig.from_dict({})
    assert cfg.dismissed_offer_ids == []
    assert cfg.shown_offer_ids == []


def test_offer_ids_round_trip_through_dict():
    cfg = WhisperstreamConfig.from_dict(
        {"dismissed_offer_ids": ["gemini-3-5-flash"], "shown_offer_ids": ["gemini-3-5-flash", "cohere-transcribe"]}
    )
    round_tripped = WhisperstreamConfig.from_dict(cfg.to_dict())
    assert round_tripped.dismissed_offer_ids == ["gemini-3-5-flash"]
    assert round_tripped.shown_offer_ids == ["gemini-3-5-flash", "cohere-transcribe"]


def test_offer_ids_survive_toml_save_load(tmp_path):
    from whisperstream.config_manager import ConfigManager

    mgr = ConfigManager(config_path=tmp_path / "config.toml", models_root=tmp_path / "models")
    cfg = WhisperstreamConfig.from_dict(
        {"dismissed_offer_ids": ["gemini-3-5-flash"], "shown_offer_ids": ["cohere-transcribe"]}
    )
    mgr.save(cfg)
    reloaded = mgr.load()
    assert reloaded.dismissed_offer_ids == ["gemini-3-5-flash"]
    assert reloaded.shown_offer_ids == ["cohere-transcribe"]
