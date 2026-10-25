"""Windows bundle and packaged-engine lifecycle conformance checks."""

from __future__ import annotations

import pytest

import json
import platform
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TAURI_DIR = REPO_ROOT / "src-tauri"


def _json(name: str) -> dict[str, object]:
    return json.loads((TAURI_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Requires the native Windows owner")
def test_native_windows_platform_is_required() -> None:
    assert platform.system() == "Windows"


def test_base_config_has_no_windows_bundle_or_binary_assumptions() -> None:
    base = _json("tauri.conf.json")
    bundle = base["bundle"]

    assert "identifier" not in base
    assert "targets" not in bundle
    assert "createUpdaterArtifacts" not in bundle
    assert "externalBin" not in bundle
    assert "windows" not in bundle
    assert "plugins" not in base
    assert not any(str(resource).lower().endswith(".dll") for resource in bundle["resources"])


def test_windows_overlay_retains_exact_bundle_inputs() -> None:
    contract = json.loads(
        (REPO_ROOT / "tests/fixtures/platform-config-contract-v1.json").read_text(
            encoding="utf-8"
        )
    )
    base = _json("tauri.conf.json")
    overlay = _json("tauri.windows.conf.json")
    shared = contract["shared"]
    windows = contract["platforms"]["windows"]
    bundle = overlay["bundle"]

    assert overlay["identifier"] == windows["identifier"]
    assert bundle["targets"] == windows["bundle_targets"]
    # The official build's overlay supplies updater artifacts and the feed.
    assert "createUpdaterArtifacts" not in bundle
    assert bundle["externalBin"] == windows["external_bin"]
    assert bundle["resources"] == shared["resources"] + windows["resources"]
    assert bundle["windows"]["nsis"] == {
        "installerIcon": "icons/icon.ico",
        "displayLanguageSelector": False,
        "installMode": "both",
        "installerHooks": "installer/hooks.nsh",
        "customLanguageFiles": {"English": "installer/whisperstream-en.nsh"},
    }
    assert "plugins" not in overlay
    assert base["bundle"]["resources"] == shared["resources"]


def test_macos_overlay_contains_no_windows_packaging_keys() -> None:
    overlay = _json("tauri.macos.conf.json")
    contract = json.loads(
        (REPO_ROOT / "tests/fixtures/platform-config-contract-v1.json").read_text(
            encoding="utf-8"
        )
    )["platforms"]["macos_internal"]
    serialized = json.dumps(overlay).lower()

    assert overlay["identifier"] == "io.lanreal.whisperstream"
    assert overlay["bundle"]["targets"] == ["app"]
    assert overlay["bundle"]["createUpdaterArtifacts"] is False
    assert overlay["bundle"]["externalBin"] == [
        "binaries/whisperstream-engine",
        "binaries/whisperstream-macos-stt-worker",
        "binaries/llama-server",
    ]
    assert overlay["bundle"]["resources"] == contract["resources"]
    assert overlay["bundle"]["icon"] == contract["icons"]
    assert overlay["bundle"]["macOS"] == {
        "minimumSystemVersion": "14.0",
        "signingIdentity": "-",
        "hardenedRuntime": False,
        "infoPlist": "Info.macos-internal.plist",
    }
    usage_plist = (TAURI_DIR / "Info.macos-internal.plist").read_text(encoding="utf-8")
    assert "only while you dictate" in usage_plist
    assert ".exe" not in serialized
    assert ".dll" not in serialized
    assert "nsis" not in serialized
    assert overlay["plugins"]["updater"] == contract["updater"]
    assert "windows" not in serialized


def test_release_autostart_reconciliation_remains_windows_only() -> None:
    source = (TAURI_DIR / "src/lib.rs").read_text(encoding="utf-8")

    assert '#[cfg(all(not(debug_assertions), target_os = "windows"))]' in source


def test_taskkill_is_isolated_to_windows_process_adapter() -> None:
    rust_sources = {
        path.relative_to(TAURI_DIR / "src").as_posix(): path.read_text(encoding="utf-8")
        for path in (TAURI_DIR / "src").glob("*.rs")
    }
    owners = [name for name, source in rust_sources.items() if "taskkill" in source.lower()]

    assert owners == ["windows_process_adapter.rs"]
    shared_contract = rust_sources["engine_lifecycle_contract.rs"].lower()
    for forbidden in ("taskkill", "windows::", ".exe", ".dll", "nsis"):
        assert forbidden not in shared_contract


def test_posix_signals_are_isolated_to_macos_process_adapter() -> None:
    rust_sources = {
        path.relative_to(TAURI_DIR / "src").as_posix(): path.read_text(encoding="utf-8")
        for path in (TAURI_DIR / "src").glob("*.rs")
    }
    owners = [
        name
        for name, source in rust_sources.items()
        if "libc::killpg" in source or "libc::sigterm" in source or "libc::sigkill" in source
    ]

    assert owners == ["macos_process_adapter.rs"]
    shared_contract = rust_sources["engine_lifecycle_contract.rs"].lower()
    for forbidden in ("killpg", "sigterm", "sigkill", "process_group"):
        assert forbidden not in shared_contract


def test_tauri_keeps_engine_ownership_above_the_stt_protocol() -> None:
    lib_source = (TAURI_DIR / "src/lib.rs").read_text(encoding="utf-8")
    adapter_source = (TAURI_DIR / "src/windows_engine_adapter.rs").read_text(
        encoding="utf-8"
    )
    combined = f"{lib_source}\n{adapter_source}".lower()

    assert '.sidecar("whisperstream-engine")' in adapter_source
    for argument in ('"--serve"', '"--port"', '"38420"', '"--engine-generation"'):
        assert argument in adapter_source
    for forbidden in ("whisperstream-stt", "stt-worker", "transcribe", "sherpa"):
        assert forbidden not in combined

    mac_adapter = (TAURI_DIR / "src/macos_engine_adapter.rs").read_text(
        encoding="utf-8"
    ).lower()
    assert '.sidecar("whisperstream-engine")' in mac_adapter
    assert '.sidecar("whisperstream-macos-stt-worker")' not in mac_adapter
    for forbidden in ("transcribe", "request_audio", "stt_worker_protocol", "sherpa"):
        assert forbidden not in mac_adapter


def test_minimized_and_exit_shutdown_paths_use_shared_lifecycle() -> None:
    lib_source = (TAURI_DIR / "src/lib.rs").read_text(encoding="utf-8")

    assert 'Some(vec!["--minimized"])' in lib_source
    assert "starts_minimized(std::env::args())" in lib_source
    assert "if !started_minimized" in lib_source
    assert "EngineShutdownReason::AppExit" in lib_source


def test_lifecycle_failure_vocabulary_and_timeouts_remain_bounded() -> None:
    source = (TAURI_DIR / "src/engine_lifecycle_contract.rs").read_text(
        encoding="utf-8"
    )
    for seconds in ("from_secs(5)", "from_secs(30)", "from_secs(3)"):
        assert seconds in source
    for failure in (
        "MissingBinary",
        "ExitedBeforeReady",
        "ReadinessTimeout",
        "GracefulShutdownTimeout",
        "EscalationFailed",
        "CleanupTimeout",
    ):
        assert failure in source
    assert "EngineShutdownStage::ForceKill" in source


def test_windows_process_identity_is_handle_and_creation_time_bound() -> None:
    source = (TAURI_DIR / "src/windows_process_adapter.rs").read_text(
        encoding="utf-8"
    )

    assert "OpenProcess" in source
    assert "GetProcessTimes" in source
    assert "WaitForSingleObject" in source
    assert "creation_time_100ns" in source
    assert "handle: ProcessHandle" in source
    assert "CloseHandle(self.as_handle())" in source
    assert "recorded: BTreeMap<u32, RecordedProcess>" in source
    assert "process_snapshot()?.contains_key(&self.root_pid)" not in source


def test_packaged_health_and_shutdown_are_bound_to_the_spawned_generation() -> None:
    windows_adapter = (TAURI_DIR / "src/windows_engine_adapter.rs").read_text(
        encoding="utf-8"
    )
    macos_adapter = (TAURI_DIR / "src/macos_engine_adapter.rs").read_text(
        encoding="utf-8"
    )
    transport = (TAURI_DIR / "src/engine_http.rs").read_text(encoding="utf-8")
    backend = (REPO_ROOT / "src/whisperstream/server/app.py").read_text(encoding="utf-8")

    for adapter in (windows_adapter, macos_adapter):
        assert "new_generation_token" in adapter
        assert '"--engine-generation"' in adapter
        assert "self.generation_token.as_deref()" in adapter
    assert "expected_generation" in transport
    assert "response_generation" in transport
    assert "x-whisperstream-engine-generation" in transport
    assert "stale_engine_generation" in backend
    assert "secrets.compare_digest" in backend


def test_engine_transport_and_protected_storage_probe_are_generation_aware() -> None:
    engine_http = (TAURI_DIR / "src/engine_http.rs").read_text(encoding="utf-8")
    protected_probe = (
        REPO_ROOT / "src" / "whisperstream" / "windows_protected_storage_probe.py"
    ).read_text(encoding="utf-8")

    assert "production_engine_address_remains_fixed" in engine_http
    assert 'const ENGINE_ADDR: &str = "127.0.0.1:38420"' in engine_http
    assert "stale_listener_rejects_generation_and_survives_shutdown" in engine_http
    assert 'TcpListener::bind("127.0.0.1:0")' in engine_http
    assert "probe_health_at" in engine_http
    assert "request_shutdown_at" in engine_http
    assert "WindowsDpapiProtectedSecretStore" in protected_probe
    assert "UnavailableProtectedSecretStore" in protected_probe
    assert "plaintext-and-log-scan" in protected_probe


def test_windows_session_end_bypasses_lifecycle_lock_with_retained_identity() -> None:
    source = (TAURI_DIR / "src/lib.rs").read_text(encoding="utf-8")
    shutdown = source.split("fn shutdown_packaged_engine(", 1)[1].split("// Apply the HUD", 1)[0]
    assert "windows_shutdown::session_ending()" in shutdown
    assert "windows_shutdown::stop_sidecar_tree(state.1.pid())" in shutdown
    assert shutdown.index("windows_shutdown::session_ending()") < shutdown.index(".lock()")
    assert "windows_shutdown::Process::open(identity.pid)" in source
    assert "windows_shutdown::exit_if_session_ended()" in source
    assert "SidecarChild" not in source
    process_adapter = (TAURI_DIR / "src/windows_process_adapter.rs").read_text(encoding="utf-8")
    assert process_adapter.index("windows_shutdown::session_ending()") < process_adapter.index('Command::new("taskkill")')
    assert "windows_shutdown::terminate_tree(" in process_adapter
    assert 'webview_visibility::install(&app.get_webview_window("main").unwrap())' in source
