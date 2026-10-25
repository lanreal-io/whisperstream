import json
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from whisperstream.config_manager import ConfigManager, WhisperstreamConfig


class TestConfigValidation(unittest.TestCase):
    def test_validate_threads_negative(self):
        """Negative threads should fail validation."""
        conf = WhisperstreamConfig(threads=-1)
        with self.assertRaisesRegex(ValueError, "threads must be non-negative"):
            conf.validate()

    def test_validate_clipboard_delay_negative(self):
        """Non-positive clipboard_clear_delay should fail validation."""
        conf = WhisperstreamConfig(clipboard_clear_delay=-1.0)
        with self.assertRaisesRegex(ValueError, "clipboard_clear_delay must be positive"):
            conf.validate()

    def test_validate_clipboard_delay_zero(self):
        """Zero clipboard_clear_delay should fail validation."""
        conf = WhisperstreamConfig(clipboard_clear_delay=0)
        with self.assertRaisesRegex(ValueError, "clipboard_clear_delay must be positive"):
            conf.validate()

    def test_validate_language_invalid(self):
        """Unsupported language code should fail validation."""
        conf = WhisperstreamConfig(language="xx")
        with self.assertRaisesRegex(ValueError, "language must be one of"):
            conf.validate()

    def test_validate_language_none_passes(self):
        """language=None (auto-detect) should pass validation."""
        conf = WhisperstreamConfig(language=None)
        conf.validate()  # Should not raise

    def test_validate_language_supported_passes(self):
        """Supported language codes should pass validation."""
        for code in ("en", "fr", "mt", "uk"):
            conf = WhisperstreamConfig(language=code)
            conf.validate()  # Should not raise

    def test_validate_all_39_union_languages_pass(self):
        """Every code in the 39-language union (Parakeet + Qwen) validates."""
        from whisperstream.config_manager import SUPPORTED_LANGUAGES

        self.assertEqual(len(SUPPORTED_LANGUAGES), 39)
        for code in SUPPORTED_LANGUAGES:
            conf = WhisperstreamConfig(language=code)
            conf.validate()  # Should not raise

    def test_validate_qwen_only_languages_pass(self):
        """Qwen-only additions (zh, ja, hi, etc.) now validate as supported."""
        for code in ("zh", "yue", "ja", "ko", "hi", "th", "vi", "id", "ms",
                     "fil", "ar", "fa", "tr", "mk"):
            conf = WhisperstreamConfig(language=code)
            conf.validate()  # Should not raise

    def test_from_dict_drops_unsupported_language(self):
        """from_dict gracefully falls back to None for unsupported language codes."""
        conf = WhisperstreamConfig.from_dict({"language": "xx"})
        self.assertIsNone(conf.language)
        conf.validate()  # Should not raise

    def test_from_dict_keeps_qwen_only_language(self):
        """from_dict lowercases and keeps a Qwen-only code (e.g. 'JA' -> 'ja')."""
        conf = WhisperstreamConfig.from_dict({"language": "JA"})
        self.assertEqual(conf.language, "ja")
        conf.validate()  # Should not raise

    def test_from_dict_keeps_supported_language(self):
        """from_dict lowercases and keeps supported language codes (e.g. 'EN' -> 'en')."""
        conf = WhisperstreamConfig.from_dict({"language": "EN"})
        self.assertEqual(conf.language, "en")

    def test_validate_model_timeout_negative(self):
        """Negative model_timeout should fail validation."""
        conf = WhisperstreamConfig(model_timeout=-1.0)
        with self.assertRaisesRegex(ValueError, "model_timeout must be non-negative"):
            conf.validate()

    def test_validate_max_recording_duration_negative(self):
        """Negative max_recording_duration should fail validation."""
        conf = WhisperstreamConfig(max_recording_duration=-1.0)
        with self.assertRaisesRegex(ValueError, "max_recording_duration must be non-negative"):
            conf.validate()

    def test_validate_max_recording_duration_excessive(self):
        """Excessive max_recording_duration should fail validation (DoS prevention)."""
        conf = WhisperstreamConfig(max_recording_duration=7201.0)
        with self.assertRaisesRegex(ValueError, "max_recording_duration must be <="):
            conf.validate()

    def test_validate_sound_path_missing(self):
        """Non-existent sound paths should fail validation."""
        conf = WhisperstreamConfig(start_sound_path="/this/path/absolutely/should/not/exist.wav")
        with self.assertRaisesRegex(ValueError, "start_sound_path does not exist"):
            conf.validate()

        conf = WhisperstreamConfig(stop_sound_path="/this/path/absolutely/should/not/exist.wav")
        with self.assertRaisesRegex(ValueError, "stop_sound_path does not exist"):
            conf.validate()

    def test_validate_audio_feedback_volume_below_zero(self):
        """Negative audio_feedback_volume should fail validation."""
        conf = WhisperstreamConfig(audio_feedback_volume=-0.1)
        with self.assertRaisesRegex(ValueError, "audio_feedback_volume must be between 0.0 and 1.0"):
            conf.validate()

    def test_validate_audio_feedback_volume_above_one(self):
        """audio_feedback_volume above 1.0 should fail validation."""
        conf = WhisperstreamConfig(audio_feedback_volume=1.5)
        with self.assertRaisesRegex(ValueError, "audio_feedback_volume must be between 0.0 and 1.0"):
            conf.validate()

    def test_validate_audio_feedback_volume_valid_range(self):
        """audio_feedback_volume within 0.0-1.0 should pass validation."""
        for vol in [0.0, 0.5, 1.0]:
            conf = WhisperstreamConfig(audio_feedback_volume=vol)
            conf.validate()  # Should not raise

    def test_valid_default_config(self):
        """Default config should pass validation."""
        conf = WhisperstreamConfig()
        conf.validate()  # Should not raise

    def test_new_install_openai_default_is_luna(self):
        self.assertEqual(WhisperstreamConfig().enhancement_openai_model, "gpt-5.6-luna")

    def test_existing_unlisted_cloud_model_survives_round_trip(self):
        conf = WhisperstreamConfig.from_dict({"enhancement_openai_model": "gpt-4o-mini"})
        self.assertEqual(conf.enhancement_openai_model, "gpt-4o-mini")
        self.assertEqual(conf.to_dict()["enhancement_openai_model"], "gpt-4o-mini")

    def test_valid_config_with_zero_timeouts(self):
        """Zero timeouts (disable) should pass validation."""
        conf = WhisperstreamConfig(model_timeout=0, max_recording_duration=0)
        conf.validate()  # Should not raise

    def test_show_live_preview_defaults_false_and_round_trips(self):
        """Live Preview is opt-in and survives config serialization."""
        default = WhisperstreamConfig()
        self.assertIs(default.show_live_preview, False)
        self.assertIs(default.to_dict()["show_live_preview"], False)

        enabled = WhisperstreamConfig.from_dict({"show_live_preview": True})
        self.assertIs(enabled.show_live_preview, True)
        self.assertIs(enabled.to_dict()["show_live_preview"], True)

        disabled = WhisperstreamConfig.from_dict({"show_live_preview": False})
        self.assertIs(disabled.show_live_preview, False)

    def test_show_recording_control_defaults_false_for_existing_configs(self):
        """Persistent recording control stays off when an older config lacks the key."""
        default = WhisperstreamConfig()
        self.assertIs(default.show_recording_control, False)

        existing = WhisperstreamConfig.from_dict(
            {
                "show_recording_indicator": True,
                "show_live_preview": True,
            }
        )
        self.assertIs(existing.show_recording_control, False)
        existing.validate()  # Should not raise

    def test_show_recording_control_explicit_values_round_trip(self):
        """Both recording-control preference values survive dict serialization."""
        for value in (True, False):
            config = WhisperstreamConfig.from_dict(
                {"show_recording_control": value}
            )
            self.assertIs(config.show_recording_control, value)
            self.assertIs(config.to_dict()["show_recording_control"], value)
            config.validate()  # Should not raise

    def test_show_recording_control_round_trips_through_config_manager(self):
        """The persisted preference survives the ordinary TOML save/load path."""
        import tempfile
        from pathlib import Path

        from whisperstream.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manager = ConfigManager(
                config_path=root / "config.toml",
                models_root=root / "models",
            )
            manager.save(WhisperstreamConfig(show_recording_control=True))
            reloaded = manager.load()

        self.assertIs(reloaded.show_recording_control, True)
        reloaded.validate()  # Should not raise

    def test_from_dict_then_validate(self):
        """Verify the flow used by ConfigManager (from_dict -> validate)."""
        data = {"threads": -5}
        conf = WhisperstreamConfig.from_dict(data)

        with self.assertRaisesRegex(ValueError, "threads must be non-negative"):
            conf.validate()


    def test_clipboard_behavior_default_is_true(self):
        """clipboard_behavior defaults to True (save-and-restore prior clipboard)."""
        conf = WhisperstreamConfig()
        self.assertIs(conf.clipboard_behavior, True)

    def test_clipboard_behavior_round_trip_via_from_dict(self):
        """clipboard_behavior round-trips through from_dict for both True and False."""
        conf_false = WhisperstreamConfig.from_dict({"clipboard_behavior": False})
        self.assertIs(conf_false.clipboard_behavior, False)
        conf_false.validate()  # Should not raise

        conf_true = WhisperstreamConfig.from_dict({"clipboard_behavior": True})
        self.assertIs(conf_true.clipboard_behavior, True)
        conf_true.validate()  # Should not raise

    def test_clipboard_clear_delay_round_trip_via_from_dict(self):
        """clipboard_clear_delay round-trips a custom value through from_dict."""
        conf = WhisperstreamConfig.from_dict({"clipboard_clear_delay": 1.5})
        self.assertEqual(conf.clipboard_clear_delay, 1.5)
        conf.validate()  # Should not raise

    # --- toggle_safety_timeout / max_recording_duration ---
    # chunk_duration / chunk_overlap were removed: chunking is locked at the
    # constants in chunking.py and is no longer a config field.

    def test_default_config_still_passes_validation(self):
        """Default config (max_recording_duration=0) passes validation."""
        conf = WhisperstreamConfig()
        self.assertEqual(conf.toggle_safety_timeout, 1800.0)
        self.assertEqual(conf.max_recording_duration, 0.0)
        conf.validate()  # Should not raise

    def test_chunking_fields_removed_from_config(self):
        """chunk_duration / chunk_overlap are no longer config fields."""
        conf = WhisperstreamConfig()
        self.assertFalse(hasattr(conf, "chunk_duration"))
        self.assertFalse(hasattr(conf, "chunk_overlap"))

    def test_legacy_chunk_keys_ignored_by_from_dict(self):
        """A legacy config.toml carrying chunk_duration/overlap loads cleanly."""
        conf = WhisperstreamConfig.from_dict(
            {"chunk_duration": 4.0, "chunk_overlap": 0.5}
        )
        self.assertFalse(hasattr(conf, "chunk_duration"))
        conf.validate()  # Should not raise

    def test_toggle_safety_timeout_negative_raises(self):
        """toggle_safety_timeout=-1 should fail validation."""
        conf = WhisperstreamConfig(toggle_safety_timeout=-1)
        with self.assertRaisesRegex(ValueError, "toggle_safety_timeout"):
            conf.validate()

    def test_toggle_safety_timeout_exceeds_max_raises(self):
        """toggle_safety_timeout=7201 should fail validation."""
        conf = WhisperstreamConfig(toggle_safety_timeout=7201)
        with self.assertRaisesRegex(ValueError, "toggle_safety_timeout"):
            conf.validate()

    def test_toggle_safety_timeout_1800_passes(self):
        """toggle_safety_timeout=1800 should pass validation."""
        conf = WhisperstreamConfig(toggle_safety_timeout=1800)
        conf.validate()  # Should not raise

    def test_legacy_max_recording_duration_from_dict(self):
        """Loading a config with max_recording_duration=45.0 via from_dict still produces a valid config."""
        conf = WhisperstreamConfig.from_dict({"max_recording_duration": 45.0})
        self.assertEqual(conf.max_recording_duration, 45.0)
        conf.validate()  # Should not raise


    # --- Task 1.1: Enhancement config fields ---

    def test_validate_enhancement_provider_invalid(self):
        """Invalid enhancement_provider should fail validation."""
        conf = WhisperstreamConfig(enhancement_provider="badprovider")
        with self.assertRaisesRegex(ValueError, "enhancement_provider"):
            conf.validate()

    def test_validate_enhancement_mode_invalid(self):
        """Invalid enhancement_mode should fail validation."""
        conf = WhisperstreamConfig(enhancement_mode="badmode")
        with self.assertRaisesRegex(ValueError, "enhancement_mode"):
            conf.validate()

    def test_enhancement_config_roundtrip(self):
        """Enhancement fields roundtrip through to_dict/from_dict correctly."""
        conf = WhisperstreamConfig(
            enhancement_enabled=True,
            enhancement_provider="openai",
            enhancement_api_key="sk-test",
            enhancement_ollama_endpoint="http://localhost:11434",
            enhancement_ollama_model="llama3.2",
            enhancement_mode="chat",
            enhancement_custom_prompt="Fix my text.",
        )
        d = conf.to_dict()
        conf2 = WhisperstreamConfig.from_dict(d)
        self.assertTrue(conf2.enhancement_enabled)
        self.assertEqual(conf2.enhancement_provider, "openai")
        self.assertEqual(conf2.enhancement_api_key, "sk-test")
        self.assertEqual(conf2.enhancement_ollama_endpoint, "http://localhost:11434")
        self.assertEqual(conf2.enhancement_ollama_model, "llama3.2")
        self.assertEqual(conf2.enhancement_mode, "chat")
        self.assertEqual(conf2.enhancement_custom_prompt, "Fix my text.")

    def test_enhancement_timeout_and_chunk_defaults(self):
        conf = WhisperstreamConfig()

        self.assertEqual(conf.enhancement_timeout_seconds, 5.0)
        self.assertTrue(conf.enhancement_chunked_cleanup)
        self.assertEqual(conf.enhancement_words_per_chunk, 120)
        self.assertTrue(conf.enhancement_limit_first_chunk)

    def test_enhancement_timeout_and_chunk_fields_roundtrip(self):
        conf = WhisperstreamConfig(
            enhancement_timeout_seconds=45,
            enhancement_chunked_cleanup=False,
            enhancement_words_per_chunk=240,
            enhancement_limit_first_chunk=False,
        )

        loaded = WhisperstreamConfig.from_dict(conf.to_dict())

        self.assertEqual(loaded.enhancement_timeout_seconds, 45)
        self.assertFalse(loaded.enhancement_chunked_cleanup)
        self.assertEqual(loaded.enhancement_words_per_chunk, 240)
        self.assertFalse(loaded.enhancement_limit_first_chunk)

    def test_enhancement_timeout_validation_is_bounded(self):
        for value in (0, 301, "5", True):
            with self.subTest(value=value):
                conf = WhisperstreamConfig(enhancement_timeout_seconds=value)
                with self.assertRaisesRegex(ValueError, "enhancement_timeout_seconds"):
                    conf.validate()

    def test_enhancement_words_per_chunk_validation_is_bounded_integer(self):
        for value in (19, 501, 120.5, "120", True):
            with self.subTest(value=value):
                conf = WhisperstreamConfig(enhancement_words_per_chunk=value)
                with self.assertRaisesRegex(ValueError, "enhancement_words_per_chunk"):
                    conf.validate()

    def test_enhancement_provider_lowercased_in_from_dict(self):
        """from_dict normalizes enhancement_provider to lowercase."""
        conf = WhisperstreamConfig.from_dict({"enhancement_provider": "OpenAI"})
        self.assertEqual(conf.enhancement_provider, "openai")

    def test_enhancement_mode_lowercased_in_from_dict(self):
        """from_dict normalizes enhancement_mode to lowercase."""
        conf = WhisperstreamConfig.from_dict({"enhancement_mode": "Custom"})
        self.assertEqual(conf.enhancement_mode, "custom")

    def test_save_load_with_enhancement_fields(self):
        """ConfigManager save/load roundtrips enhancement fields."""
        import tempfile
        from pathlib import Path
        from whisperstream.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            models_path = Path(tmpdir) / "models"
            mgr = ConfigManager(config_path=config_path, models_root=models_path)

            conf = WhisperstreamConfig(
                enhancement_enabled=True,
                enhancement_provider="anthropic",
                enhancement_api_key="",
                enhancement_mode="code",
            )
            mgr.save(conf)
            loaded = mgr.load()

        self.assertTrue(loaded.enhancement_enabled)
        self.assertEqual(loaded.enhancement_provider, "anthropic")
        self.assertEqual(loaded.enhancement_mode, "code")

    def test_per_provider_api_keys_roundtrip(self):
        """Per-provider key slots roundtrip through to_dict / from_dict.

        The active mirror enhancement_api_key reflects the slot for the
        currently selected provider; switching providers via from_dict
        promotes the new provider's slot to the mirror.
        """
        conf = WhisperstreamConfig(
            enhancement_provider="gemini",
            enhancement_gemini_api_key="aiza-gem",
            enhancement_openai_api_key="sk-oai",
            enhancement_anthropic_api_key="sk-ant",
        )
        loaded = WhisperstreamConfig.from_dict(conf.to_dict())
        self.assertEqual(loaded.enhancement_gemini_api_key, "aiza-gem")
        self.assertEqual(loaded.enhancement_openai_api_key, "sk-oai")
        self.assertEqual(loaded.enhancement_anthropic_api_key, "sk-ant")
        # Active mirror reflects the active provider's slot.
        self.assertEqual(loaded.enhancement_api_key, "aiza-gem")
        self.assertEqual(loaded.active_api_key(), "aiza-gem")

        # Switching providers via from_dict re-reconciles the mirror.
        payload = loaded.to_dict()
        payload["enhancement_provider"] = "openai"
        switched = WhisperstreamConfig.from_dict(payload)
        self.assertEqual(switched.enhancement_api_key, "sk-oai")
        self.assertEqual(switched.active_api_key(), "sk-oai")
        # Other slots are preserved across the switch.
        self.assertEqual(switched.enhancement_gemini_api_key, "aiza-gem")
        self.assertEqual(switched.enhancement_anthropic_api_key, "sk-ant")

    def test_provider_switch_does_not_bleed_legacy_into_new_slot(self):
        """Switching providers must NOT leak the previous provider's key.

        The frontend re-sends the full config on every save, so the legacy
        active-mirror arrives carrying the prior provider's key. The
        migration block must skip seeding when any per-provider slot is
        already populated — otherwise switching from Gemini (configured) to
        Anthropic (empty) would copy the Gemini key into the Anthropic slot.
        """
        # User has Gemini configured. Round-trip through disk so the legacy
        # mirror is populated alongside the gemini slot.
        initial = WhisperstreamConfig(
            enhancement_provider="gemini",
            enhancement_gemini_api_key="AIza-gem",
        )
        state = WhisperstreamConfig.from_dict(initial.to_dict())
        self.assertEqual(state.enhancement_api_key, "AIza-gem")

        # User switches provider in the UI; App.tsx PUTs the full config
        # back with provider=anthropic but the legacy mirror still carries
        # the stale gemini key.
        payload = state.to_dict()
        payload["enhancement_provider"] = "anthropic"
        switched = WhisperstreamConfig.from_dict(payload)

        # The empty anthropic slot must stay empty.
        self.assertEqual(switched.enhancement_anthropic_api_key, "")
        self.assertEqual(switched.active_api_key(), "")
        # The legacy mirror now reflects anthropic's empty slot, not the
        # stale gemini key.
        self.assertEqual(switched.enhancement_api_key, "")
        # Gemini's stored key is preserved across the switch.
        self.assertEqual(switched.enhancement_gemini_api_key, "AIza-gem")

    def test_legacy_api_key_migrates_to_active_provider_slot(self):
        """An old config with only enhancement_api_key set seeds the slot
        for the currently active provider on first load."""
        conf = WhisperstreamConfig.from_dict({
            "enhancement_provider": "anthropic",
            "enhancement_api_key": "sk-ant-legacy",
        })
        self.assertEqual(conf.enhancement_anthropic_api_key, "sk-ant-legacy")
        self.assertEqual(conf.enhancement_gemini_api_key, "")
        self.assertEqual(conf.enhancement_openai_api_key, "")
        self.assertEqual(conf.enhancement_api_key, "sk-ant-legacy")
        self.assertEqual(conf.active_api_key(), "sk-ant-legacy")

    def test_active_api_key_falls_back_to_legacy_field(self):
        """active_api_key() honors a direct enhancement_api_key write when
        the per-provider slot is empty so older callers / tests that set
        the field directly keep working."""
        conf = WhisperstreamConfig(
            enhancement_provider="openai",
            enhancement_api_key="sk-test",
        )
        self.assertEqual(conf.active_api_key(), "sk-test")

    def test_active_api_key_returns_empty_for_keyless_providers(self):
        """Providers without an API-key slot (ollama, local) return ''."""
        for provider in ("ollama", "local"):
            conf = WhisperstreamConfig(
                enhancement_provider=provider,
                enhancement_api_key="should-not-leak",
                enhancement_gemini_api_key="should-not-leak",
            )
            self.assertEqual(conf.active_api_key(), "", msg=f"provider={provider}")

    def test_legacy_acknowledged_providers_field_silently_dropped(self):
        """Old configs with enhancement_privacy_acknowledged_providers load without error.

        The field was removed in favour of byok-consent.json. from_dict drops
        unknown keys so existing config.toml files don't crash the app.
        """
        conf = WhisperstreamConfig.from_dict({
            "enhancement_enabled": True,
            "enhancement_provider": "openai",
            "enhancement_privacy_acknowledged_providers": ["openai"],
        })
        self.assertTrue(conf.enhancement_enabled)
        self.assertEqual(conf.enhancement_provider, "openai")
        self.assertFalse(hasattr(conf, "enhancement_privacy_acknowledged_providers"))


    # --- Task 1.1 (ai-enhance-local): local provider fields ---
    # NOTE: "local" now round-trips unchanged through from_dict (the unship-local
    # migration was removed in Task 1.2). The default for new installs remains
    # "gemini" -- see test_from_dict_empty_dict_defaults_to_gemini below.

    def test_local_provider_roundtrip_from_dict_and_validate(self):
        """Explicit enhancement_provider='local' round-trips through from_dict unchanged and validate() does not raise."""
        conf = WhisperstreamConfig.from_dict({"enhancement_provider": "local"})
        self.assertEqual(conf.enhancement_provider, "local")
        conf.validate()  # Must NOT raise

    def test_unknown_provider_still_raises(self):
        """Unknown enhancement_provider still raises ValueError."""
        conf = WhisperstreamConfig(enhancement_provider="unknown_provider")
        with self.assertRaisesRegex(ValueError, "enhancement_provider"):
            conf.validate()

    def test_enhancement_local_model_default(self):
        """Default enhancement_local_model is 'stream-1-mini'."""
        conf = WhisperstreamConfig()
        self.assertEqual(conf.enhancement_local_model, "stream-1-mini")

    def test_enhancement_local_port_default(self):
        """Default enhancement_local_port is 0."""
        conf = WhisperstreamConfig()
        self.assertEqual(conf.enhancement_local_port, 0)

    def test_enhancement_local_endpoint_default(self):
        """Default enhancement_local_endpoint is empty string."""
        conf = WhisperstreamConfig()
        self.assertEqual(conf.enhancement_local_endpoint, "")

    def test_enhancement_local_model_lowercased_in_from_dict(self):
        """from_dict normalizes enhancement_local_model to lowercase."""
        conf = WhisperstreamConfig.from_dict({"enhancement_local_model": "Gemma4-E2B-Instruct"})
        self.assertEqual(conf.enhancement_local_model, "gemma4-e2b-instruct")

    # --- Task 1.2 (local-model-picker): catalog-membership validation ---

    def test_enhancement_local_model_unknown_falls_back_to_default(self):
        """An unknown enhancement_local_model id falls back to the default via from_dict.

        An unrecognized id would otherwise silently skip warmup/prepare and
        mislead the benchmark, so from_dict must fall back to the default
        rather than leaving it in place.
        """
        conf = WhisperstreamConfig.from_dict({"enhancement_local_model": "bogus-model"})
        self.assertEqual(conf.enhancement_local_model, "stream-1-mini")

    def test_enhancement_local_model_valid_ids_pass_through(self):
        """Each of the 3 catalog ids passes through from_dict unchanged."""
        for model_id in ("stream-1-mini", "gemma4-e2b-instruct"):
            conf = WhisperstreamConfig.from_dict({"enhancement_local_model": model_id})
            self.assertEqual(conf.enhancement_local_model, model_id)

    def test_enhancement_local_model_case_variant_lowercases_and_passes(self):
        """A valid case-variant id lowercases and does NOT fall back to the default."""
        conf = WhisperstreamConfig.from_dict({"enhancement_local_model": "GEMMA4-E2B-INSTRUCT"})
        self.assertEqual(conf.enhancement_local_model, "gemma4-e2b-instruct")

        conf2 = WhisperstreamConfig.from_dict({"enhancement_local_model": "GEMMA4-E2B-INSTRUCT"})
        self.assertEqual(conf2.enhancement_local_model, "gemma4-e2b-instruct")

    # --- Task 1.2 (enhancement-safety): enhancement_safeguards_enabled ---

    def test_enhancement_safeguards_enabled_default_is_true(self):
        """enhancement_safeguards_enabled defaults to True."""
        conf = WhisperstreamConfig()
        self.assertIs(conf.enhancement_safeguards_enabled, True)

    def test_enhancement_safeguards_enabled_round_trip_via_from_dict(self):
        """enhancement_safeguards_enabled=False round-trips through from_dict and to_dict."""
        conf_false = WhisperstreamConfig.from_dict({"enhancement_safeguards_enabled": False})
        self.assertIs(conf_false.enhancement_safeguards_enabled, False)
        conf_false.validate()  # Should not raise

        d = conf_false.to_dict()
        self.assertIs(d["enhancement_safeguards_enabled"], False)

        conf_true = WhisperstreamConfig.from_dict({"enhancement_safeguards_enabled": True})
        self.assertIs(conf_true.enhancement_safeguards_enabled, True)
        conf_true.validate()  # Should not raise


    # --- enhancement_provider normalization and defaults ---

    def test_from_dict_LOCAL_uppercase_normalizes_to_local(self):
        """from_dict({"enhancement_provider": "LOCAL"}) lowercases to 'local' (no migration)."""
        conf = WhisperstreamConfig.from_dict({"enhancement_provider": "LOCAL"})
        self.assertEqual(conf.enhancement_provider, "local")

    def test_from_dict_empty_dict_defaults_to_gemini(self):
        """from_dict({}) (fresh install) produces enhancement_provider == 'gemini'."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertEqual(conf.enhancement_provider, "gemini")

    def test_from_dict_gemini_is_noop(self):
        """from_dict({"enhancement_provider": "gemini"}) stays 'gemini' (no-op)."""
        conf = WhisperstreamConfig.from_dict({"enhancement_provider": "gemini"})
        self.assertEqual(conf.enhancement_provider, "gemini")

    def test_validator_accepts_direct_construction_local(self):
        """Direct WhisperstreamConfig(enhancement_provider='local') still passes validate().

        The validator whitelist is intentionally kept permissive so test fixtures
        that construct config objects directly don't break.
        """
        conf = WhisperstreamConfig(enhancement_provider="local")
        conf.validate()  # Must NOT raise


    # --- GPU benchmark config fields (device identity + per-model verdict map) ---

    def test_enhancement_local_gpu_device_default(self):
        """enhancement_local_gpu_device defaults to empty string."""
        conf = WhisperstreamConfig()
        self.assertEqual(conf.enhancement_local_gpu_device, "")

    def test_enhancement_local_gpu_device_roundtrip(self):
        """enhancement_local_gpu_device round-trips a device identity string."""
        conf = WhisperstreamConfig.from_dict(
            {"enhancement_local_gpu_device": "NVIDIA GeForce RTX 4070 Ti SUPER"}
        )
        self.assertEqual(conf.enhancement_local_gpu_device, "NVIDIA GeForce RTX 4070 Ti SUPER")
        conf.validate()  # Must not raise

    def test_enhancement_local_gpu_fields_absent_from_dict_use_defaults(self):
        """from_dict({}) (fresh install) uses defaults for the GPU fields."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertEqual(conf.enhancement_local_gpu_verdicts, {})
        self.assertEqual(conf.enhancement_local_gpu_device, "")


    # --- Task 1.3 (tiered-benchmark): per-model GPU verdict map ---

    def test_enhancement_local_gpu_verdicts_default_empty(self):
        """A bare WhisperstreamConfig() has an empty per-model verdict map."""
        conf = WhisperstreamConfig()
        self.assertEqual(conf.enhancement_local_gpu_verdicts, {})

    def test_enhancement_local_gpu_verdicts_absent_from_dict_stays_empty(self):
        """from_dict({}) (fresh install, no legacy scalar) yields an empty map."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertEqual(conf.enhancement_local_gpu_verdicts, {})

    def test_enhancement_local_gpu_verdicts_to_dict_serializes(self):
        """to_dict() includes the map, and from_dict() on that payload round-trips it."""
        conf = WhisperstreamConfig.from_dict(
            {"enhancement_local_gpu_verdicts": {"stream-1": "viable", "stream-1-lite": "marginal"}}
        )
        payload = conf.to_dict()
        self.assertEqual(
            payload["enhancement_local_gpu_verdicts"],
            {"stream-1": "viable", "stream-1-lite": "marginal"},
        )
        conf2 = WhisperstreamConfig.from_dict(payload)
        self.assertEqual(conf2.enhancement_local_gpu_verdicts, conf.enhancement_local_gpu_verdicts)

    def test_enhancement_local_gpu_verdicts_migrates_legacy_scalar_onto_named_model(self):
        """Legacy scalar + a known enhancement_local_model seeds THAT model's entry."""
        conf = WhisperstreamConfig.from_dict(
            {
                "enhancement_local_gpu_verdict": "viable",
                "enhancement_local_model": "stream-1-mini",
            }
        )
        self.assertEqual(conf.enhancement_local_gpu_verdicts, {"stream-1-mini": "viable"})
        # Migration never rewrites the user's model/provider choice.
        self.assertEqual(conf.enhancement_local_model, "stream-1-mini")

    def test_enhancement_local_gpu_verdicts_migrates_legacy_scalar_onto_gemma_when_model_unset(self):
        """Legacy scalar with no enhancement_local_model migrates onto Gemma
        (LOCAL_BUNDLED_MODELS[0]), the pre-tiered-benchmark default local model."""
        conf = WhisperstreamConfig.from_dict({"enhancement_local_gpu_verdict": "unviable"})
        self.assertEqual(conf.enhancement_local_gpu_verdicts, {"gemma4-e2b-instruct": "unviable"})

    def test_enhancement_local_gpu_verdicts_migrates_legacy_scalar_onto_gemma_when_model_unknown(self):
        """An unrecognized enhancement_local_model id falls back to Gemma as the
        migration target (mirrors the field's own unknown-id fallback)."""
        conf = WhisperstreamConfig.from_dict(
            {
                "enhancement_local_gpu_verdict": "viable",
                "enhancement_local_model": "some-unknown-id",
            }
        )
        self.assertEqual(conf.enhancement_local_gpu_verdicts, {"gemma4-e2b-instruct": "viable"})

    def test_enhancement_local_gpu_verdicts_no_migration_when_legacy_not_run(self):
        """Legacy scalar of 'not_run' (the default) seeds no map entry."""
        conf = WhisperstreamConfig.from_dict({"enhancement_local_gpu_verdict": "not_run"})
        self.assertEqual(conf.enhancement_local_gpu_verdicts, {})

    def test_enhancement_local_gpu_verdicts_no_migration_when_legacy_empty(self):
        """Legacy scalar of '' seeds no map entry."""
        conf = WhisperstreamConfig.from_dict({"enhancement_local_gpu_verdict": ""})
        self.assertEqual(conf.enhancement_local_gpu_verdicts, {})

    def test_enhancement_local_gpu_verdicts_migration_gated_on_map_absence(self):
        """A present (even empty) map is never re-seeded from the legacy scalar."""
        conf = WhisperstreamConfig.from_dict(
            {
                "enhancement_local_gpu_verdict": "viable",
                "enhancement_local_gpu_verdicts": {},
            }
        )
        self.assertEqual(conf.enhancement_local_gpu_verdicts, {})

    def test_enhancement_local_gpu_verdicts_drops_non_string_keys_and_values(self):
        """Non-string keys/values in a loaded map are dropped entirely, not coerced."""
        conf = WhisperstreamConfig.from_dict(
            {
                "enhancement_local_gpu_verdicts": {
                    "stream-1": "viable",
                    123: "unviable",  # non-string key -> dropped
                    "stream-1-lite": 456,  # non-string value -> dropped
                }
            }
        )
        self.assertEqual(conf.enhancement_local_gpu_verdicts, {"stream-1": "viable"})

    def test_enhancement_local_gpu_verdicts_coerces_unknown_values_to_not_run(self):
        """An unrecognized verdict string for a present entry coerces to 'not_run'
        (the key is kept, unlike the non-string-value drop above)."""
        conf = WhisperstreamConfig.from_dict(
            {"enhancement_local_gpu_verdicts": {"stream-1": "bogus_value"}}
        )
        self.assertEqual(conf.enhancement_local_gpu_verdicts, {"stream-1": "not_run"})

    def test_enhancement_local_gpu_verdicts_non_dict_normalizes_to_empty(self):
        """A non-dict value (hand-edited config) normalizes to {} rather than crashing."""
        conf = WhisperstreamConfig.from_dict({"enhancement_local_gpu_verdicts": "not-a-dict"})
        self.assertEqual(conf.enhancement_local_gpu_verdicts, {})

    def test_enhancement_local_gpu_verdicts_accepts_all_four_tier_states(self):
        """All four tri-state values pass through the map unchanged."""
        raw = {
            "gemma4-e2b-instruct": "not_run",
            "stream-1": "viable",
            "stream-1-lite": "marginal",
            "other-id": "unviable",
        }
        conf = WhisperstreamConfig.from_dict({"enhancement_local_gpu_verdicts": dict(raw)})
        self.assertEqual(conf.enhancement_local_gpu_verdicts, raw)
        conf.validate()  # Must not raise

    def test_get_local_gpu_verdict_helper_returns_mapped_value(self):
        """get_local_gpu_verdict() reads the map, defaulting to 'not_run' for
        any model id that is absent (never benchmarked)."""
        from whisperstream.config_manager import get_local_gpu_verdict

        conf = WhisperstreamConfig.from_dict(
            {"enhancement_local_gpu_verdicts": {"stream-1": "marginal"}}
        )
        self.assertEqual(get_local_gpu_verdict(conf, "stream-1"), "marginal")
        self.assertEqual(get_local_gpu_verdict(conf, "stream-1-lite"), "not_run")
        self.assertEqual(get_local_gpu_verdict(conf, "unknown-id"), "not_run")

    def test_enhancement_local_gpu_verdicts_toml_round_trip_through_save_and_load(self):
        """A non-empty map survives ConfigManager.save() -> tomllib.load() ->
        from_dict() byte-identical, with a [enhancement_local_gpu_verdicts]
        table header on disk (mirrors the power_mode_map TOML round-trip test)."""
        import tempfile
        import tomllib
        from pathlib import Path

        from whisperstream.config_manager import ConfigManager

        verdicts = {"stream-1": "viable", "stream-1-lite": "marginal"}
        cfg = WhisperstreamConfig(enhancement_local_gpu_verdicts=dict(verdicts))

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = ConfigManager(
                config_path=Path(tmpdir) / "config.toml",
                models_root=Path(tmpdir) / "models",
            )
            mgr.save(cfg)

            on_disk_text = mgr.config_path.read_text(encoding="utf-8")
            self.assertIn("[enhancement_local_gpu_verdicts]", on_disk_text)

            with mgr.config_path.open("rb") as handle:
                data = tomllib.load(handle)

            self.assertEqual(data["enhancement_local_gpu_verdicts"], verdicts)
            reloaded = WhisperstreamConfig.from_dict(data)

        self.assertEqual(reloaded.enhancement_local_gpu_verdicts, verdicts)

    def test_enhancement_local_gpu_verdicts_empty_map_emits_no_table_header(self):
        """An empty map emits no [enhancement_local_gpu_verdicts] table (mirrors
        the power_mode_map / word_replacements only-when-non-empty rule)."""
        import tempfile
        from pathlib import Path

        from whisperstream.config_manager import ConfigManager

        cfg = WhisperstreamConfig()
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = ConfigManager(
                config_path=Path(tmpdir) / "config.toml",
                models_root=Path(tmpdir) / "models",
            )
            mgr.save(cfg)
            on_disk_text = mgr.config_path.read_text(encoding="utf-8")
        self.assertNotIn("[enhancement_local_gpu_verdicts]", on_disk_text)


    # --- Task 1.2 (settings-improvements): hotkey migration from_dict scenarios ---

    def test_migration_legacy_ptt_config(self):
        """Legacy PTT config migrates primary_shortcut into push_to_talk_shortcut."""
        conf = WhisperstreamConfig.from_dict({
            "primary_shortcut": "right shift",
            "recording_mode": "push_to_talk",
        })
        self.assertEqual(conf.push_to_talk_shortcut, "right shift")
        self.assertEqual(conf.toggle_shortcut, "")
        conf.validate()  # Should not raise

    def test_migration_legacy_toggle_config(self):
        """Legacy toggle config migrates primary_shortcut into toggle_shortcut."""
        conf = WhisperstreamConfig.from_dict({
            "primary_shortcut": "f9",
            "recording_mode": "toggle",
        })
        self.assertEqual(conf.toggle_shortcut, "f9")
        self.assertEqual(conf.push_to_talk_shortcut, "")
        conf.validate()  # Should not raise

    def test_migration_resave_guard(self):
        """A dict already containing both new hotkey fields must NOT be clobbered.

        The frontend PUTs the full config on every save. Without the both-absent
        gate the migration would overwrite the user's new shortcut with the legacy
        primary_shortcut value on every round-trip.
        """
        conf = WhisperstreamConfig.from_dict({
            "push_to_talk_shortcut": "f8",
            "toggle_shortcut": "",
            "primary_shortcut": "right shift",
            "recording_mode": "toggle",
        })
        self.assertEqual(conf.push_to_talk_shortcut, "f8")
        self.assertEqual(conf.toggle_shortcut, "")
        conf.validate()  # Should not raise

    def test_migration_fresh_config_defaults(self):
        """from_dict({}) (fresh install) produces correct hotkey defaults."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertEqual(conf.push_to_talk_shortcut, "right shift")
        self.assertEqual(conf.toggle_shortcut, "")
        conf.validate()  # Should not raise


    # --- Task 1.3 (transcripts-tab): history / save_audio / retention config fields ---

    def test_transcripts_fields_defaults(self):
        """The three transcripts config fields have the locked defaults."""
        conf = WhisperstreamConfig()
        self.assertIs(conf.history_enabled, platform.system() != "Darwin")
        self.assertIs(conf.save_audio, True)
        self.assertEqual(conf.transcript_retention_days, 7)
        conf.validate()  # Should not raise

    def test_transcripts_fields_in_to_dict(self):
        """to_dict serializes the three transcripts fields with their defaults."""
        d = WhisperstreamConfig().to_dict()
        self.assertIs(d["history_enabled"], platform.system() != "Darwin")
        self.assertIs(d["save_audio"], True)
        self.assertEqual(d["transcript_retention_days"], 7)

    def test_transcripts_fields_roundtrip_via_from_dict(self):
        """The three transcripts fields round-trip through to_dict/from_dict."""
        conf = WhisperstreamConfig(
            history_enabled=False,
            save_audio=False,
            transcript_retention_days=30,
        )
        loaded = WhisperstreamConfig.from_dict(conf.to_dict())
        self.assertIs(loaded.history_enabled, False)
        self.assertIs(loaded.save_audio, False)
        self.assertEqual(loaded.transcript_retention_days, 30)
        loaded.validate()  # Should not raise

    def test_transcripts_fields_absent_from_dict_use_defaults(self):
        """from_dict({}) (fresh install) uses defaults for the three transcripts fields."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertIs(conf.history_enabled, platform.system() != "Darwin")
        self.assertIs(conf.save_audio, True)
        self.assertEqual(conf.transcript_retention_days, 7)

    def test_transcript_retention_days_forever_zero(self):
        """transcript_retention_days=0 (Forever) round-trips unchanged."""
        conf = WhisperstreamConfig.from_dict({"transcript_retention_days": 0})
        self.assertEqual(conf.transcript_retention_days, 0)
        conf.validate()  # Should not raise

    def test_transcript_retention_days_negative_clamped_to_zero(self):
        """Negative transcript_retention_days is clamped to 0 (Forever) by from_dict."""
        conf = WhisperstreamConfig.from_dict({"transcript_retention_days": -5})
        self.assertEqual(conf.transcript_retention_days, 0)
        conf.validate()  # Should not raise

    def test_transcripts_unknown_key_strip_still_works(self):
        """An unknown key alongside the transcripts fields is stripped, not crashed on."""
        conf = WhisperstreamConfig.from_dict({
            "history_enabled": False,
            "save_audio": True,
            "transcript_retention_days": 1,
            "some_future_unknown_key": "ignored",
        })
        self.assertIs(conf.history_enabled, False)
        self.assertIs(conf.save_audio, True)
        self.assertEqual(conf.transcript_retention_days, 1)
        self.assertFalse(hasattr(conf, "some_future_unknown_key"))
        conf.validate()  # Should not raise


    # --- Task 1.1 (lockdown-hipaa-mode): lockdown config fields + validation ---

    def test_lockdown_mode_default_is_false(self):
        """lockdown_mode defaults to False."""
        conf = WhisperstreamConfig()
        self.assertIs(conf.lockdown_mode, False)

    def test_lockdown_idle_lock_seconds_default_is_300(self):
        """lockdown_idle_lock_seconds defaults to 300 (5 minutes)."""
        conf = WhisperstreamConfig()
        self.assertEqual(conf.lockdown_idle_lock_seconds, 300)

    def test_lockdown_mode_roundtrip_true(self):
        """lockdown_mode=True round-trips through from_dict and to_dict."""
        conf = WhisperstreamConfig.from_dict({"lockdown_mode": True, "enhancement_provider": "local"})
        self.assertIs(conf.lockdown_mode, True)
        d = conf.to_dict()
        self.assertIn("lockdown_mode", d)
        self.assertIs(d["lockdown_mode"], True)

    def test_lockdown_mode_roundtrip_false(self):
        """lockdown_mode=False round-trips through from_dict and to_dict."""
        conf = WhisperstreamConfig.from_dict({"lockdown_mode": False})
        self.assertIs(conf.lockdown_mode, False)

    def test_lockdown_idle_lock_seconds_roundtrip(self):
        """lockdown_idle_lock_seconds round-trips through from_dict and to_dict."""
        conf = WhisperstreamConfig.from_dict({"lockdown_idle_lock_seconds": 600})
        self.assertEqual(conf.lockdown_idle_lock_seconds, 600)
        d = conf.to_dict()
        self.assertIn("lockdown_idle_lock_seconds", d)
        self.assertEqual(d["lockdown_idle_lock_seconds"], 600)

    def test_lockdown_idle_lock_seconds_zero_never(self):
        """lockdown_idle_lock_seconds=0 (Never) round-trips unchanged."""
        conf = WhisperstreamConfig.from_dict({"lockdown_idle_lock_seconds": 0})
        self.assertEqual(conf.lockdown_idle_lock_seconds, 0)
        conf.validate()  # Should not raise

    def test_lockdown_fields_absent_from_dict_use_defaults(self):
        """from_dict({}) (fresh install) uses defaults for both lockdown fields."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertIs(conf.lockdown_mode, False)
        self.assertEqual(conf.lockdown_idle_lock_seconds, 300)

    def test_lockdown_idle_lock_seconds_string_coerced(self):
        """lockdown_idle_lock_seconds as a string integer is coerced to int."""
        conf = WhisperstreamConfig.from_dict({"lockdown_idle_lock_seconds": "600"})
        self.assertIsInstance(conf.lockdown_idle_lock_seconds, int)
        self.assertEqual(conf.lockdown_idle_lock_seconds, 600)

    def test_lockdown_idle_lock_seconds_invalid_string_defaults_to_300(self):
        """An invalid string for lockdown_idle_lock_seconds falls back to 300."""
        conf = WhisperstreamConfig.from_dict({"lockdown_idle_lock_seconds": "not_a_number"})
        self.assertEqual(conf.lockdown_idle_lock_seconds, 300)

    def test_lockdown_idle_lock_seconds_none_defaults_to_300(self):
        """None for lockdown_idle_lock_seconds falls back to 300."""
        conf = WhisperstreamConfig.from_dict({"lockdown_idle_lock_seconds": None})
        self.assertEqual(conf.lockdown_idle_lock_seconds, 300)

    def test_lockdown_idle_lock_seconds_negative_clamped_to_zero(self):
        """Negative lockdown_idle_lock_seconds is clamped to 0 by from_dict."""
        conf = WhisperstreamConfig.from_dict({"lockdown_idle_lock_seconds": -10})
        self.assertEqual(conf.lockdown_idle_lock_seconds, 0)

    def test_validate_lockdown_idle_lock_seconds_negative_raises(self):
        """Directly constructing with negative lockdown_idle_lock_seconds + validate() raises."""
        conf = WhisperstreamConfig(lockdown_idle_lock_seconds=-1)
        with self.assertRaisesRegex(ValueError, "lockdown_idle_lock_seconds"):
            conf.validate()

    def test_validate_lockdown_mode_on_local_provider_valid(self):
        """lockdown_mode=True + enhancement_provider='local' is valid.

        history_enabled is set False so this isolates the provider constraint
        from the F5 envelope rule (lockdown + history + no envelope is rejected
        separately; see test_validate_lockdown_history_without_envelope_raises).
        """
        conf = WhisperstreamConfig(
            lockdown_mode=True, enhancement_provider="local", history_enabled=False
        )
        conf.validate()  # Should not raise

    def test_validate_lockdown_history_without_envelope_raises(self):
        """F5: lockdown_mode + history_enabled with NO password envelope on disk
        must be rejected — saving history under lockdown requires a password."""
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"WHISPERSTREAM_DATA_DIR": tmpdir}):
                conf = WhisperstreamConfig(
                    lockdown_mode=True,
                    enhancement_provider="local",
                    history_enabled=True,
                )
                with self.assertRaisesRegex(ValueError, "lockdown_history_no_envelope"):
                    conf.validate()

    def test_validate_lockdown_history_with_envelope_passes(self):
        """F5: lockdown_mode + history_enabled IS valid once a password envelope
        (lockdown.key.json) exists on disk."""
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        from whisperstream.transcript_crypto import load_or_create_data_key

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"WHISPERSTREAM_DATA_DIR": tmpdir}):
                if load_or_create_data_key(Path(tmpdir) / "transcript.key") is None:
                    self.skipTest("DPAPI data key unavailable on this platform")
                from whisperstream.lockdown_keystore import enable_lockdown

                enable_lockdown(Path(tmpdir), "testpass1")
                conf = WhisperstreamConfig(
                    lockdown_mode=True,
                    enhancement_provider="local",
                    history_enabled=True,
                )
                conf.validate()  # Should not raise — envelope present.

    def test_validate_lockdown_mode_on_gemini_raises(self):
        """lockdown_mode=True + enhancement_provider='gemini' must raise ValueError."""
        conf = WhisperstreamConfig(lockdown_mode=True, enhancement_provider="gemini")
        with self.assertRaisesRegex(ValueError, "lockdown"):
            conf.validate()

    def test_validate_lockdown_mode_on_openai_raises(self):
        """lockdown_mode=True + enhancement_provider='openai' must raise ValueError."""
        conf = WhisperstreamConfig(lockdown_mode=True, enhancement_provider="openai")
        with self.assertRaisesRegex(ValueError, "lockdown"):
            conf.validate()

    def test_validate_lockdown_mode_on_anthropic_raises(self):
        """lockdown_mode=True + enhancement_provider='anthropic' must raise ValueError."""
        conf = WhisperstreamConfig(lockdown_mode=True, enhancement_provider="anthropic")
        with self.assertRaisesRegex(ValueError, "lockdown"):
            conf.validate()

    def test_validate_lockdown_mode_on_ollama_raises(self):
        """lockdown_mode=True + enhancement_provider='ollama' must raise ValueError.
        Ollama can point at a remote host, so it is not trusted as 'local' under lockdown."""
        conf = WhisperstreamConfig(lockdown_mode=True, enhancement_provider="ollama")
        with self.assertRaisesRegex(ValueError, "lockdown"):
            conf.validate()

    def test_validate_lockdown_off_gemini_valid(self):
        """lockdown_mode=False + enhancement_provider='gemini' is valid (lockdown off)."""
        conf = WhisperstreamConfig(lockdown_mode=False, enhancement_provider="gemini")
        conf.validate()  # Should not raise

    def test_validate_lockdown_off_ollama_valid(self):
        """lockdown_mode=False + enhancement_provider='ollama' is valid (lockdown off)."""
        conf = WhisperstreamConfig(lockdown_mode=False, enhancement_provider="ollama")
        conf.validate()  # Should not raise

    # --- F3 (security-hardening): loopback-only local endpoint under lockdown ---

    def test_validate_lockdown_nonloopback_local_endpoint_raises(self):
        """F3: lockdown + a non-loopback enhancement_local_endpoint must be rejected.

        Under lockdown effective_provider() forces 'local', but the local
        dispatch honors enhancement_local_endpoint — an off-device URL would
        exfiltrate transcripts while lockdown_mode stays true. validate() rejects
        it (history off so the F5 envelope rule does not fire first)."""
        conf = WhisperstreamConfig(
            lockdown_mode=True,
            enhancement_provider="local",
            history_enabled=False,
            enhancement_local_endpoint="http://evil.example/v1",
        )
        with self.assertRaisesRegex(ValueError, "local_endpoint_not_loopback"):
            conf.validate()

    def test_validate_lockdown_loopback_endpoints_pass(self):
        """F3: empty / 127.0.0.1 / localhost / [::1] endpoints all validate under lockdown."""
        for endpoint in (
            "",
            "http://127.0.0.1:8080/v1",
            "http://localhost:11434",
            "http://[::1]:8080/v1",
        ):
            conf = WhisperstreamConfig(
                lockdown_mode=True,
                enhancement_provider="local",
                history_enabled=False,
                enhancement_local_endpoint=endpoint,
            )
            conf.validate()  # Should not raise

    def test_validate_lockdown_off_nonloopback_endpoint_rejected(self):
        """Local remains loopback-only outside Lockdown too."""
        conf = WhisperstreamConfig(
            lockdown_mode=False,
            enhancement_provider="local",
            enhancement_local_endpoint="http://192.168.1.50:8080/v1",
        )
        with self.assertRaisesRegex(ValueError, "local_endpoint_not_loopback"):
            conf.validate()

    def test_is_loopback_endpoint_helper(self):
        """The is_loopback_endpoint() helper classifies hosts correctly."""
        from whisperstream.config_manager import is_loopback_endpoint

        for ok in (
            None,
            "",
            "   ",
            "http://127.0.0.1:8080/v1",
            "http://127.5.5.5/v1",
            "http://localhost",
            "http://LOCALHOST:1234",
            "http://[::1]:8080",
            "127.0.0.1",
        ):
            self.assertTrue(is_loopback_endpoint(ok), f"{ok!r} should be loopback")
        for bad in (
            "http://evil.example/v1",
            "http://10.0.0.5:8080",
            "http://192.168.1.9",
            "https://api.openai.com/v1",
            "not a url with spaces",
        ):
            self.assertFalse(is_loopback_endpoint(bad), f"{bad!r} should NOT be loopback")


    # --- Task 1.2 (lockdown-hipaa-mode): effective_provider() accessor ---

    def test_effective_provider_lockdown_off_returns_stored_provider(self):
        """When lockdown_mode is False, effective_provider() returns the stored enhancement_provider."""
        conf = WhisperstreamConfig(lockdown_mode=False, enhancement_provider="gemini")
        self.assertEqual(conf.effective_provider(), "gemini")

    def test_effective_provider_lockdown_off_openai(self):
        """When lockdown_mode is False, effective_provider() returns 'openai' if that is stored."""
        conf = WhisperstreamConfig(lockdown_mode=False, enhancement_provider="openai")
        self.assertEqual(conf.effective_provider(), "openai")

    def test_effective_provider_lockdown_off_local(self):
        """When lockdown_mode is False and provider is 'local', effective_provider() returns 'local'."""
        conf = WhisperstreamConfig(lockdown_mode=False, enhancement_provider="local")
        self.assertEqual(conf.effective_provider(), "local")

    def test_effective_provider_lockdown_on_returns_local_regardless_of_stored(self):
        """When lockdown_mode is True, effective_provider() always returns 'local'."""
        conf = WhisperstreamConfig(lockdown_mode=True, enhancement_provider="local")
        self.assertEqual(conf.effective_provider(), "local")

    def test_effective_provider_lockdown_on_gemini_stored_returns_local(self):
        """When lockdown_mode is True and stored provider is 'gemini', effective_provider() returns 'local'."""
        conf = WhisperstreamConfig(lockdown_mode=True, enhancement_provider="gemini")
        self.assertEqual(conf.effective_provider(), "local")

    def test_effective_provider_lockdown_on_openai_stored_returns_local(self):
        """When lockdown_mode is True and stored provider is 'openai', effective_provider() returns 'local'."""
        conf = WhisperstreamConfig(lockdown_mode=True, enhancement_provider="openai")
        self.assertEqual(conf.effective_provider(), "local")

    def test_effective_provider_lockdown_on_anthropic_stored_returns_local(self):
        """When lockdown_mode is True and stored provider is 'anthropic', effective_provider() returns 'local'."""
        conf = WhisperstreamConfig(lockdown_mode=True, enhancement_provider="anthropic")
        self.assertEqual(conf.effective_provider(), "local")

    def test_effective_provider_lockdown_on_ollama_stored_returns_local(self):
        """When lockdown_mode is True and stored provider is 'ollama', effective_provider() returns 'local'."""
        conf = WhisperstreamConfig(lockdown_mode=True, enhancement_provider="ollama")
        self.assertEqual(conf.effective_provider(), "local")

    def test_effective_provider_does_not_mutate_stored_enhancement_provider(self):
        """Calling effective_provider() must not change the stored enhancement_provider field.

        This invariant is what allows restore-on-off: flipping lockdown off should
        restore the user's original cloud provider unchanged.
        """
        conf = WhisperstreamConfig(lockdown_mode=True, enhancement_provider="gemini")
        _ = conf.effective_provider()
        # The stored field must remain 'gemini' even though the accessor returned 'local'.
        self.assertEqual(conf.enhancement_provider, "gemini")

    def test_effective_provider_restore_on_off(self):
        """Toggling lockdown off restores the original stored provider via effective_provider()."""
        conf = WhisperstreamConfig(lockdown_mode=True, enhancement_provider="openai")
        self.assertEqual(conf.effective_provider(), "local")
        # Simulate turning lockdown off (stored provider is still 'openai').
        conf.lockdown_mode = False
        self.assertEqual(conf.effective_provider(), "openai")
        self.assertEqual(conf.enhancement_provider, "openai")

    # --- Task 1.5 (email-mode-product): legacy enhancement_mode validator allows "email" ---

    def test_validate_enhancement_mode_email_passes(self):
        """enhancement_mode='email' must not raise (legacy field, email is a valid mode)."""
        conf = WhisperstreamConfig(enhancement_mode="email")
        conf.validate()  # Must NOT raise

    def test_validate_enhancement_mode_unknown_still_raises(self):
        """Unknown enhancement_mode (e.g. 'garbage') must still raise ValueError."""
        conf = WhisperstreamConfig(enhancement_mode="garbage")
        with self.assertRaisesRegex(ValueError, "enhancement_mode"):
            conf.validate()


    # --- email-mode-product: signature is now a PER-PROFILE field ---
    # (The legacy global `email_signature` config field was removed; the default
    #  email sign-off lives on the profile as profile["signature"], migrated from
    #  any legacy top-level value onto the "email" profile.)

    def test_email_signature_is_not_a_global_field(self):
        """The global email_signature dataclass field no longer exists."""
        self.assertFalse(hasattr(WhisperstreamConfig(), "email_signature"))

    def _email_profile(self, conf):
        return next(p for p in conf.profiles if p.get("id") == "email")

    def test_legacy_email_signature_migrates_onto_email_profile(self):
        """A legacy top-level email_signature is moved onto the email profile's
        `signature` and is not resurrected as a config field."""
        conf = WhisperstreamConfig.from_dict({
            "email_signature": "Best,\nAlex",
            "profiles": [
                {"id": "chat", "name": "Chat", "prompt": "c"},
                {"id": "email", "name": "Email", "prompt": "e"},
            ],
            "default_profile_id": "chat",
        })
        self.assertEqual(self._email_profile(conf).get("signature"), "Best,\nAlex")
        self.assertFalse(hasattr(conf, "email_signature"))
        conf.validate()  # Should not raise

    def test_legacy_migration_does_not_clobber_existing_profile_signature(self):
        """If the email profile already has a signature, migration leaves it."""
        conf = WhisperstreamConfig.from_dict({
            "email_signature": "Best,\nAlex",
            "profiles": [
                {"id": "email", "name": "Email", "prompt": "e", "signature": "Cheers,\nSam"},
            ],
            "default_profile_id": "email",
        })
        self.assertEqual(self._email_profile(conf).get("signature"), "Cheers,\nSam")

    def test_per_profile_signature_multiline_roundtrip_save_load(self):
        """A per-profile signature round-trips (multi-line) through save/load."""
        import tempfile
        from pathlib import Path
        from whisperstream.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            models_path = Path(tmpdir) / "models"
            mgr = ConfigManager(config_path=config_path, models_root=models_path)
            conf = mgr.load()
            self._email_profile(conf)["signature"] = "Warm regards,\nJordan Lee"
            mgr.save(conf)
            text = config_path.read_text(encoding="utf-8")
            self.assertIn("signature", text)
            loaded = mgr.load()
        self.assertEqual(self._email_profile(loaded).get("signature"), "Warm regards,\nJordan Lee")
        loaded.validate()  # Should not raise

    def test_profile_without_signature_emits_no_signature_line(self):
        """A profile with no signature must not emit a `signature =` key line.

        (The word "signature" legitimately appears inside the email prompt text;
        the assertion targets the TOML KEY line, not any substring.)"""
        import re
        import tempfile
        from pathlib import Path
        from whisperstream.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = ConfigManager(
                config_path=Path(tmpdir) / "config.toml",
                models_root=Path(tmpdir) / "models",
            )
            conf = mgr.load()  # fresh install: no profile has a signature
            mgr.save(conf)
            text = (Path(tmpdir) / "config.toml").read_text(encoding="utf-8")
            self.assertFalse(
                any(re.match(r"\s*signature\s*=", ln) for ln in text.splitlines()),
                "no profile should emit a `signature =` key line on a fresh install",
            )

    def test_fresh_install_has_no_profile_signatures(self):
        """from_dict({}) seeds profiles but none carry a signature."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertTrue(all("signature" not in p for p in conf.profiles))


    # --- Task 1.1 (keystroke-input): input_style + keystroke_delay_ms config fields ---

    def test_from_dict_unknown_input_style_coerces_to_clipboard(self):
        """Unknown input_style values (string, int, None) coerce to 'clipboard' via from_dict."""
        for bad_value in ("paste", 123, None):
            conf = WhisperstreamConfig.from_dict({"input_style": bad_value})
            self.assertEqual(
                conf.input_style,
                "clipboard",
                msg=f"Expected 'clipboard' for input_style={bad_value!r}, got {conf.input_style!r}",
            )

    def test_from_dict_keystroke_delay_ms_coerces_string_and_clamps_negative(self):
        """keystroke_delay_ms: string '10' coerces to int 10; negative coerces to 0."""
        conf_str = WhisperstreamConfig.from_dict({"keystroke_delay_ms": "10"})
        self.assertEqual(conf_str.keystroke_delay_ms, 10)

        conf_neg = WhisperstreamConfig.from_dict({"keystroke_delay_ms": -5})
        self.assertEqual(conf_neg.keystroke_delay_ms, 0)

    def test_validate_raises_for_invalid_input_style_and_negative_delay(self):
        """validate() raises ValueError for direct-construction with bad input_style or negative delay."""
        conf_bad_style = WhisperstreamConfig(input_style="bogus")
        with self.assertRaises(ValueError):
            conf_bad_style.validate()

        conf_neg_delay = WhisperstreamConfig(keystroke_delay_ms=-1)
        with self.assertRaises(ValueError):
            conf_neg_delay.validate()

    def test_valid_input_style_and_delay_round_trip_unchanged(self):
        """Valid input_style and keystroke_delay_ms values round-trip through from_dict unchanged."""
        conf_clipboard = WhisperstreamConfig.from_dict({
            "input_style": "clipboard",
            "keystroke_delay_ms": 15,
        })
        self.assertEqual(conf_clipboard.input_style, "clipboard")
        self.assertEqual(conf_clipboard.keystroke_delay_ms, 15)
        conf_clipboard.validate()  # Should not raise

        conf_keystroke = WhisperstreamConfig.from_dict({
            "input_style": "keystroke",
            "keystroke_delay_ms": 0,
        })
        self.assertEqual(conf_keystroke.input_style, "keystroke")
        self.assertEqual(conf_keystroke.keystroke_delay_ms, 0)
        conf_keystroke.validate()  # Should not raise


    # --- Task 1.2 (keystroke-input): per-profile input_style TOML round-trip ---

    def test_per_profile_input_style_roundtrip_via_config_manager(self):
        """Profile with input_style='keystroke' survives a ConfigManager save/load round-trip.

        The emit loop must conditionally emit input_style only when present so a
        keystroke profile retains its style and a profile without input_style emits
        no input_style line.
        """
        import tempfile
        from pathlib import Path
        from whisperstream.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            models_path = Path(tmpdir) / "models"
            mgr = ConfigManager(config_path=config_path, models_root=models_path)

            conf = WhisperstreamConfig(
                profiles=[
                    {"id": "prof-ks", "name": "Keystroke Profile", "prompt": "Clean this up.", "input_style": "keystroke"},
                    {"id": "prof-def", "name": "Default Profile", "prompt": "Fix grammar."},
                ],
            )
            mgr.save(conf)

            # Assert the written TOML contains input_style for the keystroke profile
            toml_text = config_path.read_text(encoding="utf-8")

            # The keystroke profile block must contain input_style = "keystroke"
            self.assertIn('input_style = "keystroke"', toml_text)

            # The default profile (no input_style) must not emit any input_style line
            # in its block. We verify by loading and checking the profile dict.
            loaded = mgr.load()

            ks_prof = next((p for p in loaded.profiles if p.get("id") == "prof-ks"), None)
            def_prof = next((p for p in loaded.profiles if p.get("id") == "prof-def"), None)

            self.assertIsNotNone(ks_prof, "Keystroke profile not found after reload")
            self.assertIsNotNone(def_prof, "Default profile not found after reload")

            self.assertEqual(ks_prof.get("input_style"), "keystroke",
                             "Keystroke profile should retain input_style=='keystroke' after round-trip")
            self.assertNotIn("input_style", def_prof,
                             "Profile without input_style should have no input_style key after reload")

    def test_per_profile_without_input_style_emits_no_input_style_line(self):
        """A profile without input_style must not emit an input_style line in the [[profiles]] block."""
        import tempfile
        from pathlib import Path
        from whisperstream.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            models_path = Path(tmpdir) / "models"
            mgr = ConfigManager(config_path=config_path, models_root=models_path)

            conf = WhisperstreamConfig(
                profiles=[
                    {"id": "plain", "name": "Plain Profile", "prompt": "Do nothing."},
                ],
            )
            mgr.save(conf)

            # Split at [[profiles]] to isolate the profiles section.
            toml_text = config_path.read_text(encoding="utf-8")
            profile_section = toml_text.split("[[profiles]]", 1)[-1]  # everything after the first [[profiles]]
            self.assertNotIn("input_style", profile_section,
                             "A profile without input_style must emit no input_style line in the [[profiles]] block")

            # Also confirm via reload that the profile dict has no input_style key.
            loaded = mgr.load()
            plain_prof = next((p for p in loaded.profiles if p.get("id") == "plain"), None)
            self.assertIsNotNone(plain_prof, "Plain profile not found after reload")
            self.assertNotIn("input_style", plain_prof,
                             "Profile without input_style should have no input_style key after reload")

    def test_unknown_and_empty_profile_icons_are_dropped_on_load(self):
        """Unknown icon values degrade to the frontend letter fallback."""
        import tempfile
        from pathlib import Path
        from whisperstream.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            models_path = Path(tmpdir) / "models"
            config_path.write_text(
                """
default_profile_id = "unknown"

[[profiles]]
id = "unknown"
name = "Unknown"
prompt = "Clean this up."
icon = "future-icon"

[[profiles]]
id = "empty"
name = "Empty"
prompt = "Clean this up."
icon = ""

[[profiles]]
id = "absent"
name = "Absent"
prompt = "Clean this up."

[[profiles]]
id = "malformed"
name = "Malformed"
prompt = "Clean this up."
icon = ["chat"]
""".lstrip(),
                encoding="utf-8",
            )
            loaded = ConfigManager(
                config_path=config_path,
                models_root=models_path,
            ).load()

        by_id = {profile["id"]: profile for profile in loaded.profiles}
        self.assertNotIn("icon", by_id["unknown"])
        self.assertNotIn("icon", by_id["empty"])
        self.assertNotIn("icon", by_id["absent"])
        self.assertNotIn("icon", by_id["malformed"])



    # --- Task K2: input_style_user_set dirty flag ---

    def test_input_style_user_set_default_is_false(self):
        """input_style_user_set defaults to False (app may auto-pick)."""
        conf = WhisperstreamConfig()
        self.assertIs(conf.input_style_user_set, False)

    def test_input_style_user_set_default_in_to_dict(self):
        """to_dict serializes input_style_user_set with its default (False)."""
        d = WhisperstreamConfig().to_dict()
        self.assertIn("input_style_user_set", d)
        self.assertIs(d["input_style_user_set"], False)

    def test_input_style_user_set_round_trip_true(self):
        """input_style_user_set=True round-trips through from_dict/to_dict."""
        conf = WhisperstreamConfig.from_dict({"input_style_user_set": True})
        self.assertIs(conf.input_style_user_set, True)
        conf.validate()  # Must not raise
        d = conf.to_dict()
        self.assertIs(d["input_style_user_set"], True)

    def test_input_style_user_set_round_trip_false(self):
        """input_style_user_set=False round-trips through from_dict/to_dict."""
        conf = WhisperstreamConfig.from_dict({"input_style_user_set": False})
        self.assertIs(conf.input_style_user_set, False)
        conf.validate()  # Must not raise

    def test_input_style_user_set_coerce_truthy_int_to_bool(self):
        """from_dict coerces truthy int (1) to True for input_style_user_set."""
        conf = WhisperstreamConfig.from_dict({"input_style_user_set": 1})
        self.assertIs(conf.input_style_user_set, True)

    def test_input_style_user_set_coerce_falsy_int_to_bool(self):
        """from_dict coerces 0 to False for input_style_user_set."""
        conf = WhisperstreamConfig.from_dict({"input_style_user_set": 0})
        self.assertIs(conf.input_style_user_set, False)

    def test_input_style_user_set_absent_from_dict_is_false(self):
        """from_dict({}) (fresh install) defaults input_style_user_set to False."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertIs(conf.input_style_user_set, False)

    def test_input_style_user_set_no_validate_constraint(self):
        """validate() has no constraint on input_style_user_set (both True/False pass)."""
        for val in (True, False):
            conf = WhisperstreamConfig(input_style_user_set=val)
            conf.validate()  # Must not raise

    def test_input_style_user_set_roundtrip_via_config_manager(self):
        """input_style_user_set round-trips through ConfigManager save/load."""
        import tempfile
        from pathlib import Path
        from whisperstream.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            models_path = Path(tmpdir) / "models"
            mgr = ConfigManager(config_path=config_path, models_root=models_path)

            conf = WhisperstreamConfig(input_style_user_set=True)
            mgr.save(conf)
            loaded = mgr.load()

        self.assertIs(loaded.input_style_user_set, True)


    # --- Task 1.1 (openai-compat-enhance): custom provider config fields ---

    def test_validate_accepts_custom_provider(self):
        """validate() must NOT raise when enhancement_provider='custom'."""
        conf = WhisperstreamConfig(enhancement_provider="custom")
        conf.validate()  # Must not raise

    def test_validate_custom_provider_in_error_message(self):
        """Error message for invalid provider must mention 'custom' in the allowed set."""
        conf = WhisperstreamConfig(enhancement_provider="notaprovider")
        try:
            conf.validate()
            self.fail("Expected ValueError was not raised")
        except ValueError as exc:
            self.assertIn("custom", str(exc))

    def test_custom_fields_defaults(self):
        """The three custom provider fields default to empty strings."""
        conf = WhisperstreamConfig()
        self.assertEqual(conf.enhancement_custom_base_url, "")
        self.assertEqual(conf.enhancement_custom_model, "")
        self.assertEqual(conf.enhancement_custom_api_key, "")

    def test_custom_fields_roundtrip_via_from_dict_to_dict(self):
        """The three custom fields round-trip through from_dict -> to_dict."""
        original = WhisperstreamConfig(
            enhancement_provider="custom",
            enhancement_custom_base_url="https://openrouter.ai/api/v1",
            enhancement_custom_model="openai/gpt-4o-mini",
            enhancement_custom_api_key="sk-or-test-key",
        )
        loaded = WhisperstreamConfig.from_dict(original.to_dict())
        self.assertEqual(loaded.enhancement_custom_base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(loaded.enhancement_custom_model, "openai/gpt-4o-mini")
        self.assertEqual(loaded.enhancement_custom_api_key, "sk-or-test-key")

    @unittest.skipUnless(sys.platform == "win32", "DPAPI encryption is Windows-only")
    def test_custom_api_key_persists_as_dpapi_reference(self):
        """ConfigManager persists the custom key in the legacy DPAPI format."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = ConfigManager(
                config_path=root / "config.toml",
                models_root=root / "models",
            )
            manager.save(
                WhisperstreamConfig(
                    enhancement_provider="custom",
                    enhancement_custom_api_key="sk-or-test-key",
                )
            )
            persisted = manager.config_path.read_text(encoding="utf-8")
        self.assertIn('enhancement_custom_api_key = "enc:', persisted)
        self.assertNotIn("sk-or-test-key", persisted)

    def test_custom_api_key_decrypted_in_from_dict(self):
        """from_dict keeps a directly supplied plaintext key in memory."""
        original = WhisperstreamConfig(
            enhancement_provider="custom",
            enhancement_custom_api_key="sk-or-test-key",
        )
        loaded = WhisperstreamConfig.from_dict(original.to_dict())
        self.assertEqual(loaded.enhancement_custom_api_key, "sk-or-test-key")

    def test_active_api_key_returns_custom_slot_when_provider_is_custom(self):
        """active_api_key() returns enhancement_custom_api_key when provider='custom'."""
        conf = WhisperstreamConfig(
            enhancement_provider="custom",
            enhancement_custom_api_key="sk-or-test-key",
        )
        self.assertEqual(conf.active_api_key(), "sk-or-test-key")

    def test_active_api_key_custom_roundtrips_through_from_dict(self):
        """active_api_key() returns the custom key after a to_dict/from_dict round-trip."""
        original = WhisperstreamConfig(
            enhancement_provider="custom",
            enhancement_custom_api_key="sk-or-round-trip",
        )
        loaded = WhisperstreamConfig.from_dict(original.to_dict())
        self.assertEqual(loaded.active_api_key(), "sk-or-round-trip")

    def test_custom_provider_in_provider_key_slot(self):
        """_PROVIDER_KEY_SLOT must map 'custom' to 'enhancement_custom_api_key'."""
        from whisperstream.config_manager import _PROVIDER_KEY_SLOT
        self.assertEqual(_PROVIDER_KEY_SLOT.get("custom"), "enhancement_custom_api_key")

    def test_custom_base_url_and_model_absent_from_dict_use_defaults(self):
        """from_dict({}) (fresh install) uses empty defaults for the three custom fields."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertEqual(conf.enhancement_custom_base_url, "")
        self.assertEqual(conf.enhancement_custom_model, "")
        self.assertEqual(conf.enhancement_custom_api_key, "")


    # --- Task 1.3 (app-themes): theme config field ---

    def test_theme_default_is_light(self):
        """theme defaults to 'light'."""
        conf = WhisperstreamConfig()
        self.assertEqual(conf.theme, "light")

    def test_theme_valid_passthrough_dark(self):
        """Valid theme 'dark' passes through from_dict unchanged."""
        conf = WhisperstreamConfig.from_dict({"theme": "dark"})
        self.assertEqual(conf.theme, "dark")

    def test_theme_legacy_names_alias_to_light_dark(self):
        """Pre-rename theme names alias to the new light/dark values."""
        self.assertEqual(WhisperstreamConfig.from_dict({"theme": "glacier"}).theme, "light")
        self.assertEqual(WhisperstreamConfig.from_dict({"theme": "glacier-dark"}).theme, "dark")
        self.assertEqual(WhisperstreamConfig.from_dict({"theme": "classic"}).theme, "light")

    def test_theme_invalid_coerces_to_light(self):
        """Invalid theme string coerces to 'light' via from_dict."""
        conf = WhisperstreamConfig.from_dict({"theme": "bogus"})
        self.assertEqual(conf.theme, "light")

    def test_theme_missing_from_dict_defaults_to_light(self):
        """Missing theme key in from_dict({}) defaults to 'light'."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertEqual(conf.theme, "light")


    # --- Task 1.1 (agent-speech): agent_speech_* config fields ---

    def test_agent_speech_fields_defaults(self):
        """The agent_speech_* fields have the locked seed-only defaults."""
        for system, expected_engine in (("Windows", "supertonic"), ("Darwin", "auto")):
            with self.subTest(system=system), patch("whisperstream.config_manager.platform.system", return_value=system):
                conf = WhisperstreamConfig()
                self.assertEqual(conf.agent_speech_mode, "off")
                self.assertEqual(conf.agent_speech_engine, expected_engine)
                self.assertEqual(conf.agent_speech_voice, 1)
                self.assertEqual(conf.agent_speech_kokoro_voice, "af_heart")
                self.assertEqual(conf.agent_speech_speed, 1.0)
                self.assertEqual(conf.agent_speech_volume, 1.0)

    def test_agent_speech_fields_in_to_dict(self):
        """to_dict serializes the speech fields with their defaults."""
        for system, expected_engine in (("Windows", "supertonic"), ("Darwin", "auto")):
            with self.subTest(system=system), patch("whisperstream.config_manager.platform.system", return_value=system):
                d = WhisperstreamConfig().to_dict()
                self.assertEqual(d["agent_speech_mode"], "off")
                self.assertEqual(d["agent_speech_engine"], expected_engine)
                self.assertEqual(d["agent_speech_voice"], 1)
                self.assertEqual(d["agent_speech_kokoro_voice"], "af_heart")
                self.assertEqual(d["agent_speech_speed"], 1.0)
                self.assertEqual(d["agent_speech_volume"], 1.0)

    def test_agent_speech_fields_roundtrip_via_from_dict(self):
        """Non-default agent_speech_* values survive a to_dict/from_dict round-trip."""
        conf = WhisperstreamConfig(
            agent_speech_mode="summary",
            agent_speech_engine="kokoro",
            agent_speech_voice=3,
            agent_speech_kokoro_voice="am_puck",
            agent_speech_speed=1.5,
            agent_speech_volume=0.75,
        )
        loaded = WhisperstreamConfig.from_dict(conf.to_dict())
        self.assertEqual(loaded.agent_speech_mode, "summary")
        self.assertEqual(loaded.agent_speech_engine, "kokoro")
        self.assertEqual(loaded.agent_speech_voice, 3)
        self.assertEqual(loaded.agent_speech_kokoro_voice, "am_puck")
        self.assertEqual(loaded.agent_speech_speed, 1.5)
        self.assertEqual(loaded.agent_speech_volume, 0.75)
        loaded.validate()  # Should not raise

    def test_agent_speech_engine_switch_preserves_each_voice_slot(self):
        """Switching engines never overwrites either model's remembered voice."""
        conf = WhisperstreamConfig(
            agent_speech_engine="supertonic",
            agent_speech_voice=6,
            agent_speech_kokoro_voice="am_michael",
        )

        kokoro_payload = conf.to_dict()
        kokoro_payload["agent_speech_engine"] = "kokoro"
        kokoro = WhisperstreamConfig.from_dict(kokoro_payload)
        self.assertEqual(kokoro.agent_speech_voice, 6)
        self.assertEqual(kokoro.agent_speech_kokoro_voice, "am_michael")

        supertonic_payload = kokoro.to_dict()
        supertonic_payload["agent_speech_engine"] = "supertonic"
        supertonic = WhisperstreamConfig.from_dict(supertonic_payload)
        self.assertEqual(supertonic.agent_speech_voice, 6)
        self.assertEqual(supertonic.agent_speech_kokoro_voice, "am_michael")

    def test_agent_speech_presentation_labels_are_not_config_values(self):
        """Voice aliases never replace the stable synthesis-facing IDs."""
        loaded = WhisperstreamConfig.from_dict(
            {
                "agent_speech_voice": "Ava",
                "agent_speech_kokoro_voice": "Heart",
                "agent_speech_voice_label": "Ava",
                "agent_speech_voice_group": "female",
            }
        )

        self.assertEqual(loaded.agent_speech_voice, 1)
        self.assertIs(type(loaded.agent_speech_voice), int)
        self.assertEqual(loaded.agent_speech_kokoro_voice, "af_heart")
        self.assertIs(type(loaded.agent_speech_kokoro_voice), str)
        serialized = loaded.to_dict()
        self.assertNotIn("agent_speech_voice_label", serialized)
        self.assertNotIn("agent_speech_voice_group", serialized)

    def test_agent_speech_fields_absent_from_dict_use_defaults(self):
        """from_dict({}) (fresh install) uses the platform voice default."""
        for system, expected_engine in (("Windows", "supertonic"), ("Darwin", "auto")):
            with self.subTest(system=system), patch("whisperstream.config_manager.platform.system", return_value=system):
                conf = WhisperstreamConfig.from_dict({})
                self.assertEqual(conf.agent_speech_mode, "off")
                self.assertEqual(conf.agent_speech_engine, expected_engine)
                self.assertEqual(conf.agent_speech_voice, 1)
                self.assertEqual(conf.agent_speech_kokoro_voice, "af_heart")
                self.assertEqual(conf.agent_speech_speed, 1.0)
                self.assertEqual(conf.agent_speech_volume, 1.0)

    def test_legacy_kitten_engine_loads_as_platform_default_without_rewriting_file(self):
        """Legacy Kitten values fall back in memory and leave TOML untouched."""
        for system, expected_engine in (("Windows", "supertonic"), ("Darwin", "auto")):
            with self.subTest(system=system), patch("whisperstream.config_manager.platform.system", return_value=system):
                import tempfile

                warning = (
                    "WARNING:whisperstream.config_manager:"
                    "config legacy agent speech engine fallback "
                    f"engine=kitten fallback={expected_engine}"
                )
                for stored_engine in ("kitten", "KiTtEn", "kittentts"):
                    with self.subTest(stored_engine=stored_engine):
                        with tempfile.TemporaryDirectory() as tmpdir:
                            root = Path(tmpdir)
                            config_path = root / "config.toml"
                            config_path.write_bytes(
                                (
                                    f'agent_speech_engine = "{stored_engine}"\r\n'
                                    "agent_speech_voice = 4\r\n"
                                ).encode("utf-8")
                            )
                            original_bytes = config_path.read_bytes()
                            manager = ConfigManager(
                                config_path=config_path,
                                models_root=root / "models",
                            )

                            with self.assertLogs(
                                "whisperstream.config_manager", level="WARNING"
                            ) as captured:
                                loaded = manager.load()

                            self.assertEqual(loaded.agent_speech_engine, expected_engine)
                            normalized_engine = stored_engine.casefold()
                            self.assertEqual(
                                captured.output,
                                [warning.replace("engine=kitten", f"engine={normalized_engine}")],
                            )
                            self.assertEqual(config_path.read_bytes(), original_bytes)

    def test_supertonic_engine_load_is_canonical_and_warning_free(self):
        """Current engine values normalize case without a legacy warning."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.toml"
            config_path.write_bytes(
                b'agent_speech_engine = "SuPeRtOnIc"\r\n'
            )
            original_bytes = config_path.read_bytes()
            manager = ConfigManager(
                config_path=config_path,
                models_root=root / "models",
            )

            with self.assertNoLogs(
                "whisperstream.config_manager", level="WARNING"
            ):
                loaded = manager.load()

            self.assertEqual(loaded.agent_speech_engine, "supertonic")
            self.assertEqual(config_path.read_bytes(), original_bytes)

    def test_agent_speech_engine_normalizes_all_current_choices(self):
        for raw, expected in (
            (" AUTO ", "auto"),
            ("KoKoRo", "kokoro"),
            ("SuPeRtOnIc", "supertonic"),
        ):
            with self.subTest(raw=raw):
                loaded = WhisperstreamConfig.from_dict(
                    {"agent_speech_engine": raw}
                )
                self.assertEqual(loaded.agent_speech_engine, expected)

    def test_agent_speech_invalid_engine_falls_back_to_platform_default(self):
        for system, expected_engine in (("Windows", "supertonic"), ("Darwin", "auto")):
            with self.subTest(system=system), patch("whisperstream.config_manager.platform.system", return_value=system):
                for raw in ("unknown", "", None, 3):
                    with self.subTest(raw=raw):
                        loaded = WhisperstreamConfig.from_dict(
                            {"agent_speech_engine": raw}
                        )
                        self.assertEqual(loaded.agent_speech_engine, expected_engine)

    def test_kokoro_voice_normalizes_and_invalid_falls_back(self):
        self.assertEqual(
            WhisperstreamConfig.from_dict(
                {"agent_speech_kokoro_voice": " AM_PUCK "}
            ).agent_speech_kokoro_voice,
            "am_puck",
        )
        for raw in ("not-a-voice", "", None, 7):
            with self.subTest(raw=raw):
                loaded = WhisperstreamConfig.from_dict(
                    {"agent_speech_kokoro_voice": raw}
                )
                self.assertEqual(
                    loaded.agent_speech_kokoro_voice,
                    "af_heart",
                )

    def test_supertonic_voice_invalid_load_falls_back(self):
        for raw in (-1, 10, True, "4", None):
            with self.subTest(raw=raw):
                loaded = WhisperstreamConfig.from_dict(
                    {"agent_speech_voice": raw}
                )
                self.assertEqual(loaded.agent_speech_voice, 1)

    def test_direct_agent_speech_values_are_validated(self):
        with self.assertRaisesRegex(ValueError, "agent_speech_engine"):
            WhisperstreamConfig(agent_speech_engine="unknown").validate()
        with self.assertRaisesRegex(ValueError, "agent_speech_voice"):
            WhisperstreamConfig(agent_speech_voice=10).validate()
        with self.assertRaisesRegex(ValueError, "agent_speech_kokoro_voice"):
            WhisperstreamConfig(
                agent_speech_kokoro_voice="not-a-voice"
            ).validate()

    def test_agent_speech_fields_roundtrip_via_config_manager(self):
        """Non-default agent_speech_* values survive a ConfigManager save/load round-trip."""
        import tempfile
        from pathlib import Path
        from whisperstream.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            models_path = Path(tmpdir) / "models"
            mgr = ConfigManager(config_path=config_path, models_root=models_path)

            conf = WhisperstreamConfig(
                agent_speech_mode="verbatim",
                agent_speech_engine="kokoro",
                agent_speech_voice=5,
                agent_speech_kokoro_voice="af_bella",
                agent_speech_speed=0.8,
                agent_speech_volume=0.4,
                agent_speech_content="short",
                agent_speech_short_seconds=17,
            )
            mgr.save(conf)
            loaded = mgr.load()

        self.assertEqual(loaded.agent_speech_mode, "verbatim")
        self.assertEqual(loaded.agent_speech_engine, "kokoro")
        self.assertEqual(loaded.agent_speech_voice, 5)
        self.assertEqual(loaded.agent_speech_kokoro_voice, "af_bella")
        self.assertEqual(loaded.agent_speech_speed, 0.8)
        self.assertEqual(loaded.agent_speech_volume, 0.4)
        self.assertEqual(loaded.agent_speech_content, "short")
        self.assertEqual(loaded.agent_speech_short_seconds, 17)
        loaded.validate()  # Should not raise

    def test_agent_speech_config_missing_keys_seed_defaults(self):
        """A config.toml written before this field existed loads with seed defaults.

        Simulates an upgrading user: the dict passed to from_dict has none of
        the agent_speech_* keys, mirroring a pre-existing config.toml on disk.
        """
        for system, expected_engine in (("Windows", "supertonic"), ("Darwin", "auto")):
            with self.subTest(system=system), patch("whisperstream.config_manager.platform.system", return_value=system):
                conf = WhisperstreamConfig.from_dict({"claude_hud_enabled": True})
                self.assertEqual(conf.agent_speech_mode, "off")
                self.assertEqual(conf.agent_speech_engine, expected_engine)
                self.assertEqual(conf.agent_speech_voice, 1)
                self.assertEqual(conf.agent_speech_kokoro_voice, "af_heart")
                self.assertEqual(conf.agent_speech_speed, 1.0)
                self.assertEqual(conf.agent_speech_volume, 1.0)
                conf.validate()  # Should not raise

            # --- Claude HUD Complete Task 1.1: normalized agent contracts ---

    def test_agent_contract_defaults_match_locked_scope(self):
        conf = WhisperstreamConfig()

        self.assertFalse(conf.codex_hud_enabled)
        self.assertFalse(conf.agent_speech_enabled)
        self.assertFalse(conf.agent_summary_text_enabled)
        self.assertEqual(conf.agent_speech_activation, "always_on")
        self.assertEqual(conf.agent_speech_summary_source, "agent")
        self.assertEqual(conf.claude_reply_grace_seconds, 6)
        self.assertEqual(conf.claude_reply_queue_delay_seconds, 2)
        self.assertFalse(conf.claude_reply_auto_submit)
        self.assertFalse(conf.claude_permission_decisions_enabled)

    def test_agent_contract_roundtrips_via_config_manager(self):
        import tempfile
        from pathlib import Path
        from whisperstream.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ConfigManager(
                config_path=Path(tmpdir) / "config.toml",
                models_root=Path(tmpdir) / "models",
            )
            manager.save(
                WhisperstreamConfig(
                    codex_hud_enabled=True,
                    agent_speech_enabled=True,
                    agent_summary_text_enabled=True,
                    agent_speech_activation="always_on",
                    agent_speech_summary_source="agent",
                    claude_reply_grace_seconds=20,
                    claude_reply_queue_delay_seconds=4,
                    claude_reply_auto_submit=True,
                    claude_permission_decisions_enabled=True,
                )
            )
            loaded = manager.load()

        self.assertTrue(loaded.codex_hud_enabled)
        self.assertTrue(loaded.agent_speech_enabled)
        self.assertTrue(loaded.agent_summary_text_enabled)
        self.assertEqual(loaded.agent_speech_activation, "always_on")
        self.assertEqual(loaded.agent_speech_summary_source, "agent")
        self.assertEqual(loaded.claude_reply_grace_seconds, 20)
        self.assertEqual(loaded.claude_reply_queue_delay_seconds, 4)
        self.assertTrue(loaded.claude_reply_auto_submit)
        self.assertTrue(loaded.claude_permission_decisions_enabled)

    def test_legacy_reply_parking_migrates_to_numeric_grace(self):
        enabled = WhisperstreamConfig.from_dict(
            {"claude_reply_parking_enabled": True}
        )
        disabled = WhisperstreamConfig.from_dict(
            {"claude_reply_parking_enabled": False}
        )
        explicit = WhisperstreamConfig.from_dict(
            {
                "claude_reply_parking_enabled": False,
                "claude_reply_grace_seconds": 9,
            }
        )

        self.assertEqual(enabled.claude_reply_grace_seconds, 6)
        self.assertEqual(disabled.claude_reply_grace_seconds, 0)
        self.assertEqual(explicit.claude_reply_grace_seconds, 9)
        self.assertNotIn("claude_reply_parking_enabled", enabled.to_dict())

    def test_legacy_agent_speech_mode_loads_into_safe_master_toggle(self):
        cases = (
            ("off", False),
            ("verbatim", True),
            ("summary", True),
            ("conversational", True),
            ("unknown", False),
        )
        for legacy_mode, expected_enabled in cases:
            with self.subTest(legacy_mode=legacy_mode):
                conf = WhisperstreamConfig.from_dict(
                    {"agent_speech_mode": legacy_mode}
                )

                self.assertEqual(conf.agent_speech_mode, legacy_mode)
                self.assertIs(conf.agent_speech_enabled, expected_enabled)
                self.assertEqual(conf.agent_speech_activation, "always_on")
                self.assertEqual(conf.agent_speech_summary_source, "agent")

    def test_explicit_new_agent_speech_toggle_wins_over_legacy_mode(self):
        conf = WhisperstreamConfig.from_dict(
            {
                "agent_speech_mode": "off",
                "agent_speech_enabled": True,
                "agent_speech_activation": "per_session",
            }
        )

        self.assertTrue(conf.agent_speech_enabled)
        self.assertEqual(conf.agent_speech_mode, "off")
        self.assertEqual(conf.agent_speech_activation, "per_session")

    def test_invalid_agent_contract_values_normalize_safely(self):
        cases = (
            ("agent_speech_activation", "global", "always_on"),
            ("agent_speech_activation", None, "always_on"),
            ("agent_speech_summary_source", "local", "agent"),
            ("agent_speech_summary_source", 7, "agent"),
        )
        for field, bad_value, expected in cases:
            with self.subTest(field=field, bad_value=bad_value):
                conf = WhisperstreamConfig.from_dict({field: bad_value})
                self.assertEqual(getattr(conf, field), expected)

    def test_invalid_agent_boolean_values_normalize_safely(self):
        cases = (
            ("agent_speech_enabled", False),
            ("agent_summary_text_enabled", False),
            ("claude_reply_auto_submit", False),
            ("claude_permission_decisions_enabled", False),
        )
        for field, default in cases:
            with self.subTest(field=field):
                conf = WhisperstreamConfig.from_dict({field: "not-a-boolean"})
                self.assertIs(getattr(conf, field), default)

    def test_reply_grace_normalizes_invalid_values_and_accepts_boundaries(self):
        for value in (-1, 31, 6.0, True, "6", None):
            with self.subTest(value=value):
                conf = WhisperstreamConfig.from_dict(
                    {"claude_reply_grace_seconds": value}
                )
                self.assertEqual(conf.claude_reply_grace_seconds, 6)

        self.assertEqual(
            WhisperstreamConfig.from_dict(
                {"claude_reply_grace_seconds": 0}
            ).claude_reply_grace_seconds,
            0,
        )
        self.assertEqual(
            WhisperstreamConfig.from_dict(
                {"claude_reply_grace_seconds": 20}
            ).claude_reply_grace_seconds,
            20,
        )
        self.assertEqual(
            WhisperstreamConfig.from_dict(
                {"claude_reply_grace_seconds": 30}
            ).claude_reply_grace_seconds,
            30,
        )

    def test_reply_queue_delay_normalizes_invalid_values_and_accepts_boundaries(self):
        for value in (-1, 11, 2.0, True, "2", None):
            with self.subTest(value=value):
                conf = WhisperstreamConfig.from_dict(
                    {"claude_reply_queue_delay_seconds": value}
                )
                self.assertEqual(conf.claude_reply_queue_delay_seconds, 2)

        self.assertEqual(
            WhisperstreamConfig.from_dict(
                {"claude_reply_queue_delay_seconds": 0}
            ).claude_reply_queue_delay_seconds,
            0,
        )
        self.assertEqual(
            WhisperstreamConfig.from_dict(
                {"claude_reply_queue_delay_seconds": 10}
            ).claude_reply_queue_delay_seconds,
            10,
        )

    def test_direct_invalid_agent_enums_fail_validation(self):
        with self.assertRaisesRegex(ValueError, "agent_speech_activation"):
            WhisperstreamConfig(agent_speech_activation="global").validate()
        with self.assertRaisesRegex(ValueError, "agent_speech_summary_source"):
            WhisperstreamConfig(agent_speech_summary_source="local").validate()
        with self.assertRaisesRegex(ValueError, "claude_reply_grace_seconds"):
            WhisperstreamConfig(claude_reply_grace_seconds=31).validate()
        WhisperstreamConfig(claude_reply_grace_seconds=20).validate()
        WhisperstreamConfig(claude_reply_grace_seconds=30).validate()
        with self.assertRaisesRegex(
            ValueError, "agent_summary_text_enabled"
        ):
            WhisperstreamConfig(
                agent_summary_text_enabled="yes"
            ).validate()
        with self.assertRaisesRegex(
            ValueError, "claude_reply_queue_delay_seconds"
        ):
            WhisperstreamConfig(
                claude_reply_queue_delay_seconds=11
            ).validate()
        with self.assertRaisesRegex(
            ValueError, "claude_reply_auto_submit"
        ):
            WhisperstreamConfig(
                claude_reply_auto_submit="yes"
            ).validate()

    def test_removed_agent_console_shortcut_is_not_loaded_or_serialized(self):
        conf = WhisperstreamConfig.from_dict(
            {"agent_console_shortcut": "ctrl+shift+\\"}
        )

        self.assertFalse(hasattr(conf, "agent_console_shortcut"))
        self.assertNotIn("agent_console_shortcut", conf.to_dict())

    def test_removed_codex_toggle_is_not_loaded_or_serialized(self):
        conf = WhisperstreamConfig.from_dict({"codex_hooks_enabled": True})

        self.assertFalse(hasattr(conf, "codex_hooks_enabled"))
        self.assertNotIn("codex_hooks_enabled", conf.to_dict())

    def test_all_recording_shortcut_slots_are_reserved_from_profile_hotkeys(self):
        profiles = [
            {"id": "ptt", "name": "PTT", "prompt": "p"},
            {"id": "toggle", "name": "Toggle", "prompt": "t"},
        ]
        conf = WhisperstreamConfig(
            profiles=profiles,
            default_profile_id="ptt",
            push_to_talk_shortcut="ctrl+shift+1",
            toggle_shortcut="ctrl+shift+2",
            profile_hotkeys={
                "ptt": "CTRL+SHIFT+1",
                "toggle": "ctrl+shift+2",
            },
        )

        conf.validate()

        self.assertEqual(conf.profile_hotkeys, {})

    def test_claude_view_shortcut_defaults_and_normalizes(self):
        self.assertEqual(
            WhisperstreamConfig().claude_view_shortcut,
            "ctrl+alt+`",
        )
        self.assertEqual(
            WhisperstreamConfig.from_dict({}).claude_view_shortcut,
            "ctrl+alt+`",
        )
        self.assertEqual(
            WhisperstreamConfig.from_dict(
                {"claude_view_shortcut": "CTRL+SHIFT+V"}
            ).claude_view_shortcut,
            "ctrl+shift+v",
        )

    def test_claude_view_shortcut_is_clearable(self):
        conf = WhisperstreamConfig(claude_view_shortcut="")

        conf.validate()

        self.assertEqual(conf.claude_view_shortcut, "")

    def test_claude_view_shortcut_clears_on_action_conflict(self):
        conf = WhisperstreamConfig(
            claude_view_shortcut="CTRL+SHIFT+V",
            toggle_shortcut="ctrl+shift+v",
        )

        conf.validate()

        self.assertEqual(conf.claude_view_shortcut, "")

    def test_claude_view_shortcut_clears_on_profile_conflict(self):
        conf = WhisperstreamConfig(
            claude_view_shortcut="ctrl+shift+v",
            profile_hotkeys={"profile-1": "CTRL+SHIFT+V"},
        )

        conf.validate()

        self.assertEqual(conf.claude_view_shortcut, "")

    # --- Enhancement output guard rollout mode ---

    def test_enhancement_output_guard_mode_dataclass_and_existing_file_default_off(self):
        """Direct construction and an old config missing the key stay off."""
        self.assertEqual(
            WhisperstreamConfig().enhancement_output_guard_mode,
            "off",
        )
        self.assertEqual(
            WhisperstreamConfig.from_dict(
                {"enhancement_enabled": True}
            ).enhancement_output_guard_mode,
            "off",
        )

    def test_enhancement_output_guard_mode_normalizes_and_round_trips(self):
        """All rollout values normalize and survive the scalar config path."""
        for mode in ("enforce", "log", "off"):
            conf = WhisperstreamConfig.from_dict(
                {"enhancement_output_guard_mode": mode.upper()}
            )
            self.assertEqual(conf.enhancement_output_guard_mode, mode)
            conf.validate()
            self.assertEqual(
                WhisperstreamConfig.from_dict(
                    conf.to_dict()
                ).enhancement_output_guard_mode,
                mode,
            )

    def test_enhancement_output_guard_mode_rejects_invalid_value(self):
        conf = WhisperstreamConfig(enhancement_output_guard_mode="sometimes")
        with self.assertRaisesRegex(ValueError, "enhancement_output_guard_mode"):
            conf.validate()

    def test_new_install_and_existing_file_default_legacy_guard_mode_off(self):
        """New installs no longer enable the retired semantic guard."""
        import tempfile
        from pathlib import Path

        from whisperstream.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            new_manager = ConfigManager(
                config_path=root / "new.toml",
                models_root=root / "new-models",
            )
            self.assertEqual(
                new_manager.load().enhancement_output_guard_mode,
                "off",
            )

            existing_path = root / "existing.toml"
            existing_path.write_text(
                "enhancement_enabled = false\n",
                encoding="utf-8",
            )
            existing_manager = ConfigManager(
                config_path=existing_path,
                models_root=root / "existing-models",
            )
            self.assertEqual(
                existing_manager.load().enhancement_output_guard_mode,
                "off",
            )

    def test_config_manager_round_trips_each_output_guard_mode(self):
        import tempfile
        from pathlib import Path

        from whisperstream.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manager = ConfigManager(
                config_path=root / "config.toml",
                models_root=root / "models",
            )
            for mode in ("enforce", "log", "off"):
                manager.save(
                    WhisperstreamConfig(enhancement_output_guard_mode=mode)
                )
                self.assertEqual(
                    manager.load().enhancement_output_guard_mode,
                    mode,
                )


def _configure_candidate_overlay(tmp_path, monkeypatch) -> None:
    pytest.importorskip("whisperstream.dev_candidate_models")  # omitted from source releases
    registry = tmp_path / "candidate-models.json"
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()
    registry.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "candidates": [
                    {
                        "id": "Candidate-Exact",
                        "label": "Candidate-Exact",
                        "artifact_filename": "candidate-exact.gguf",
                        "parameter_size": "350M",
                        "role": "Review candidate",
                        "status": "Evaluation candidate",
                        "serving_contract": "wrap-v2",
                        "trained_modes": ["chat", "code"],
                        "prompt_key": "stream",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "WHISPERSTREAM_DEV_CANDIDATE_REGISTRY", str(registry)
    )
    monkeypatch.setenv("WHISPERSTREAM_DEV_CANDIDATE_DIR", str(candidate_dir))


def test_config_accepts_enabled_candidate_and_retains_canonical_exact_id(
    tmp_path, monkeypatch
):
    _configure_candidate_overlay(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "whisperstream._build_flags.DEV_TOOLS_AVAILABLE", True
    )

    config = WhisperstreamConfig.from_dict(
        {"enhancement_local_model": "candidate-exact"}
    )

    assert config.enhancement_local_model == "Candidate-Exact"


def test_config_rejects_candidate_when_production_gate_is_closed(
    tmp_path, monkeypatch
):
    _configure_candidate_overlay(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "whisperstream._build_flags.DEV_TOOLS_AVAILABLE", False
    )

    config = WhisperstreamConfig.from_dict(
        {"enhancement_local_model": "Candidate-Exact"}
    )

    assert config.enhancement_local_model == "stream-1-mini"


def test_config_round_trips_every_tracked_candidate_id(
    tmp_path, monkeypatch
):
    pytest.importorskip("whisperstream.dev_candidate_models")  # omitted from source releases
    tracked_registry = (
        Path(__file__).parents[1] / "dev" / "candidate-models.json"
    )
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()
    monkeypatch.setattr(
        "whisperstream._build_flags.DEV_TOOLS_AVAILABLE", True
    )
    monkeypatch.setenv(
        "WHISPERSTREAM_DEV_CANDIDATE_REGISTRY",
        str(tracked_registry),
    )
    monkeypatch.setenv(
        "WHISPERSTREAM_DEV_CANDIDATE_DIR",
        str(candidate_dir),
    )
    candidate_ids = [
        row["id"]
        for row in json.loads(
            tracked_registry.read_text(encoding="utf-8")
        )["candidates"]
    ]
    manager = ConfigManager(
        config_path=tmp_path / "config.toml",
        models_root=tmp_path / "models",
    )

    for candidate_id in candidate_ids:
        manager.save(
            WhisperstreamConfig(enhancement_local_model=candidate_id)
        )
        assert manager.load().enhancement_local_model == candidate_id


if __name__ == "__main__":
    unittest.main()
