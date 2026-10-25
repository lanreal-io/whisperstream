"""Native-free parent Qwen manager using the shared STT supervisor."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .stt_contracts import (
    SttCapabilities,
    SttLanguageHintCapability,
    SttLanguageHintSupport,
    SttPreviewMetadata,
    SttPreviewSupport,
)
from .stt_supervisor import NativeSttWorkerCrashed, SttWorkerSupervisor
from .supervised_stt_manager import SupervisedSttManager


# Keep the product guard visible to native-free callers and tests. The child
# adapter is the only module that passes it to sherpa.
QWEN_MAX_NEW_TOKENS = 512


class QwenManager(SupervisedSttManager):
    """Qwen3-ASR contract with native ownership in one supervised child.

    Parakeet-only construction arguments remain accepted for factory parity,
    but Qwen advertises no hotword capability. Preview is text-only, and its
    dynamic language hint updates worker decode options without reloading.
    """

    _log_tag = "qwen"
    engine_id = "qwen"
    capabilities = SttCapabilities(
        preview=SttPreviewSupport(
            metadata=SttPreviewMetadata.TEXT,
            nonblocking_admission=True,
        ),
        language_hint=SttLanguageHintSupport(dynamic_updates=True),
    )

    def __init__(
        self,
        *,
        model_name: str,
        quantization: Optional[str] = None,
        threads: Optional[int] = None,
        logger: logging.Logger,
        model_dir: Path,
        timeout: float = 300.0,
        hotwords: list[str] | None = None,
        hotwords_score: float = 1.5,
        beam_width: int = 4,
        forced_language: Optional[str] = None,
        _supervisor: SttWorkerSupervisor | None = None,
    ) -> None:
        # Parakeet-only values are deliberately accepted but not stored. Their
        # absence from the Qwen load body makes the capability difference
        # explicit at the process boundary.
        _ = quantization, hotwords, hotwords_score, beam_width
        self._forced_language = forced_language
        super().__init__(
            model_name=model_name,
            threads=threads,
            logger=logger,
            model_dir=model_dir,
            timeout=timeout,
            _supervisor=_supervisor,
        )

    def _language_body(self, language):
        if language is None:
            return {}
        from .language_routing import qwen_forced_language_value
        return {"language_override": qwen_forced_language_value(language, True) or ""}

    def _load_body(self) -> dict[str, Any]:
        return {
            "model_name": self._model_name,
            "model_dir": str(self._model_dir),
            "threads": self._threads,
            "forced_language": self._forced_language,
            "max_new_tokens": QWEN_MAX_NEW_TOKENS,
        }

    def _log_loading(self, *, reason: str) -> None:
        self._logger.info(
            "qwen model loading model=%s threads=%s max_new_tokens=%d "
            "language=%s reason=%s",
            self._model_name,
            self._threads or "auto",
            QWEN_MAX_NEW_TOKENS,
            "forced" if self._forced_language else "auto",
            reason,
        )

    @property
    def language_hint_capability(self) -> SttLanguageHintCapability:
        return self

    def set_language_hint(self, name: Optional[str]) -> None:
        """Set or clear the worker-owned Qwen decode language option."""

        if name is not None and not isinstance(name, str):
            raise TypeError("Qwen language hint must be a string or None")
        with self._lock:
            if self._shutdown:
                return
            try:
                if self._loaded:
                    self._supervisor.set_language_hint(name)
            except Exception as exc:  # noqa: BLE001 - config updates never raise
                if isinstance(exc, NativeSttWorkerCrashed):
                    self._loaded = False
                else:
                    self._supervisor.stop_worker()
                    self._loaded = False
                self._logger.warning(
                    "qwen language hint update failed reason=%s action=lazy_reload",
                    type(exc).__name__,
                )
            self._forced_language = name
        self._logger.info(
            "qwen language hint %s",
            "set" if name else "cleared",
        )


__all__ = ["QWEN_MAX_NEW_TOKENS", "QwenManager"]
