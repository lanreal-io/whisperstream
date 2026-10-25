"""Native-free parent Parakeet manager using the shared STT supervisor."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .stt_contracts import (
    SttCapabilities,
    SttHotwordCapability,
    SttHotwordSupport,
    SttHotwordUpdateMode,
    SttPreviewMetadata,
    SttPreviewSupport,
)
from .stt_errors import ModelNotPreparedError
from .stt_supervisor import NativeSttWorkerCrashed, SttWorkerSupervisor
from .supervised_stt_manager import SupervisedSttManager


class ParakeetManager(SupervisedSttManager):
    """Parakeet TDT contract with native ownership in one supervised child."""

    _log_tag = "parakeet"
    engine_id = "parakeet"
    capabilities = SttCapabilities(
        final_alignment=True,
        preview=SttPreviewSupport(
            metadata=SttPreviewMetadata.TIMED_TOKENS,
            nonblocking_admission=True,
        ),
        hotwords=SttHotwordSupport(update_mode=SttHotwordUpdateMode.RELOAD),
    )

    def __init__(
        self,
        *,
        model_name: str,
        quantization: Optional[str],
        threads: Optional[int],
        logger: logging.Logger,
        model_dir: Path,
        timeout: float = 300.0,
        hotwords: list[str] | None = None,
        hotwords_score: float = 1.5,
        beam_width: int = 4,
        _supervisor: SttWorkerSupervisor | None = None,
    ) -> None:
        self._quantization = quantization or "int8"
        self._hotwords = list(hotwords or [])
        self._hotwords_score = hotwords_score
        self._beam_width = beam_width
        super().__init__(
            model_name=model_name,
            threads=threads,
            logger=logger,
            model_dir=model_dir,
            timeout=timeout,
            _supervisor=_supervisor,
        )

    def _supervisor_model_identifier(self) -> str:
        return f"{self._model_name}:{self._quantization}"

    def _load_body(self) -> dict[str, Any]:
        return {
            "model_name": self._model_name,
            "model_dir": str(self._model_dir),
            "quantization": self._quantization,
            "threads": self._threads,
            "hotwords": list(self._hotwords),
            "hotword_score": float(self._hotwords_score),
            "beam_width": int(self._beam_width),
        }

    def _log_loading(self, *, reason: str) -> None:
        self._logger.info(
            "parakeet model loading model=%s quantization=%s threads=%s "
            "decoding=greedy_search reason=%s",
            self._model_name,
            self._quantization,
            self._threads or "auto",
            reason,
        )

    def _log_loaded(self, *, elapsed: float) -> None:
        variant = "int8" if self._quantization == "int8" else "fp16"
        self._logger.info(
            "parakeet model loaded variant=%s latency=%.2fs worker_pid=%s",
            variant,
            elapsed,
            self.worker_pid,
        )

    @property
    def hotword_capability(self) -> SttHotwordCapability:
        return self

    def update_hotwords(self, hotwords: list[str], score: float) -> None:
        """Reload the child-owned recognizer with the complete hotword set."""

        with self._lock:
            if self._shutdown:
                return
            self._logger.info(
                "parakeet hotwords updated count=%d score=%.1f action=reload_model",
                len(hotwords),
                score,
            )
            previous_hotwords = self._hotwords
            previous_score = self._hotwords_score
            self._hotwords = list(hotwords)
            self._hotwords_score = score
            try:
                if self._loaded:
                    self._supervisor.update_hotwords(list(hotwords), score)
                else:
                    # Historical behavior reloads Parakeet even when an idle
                    # model receives a dictionary update.
                    self._load_model_unlocked(reason="hotword_update")
            except Exception as exc:  # noqa: BLE001 - config updates never raise
                self._hotwords = previous_hotwords
                self._hotwords_score = previous_score
                if isinstance(exc, NativeSttWorkerCrashed):
                    self._loaded = False
                self._logger.warning(
                    "parakeet hotword reload failed reason=%s",
                    type(exc).__name__,
                )


__all__ = ["ModelNotPreparedError", "ParakeetManager"]
