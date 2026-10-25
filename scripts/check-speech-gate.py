"""Evaluate local speech admission without ASR, text injection or audio storage."""
from __future__ import annotations

import argparse
import time

import numpy as np

from whisperstream.speech_gate import SAMPLE_RATE, SpeechGate, SpeechGateSettings


def diagnose(waveform: np.ndarray, gate: SpeechGate) -> None:
    """Compare admission on identical audio without changing production defaults."""
    original = gate.settings
    try:
        print('Same recording, unchanged audio. Diagnostic values are not saved settings.')
        for threshold in (0.05, 0.1, 0.2, 0.35, 0.5):
            results = []
            for duration in (32, 64, 96):
                gate.settings = SpeechGateSettings(threshold=threshold, min_speech_ms=duration)
                detected = gate.has_speech(waveform)
                if gate.failed:
                    print('diagnostics_unavailable=true detector_failed=true')
                    return
                results.append(f'{duration}ms={str(detected).lower()}')
            print(f'threshold={threshold:g} ' + ' '.join(results))
    finally:
        gate.settings = original


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--wav', help='Existing 16 kHz WAV; never modified')
    source.add_argument('--record-seconds', type=float, help='Record the default microphone into memory only')
    parser.add_argument('--threshold', type=float, default=SpeechGateSettings().threshold)
    parser.add_argument('--min-speech-ms', type=float, default=SpeechGateSettings().min_speech_ms)
    parser.add_argument('--device', type=int, help='Explicit sounddevice input index for recording')
    parser.add_argument('--diagnose', action='store_true', help='Compare five thresholds and three durations on the same audio')
    args = parser.parse_args(argv)
    if args.wav and args.device is not None:
        parser.error('--device applies only to microphone recordings')
    try:
        settings = SpeechGateSettings(threshold=args.threshold, min_speech_ms=args.min_speech_ms)
    except ValueError as exc:
        parser.error(str(exc))
    if args.wav:
        import soundfile as sf
        audio, rate = sf.read(args.wav, dtype='float32', always_2d=True)
        if rate != SAMPLE_RATE:
            parser.error('WAV must use a 16000 Hz sample rate')
        waveform = audio.mean(axis=1)
    else:
        if not np.isfinite(args.record_seconds) or not 0 < args.record_seconds <= 60:
            parser.error('Recording duration must be between zero and 60 seconds')
        import sounddevice as sd
        device = args.device if args.device is not None else int(sd.default.device[0])
        sd.check_input_settings(device=device, channels=1, dtype='float32', samplerate=SAMPLE_RATE)
        print(f'input_device_index={device}', flush=True)
        print('Recording now. Audio stays in memory and is discarded after detection.', flush=True)
        waveform = sd.rec(
            round(args.record_seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
            channels=1, dtype='float32', blocking=True, device=device,
        ).reshape(-1)
    gate = SpeechGate(settings)
    start = time.perf_counter()
    detected = gate.has_speech(waveform)
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(
        f'speech_detected={str(detected).lower()} '
        f'detector_failed={str(gate.failed).lower()} '
        f'threshold={settings.threshold:g} min_speech_ms={settings.min_speech_ms:g} '
        f'audio_ms={waveform.size / SAMPLE_RATE * 1000:.0f} elapsed_ms={elapsed_ms:.1f}'
    )
    if args.diagnose and not gate.failed:
        diagnose(waveform, gate)
    gate.close()
    return 1 if gate.failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
