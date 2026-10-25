#!/usr/bin/env python3
"""Run the pinned public Kokoro engine into a disposable fileless sink on Mac."""

from __future__ import annotations

import hashlib
import json
import platform

from whisperstream.macos_tts_playback import (
    MacOSSpeechPlaybackAdapter,
    MacOSSpeechSynthesisAdapter,
)
from whisperstream.model_downloader import ModelId, is_present
from whisperstream.speech.engine_specs import KOKORO_SPEC
from whisperstream.speech.selection import EngineManager
from whisperstream.tts_playback_contract import (
    SpeechEngineChoice,
    SpeechRequest,
    SpeechStage,
)


class _DisposableStream:
    def __init__(self, sink: list[int], **_kwargs: object) -> None:
        self._sink = sink

    def start(self) -> None:
        return None

    def write(self, frames: object) -> None:
        self._sink.append(int(len(frames)))  # type: ignore[arg-type]

    def abort(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class _DisposableSoundDevice:
    def __init__(self, sink: list[int]) -> None:
        self._sink = sink

    def OutputStream(self, **kwargs: object) -> _DisposableStream:  # noqa: N802
        return _DisposableStream(self._sink, **kwargs)


def main() -> int:
    if platform.system() != "Darwin" or platform.machine().lower() not in {
        "arm64",
        "aarch64",
    }:
        print(json.dumps({"status": "invalid-host"}, sort_keys=True))
        return 2
    if not is_present(ModelId.KOKORO_82M_FP32):
        print(json.dumps({"status": "not-available", "engine": "kokoro"}, sort_keys=True))
        return 3

    manager = EngineManager()
    synthesis = MacOSSpeechSynthesisAdapter(engine_manager=manager)
    sink: list[int] = []
    playback = MacOSSpeechPlaybackAdapter(
        sounddevice_module=_DisposableSoundDevice(sink),
        current_output_resolver=lambda: 1,
    )
    cleaned = False
    try:
        pcm_digest = hashlib.sha256()
        buffers_completed = 0
        for generation, voice in enumerate(KOKORO_SPEC.voices, start=1):
            request = SpeechRequest(
                f"fixed-public-kokoro-{generation}",
                generation,
                SpeechEngineChoice.KOKORO,
                str(voice.id),
                1.0,
                0.25,
                "Whisperstream synthesis verification.",
            )
            buffers = list(synthesis.synthesize(request))
            receipt = playback.play(request, buffers)
            if receipt.stage is not SpeechStage.COMPLETED:
                raise RuntimeError("fileless playback did not complete")
            for buffer in buffers:
                pcm_digest.update(buffer.pcm_f32le)
            buffers_completed += len(buffers)
        report = {
            "status": "pass",
            "engine": "kokoro",
            "voices": len(KOKORO_SPEC.voices),
            "buffers": buffers_completed,
            "sample_rate_hz": KOKORO_SPEC.sample_rate,
            "pcm_sha256": pcm_digest.hexdigest(),
            "frames_written": sum(sink),
        }
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "engine": "kokoro", "error_type": type(exc).__name__},
                sort_keys=True,
            )
        )
        return 1
    finally:
        playback.close()
        cleaned = synthesis.shutdown(10.0)
    if not cleaned or manager._leases:  # noqa: SLF001 - exact cleanup probe
        print(json.dumps({"status": "failed", "error_type": "CleanupIncomplete"}, sort_keys=True))
        return 1
    print(json.dumps({**report, "cleanup_drained": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
