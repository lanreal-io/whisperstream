"""Native-free admission client; detector failures never become ASR failures."""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
import logging
import numpy as np
from .speech_gate_settings import SpeechGateSettings
from .stt_supervisor import default_worker_command
from .stt_worker_process import SttWorkerProcess


class SpeechGateClient:
    def __init__(self, settings: SpeechGateSettings, *, diagnostic_dir: Path,
                 logger: logging.Logger, timeout: float = 5.0, process_factory=None):
        self.settings = settings
        self.failed = False
        self._process = None
        self._timeout = timeout
        self._logger = logger
        self._factory = process_factory or (lambda: SttWorkerProcess(
            default_worker_command(), backend='speech_gate', diagnostic_dir=diagnostic_dir,
            cwd=Path(__file__).resolve().parents[2]))

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.is_running

    def close(self):
        process, self._process = self._process, None
        if process is not None:
            try:
                process.shutdown(timeout=1.0)
            except Exception as exc:
                self.failed = True
                self._logger.warning(
                    "speech_gate unavailable action=bypass error_type=%s", type(exc).__name__)
                process.terminate_owned_worker(expected=False)

    def has_speech(self, audio: np.ndarray, *, sample_rate=16000) -> bool:
        if not self.settings.enabled or self.failed or sample_rate != 16000:
            return True
        if not audio.size or not np.any(audio):
            return False
        try:
            if self._process is None:
                self._process = self._factory()
                self._process.start(timeout=self._timeout)
                self._process.request('load', body=asdict(self.settings), timeout=self._timeout)
            body = self._process.request_audio('admit', audio, sample_rate=sample_rate,
                                               timeout=self._timeout)
            admitted = body.get('admitted')
            failed = body.get('detector_failed')
            if type(admitted) is not bool or type(failed) is not bool:
                raise ValueError('Invalid speech admission reply')
            if failed:
                raise RuntimeError('Speech detector unavailable')
            return admitted
        except Exception as exc:
            self.failed = True
            try:
                self.close()
            except Exception:
                pass
            self._logger.warning('speech_gate unavailable action=bypass error_type=%s',
                                 type(exc).__name__)
            return True
