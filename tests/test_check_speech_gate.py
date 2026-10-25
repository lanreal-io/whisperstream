import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

from whisperstream.speech_gate import SpeechGateSettings


spec = importlib.util.spec_from_file_location(
    'check_speech_gate', Path(__file__).parents[1] / 'scripts/check-speech-gate.py'
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_scan_uses_identical_audio_and_restores_settings(capsys):
    waveform = np.ones(512, dtype=np.float32)
    calls = []
    settings = SpeechGateSettings(threshold=0.35, min_speech_ms=64)
    gate = SimpleNamespace(settings=settings, failed=False)
    def detect(audio):
        assert audio is waveform
        calls.append((gate.settings.threshold, gate.settings.min_speech_ms))
        return gate.settings.threshold < 0.2
    gate.has_speech = detect
    checker.diagnose(waveform, gate)
    assert len(calls) == len(set(calls)) == 15
    assert gate.settings is settings
    output = capsys.readouterr().out
    assert 'threshold=0.1 32ms=true 64ms=true 96ms=true' in output
    assert 'threshold=0.5 32ms=false 64ms=false 96ms=false' in output


def test_detector_failure_is_not_reported_as_speech(capsys):
    gate = SimpleNamespace(settings=SpeechGateSettings(), failed=False)
    def fail(audio):
        gate.failed = True
        return True
    gate.has_speech = fail
    checker.diagnose(np.ones(512, dtype=np.float32), gate)
    output = capsys.readouterr().out
    assert 'diagnostics_unavailable=true' in output
    assert '32ms=true' not in output


def test_explicit_microphone_is_used_without_storage(monkeypatch, capsys):
    import sounddevice as sd
    recorder = MagicMock(return_value=np.zeros((16000, 1), dtype=np.float32))
    validation = MagicMock()
    monkeypatch.setattr(sd, 'rec', recorder)
    monkeypatch.setattr(sd, 'check_input_settings', validation)
    assert checker.main(['--record-seconds', '1', '--device', '7', '--diagnose']) == 0
    assert recorder.call_args.kwargs['device'] == 7
    assert validation.call_args.kwargs['device'] == 7
    output = capsys.readouterr().out
    assert 'input_device_index=7' in output
    assert f'threshold={SpeechGateSettings().threshold:g} min_speech_ms=96' in output
