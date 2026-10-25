"""STT backend factory.

Single place that selects the platform adapter and configured local
speech-to-text backend. The construction sites (serve, smoke check, and live
model-download swap) call ``make_stt_manager`` instead of constructing a
manager directly, so platform/backend selection and per-adapter kwargs live
here and nowhere else.

Selection (research C1 — default-fallback to Parakeet for unknown values):

* Automatic mode chooses the exact recommended artifact for ``language``.
* Manual mode preserves the exact compatible artifact encoded by
  ``config.stt_backend`` + ``config.parakeet_model``.
* ``WHISPERSTREAM_STT_BACKEND`` env var overrides the config value when set
  and non-empty (env WINS). Whitespace-only is treated as unset.
* ``"qwen"`` (case-insensitive) -> ``QwenManager`` (model dir from
  ``model_dir(ModelId.QWEN3_ASR_06B_INT8)``).
* ``"parakeet"`` OR any unknown / empty value -> ``ParakeetManager``,
  constructed exactly as the existing call sites did.
* On Darwin, multilingual Parakeet v3 uses FluidAudio. English Parakeet v2,
  Qwen and Cohere use the official locked sherpa-onnx worker.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, Dict

from .cohere_manager import CohereManager
from .model_downloader import ModelId, is_present, model_dir, model_id_for_parakeet
from .parakeet_manager import ModelNotPreparedError, ParakeetManager
from .stt_contracts import SttEngine
from .stt_errors import SttEngineUnavailableError

if TYPE_CHECKING:
    from .config_manager import ConfigManager, WhisperstreamConfig

# Env override seam for quick A/B flipping without editing config.toml.
STT_BACKEND_ENV_VAR = "WHISPERSTREAM_STT_BACKEND"

# Catalog id of the Qwen artifact (mirrors qwen_models.QWEN_MODELS[0]["id"]).
# Used as the QwenManager model_name for logging/metadata only.
QWEN_MODEL_NAME = "qwen3-asr-0.6b-int8"
COHERE_MODEL_NAME = "cohere-transcribe-03-2026-int8"


def _selection_from_config(config: "WhisperstreamConfig"):
    """Return normalized routing, with compatibility for legacy config doubles.

    Real persisted configs always have ``stt_model_selection_mode`` after
    ``WhisperstreamConfig.from_dict`` migration. A few integration seams and
    older test doubles are intentionally duck-typed, so absence of the new
    field retains their historical explicit-backend behavior.
    """
    from .language_routing import (
        COHERE_ARTIFACT,
        QWEN_ARTIFACT,
        SttSelection,
        configured_artifact,
        normalize_stt_selection,
    )

    mode = getattr(config, "stt_model_selection_mode", None)
    if not isinstance(mode, str):
        artifact = configured_artifact(
            getattr(config, "stt_backend", None),
            getattr(config, "parakeet_model", None),
        )
        backend = (
            "qwen"
            if artifact == QWEN_ARTIFACT
            else "cohere"
            if artifact == COHERE_ARTIFACT
            else "parakeet"
        )
        force = getattr(config, "qwen_force_language", False)
        return SttSelection(
            mode="manual",
            backend=backend,
            artifact=artifact,
            parakeet_model=configured_artifact(
                "parakeet", getattr(config, "parakeet_model", None)
            ),
            qwen_force_language=bool(force) if isinstance(force, bool) else False,
        )

    return normalize_stt_selection(
        language=config.language,
        selection_mode=mode,
        selection_mode_present=True,
        stt_backend=config.stt_backend,
        parakeet_model=config.parakeet_model,
        qwen_force_language=config.qwen_force_language,
    )


def _qwen_manager_class():
    """Import the still-native Qwen manager only when Qwen is selected."""

    from .qwen_manager import QwenManager

    return QwenManager


def _macos_manager_class():
    """Import the native-free macOS adapter only when Darwin is selected."""

    from .macos_stt_manager import MacosFluidAudioManager

    return MacosFluidAudioManager


def _resolve_backend(config: "WhisperstreamConfig") -> tuple[str, str]:
    """Return ``(backend, source)``.

    ``backend`` is the lowercased selection; ``source`` is ``"env"`` when the
    ``WHISPERSTREAM_STT_BACKEND`` override won, else ``"config"``.
    """
    override = os.environ.get(STT_BACKEND_ENV_VAR, "").strip()
    if override:
        return override.lower(), "env"
    selection = _selection_from_config(config)
    return selection.backend, "config"


def selected_model_id(config) -> ModelId:
    """Resolve the same exact artifact used by construction, including overrides."""
    backend, _ = _resolve_backend(config)
    if backend == "qwen":
        return ModelId.QWEN3_ASR_06B_INT8
    if backend == "cohere":
        return ModelId.COHERE_TRANSCRIBE_INT8
    return model_id_for_parakeet(_selection_from_config(config).parakeet_model)


def make_stt_manager(
    config: "WhisperstreamConfig",
    *,
    logger: logging.Logger,
    config_manager: "ConfigManager",
) -> SttEngine:
    """Construct the STT manager selected by ``config`` + env override.

    Both backends conform to the portable :class:`SttEngine` contract, so the
    returned object is interchangeable from the caller's perspective. The caller passes
    the same values it already has in scope (``config``, ``logger``, and the
    ``config_manager`` used to resolve model directories); the factory derives
    each backend's construction kwargs from those.

    ``"qwen"`` (case-insensitive, or via ``WHISPERSTREAM_STT_BACKEND``) builds a
    ``QwenManager`` pointed at ``model_dir(ModelId.QWEN3_ASR_06B_INT8)``.
    ``"parakeet"`` or any unknown / empty value builds a ``ParakeetManager``
    exactly as the original call sites did (default-fallback per research C1).
    """
    selection = _selection_from_config(config)
    backend, source = _resolve_backend(config)

    if (sys.platform == "darwin" and backend not in {"qwen", "cohere"}
            and model_id_for_parakeet(selection.parakeet_model) == ModelId.PARAKEET_V3_INT8):
        MacosFluidAudioManager = _macos_manager_class()
        logger.info("stt backend selected backend=parakeet adapter=fluidaudio source=%s", source)
        return MacosFluidAudioManager(logger=logger, timeout=config.model_timeout)

    if backend == "qwen":
        from .language_routing import qwen_forced_language_value

        # Fail fast (and uniformly) when the selected model isn't on disk, using
        # the same zero-byte-aware registry presence check the download gate
        # uses. Raised as ModelNotPreparedError so the existing handlers in
        # main.py (serve load + smoke check) keep catching it. Without this the
        # manager would attempt a load and raise deeper in _load_model.
        qwen_id = ModelId.QWEN3_ASR_06B_INT8
        if not is_present(qwen_id):
            raise ModelNotPreparedError(
                f"Qwen model {qwen_id.value!r} is not downloaded "
                f"(missing or truncated files in {model_dir(qwen_id)})"
            )
        # Qwen's parent manager is native-free; its selected child adapter owns
        # every sherpa/ONNX import and recognizer operation.
        QwenManager = _qwen_manager_class()
        forced_language = qwen_forced_language_value(
            config.language,
            selection.qwen_force_language if selection.backend == "qwen" else False,
        )
        logger.info(
            "stt backend selected backend=qwen source=%s force_language=%s",
            source,
            forced_language or "auto",
        )
        return QwenManager(
            model_name=QWEN_MODEL_NAME,
            quantization=config.parakeet_quantization,
            threads=config.threads,
            logger=logger,
            model_dir=model_dir(qwen_id),
            timeout=config.model_timeout,
            hotwords=list(config.word_boosts),
            hotwords_score=config.hotword_score,
            beam_width=config.beam_width,
            forced_language=forced_language,
        )

    if backend == "cohere":
        cohere_id = ModelId.COHERE_TRANSCRIBE_INT8
        if not is_present(cohere_id):
            raise ModelNotPreparedError(
                f"Cohere model {cohere_id.value!r} is not downloaded "
                f"(missing or truncated files in {model_dir(cohere_id)})"
            )
        logger.info(
            "stt backend selected backend=cohere source=%s language=%s",
            source,
            config.language,
        )
        return CohereManager(
            model_name=COHERE_MODEL_NAME,
            language=config.language,
            quantization=config.parakeet_quantization,
            threads=config.threads,
            logger=logger,
            model_dir=model_dir(cohere_id),
            timeout=config.model_timeout,
            hotwords=list(config.word_boosts),
            hotwords_score=config.hotword_score,
            beam_width=config.beam_width,
        )

    # Parakeet: the default-fallback for "parakeet" and any unknown/empty value.
    # Reproduce the existing call-site kwargs byte-for-byte.
    if backend not in ("", "parakeet"):
        logger.warning(
            "stt backend unknown value=%s action=fallback_to_parakeet source=%s",
            backend,
            source,
        )
    parakeet_model = selection.parakeet_model
    parakeet_id = model_id_for_parakeet(parakeet_model)
    if not is_present(parakeet_id):
        raise ModelNotPreparedError(
            f"Parakeet model {parakeet_id.value!r} is not downloaded "
            f"(missing or truncated files in {model_dir(parakeet_id)})"
        )
    logger.info("stt backend selected backend=parakeet source=%s", source)
    return ParakeetManager(
        model_name=parakeet_model,
        quantization=config.parakeet_quantization,
        threads=config.threads,
        logger=logger,
        model_dir=model_dir(parakeet_id),
        timeout=config.model_timeout,
        hotwords=list(config.word_boosts),
        hotwords_score=config.hotword_score,
        beam_width=config.beam_width,
    )


# ---------------------------------------------------------------------------
# Per-backend presence / status
# ---------------------------------------------------------------------------


def parakeet_model_present(
    config: "WhisperstreamConfig",
    config_manager: "ConfigManager",
) -> bool:
    """Return ``True`` when the currently-configured Parakeet model is on disk.

    Thin wrapper over the unified ``model_downloader.is_present``, keyed by the
    v2/v3 ``ModelId`` that ``config.parakeet_model`` selects — so a status query
    reflects exactly the model the user's current language would load, and the
    presence check is the same zero-byte-aware one the download gate uses.
    ``config_manager`` is accepted for signature stability but no longer
    consulted (the registry owns destination + presence now).
    """
    if sys.platform == "darwin" and model_id_for_parakeet(config.parakeet_model) == ModelId.PARAKEET_V3_INT8:
        from .macos_stt_manager import macos_stt_model_present
        return macos_stt_model_present()
    return is_present(model_id_for_parakeet(config.parakeet_model))


def qwen_model_present() -> bool:
    """Return ``True`` when every required Qwen3-ASR file is on disk.

    Thin wrapper over ``model_downloader.is_present`` for the Qwen ModelId. The
    registry's ``files`` (sourced once from the ``qwen_models`` catalog) are the
    same files ``QwenManager._load_model`` requires before it will load.
    Idempotent and side-effect free.
    """
    return is_present(ModelId.QWEN3_ASR_06B_INT8)


def cohere_model_present() -> bool:
    """Return whether the complete Cohere Transcribe artifact is on disk."""
    return is_present(ModelId.COHERE_TRANSCRIBE_INT8)


def stt_model_status(
    config: "WhisperstreamConfig",
    config_manager: "ConfigManager",
) -> Dict[str, Dict[str, bool | str]]:
    """Report per-backend ``{downloaded, active}`` for both STT backends.

    ``downloaded`` is whether that backend's model files are present on disk
    (for Parakeet, the variant the current ``parakeet_model`` selects).
    ``active`` is whether that backend is the one the config (or env override)
    currently resolves to via :func:`_resolve_backend`. Backs the
    ``GET /api/stt/status`` endpoint used by the confirm-then-download and
    manual-delete flows.
    """
    backend, _source = _resolve_backend(config)
    active = backend if backend in ("parakeet", "qwen", "cohere") else "parakeet"

    return {
        "parakeet": {
            "downloaded": parakeet_model_present(config, config_manager),
            "active": active == "parakeet",
        },
        "qwen": {
            "downloaded": qwen_model_present(),
            "active": active == "qwen",
        },
        "cohere": {
            "downloaded": cohere_model_present(),
            "active": active == "cohere",
        },
    }
