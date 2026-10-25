"""Static speech-engine metadata without importing either TTS runtime.

API endpoints and selection policy use this module for stable engine and voice
identities. Heavy construction remains behind
:func:`whisperstream.speech.engines.make_engine`.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, TypeAlias

from ..model_downloader import ModelId

__all__ = [
    "DEFAULT_SID",
    "DEFAULT_TTS_THREADS",
    "ENGINE_KEY",
    "ENGINE_LABEL",
    "ENGINE_SPECS",
    "EngineSpec",
    "KOKORO_SPEC",
    "MODEL_ID",
    "SpeechInputError",
    "SUPERTONIC_SPEC",
    "VOICES",
    "VoiceGroup",
    "VoiceId",
    "VoiceInfo",
    "clamp_sid",
    "get_engine_spec",
]

DEFAULT_TTS_THREADS = 2

VoiceId: TypeAlias = int | str
VoiceGroup: TypeAlias = Literal["female", "male"]


class SpeechInputError(ValueError):
    """One utterance cannot be spoken, without implying engine corruption."""


@dataclass(frozen=True)
class VoiceInfo:
    """One selectable speaker with a stable API identity and display label."""

    id: VoiceId
    label: str
    group: VoiceGroup
    reference_label: str | None = None

    @property
    def sid(self) -> VoiceId:
        """Legacy alias retained while Supertonic API consumers migrate."""

        return self.id


@dataclass(frozen=True)
class EngineSpec:
    """Cheap immutable metadata for one concrete speech engine."""

    key: str
    model_id: ModelId
    label: str
    voices: tuple[VoiceInfo, ...]
    default_voice_id: VoiceId
    sample_rate: int

    def normalize_voice_id(self, value: object) -> VoiceId:
        """Return a supported voice identity or this engine's default."""

        return (
            value
            if any(voice.id == value for voice in self.voices)
            else self.default_voice_id
        )


SUPERTONIC_SPEC = EngineSpec(
    key="supertonic",
    model_id=ModelId.SUPERTONIC_3_INT8,
    label="Supertonic 3",
    voices=(
        VoiceInfo(0, "Ava", "female", "Voice 0"),
        VoiceInfo(1, "Brianna", "female", "Voice 1"),
        VoiceInfo(2, "Claire", "female", "Voice 2"),
        VoiceInfo(3, "Danielle", "female", "Voice 3"),
        VoiceInfo(4, "Eleanor", "female", "Voice 4"),
        VoiceInfo(5, "Frank", "male", "Voice 5"),
        VoiceInfo(6, "George", "male", "Voice 6"),
        VoiceInfo(7, "Henry", "male", "Voice 7"),
        VoiceInfo(8, "Isaiah", "male", "Voice 8"),
        VoiceInfo(9, "James", "male", "Voice 9"),
    ),
    default_voice_id=1,
    sample_rate=44_100,
)

KOKORO_SPEC = EngineSpec(
    key="kokoro",
    model_id=ModelId.KOKORO_82M_FP32,
    label="Kokoro",
    voices=(
        VoiceInfo("af_heart", "Heart", "female"),
        VoiceInfo("af_bella", "Bella", "female"),
        VoiceInfo("af_nicole", "Nicole", "female"),
        VoiceInfo("af_aoede", "Aoede", "female"),
        VoiceInfo("af_kore", "Kore", "female"),
        VoiceInfo("af_sarah", "Sarah", "female"),
        VoiceInfo("am_fenrir", "Fenrir", "male"),
        VoiceInfo("am_michael", "Michael", "male"),
        VoiceInfo("am_puck", "Puck", "male"),
    ),
    default_voice_id="af_heart",
    sample_rate=24_000,
)

ENGINE_SPECS = MappingProxyType(
    {
        SUPERTONIC_SPEC.key: SUPERTONIC_SPEC,
        KOKORO_SPEC.key: KOKORO_SPEC,
    }
)


def get_engine_spec(engine_key: str) -> EngineSpec:
    """Return metadata for *engine_key*, rejecting unknown external input."""

    from .availability import require_engine_available
    require_engine_available(engine_key)
    try:
        return ENGINE_SPECS[engine_key]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unknown speech engine: {engine_key!r}") from exc


# Backward-compatible Supertonic constants for the current controller and API.
# New multi-engine code should consume ``SUPERTONIC_SPEC`` / ``ENGINE_SPECS``.
ENGINE_KEY = SUPERTONIC_SPEC.key
MODEL_ID = SUPERTONIC_SPEC.model_id
ENGINE_LABEL = SUPERTONIC_SPEC.label
VOICES = SUPERTONIC_SPEC.voices
DEFAULT_SID = int(SUPERTONIC_SPEC.default_voice_id)


def clamp_sid(sid: int) -> int:
    """Return a valid Supertonic speaker ID or the product default."""

    normalized = SUPERTONIC_SPEC.normalize_voice_id(sid)
    return int(normalized)
