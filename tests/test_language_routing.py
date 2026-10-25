import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from whisperstream.config_manager import (
    SUPPORTED_LANGUAGES,
    ConfigManager,
    WhisperstreamConfig,
)
from whisperstream.language_routing import (
    COHERE_ARTIFACT,
    COHERE_LANGUAGES,
    PARAKEET_LANGUAGES,
    PARAKEET_V2_MODEL,
    PARAKEET_V3_MODEL,
    QWEN_ARTIFACT,
    QWEN_LANGUAGES,
    artifact_supports_language,
    configured_artifact,
    language_to_backend,
    models_for_language,
    parakeet_variant,
    recommended_artifact,
)

# Classification of each language by which engine supports it.
DUAL = {"en", "es", "fr", "de", "it", "pt", "nl", "ru", "pl", "cs",
        "da", "fi", "el", "hu", "ro", "sv"}
PARAKEET_ONLY = {"bg", "hr", "et", "lv", "lt", "mt", "sk", "sl", "uk"}
QWEN_ONLY = {"zh", "yue", "ja", "ko", "hi", "th", "vi", "id", "ms",
             "fil", "ar", "fa", "tr", "mk"}


class TestLanguageSets(unittest.TestCase):
    def test_classification_counts(self):
        """16 dual / 9 Parakeet-only / 14 Qwen-only / 39 union (locked table)."""
        self.assertEqual(len(DUAL), 16)
        self.assertEqual(len(PARAKEET_ONLY), 9)
        self.assertEqual(len(QWEN_ONLY), 14)
        self.assertEqual(len(PARAKEET_LANGUAGES), 25)
        self.assertEqual(len(QWEN_LANGUAGES), 30)
        self.assertEqual(len(COHERE_LANGUAGES), 14)

    def test_union_is_39_and_matches_supported_languages(self):
        """The 39-language union must equal config_manager.SUPPORTED_LANGUAGES."""
        union = PARAKEET_LANGUAGES | QWEN_LANGUAGES
        self.assertEqual(len(union), 39)
        self.assertEqual(union, set(SUPPORTED_LANGUAGES))

    def test_classification_partitions_the_union(self):
        """DUAL / PARAKEET_ONLY / QWEN_ONLY partition the 39-language union."""
        self.assertEqual(PARAKEET_LANGUAGES & QWEN_LANGUAGES, DUAL)
        self.assertEqual(PARAKEET_LANGUAGES - QWEN_LANGUAGES, PARAKEET_ONLY)
        self.assertEqual(QWEN_LANGUAGES - PARAKEET_LANGUAGES, QWEN_ONLY)


class TestModelsForLanguage(unittest.TestCase):
    def test_dual_support_lists_parakeet_first_then_qwen(self):
        for code in DUAL:
            expected = ["parakeet", "qwen"]
            if code in COHERE_LANGUAGES:
                expected.append("cohere")
            self.assertEqual(
                models_for_language(code), expected, msg=f"code={code}"
            )

    def test_parakeet_only(self):
        for code in PARAKEET_ONLY:
            self.assertEqual(
                models_for_language(code), ["parakeet"], msg=f"code={code}"
            )

    def test_qwen_only(self):
        for code in QWEN_ONLY:
            expected = ["qwen"]
            if code in COHERE_LANGUAGES:
                expected.append("cohere")
            self.assertEqual(
                models_for_language(code), expected, msg=f"code={code}"
            )

    def test_unknown_and_empty_return_empty(self):
        for code in ("xx", "", None, "   "):
            self.assertEqual(models_for_language(code), [], msg=f"code={code!r}")

    def test_case_insensitive(self):
        self.assertEqual(models_for_language("EN"), ["parakeet", "qwen", "cohere"])
        self.assertEqual(models_for_language("ZH"), ["qwen", "cohere"])
        self.assertEqual(models_for_language(" Ja "), ["qwen", "cohere"])


class TestLanguageToBackend(unittest.TestCase):
    def test_dual_support_defaults_to_parakeet(self):
        for code in DUAL:
            self.assertEqual(language_to_backend(code), "parakeet", msg=f"code={code}")

    def test_parakeet_only_routes_to_parakeet(self):
        for code in PARAKEET_ONLY:
            self.assertEqual(language_to_backend(code), "parakeet", msg=f"code={code}")

    def test_qwen_only_routes_to_qwen(self):
        for code in QWEN_ONLY:
            self.assertEqual(language_to_backend(code), "qwen", msg=f"code={code}")

    def test_unknown_and_empty_default_to_parakeet(self):
        for code in ("xx", "", None, "   "):
            self.assertEqual(
                language_to_backend(code), "parakeet", msg=f"code={code!r}"
            )

    def test_case_insensitive(self):
        self.assertEqual(language_to_backend("ZH"), "qwen")
        self.assertEqual(language_to_backend("EN"), "parakeet")


