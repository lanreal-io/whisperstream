"""Own one persistent Kokoro child and a serialized, bounded chunk exchange."""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import psutil

from ..stt_worker_protocol import read_frame, write_frame
from .kokoro_worker import MAX_SAMPLES

LOAD_TIMEOUT = 90.0
GENERATE_TIMEOUT = 120.0
MAX_OUTPUT_SAMPLES = 24_000 * 600


def worker_command() -> tuple[str, ...]:
    # Reuse the existing source/onefile executable resolution, including the
    # Nuitka outer-binary identity. No shell, argv text or credentials are used.
    from ..stt_supervisor import default_worker_command
    command = default_worker_command()
    return (*command[:-1], "--kokoro-worker")


class KokoroProcess:
    def __init__(self, directory: Path, threads: int, *, command=None,
                 load_timeout: float = LOAD_TIMEOUT, generate_timeout: float = GENERATE_TIMEOUT):
        self._lock = threading.Lock()
        self._stop_reader = threading.Event()
        self._events = queue.Queue(maxsize=2)
        self._generate_timeout = generate_timeout
        self._closed = False
        self._runtime_identity = None
        self._process = subprocess.Popen(
            command or worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0))
        self._root_identity = psutil.Process(self._process.pid)
        self._root_identity.create_time()
        self._reader = threading.Thread(target=self._read, daemon=True, name="kokoro-protocol")
        self._reader.start()
        try:
            deadline = time.monotonic() + load_timeout
            ready = self._receive(deadline)
            pid = ready.header.get("pid")
            if ready.payload or ready.header.get("event") != "ready" or not isinstance(pid, int):
                raise RuntimeError("Kokoro worker startup contract failed")
            owned = [self._root_identity, *self._root_identity.children(recursive=True)]
            self._runtime_identity = next((child for child in owned if child.pid == pid), None)
            if self._runtime_identity is None:
                raise RuntimeError("Kokoro worker process ownership failed")
            self._runtime_identity.create_time()
            self._send({"operation": "load", "directory": str(directory), "threads": threads})
            event = self._receive(deadline)
            if event.payload or event.header != {"event": "loaded", "sample_rate": 24000, "num_speakers": 54}:
                raise RuntimeError("Kokoro worker model contract failed")
            self.sample_rate = 24_000
            self.num_speakers = 54
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            while not self._stop_reader.is_set():
                event = read_frame(self._process.stdout)
                if len(event.payload) > MAX_SAMPLES * 4:
                    raise ValueError("oversized speech chunk")
                if not self._enqueue(event):
                    return
        except Exception:
            self._enqueue(None)

    def _enqueue(self, event):
        while not self._stop_reader.is_set():
            try:
                self._events.put(event, timeout=0.1)
                return True
            except queue.Full:
                pass
        return False

    def _send(self, header):
        if self._closed:
            raise RuntimeError("Kokoro worker is closed")
        try:
            write_frame(self._process.stdin, header)
        except Exception:
            raise RuntimeError("Kokoro worker transport failed") from None

    def _receive(self, deadline):
        try:
            event = self._events.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            raise RuntimeError("Kokoro worker timed out") from None
        if event is None or event.header.get("event") == "failed":
            raise RuntimeError("Kokoro worker synthesis failed")
        return event

    def generate(self, text, *, sid, speed, callback):
        with self._lock:
            try:
                self._send({"operation": "generate", "text": text, "sid": sid, "speed": speed})
                deadline = time.monotonic() + self._generate_timeout
                chunks = []
                count = 0
                stopped = False
                while True:
                    event = self._receive(deadline)
                    if event.header.get("event") == "done" and not event.payload:
                        break
                    if event.header.get("event") != "chunk" or not event.payload or len(event.payload) % 4:
                        raise RuntimeError("Kokoro worker protocol failed")
                    frames = np.frombuffer(event.payload, dtype="<f4").copy()
                    count += frames.size
                    progress = event.header.get("progress")
                    if (count > MAX_OUTPUT_SAMPLES or not np.isfinite(frames).all()
                            or not isinstance(progress, (int, float)) or not 0 < progress <= 1):
                        raise RuntimeError("Kokoro worker audio contract failed")
                    if stopped:
                        raise RuntimeError("Kokoro worker delivered audio after interruption")
                    chunks.append(frames)
                    # This process facade deliberately exposes sherpa's native
                    # 1=continue convention. The product adapter translates it.
                    stopped = not bool(callback(frames, float(progress)))
                    self._send({"operation": "continue", "continue": not stopped})
                return SimpleNamespace(samples=np.concatenate(chunks) if chunks else np.empty(0, dtype=np.float32))
            except BaseException:
                # Exception, EOF or timeout retires this exact process. Callers
                # retain generation ownership and can use their existing fallback.
                self.close()
                raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop_reader.set()
        process = self._process
        if self._runtime_identity is not None or process.poll() is None:
            # A Nuitka onefile launcher can own a second runtime process. Keep
            # psutil Process identities (which protect against PID reuse), and
            # terminate descendants before the exact launcher we spawned.
            try:
                children = self._root_identity.children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            if self._runtime_identity is not None and self._runtime_identity.pid != process.pid:
                if all(child.pid != self._runtime_identity.pid for child in children):
                    children.append(self._runtime_identity)
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(children, timeout=2.0)
            for child in alive:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(alive, timeout=2.0)
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        self._reader.join(timeout=2.0)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    @property
    def alive(self):
        return not self._closed and self._process.poll() is None
