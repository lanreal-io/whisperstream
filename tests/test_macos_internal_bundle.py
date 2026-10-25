"""Focused tests for the isolated internal Apple Silicon bundle tooling."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import plistlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from whisperstream import macos_stt_manager
from whisperstream import eula
from whisperstream.main import _build_parser
from whisperstream.text_injector import _LazyPyperclip


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_script = _load_script("build_macos_internal.py")
audit_script = _load_script("audit_macos_internal_bundle.py")
launch_script = _load_script("verify_macos_internal_launch.py")
lifecycle_script = _load_script("verify_macos_process_lifecycle.py")


@pytest.mark.parametrize("exit_code", [0, 7])
def test_builder_tees_stdout_stderr_and_preserves_failure(tmp_path, capsys, exit_code):
    log_path = tmp_path / "logs" / "command.log"
    payload = "Compiling café\r50%\n".encode() + b"stderr: warning\xff\n"
    command = (
        "import os, sys; "
        f"os.write(1, {payload[:18]!r}); "
        f"os.write(2, {payload[18:]!r}); sys.exit({exit_code})"
    )

    def run():
        return build_script._run(
            [sys.executable, "-c", command],
            cwd=tmp_path, env=os.environ.copy(), log_path=log_path,
        )

    if exit_code:
        with pytest.raises(build_script.MacInternalBuildError) as error:
            run()
        assert "exit_code=7 log=command.log" in str(error.value)
        assert payload.decode("utf-8", errors="replace").replace("\r", "\n") in str(error.value)
    else:
        assert run() >= 0
    assert log_path.read_bytes() == payload
    assert capsys.readouterr().out.endswith(payload.decode("utf-8", errors="replace"))


def test_builder_flushes_partial_lines_before_child_exits(tmp_path, monkeypatch):
    log_path = tmp_path / "command.log"
    release = tmp_path / "continue"

    class Terminal(io.StringIO):
        def flush(self):
            if self.getvalue().endswith("progress "):
                # The child waits for terminal output before finishing its
                # split UTF-8 character. This also proves the log is flushed.
                assert log_path.read_bytes() == b"progress \xe2"
                release.touch()
            super().flush()

    terminal = Terminal()
    monkeypatch.setattr(build_script.sys, "stdout", terminal)
    command = """
import os, pathlib, sys, time
os.write(1, b'progress \\xe2')
deadline = time.monotonic() + 10
while not pathlib.Path(sys.argv[1]).exists():
    if time.monotonic() > deadline:
        sys.exit(9)
    time.sleep(0.01)
