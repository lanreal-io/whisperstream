"""Tests for the STT backend factory (`make_stt_manager`).

The factory selects between ParakeetManager and QwenManager based on
``config.stt_backend`` with an optional ``WHISPERSTREAM_STT_BACKEND`` env
override (env wins when set and non-empty). Unknown / empty values fall back
to Parakeet (default-fallback per research C1).

Both manager classes are mocked so no model ever loads.
"""
import logging
import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from whisperstream.config_manager import ConfigManager, WhisperstreamConfig
from whisperstream.model_downloader import ModelId
from whisperstream.stt_factory import make_stt_manager
from whisperstream.stt_contracts import SttEngine


# Sentinels distinguishing which manager (and which model_dir) the factory built.
PARAKEET_MODEL_DIR = Path("/tmp/parakeet-model-dir")
QWEN_MODEL_DIR = Path("/tmp/models/qwen3-asr-0.6b-int8")
COHERE_MODEL_DIR = Path("/tmp/models/cohere-transcribe-03-2026-int8")


def _fake_model_dir(model_id):
    """Stand-in for model_downloader.model_dir: sentinel per ModelId family."""
    if model_id is ModelId.QWEN3_ASR_06B_INT8:
        return QWEN_MODEL_DIR
    if model_id is ModelId.COHERE_TRANSCRIBE_INT8:
        return COHERE_MODEL_DIR
    return PARAKEET_MODEL_DIR


