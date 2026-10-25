"""Language and selection-mode STT routing (Python source of truth).

In Automatic mode, the selected language decides the exact local STT artifact
to download and run. In Manual mode, ``stt_backend`` plus ``parakeet_model``
pin the exact compatible artifact. The language becomes a per-call decode hint
only for Manual Qwen when ``qwen_force_language`` is enabled; otherwise both
Parakeet v3 and Qwen3-ASR auto-detect spoken language at inference time.

Two local backends with overlapping language coverage:

* **Parakeet TDT v3** -- 25 European languages. ``en`` ships as its own v2
  (English-only) model; every other Parakeet language uses v3 (multilingual).
* **Qwen3-ASR 0.6B** -- 30 languages (East/SE Asian + Hindi + Arabic + Persian +
  Macedonian + most of Europe).

Union = 39 languages. Classification: 16 dual-support, 9 Parakeet-only,
14 Qwen-only.

These sets MUST stay in sync with:
* ``SUPPORTED_LANGUAGES`` in ``config_manager.py`` (their union),
* ``PARAKEET_LANGUAGES`` / ``QWEN_LANGUAGES`` in
  ``frontend/src/lib/languages.ts``,
* ``DOWNLOAD_REGISTRY`` + ``model_id_for_parakeet`` in ``model_downloader.py``
  (the Parakeet v2/v3 variant mapping).

``stt_factory._resolve_backend`` remains the authority for the *active* backend
(config field + env override). This module answers the orthogonal question "which
backend *should* serve this language" so callers can validate or auto-select.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, cast

Backend = Literal["parakeet", "qwen", "cohere"]
SelectionMode = Literal["automatic", "manual"]

# 25 Parakeet TDT v3 languages.
PARAKEET_LANGUAGES: frozenset[str] = frozenset({
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de",
    "el", "hu", "it", "lv", "lt", "mt", "pl", "pt", "ro", "sk",
    "sl", "es", "sv", "ru", "uk",
})

# 30 Qwen3-ASR 0.6B languages (research.md confirmed set).
QWEN_LANGUAGES: frozenset[str] = frozenset({
    "zh", "en", "yue", "ar", "de", "fr", "es", "pt", "id", "it",
    "ko", "ru", "th", "vi", "ja", "tr", "hi", "ms", "nl", "sv",
    "da", "fi", "pl", "cs", "fil", "fa", "el", "hu", "mk", "ro",
})

# Cohere Transcribe requires one explicit language per stream and supports 14.
COHERE_LANGUAGES: frozenset[str] = frozenset({
    "ar", "de", "el", "en", "es", "fr", "it", "ja", "ko", "nl",
    "pl", "pt", "vi", "zh",
})

# ISO code -> the value Qwen actually honors when forcing a language via
# OfflineStream.set_option("language", <value>). This is the **English language
# NAME**, NOT the ISO code: an empirical sweep on the official 0.6B model (CPU,
# greedy) confirmed English names bias output (e.g. "Japanese" flips a clip to
# Japanese; "Russian" to Cyrillic) while ISO codes ("ja"/"ru") are largely
# inert. The Qwen3-ASR model card corroborates it (`language="English"` /
# `language=["Chinese", "English"]`). Map covers all 30 QWEN_LANGUAGES.
QWEN_LANGUAGE_NAMES: dict[str, str] = {
    "zh": "Chinese", "en": "English", "yue": "Cantonese", "ar": "Arabic",
    "de": "German", "fr": "French", "es": "Spanish", "pt": "Portuguese",
    "id": "Indonesian", "it": "Italian", "ko": "Korean", "ru": "Russian",
    "th": "Thai", "vi": "Vietnamese", "ja": "Japanese", "tr": "Turkish",
    "hi": "Hindi", "ms": "Malay", "nl": "Dutch", "sv": "Swedish",
    "da": "Danish", "fi": "Finnish", "pl": "Polish", "cs": "Czech",
    "fil": "Filipino", "fa": "Persian", "el": "Greek", "hu": "Hungarian",
    "mk": "Macedonian", "ro": "Romanian",
}

# Parakeet model variants. en -> v2 (English-only); everything else -> v3
# (multilingual). Mirrors getParakeetModel() in languages.ts and
# model_id_for_parakeet() in model_downloader.py. The v2/v3 split is never
# user-visible -- the picker only ever exposes Parakeet vs Qwen.
PARAKEET_V2_MODEL = "nemo-parakeet-tdt-0.6b-v2"
PARAKEET_V3_MODEL = "nemo-parakeet-tdt-0.6b-v3"
QWEN_ARTIFACT = "qwen3-asr-0.6b-int8"
COHERE_ARTIFACT = "cohere-transcribe-03-2026-int8"


@dataclass(frozen=True, slots=True)
class SttSelection:
    """A normalized persisted STT selection.

    ``artifact`` is exact even though the UI groups both Parakeet artifacts
    into one user-facing model. ``parakeet_model`` remains populated while
    Qwen is selected so switching back can retain the last known variant.
    """

    mode: SelectionMode
    backend: Backend
    artifact: str
    parakeet_model: str
    qwen_force_language: bool


def _normalize(code: str | None) -> str:
    return (code or "").strip().lower()


def models_for_language(code: str | None) -> List[Backend]:
    """Return the backend(s) that can transcribe ``code``.

    Parakeet is listed first when both support the language (Parakeet is the
    default for dual-support languages). Unknown/empty codes return ``[]``.
    """
    lang = _normalize(code)
    models: List[Backend] = []
    if lang in PARAKEET_LANGUAGES:
        models.append("parakeet")
    if lang in QWEN_LANGUAGES:
        models.append("qwen")
    if lang in COHERE_LANGUAGES:
        models.append("cohere")
    return models


def language_to_backend(code: str | None) -> Backend:
    """Resolve ``code`` to its default backend.

    Parakeet when it supports the language, otherwise Qwen. Unknown/empty codes
    default to Parakeet (matching ``stt_factory``'s default-fallback posture).
    Mirrors ``getDefaultBackendForLanguage`` in ``languages.ts``.
    """
    lang = _normalize(code)
    if lang in QWEN_LANGUAGES and lang not in PARAKEET_LANGUAGES:
        return "qwen"
    return "parakeet"


def recommended_artifact(code: str | None) -> str:
    """Return Whisperstream's exact Automatic artifact for ``code``."""
    if language_to_backend(code) == "qwen":
        return QWEN_ARTIFACT
    return parakeet_variant(code)


def configured_artifact(backend: str | None, parakeet_model: str | None) -> str:
    """Return the exact artifact represented by legacy routing fields.

    Unknown backend values preserve the factory's historical fallback to
    Parakeet. Unknown Parakeet model values likewise retain the historical v3
    fallback rather than allowing an arbitrary path-like identifier through.
    """
    if (backend or "").strip().lower() == "qwen":
        return QWEN_ARTIFACT
    if (backend or "").strip().lower() == "cohere":
        return COHERE_ARTIFACT
    if (parakeet_model or "").strip().lower() == PARAKEET_V2_MODEL:
        return PARAKEET_V2_MODEL
    return PARAKEET_V3_MODEL


def artifact_supports_language(artifact: str, code: str | None) -> bool:
    """Return whether one exact STT artifact supports ``code``."""
    lang = _normalize(code)
    if artifact == PARAKEET_V2_MODEL:
        return lang == "en"
    if artifact == PARAKEET_V3_MODEL:
        return lang in PARAKEET_LANGUAGES
    if artifact == QWEN_ARTIFACT:
        return lang in QWEN_LANGUAGES
    if artifact == COHERE_ARTIFACT:
        return lang in COHERE_LANGUAGES
    return False


def normalize_stt_selection(
    *,
    language: str | None,
    selection_mode: str | None,
    selection_mode_present: bool,
    stt_backend: str | None,
    parakeet_model: str | None,
    qwen_force_language: bool,
) -> SttSelection:
    """Normalize persisted routing state without changing a valid legacy route.

    A config written before selection mode existed is inferred as Manual only
    when its exact configured artifact is compatible but differs from the
    Automatic recommendation, or when it already has an effective Qwen force
    hint. This preserves working overrides on upgrade. Explicit invalid modes
    and incompatible Manual pins fall back to Automatic.
    """
    current_artifact = configured_artifact(stt_backend, parakeet_model)
    recommendation = recommended_artifact(language)
    current_is_compatible = artifact_supports_language(current_artifact, language)
    current_backend: Backend = (
        "qwen"
        if current_artifact == QWEN_ARTIFACT
        else "cohere"
        if current_artifact == COHERE_ARTIFACT
        else "parakeet"
    )

    normalized_mode = (selection_mode or "").strip().lower()
    if not selection_mode_present:
        force_requires_manual = (
            bool(qwen_force_language)
            and current_backend == "qwen"
            and current_is_compatible
        )
        normalized_mode = (
            "manual"
            if current_is_compatible
            and (current_artifact != recommendation or force_requires_manual)
            else "automatic"
        )
    elif normalized_mode not in ("automatic", "manual"):
        normalized_mode = "automatic"

    if normalized_mode == "manual" and current_is_compatible:
        selected_artifact = current_artifact
        selected_backend = current_backend
        effective_force = bool(qwen_force_language) and selected_backend == "qwen"
    else:
        normalized_mode = "automatic"
        selected_artifact = recommendation
        selected_backend = (
            "qwen" if selected_artifact == QWEN_ARTIFACT else "parakeet"
        )
        effective_force = False

    normalized_parakeet = configured_artifact("parakeet", parakeet_model)
    if selected_backend == "parakeet":
        normalized_parakeet = selected_artifact

    return SttSelection(
        mode=cast(SelectionMode, normalized_mode),
        backend=selected_backend,
        artifact=selected_artifact,
        parakeet_model=normalized_parakeet,
        qwen_force_language=effective_force,
    )


def qwen_forced_language_value(code: str | None, force: bool) -> str | None:
    """Resolve the value to force on Qwen, or ``None`` for auto-detect.

    Returns the English language NAME (per :data:`QWEN_LANGUAGE_NAMES`) when
    ``force`` is True and ``code`` is a Qwen-supported language; otherwise
    ``None`` (auto-detect). ``None`` is also returned for an unknown/empty code
    or a code Qwen doesn't support, so a stray value never forces a wrong
    language. Callers pass the result to
    ``QwenManager`` / ``stream.set_option("language", value)``; ``None`` means
    "set no option" (the auto-detect default).

    Only meaningful for the Qwen backend — Parakeet auto-detects and ignores a
    per-call language, so this is never consulted for Parakeet.
    """
    if not force:
        return None
    return QWEN_LANGUAGE_NAMES.get(_normalize(code))


def parakeet_variant(code: str | None) -> str:
    """Return the Parakeet model name for ``code``.

    ``en`` -> v2 (English-only); every other code -> v3 (multilingual). Mirrors
    ``getParakeetModel`` in ``languages.ts`` and ``model_id_for_parakeet`` in
    ``model_downloader.py``.
    """
    return PARAKEET_V2_MODEL if _normalize(code) == "en" else PARAKEET_V3_MODEL