os.write(2, b'\\x82\\xac\\rfinished')
"""
    build_script._run(
        [sys.executable, "-c", command, str(release)],
        cwd=tmp_path, env=os.environ.copy(), log_path=log_path,
    )
    assert terminal.getvalue().endswith("progress €\rfinished")
    assert log_path.read_bytes() == "progress €\rfinished".encode()


def test_builder_stops_child_when_terminal_write_fails(tmp_path, monkeypatch):
    processes = []
    popen = subprocess.Popen

    def record_process(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    class BrokenTerminal(io.StringIO):
        def write(self, value):
            if "child output" in value:
                raise BrokenPipeError("terminal closed")
            return super().write(value)

    monkeypatch.setattr(build_script.subprocess, "Popen", record_process)
    monkeypatch.setattr(build_script.sys, "stdout", BrokenTerminal())
    log_path = tmp_path / "command.log"
    with pytest.raises(BrokenPipeError, match="terminal closed"):
        build_script._run(
            [sys.executable, "-c", "import os, time; os.write(1, b'child output'); time.sleep(60)"],
            cwd=tmp_path, env=os.environ.copy(), log_path=log_path,
        )
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert log_path.read_bytes() == b"child output"


@pytest.mark.parametrize("cache_state", ["healthy", "missing", "partial", "broken"])
def test_managed_python_repairs_only_unusable_pinned_cache(tmp_path, monkeypatch, cache_state):
    python = tmp_path / "python" / f"cpython-{build_script.PYTHON_VERSION}-macos-aarch64-none/bin/python3.12"
    if cache_state != "missing":
        python.parent.mkdir(parents=True)
    if cache_state in {"healthy", "broken"}:
        python.write_bytes(b"synthetic interpreter")
    repaired = False
    calls = []

    def probe(argv):
        assert argv[0] == str(python)
        if cache_state == "broken" and not repaired:
            raise build_script.MacInternalBuildError("missing standard library")
        return build_script.PYTHON_VERSION

    def install(argv, **kwargs):
        nonlocal repaired
        calls.append(argv)
        assert kwargs["env"]["UV_PYTHON_INSTALL_DIR"] == str(tmp_path / "python")
        assert "--reinstall" in argv and "--no-bin" in argv
        assert argv[-1] == build_script.PYTHON_VERSION
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"repaired interpreter")
        repaired = True
        return 0.0

    monkeypatch.setattr(build_script, "_output", probe)
    monkeypatch.setattr(build_script, "_run", install)
    monkeypatch.setattr(build_script.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(build_script, "_assert_arm64_macho", lambda *args: None)
    assert build_script._managed_python(tmp_path, {}, tmp_path) == python
    assert len(calls) == (cache_state != "healthy")


def test_current_tauri_api_matches_locked_rust() -> None:
    build_script._assert_tauri_api_compatibility(REPO_ROOT)


@pytest.mark.parametrize("locked,installed", [("2.11.1", None), ("2.10.1", "2.11.1")])
def test_tauri_api_drift_fails_before_compilation(tmp_path, locked, installed):
    (tmp_path / "src-tauri").mkdir()
    (tmp_path / "src-tauri/Cargo.lock").write_text('[[package]]\nname = "tauri"\nversion = "2.10.2"\n')
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/package-lock.json").write_text(json.dumps({
        "packages": {"node_modules/@tauri-apps/api": {"version": locked}}
    }))
    if installed:
        package = tmp_path / "frontend/node_modules/@tauri-apps/api/package.json"
        package.parent.mkdir(parents=True)
        package.write_text(json.dumps({"version": installed}))
    with pytest.raises(build_script.MacInternalBuildError, match="Tauri API and Rust"):
        build_script._assert_tauri_api_compatibility(tmp_path)


def test_internal_builder_bundle_selection_defaults_to_existing_app_path() -> None:
    default = build_script._parse_args([])
    dmg = build_script._parse_args(
        ["--build-root", "/tmp/example", "--bundles", "dmg"]
    )
    both = build_script._parse_args(
        ["--build-root", "/tmp/example", "--bundles", "both"]
    )

    assert default.bundles == "app"
    assert default.build_root is None
    assert dmg.bundles == "dmg"
    assert both.bundles == "both"
    assert build_script.BUNDLE_SELECTIONS == {
        "app": ("app",),
        "dmg": ("dmg",),
        "both": ("app", "dmg"),
    }


def test_internal_builder_rejects_invalid_programmatic_bundle_selection() -> None:
    with pytest.raises(
        build_script.MacInternalBuildError,
        match="invalid internal bundle selection",
    ):
        build_script.build(Path("/tmp/example"), Path("/tmp/tools"), bundles="zip")


def test_internal_builder_uses_unattended_tauri_mode_only_for_dmg() -> None:
    app = build_script._tauri_build_argv(Path("/tmp/tauri"), "app")
    dmg = build_script._tauri_build_argv(Path("/tmp/tauri"), "dmg")
    both = build_script._tauri_build_argv(Path("/tmp/tauri"), "both")

    assert app[app.index("--bundles") + 1] == "app"
    assert dmg[dmg.index("--bundles") + 1] == "dmg"
    assert both[both.index("--bundles") + 1] == "app,dmg"
    assert "--ci" not in app
    assert "--ci" in dmg
    assert "--ci" in both


def test_frontend_prebuild_dependency_closure_is_staged(tmp_path) -> None:
    source_root = tmp_path / "source"
    build_script._stage_sources(source_root, "1.5.1")
    manifest = json.loads(
        (source_root / "frontend/src/assets/documentation/manifest.json").read_text()
    )
    for relative in manifest["inputs"]:
        assert (source_root / relative).is_file(), relative
    for relative in build_script.FRONTEND_BUILD_SCRIPTS:
        assert (source_root / relative).is_file(), relative
    index = source_root / ".local-dependencies/sherpa-onnx"
    assert not index.exists()
    for name in ("EULA", "PRIVACY"):
        assert (source_root / f"src-tauri/resources/{name}.txt").is_file()
    # Exercise the actual manifest checker from the isolated source root.
    result = subprocess.run(
        ["node", "scripts/check-offline-docs.mjs"], cwd=source_root,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_platform_stage_is_tracked_allowlist_without_windows_or_user_artifacts() -> None:
    selected = build_script._selected_source_paths()

    assert "src-tauri/Info.macos-internal.plist" in selected
    assert "src-tauri/tauri.macos.conf.json" in selected
    assert "src-tauri/icons/icon.icns" in selected
    assert "src-tauri/resources/EULA.txt" in selected
    assert "src-tauri/resources/PRIVACY.txt" in selected
    assert "src-tauri/resources/THIRD_PARTY_LICENSES.txt" in selected
    # Bundled upstream notices, staged at the paths the Mac overlay names.
    assert {"LICENSE", "NOTICE", "LICENSES/Chirp-MIT.txt"} <= set(selected)
    assert "src-tauri/tauri.windows.conf.json" not in selected
    assert "src-tauri/icons/icon.ico" not in selected
    assert "src/whisperstream/windows_protected_secret.py" not in selected
    assert "src/whisperstream/windows_permission_status.py" not in selected
    assert "src/whisperstream/audio_capture.py" not in selected
    assert "src/whisperstream/keyboard_shortcuts.py" not in selected
    assert "src/whisperstream/windows_delivery_adapter.py" not in selected
    assert "src/whisperstream/capture.py" in selected
    assert "src/whisperstream/macos_capture.py" in selected
    assert "src/whisperstream/macos_delivery_adapter.py" in selected
    assert "src/whisperstream/macos_agent_integration.py" in selected
    assert "src/whisperstream/claude_plugin/scripts/forward-hook.sh" in selected
    assert "src/whisperstream/claude_plugin/scripts/forward-hook.ps1" not in selected
    assert "src/whisperstream/shortcuts.py" in selected
    assert "src/whisperstream/macos_shortcuts.py" in selected
    assert not any(path.startswith("models/") for path in selected)
    assert not any(
        path.lower().endswith((".dll", ".exe", ".pyd", ".ps1"))
        for path in selected
    )


def test_builder_binds_llama_sidecar_to_packaged_resource_dylibs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecar = (
        tmp_path
        / "src-tauri/binaries/llama-server-aarch64-apple-darwin"
    )
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b"Mach-O")
    observed_rpaths = iter(
        [[], [build_script.LLAMA_BUNDLE_RPATH]]
    )
    commands: list[list[str]] = []

    monkeypatch.setattr(build_script, "_macho_rpaths", lambda _path: next(observed_rpaths))
    monkeypatch.setattr(build_script, "_sha256", lambda _path: "b" * 64)

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(build_script.subprocess, "run", fake_run)
    result = build_script._bind_llama_bundle_rpath(
        tmp_path,
        {"sidecar_sha256": "a" * 64, "dylib_count": 27},
    )

    assert commands == [[
        "install_name_tool", "-add_rpath", build_script.LLAMA_BUNDLE_RPATH,
        str(sidecar),
    ]]
    assert result["upstream_sidecar_sha256"] == "a" * 64
    assert result["sidecar_sha256"] == "b" * 64
    assert result["bundle_rpaths"] == [build_script.LLAMA_BUNDLE_RPATH]


def test_builder_llama_rpath_binding_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecar = (
        tmp_path
        / "src-tauri/binaries/llama-server-aarch64-apple-darwin"
    )
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b"Mach-O")
    monkeypatch.setattr(
        build_script,
        "_macho_rpaths",
        lambda _path: [build_script.LLAMA_BUNDLE_RPATH],
    )
    monkeypatch.setattr(build_script, "_sha256", lambda _path: "b" * 64)

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("install_name_tool must not rerun")

    monkeypatch.setattr(build_script.subprocess, "run", unexpected_run)
    result = build_script._bind_llama_bundle_rpath(
        tmp_path,
        {
            "upstream_sidecar_sha256": "a" * 64,
            "sidecar_sha256": "b" * 64,
            "dylib_count": 27,
            "bundle_rpaths": [build_script.LLAMA_BUNDLE_RPATH],
        },
    )

    assert result["upstream_sidecar_sha256"] == "a" * 64
    assert result["sidecar_sha256"] == "b" * 64


def test_builder_rejects_llama_install_name_tool_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecar = (
        tmp_path
        / "src-tauri/binaries/llama-server-aarch64-apple-darwin"
    )
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b"Mach-O")
    monkeypatch.setattr(build_script, "_macho_rpaths", lambda _path: [])
    monkeypatch.setattr(
        build_script.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", ""),
    )

    with pytest.raises(
        build_script.MacInternalBuildError,
        match="failed to bind packaged llama library rpath",
    ):
        build_script._bind_llama_bundle_rpath(
            tmp_path,
            {"sidecar_sha256": "a" * 64, "dylib_count": 27},
        )


def test_internal_builder_requires_exact_policy_selected_eula_bytes() -> None:
    resources = REPO_ROOT / "src-tauri/resources"
    policy = json.loads(
        (resources / "eula-policy.json").read_text(encoding="utf-8")
    )
    installer = (resources / "EULA.txt").read_bytes()
    selected = (resources / policy["terms_path"]).read_bytes()

    build_script._assert_internal_legal_resources(REPO_ROOT)
    assert selected == installer
    assert hashlib.sha256(selected).hexdigest() == policy["terms_sha256"]
    assert "src-tauri/resources/eula/*.txt binary" in (
        REPO_ROOT / ".gitattributes"
    ).read_text(encoding="utf-8")


def test_internal_builder_fails_closed_on_mismatched_eula_bytes(tmp_path) -> None:
    source = REPO_ROOT / "src-tauri/resources"
    resources = tmp_path / "src-tauri/resources"
    (resources / "eula").mkdir(parents=True)
    for relative in ("EULA.txt", "eula-policy.json", "eula/2.1.txt"):
        destination = resources / relative
        destination.write_bytes((source / relative).read_bytes())
    (resources / "eula/2.1.txt").write_bytes(b"mismatched")

    with pytest.raises(
        build_script.MacInternalBuildError,
        match="internal EULA resource identity is invalid",
    ):
        build_script._assert_internal_legal_resources(tmp_path)


def test_internal_overlay_locks_identity_usage_intent_and_ad_hoc_signing() -> None:
    overlay = json.loads(
        (REPO_ROOT / "src-tauri/tauri.macos.conf.json").read_text(encoding="utf-8")
    )
    macos = overlay["bundle"]["macOS"]
    assert overlay["app"]["macOSPrivateApi"] is True
    import tomllib
    manifest = tomllib.loads((REPO_ROOT / "src-tauri/Cargo.toml").read_text())
    # Tauri checks the primary dependency, ignoring target-specific additions.
    assert "macos-private-api" in manifest["dependencies"]["tauri"]["features"]
    config = json.loads((REPO_ROOT / "src-tauri/tauri.conf.json").read_text())
    assert config["app"]["macOSPrivateApi"] is True

    assert overlay["identifier"] == build_script.INTERNAL_IDENTIFIER
    assert overlay["bundle"]["targets"] == ["app"]
    assert overlay["bundle"]["createUpdaterArtifacts"] is False
    assert overlay["plugins"]["updater"] == {"endpoints": [], "pubkey": ""}
    assert macos == {
        "minimumSystemVersion": "14.0",
        "signingIdentity": "-",
        "hardenedRuntime": False,
        "infoPlist": "Info.macos-internal.plist",
    }
    usage = (REPO_ROOT / "src-tauri/Info.macos-internal.plist").read_text(
        encoding="utf-8"
    )
    assert audit_script.EXPECTED_USAGE_DESCRIPTION in usage
    assert "icon.icns" in audit_script.EXPECTED_RESOURCES
    resources = overlay["bundle"]["resources"]
    assert {"../LICENSE", "../NOTICE", "../LICENSES/Chirp-MIT.txt"} <= set(resources)
    assert {"LICENSE", "NOTICE", "Chirp-MIT.txt"} <= audit_script.EXPECTED_RESOURCES


def test_bundle_audit_version_tracks_authoritative_tauri_config() -> None:
    config = json.loads(
        (REPO_ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    )

    assert audit_script.EXPECTED_VERSION == config["version"]


def test_internal_icns_locks_current_canonical_icon_content() -> None:
    source = REPO_ROOT / "src-tauri/icons/macos-app-icon-1024.png"
    icon = REPO_ROOT / "src-tauri/icons/icon.icns"

    assert hashlib.sha256(source.read_bytes()).hexdigest() == (
        audit_script.EXPECTED_ICON_SOURCE_SHA256
    )
    measurement = audit_script._icon_measurement(icon)
    assert measurement["file"] == audit_script.EXPECTED_ICON_FILE
    assert set(measurement["representations"]) == set(
        audit_script.EXPECTED_ICON_REPRESENTATIONS
    )
    assert measurement["representations"]["ic10"] == {
        "width": 1024,
        "height": 1024,
        "sha256": audit_script.EXPECTED_ICON_1024_SHA256,
    }


def test_internal_icns_rejects_different_1024_content(tmp_path) -> None:
    data = bytearray((REPO_ROOT / "src-tauri/icons/icon.icns").read_bytes())
    offset = 8
    while data[offset : offset + 4] != b"ic10":
        offset += struct.unpack(">I", data[offset + 4 : offset + 8])[0]
    chunk_size = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
    data[offset + chunk_size - 1] ^= 1
    stale = tmp_path / "icon.icns"
    stale.write_bytes(data)

    with pytest.raises(
        audit_script.BundleAuditError,
        match="canonical 1024 representation differs",
    ):
        audit_script._icon_measurement(stale)


def test_bundle_audit_rejects_wrong_icon_binding(tmp_path) -> None:
    contents = tmp_path / "Whisperstream.app/Contents"
    contents.mkdir(parents=True)
    plist = {
        "CFBundleIdentifier": audit_script.INTERNAL_IDENTIFIER,
        "CFBundleShortVersionString": audit_script.EXPECTED_VERSION,
        "CFBundleVersion": audit_script.EXPECTED_VERSION,
        "LSMinimumSystemVersion": audit_script.EXPECTED_MINIMUM_MACOS,
        "CFBundleExecutable": "whisperstream",
        "CFBundleIconFile": "stale.icns",
        "NSMicrophoneUsageDescription": audit_script.EXPECTED_USAGE_DESCRIPTION,
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(plist))

    with pytest.raises(
        audit_script.BundleAuditError,
        match="bundle identity field mismatch field=icon_file",
    ):
        audit_script.audit(contents.parent)


def test_internal_microphone_usage_copy_is_locked_and_truthful() -> None:
    expected = (
        "Whisperstream uses the microphone only while you dictate. "
        "Your speech is transcribed on this Mac, and the audio never leaves it."
    )
    usage = (REPO_ROOT / "src-tauri/Info.macos-internal.plist").read_text(
        encoding="utf-8"
    )

    assert audit_script.EXPECTED_USAGE_DESCRIPTION == expected
    assert expected in usage


def test_internal_engine_dependencies_use_shared_locked_public_graph() -> None:
    source = (REPO_ROOT / "scripts/build_macos_internal.py").read_text(encoding="utf-8")
    assert '["uv", "sync", "--locked", "--no-dev", "--project", str(source_root)' in source
    assert 'scripts/check-sherpa-source.py' in source
    assert 'build_macos_sherpa.py' not in source
    assert build_script.PYTHON_VERSION == "3.12.8"
    assert build_script.NUITKA_VERSION == "4.0.3"
    assert build_script.ROLLUP_NATIVE_VERSION == "4.60.2"
    assert build_script.ESBUILD_NATIVE_VERSION == "0.25.12"
    assert build_script.TAILWIND_OXIDE_VERSION == "4.2.4"
    assert build_script.LIGHTNINGCSS_NATIVE_VERSION == "1.32.0"
    assert build_script.RUST_TOOLCHAIN == "stable-aarch64-apple-darwin"
    assert build_script.RUSTC_VERSION == "1.97.1"


def test_internal_engine_keeps_supported_sherpa_worker_modules(tmp_path, monkeypatch) -> None:
    """The frozen child must retain every backend exposed by the Mac picker."""
    source_root = tmp_path / "source"
    build_script._stage_sources(source_root, "1.5.1")
    monkeypatch.setattr(
        build_script, "_engine_environment",
        lambda *args: (Path("isolated-python"), {"PYTHONPATH": str(source_root / "src")}),
    )
    commands = []

    def fake_run(argv, **kwargs):
        commands.append(argv)
        if argv[-1] == "--help":
            assert "PYTHONPATH" not in kwargs["env"]
            assert kwargs["cwd"] == tmp_path / "engine-output"
        if "nuitka" in argv:
            (tmp_path / "engine-output/whisperstream-engine").write_bytes(b"synthetic")
        return 0.0

    monkeypatch.setattr(build_script, "_run", fake_run)
    build_script._build_engine(tmp_path, tmp_path, source_root, {"WHISPERSTREAM_BUILD_SIGNING_IDENTITY": "local-test-identity"}, tmp_path, "1.5.1")
    assert commands[0][1] == "-c"
    assert "import whisperstream.main" in commands[0][2]
    assert "process.extractOne" in commands[0][2]
    assert "--macos-sign-identity=local-test-identity" in commands[1]
    assert commands[2] == [str(tmp_path / "engine-output/whisperstream-engine"), "--help"]
    argv = commands[1]
    assert "--include-package=rapidfuzz" in argv
    assert "--include-package=metaphone" in argv
    for backend in ("parakeet", "qwen", "cohere", "speech_gate"):
        module = f"whisperstream.stt_worker_{backend}"
        staged = source_root / f"src/whisperstream/stt_worker_{backend}.py"
        assert staged.is_file(), f"Supported worker missing from staged sources: {backend}"
        assert f"--nofollow-import-to={module}" not in argv
        assert f"--include-module={module}" in argv
    gate_module = source_root / "src/whisperstream/speech_gate.py"
    gate_constants = {}
    import ast
    for node in ast.parse(gate_module.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    gate_constants[target.id] = node.value.value
    assets = source_root / "src/whisperstream/assets"
    assert hashlib.sha256((assets / "silero_vad.onnx").read_bytes()).hexdigest() == gate_constants["MODEL_SHA256"]
    assert (assets / "silero-vad-LICENSE.txt").read_text().startswith("MIT License")
    assert "--include-module=whisperstream.speech_gate" in argv


def test_internal_engine_explicitly_includes_lazy_macos_keychain_adapter() -> None:
    source = (REPO_ROOT / "scripts/build_macos_internal.py").read_text(
        encoding="utf-8"
    )

    assert "--include-module=whisperstream.macos_protected_secret" in source
    assert "--include-module=whisperstream.macos_keychain_probe" in source
    assert "--include-module=whisperstream.macos_protected_storage_probe" in source
    assert "--include-module=whisperstream.macos_phase4_probe" in source
    assert "--nofollow-import-to=whisperstream.windows_protected_secret" in source


def test_internal_engine_explicitly_includes_lazy_macos_permission_adapter() -> None:
    source = (REPO_ROOT / "scripts/build_macos_internal.py").read_text(
        encoding="utf-8"
    )

    assert "--include-module=whisperstream.macos_permission_status" in source
    assert "--include-module=whisperstream.permission_actions" in source
    assert "--include-module=whisperstream.macos_permission_actions" in source
    assert "--nofollow-import-to=whisperstream.windows_permission_status" in source


def test_internal_engine_explicitly_includes_macos_capture_stack() -> None:
    source = (REPO_ROOT / "scripts/build_macos_internal.py").read_text(
        encoding="utf-8"
    )

    assert "--include-module=whisperstream.macos_capture" in source
    assert "--include-module=sounddevice" in source
    assert "--include-package=_sounddevice_data" in source
    assert "--include-package-data=_sounddevice_data" in source
    assert "--nofollow-import-to=whisperstream.audio_capture" in source


def test_internal_engine_explicitly_includes_macos_shortcut_stack() -> None:
    source = (REPO_ROOT / "scripts/build_macos_internal.py").read_text(
        encoding="utf-8"
    )

    assert "--include-module=whisperstream.macos_shortcuts" in source
    assert "--nofollow-import-to=whisperstream.keyboard_shortcuts" in source
    assert "--nofollow-import-to=pynput" in source


def test_internal_engine_explicitly_includes_macos_delivery_stack() -> None:
    source = (REPO_ROOT / "scripts/build_macos_internal.py").read_text(
        encoding="utf-8"
    )

    assert "--include-module=whisperstream.macos_delivery_adapter" in source
    assert "--nofollow-import-to=whisperstream.windows_delivery_adapter" in source
    assert "--nofollow-import-to=pyperclip" in source


def test_internal_engine_explicitly_includes_macos_tts_stack() -> None:
    source = (REPO_ROOT / "scripts/build_macos_internal.py").read_text(
        encoding="utf-8"
    )

    assert 'name = "onnxruntime"' in (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert "--include-module=whisperstream.macos_tts_playback" in source
    assert "--include-module=whisperstream.speech.kokoro_engine" in source
    assert "--include-module=whisperstream.speech.kokoro_frontend" in source
    assert "--nofollow-import-to=whisperstream.windows_tts_playback" in source


def test_internal_engine_explicitly_includes_macos_agent_stack() -> None:
    source = (REPO_ROOT / "scripts/build_macos_internal.py").read_text(
        encoding="utf-8"
    )

    assert "--include-module=whisperstream.macos_agent_integration" in source
    assert "--include-package-data=whisperstream" in source


def test_file_only_cli_is_explicit_and_implies_no_hotkey_registration() -> None:
    parser = _build_parser()
    args = parser.parse_args(["--serve", "--file-only"])

    assert args.serve is True
    assert args.file_only is True
    assert args.no_hotkeys is False


def test_internal_lifecycle_cli_keeps_worker_probe_file_only() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--serve",
            "--file-only",
            "--no-hotkeys",
            "--lifecycle-probe-worker",
            "--owner-pid",
            "1234",
        ]
    )

    assert args.serve is True
    assert args.file_only is True
    assert args.no_hotkeys is True
    assert args.lifecycle_probe_worker is True
    assert args.owner_pid == 1234


def test_internal_fixture_cli_keeps_real_transcription_probe_file_only() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--serve",
            "--file-only",
            "--no-hotkeys",
            "--fixture-probe-worker",
        ]
    )

    assert args.serve is True
    assert args.file_only is True
    assert args.no_hotkeys is True
    assert args.fixture_probe_worker is True


def test_file_only_app_skips_transcript_persistence() -> None:
    source = (REPO_ROOT / "src/whisperstream/main.py").read_text(encoding="utf-8")

    assert '"app persistence surfaces disabled mode=file_only"' in source
    assert "if self._file_only:" in source


def test_internal_lifecycle_platform_validation_imports_sys() -> None:
    source = (REPO_ROOT / "src/whisperstream/main.py").read_text(encoding="utf-8")

    assert "import sys" in source


def test_lifecycle_worker_probe_negotiates_without_loading_model() -> None:
    manager = macos_stt_manager.MacosFluidAudioManager.__new__(
        macos_stt_manager.MacosFluidAudioManager
    )
    manager._supervisor = MagicMock()
    manager._supervisor.pid = 4321
    manager._validate_worker_capabilities = MagicMock()
    manager._logger = MagicMock()
    manager._lifecycle_probe_only = True

    worker_pid = manager.start_worker_for_lifecycle_probe()

    assert worker_pid == 4321
    manager._supervisor.start.assert_called_once_with()
    manager._validate_worker_capabilities.assert_called_once_with()


def test_lifecycle_manager_defers_model_load_and_starts_only_worker(
    monkeypatch, tmp_path: Path
) -> None:
    supervisor = MagicMock()
    supervisor.pid = 4321
    supervisor.capabilities = {}
    monkeypatch.setattr(
        macos_stt_manager,
        "SttWorkerSupervisor",
        lambda **_kwargs: supervisor,
    )
    monkeypatch.setenv("CFFIXED_USER_HOME", str(tmp_path))

    manager = macos_stt_manager.MacosFluidAudioManager(
        logger=MagicMock(),
        timeout=0,
        lifecycle_probe_only=True,
    )
    try:
        assert supervisor.load.call_count == 0
        assert manager.start_worker_for_lifecycle_probe() == 4321
        supervisor.start.assert_called_once_with()
        assert supervisor.load.call_count == 0
    finally:
        manager.shutdown()
    manager._supervisor.load.assert_not_called()


def test_file_only_server_does_not_schedule_model_startup() -> None:
    server_source = (REPO_ROOT / "src/whisperstream/server/app.py").read_text(
        encoding="utf-8"
    )

    assert "self._app.stt_engine is None and not self._app.file_only" in server_source


def test_clipboard_module_stays_lazy_until_delivery() -> None:
    proxy = _LazyPyperclip()

    assert proxy._module is None
    assert "Foundation" not in sys.modules or sys.platform == "darwin"


def test_frozen_macos_worker_resolves_next_to_original_onefile(monkeypatch, tmp_path) -> None:
    engine = tmp_path / "Whisperstream.app/Contents/MacOS/whisperstream-engine"
    engine.parent.mkdir(parents=True)
    engine.write_bytes(b"engine")
    monkeypatch.setenv("NUITKA_ONEFILE_BINARY", str(engine))
    monkeypatch.delenv(macos_stt_manager.MACOS_STT_WORKER_ENV_VAR, raising=False)

    assert macos_stt_manager.default_macos_stt_worker_path() == engine.with_name(
        "whisperstream-macos-stt-worker"
    )


def test_macos_onefile_resolves_contents_resources(monkeypatch, tmp_path) -> None:
    contents = tmp_path / "Whisperstream.app/Contents"
    engine = contents / "MacOS/whisperstream-engine"
    resource = contents / "Resources/EULA.txt"
    engine.parent.mkdir(parents=True)
    resource.parent.mkdir(parents=True)
    engine.write_bytes(b"engine")
    resource.write_bytes(b"eula")
    monkeypatch.setenv("NUITKA_ONEFILE_BINARY", str(engine))

    assert eula._find_eula_path() == resource


def test_macos_bundle_resource_contract_resolves_eula(monkeypatch, tmp_path) -> None:
    resources = tmp_path / "Whisperstream.app/Contents/Resources"
    resource = resources / "EULA.txt"
    resources.mkdir(parents=True)
    resource.write_bytes(b"eula")
    monkeypatch.setenv("WHISPERSTREAM_BUNDLE_RESOURCES_DIR", str(resources))
    monkeypatch.delenv("NUITKA_ONEFILE_BINARY", raising=False)

    assert eula._find_eula_path() == resource


def test_compiled_macos_argv0_resolves_contents_resources(monkeypatch, tmp_path) -> None:
    contents = tmp_path / "Whisperstream.app/Contents"
    engine = contents / "MacOS/whisperstream-engine"
    resource = contents / "Resources/EULA.txt"
    engine.parent.mkdir(parents=True)
    resource.parent.mkdir(parents=True)
    engine.write_bytes(b"engine")
    resource.write_bytes(b"eula")
    monkeypatch.delenv("NUITKA_ONEFILE_BINARY", raising=False)
    monkeypatch.setattr(eula.sys, "argv", [str(engine)])

    assert eula._find_eula_path() == resource


def test_macos_worker_argv0_resolves_sibling_without_onefile_env(
    monkeypatch, tmp_path
) -> None:
    engine = tmp_path / "Whisperstream.app/Contents/MacOS/whisperstream-engine"
    engine.parent.mkdir(parents=True)
    engine.write_bytes(b"engine")
    monkeypatch.delenv("NUITKA_ONEFILE_BINARY", raising=False)
    monkeypatch.delenv(macos_stt_manager.MACOS_STT_WORKER_ENV_VAR, raising=False)
    monkeypatch.setattr(macos_stt_manager.sys, "argv", [str(engine)])

    assert macos_stt_manager.default_macos_stt_worker_path() == engine.with_name(
        "whisperstream-macos-stt-worker"
    )


def test_launch_probe_rejects_frontend_backend_exception(tmp_path) -> None:
    engine_log = tmp_path / "engine.log"
    engine_log.write_text("api unhandled_exception method=GET path=/content-free\n")

    with pytest.raises(launch_script.LaunchProbeError, match="backend exception"):
        launch_script._assert_frontend_requests_clean(engine_log)


def test_launch_probe_requires_guarded_main_webview_first_run_surface() -> None:
    frontend_log = (
        "[main] [2026-08-04T10:00:00Z] [INFO] "
        "[EULA] status loaded state=first_run"
    )
    engine_log = (
        "api request method=GET path=/api/eula/status\n"
        "api response method=GET path=/api/eula/status status=200 duration_ms=1\n"
    )
    status = {
        "state": "first_run",
        "needs_first_run": True,
        "bundled_version": "1.4",
        "bundled_sha256": "a" * 64,
        "bundled_text": "synthetic terms",
    }

    assert launch_script._main_webview_ready_state(
        frontend_log,
        engine_log,
        status,
        inline_contract=True,
    ) == "first-run-eula-guarded"


def test_launch_probe_does_not_accept_loading_or_other_webview_markers() -> None:
    status = {
        "state": "first_run",
        "needs_first_run": True,
        "bundled_version": "1.4",
        "bundled_sha256": "a" * 64,
        "bundled_text": "synthetic terms",
    }
    assert launch_script._main_webview_ready_state(
        "",
        "",
        status,
        inline_contract=True,
    ) is None
    assert launch_script._main_webview_ready_state(
        "[popup-eula] [INFO] [EULA] status loaded state=first_run",
        "api request method=GET path=/api/eula/status",
        status,
        inline_contract=False,
    ) is None


def test_launch_probe_rejects_main_webview_hydration_failure() -> None:
    frontend_log = (
        "[main] [2026-08-04T10:00:00Z] [ERROR] "
        "[App] config load failed reason=Failed to fetch"
    )

    with pytest.raises(launch_script.LaunchProbeError, match="hydration failure"):
        launch_script._main_webview_ready_state(
            frontend_log,
            "api request method=GET path=/api/eula/status",
            {
                "state": "first_run",
                "needs_first_run": True,
                "bundled_version": "1.4",
                "bundled_sha256": "a" * 64,
                "bundled_text": "synthetic terms",
            },
            inline_contract=True,
        )


def test_launch_probe_rejects_app_shell_hydration_before_first_run_acceptance() -> None:
    with pytest.raises(launch_script.LaunchProbeError, match="legal guard"):
        launch_script._main_webview_ready_state(
            "[main] [INFO] [App] config loaded",
            "\n".join(
                [
                    "api request method=GET path=/api/eula/status",
                    "api request method=GET path=/api/config",
                ]
            ),
            {
                "state": "first_run",
                "needs_first_run": True,
                "bundled_version": "1.4",
                "bundled_sha256": "a" * 64,
                "bundled_text": "synthetic terms",
            },
            inline_contract=True,
        )


def test_launch_probe_source_binds_first_run_to_inline_gate() -> None:
    source = (REPO_ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")

    assert launch_script._first_run_inline_contract(source) is True


def test_launch_probe_keeps_default_and_permission_launches_minimized() -> None:
    shell = Path("/isolated/Whisperstream.app/Contents/MacOS/whisperstream")

    assert launch_script._shell_launch_command(
        shell,
        prove_main_webview=False,
    ) == [str(shell), "--minimized"]
    args = launch_script._parse_args(
        ["--app", "/isolated/Whisperstream.app", "--output", "/tmp/report.json"]
    )
    permission_args = launch_script._parse_args(
        [
            "--app",
            "/isolated/Whisperstream.app",
            "--output",
            "/tmp/report.json",
            "--query-permissions",
        ]
    )
    assert args.prove_main_webview is False
    assert permission_args.prove_main_webview is False


def test_shell_probes_request_the_same_explicit_lifecycle_mode(tmp_path) -> None:
    assert launch_script.MACOS_ENGINE_SPAWN_MODE_ENV == (
        lifecycle_script.MACOS_ENGINE_SPAWN_MODE_ENV
    )
    assert launch_script.MACOS_ENGINE_LIFECYCLE_PROBE_MODE == (
        lifecycle_script.MACOS_ENGINE_LIFECYCLE_PROBE_MODE
    )
    assert launch_script._probe_spawn_environment() == {
        "WHISPERSTREAM_MACOS_ENGINE_SPAWN_MODE": "lifecycle-probe"
    }
    environment = lifecycle_script._base_environment(tmp_path)
    assert environment["WHISPERSTREAM_MACOS_ENGINE_SPAWN_MODE"] == "lifecycle-probe"


def test_real_launch_removes_selector_and_accepts_only_real_argv() -> None:
    inherited = {
        "KEEP": "sentinel",
        "WHISPERSTREAM_MACOS_ENGINE_SPAWN_MODE": "lifecycle-probe",
    }

    environment = launch_script._spawn_environment(
        inherited, real_dictation_runtime=True
    )

    assert environment == {"KEEP": "sentinel"}
    assert launch_script._validate_real_engine_arguments(
        [
            "--serve",
            "--port",
            "38420",
            "--owner-pid",
            "321",
            "--engine-generation",
            "synthetic-generation",
        ],
        owner_pid=321,
    ) == {
        "arguments": [
            "--serve",
            "--port",
            "38420",
            "--owner-pid",
            "<owner-pid>",
            "--engine-generation",
            "<generation-token>",
        ],
        "owner_pid_matches_shell": True,
        "generation_token_present": True,
        "probe_flags_absent": True,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "--serve",
            "--port",
            "38420",
            "--no-hotkeys",
            "--file-only",
            "--lifecycle-probe-worker",
            "--owner-pid",
            "321",
            "--engine-generation",
            "synthetic-generation",
        ],
        [
            "--serve",
            "--port",
            "38420",
            "--owner-pid",
            "999",
            "--engine-generation",
            "synthetic-generation",
        ],
    ],
)
def test_real_launch_rejects_probe_or_unowned_argv(arguments) -> None:
    with pytest.raises(launch_script.LaunchProbeError):
        launch_script._validate_real_engine_arguments(arguments, owner_pid=321)


def test_real_launch_cli_is_explicit_and_requires_model_source() -> None:
    args = launch_script._parse_args(
        [
            "--app",
            "/isolated/Whisperstream.app",
            "--output",
            "/tmp/report.json",
            "--prove-real-dictation-runtime",
            "--model-source",
            "/isolated/model",
        ]
    )

    assert args.prove_real_dictation_runtime is True
    assert args.model_source == Path("/isolated/model")


def test_launch_probe_visible_mode_omits_minimized_and_requires_readiness(
    monkeypatch,
) -> None:
    shell = Path("/isolated/Whisperstream.app/Contents/MacOS/whisperstream")
    process = MagicMock()
    wait_ready = MagicMock(return_value=("first-run-eula", 10.25))
    monkeypatch.setattr(launch_script, "_wait_main_webview_ready", wait_ready)

    args = launch_script._parse_args(
        [
            "--app",
            "/isolated/Whisperstream.app",
            "--output",
            "/tmp/report.json",
            "--prove-main-webview",
        ]
    )
    assert args.prove_main_webview is True
    assert launch_script._shell_launch_command(
        shell,
        prove_main_webview=True,
    ) == [str(shell)]
    assert launch_script._maybe_wait_main_webview_ready(
        process,
        Path("/isolated/whisperstream.log"),
        prove_main_webview=True,
    ) == ("first-run-eula", 10.25)
    wait_ready.assert_called_once_with(
        process,
        Path("/isolated/whisperstream.log"),
        20.0,
    )


def test_launch_probe_minimized_mode_skips_readiness_wait(monkeypatch) -> None:
    wait_ready = MagicMock()
    monkeypatch.setattr(launch_script, "_wait_main_webview_ready", wait_ready)

    assert launch_script._maybe_wait_main_webview_ready(
        MagicMock(),
        Path("/isolated/whisperstream.log"),
        prove_main_webview=False,
    ) == ("not-requested", None)
    wait_ready.assert_not_called()


def test_launch_probe_supplies_original_onefile_path() -> None:
    source = (REPO_ROOT / "scripts/verify_macos_internal_launch.py").read_text(
        encoding="utf-8"
    )

    assert (
        'app / "Contents/Resources/resources"'
        in source
    )


def test_launch_probe_permission_option_is_query_only() -> None:
    source = (REPO_ROOT / "scripts/verify_macos_internal_launch.py").read_text(
        encoding="utf-8"
    )

    assert 'PERMISSIONS_URL = f"http://127.0.0.1:{PORT}/api/permissions"' in source
    assert '"--query-permissions"' in source
    assert '"permission_request_called": False' in source
    assert "/api/permissions/request" not in source


def test_launch_probe_lineage_tolerates_an_exited_webkit_helper(monkeypatch) -> None:
    root = MagicMock()
    root.pid = 101
    root.is_running.return_value = True
    root.ppid.return_value = 1
    root.create_time.return_value = 100.25
    child = MagicMock()
    child.pid = 102
    child.is_running.return_value = True
    child.ppid.return_value = 101
    root.children.return_value = [child]
    monkeypatch.setattr(launch_script.psutil, "Process", lambda _pid: root)

    def getpgid(pid: int) -> int:
        if pid == child.pid:
            raise ProcessLookupError(pid)
        return pid

    monkeypatch.setattr(launch_script.os, "getpgid", getpgid, raising=False)

    assert launch_script._snapshot_lineage(root.pid) == [
        {
            "pid": root.pid,
            "ppid": 1,
            "pgid": root.pid,
            "create_time": 100.25,
        }
    ]


def test_launch_probe_waits_for_shell_owned_generation(tmp_path) -> None:
    shell_log = tmp_path / "shell.log"
    shell_log.write_text("[tauri] engine ready generation=1\n", encoding="utf-8")
    process = MagicMock()
    process.poll.return_value = None

    assert launch_script._wait_shell_engine_ready(shell_log, process, 0.1) >= 0


def test_launch_probe_accepts_privacy_minimized_frontend_connection(tmp_path) -> None:
    engine_log = tmp_path / "whisperstream.log"
    engine_log.write_text("ws connect status=accepted\n", encoding="utf-8")
    process = MagicMock()
    process.poll.return_value = None

    assert launch_script._wait_frontend_connection(engine_log, process, 0.1) >= 0
    server_source = (REPO_ROOT / "src/whisperstream/server/app.py").read_text(
        encoding="utf-8"
    )
    assert 'self._logger.info("ws connect status=accepted")' in server_source


def test_real_runtime_probe_does_not_depend_on_preacceptance_websocket(
    tmp_path,
) -> None:
    wait = MagicMock(return_value=1.0)
    process = MagicMock()

    assert launch_script._maybe_wait_frontend_connection(
        tmp_path / "whisperstream.log",
        process,
        real_dictation_runtime=True,
        waiter=wait,
    ) is None
    wait.assert_not_called()
    assert launch_script._maybe_wait_frontend_connection(
        tmp_path / "whisperstream.log",
        process,
        real_dictation_runtime=False,
        waiter=wait,
    ) == 1.0
    wait.assert_called_once()


def test_launch_probe_does_not_prelaunch_an_unscoped_engine() -> None:
    source = (REPO_ROOT / "scripts/verify_macos_internal_launch.py").read_text(
        encoding="utf-8"
    )

    assert 'engine_log_path = data / "logs/whisperstream.log"' in source
    assert '"owner": "packaged-shell"' in source
    assert 'payload.get("model_loaded") is True' in source
    assert "engine_process = subprocess.Popen" not in source

    app_source = (REPO_ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    marker = 'console.log("[EULA] showing first_run popup mode=inline");'
    assert app_source.index(marker) < app_source.index("void flushLogSync();", app_source.index(marker))


def test_launch_probe_allows_shutdown_response_socket_race(monkeypatch) -> None:
    monkeypatch.setattr(
        launch_script.urllib.request,
        "urlopen",
        MagicMock(side_effect=ConnectionResetError(54, "Connection reset by peer")),
    )

    assert launch_script._post_shutdown() is None


def test_launch_probe_accepts_frontend_shutdown_of_exact_engine(monkeypatch) -> None:
    process = MagicMock()
    process.poll.return_value = None
    monkeypatch.setattr(
        launch_script,
        "_post_shutdown",
        MagicMock(side_effect=launch_script.urllib.error.URLError("refused")),
    )
    wait_exit = MagicMock(return_value=True)
    monkeypatch.setattr(launch_script, "_wait_exit", wait_exit)

    assert launch_script._shutdown_exact_engine(process) is None
    wait_exit.assert_called_once_with(process, 1.0)


def test_lifecycle_probe_uses_exact_identities_and_never_global_names() -> None:
    source = (
        REPO_ROOT / "scripts/verify_macos_process_lifecycle.py"
    ).read_text(encoding="utf-8")

    assert "process.create_time()" in source
    assert "os.kill(target[\"pid\"], signal.SIGKILL)" in source
    assert "sentinel_process.terminate()" in source
    assert 'shutil.copyfile("/usr/bin/false", engine)' in source
    assert "protocol_version = 'HTTP/1.1'" in source
    assert "sys.argv.index('--engine-generation')" in source
    assert "'X-Whisperstream-Engine-Generation', generation" in source
    assert source.count('"[tauri] engine ready generation=1", process, 5.0') >= 4
    assert '"exit_classification": "signal-SIGKILL"' in source
    assert "not _identity_present(item)" in source
    assert '"engine_spawn_after_shell_seconds"' not in source
    assert 'timings[f"{role}_spawn_after_shell_seconds"]' in source
    assert '"shutdown_seconds": round(shutdown_seconds, 3)' in source
    assert "pkill" not in source
    assert "killall" not in source


def test_lifecycle_privacy_measurement_is_bounded_and_content_free(tmp_path) -> None:
    root = tmp_path / "probe"
    logs = root / "data/logs"
    workers = logs / "stt-workers"
    workers.mkdir(parents=True)
    (logs / "whisperstream.log").write_text(
        "engine ready generation=1 cleanup_verified=true\n",
        encoding="utf-8",
    )
    (workers / "worker-123.last-operation.json").write_text(
        '{"operation":"hello","phase":"complete"}\n',
        encoding="utf-8",
    )
    packaged_audio = root / "normal/cache/runtime/whisperstream/assets/start.wav"
    packaged_audio.parent.mkdir(parents=True)
    packaged_audio.write_bytes(b"")

    result = lifecycle_script._privacy_measurements(root)

    assert result["prohibited_matches"] == 0
    assert result["audio_files"] == 0
    assert result["packaged_audio_assets_excluded"] == 1
    assert result["engine_log_files"] == 1
    assert result["worker_diagnostic_files"] == 1


def test_lifecycle_privacy_measurement_rejects_runtime_audio(tmp_path) -> None:
    root = tmp_path / "probe"
    audio = root / "data/history/captured.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"")

    with pytest.raises(
        lifecycle_script.LifecycleProbeError,
        match="created an audio artifact",
    ):
        lifecycle_script._privacy_measurements(root)


def test_bundle_compare_requires_equivalent_stage_and_reports_binary_differences(
    tmp_path,
) -> None:
    first = tmp_path / "first.app"
    second = tmp_path / "second.app"
    first.mkdir()
    second.mkdir()
    (first / "binary").write_bytes(b"first")
    (second / "binary").write_bytes(b"second")
    identity = {"identifier": build_script.INTERNAL_IDENTIFIER}
    source = {"files": 1, "bytes": 1, "tree_sha256": "a" * 64}
    first_report = {
        "identity": identity,
        "source_stage": source,
        "bundle": audit_script._tree_measurement(audit_script._inventory(first)),
    }
    second_report = {
        "identity": identity,
        "source_stage": source,
        "bundle": audit_script._tree_measurement(audit_script._inventory(second)),
    }

    result = audit_script.compare(first_report, second_report, first, second)

    assert result["equivalent_staged_inputs"] is True
    assert result["stable_identity"] is True
    assert result["byte_identical"] is False
    assert result["differing_files"] == ["binary"]


def test_build_root_can_be_any_fresh_directory(tmp_path) -> None:
    build_root = tmp_path / "nested/build"
    assert build_script._validate_new_build_root(build_root) == build_root.resolve()
    with pytest.raises(build_script.MacInternalBuildError, match="already exists"):
        build_script._validate_new_build_root(build_root)


@pytest.fixture
def default_build(monkeypatch, tmp_path):
    pytest.importorskip("fcntl")
    parent = tmp_path / "dist/macos"
    monkeypatch.setattr(build_script, "DEFAULT_BUILD_PARENT", parent)
    state = {"version": "1", "fail": False}

    def fake_build(root, _tools, *, bundles, signing="local", team_id=None, signing_sha1=None):
        assert (signing, team_id, signing_sha1) == ("local", None, None)
        build_script._validate_new_build_root(root)
        (root / "logs").mkdir()
        (root / "logs/engine-build.log").write_text(state["version"])
        (root / "intermediate").write_text("disposable compilation output")
        if state["fail"]:
            raise build_script.MacInternalBuildError("synthetic compiler failure")
        app = root / "bundle/Whisperstream.app"
        app.mkdir(parents=True)
        (app / "binary").write_text(state["version"])
        (app / "link").symlink_to("binary")
        artifacts = {"app": {"path": str(app)}}
        if bundles != "app":
            dmg = root / f"Whisperstream_{state['version']}_aarch64.dmg"
            dmg.write_text(state["version"])
            artifacts["dmg"] = {"path": str(dmg)}
        return {"artifacts": artifacts, "identity": {"version": state["version"]},
            "signing": {"kind": "developer-id"}}

    def fake_copy(path, platform, name):
        destination = parent / "builds" / platform / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        return destination

    monkeypatch.setattr(build_script, "build", fake_build)
    monkeypatch.setattr(build_script, "copy_to_builds", fake_copy)
    return parent, state


def test_default_build_replaces_fixed_outputs_and_removes_intermediates(default_build):
    parent, state = default_build
    for version in ("1", "2"):
        state["version"] = version
        result = build_script._build_default(parent / "tools", bundles="both")
        latest = parent / "latest"
        assert result["artifacts"]["dmg"]["path"] == str(latest / "Whisperstream.dmg")
        assert (latest / "Whisperstream.dmg").read_text() == version
        assert (latest / "Whisperstream.app/binary").read_text() == version
        assert (latest / "Whisperstream.app/link").is_symlink()
        assert (latest / "logs/engine-build.log").read_text() == version
        assert json.loads((latest / "build-report.json").read_text()) == result
        assert set(p.name for p in parent.iterdir()) == {"latest", ".build.lock"}


def test_default_failed_build_preserves_installer_and_keeps_only_latest_failure_logs(default_build):
    parent, state = default_build
    build_script._build_default(parent / "tools", bundles="both")
    state["fail"] = True
    for version in ("2", "3"):
        state["version"] = version
        with pytest.raises(build_script.MacInternalBuildError, match="synthetic compiler failure"):
            build_script._build_default(parent / "tools", bundles="both")
        assert (parent / "latest/Whisperstream.dmg").read_text() == "1"
        assert (parent / "last-failed-logs/engine-build.log").read_text() == version
        assert set(p.name for p in parent.iterdir()) == {"latest", "last-failed-logs", ".build.lock"}


def test_default_app_only_build_does_not_leave_stale_dmg(default_build):
    parent, state = default_build
    build_script._build_default(parent / "tools", bundles="both")
    state["version"] = "2"
    build_script._build_default(parent / "tools", bundles="app")
    assert not (parent / "latest/Whisperstream.dmg").exists()
    assert (parent / "latest/Whisperstream.app/binary").read_text() == "2"


def test_default_command_uses_stable_output(default_build, monkeypatch, capsys):
    parent, _state = default_build
    monkeypatch.setattr(build_script, "_assert_native_host", lambda: None)
    assert build_script.main(["--bundles", "both"]) == 0
    output = capsys.readouterr().out
    assert str(parent / "latest/Whisperstream.dmg") in output
    assert (parent / "latest/Whisperstream.dmg").exists()
    assert (parent / f"builds/mac/Whisperstream_{_state['version']}_aarch64.dmg").read_text() == _state["version"]
    assert not list(parent.glob(".build-*"))


def test_default_build_rejects_concurrent_build(default_build):
    import fcntl

    parent, _state = default_build
    parent.mkdir(parents=True)
    with (parent / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(build_script.MacInternalBuildError, match="already running"):
            build_script._build_default(parent / "tools", bundles="both")
    assert set(p.name for p in parent.iterdir()) == {".build.lock"}


def test_output_promotion_failure_restores_previous_installer(tmp_path, monkeypatch):
    staged = tmp_path / "staged"
    destination = tmp_path / "latest"
    staged.mkdir()
    destination.mkdir()
    (destination / "Whisperstream.dmg").write_text("previous")
    rename = Path.rename

    def fail_promotion(source, target):
        if source == staged:
            raise OSError("synthetic rename failure")
        return rename(source, target)

    monkeypatch.setattr(Path, "rename", fail_promotion)
    with pytest.raises(OSError, match="synthetic rename failure"):
        build_script._replace_build_output(staged, destination)
    assert (destination / "Whisperstream.dmg").read_text() == "previous"


def test_default_tool_root_is_not_swept_by_macos_tmp_cleanup():
    root = build_script.DEFAULT_TOOL_ROOT
    assert root.is_relative_to(Path.home() / "Library/Caches")
    assert not root.is_relative_to(Path("/tmp")) and not root.is_relative_to(Path("/private/tmp"))


def test_explicit_build_root_still_retains_full_build_tree(tmp_path, monkeypatch):
    build = MagicMock(return_value={"artifacts": {}})
    monkeypatch.setattr(build_script, "build", build)
    root = tmp_path / "audit"
    assert build_script.main(["--build-root", str(root), "--bundles", "both"]) == 0
    build.assert_called_once_with(
        root, build_script.DEFAULT_TOOL_ROOT, bundles="both",
        signing="local", team_id=None, signing_sha1=None,
    )


def test_distribution_credentials_fail_closed(monkeypatch) -> None:
    for name in build_script.FORBIDDEN_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APPLE_SIGNING_IDENTITY", "synthetic-test-marker")

    with pytest.raises(build_script.MacInternalBuildError, match="not allowed"):
        build_script._assert_non_distribution_environment()


def test_native_tool_helper_rejects_non_macho_file(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "not-a-tool"
    marker.write_text("marker", encoding="utf-8")
    monkeypatch.setattr(
        build_script.subprocess,
        "run",
        MagicMock(
            return_value=build_script.subprocess.CompletedProcess(
                args=["lipo", "-archs", str(marker)],
                returncode=1,
                stdout="",
                stderr="not a Mach-O file",
            )
        ),
    )

    with pytest.raises(build_script.MacInternalBuildError, match="not native arm64"):
        build_script._assert_arm64_macho(marker, "test tool")


def test_bundle_required_resources_accepts_packaged_cli_path():
    inventory = {"Contents/Resources/" + name: {} for name in audit_script.EXPECTED_RESOURCES}
    inventory["Contents/Resources/bin/whisperstream"] = {}
    audit_script._assert_required_resources(inventory)


def test_bundle_required_resources_rejects_cli_in_wrong_directory():
    inventory = {"Contents/Resources/" + name: {} for name in audit_script.EXPECTED_RESOURCES}
    inventory["Contents/MacOS/whisperstream"] = {}
    with pytest.raises(audit_script.BundleAuditError, match="required CLI resource"):
        audit_script._assert_required_resources(inventory)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("developer-id", "Whisperstream_2.0.0_aarch64.dmg"),
     ("self-signed", "Whisperstream_2.0.0_aarch64-local-signed.dmg")],
)
def test_finished_dmg_is_copied_to_builds_mac(monkeypatch, tmp_path, kind, expected):
    dmg = tmp_path / "latest/Whisperstream.dmg"
    dmg.parent.mkdir()
    dmg.write_bytes(b"dmg")
    copied = []
    monkeypatch.setattr(build_script, "copy_to_builds",
        lambda path, platform, name: copied.append((Path(path), platform, name)) or Path(name))
    result = {"artifacts": {"dmg": {"path": str(dmg)}}, "identity": {"version": "2.0.0"},
        "signing": {"kind": kind}}
    build_script._copy_dmg_to_builds(result)
    assert copied == [(dmg, "mac", expected)]


def test_app_only_build_copies_nothing_to_builds(monkeypatch):
    monkeypatch.setattr(build_script, "copy_to_builds", lambda *a: pytest.fail("no DMG to copy"))
    assert build_script._copy_dmg_to_builds({"artifacts": {}, "identity": {"version": "2.0.0"},
        "signing": {"kind": "developer-id"}}) is None


# --- Updater build -----------------------------------------------------------

def _provider_tree(root: Path) -> dict[str, str]:
    """A synthetic updater provider inside ``root``, as its hook variables."""
    (root / "edition/native").mkdir(parents=True)
    (root / "edition/web/modules").mkdir(parents=True)
    (root / "edition/native/updater.rs").write_text("// module", encoding="utf-8")
    (root / "edition/native/policy.json").write_text("{}", encoding="utf-8")
    (root / "edition/native/overlay.json").write_text(json.dumps({
        "plugins": {"updater": {"endpoints": ["https://updates.example.invalid/feed/latest.json"],
                                "pubkey": "synthetic"}},
    }), encoding="utf-8")
    (root / "edition/web/edition.json").write_text("{}", encoding="utf-8")
    (root / "edition/web/modules/updater.ts").write_text("export {};", encoding="utf-8")
    return {
        "WHISPERSTREAM_UPDATER_RS": str(root / "edition/native/updater.rs"),
        "WHISPERSTREAM_UPDATER_POLICY": str(root / "edition/native/policy.json"),
        "WHISPERSTREAM_TAURI_OVERLAY": str(root / "edition/native/overlay.json"),
        "WHISPERSTREAM_FRONTEND_EDITION": str(root / "edition/web/edition.json"),
    }


def test_updater_ships_only_in_complete_developer_id_builds(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_script, "REPO_ROOT", tmp_path)
    hooks = _provider_tree(tmp_path)
    assert build_script._updater_providers({}, "developer-id") is None
    assert build_script._updater_providers(hooks, "local") is None
    providers = build_script._updater_providers(hooks, "developer-id")
    assert providers == {
        "WHISPERSTREAM_UPDATER_RS": "edition/native/updater.rs",
        "WHISPERSTREAM_UPDATER_POLICY": "edition/native/policy.json",
        "WHISPERSTREAM_TAURI_OVERLAY": "edition/native/overlay.json",
        "WHISPERSTREAM_FRONTEND_EDITION": "edition/web/edition.json",
    }
    partial = dict(hooks)
    del partial["WHISPERSTREAM_TAURI_OVERLAY"]
    with pytest.raises(build_script.MacInternalBuildError, match="incomplete"):
        build_script._updater_providers(partial, "developer-id")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.rs"
    outside.write_text("// other checkout", encoding="utf-8")
    with pytest.raises(build_script.MacInternalBuildError, match="inside this checkout"):
        build_script._updater_providers({**hooks, "WHISPERSTREAM_UPDATER_RS": str(outside)}, "developer-id")


def test_updater_providers_are_staged_with_their_siblings(tmp_path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(build_script, "REPO_ROOT", checkout)
    providers = build_script._updater_providers(_provider_tree(checkout), "developer-id")
    source_root = tmp_path / "source"
    staged = build_script._stage_updater_providers(source_root, providers)
    assert staged["WHISPERSTREAM_UPDATER_RS"] == str(source_root / "edition/native/updater.rs")
    # build.rs and frontend/edition.ts accept only providers inside the built tree,
    # and the native module includes the policy and overlay beside it.
    for relative in ("edition/native/policy.json", "edition/native/overlay.json",
                     "edition/web/modules/updater.ts"):
        assert (source_root / relative).is_file(), relative


def test_child_environment_never_passes_hooks_or_updater_key() -> None:
    inherited = {"PATH": "/usr/bin", "TAURI_SIGNING_PRIVATE_KEY": "secret",
                 "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": "pw",
                 **{name: "x" for name in build_script.UPDATER_HOOK_ENVS}}
    assert build_script._child_environment(inherited) == {"PATH": "/usr/bin"}


def test_updater_build_admits_only_the_updater_key(monkeypatch) -> None:
    for name in build_script.FORBIDDEN_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "synthetic-test-marker")
    with pytest.raises(build_script.MacInternalBuildError, match="not allowed"):
        build_script._assert_non_distribution_environment()
    build_script._assert_non_distribution_environment(updater=True)
    monkeypatch.setenv("APPLE_SIGNING_IDENTITY", "synthetic-test-marker")
    with pytest.raises(build_script.MacInternalBuildError, match="not allowed"):
        build_script._assert_non_distribution_environment(updater=True)
    monkeypatch.delenv("APPLE_SIGNING_IDENTITY")
    monkeypatch.delenv("TAURI_SIGNING_PRIVATE_KEY")
    with pytest.raises(build_script.MacInternalBuildError, match="TAURI_SIGNING_PRIVATE_KEY"):
        build_script._assert_non_distribution_environment(updater=True)


def test_tauri_overlay_merges_before_artifacts_are_turned_off() -> None:
    plain = build_script._tauri_build_argv(Path("/tmp/tauri"), "both")
    assert plain.count("--config") == 1
    argv = build_script._tauri_build_argv(Path("/tmp/tauri"), "both", overlay="/tmp/overlay.json")
    first = argv.index("--config")
    assert argv[first + 1] == "/tmp/overlay.json"
    builder = json.loads(argv[argv.index("--config", first + 1) + 1])
    # The update bundle is packed after notarization, not by Tauri.
    assert builder["bundle"]["createUpdaterArtifacts"] is False


def test_update_bundle_feed_points_at_the_versioned_tarball(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_script, "REPO_ROOT", tmp_path)
    hooks = _provider_tree(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("## [2.0.0] - 2026-10-25\n\n- Mac updates\n", encoding="utf-8")
    app = tmp_path / "out/Whisperstream.app"
    app.mkdir(parents=True)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(argv, *, cwd, env, log_path):
        calls.append((argv, env))
        if argv[1:3] == ["signer", "sign"]:
            Path(argv[3] + ".sig").write_text("synthetic-signature", encoding="utf-8")
        else:
            Path(argv[2]).write_bytes(b"tarball")
        return 0.0

    monkeypatch.setattr(build_script, "_run", run)
    update = build_script._write_update_bundle(
        app, "2.0.0", tauri_cli=Path("/tmp/tauri"), node_path="/tmp/node",
        overlay=hooks["WHISPERSTREAM_TAURI_OVERLAY"], env={"PATH": "/usr/bin"},
        signing_keys={"TAURI_SIGNING_PRIVATE_KEY": "secret"}, log_dir=tmp_path / "logs",
    )
    (tar, tar_env), (sign, sign_env) = calls
    assert tar[:2] == ["/usr/bin/tar", "-czf"] and tar[-1] == "Whisperstream.app"
    assert tar_env["COPYFILE_DISABLE"] == "1" and "TAURI_SIGNING_PRIVATE_KEY" not in tar_env
    assert sign_env["TAURI_SIGNING_PRIVATE_KEY"] == "secret"
    url = "https://updates.example.invalid/feed/2.0.0/Whisperstream.app.tar.gz"
    assert update["url"] == url
    feed = json.loads(update["manifest"].read_text(encoding="utf-8"))
    assert feed["version"] == "2.0.0" and feed["notes"] == "- Mac updates"
    assert feed["platforms"] == {"darwin-aarch64": {"signature": "synthetic-signature", "url": url}}
