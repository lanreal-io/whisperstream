"""Destination-resolver security tests.

The former ``ConfigManager.model_dir(name, quantization)`` sanitized an
arbitrary, user-influenced model name into a directory under ``models/`` and
guarded against ``..`` path traversal. The model-layer unification (round 03)
replaced that resolver with ``model_downloader.model_dir(ModelId)``, which joins
a TRUSTED registry constant (``ModelSpec.dir_name``) onto the models root. There
is no longer any user-controlled component in the path, so the traversal /
sanitize guard is moot and its tests were removed along with the method.

This test pins the new invariant: every registry ``dir_name`` is a plain,
single-segment directory name (no separators, no ``..``) and resolves strictly
inside the models root.
"""

import unittest
from pathlib import Path

from whisperstream.model_downloader import DOWNLOAD_REGISTRY, ModelId, model_dir


class TestModelDirIsTrustedConstant(unittest.TestCase):
    def test_dir_names_are_single_trusted_segments(self):
        """Registry dir_names must be plain segments — no traversal possible."""
        for model_id, spec in DOWNLOAD_REGISTRY.items():
            self.assertNotIn("/", spec.dir_name, f"{model_id}: dir_name has a slash")
            self.assertNotIn("\\", spec.dir_name, f"{model_id}: dir_name has a backslash")
            self.assertNotIn("..", spec.dir_name, f"{model_id}: dir_name has ..")
            self.assertEqual(spec.dir_name, Path(spec.dir_name).name)

    def test_model_dir_stays_inside_models_root(self):
        """The resolved dir is always a child of get_models_root()."""
        from whisperstream.config_manager import get_models_root

        root = get_models_root().resolve()
        for model_id in DOWNLOAD_REGISTRY:
            resolved = model_dir(model_id).resolve()
            self.assertTrue(
                resolved.is_relative_to(root),
                f"{model_id}: {resolved} escapes {root}",
            )

    def test_qwen_moved_under_models_root(self):
        """Qwen now lives under models/ (new location), not the old sibling dir."""
        self.assertEqual(
            DOWNLOAD_REGISTRY[ModelId.QWEN3_ASR_06B_INT8].dir_name,
            "qwen3-asr-0.6b-int8",
        )

    def test_parakeet_dir_names_unchanged(self):
        """Parakeet dir_names must equal the historical paths (no re-download)."""
        self.assertEqual(
            DOWNLOAD_REGISTRY[ModelId.PARAKEET_V3_INT8].dir_name,
            "nemo-parakeet-tdt-0.6b-v3-int8",
        )
        self.assertEqual(
            DOWNLOAD_REGISTRY[ModelId.PARAKEET_V2_INT8].dir_name,
            "nemo-parakeet-tdt-0.6b-v2-int8",
        )


if __name__ == "__main__":
    unittest.main()
