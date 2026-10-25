"""Native macOS conformance for the query-only permission capability seam."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whisperstream.permission_contract import (
    PermissionKind,
    PermissionRequester,
    PermissionState,
    PermissionStatusProvider,
)


pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64",
    reason="native Apple Silicon permission capability conformance",
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeBridge:
    def __init__(
        self,
        *,
        microphone: int = 0,
        accessibility: bool = False,
        input_monitoring: bool = False,
    ) -> None:
        self.microphone = microphone
        self.accessibility = accessibility
        self.input_monitoring = input_monitoring
        self.calls = {"microphone": 0, "accessibility": 0, "input-monitoring": 0}

    def microphone_authorization_status(self) -> int:
        self.calls["microphone"] += 1
        return self.microphone

    def accessibility_is_trusted(self) -> bool:
        self.calls["accessibility"] += 1
        return self.accessibility

    def input_monitoring_is_trusted(self) -> bool:
        self.calls["input-monitoring"] += 1
        return self.input_monitoring


def _by_kind(report):
    return {snapshot.kind: snapshot for snapshot in report.permissions}


def test_api_minimums_match_apple_sdk_contract() -> None:
    from whisperstream.macos_permission_status import MACOS_PERMISSION_API_MINIMUMS

    assert MACOS_PERMISSION_API_MINIMUMS == {
        PermissionKind.MICROPHONE: (10, 14),
        PermissionKind.ACCESSIBILITY: (10, 9),
        PermissionKind.INPUT_MONITORING: (10, 15),
    }


@pytest.mark.parametrize(
    ("native_state", "expected"),
    [
        (0, PermissionState.NOT_DETERMINED),
        (1, PermissionState.RESTRICTED),
        (2, PermissionState.DENIED),
        (3, PermissionState.GRANTED),
        (99, PermissionState.UNKNOWN),
    ],
)
def test_microphone_native_state_mapping_is_exact(native_state, expected) -> None:
    from whisperstream.macos_permission_status import MacosPermissionStatusProvider

    bridge = FakeBridge(microphone=native_state)
    report = MacosPermissionStatusProvider(
        bridge_factory=lambda: bridge,
        runtime_macos=(14, 0),
    ).query_permissions()

    assert _by_kind(report)[PermissionKind.MICROPHONE].state is expected


@pytest.mark.parametrize(
    ("native_state", "expected"),
    [(True, PermissionState.GRANTED), (False, PermissionState.UNKNOWN)],
)
def test_boolean_preflights_never_invent_denied(native_state, expected) -> None:
    from whisperstream.macos_permission_status import MacosPermissionStatusProvider

    bridge = FakeBridge(
        accessibility=native_state,
        input_monitoring=native_state,
    )
    report = MacosPermissionStatusProvider(
        bridge_factory=lambda: bridge,
        runtime_macos=(14, 0),
    ).query_permissions()

    by_kind = _by_kind(report)
    assert by_kind[PermissionKind.ACCESSIBILITY].state is expected
    assert by_kind[PermissionKind.INPUT_MONITORING].state is expected


def test_repeated_queries_refresh_state_and_report_separate_request_support() -> None:
    from whisperstream.macos_permission_status import MacosPermissionStatusProvider

    bridge = FakeBridge()
    provider = MacosPermissionStatusProvider(
        bridge_factory=lambda: bridge,
        runtime_macos=(14, 0),
    )

    first = provider.query_permissions()
    bridge.microphone = 3
    bridge.accessibility = True
    bridge.input_monitoring = True
    second = provider.query_permissions()

    assert _by_kind(first)[PermissionKind.MICROPHONE].state is PermissionState.NOT_DETERMINED
    assert all(
        snapshot.state is PermissionState.GRANTED for snapshot in second.permissions
    )
    assert all(snapshot.query_supported for snapshot in second.permissions)
    assert all(snapshot.request_supported for snapshot in second.permissions)
    assert bridge.calls == {
        "microphone": 2,
        "accessibility": 2,
        "input-monitoring": 2,
    }


def test_unsupported_os_paths_do_not_construct_native_bridge() -> None:
    from whisperstream.macos_permission_status import MacosPermissionStatusProvider

    def fail_if_constructed():
        raise AssertionError("unsupported path loaded a native framework")

    report = MacosPermissionStatusProvider(
        bridge_factory=fail_if_constructed,
        runtime_macos=(10, 8),
    ).query_permissions()

    assert all(
        snapshot.state is PermissionState.UNSUPPORTED
        and not snapshot.query_supported
        and not snapshot.request_supported
        for snapshot in report.permissions
    )


def test_native_error_is_bounded_per_capability_and_content_free() -> None:
    from whisperstream.macos_permission_status import MacosPermissionStatusProvider

    marker = "private-native-error-detail"

    class PartlyFailingBridge(FakeBridge):
        def microphone_authorization_status(self) -> int:
            raise RuntimeError(marker)

    report = MacosPermissionStatusProvider(
        bridge_factory=PartlyFailingBridge,
        runtime_macos=(14, 0),
    ).query_permissions()
    by_kind = _by_kind(report)

    assert by_kind[PermissionKind.MICROPHONE].state is PermissionState.UNKNOWN
    assert by_kind[PermissionKind.MICROPHONE].query_supported is False
    assert by_kind[PermissionKind.ACCESSIBILITY].state is PermissionState.UNKNOWN
    assert by_kind[PermissionKind.ACCESSIBILITY].query_supported is True
    assert by_kind[PermissionKind.INPUT_MONITORING].state is PermissionState.UNKNOWN
    assert by_kind[PermissionKind.INPUT_MONITORING].query_supported is True
    assert marker not in json.dumps(report.as_dict())


def test_selector_failure_returns_complete_conservative_report(monkeypatch) -> None:
    import whisperstream.permission_status as permission_status

    def fail_import(*_args, **_kwargs):
        raise ImportError("private-adapter-load-detail")

    monkeypatch.setattr(permission_status.importlib, "import_module", fail_import)
    missing = permission_status.query_permissions(system="Darwin")
    assert missing.platform == "macos"
    assert all(
        snapshot.state is PermissionState.UNKNOWN
        and not snapshot.query_supported
        and not snapshot.request_supported
        for snapshot in missing.permissions
    )

    class ExplodingProvider:
        def query_permissions(self):
            raise RuntimeError("private-query-detail")

    monkeypatch.setattr(
        permission_status,
        "get_permission_status_provider",
        lambda **_kwargs: ExplodingProvider(),
    )
    exploding = permission_status.query_permissions(system="Darwin")
    assert exploding.as_dict() == missing.as_dict()


def test_query_and_request_implementations_are_structurally_separate() -> None:
    from whisperstream.macos_permission_status import MacosPermissionStatusProvider

    provider = MacosPermissionStatusProvider(
        bridge_factory=FakeBridge,
        runtime_macos=(14, 0),
    )
    assert isinstance(provider, PermissionStatusProvider)
    assert not isinstance(provider, PermissionRequester)
    assert {
        name for name in type(provider).__dict__ if not name.startswith("_")
    } == {"query_permissions"}

    source = (
        REPO_ROOT / "src/whisperstream/macos_permission_status.py"
    ).read_text(encoding="utf-8")
    forbidden = {
        "requestAccessForMediaType",
        "CGRequestListenEventAccess",
        "CGRequestPostEventAccess",
        "kAXTrustedCheckOptionPrompt",
        "CGEventTap",
        "AVCaptureDeviceInput",
        "sounddevice",
        "pynput",
        "subprocess",
        "webbrowser",
        "NSWorkspace",
    }
    assert not sorted(token for token in forbidden if token in source)


def test_import_and_construction_do_not_load_native_frameworks(tmp_path: Path) -> None:
    probe = r'''
import ctypes
import json
import sys

calls = []
def fail_load(name, *args, **kwargs):
    calls.append(str(name))
    raise AssertionError("native framework loaded before query")

ctypes.CDLL = fail_load

import whisperstream.permission_contract
import whisperstream.permission_status
import whisperstream.server
from whisperstream.macos_permission_status import MacosPermissionStatusProvider

provider = MacosPermissionStatusProvider(runtime_macos=(14, 0))
print(json.dumps({
    "native_load_calls": calls,
    "adapter_loaded_only_by_explicit_import": (
        "whisperstream.macos_permission_status" in sys.modules
    ),
    "provider_has_query": hasattr(provider, "query_permissions"),
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
    assert json.loads(completed.stdout.strip()) == {
        "native_load_calls": [],
        "adapter_loaded_only_by_explicit_import": True,
        "provider_has_query": True,
    }


def test_file_only_backend_construction_does_not_import_permission_adapter(
    tmp_path: Path,
) -> None:
    probe = r'''
import json
import sys

import whisperstream.transcript_crypto as transcript_crypto
import whisperstream.main as main_module
from whisperstream.main import WhisperstreamApp
from whisperstream.server import WhisperstreamServer

def fail_protected_store():
    raise AssertionError("file-only construction reached protected storage")

transcript_crypto.get_protected_secret_store = fail_protected_store

# The Windows Phase 3 intake now rejects a missing native delivery adapter on
# Darwin. This probe is about file-only permission import behavior, so provide
# the same inert constructor seam used by narrower app-construction tests.
class FileOnlyTextInjector:
    def __init__(self, *args, **kwargs):
        self.last_transcript = None

    def close(self):
        return None

main_module.TextInjector = FileOnlyTextInjector

app = WhisperstreamApp(
    verbose=False,
    headless=True,
    defer_model_load=True,
    file_only=True,
)
server = WhisperstreamServer(app, port=0)
try:
    forbidden = {
        "sounddevice",
        "pynput",
        "pynput.keyboard",
        "whisperstream.audio_capture",
        "whisperstream.keyboard_shortcuts",
        "whisperstream.macos_permission_status",
    }
    print(json.dumps({
        "keyboard_absent": app.keyboard is None,
        "audio_absent": app.audio_capture is None,
        "permission_route": any(
            getattr(route, "path", None) == "/api/permissions"
            for route in server.fastapi.routes
        ),
        "transcript_store_absent": app.transcript_store is None,
        "forbidden_modules": sorted(forbidden.intersection(sys.modules)),
    }))
finally:
    app.cleanup()
'''
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env={**os.environ, "WHISPERSTREAM_DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=45,
        check=True,
    )
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "keyboard_absent": True,
        "audio_absent": True,
        "permission_route": True,
        "transcript_store_absent": True,
        "forbidden_modules": [],
    }
    assert not (tmp_path / "transcript.key").exists()


def test_real_native_source_queries_and_endpoint_are_truthful() -> None:
    from tests._platform_test_support import _make_server
    from whisperstream.permission_status import query_permissions

    first = query_permissions()
    second = query_permissions()
    assert first.platform == second.platform == "macos"
    assert tuple(item.kind for item in first.permissions) == tuple(PermissionKind)
    assert all(item.query_supported for item in first.permissions)
    assert all(item.request_supported for item in first.permissions)
    assert all(item.state in PermissionState for item in first.permissions)

    server = _make_server()
    with TestClient(server.fastapi, raise_server_exceptions=True) as client:
        one = client.get("/api/permissions")
        two = client.get("/api/permissions")
        assert one.status_code == two.status_code == 200
        assert one.json() == two.json() == second.as_dict()
        assert client.post("/api/permissions").status_code == 405
        assert set(server.fastapi.openapi()["paths"]["/api/permissions"]) == {"get"}


def test_operation_requirements_preserve_complete_passive_report() -> None:
    from whisperstream.permission_contract import required_permissions_for_operation

    from whisperstream.macos_permission_status import MacosPermissionStatusProvider

    provider = MacosPermissionStatusProvider(
        bridge_factory=lambda: FakeBridge(accessibility=True, input_monitoring=False),
        runtime_macos=(14, 0),
    )
    report = provider.query_permissions()
    payload = report.as_dict()
    assert tuple(item.kind for item in report.permissions) == tuple(PermissionKind)
    assert _by_kind(report)[PermissionKind.INPUT_MONITORING].state is PermissionState.UNKNOWN
    assert payload["required_for_operations"] == {
        "microphone-capture": ["microphone"],
        "global-shortcuts": ["accessibility"],
        "text-injection": ["accessibility"],
        "dictation": ["microphone", "accessibility"],
    }
    assert required_permissions_for_operation("global-shortcuts") == (PermissionKind.ACCESSIBILITY,)
    with pytest.raises(KeyError):
        required_permissions_for_operation("unknown-operation")

@pytest.mark.parametrize("before,after", [(False, True), (True, False)])
def test_native_main_run_loop_delivers_pending_trust_changes(before, after) -> None:
    """Use a real CF timer to reproduce work asyncio alone does not dispatch."""
    import ctypes
    import threading
    from types import SimpleNamespace

    from whisperstream.macos_permission_status import _NativeMacosPermissionBridge

    bridge = _NativeMacosPermissionBridge()
    cached_trust = [before]

    def query(_options):
        return cached_trust[0]

    bridge._libraries["ApplicationServices"] = SimpleNamespace(
        AXIsProcessTrustedWithOptions=query,
    )
    cf = bridge._library("CoreFoundation")
    callback_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def deliver_change(_timer, _context):
        cached_trust[0] = after

    cf.CFRunLoopTimerCreate.argtypes = [ctypes.c_void_p, ctypes.c_double,
        ctypes.c_double, ctypes.c_ulong, ctypes.c_long, callback_type, ctypes.c_void_p]
    cf.CFRunLoopTimerCreate.restype = ctypes.c_void_p
    cf.CFRunLoopGetMain.argtypes = []
    cf.CFRunLoopGetMain.restype = ctypes.c_void_p
    cf.CFRunLoopAddTimer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    cf.CFRunLoopAddTimer.restype = None
    cf.CFRunLoopTimerInvalidate.argtypes = [ctypes.c_void_p]
    cf.CFRunLoopTimerInvalidate.restype = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    timer = cf.CFRunLoopTimerCreate(None, 0.0, 0.0, 0, 0, deliver_change, None)
    assert timer
    try:
        mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode").value
        cf.CFRunLoopAddTimer(cf.CFRunLoopGetMain(), timer, mode)
        # A worker must not dispatch main-thread native notifications.
        worker_results = []
        worker = threading.Thread(target=lambda: worker_results.append(bridge.accessibility_is_trusted()))
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert worker_results == [before]
        assert cached_trust[0] is before
        # The production read dispatches the pending change before reading AX.
        assert bridge.accessibility_is_trusted() is after
    finally:
        cf.CFRunLoopTimerInvalidate(timer)
        cf.CFRelease(timer)
