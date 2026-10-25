"""Native probe permission policy with injected host and permission observations."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests import macos_native_interaction_probe as probe
from whisperstream.permission_contract import (
    PermissionKind,
    PermissionReport,
    PermissionSnapshot,
    PermissionState,
)


@pytest.fixture
def native_host(monkeypatch):
    monkeypatch.setattr(probe.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(probe.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        probe.subprocess, "run", Mock(return_value=SimpleNamespace(stdout="0\n"))
    )
    provider = Mock()
    monkeypatch.setattr(probe, "MacosPermissionStatusProvider", lambda: provider)
    return provider


def _report(accessibility, input_monitoring):
    states = {
        PermissionKind.MICROPHONE: PermissionState.DENIED,
        PermissionKind.ACCESSIBILITY: accessibility,
        PermissionKind.INPUT_MONITORING: input_monitoring,
    }
    return PermissionReport(
        platform="macos",
        permissions=tuple(
            PermissionSnapshot(
                kind=kind,
                state=state,
                query_supported=True,
                request_supported=False,
            )
            for kind, state in states.items()
        ),
    )


@pytest.mark.parametrize("input_monitoring", list(PermissionState))
def test_active_tap_requires_accessibility_and_preserves_other_states(
    native_host, input_monitoring
):
    native_host.query_permissions.return_value = _report(
        PermissionState.GRANTED, input_monitoring
    )

    assert probe._require_native_permissions() == {
        "microphone": "denied",
        "accessibility": "granted",
        "input-monitoring": input_monitoring.value,
    }
    native_host.query_permissions.assert_called_once_with()
    assert native_host.method_calls == [("query_permissions", (), {})]


@pytest.mark.parametrize(
    "accessibility",
    [state for state in PermissionState if state is not PermissionState.GRANTED],
)
def test_input_monitoring_grant_does_not_replace_accessibility(
    native_host, accessibility
):
    native_host.query_permissions.return_value = _report(
        accessibility, PermissionState.GRANTED
    )

    with pytest.raises(
        probe.NativeInteractionProbeError,
        match="required permission is not granted kind=accessibility",
    ):
        probe._require_native_permissions()


def test_translated_host_rejected_before_permission_query(native_host, monkeypatch):
    monkeypatch.setattr(
        probe.subprocess, "run", Mock(return_value=SimpleNamespace(stdout="1\n"))
    )

    with pytest.raises(probe.NativeInteractionProbeError, match="native Darwin arm64"):
        probe._require_native_permissions()

    native_host.query_permissions.assert_not_called()


@pytest.mark.parametrize("receipt_state", ["started", "active"])
def test_launch_failure_cleans_early_receipt_identity(monkeypatch, tmp_path, receipt_state):
    broker = Mock()
    broker.poll.return_value = None
    monkeypatch.setattr(probe.subprocess, "Popen", Mock(return_value=broker))
    process = Mock()
    process.create_time.return_value = 12.5
    monkeypatch.setattr(probe.psutil, "Process", Mock(return_value=process))
    receipt = tmp_path / "target.receipt"
    receipt.write_text(f"{receipt_state}\n123\nowned-nonce\n")

    def fail_activation(callback, **kwargs):
        callback()
        raise probe.NativeInteractionProbeError("activation timeout")

    monkeypatch.setattr(probe, "_wait", fail_activation)
    with pytest.raises(probe.NativeInteractionProbeError, match="activation timeout"):
        probe._launch_app(tmp_path / "Target.app", receipt=receipt,
                          nonce="owned-nonce", mode="target")
    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5.0)
    broker.terminate.assert_called_once_with()
    broker.wait.assert_called_once_with(timeout=5.0)


def test_launch_failure_does_not_stop_recycled_pid(monkeypatch, tmp_path):
    broker = Mock()
    broker.poll.return_value = None
    monkeypatch.setattr(probe.subprocess, "Popen", Mock(return_value=broker))
    original, replacement = Mock(), Mock()
    original.create_time.return_value = 12.5
    replacement.create_time.return_value = 19.0
    monkeypatch.setattr(probe.psutil, "Process", Mock(side_effect=[original, original, replacement]))
    receipt = tmp_path / "target.receipt"
    receipt.write_text("started\n123\nowned-nonce\n")

    def fail_activation(callback, **kwargs):
        assert callback() is None
        raise probe.NativeInteractionProbeError("activation timeout")

    monkeypatch.setattr(probe, "_wait", fail_activation)
    with pytest.raises(probe.NativeInteractionProbeError, match="activation timeout"):
        probe._launch_app(tmp_path / "Target.app", receipt=receipt,
                          nonce="owned-nonce", mode="target")
    original.terminate.assert_not_called()
    replacement.terminate.assert_not_called()
    broker.wait.assert_called_once_with(timeout=5.0)


@pytest.mark.parametrize("receipt_text", ["started\n123\nforeign-nonce\n", "malformed\n"])
def test_launch_failure_ignores_unowned_receipt(monkeypatch, tmp_path, receipt_text):
    broker = Mock()
    broker.poll.return_value = None
    monkeypatch.setattr(probe.subprocess, "Popen", Mock(return_value=broker))
    process_factory = Mock()
    monkeypatch.setattr(probe.psutil, "Process", process_factory)
    receipt = tmp_path / "target.receipt"
    receipt.write_text(receipt_text)

    def fail_activation(callback, **kwargs):
        assert callback() is None
        raise probe.NativeInteractionProbeError("activation timeout")

    monkeypatch.setattr(probe, "_wait", fail_activation)
    with pytest.raises(probe.NativeInteractionProbeError, match="activation timeout"):
        probe._launch_app(tmp_path / "Target.app", receipt=receipt,
                          nonce="owned-nonce", mode="target")
    process_factory.assert_not_called()
    broker.wait.assert_called_once_with(timeout=5.0)


def test_launch_success_retains_started_identity_for_caller_cleanup(monkeypatch, tmp_path):
    broker = Mock()
    monkeypatch.setattr(probe.subprocess, "Popen", Mock(return_value=broker))
    process = Mock()
    process.create_time.return_value = 12.5
    monkeypatch.setattr(probe.psutil, "Process", Mock(return_value=process))
    monkeypatch.setattr(probe.time, "sleep", Mock())
    receipt = tmp_path / "target.receipt"
    receipt.write_text("started\n123\nowned-nonce\n")

    def activate(callback, **kwargs):
        assert callback() is None
        receipt.write_text("active\n123\nowned-nonce\n")
        return callback()

    monkeypatch.setattr(probe, "_wait", activate)
    assert probe._launch_app(tmp_path / "Target.app", receipt=receipt,
                             nonce="owned-nonce", mode="target") == (broker, 123, 12.5)
    process.terminate.assert_not_called()
    broker.terminate.assert_not_called()