class TestParakeetVariant(unittest.TestCase):
    def test_english_maps_to_v2(self):
        self.assertEqual(parakeet_variant("en"), PARAKEET_V2_MODEL)
        self.assertEqual(parakeet_variant("EN"), PARAKEET_V2_MODEL)

    def test_other_languages_map_to_v3(self):
        for code in ("es", "fr", "de", "uk", "mt", "zh", "ja"):
            self.assertEqual(parakeet_variant(code), PARAKEET_V3_MODEL, msg=f"code={code}")

    def test_unknown_and_empty_map_to_v3(self):
        for code in ("xx", "", None):
            self.assertEqual(parakeet_variant(code), PARAKEET_V3_MODEL, msg=f"code={code!r}")


class TestExactArtifactRouting(unittest.TestCase):
    def test_recommended_artifact_resolves_exact_language_target(self):
        self.assertEqual(recommended_artifact("en"), PARAKEET_V2_MODEL)
        self.assertEqual(recommended_artifact("fr"), PARAKEET_V3_MODEL)
        self.assertEqual(recommended_artifact("ja"), QWEN_ARTIFACT)
        self.assertEqual(recommended_artifact(None), PARAKEET_V3_MODEL)

    def test_configured_artifact_keeps_exact_parakeet_variant(self):
        self.assertEqual(
            configured_artifact("parakeet", PARAKEET_V2_MODEL),
            PARAKEET_V2_MODEL,
        )
        self.assertEqual(
            configured_artifact("parakeet", PARAKEET_V3_MODEL),
            PARAKEET_V3_MODEL,
        )
        self.assertEqual(configured_artifact("qwen", PARAKEET_V2_MODEL), QWEN_ARTIFACT)
        self.assertEqual(
            configured_artifact("cohere", PARAKEET_V2_MODEL), COHERE_ARTIFACT
        )

    def test_artifact_compatibility_is_exact(self):
        self.assertTrue(artifact_supports_language(PARAKEET_V2_MODEL, "en"))
        self.assertFalse(artifact_supports_language(PARAKEET_V2_MODEL, "fr"))
        self.assertTrue(artifact_supports_language(PARAKEET_V3_MODEL, "en"))
        self.assertTrue(artifact_supports_language(PARAKEET_V3_MODEL, "fr"))
        self.assertTrue(artifact_supports_language(QWEN_ARTIFACT, "ja"))
        self.assertFalse(artifact_supports_language(QWEN_ARTIFACT, "uk"))
        self.assertTrue(artifact_supports_language(COHERE_ARTIFACT, "en"))
        self.assertTrue(artifact_supports_language(COHERE_ARTIFACT, "zh"))
        self.assertFalse(artifact_supports_language(COHERE_ARTIFACT, "ru"))

    def test_automatic_recommendations_never_select_cohere(self):
        for code in SUPPORTED_LANGUAGES:
            self.assertNotEqual(recommended_artifact(code), COHERE_ARTIFACT)
            self.assertNotEqual(language_to_backend(code), "cohere")


