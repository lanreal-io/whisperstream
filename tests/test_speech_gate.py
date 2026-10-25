from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from whisperstream.speech_gate import SpeechGate, SpeechGateSettings

from tests._speech_fixture import requires_speech_fixture, speech_audio

# Short words synthesized with Kokoro (scripts/generate_startup_capture_fixture.py).
# Audio may be absent from a checkout, so the word test skips without it.
STARTUP_WORDS = Path(__file__).parent / 'fixtures' / 'startup-capture' / 'words'
WORD_NAMES = ['one', 'two', 'three', 'four', 'five']


class ProbabilitySession:
    def __init__(self, probabilities):
        self.probabilities = iter(probabilities)
        self.inputs = []

    def run(self, outputs, inputs):
        self.inputs.append({key: value.copy() for key, value in inputs.items()})
        return np.array([[next(self.probabilities)]], dtype=np.float32), inputs['state'] + 1


def make_gate(monkeypatch, probabilities, **settings):
    session = ProbabilitySession(probabilities)
    gate = SpeechGate(SpeechGateSettings(**settings))
    monkeypatch.setattr(gate, '_load_session', lambda: session)
    return gate, session


def audio(frames):
    return np.full(512 * frames, 0.01, dtype=np.float32)


def test_rejects_click_and_disconnected_spikes(monkeypatch):
    gate, session = make_gate(monkeypatch, [0.01, 0.9, 0.01, 0.9, 0.01])
    assert not gate.has_speech(audio(5))
    assert len(session.inputs) == 5


def test_accepts_sustained_speech_and_stops_early(monkeypatch):
    gate, session = make_gate(monkeypatch, [0.1, 0.25, 0.25, 0.25])
    original = audio(20)
    assert gate.has_speech(original)
    assert len(session.inputs) == 4
    np.testing.assert_array_equal(original, audio(20))


def test_threshold_and_minimum_duration_are_tunable(monkeypatch):
    gate, _ = make_gate(monkeypatch, [0.4, 0.4], threshold=0.3, min_speech_ms=64)
    assert gate.has_speech(audio(2))


def test_windows_reset_state_but_frames_keep_context(monkeypatch):
    gate, session = make_gate(monkeypatch, [0.1] * 4)
    assert not gate.has_speech(audio(2))
    assert not gate.has_speech(audio(2))
    for index in (0, 2):
        assert not session.inputs[index]['state'].any()
        assert not session.inputs[index]['input'][0, :64].any()
    assert session.inputs[1]['state'].any()
    np.testing.assert_array_equal(session.inputs[1]['input'][0, :64], audio(1)[-64:])


def test_partial_frame_padding_does_not_count_as_speech(monkeypatch):
    gate, session = make_gate(monkeypatch, [0.9] * 3)
    assert not gate.has_speech(np.full(1025, 0.01, dtype=np.float32))
    assert session.inputs[-1]['input'].shape == (1, 576)
    assert not session.inputs[-1]['input'][0, 65:].any()


def test_zero_samples_need_no_session(monkeypatch):
    gate = SpeechGate(SpeechGateSettings())
    loader = MagicMock(side_effect=AssertionError('unexpected load'))
    monkeypatch.setattr(gate, '_load_session', loader)
    assert not gate.has_speech(np.zeros(16000, dtype=np.float32))
    assert not gate.has_speech(np.empty(0, dtype=np.float32))
    loader.assert_not_called()


def test_failure_falls_back_and_warns_once(monkeypatch, caplog):
    gate = SpeechGate(SpeechGateSettings())
    loader = MagicMock(side_effect=RuntimeError('private path must not be logged'))
    monkeypatch.setattr(gate, '_load_session', loader)
    assert gate.has_speech(audio(5))
    assert gate.has_speech(audio(5))
    loader.assert_called_once()
    assert 'private path' not in caplog.text
    assert caplog.text.count('action=bypass') == 1


def test_nonfinite_probability_does_not_silently_discard_audio(monkeypatch):
    gate, _ = make_gate(monkeypatch, [float('nan')])
    assert gate.has_speech(audio(2))


def test_other_sample_rates_bypass_without_loading(monkeypatch):
    gate = SpeechGate(SpeechGateSettings())
    loader = MagicMock()
    monkeypatch.setattr(gate, '_load_session', loader)
    assert gate.has_speech(audio(2), sample_rate=8000)
    loader.assert_not_called()


@pytest.mark.parametrize('threshold', [0, 1, -1, float('nan'), float('inf')])
def test_rejects_invalid_threshold(threshold):
    with pytest.raises(ValueError):
        SpeechGateSettings(threshold=threshold)


@pytest.mark.parametrize('duration', [0, 31, 1001, float('nan')])
def test_rejects_invalid_duration(duration):
    with pytest.raises(ValueError):
        SpeechGateSettings(min_speech_ms=duration)


def test_environment_validation_uses_safe_defaults(monkeypatch, caplog):
    monkeypatch.setenv('WHISPERSTREAM_VAD_THRESHOLD', 'nan')
    monkeypatch.setenv('WHISPERSTREAM_VAD_MIN_SPEECH_MS', 'private garbage')
    settings = SpeechGateSettings.from_environment()
    assert settings == SpeechGateSettings()
    assert 'private garbage' not in caplog.text


def test_environment_can_disable_for_comparison(monkeypatch):
    monkeypatch.setenv('WHISPERSTREAM_VAD_ENABLED', '0')
    gate = SpeechGate(SpeechGateSettings.from_environment())
    assert gate.has_speech(np.zeros(16000, dtype=np.float32))


def test_pinned_real_model_rejects_silence_and_low_noise():
    gate = SpeechGate(SpeechGateSettings())
    assert not gate.has_speech(np.zeros(16000, dtype=np.float32))
    assert not gate.has_speech(np.random.default_rng(7).normal(0, 0.002, 16000).astype(np.float32))
    assert not gate.failed


@requires_speech_fixture
def test_pinned_real_model_keeps_speech_and_quiet_speech():
    waveform, sample_rate = speech_audio()
    assert sample_rate == 16000
    gate = SpeechGate(SpeechGateSettings())
    assert gate.has_speech(waveform)
    assert gate.has_speech(waveform * 0.05)
    assert not gate.failed


@pytest.mark.skipif(
    not all((STARTUP_WORDS / f'{name}.wav').is_file() for name in WORD_NAMES),
    reason='the startup-capture word clips are not present in this checkout',
)
@pytest.mark.parametrize('name', WORD_NAMES)
def test_pinned_real_model_keeps_short_single_words(name):
    import soundfile as sf
    path = STARTUP_WORDS / f'{name}.wav'
    waveform, sample_rate = sf.read(path, dtype='float32')
    gate = SpeechGate(SpeechGateSettings())
    assert gate.has_speech(waveform, sample_rate=sample_rate)
    assert gate.has_speech(waveform * 0.05, sample_rate=sample_rate)
    assert not gate.failed


def test_corrupt_asset_bypasses_instead_of_discarding(monkeypatch, tmp_path):
    path = tmp_path / 'bad.onnx'
    path.write_bytes(b'not the pinned asset')
    monkeypatch.setattr('whisperstream.speech_gate.MODEL_PATH', path)
    gate = SpeechGate(SpeechGateSettings())
    assert gate.has_speech(audio(3))
    assert gate.failed
