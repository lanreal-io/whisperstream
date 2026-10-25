"""Synthetic development process ownership checks; no listeners or processes start."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from scripts import macos_dev_process as owner


def process(pid, created):
    value = MagicMock()
    value.pid = pid
    value.create_time.return_value = created
    value.is_running.return_value = True
    value.children.return_value = []
    value.net_connections.return_value = []
    return value


def test_reused_parent_pid_never_checks_port_or_sends_signal(monkeypatch):
    replaced = process(10, 2.0)
    monkeypatch.setattr(owner.psutil, 'Process', lambda pid: replaced)
    assert not owner.owns_listener(10, 1.0, 38420)
    owner.stop_owned(10, 1.0)
    replaced.net_connections.assert_not_called()
    replaced.terminate.assert_not_called()
    replaced.kill.assert_not_called()


def test_readiness_accepts_only_owned_descendant_listener(monkeypatch):
    root = process(10, 1.0)
    child = process(11, 2.0)
    root.children.return_value = [child]
    monkeypatch.setattr(owner.psutil, 'Process', lambda pid: {10: root, 11: child}[pid])
    assert not owner.owns_listener(10, 1.0, 38420)
    child.net_connections.return_value = [SimpleNamespace(status=owner.psutil.CONN_LISTEN, laddr=SimpleNamespace(port=38420))]
    assert owner.owns_listener(10, 1.0, 38420)
    assert not owner.owns_listener(10, 1.0, 1420)


def test_cleanup_preserves_reused_child_and_settles_original_orphan(monkeypatch):
    root = process(10, 1.0)
    child = process(11, 2.0)
    replaced = process(11, 3.0)
    orphan = process(12, 4.0)
    root.children.return_value = [child, orphan]
    registry = {10: root, 11: child, 12: orphan}
    def terminate_root():
        root.is_running.return_value = False
        registry[11] = replaced
    root.terminate.side_effect = terminate_root
    monkeypatch.setattr(owner.psutil, 'Process', lambda pid: registry[pid])
    monkeypatch.setattr(owner.psutil, 'wait_procs', lambda *args, **kwargs: None)
    owner.stop_owned(10, 1.0)
    root.terminate.assert_called_once()
    replaced.terminate.assert_not_called()
    replaced.kill.assert_not_called()
    orphan.terminate.assert_called_once()
    orphan.kill.assert_called_once()
