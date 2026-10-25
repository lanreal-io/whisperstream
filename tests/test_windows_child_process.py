"""Owned Windows process lifetime: native setup and real hard-parent exit."""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import psutil
import pytest

from whisperstream import windows_child_process as child_process


def test_atomic_job_attribute_and_noninherited_handle():
    api = MagicMock()
    job = MagicMock()
    job.__int__.return_value = 47
    jobs = MagicMock()
    jobs.QueryInformationJobObject.return_value = {
        "BasicLimitInformation": {"LimitFlags": 0}
    }
    jobs.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    jobs.CreateJobObject.return_value = job
    native_calls = []

    def initialize(buffer, count, flags, size):
        size._obj.value = 128
        return buffer is not None

    def update(buffer, flags, attribute, handles, size, previous, returned):
        assert attribute == 0x2000D
        assert handles[0] == 47
        native_calls.append("assign_at_creation")
        return True

    def create(exe, command, security, thread_security, inherit, flags,
               environment, cwd, startup, info):
        assert inherit is False
        assert flags & 0x80000  # EXTENDED_STARTUPINFO_PRESENT
        assert flags & 0x8000000  # CREATE_NO_WINDOW
        assert startup._obj.lpAttributeList
        assert command.value == 'server.exe "arg with spaces"'
        info._obj.hProcess = 101
        info._obj.hThread = 102
        info._obj.dwProcessId = 103
        native_calls.append("create")
        return True

    api.InitializeProcThreadAttributeList.side_effect = initialize
    api.UpdateProcThreadAttribute.side_effect = update
    api.CreateProcessW.side_effect = create
    with (
        patch.object(child_process, "_kernel_api", return_value=api),
        patch.object(child_process, "win32job", jobs),
        patch.object(child_process, "win32api") as handles,
    ):
        child = child_process.spawn_windows_job_process(
            ["server.exe", "arg with spaces"], cwd=".", env={"Z": "a", "A": "b"}
        )
        handles.SetHandleInformation.assert_called_once_with(job, 1, 0)
        assert jobs.SetInformationJobObject.call_args.args[2][
            "BasicLimitInformation"
        ]["LimitFlags"] == 0x2000
        assert native_calls == ["assign_at_creation", "create"]
        assert child.pid == 103
        job.Close.assert_not_called()
        api.CloseHandle.assert_called_once_with(102)
        api.DeleteProcThreadAttributeList.assert_called_once()
        child.close()
        child.close()
        job.Close.assert_called_once()
        assert api.CloseHandle.call_args.args == (101,)


@pytest.mark.parametrize("failure", ["limits", "attributes", "create"])
def test_setup_failure_closes_job_without_unprotected_fallback(failure):
    api = MagicMock()
    job = MagicMock()
    jobs = MagicMock()
    jobs.CreateJobObject.return_value = job
    jobs.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    jobs.QueryInformationJobObject.return_value = {
        "BasicLimitInformation": {"LimitFlags": 0}
    }

    def initialize(buffer, count, flags, size):
        size._obj.value = 128
        return True

    api.InitializeProcThreadAttributeList.side_effect = initialize
    if failure == "limits":
        jobs.SetInformationJobObject.side_effect = OSError("setup")
    elif failure == "attributes":
        api.UpdateProcThreadAttribute.side_effect = OSError("setup")
    else:
        api.CreateProcessW.side_effect = OSError("setup")
    with (
        patch.object(child_process, "_kernel_api", return_value=api),
        patch.object(child_process, "win32job", jobs),
        patch.object(child_process, "win32api"),
        pytest.raises(OSError),
    ):
        child_process.spawn_windows_job_process(["server.exe"], cwd=".", env={})
    job.Close.assert_called_once()
    if failure != "create":
        api.CreateProcessW.assert_not_called()
    if failure != "limits":
        api.DeleteProcThreadAttributeList.assert_called_once()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object integration")
def test_force_killed_parent_leaves_no_child(tmp_path):
    # Test the production launcher in a disposable backend process. stdout is
    # used only for PID handoff; the job child inherits no pipe/job handles.
    handoff = tmp_path / "owned-pids.json"
    script = '''
import json, os, sys, time
from pathlib import Path
from whisperstream.windows_child_process import spawn_windows_job_process
child = spawn_windows_job_process(
    [sys.executable, '-c', 'import time; time.sleep(120)'],
    cwd=os.getcwd(), env=os.environ,
)
Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid]))
time.sleep(120)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    parent = subprocess.Popen(
        [sys.executable, "-c", script, str(handoff)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    owned = []
    try:
        deadline = time.monotonic() + 15
        while not handoff.exists():
            assert parent.poll() is None, parent.stderr.read().decode(errors="replace")
            assert time.monotonic() < deadline, "parent did not publish child PID"
            time.sleep(0.05)
        parent_pid, child_pid = json.loads(handoff.read_text())
        owned = [psutil.Process(parent_pid), psutil.Process(child_pid)]
        owned.extend(owned[1].children(recursive=True))
        assert owned[1].is_running()
        owned[0].kill()  # TerminateProcess: no Python finally/atexit path.
        owned[0].wait(timeout=5)
        for descendant in owned[1:]:
            descendant.wait(timeout=5)
        parent.wait(timeout=5)
    finally:
        for process in owned:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        if parent.poll() is None:
            parent.kill()
        parent.communicate(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object integration")
@pytest.mark.asyncio
async def test_real_exit_and_explicit_stop_release_handles():
    child = child_process.spawn_windows_job_process(
        [sys.executable, "-c", "raise SystemExit(7)"], cwd=os.getcwd(), env=os.environ
    )
    try:
        assert await asyncio.wait_for(child.wait(), 5) == 7
        assert child._job is None
        assert child._handle is None
    finally:
        child.close()
    child = child_process.spawn_windows_job_process(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=os.getcwd(), env=os.environ,
    )
    try:
        child.terminate()
        assert await asyncio.wait_for(child.wait(), 5) == 1
        assert child._job is None
        assert child._handle is None
    finally:
        child.close()


def test_exit_poll_and_close_serialize_native_handle_ownership():
    import threading

    api = MagicMock()
    job = MagicMock()
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    results = []

    def wait(handle, timeout):
        assert handle == 101
        entered.set()
        assert release.wait(2)
        return 0

    def exit_code(handle, code):
        assert handle == 101
        code._obj.value = 7
        return True

    api.WaitForSingleObject.side_effect = wait
    api.GetExitCodeProcess.side_effect = exit_code
    child = child_process.WindowsJobProcess(api, 101, job, 103)

    def poll():
        try:
            results.append(child.returncode)
        except Exception as exc:
            results.append(exc)

    def close():
        child.close()
        closed.set()

    reader = threading.Thread(target=poll)
    closer = threading.Thread(target=close)
    reader.start()
    try:
        assert entered.wait(2)
        closer.start()
        assert not closed.wait(0.05), "close released a handle still used by exit polling"
    finally:
        release.set()
        reader.join(2)
        if closer.ident is not None:
            closer.join(2)
        child.close()
    assert results == [7]
    assert closed.is_set()
    job.Close.assert_called_once()
    api.CloseHandle.assert_called_once_with(101)
