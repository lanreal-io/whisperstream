#!/usr/bin/env python3
"""Real Windows TTS archive synthesis into a disposable in-memory sink."""

from __future__ import annotations

import hashlib
import json
import platform
import sys

from whisperstream.model_downloader import ModelId, is_present
from whisperstream.speech.engine_specs import ENGINE_SPECS
from whisperstream.speech.selection import EngineManager
from whisperstream.tts_playback_contract import (
    SpeechEngineChoice,
    SpeechRequest,
    SpeechStage,
)
from whisperstream.windows_tts_playback import (
    WindowsSpeechPlaybackAdapter,
    WindowsSpeechSynthesisAdapter,
)


class _DisposableStream:
    def __init__(self, sink: list[int], **_kwargs: object) -> None:
        self._sink = sink

    def start(self) -> None:
        return None

    def write(self, frames) -> None:
        self._sink.append(int(len(frames)))

    def abort(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class _DisposableSoundDevice:
    def __init__(self, sink: list[int]) -> None:
        self._sink = sink

    def OutputStream(self, **kwargs: object) -> _DisposableStream:
        return _DisposableStream(self._sink, **kwargs)

    @staticmethod
    def WasapiSettings(**kwargs: object) -> object:
        return kwargs


def main() -> int:
    if platform.system() != "Windows" or platform.machine().lower() not in {
        "amd64",
        "x86_64",
    }:
        print(json.dumps({"status": "invalid-host"}, sort_keys=True))
        return 2

    installed = [
        key
        for key, spec in ENGINE_SPECS.items()
        if is_present(spec.model_id)
    ]
    if not installed:
        print(
            json.dumps(
                {
                    "status": "not-available",
                    "availability_probe": {
                        ModelId.KOKORO_82M_FP32.value: False,
                        ModelId.SUPERTONIC_3_INT8.value: False,
                    },
                },
                sort_keys=True,
            )
        )
        return 3

    manager = EngineManager()
    synthesis = WindowsSpeechSynthesisAdapter(engine_manager=manager)
    sink: list[int] = []
    playback = WindowsSpeechPlaybackAdapter(
        sounddevice_module=_DisposableSoundDevice(sink),
        platform_provider=lambda: "win32",
        default_output_name=lambda: "Disposable Output",
        wasapi_device_resolver=lambda _name: 1,
        mapper_device_resolver=lambda: None,
    )
    results: list[dict[str, object]] = []
    cleanup_drained = False
    try:
        for generation, key in enumerate(installed, start=1):
            spec = ENGINE_SPECS[key]
            request = SpeechRequest(
                f"real-synthesis-{generation}",
                generation,
                SpeechEngineChoice(key),
                str(spec.default_voice_id),
                1.0,
                0.25,
                "Whisperstream synthesis verification.",
            )
            buffers = list(synthesis.synthesize(request))
            digest = hashlib.sha256(
                b"".join(buffer.pcm_f32le for buffer in buffers)
            ).hexdigest()
            receipt = playback.play(request, buffers)
            if receipt.stage is not SpeechStage.COMPLETED:
                raise RuntimeError("disposable playback did not complete")
            results.append(
                {
                    "engine": key,
                    "buffers": len(buffers),
                    "sample_rate_hz": buffers[0].sample_rate_hz,
                    "pcm_sha256": digest,
                    "frames_written": sink[-1],
                }
            )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "available_engines": installed,
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 1
    finally:
        playback.close()
        cleanup_drained = synthesis.shutdown(10.0)

    if not cleanup_drained or manager._leases:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "available_engines": installed,
                    "error_type": "CleanupIncomplete",
                },
                sort_keys=True,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "status": "pass",
                "available_engines": installed,
                "results": results,
                "sink": "disposable-memory",
                "cleanup_drained": cleanup_drained,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
