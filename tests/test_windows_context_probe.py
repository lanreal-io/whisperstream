"""Probe startup failures must release only resources the probe owns."""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("win32gui")
from tests import windows_context_probe as probe


def test_target_failed_startup_closes_spawned_process(tmp_path, monkeypatch):
    child = MagicMock()
    child.poll.return_value = None
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: child)

    def fail_wait(*args, **kwargs):
        raise TimeoutError("synthetic startup failure")

    monkeypatch.setattr(probe, "_wait", fail_wait)
    with pytest.raises(TimeoutError):
        probe._Target(tmp_path, "context")
    assert (tmp_path / "context-command.txt").read_text() == "exit"
    child.wait.assert_called_once_with(timeout=5.0)


def test_browser_failed_spawn_releases_listener(tmp_path, monkeypatch):
    owned_threads = []

    def own_thread(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        owned_threads.append(thread)
        return thread

    # Other test owners can publish or retire workers concurrently. Track the
    # actual probe listener instead of asserting a process-wide thread snapshot.
    monkeypatch.setattr(probe, "threading", SimpleNamespace(Thread=own_thread))
    monkeypatch.setattr(probe, "_edge_path", lambda: tmp_path / "synthetic.exe")

    def fail_spawn(*args, **kwargs):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(probe.subprocess, "Popen", fail_spawn)
    with pytest.raises(OSError):
        probe._probe_browser(tmp_path)
    assert len(owned_threads) == 1
    assert not owned_threads[0].is_alive()
