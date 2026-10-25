"""Recovery uses synthetic listeners only, never native permission providers."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from fastapi.testclient import TestClient

from whisperstream.shortcut_recovery import ShortcutRecovery


class Keyboard:
    listener_ready = False
    capture_active = False
    granted = False
    held = False
    failed = False

    def ensure_ready(self):
        if not self.granted:
            raise PermissionError()
        if self.held:
            return False
        if self.failed:
            raise RuntimeError()
        self.listener_ready = True
        return True

    def unhook_all(self):
        self.listener_ready = False

    def close(self):
        self.listener_ready = False


class Timer:
    def __init__(self, _delay, callback):
        self.callback = callback
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def tick(self):
        if not self.cancelled:
            self.callback()


def test_grant_recovers_same_config_and_concurrent_checks_do_not_reregister():
    keyboard = Keyboard()
    register = Mock()
    recovery = ShortcutRecovery(keyboard, register, lambda: False)
    assert recovery.reconcile()["state"] == "blocked"
    register.assert_not_called()
    keyboard.granted = True
    with ThreadPoolExecutor(4) as pool:
        assert all(r["state"] == "ready" for r in pool.map(lambda _: recovery.reconcile(), range(8)))
    register.assert_called_once()
    keyboard.granted = False
    assert recovery.reconcile()["state"] == "blocked"
    assert not keyboard.listener_ready
    keyboard.granted = True
    assert recovery.reconcile()["state"] == "ready"
    assert register.call_count == 2


def test_capture_recording_and_held_keys_defer_and_keep_config_dirty():
    keyboard = Keyboard()
    keyboard.granted = True
    busy = False
    register = Mock()
    recovery = ShortcutRecovery(keyboard, register, lambda: busy, timer_factory=Timer)
    assert recovery.reconcile()["state"] == "ready"
    busy = True
    assert recovery.reconcile(force=True)["failure"] == "busy"
    busy = False
    keyboard.capture_active = True
    assert recovery.reconcile()["failure"] == "busy"
    keyboard.capture_active = False
    keyboard.held = True
    assert recovery.reconcile()["failure"] == "busy"
    keyboard.held = False
    recovery._deferred.tick()
    assert recovery.snapshot()["state"] == "ready"
    assert register.call_count == 2


def test_deferred_retry_is_single_owned_timer_and_shutdown_cancels_it():
    keyboard = Keyboard()
    keyboard.granted = True
    keyboard.held = True
    register = Mock()
    recovery = ShortcutRecovery(keyboard, register, lambda: False, timer_factory=Timer)
    recovery.reconcile(force=True)
    timer = recovery._deferred
    recovery.reconcile()
    assert recovery._deferred is timer
    recovery.close()
    assert timer.cancelled
    keyboard.held = False
    timer.tick()
    register.assert_not_called()


def test_failed_registration_cleans_partial_binding_and_can_retry():
    keyboard = Keyboard()
    keyboard.granted = True
    register = Mock(side_effect=[RuntimeError(), None])
    recovery = ShortcutRecovery(keyboard, register, lambda: False)
    assert recovery.reconcile() == {"state": "failed", "failure": "native_error"}
    assert not keyboard.listener_ready
    assert recovery.reconcile()["state"] == "ready"
    keyboard.listener_ready = False
    assert recovery.snapshot()["state"] == "failed"


def test_disabled_and_shutdown_cannot_start_listener():
    keyboard = Keyboard()
    register = Mock()
    recovery = ShortcutRecovery(keyboard, register, lambda: False)
    recovery.enabled = False
    assert recovery.reconcile()["state"] == "disabled"
    recovery.enabled = True
    recovery.close()
    assert recovery.reconcile() == {"state": "disabled", "failure": "shutdown"}
    register.assert_not_called()


def test_api_passive_status_and_explicit_reconcile_with_injected_listener():
    from tests._platform_test_support import _make_server

    server = _make_server()
    keyboard = Keyboard()
    register = Mock()
    recovery = ShortcutRecovery(keyboard, register, lambda: False)
    server._app._shortcut_recovery = recovery
    headers = {"X-Whisperstream-User-Intent": "permission-action-v1"}
    with TestClient(server.fastapi) as client:
        assert client.get("/api/shortcuts/readiness").json()["state"] == "pending"
        register.assert_not_called()
        assert client.post("/api/shortcuts/reconcile").status_code == 403
        assert client.post("/api/shortcuts/reconcile", headers=headers).json()["state"] == "blocked"
        keyboard.granted = True
        assert client.post("/api/shortcuts/reconcile", headers=headers).json()["state"] == "ready"
        keyboard.failed = True
        assert client.post("/api/shortcuts/reconcile", headers=headers).json()["state"] == "failed"
        keyboard.failed = False
        assert client.post("/api/shortcuts/reconcile", headers=headers).json()["state"] == "ready"
    assert server._permission_action_service is None


def test_app_run_survives_missing_access_and_disabled_run_stays_inert():
    from whisperstream.main import WhisperstreamApp

    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app._file_only = False
    app.logger = Mock()
    app.keyboard = Keyboard()
    app.keyboard.wait = Mock()
    register = Mock()
    app._shortcut_recovery = ShortcutRecovery(app.keyboard, register, lambda: False)
    app.run()
    app.keyboard.wait.assert_called_once()
    assert app._shortcut_recovery.snapshot()["state"] == "blocked"
    app.keyboard.granted = True
    assert app._shortcut_recovery.reconcile()["state"] == "ready"
    app.run(register_hotkeys=False)
    assert app._shortcut_recovery.reconcile()["state"] == "disabled"


def test_native_capture_failure_is_reported_without_losing_backend():
    from tests._platform_test_support import _make_server

    server = _make_server()
    keyboard = Keyboard()
    keyboard.capture_shortcut = Mock(side_effect=RuntimeError())
    server._app.keyboard = keyboard
    server._app._shortcut_recovery = ShortcutRecovery(keyboard, Mock(), lambda: False)
    server.broadcast = Mock()
    server._capture_shortcut("primary")
    server.broadcast.assert_called_once_with({"type": "shortcut.capture.failed", "target": "primary"})
    assert server._app._shortcut_recovery.snapshot()["state"] == "failed"
