"""Upstream and legacy Kokoro caches for ordinary app builds."""
from pathlib import Path


def kokoro_download_enabled() -> bool:
    return True


LEGACY_KOKORO_DIR = "kokoro-82m-fp32-en-us-2026-07-28"
LEGACY_KOKORO_FILES = (
    "kokoro-v1.0.onnx", "voices-v1.0.npz", "config.json", "g2p-en-us.onnx",
    "g2p-en-us.json", "us_gold.json", "us_silver.json",
    "averaged_perceptron_tagger_eng.weights.json",
    "averaged_perceptron_tagger_eng.classes.json",
    "averaged_perceptron_tagger_eng.tagdict.json",
)


def _complete(directory: Path, files: tuple[str, ...]) -> bool:
    try:
        return all((directory / name).is_file() and (directory / name).stat().st_size > 0
                   for name in files)
    except OSError:
        return False


def legacy_kokoro_dir(*, models_root: Path | None = None) -> Path:
    from ..config_manager import get_models_root
    return (models_root if models_root is not None else get_models_root()) / LEGACY_KOKORO_DIR


def legacy_kokoro_cached(*, models_root: Path | None = None) -> bool:
    return _complete(legacy_kokoro_dir(models_root=models_root), LEGACY_KOKORO_FILES)


def kokoro_cached(*, models_root: Path | None = None) -> bool:
    from ..model_downloader import DOWNLOAD_REGISTRY, ModelId, model_dir
    model = ModelId.KOKORO_82M_FP32
    spec = DOWNLOAD_REGISTRY[model]
    directory = models_root / spec.dir_name if models_root is not None else model_dir(model)
    return _complete(directory, spec.files) or legacy_kokoro_cached(models_root=models_root)


def kokoro_available(*, models_root: Path | None = None) -> bool:
    return kokoro_download_enabled() or kokoro_cached(models_root=models_root)


def require_engine_available(engine: str) -> None:
    if engine == "kokoro" and not kokoro_available():
        raise ValueError("Kokoro is unavailable.")
