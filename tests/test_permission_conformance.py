"""Native Windows conformance for the shared permission capability boundary."""
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
    PERMISSION_SCHEMA_VERSION,
    PermissionKind,
    PermissionReport,
    PermissionRequester,
    PermissionSnapshot,
    PermissionState,
    PermissionStatusProvider,
)


pytestmark = pytest.mark.skipif(
    platform.system() != "Windows",
    reason="native Windows permission capability conformance",
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _windows_report_dict() -> dict[str, object]:
    return {
        "schema_version": PERMISSION_SCHEMA_VERSION,
        "platform": "windows",
        "permissions": [
            {
                "kind": "microphone",
                "state": "unknown",
                "query_supported": False,
                "request_supported": False,
            },
            {
                "kind": "accessibility",
                "state": "not-required",
                "query_supported": True,
                "request_supported": False,
            },
            {
                "kind": "input-monitoring",
                "state": "not-required",
                "query_supported": True,
                "request_supported": False,
            },
        ],
    }


def test_permission_contract_schema_and_serialization_are_stable() -> None:
    from whisperstream.windows_permission_status import (
        WindowsPermissionStatusProvider,
    )

    report = WindowsPermissionStatusProvider().query_permissions()
    assert isinstance(report, PermissionReport)
    assert report.as_dict() == _windows_report_dict()
    assert tuple(snapshot.kind for snapshot in report.permissions) == tuple(
        PermissionKind
    )
    assert set(PermissionState) == {
        PermissionState.NOT_DETERMINED,
        PermissionState.GRANTED,
        PermissionState.DENIED,
        PermissionState.RESTRICTED,
        PermissionState.NOT_REQUIRED,
        PermissionState.UNSUPPORTED,
        PermissionState.UNKNOWN,
    }
    with pytest.raises(ValueError, match="permission kind"):
        PermissionSnapshot(  # type: ignore[arg-type]
            kind="microphone",
            state=PermissionState.UNKNOWN,
            query_supported=False,
            request_supported=False,
        )
    with pytest.raises(ValueError, match="permission state"):
        PermissionSnapshot(  # type: ignore[arg-type]
            kind=PermissionKind.MICROPHONE,
            state="unknown",
            query_supported=False,
            request_supported=False,
        )
    with pytest.raises(ValueError, match="query_supported"):
        PermissionSnapshot(  # type: ignore[arg-type]
            kind=PermissionKind.MICROPHONE,
            state=PermissionState.UNKNOWN,
            query_supported=1,
            request_supported=False,
        )


def test_windows_state_map_is_truthful_and_has_no_request_support() -> None:
    from whisperstream.permission_status import query_permissions

    report = query_permissions()
    assert report.as_dict() == _windows_report_dict()
    by_kind = {snapshot.kind: snapshot for snapshot in report.permissions}
    assert by_kind[PermissionKind.MICROPHONE] == PermissionSnapshot(
        kind=PermissionKind.MICROPHONE,
        state=PermissionState.UNKNOWN,
        query_supported=False,
        request_supported=False,
    )
    for kind in (PermissionKind.ACCESSIBILITY, PermissionKind.INPUT_MONITORING):
        assert by_kind[kind].state is PermissionState.NOT_REQUIRED
        assert by_kind[kind].query_supported is True
        assert by_kind[kind].request_supported is False


def test_capability_selector_is_lazy_and_failure_safe(monkeypatch) -> None:
    import whisperstream.permission_status as permission_status

    marker = "private-native-detail-must-not-cross-boundary"

    class ExplodingProvider:
        def query_permissions(self) -> PermissionReport:
            raise RuntimeError(marker)

    monkeypatch.setattr(
        permission_status,
        "get_permission_status_provider",
        lambda **_kwargs: ExplodingProvider(),
    )
    report = permission_status.query_permissions(system="Windows")
    payload = report.as_dict()
    assert payload == {
        "schema_version": 1,
        "platform": "windows",
        "permissions": [
            {
                "kind": kind.value,
                "state": "unknown",
                "query_supported": False,
                "request_supported": False,
            }
            for kind in PermissionKind
        ],
    }
    assert marker not in json.dumps(payload)


def test_missing_or_malformed_adapter_returns_conservative_report(monkeypatch) -> None:
    import whisperstream.permission_status as permission_status

    def fail_import(*_args, **_kwargs):
        raise ImportError("private adapter load detail")

    monkeypatch.setattr(
        permission_status.importlib,
        "import_module",
        fail_import,
    )
    missing = permission_status.get_permission_status_provider(system="Windows")
    assert missing.query_permissions().as_dict()["permissions"] == [
        {
            "kind": kind.value,
            "state": "unknown",
            "query_supported": False,
            "request_supported": False,
        }
        for kind in PermissionKind
    ]

    class WrongPlatformProvider:
        def query_permissions(self) -> PermissionReport:
            return PermissionReport(
                platform="macos",
                permissions=tuple(
                    PermissionSnapshot(
                        kind=kind,
                        state=PermissionState.GRANTED,
                        query_supported=True,
                        request_supported=True,
                    )
                    for kind in PermissionKind
                ),
            )

    monkeypatch.setattr(
        permission_status,
        "get_permission_status_provider",
        lambda **_kwargs: WrongPlatformProvider(),
    )
    malformed = permission_status.query_permissions(system="Windows")
    assert all(
        snapshot.state is PermissionState.UNKNOWN
        and not snapshot.query_supported
        and not snapshot.request_supported
        for snapshot in malformed.permissions
    )


def test_query_and_request_interfaces_remain_disjoint(monkeypatch) -> None:
    import whisperstream.permission_status as permission_status
    from whisperstream.windows_permission_status import (
        WindowsPermissionStatusProvider,
    )

    provider = WindowsPermissionStatusProvider()
    assert isinstance(provider, PermissionStatusProvider)
    assert not isinstance(provider, PermissionRequester)
    assert {
        name for name in type(provider).__dict__ if not name.startswith("_")
    } == {"query_permissions"}

    calls = {"query": 0, "request": 0}

    class BothInterfaces:
        def query_permissions(self) -> PermissionReport:
            calls["query"] += 1
            return provider.query_permissions()

        def request_permission(self, kind: PermissionKind) -> PermissionSnapshot:
            del kind
            calls["request"] += 1
            raise AssertionError("query path invoked request path")

    monkeypatch.setattr(
        permission_status,
        "get_permission_status_provider",
        lambda **_kwargs: BothInterfaces(),
    )
    assert permission_status.query_permissions(system="Windows").as_dict() == (
        _windows_report_dict()
    )
    assert calls == {"query": 1, "request": 0}


def test_shared_imports_do_not_load_platform_or_permission_sensitive_modules() -> None:
    probe = r'''
import json
import sys

import whisperstream.permission_contract
import whisperstream.permission_status
import whisperstream.server

forbidden = {
    "AVFoundation",
    "ApplicationServices",
    "CoreGraphics",
    "Quartz",
    "sounddevice",
    "pynput",
    "pynput.keyboard",
    "whisperstream.audio_capture",
    "whisperstream.keyboard_shortcuts",
    "whisperstream.windows_permission_status",
    "whisperstream.macos_permission_status",
}
print(json.dumps(sorted(forbidden.intersection(sys.modules))))
'''
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(completed.stdout.strip()) == []


def test_file_only_backend_construction_does_not_query_or_prompt(
    tmp_path: Path,
) -> None:
    probe = r'''
import json
import sys

from whisperstream.main import WhisperstreamApp
from whisperstream.server import WhisperstreamServer

app = WhisperstreamApp(
    verbose=False,
    headless=True,
    defer_model_load=True,
    file_only=True,
)
server = WhisperstreamServer(app, port=0)
try:
    forbidden = {
        "AVFoundation",
        "ApplicationServices",
        "CoreGraphics",
        "Quartz",
        "sounddevice",
        "pynput",
        "pynput.keyboard",
        "whisperstream.audio_capture",
        "whisperstream.keyboard_shortcuts",
        "whisperstream.windows_permission_status",
        "whisperstream.macos_permission_status",
    }
    print(json.dumps({
        "keyboard_absent": app.keyboard is None,
        "audio_absent": app.audio_capture is None,
        "permission_route": any(
            getattr(route, "path", None) == "/api/permissions"
            for route in server.fastapi.routes
        ),
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
        "forbidden_modules": [],
    }


def test_permission_endpoint_is_query_only_and_failure_safe(monkeypatch) -> None:
    from tests._platform_test_support import _make_server
    import whisperstream.permission_status as permission_status

    server = _make_server()
    with TestClient(server.fastapi, raise_server_exceptions=True) as client:
        response = client.get("/api/permissions")
        assert response.status_code == 200
        assert response.json() == _windows_report_dict()
        assert client.post("/api/permissions").status_code == 405

        class ExplodingProvider:
            def query_permissions(self) -> PermissionReport:
                raise RuntimeError("native-private-detail")

        monkeypatch.setattr(
            permission_status,
            "get_permission_status_provider",
            lambda **_kwargs: ExplodingProvider(),
        )
        degraded = client.get("/api/permissions")
        assert degraded.status_code == 200
        assert all(
            item == {
                "kind": kind.value,
                "state": "unknown",
                "query_supported": False,
                "request_supported": False,
            }
            for item, kind in zip(degraded.json()["permissions"], PermissionKind)
        )

        paths = server.fastapi.openapi()["paths"]
        assert set(paths["/api/permissions"]) == {"get"}