class TestSelectionModeMigration(unittest.TestCase):
    def test_missing_mode_matching_recommendation_infers_automatic(self):
        config = WhisperstreamConfig.from_dict(
            {
                "language": "fr",
                "stt_backend": "parakeet",
                "parakeet_model": PARAKEET_V3_MODEL,
            }
        )
        self.assertEqual(config.stt_model_selection_mode, "automatic")
        self.assertEqual(config.stt_backend, "parakeet")
        self.assertEqual(config.parakeet_model, PARAKEET_V3_MODEL)

    def test_missing_mode_preserves_compatible_nonrecommended_artifact(self):
        config = WhisperstreamConfig.from_dict(
            {
                "language": "en",
                "stt_backend": "parakeet",
                "parakeet_model": PARAKEET_V3_MODEL,
            }
        )
        self.assertEqual(config.stt_model_selection_mode, "manual")
        self.assertEqual(config.stt_backend, "parakeet")
        self.assertEqual(config.parakeet_model, PARAKEET_V3_MODEL)

    def test_missing_mode_preserves_existing_qwen_override(self):
        config = WhisperstreamConfig.from_dict(
            {
                "language": "fr",
                "stt_backend": "qwen",
                "qwen_force_language": True,
            }
        )
        self.assertEqual(config.stt_model_selection_mode, "manual")
        self.assertEqual(config.stt_backend, "qwen")
        self.assertTrue(config.qwen_force_language)

    def test_missing_mode_recommended_qwen_without_force_stays_automatic(self):
        config = WhisperstreamConfig.from_dict(
            {
                "language": "ja",
                "stt_backend": "qwen",
                "qwen_force_language": False,
            }
        )
        self.assertEqual(config.stt_model_selection_mode, "automatic")
        self.assertEqual(config.stt_backend, "qwen")
        self.assertFalse(config.qwen_force_language)

    def test_explicit_automatic_resolves_recommendation_and_clears_force(self):
        config = WhisperstreamConfig.from_dict(
            {
                "language": "en",
                "stt_model_selection_mode": "automatic",
                "stt_backend": "qwen",
                "parakeet_model": PARAKEET_V3_MODEL,
                "qwen_force_language": True,
            }
        )
        self.assertEqual(config.stt_model_selection_mode, "automatic")
        self.assertEqual(config.stt_backend, "parakeet")
        self.assertEqual(config.parakeet_model, PARAKEET_V2_MODEL)
        self.assertFalse(config.qwen_force_language)

    def test_manual_preserves_compatible_exact_artifact_and_qwen_force(self):
        config = WhisperstreamConfig.from_dict(
            {
                "language": "en",
                "stt_model_selection_mode": "manual",
                "stt_backend": "qwen",
                "parakeet_model": PARAKEET_V3_MODEL,
                "qwen_force_language": True,
            }
        )
        self.assertEqual(config.stt_model_selection_mode, "manual")
        self.assertEqual(config.stt_backend, "qwen")
        self.assertEqual(config.parakeet_model, PARAKEET_V3_MODEL)
        self.assertTrue(config.qwen_force_language)

    def test_manual_preserves_compatible_cohere_and_clears_qwen_force(self):
        config = WhisperstreamConfig.from_dict(
            {
                "language": "en",
                "stt_model_selection_mode": "manual",
                "stt_backend": "cohere",
                "qwen_force_language": True,
            }
        )
        self.assertEqual(config.stt_model_selection_mode, "manual")
        self.assertEqual(config.stt_backend, "cohere")
        self.assertFalse(config.qwen_force_language)

    def test_incompatible_manual_cohere_returns_to_automatic(self):
        config = WhisperstreamConfig.from_dict(
            {
                "language": "ru",
                "stt_model_selection_mode": "manual",
                "stt_backend": "cohere",
            }
        )
        self.assertEqual(config.stt_model_selection_mode, "automatic")
        self.assertEqual(config.stt_backend, "parakeet")

    def test_incompatible_manual_artifact_falls_back_to_automatic(self):
        config = WhisperstreamConfig.from_dict(
            {
                "language": "fr",
                "stt_model_selection_mode": "manual",
                "stt_backend": "parakeet",
                "parakeet_model": PARAKEET_V2_MODEL,
                "qwen_force_language": True,
            }
        )
        self.assertEqual(config.stt_model_selection_mode, "automatic")
        self.assertEqual(config.stt_backend, "parakeet")
        self.assertEqual(config.parakeet_model, PARAKEET_V3_MODEL)
        self.assertFalse(config.qwen_force_language)

    def test_invalid_mode_normalizes_to_automatic(self):
        config = WhisperstreamConfig.from_dict(
            {
                "language": "ja",
                "stt_model_selection_mode": "sometimes",
                "stt_backend": "parakeet",
            }
        )
        self.assertEqual(config.stt_model_selection_mode, "automatic")
        self.assertEqual(config.stt_backend, "qwen")

    def test_selection_mode_persists_through_toml_round_trip(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manager = ConfigManager(
                config_path=root / "config.toml",
                models_root=root / "models",
            )
            manager.save(
                WhisperstreamConfig(
                    language="en",
                    stt_model_selection_mode="manual",
                    stt_backend="parakeet",
                    parakeet_model=PARAKEET_V3_MODEL,
                )
            )
            reloaded = manager.load()

        self.assertEqual(reloaded.stt_model_selection_mode, "manual")
        self.assertEqual(reloaded.parakeet_model, PARAKEET_V3_MODEL)


if __name__ == "__main__":
    unittest.main()
