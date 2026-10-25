"""Managed llama-server lifetime on macOS, independent of backend cleanup.

A separate guardian owns the server's session and the read end of a lifetime
pipe. Only the backend holds its writer. EOF kills the server group even when
native inference is stuck. The guardian never reaps the session leader until
it has killed the group, so its PID cannot be reused during cleanup.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence

_STARTUP_TIMEOUT = 30.0


def _guardian_command() -> list[str]:
    if "__compiled__" in globals() or getattr(sys, "frozen", False):
        executable = os.environ.get("NUITKA_ONEFILE_BINARY")
        if not executable:
            launched = Path(sys.argv[0]).resolve()
            executable = str(launched) if launched.name == "whisperstream-engine" else sys.executable
        return [executable, "--macos-child-guardian"]
    return [sys.executable, str(Path(__file__).resolve().parents[2] / "whisperstream_entry.py"),
            "--macos-child-guardian"]


class MacOSGuardianProcess:
    """Small async Process interface; signals travel through the owned pipe."""

    def __init__(self, guardian: subprocess.Popen):
        self._guardian = guardian
        self._lock = threading.RLock()
        self.pid = 0  # Set only after the guardian has established protection.

    @property
    def returncode(self) -> int | None:
        with self._lock:
            code = self._guardian.poll()
            if code is not None:
                self.close()
            return code

    async def wait(self) -> int:
        while (code := self.returncode) is None:
            await asyncio.sleep(0.01)
        return code

    def terminate(self) -> None:
        with self._lock:
            stream = self._guardian.stdin
            if stream is not None and not stream.closed:
                try:
                    os.write(stream.fileno(), b"T")
                except BrokenPipeError:
                    pass

    def close(self) -> None:
        with self._lock:
            for stream in (self._guardian.stdin, self._guardian.stdout):
                if stream is not None and not stream.closed:
                    stream.close()

    # Closing the lease requests unconditional group kill, never guardian kill.
    kill = close


async def spawn_macos_guardian_process(
    args: Sequence[str], *, cwd: str, env: Mapping[str, str]
) -> MacOSGuardianProcess:
    if sys.platform != "darwin":
        raise RuntimeError("macOS child lifetime protection unavailable")
    # Synchronous creation transfers pipe ownership before any cancellation point.
    # The writer is non-inheritable; later children cannot keep this lease alive.
    guardian = subprocess.Popen(
        [*_guardian_command(), *args], cwd=cwd, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True, bufsize=0,
    )
    process = MacOSGuardianProcess(guardian)
    try:
        os.set_blocking(guardian.stdout.fileno(), False)
        deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT
        reply = b""
        while b"\n" not in reply:
            try:
                chunk = os.read(guardian.stdout.fileno(), 64)
            except BlockingIOError:
                chunk = None
            if chunk == b"" or process.returncode is not None:
                raise RuntimeError("macOS child lifetime protection failed")
            if chunk:
                reply += chunk
            if len(reply) > 32 or asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("macOS child lifetime protection startup failed")
            if b"\n" not in reply:
                await asyncio.sleep(0.01)
        process.pid = int(reply.strip())
        if process.pid <= 0:
            raise RuntimeError("macOS child lifetime protection invalid reply")
        guardian.stdout.close()
        return process
    except BaseException:
        process.close()
        # Shield cleanup from cancellation; the pipe is already closed even if
        # the owner loop stops before the guardian has finished reaping.
        await asyncio.shield(process.wait())
        raise


def _kill_group(child: subprocess.Popen) -> None:
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def guardian_main(args: Sequence[str] | None = None) -> int:
    """Internal engine entry; stdout carries only the protected child's PID."""
    if sys.platform != "darwin":
        return 1
    child = None
    try:
        with closing(select.kqueue()) as events:
            events.control([select.kevent(0, filter=select.KQ_FILTER_READ,
                                          flags=select.KQ_EV_ADD)], 0, 0)
            # No backend lease means no reason to start an expensive model.
            if select.select([0], [], [], 0)[0]:
                return 1
            child = subprocess.Popen(
                list(args if args is not None else sys.argv[1:]),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
            )
            # NOTE_EXIT observes without reaping. In particular do not poll()
            # before killing the group: keep the leader PID reserved throughout.
            try:
                events.control([select.kevent(child.pid, filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD, fflags=select.KQ_NOTE_EXIT)], 0, 0)
            except ProcessLookupError:
                return 1  # Already exited; finally still cleans its descendants.
            os.write(1, f"{child.pid}\n".encode("ascii"))
            while True:
                for event in events.control(None, 2, None):
                    if event.filter == select.KQ_FILTER_PROC:
                        _kill_group(child)
                        code = child.wait()
                        return code if code >= 0 else 128 - code
                    command = os.read(0, 64)
                    if not command or command != b"T":
                        return 0  # EOF or invalid command: unconditional cleanup.
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        return 0
    except Exception:
        # Fail closed; neither command arguments nor paths enter diagnostics.
        return 1
    finally:
        if child is not None:
            if child.returncode is None:
                _kill_group(child)
                child.wait()
