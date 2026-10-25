import os
import subprocess

import pytest

from whisperstream import macos_user_path as user_path

# The merge runs only on macOS; these cases rely on POSIX absolute paths and
# the ':' PATH separator, which Windows does not have.
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX PATH semantics")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(user_path, "_merged", False)
    monkeypatch.setattr(user_path, "_fallback_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr(user_path.sys, "platform", "darwin")


def _probe_result(stdout):
    return subprocess.CompletedProcess([], 0, stdout, "")


@posix_only
def test_merges_login_shell_path_ahead_of_system_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(
        user_path.subprocess,
        "run",
        lambda *a, **k: _probe_result(
            f"banner{user_path._MARKER}/opt/homebrew/bin:/usr/bin\n{user_path._MARKER}"
        ),
    )

    user_path.ensure_user_path()

    assert os.environ["PATH"].split(os.pathsep) == [
        "/opt/homebrew/bin", "/usr/bin", "/bin", str(tmp_path)
    ]


@posix_only
def test_probe_failure_still_adds_known_install_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SHELL", "/bin/sh")

    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("sh", 5)

    monkeypatch.setattr(user_path.subprocess, "run", fail)

    user_path.ensure_user_path()

    assert os.environ["PATH"].split(os.pathsep) == ["/usr/bin", str(tmp_path)]


@posix_only
def test_unmarked_output_and_relative_entries_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:relative")
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(
        user_path.subprocess, "run", lambda *a, **k: _probe_result("/evil/bin")
    )

    user_path.ensure_user_path()

    assert os.environ["PATH"].split(os.pathsep) == ["/usr/bin", str(tmp_path)]


@posix_only
def test_runs_once_per_process(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SHELL", "/bin/sh")
    calls = []
    monkeypatch.setattr(
        user_path.subprocess, "run",
        lambda *a, **k: calls.append(a) or _probe_result(""),
    )

    user_path.ensure_user_path()
    user_path.ensure_user_path()

    assert len(calls) == 1


def test_other_platforms_leave_path_untouched(monkeypatch):
    monkeypatch.setattr(user_path.sys, "platform", "win32")
    monkeypatch.setenv("PATH", "C:/Windows")

    user_path.ensure_user_path()

    assert os.environ["PATH"] == "C:/Windows"