class TestMakeSttManager(unittest.TestCase):
    def setUp(self):
        self.logger = MagicMock(spec=logging.Logger)
        # A config with known fields so we can assert how the factory derives kwargs.
        self.config = WhisperstreamConfig(
            language="en",
            stt_model_selection_mode="manual",
            stt_backend="parakeet",
            parakeet_model="nemo-parakeet-tdt-0.6b-v3",
            parakeet_quantization="int8",
            threads=3,
            model_timeout=222.0,
            hotword_score=2.5,
            beam_width=7,
            word_boosts=["foo", "bar"],
        )
        # config_manager is still passed for signature stability but the factory
        # no longer resolves dirs through it (the registry owns destination now).
        self.config_manager = MagicMock(spec=ConfigManager)

    def _make(self, **overrides):
        """Build a manager with both backend classes + the registry resolver patched.

        ``is_present`` is patched to ``True`` so the construction-time presence
        gate never trips: these tests pin backend *selection*, not presence.
        (A dedicated test asserts the gate raises when the model is absent.)
        """
        mock_qwen = MagicMock(name="QwenManager")
        with patch("whisperstream.stt_factory.ParakeetManager") as mock_parakeet, patch(
            "whisperstream.stt_factory._qwen_manager_class",
            return_value=mock_qwen,
        ), patch(
            "whisperstream.stt_factory.sys.platform", "win32"
        ), patch(
            "whisperstream.stt_factory.CohereManager"
        ) as mock_cohere, patch(
            "whisperstream.stt_factory.model_dir", side_effect=_fake_model_dir
        ), patch(
            "whisperstream.stt_factory.is_present", return_value=True
        ):
            mgr = make_stt_manager(
                self.config,
                logger=self.logger,
                config_manager=self.config_manager,
                **overrides,
            )
        self.mock_cohere = mock_cohere
        return mgr, mock_parakeet, mock_qwen

    # ------------------------------------------------------------------
    # Selection truth-table: config value
    # ------------------------------------------------------------------

    def test_factory_declares_portable_engine_contract(self):
        annotation = inspect.get_annotations(
            make_stt_manager, eval_str=False
        )["return"]
        self.assertEqual(annotation, SttEngine.__name__)

    def test_config_qwen_builds_qwen(self):
        self.config.stt_backend = "qwen"
        mgr, mock_parakeet, mock_qwen = self._make()
        mock_qwen.assert_called_once()
        mock_parakeet.assert_not_called()
        self.assertIs(mgr, mock_qwen.return_value)

    def test_config_parakeet_builds_parakeet(self):
        self.config.stt_backend = "parakeet"
        mgr, mock_parakeet, mock_qwen = self._make()
        mock_parakeet.assert_called_once()
        mock_qwen.assert_not_called()
        self.assertIs(mgr, mock_parakeet.return_value)

    def test_config_cohere_builds_cohere(self):
        self.config.stt_backend = "cohere"
        mgr, mock_parakeet, mock_qwen = self._make()
        self.mock_cohere.assert_called_once()
        mock_parakeet.assert_not_called()
        mock_qwen.assert_not_called()
        self.assertIs(mgr, self.mock_cohere.return_value)

    def test_config_empty_falls_back_to_parakeet(self):
        self.config.stt_backend = ""
        _mgr, mock_parakeet, mock_qwen = self._make()
        mock_parakeet.assert_called_once()
        mock_qwen.assert_not_called()

    def test_config_garbage_falls_back_to_parakeet(self):
        self.config.stt_backend = "garbage"
        _mgr, mock_parakeet, mock_qwen = self._make()
        mock_parakeet.assert_called_once()
        mock_qwen.assert_not_called()

    # ------------------------------------------------------------------
    # Case-insensitivity
    # ------------------------------------------------------------------

    def test_config_qwen_is_case_insensitive(self):
        self.config.stt_backend = "QWEN"
        _mgr, mock_parakeet, mock_qwen = self._make()
        mock_qwen.assert_called_once()
        mock_parakeet.assert_not_called()

    def test_config_parakeet_is_case_insensitive(self):
        self.config.stt_backend = "Parakeet"
        _mgr, mock_parakeet, mock_qwen = self._make()
        mock_parakeet.assert_called_once()
        mock_qwen.assert_not_called()

    # ------------------------------------------------------------------
    # Env override: WHISPERSTREAM_STT_BACKEND wins when set + non-empty
    # ------------------------------------------------------------------

    def test_env_qwen_overrides_parakeet_config(self):
        self.config.stt_backend = "parakeet"
        with patch.dict("os.environ", {"WHISPERSTREAM_STT_BACKEND": "qwen"}):
            _mgr, mock_parakeet, mock_qwen = self._make()
        mock_qwen.assert_called_once()
        mock_parakeet.assert_not_called()

    def test_env_parakeet_overrides_qwen_config(self):
        self.config.stt_backend = "qwen"
        with patch.dict("os.environ", {"WHISPERSTREAM_STT_BACKEND": "parakeet"}):
            _mgr, mock_parakeet, mock_qwen = self._make()
        mock_parakeet.assert_called_once()
        mock_qwen.assert_not_called()

    def test_env_is_case_insensitive(self):
        self.config.stt_backend = "parakeet"
        with patch.dict("os.environ", {"WHISPERSTREAM_STT_BACKEND": "QwEn"}):
            _mgr, mock_parakeet, mock_qwen = self._make()
        mock_qwen.assert_called_once()
        mock_parakeet.assert_not_called()

    def test_empty_env_does_not_override_config(self):
        # Empty / whitespace env must NOT win — config value is used.
        self.config.stt_backend = "qwen"
        with patch.dict("os.environ", {"WHISPERSTREAM_STT_BACKEND": "   "}):
            _mgr, mock_parakeet, mock_qwen = self._make()
        mock_qwen.assert_called_once()
        mock_parakeet.assert_not_called()

    def test_unset_env_falls_back_to_config_value(self):
        self.config.stt_backend = "qwen"
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("WHISPERSTREAM_STT_BACKEND", None)
            _mgr, mock_parakeet, mock_qwen = self._make()
        mock_qwen.assert_called_once()
        mock_parakeet.assert_not_called()

    # ------------------------------------------------------------------
    # Construction kwargs: factory reproduces the existing call sites
    # ------------------------------------------------------------------

    def test_parakeet_constructed_with_call_site_kwargs(self):
        self.config.stt_backend = "parakeet"
        _mgr, mock_parakeet, _mock_qwen = self._make()
        kwargs = mock_parakeet.call_args.kwargs
        self.assertEqual(kwargs["model_name"], self.config.parakeet_model)
        self.assertEqual(kwargs["quantization"], self.config.parakeet_quantization)
        self.assertEqual(kwargs["threads"], self.config.threads)
        self.assertIs(kwargs["logger"], self.logger)
        self.assertEqual(kwargs["model_dir"], PARAKEET_MODEL_DIR)
        self.assertEqual(kwargs["timeout"], self.config.model_timeout)
        self.assertEqual(kwargs["hotwords"], list(self.config.word_boosts))
        self.assertEqual(kwargs["hotwords_score"], self.config.hotword_score)
        self.assertEqual(kwargs["beam_width"], self.config.beam_width)
        # Parakeet model_dir comes from the unified registry resolver, keyed by
        # the v2/v3 ModelId — NOT the old config_manager.model_dir(name, quant),
        # which no longer exists.
        self.assertEqual(kwargs["model_dir"], PARAKEET_MODEL_DIR)

    def test_qwen_constructed_with_qwen_model_dir(self):
        self.config.stt_backend = "qwen"
        _mgr, _mock_parakeet, mock_qwen = self._make()
        kwargs = mock_qwen.call_args.kwargs
        # Qwen uses the registry resolver for its own ModelId, NOT the Parakeet dir.
        self.assertEqual(kwargs["model_dir"], QWEN_MODEL_DIR)
        self.assertIs(kwargs["logger"], self.logger)
        self.assertEqual(kwargs["threads"], self.config.threads)
        self.assertEqual(kwargs["timeout"], self.config.model_timeout)

    def test_cohere_constructed_with_language_and_exact_model_dir(self):
        self.config.stt_backend = "cohere"
        self.config.language = "en"
        self._make()
        kwargs = self.mock_cohere.call_args.kwargs
        self.assertEqual(kwargs["model_dir"], COHERE_MODEL_DIR)
        self.assertEqual(kwargs["language"], "en")
        self.assertEqual(kwargs["threads"], self.config.threads)
        self.assertEqual(kwargs["timeout"], self.config.model_timeout)

    def test_automatic_english_resolves_parakeet_v2(self):
        self.config.stt_model_selection_mode = "automatic"
        self.config.language = "en"
        self.config.stt_backend = "qwen"
        self.config.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
        _mgr, mock_parakeet, mock_qwen = self._make()
        mock_qwen.assert_not_called()
        self.assertEqual(
            mock_parakeet.call_args.kwargs["model_name"],
            "nemo-parakeet-tdt-0.6b-v2",
        )

    def test_automatic_qwen_only_language_resolves_qwen(self):
        self.config.stt_model_selection_mode = "automatic"
        self.config.language = "ja"
        self.config.stt_backend = "parakeet"
        _mgr, mock_parakeet, mock_qwen = self._make()
        mock_parakeet.assert_not_called()
        mock_qwen.assert_called_once()

    def test_manual_multilingual_parakeet_is_preserved_for_english(self):
        self.config.stt_model_selection_mode = "manual"
        self.config.language = "en"
        self.config.stt_backend = "parakeet"
        self.config.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
        _mgr, mock_parakeet, _mock_qwen = self._make()
        self.assertEqual(
            mock_parakeet.call_args.kwargs["model_name"],
            "nemo-parakeet-tdt-0.6b-v3",
        )

    def test_incompatible_manual_artifact_falls_back_to_recommendation(self):
        self.config.stt_model_selection_mode = "manual"
        self.config.language = "fr"
        self.config.stt_backend = "parakeet"
        self.config.parakeet_model = "nemo-parakeet-tdt-0.6b-v2"
        _mgr, mock_parakeet, _mock_qwen = self._make()
        self.assertEqual(
            mock_parakeet.call_args.kwargs["model_name"],
            "nemo-parakeet-tdt-0.6b-v3",
        )

    def test_qwen_force_is_effective_only_for_manual_qwen(self):
        self.config.language = "ja"
        self.config.stt_backend = "qwen"
        self.config.qwen_force_language = True

        self.config.stt_model_selection_mode = "automatic"
        _mgr, _mock_parakeet, mock_qwen = self._make()
        self.assertIsNone(mock_qwen.call_args.kwargs["forced_language"])

        self.config.stt_model_selection_mode = "manual"
        _mgr, _mock_parakeet, mock_qwen = self._make()
        self.assertEqual(mock_qwen.call_args.kwargs["forced_language"], "Japanese")

    # ------------------------------------------------------------------
    # Construction-time presence gate (is_present)
    # ------------------------------------------------------------------

    def test_parakeet_absent_raises_model_not_prepared(self):
        from whisperstream.parakeet_manager import ModelNotPreparedError

        self.config.stt_backend = "parakeet"
        with patch("whisperstream.stt_factory.ParakeetManager") as mock_parakeet, patch(
            "whisperstream.stt_factory.sys.platform", "win32"
        ), patch(
            "whisperstream.stt_factory.model_dir", side_effect=_fake_model_dir
        ), patch("whisperstream.stt_factory.is_present", return_value=False):
            with self.assertRaises(ModelNotPreparedError):
                make_stt_manager(
                    self.config,
                    logger=self.logger,
                    config_manager=self.config_manager,
                )
            # Gate runs BEFORE construction.
            mock_parakeet.assert_not_called()

    def test_qwen_absent_raises_model_not_prepared(self):
        from whisperstream.parakeet_manager import ModelNotPreparedError

        self.config.stt_backend = "qwen"
        with patch("whisperstream.stt_factory._qwen_manager_class") as qwen_class, patch(
            "whisperstream.stt_factory.sys.platform", "win32"
        ), patch(
            "whisperstream.stt_factory.model_dir", side_effect=_fake_model_dir
        ), patch("whisperstream.stt_factory.is_present", return_value=False):
            with self.assertRaises(ModelNotPreparedError):
                make_stt_manager(
                    self.config,
                    logger=self.logger,
                    config_manager=self.config_manager,
                )
            qwen_class.assert_not_called()

    # ------------------------------------------------------------------
    # Darwin selection and explicit unsupported engine behavior
    # ------------------------------------------------------------------

    def test_darwin_parakeet_builds_native_fluidaudio_manager(self):
        mac_manager = MagicMock(name="MacosFluidAudioManager")
        with patch("whisperstream.stt_factory.sys.platform", "darwin"), patch(
            "whisperstream.stt_factory._macos_manager_class",
            return_value=mac_manager,
        ), patch("whisperstream.stt_factory.is_present") as present:
            manager = make_stt_manager(
                self.config,
                logger=self.logger,
                config_manager=self.config_manager,
            )

        self.assertIs(manager, mac_manager.return_value)
        mac_manager.assert_called_once_with(
            logger=self.logger,
            timeout=self.config.model_timeout,
        )
        present.assert_not_called()

    def test_darwin_qwen_uses_supervised_manager_without_fluidaudio_substitution(self):
        self.config.stt_backend = "qwen"
        with patch("whisperstream.stt_factory.sys.platform", "darwin"), patch(
            "whisperstream.stt_factory._macos_manager_class"
        ) as mac_manager, patch(
            "whisperstream.stt_factory._qwen_manager_class"
        ) as qwen_manager, patch("whisperstream.stt_factory.is_present", return_value=True):
            result = make_stt_manager(self.config, logger=self.logger, config_manager=self.config_manager)
        self.assertIs(result, qwen_manager.return_value.return_value)
        mac_manager.assert_not_called()
        qwen_manager.assert_called_once_with()

    def test_cohere_absent_raises_model_not_prepared(self):
        from whisperstream.parakeet_manager import ModelNotPreparedError

        self.config.stt_backend = "cohere"
        with patch("whisperstream.stt_factory.CohereManager") as mock_cohere, patch(
            "whisperstream.stt_factory.model_dir", side_effect=_fake_model_dir
        ), patch("whisperstream.stt_factory.is_present", return_value=False):
            with self.assertRaises(ModelNotPreparedError):
                make_stt_manager(
                    self.config,
                    logger=self.logger,
                    config_manager=self.config_manager,
                )
            mock_cohere.assert_not_called()


if __name__ == "__main__":
    unittest.main()
