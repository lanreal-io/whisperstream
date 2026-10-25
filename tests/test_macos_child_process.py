"""Native lifetime tests use disposable owners and children, never installed apps."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import AsyncMock, patch

import psutil
import pytest

from whisperstream import macos_child_process as managed

NATIVE = pytest.mark.skipif(sys.platform != "darwin", reason="macOS native guardian")


def _gone(pid):
    return not psutil.pid_exists(pid)


def _eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


@pytest.mark.asyncio
async def test_macos_dispatch_is_protected_and_fails_closed():
    from whisperstream import llama_server
    with (patch.object(llama_server.os, "name", "posix"),
          patch.object(llama_server.sys, "platform", "darwin"),
          patch.object(managed, "spawn_macos_guardian_process", new_callable=AsyncMock) as spawn,
          patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as ordinary):
        result = await llama_server._create_subprocess_exec("server", cwd="runtime", env={})
        assert result is spawn.return_value
        spawn.assert_awaited_once_with(("server",), cwd="runtime", env={})
        spawn.side_effect = OSError("guard unavailable")
        with pytest.raises(OSError):
            await llama_server._create_subprocess_exec("server", cwd="runtime", env={})
        ordinary.assert_not_awaited()


def test_frozen_command_uses_engine_without_python(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("NUITKA_ONEFILE_BINARY", str(tmp_path / "whisperstream-engine"))
    assert managed._guardian_command() == [str(tmp_path / "whisperstream-engine"), "--macos-child-guardian"]
    monkeypatch.delenv("NUITKA_ONEFILE_BINARY")
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "whisperstream-engine")])
    assert managed._guardian_command() == [str(tmp_path / "whisperstream-engine"), "--macos-child-guardian"]


@NATIVE
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["terminate", "kill", "natural"])
async def test_stop_and_child_exit_reap_guardian(tmp_path, action):
    finish = tmp_path / "finish"
    code = (f"import time,pathlib; p=pathlib.Path({str(finish)!r})\nwhile not p.exists(): time.sleep(0.01)"
            if action == "natural" else "import time; time.sleep(60)")
    process = await managed.spawn_macos_guardian_process(
        [sys.executable, "-c", code], cwd=str(tmp_path), env=os.environ.copy())
    assert psutil.Process(process.pid).ppid() == process._guardian.pid
    if action != "natural":
        getattr(process, action)()
    else:
        finish.touch()
    await asyncio.wait_for(process.wait(), 5)
    assert _gone(process.pid) and _gone(process._guardian.pid)
    assert process._guardian.stdin.closed and process._guardian.stdout.closed


@NATIVE
@pytest.mark.asyncio
async def test_ignored_term_escalates_via_pipe(tmp_path):
    ready = tmp_path / "ready"
    code = f"import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    process = await managed.spawn_macos_guardian_process(
        [sys.executable, "-c", code], cwd=str(tmp_path), env=os.environ.copy())
    try:
        _eventually(ready.exists)
        process.terminate()
        await asyncio.sleep(0.1)
        assert process.returncode is None
    finally:
        process.kill()
        await asyncio.wait_for(process.wait(), 5)
    assert _gone(process.pid)


@NATIVE
@pytest.mark.asyncio
async def test_missing_binary_leaves_no_guardian(tmp_path, monkeypatch):
    children = []
    original = subprocess.Popen
    def capture(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(managed.subprocess, "Popen", capture)
    with pytest.raises(RuntimeError, match="protection failed"):
        await managed.spawn_macos_guardian_process(
            [str(tmp_path / "missing")], cwd=str(tmp_path), env=os.environ.copy())
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdin.closed and children[0].stdout.closed


@NATIVE
@pytest.mark.parametrize("death", ["sigkill", "crash"])
def test_abrupt_owner_death_kills_children_and_guardian(tmp_path, death):
    manifest = tmp_path / "children.json"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os,sys,json,subprocess,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"open({str(manifest)!r},'w').write(json.dumps([os.getpid(),child.pid]))\n"
        "time.sleep(60)\n")
    owner_script = tmp_path / "owner.py"
    owner_script.write_text(
        "import asyncio,json,os,sys,time\n"
        "from whisperstream.macos_child_process import spawn_macos_guardian_process\n"
        "async def main():\n"
        f" p=await spawn_macos_guardian_process([sys.executable,{str(child_script)!r}],cwd={str(tmp_path)!r},env=os.environ.copy())\n"
        " print(json.dumps([p.pid,p._guardian.pid]),flush=True)\n"
        " await asyncio.sleep(0.3)\n"
        + (" os._exit(17)\n" if death == "crash" else " await asyncio.sleep(60)\n")
        + "asyncio.run(main())\n")
    owner = subprocess.Popen([sys.executable, str(owner_script)], stdout=subprocess.PIPE, text=True)
    pids = []
    try:
        pids = json.loads(owner.stdout.readline())
        _eventually(manifest.exists)
        descendants = json.loads(manifest.read_text())
        assert descendants[0] == pids[0]
        pids += descendants[1:]
        if death == "sigkill":
            owner.kill()
        owner.wait(timeout=5)
        _eventually(lambda: all(_gone(pid) for pid in pids))
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        owner.stdout.close()
        for pid in pids:
            if psutil.pid_exists(pid):
                os.kill(pid, signal.SIGKILL)


@NATIVE
@pytest.mark.asyncio
async def test_cancel_startup_closes_lease_and_reaps(tmp_path, monkeypatch):
    # Pause handshake polling after the real guardian has spawned its child.
    real_read = os.read
    captured = []
    original = subprocess.Popen
    def capture(*args, **kwargs):
        guardian = original(*args, **kwargs)
        captured.append(guardian)
        return guardian
    def stalled_read(fd, size):
        if captured and fd == captured[0].stdout.fileno():
            raise BlockingIOError()
        return real_read(fd, size)
    monkeypatch.setattr(managed.subprocess, "Popen", capture)
    monkeypatch.setattr(managed.os, "read", stalled_read)
    task = asyncio.create_task(managed.spawn_macos_guardian_process(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=str(tmp_path), env=os.environ.copy()))
    deadline = time.monotonic() + 5
    pids = []
    while not pids and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
        if captured:
            pids = [p.pid for p in psutil.Process(captured[0].pid).children(recursive=True)]
    assert pids
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert captured[0].poll() is not None
    assert all(_gone(pid) for pid in pids)


@NATIVE
@pytest.mark.parametrize("stage", ["before_spawn", "before_handshake"])
def test_owner_killed_during_startup_leaves_no_processes(tmp_path, stage):
    marker = tmp_path / "child.pid"
    child_code = f"import os,pathlib,time; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
    owner_code = f'''
import asyncio,os,sys
from whisperstream import macos_child_process as managed
if {stage!r} == "before_spawn":
    managed._guardian_command = lambda: [sys.executable, "-c", "import time,sys; time.sleep(0.4); from whisperstream.macos_child_process import guardian_main; raise SystemExit(guardian_main(sys.argv[1:]))"]
else:
    original = os.read
    def read(fd, size):
        if size == 64:
            raise BlockingIOError()
        return original(fd, size)
    managed.os.read = read
asyncio.run(managed.spawn_macos_guardian_process([sys.executable,"-c",{child_code!r}],cwd={str(tmp_path)!r},env=os.environ.copy()))
'''
    owner = subprocess.Popen([sys.executable, "-c", owner_code])
    pids = []
    try:
        if stage == "before_handshake":
            _eventually(marker.exists)
        _eventually(lambda: bool(psutil.Process(owner.pid).children()))
        pids = [p.pid for p in psutil.Process(owner.pid).children(recursive=True)]
        owner.kill()
        owner.wait(timeout=5)
        _eventually(lambda: all(_gone(pid) for pid in pids))
        if stage == "before_spawn":
            assert not marker.exists()
        else:
            assert _gone(int(marker.read_text()))
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        for pid in pids:
            if psutil.pid_exists(pid):
                os.kill(pid, signal.SIGKILL)


@NATIVE
@pytest.mark.asyncio
async def test_natural_child_exit_kills_remaining_descendants(tmp_path):
    marker = tmp_path / "descendant.pid"
    code = f'''import subprocess,sys,pathlib,time
child = subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])
pathlib.Path({str(marker)!r}).write_text(str(child.pid))
time.sleep(0.2)
'''
    process = await managed.spawn_macos_guardian_process(
        [sys.executable, "-c", code], cwd=str(tmp_path), env=os.environ.copy())
    try:
        await asyncio.wait_for(process.wait(), 5)
        _eventually(lambda: _gone(int(marker.read_text())))
    finally:
        process.kill()
        await process.wait()


@NATIVE
@pytest.mark.asyncio
async def test_startup_timeout_kills_child(tmp_path, monkeypatch):
    monkeypatch.setattr(managed, "_STARTUP_TIMEOUT", 1.0)
    real_read = os.read
    def stalled_read(fd, size):
        if size == 64:
            raise BlockingIOError()
        return real_read(fd, size)
    monkeypatch.setattr(managed.os, "read", stalled_read)
    marker = tmp_path / "child.pid"
    code = f"import os,pathlib,time; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
    with pytest.raises(RuntimeError, match="startup failed"):
        await managed.spawn_macos_guardian_process(
            [sys.executable, "-c", code], cwd=str(tmp_path), env=os.environ.copy())
    assert marker.exists() and _gone(int(marker.read_text()))
