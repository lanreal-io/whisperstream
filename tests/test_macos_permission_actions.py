"""Native Mac contract tests for explicit permission request and recovery."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whisperstream.permission_actions import (
    PermissionActionProviders,
    PermissionActionService,
)
from whisperstream.permission_contract import (
    PermissionAction,
    PermissionActionFailure,
    PermissionActionStatus,
    PermissionIntent,
    PermissionIntentRegistry,
    PermissionKind,
    PermissionState,
)


pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64",
    reason="native Apple Silicon permission action conformance",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION_HEADERS = {"X-Whisperstream-User-Intent": "permission-action-v1"}


class StateStore:
    def __init__(self, **states: PermissionState) -> None:
        self.states = {
            PermissionKind(key.replace("_", "-")): value
            for key, value in states.items()
        }
        self.calls: list[PermissionKind] = []

    def query(self, kind: PermissionKind) -> PermissionState:
        self.calls.append(kind)
        return self.states[kind]


class FakeBridge:
    def __init__(self, states: StateStore | None = None) -> None:
        self.states = states
        self.calls = {
            "microphone": 0,
            "accessibility": 0,
            "input-monitoring": 0,
            "settings": 0,
        }
        self.results = {
            PermissionKind.MICROPHONE: True,
            PermissionKind.ACCESSIBILITY: True,
            PermissionKind.INPUT_MONITORING: True,
        }
        self.settings_result = True
        self.raise_on: PermissionKind | str | None = None
        self.update_to_granted = True

    def _request(self, kind: PermissionKind) -> bool:
        self.calls[kind.value] += 1
        if self.raise_on is kind:
            raise RuntimeError("private-native-request-detail")
        result = self.results[kind]
        if result and self.states is not None and self.update_to_granted:
            self.states.states[kind] = PermissionState.GRANTED
        return result

    def request_microphone_access(self) -> bool:
        return self._request(PermissionKind.MICROPHONE)

    def request_accessibility_access(self) -> bool:
        return self._request(PermissionKind.ACCESSIBILITY)

    def request_input_monitoring_access(self) -> bool:
        return self._request(PermissionKind.INPUT_MONITORING)

    def open_system_settings(self) -> bool:
        self.calls["settings"] += 1
        if self.raise_on == "settings":
            raise RuntimeError("private-native-settings-detail")
        return self.settings_result


def _providers(
    registry: PermissionIntentRegistry,
    states: StateStore,
    bridge: FakeBridge,
    *,
    runtime_macos: tuple[int, int] = (14, 0),
) -> PermissionActionProviders:
    from whisperstream.macos_permission_actions import (
        MacosPermissionRecoveryProvider,
        MacosPermissionRequester,
    )

    return PermissionActionProviders(
        requester=MacosPermissionRequester(
            registry,
            state_query=states.query,
            bridge_factory=lambda: bridge,
            runtime_macos=runtime_macos,
        ),
        recovery=MacosPermissionRecoveryProvider(
            registry,
            state_query=states.query,
            bridge_factory=lambda: bridge,
            runtime_macos=runtime_macos,
        ),
    )


def _service(
    states: StateStore,
    bridge: FakeBridge,
    *,
    runtime_macos: tuple[int, int] = (14, 0),
) -> PermissionActionService:
    return PermissionActionService(
        system="Darwin",
        provider_factory=lambda registry: _providers(
            registry, states, bridge, runtime_macos=runtime_macos
        ),
    )


def test_action_api_minimums_match_apple_sdk_contract() -> None:
    from whisperstream.macos_permission_actions import (
        MACOS_SYSTEM_SETTINGS_API_MINIMUM,
    )
    from whisperstream.macos_permission_status import MACOS_PERMISSION_API_MINIMUMS

    assert MACOS_PERMISSION_API_MINIMUMS == {
        PermissionKind.MICROPHONE: (10, 14),
        PermissionKind.ACCESSIBILITY: (10, 9),
        PermissionKind.INPUT_MONITORING: (10, 15),
    }
    assert MACOS_SYSTEM_SETTINGS_API_MINIMUM == (10, 6)


def test_explicit_action_marker_is_required_before_intent_or_native_provider() -> None:
    calls = {"factory": 0}

    def provider_factory(_registry):
        calls["factory"] += 1
        raise AssertionError("provider constructed without explicit user action")

    service = PermissionActionService(
        system="Darwin", provider_factory=provider_factory
    )
    request = service.request_permission(
        PermissionKind.MICROPHONE, explicit_user_action=False
    )
    recovery = service.open_permission_settings(
        PermissionKind.ACCESSIBILITY, explicit_user_action=False
    )

    assert request.failure is PermissionActionFailure.INTENT_REQUIRED
    assert recovery.failure is PermissionActionFailure.INTENT_REQUIRED
    assert calls == {"factory": 0}
    assert "intent_id" not in request.as_dict()


def test_unknown_and_reused_intents_fail_before_state_or_native_calls() -> None:
    from whisperstream.macos_permission_actions import MacosPermissionRequester

    registry = PermissionIntentRegistry()
    states = StateStore(microphone=PermissionState.DENIED)
    bridge = FakeBridge(states)
    requester = MacosPermissionRequester(
        registry,
        state_query=states.query,
        bridge_factory=lambda: bridge,
        runtime_macos=(14, 0),
    )
    forged = PermissionIntent(
        intent_id="not-issued",
        kind=PermissionKind.MICROPHONE,
        action=PermissionAction.REQUEST,
    )
    unknown = requester.request_permission(forged)
    valid = registry.issue(PermissionKind.MICROPHONE, PermissionAction.REQUEST)
    first = requester.request_permission(valid)
    calls_after_first = list(states.calls)
    reused = requester.request_permission(valid)

    assert unknown.failure is PermissionActionFailure.INTENT_REQUIRED
    assert first.status is PermissionActionStatus.DECLINED
    assert reused.failure is PermissionActionFailure.INTENT_REUSED
    assert states.calls == calls_after_first
    assert bridge.calls["microphone"] == 0


def test_wrong_action_intent_fails_before_state_or_native_calls() -> None:
    from whisperstream.macos_permission_actions import MacosPermissionRequester

    registry = PermissionIntentRegistry()
    states = StateStore(microphone=PermissionState.NOT_DETERMINED)
    bridge = FakeBridge(states)
    requester = MacosPermissionRequester(
        registry,
        state_query=states.query,
        bridge_factory=lambda: bridge,
        runtime_macos=(14, 0),
    )
    wrong_action = registry.issue(
        PermissionKind.MICROPHONE,
        PermissionAction.OPEN_SETTINGS,
    )

    outcome = requester.request_permission(wrong_action)

    assert outcome.failure is PermissionActionFailure.INTENT_REQUIRED
    assert states.calls == []
    assert bridge.calls["microphone"] == 0


@pytest.mark.parametrize(
    ("initial", "native_result", "expected_status", "expected_after", "native_calls"),
    [
        (
            PermissionState.NOT_DETERMINED,
            True,
            PermissionActionStatus.COMPLETED,
            PermissionState.GRANTED,
            1,
        ),
        (
            PermissionState.NOT_DETERMINED,
            False,
            PermissionActionStatus.DECLINED,
            PermissionState.DENIED,
            1,
        ),
        (
            PermissionState.DENIED,
            True,
            PermissionActionStatus.DECLINED,
            PermissionState.DENIED,
            0,
        ),
        (
            PermissionState.GRANTED,
            True,
            PermissionActionStatus.UNCHANGED,
            PermissionState.GRANTED,
            0,
        ),
    ],
)
def test_microphone_request_transitions_are_typed_and_requeried(
    initial,
    native_result,
    expected_status,
    expected_after,
    native_calls,
) -> None:
    states = StateStore(microphone=initial)
    bridge = FakeBridge(states)
    bridge.results[PermissionKind.MICROPHONE] = native_result
    if not native_result:
        bridge.update_to_granted = False
    outcome = _service(states, bridge).request_permission(
        PermissionKind.MICROPHONE, explicit_user_action=True
    )

    assert outcome.status is expected_status
    assert outcome.before is initial
    assert outcome.after is expected_after
    assert bridge.calls["microphone"] == native_calls
    assert len(states.calls) == (2 if native_calls else 1)


def test_restricted_and_unavailable_request_paths_never_construct_native_bridge() -> None:
    restricted = StateStore(microphone=PermissionState.RESTRICTED)
    bridge = FakeBridge(restricted)
    outcome = _service(restricted, bridge).request_permission(
        PermissionKind.MICROPHONE, explicit_user_action=True
    )
    assert outcome.status is PermissionActionStatus.UNSUPPORTED
    assert outcome.failure is PermissionActionFailure.NOT_REQUESTABLE
    assert bridge.calls["microphone"] == 0

    old = StateStore(input_monitoring=PermissionState.UNKNOWN)
    old_bridge = FakeBridge(old)
    unavailable = _service(old, old_bridge, runtime_macos=(10, 14)).request_permission(
        PermissionKind.INPUT_MONITORING, explicit_user_action=True
    )
    assert unavailable.before is PermissionState.UNSUPPORTED
    assert unavailable.failure is PermissionActionFailure.NOT_REQUESTABLE
    assert old.calls == []
    assert old_bridge.calls["input-monitoring"] == 0


def test_accessibility_and_input_monitoring_requests_do_not_repeat_prompts() -> None:
    for kind, state_name in (
        (PermissionKind.ACCESSIBILITY, "accessibility"),
        (PermissionKind.INPUT_MONITORING, "input_monitoring"),
    ):
        states = StateStore(**{state_name: PermissionState.UNKNOWN})
        bridge = FakeBridge(states)
        bridge.results[kind] = False
        bridge.update_to_granted = False
        service = _service(states, bridge)

        first = service.request_permission(kind, explicit_user_action=True)
        second = service.request_permission(kind, explicit_user_action=True)

        assert first.status is PermissionActionStatus.UNCHANGED
        assert first.after is PermissionState.UNKNOWN
        assert second.status is PermissionActionStatus.UNCHANGED
        assert bridge.calls[kind.value] == 1


def test_native_request_error_is_content_free_and_latched() -> None:
    states = StateStore(microphone=PermissionState.NOT_DETERMINED)
    bridge = FakeBridge(states)
    bridge.raise_on = PermissionKind.MICROPHONE
    service = _service(states, bridge)

    failed = service.request_permission(
        PermissionKind.MICROPHONE, explicit_user_action=True
    )
    repeated = service.request_permission(
        PermissionKind.MICROPHONE, explicit_user_action=True
    )

    assert failed.failure is PermissionActionFailure.NATIVE_ERROR
    assert repeated.status is PermissionActionStatus.UNCHANGED
    assert bridge.calls["microphone"] == 1
    assert "private-native" not in json.dumps(failed.as_dict())


def test_denied_or_revoked_recovery_opens_settings_and_requeries() -> None:
    for initial in (PermissionState.DENIED, PermissionState.UNKNOWN):
        states = StateStore(accessibility=initial)
        bridge = FakeBridge(states)
        outcome = _service(states, bridge).open_permission_settings(
            PermissionKind.ACCESSIBILITY, explicit_user_action=True
        )

        assert outcome.status is PermissionActionStatus.COMPLETED
        assert outcome.before is outcome.after is initial
        assert outcome.relaunch_required is True
        assert bridge.calls["settings"] == 1
        assert states.calls == [
            PermissionKind.ACCESSIBILITY,
            PermissionKind.ACCESSIBILITY,
        ]


def test_recovery_requery_can_observe_grant_without_relaunch() -> None:
    states = StateStore(microphone=PermissionState.DENIED)

    class GrantingSettingsBridge(FakeBridge):
        def open_system_settings(self) -> bool:
            self.calls["settings"] += 1
            states.states[PermissionKind.MICROPHONE] = PermissionState.GRANTED
            return True

    bridge = GrantingSettingsBridge(states)
    outcome = _service(states, bridge).open_permission_settings(
        PermissionKind.MICROPHONE, explicit_user_action=True
    )

    assert outcome.status is PermissionActionStatus.COMPLETED
    assert outcome.before is PermissionState.DENIED
    assert outcome.after is PermissionState.GRANTED
    assert outcome.relaunch_required is False
    assert states.calls == [PermissionKind.MICROPHONE, PermissionKind.MICROPHONE]


def test_recovery_granted_unavailable_and_native_error_edges_are_typed() -> None:
    granted = StateStore(input_monitoring=PermissionState.GRANTED)
    granted_bridge = FakeBridge(granted)
    unchanged = _service(granted, granted_bridge).open_permission_settings(
        PermissionKind.INPUT_MONITORING, explicit_user_action=True
    )
    assert unchanged.status is PermissionActionStatus.UNCHANGED
    assert granted_bridge.calls["settings"] == 0

    denied = StateStore(input_monitoring=PermissionState.UNKNOWN)
    unavailable_bridge = FakeBridge(denied)
    unavailable_bridge.settings_result = False
    unavailable = _service(denied, unavailable_bridge).open_permission_settings(
        PermissionKind.INPUT_MONITORING, explicit_user_action=True
    )
    assert unavailable.failure is PermissionActionFailure.RECOVERY_UNAVAILABLE

    failed_states = StateStore(microphone=PermissionState.DENIED)
    failed_bridge = FakeBridge(failed_states)
    failed_bridge.raise_on = "settings"
    failed = _service(failed_states, failed_bridge).open_permission_settings(
        PermissionKind.MICROPHONE, explicit_user_action=True
    )
    assert failed.failure is PermissionActionFailure.NATIVE_ERROR
    assert "private-native" not in json.dumps(failed.as_dict())


def test_real_native_action_symbols_are_packaged_without_invoking_them() -> None:
    from whisperstream.macos_permission_actions import (
        _NativeMacosPermissionActionBridge,
    )

    support = _NativeMacosPermissionActionBridge().probe_support()
    assert support == {
        "microphone_request": True,
        "accessibility_request": True,
        "input_monitoring_request": True,
        "system_settings_recovery": True,
    }


def test_import_construction_and_passive_paths_do_not_load_action_frameworks(
    tmp_path: Path,
) -> None:
    probe = r'''
import ctypes
import json
import sys

loads = []
def fail_load(name, *args, **kwargs):
    loads.append(str(name))
    raise AssertionError("action framework loaded without explicit action")
ctypes.CDLL = fail_load

import whisperstream.permission_actions
import whisperstream.server
from whisperstream.macos_permission_actions import (
    MacosPermissionRecoveryProvider,
    MacosPermissionRequester,
)
from whisperstream.permission_contract import PermissionIntentRegistry

registry = PermissionIntentRegistry()
requester = MacosPermissionRequester(registry, runtime_macos=(14, 0))
recovery = MacosPermissionRecoveryProvider(registry, runtime_macos=(14, 0))
print(json.dumps({
    "native_loads": loads,
    "requester": type(requester).__name__,
    "recovery": type(recovery).__name__,
}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env={**os.environ, "WHISPERSTREAM_DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(completed.stdout) == {
        "native_loads": [],
        "requester": "MacosPermissionRequester",
        "recovery": "MacosPermissionRecoveryProvider",
    }


def test_passive_endpoint_and_hydration_do_not_construct_action_service() -> None:
    from tests._platform_test_support import _make_server

    server = _make_server()
    with TestClient(server.fastapi, raise_server_exceptions=True) as client:
        assert client.get("/api/config").status_code == 200
        assert client.get("/api/devices").status_code == 200
        assert client.get("/api/permissions").status_code == 200
    assert server._permission_action_service is None


def test_action_endpoints_require_marker_and_never_serialize_intent() -> None:
    from tests._platform_test_support import _make_server

    states = StateStore(microphone=PermissionState.GRANTED)
    bridge = FakeBridge(states)
    service = _service(states, bridge)
    server = _make_server()
    server._permission_action_service = service
    with TestClient(server.fastapi, raise_server_exceptions=True) as client:
        missing = client.post(
            "/api/permissions/request", json={"kind": "microphone"}
        )
        hostile = client.post(
            "/api/permissions/request",
            json={"kind": "microphone"},
            headers={**ACTION_HEADERS, "Origin": "https://example.com"},
        )
        request = client.post(
            "/api/permissions/request",
            json={"kind": "microphone"},
            headers=ACTION_HEADERS,
        )
        tauri_request = client.post(
            "/api/permissions/request",
            json={"kind": "microphone"},
            headers={**ACTION_HEADERS, "Origin": "tauri://localhost"},
        )
        recovery = client.post(
            "/api/permissions/recovery",
            json={"kind": "microphone"},
            headers=ACTION_HEADERS,
        )

    assert missing.status_code == hostile.status_code == 403
    assert (
        request.status_code
        == tauri_request.status_code
        == recovery.status_code
        == 200
    )
    assert request.json()["status"] == "unchanged"
    assert tauri_request.json()["status"] == "unchanged"
    assert recovery.json()["status"] == "unchanged"
    assert "intent_id" not in request.json()
    assert "intent_id" not in recovery.json()


def test_source_and_internal_builder_keep_query_and_actions_separate() -> None:
    status_source = (
        REPO_ROOT / "src/whisperstream/macos_permission_status.py"
    ).read_text(encoding="utf-8")
    action_source = (
        REPO_ROOT / "src/whisperstream/macos_permission_actions.py"
    ).read_text(encoding="utf-8")
    build_source = (
        REPO_ROOT / "scripts/build_macos_internal.py"
    ).read_text(encoding="utf-8")

    for prompt_symbol in (
        "requestAccessForMediaType:completionHandler:",
        "CGRequestListenEventAccess",
        "kAXTrustedCheckOptionPrompt",
        "open_system_settings",
    ):
        assert prompt_symbol not in status_source
        assert prompt_symbol in action_source
    assert "--include-module=whisperstream.permission_actions" in build_source
    assert "--include-module=whisperstream.macos_permission_actions" in build_source
