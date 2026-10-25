"""Native-free immutable speech admission settings."""
from __future__ import annotations
import logging
import math
import os
from dataclasses import dataclass
logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class SpeechGateSettings:
    enabled: bool = True
    threshold: float = 0.2
    min_speech_ms: float = 96

    def __post_init__(self) -> None:
        if not math.isfinite(self.threshold) or not 0 < self.threshold < 1:
            raise ValueError("Speech threshold must be between zero and one")
        if not math.isfinite(self.min_speech_ms) or not 32 <= self.min_speech_ms <= 1000:
            raise ValueError("Speech duration must be between 32 and 1000 milliseconds")

    @classmethod
    def from_environment(cls) -> SpeechGateSettings:
        defaults = cls()
        values = {}
        for name, field, default in (
            ("WHISPERSTREAM_VAD_THRESHOLD", "threshold", defaults.threshold),
            ("WHISPERSTREAM_VAD_MIN_SPEECH_MS", "min_speech_ms", defaults.min_speech_ms),
        ):
            raw = os.environ.get(name)
            if raw is None:
                continue
            try:
                value = float(raw)
                cls(**{field: value})
            except ValueError:
                logger.warning("speech_gate setting_invalid field=%s action=use_default", field)
                value = default
            values[field] = value
        enabled = os.environ.get("WHISPERSTREAM_VAD_ENABLED", "1")
        if enabled not in ("0", "1"):
            logger.warning("speech_gate setting_invalid field=enabled action=use_default")
        return cls(enabled=enabled != "0", **values)
