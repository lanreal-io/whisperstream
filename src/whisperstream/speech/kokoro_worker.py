"""Private Kokoro process: native diagnostics never reach the backend console.

Only bounded JSON metadata and float32 chunks use stdout. Text and phonemes
stay in process memory; no diagnostics, traceback text or input is forwarded.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys

import numpy as np

from ..stt_worker_protocol import read_frame, write_frame, SttWorkerEof

MAX_SAMPLES = 2_000_000


def run_worker(input_stream, output_stream, *, runtime_factory=None) -> int:
    """Serve one owned parent; errors expose only a fixed failure classification."""
    runtime = None
    try:
        write_frame(output_stream, {"event": "ready", "pid": os.getpid()})
        while True:
            frame = read_frame(input_stream)
            operation = frame.header.get("operation")
            if frame.payload:
                raise ValueError("unexpected request payload")
            if operation == "shutdown":
                return 0
            if operation == "load" and runtime is None:
                directory = Path(frame.header["directory"])
                threads = frame.header["threads"]
                if not isinstance(threads, int) or isinstance(threads, bool) or not 1 <= threads <= 64:
                    raise ValueError("invalid thread count")
                runtime = (runtime_factory or _load_native)(directory, threads)
                write_frame(output_stream, {"event": "loaded", "sample_rate": runtime.sample_rate,
                                            "num_speakers": runtime.num_speakers})
            elif operation == "generate" and runtime is not None:
                def callback(samples, progress):
                    samples = np.asarray(samples, dtype="<f4").reshape(-1)
                    if not 0 < samples.size <= MAX_SAMPLES or not np.isfinite(samples).all():
                        raise ValueError("invalid native output")
                    write_frame(output_stream, {"event": "chunk", "progress": float(progress)},
                                samples.tobytes())
                    ack = read_frame(input_stream)
                    if ack.payload or ack.header.get("operation") != "continue":
                        raise ValueError("invalid callback acknowledgement")
                    return 1 if ack.header.get("continue") is True else 0
                runtime.generate(frame.header["text"], sid=frame.header["sid"],
                                 speed=frame.header["speed"], callback=callback)
                write_frame(output_stream, {"event": "done"})
            else:
                raise ValueError("invalid worker operation")
    except SttWorkerEof:
        return 0
    except Exception:
        try:
            write_frame(output_stream, {"event": "failed"})
        except Exception:
            pass
        return 1


def _load_native(directory: Path, threads: int):
    from ..model_sources import MODEL_SOURCES
    from ..model_downloader import ModelId
    from .kokoro_engine import resolve_model_files
    files = resolve_model_files(directory)
    # Native libraries may exit on malformed models/data. Validate every pinned
    # byte in this isolated child before native construction.
    for name, pin in MODEL_SOURCES[ModelId.KOKORO_82M_FP32.value]["files"].items():
        path = directory / name
        if path.stat().st_size != pin["size"]:
            raise ValueError("model size mismatch")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != pin["sha256"]:
                raise ValueError("model hash mismatch")
    import sherpa_onnx as sherpa
    kokoro = sherpa.OfflineTtsKokoroModelConfig(
        model=str(files.model), voices=str(files.voices), tokens=str(files.tokens),
        lexicon=str(files.lexicon), data_dir=str(files.data_dir), lang="en-us")
    config = sherpa.OfflineTtsConfig(model=sherpa.OfflineTtsModelConfig(
        kokoro=kokoro, num_threads=threads, provider="cpu", debug=False), max_num_sentences=1)
    return sherpa.OfflineTts(config)


def main() -> int:
    # Duplicate the protocol output before discarding *native* fd 1/2. Python
    # redirect_stderr alone cannot intercept C++ diagnostics. This change affects
    # only the owned child, never the long-lived backend or another worker.
    protocol_output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    with protocol_output, open(os.devnull, "wb") as null:
        os.dup2(null.fileno(), 1)
        os.dup2(null.fileno(), 2)
        return run_worker(sys.stdin.buffer, protocol_output)
