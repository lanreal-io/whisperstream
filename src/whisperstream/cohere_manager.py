"""Native-free Cohere parent using the supervised STT lifecycle."""
from __future__ import annotations

from .language_routing import COHERE_LANGUAGES
from .qwen_manager import QwenManager

COHERE_DEFAULT_THREADS = 6


def validated_cohere_language(language: str | None) -> str:
    normalized = (language or "").strip().lower()
    if normalized not in COHERE_LANGUAGES:
        raise ValueError("Unsupported Cohere Transcribe language")
    return normalized


class CohereManager(QwenManager):
    """Explicit-language decoding with native inference confined to a child."""

    _log_tag = "cohere"
    engine_id = "cohere"
    _validated_language = staticmethod(validated_cohere_language)

    def __init__(self, *, language: str, threads=None, **kwargs):
        super().__init__(
            forced_language=validated_cohere_language(language),
            threads=threads or COHERE_DEFAULT_THREADS,
            **kwargs,
        )

    def _language_body(self, language):
        return {} if language is None else {"language_override": validated_cohere_language(language)}

    def _load_body(self):
        body = super()._load_body()
        body.pop("max_new_tokens")
        return body

    def _log_loading(self, *, reason: str) -> None:
        self._logger.info("cohere model loading threads=%s reason=%s", self._threads, reason)

    def set_language_hint(self, language: str | None) -> None:
        super().set_language_hint(validated_cohere_language(language))
