"""Tests for the static, sherpa-free Supertonic speech metadata boundary."""

from __future__ import annotations

import subprocess
import sys

import pytest

from whisperstream.model_downloader import ModelId
from whisperstream.speech.engine_specs import (
    DEFAULT_SID,
    ENGINE_SPECS,
    ENGINE_KEY,
    ENGINE_LABEL,
    KOKORO_SPEC,
    MODEL_ID,
    SUPERTONIC_SPEC,
    VOICES,
    clamp_sid,
    get_engine_spec,
)


def test_static_supertonic_metadata() -> None:
    assert ENGINE_KEY == "supertonic"
    assert MODEL_ID is ModelId.SUPERTONIC_3_INT8
    assert ENGINE_LABEL == "Supertonic 3"
    assert DEFAULT_SID == 1
    assert [
        (voice.sid, voice.label, voice.group, voice.reference_label)
        for voice in VOICES
    ] == [
        (0, "Ava", "female", "Voice 0"),
        (1, "Brianna", "female", "Voice 1"),
        (2, "Claire", "female", "Voice 2"),
        (3, "Danielle", "female", "Voice 3"),
        (4, "Eleanor", "female", "Voice 4"),
        (5, "Frank", "male", "Voice 5"),
        (6, "George", "male", "Voice 6"),
        (7, "Henry", "male", "Voice 7"),
        (8, "Isaiah", "male", "Voice 8"),
        (9, "James", "male", "Voice 9"),
    ]
    assert [type(voice.id) for voice in VOICES] == [int] * 10
    assert SUPERTONIC_SPEC.normalize_voice_id("Ava") == DEFAULT_SID


def test_clamp_sid_preserves_valid_speakers_and_falls_back_to_default() -> None:
    assert clamp_sid(0) == 0
    assert clamp_sid(9) == 9
    assert clamp_sid(-1) == DEFAULT_SID
    assert clamp_sid(10) == DEFAULT_SID


def test_keyed_engine_registry_has_stable_voice_ids_and_labels() -> None:
    assert tuple(ENGINE_SPECS) == ("supertonic", "kokoro")
    assert get_engine_spec("supertonic") is SUPERTONIC_SPEC
    assert get_engine_spec("kokoro") is KOKORO_SPEC
    assert SUPERTONIC_SPEC.default_voice_id == 1
    assert KOKORO_SPEC.model_id is ModelId.KOKORO_82M_FP32
    assert KOKORO_SPEC.default_voice_id == "af_heart"
    assert [
        (voice.id, voice.label, voice.group, voice.reference_label)
        for voice in KOKORO_SPEC.voices
    ] == [
        ("af_heart", "Heart", "female", None),
        ("af_bella", "Bella", "female", None),
        ("af_nicole", "Nicole", "female", None),
        ("af_aoede", "Aoede", "female", None),
        ("af_kore", "Kore", "female", None),
        ("af_sarah", "Sarah", "female", None),
        ("am_fenrir", "Fenrir", "male", None),
        ("am_michael", "Michael", "male", None),
        ("am_puck", "Puck", "male", None),
    ]
    assert [type(voice.id) for voice in KOKORO_SPEC.voices] == [str] * 9
    assert KOKORO_SPEC.normalize_voice_id("af_bella") == "af_bella"
    assert KOKORO_SPEC.normalize_voice_id("missing") == "af_heart"
    assert KOKORO_SPEC.normalize_voice_id("Heart") == "af_heart"


def test_unknown_engine_spec_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown speech engine"):
        get_engine_spec("unknown")


def test_engine_factory_keeps_supertonic_heavy_import_lazy() -> None:
    code = """
import sys
import whisperstream.speech.engines as engines
assert "whisperstream.speech.supertonic_engine" not in sys.modules
engine = engines.make_engine()
assert type(engine).__name__ == "SupertonicEngine"
assert engine._num_threads == 2
assert "whisperstream.speech.supertonic_engine" in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_engine_factory_is_keyed_and_keeps_both_implementations_lazy() -> None:
    code = """
import sys
import whisperstream.speech.engines as engines
assert "whisperstream.speech.supertonic_engine" not in sys.modules
assert "whisperstream.speech.kokoro_engine" not in sys.modules
engine = engines.make_engine("kokoro")
assert type(engine).__name__ == "KokoroEngine"
assert engine._num_threads == 2
assert "whisperstream.speech.kokoro_engine" in sys.modules
assert "whisperstream.speech.supertonic_engine" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_engine_factory_rejects_unknown_engine_without_heavy_import() -> None:
    code = """
import sys
import whisperstream.speech.engines as engines
try:
    engines.make_engine("unknown")
except ValueError as exc:
    assert "unknown speech engine" in str(exc)
else:
    raise AssertionError("unknown engine did not fail")
assert "whisperstream.speech.supertonic_engine" not in sys.modules
assert "whisperstream.speech.kokoro_engine" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture(autouse=True)
def experimental_voice_runtime(monkeypatch):
    """These cases exercise the retained experimental Kokoro implementation."""
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
