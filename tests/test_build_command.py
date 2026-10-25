"""Focused tests for the package-build command and sherpa native preflight."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from email.message import Message
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import build as build_script


def _load_native_verifier():
    path = SCRIPTS_DIR / "verify-sherpa-binary.py"
    spec = importlib.util.spec_from_file_location("test_sherpa_verifier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDistribution:
    def __init__(
        self,
        root: Path,
        *,
        version: str = "1.13.3+whisperstream.2",
        name: str = "sherpa-onnx",
        native_names: tuple[str, ...] = (
            "sherpa_onnx/lib/_sherpa_onnx.cp312-win_amd64.pyd",
            "sherpa_onnx/lib/sherpa-onnx-c-api.dll",
        ),
    ) -> None:
        self.root = root
        self.version = version
        self.files = [Path(name) for name in native_names]
        self.metadata = Message()
        self.metadata["Name"] = name

    def locate_file(self, relative: Path) -> Path:
        return self.root / relative


def _make_distribution(
    tmp_path: Path,
    *,
    version: str = "1.13.3+whisperstream.2",
    extension_data: bytes = b"reviewed native payload",
) -> FakeDistribution:
    distribution = FakeDistribution(tmp_path, version=version)
    for relative in distribution.files:
        path = distribution.locate_file(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            extension_data if path.suffix.lower() == ".pyd" else b"clean dll"
        )
    return distribution


def test_nuitka_uses_locked_dependency_resolution():
    argv = build_script._nuitka_argv("1.2.3", "1.2.3.0")

    assert argv[:4] == ["uv", "run", "--locked", "--extra"]


def test_nuitka_includes_lazy_windows_platform_adapters():
    argv = build_script._nuitka_argv("1.2.3", "1.2.3.0")

    assert "--include-module=whisperstream.windows_protected_secret" in argv
    assert "--include-module=whisperstream.windows_permission_status" in argv
    assert "--include-module=whisperstream.windows_protected_storage_probe" in argv
    assert "--include-module=whisperstream.permission_actions" in argv
    assert "--include-module=whisperstream.audio_capture" in argv
    assert "--include-module=whisperstream.windows_audio_policy" in argv
    assert "--include-module=whisperstream.windows_history_security" in argv
    assert "--include-module=whisperstream.keyboard_shortcuts" in argv
    assert "--include-module=whisperstream.windows_delivery_adapter" in argv
    assert "--include-module=whisperstream.windows_app_context" in argv
    excluded_macos_modules = {
        argument.removeprefix("--nofollow-import-to=")
        for argument in argv
        if argument.startswith("--nofollow-import-to=whisperstream.macos_")
    }
    assert excluded_macos_modules == {
        "whisperstream.macos_capture",
        "whisperstream.macos_delivery_adapter",
        "whisperstream.macos_keychain_probe",
        "whisperstream.macos_permission_actions",
        "whisperstream.macos_permission_status",
        "whisperstream.macos_protected_secret",
        "whisperstream.macos_protected_storage_probe",
        "whisperstream.macos_shortcuts",
        "whisperstream.macos_stt_manager",
    }


def _write_portable_inputs(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    release_exe = tmp_path / "target" / "release" / "whisperstream.exe"
    release_exe.parent.mkdir(parents=True)
    release_exe.write_bytes(b"shell")

    resources = repo_root / "src-tauri" / "resources"
    binaries = repo_root / "src-tauri" / "binaries"
    (repo_root / "dist").mkdir(parents=True)
    (repo_root / "dist" / "whisperstream-engine.exe").write_bytes(b"engine")
    (repo_root / "LICENSE").write_bytes(b"license")
    (repo_root / "NOTICE").write_bytes(b"notice")
    (repo_root / "LICENSES").mkdir()
    (repo_root / "LICENSES" / "Chirp-MIT.txt").write_bytes(b"chirp mit")
    cli = repo_root / "src-tauri" / "bin" / "whisperstream.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"cli")
    for relative_name in build_script._PORTABLE_RESOURCE_FILES:
        path = resources / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative_name.encode("ascii"))
    sidecar = binaries / "llama-server-x86_64-pc-windows-msvc.exe"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_bytes(b"llama")
    for filename in (*build_script._PORTABLE_LLAMA_DLLS, *build_script._PORTABLE_LLAMA_NOTICES):
        (binaries / filename).write_bytes(filename.encode("ascii"))

    shortcuts: list[Path] = []
    monkeypatch.setattr(build_script.b, "REPO_ROOT", repo_root)
    monkeypatch.setattr(build_script, "_create_start_menu_shortcut", shortcuts.append)
    return repo_root, release_exe, shortcuts


def test_portable_payload_matches_windows_bundle_runtime_contract(tmp_path, monkeypatch):
    repo_root, release_exe, _shortcuts = _write_portable_inputs(tmp_path, monkeypatch)
    build_script._assemble_portable_folder(release_exe)

    portable = repo_root / "dist" / "Whisperstream"
    assert (portable / "Whisperstream.exe").read_bytes() == b"shell"
    assert (portable / "whisperstream-engine.exe").read_bytes() == b"engine"
    assert (portable / "llama-server.exe").read_bytes() == b"llama"
    assert (portable / "LICENSE").read_bytes() == b"license"
    assert (portable / "NOTICE").read_bytes() == b"notice"
    assert (portable / "LICENSES" / "Chirp-MIT.txt").read_bytes() == b"chirp mit"
    assert {
        path.relative_to(portable / "resources").as_posix()
        for path in (portable / "resources").rglob("*")
        if path.is_file()
    } == set(build_script._PORTABLE_RESOURCE_FILES)
    assert {
        path.name for path in (portable / "binaries").iterdir() if path.is_file()
    } == set(build_script._PORTABLE_LLAMA_DLLS) | set(build_script._PORTABLE_LLAMA_NOTICES)

    tauri_windows = json.loads(
        (REPO_ROOT / "src-tauri" / "tauri.windows.conf.json").read_text(
            encoding="utf-8"
        )
    )
    configured_dlls = {
        Path(item).name
        for item in tauri_windows["bundle"]["resources"]
        if item.lower().endswith(".dll")
    }
    assert set(build_script._PORTABLE_LLAMA_DLLS) == configured_dlls


@pytest.mark.parametrize("missing", ["NOTICE", "LICENSES/Chirp-MIT.txt"])
def test_missing_portable_notice_fails_before_shortcut(tmp_path, monkeypatch, missing):
    repo_root, release_exe, shortcuts = _write_portable_inputs(tmp_path, monkeypatch)
    (repo_root / missing).unlink()
    with pytest.raises(FileNotFoundError):
        build_script._assemble_portable_folder(release_exe)
    assert shortcuts == []


def test_bundles_ship_the_upstream_notice():
    # The Windows overlay replaces the base resource list, so the notices are
    # declared there (and in the platform contract's Windows resources).
    config = json.loads(
        (REPO_ROOT / "src-tauri" / "tauri.windows.conf.json").read_text(encoding="utf-8")
    )
    resources = config["bundle"]["resources"]
    assert {"../LICENSE", "../NOTICE", "../LICENSES/Chirp-MIT.txt"} <= set(resources)
    for relative in ("NOTICE", "LICENSES/Chirp-MIT.txt"):
        assert (REPO_ROOT / relative).is_file()


def test_nuitka_includes_native_trust_store_package():
    argv = build_script._nuitka_argv("1.2.3", "1.2.3.0")

    assert "--include-package=truststore" in argv


def test_nuitka_includes_speech_detector_and_its_license():
    argv = build_script._nuitka_argv("1.2.3", "1.2.3.0")
    for name in ('silero_vad.onnx', 'silero-vad-LICENSE.txt'):
        assert f'--include-data-files=src/whisperstream/assets/{name}=whisperstream/assets/{name}' in argv
        assert (REPO_ROOT / 'src/whisperstream/assets' / name).is_file()


def test_public_native_preflight_wraps_verification_failure(monkeypatch):
    from types import SimpleNamespace
    def fail():
        raise ValueError("native file hash mismatch")
    loader = SimpleNamespace(exec_module=lambda module: setattr(module, "verify", fail))
    monkeypatch.setattr(build_script.importlib.util, "spec_from_file_location",
                        lambda *_: SimpleNamespace(loader=loader))
    monkeypatch.setattr(build_script.importlib.util, "module_from_spec", lambda _: SimpleNamespace())
    with pytest.raises(build_script.SherpaPreflightError, match="native file hash mismatch"):
        build_script._verify_installed_sherpa()


def test_selected_stt_model_preflight_rejects_missing_and_empty_files(tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "encoder.onnx").write_bytes(b"ready")
    (model_dir / "decoder.onnx").write_bytes(b"")

    with pytest.raises(
        build_script.BuildEnvironmentPreflightError,
        match=r"model=test-model.*decoder\.onnx, tokens\.txt.*whisperstream\.setup",
    ):
        build_script._verify_selected_stt_model(
            selected=(
                "test-model",
                model_dir,
                ("encoder.onnx", "decoder.onnx", "tokens.txt"),
            )
        )


def test_selected_stt_model_preflight_accepts_complete_artifact(tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    for name in ("encoder.onnx", "decoder.onnx"):
        (model_dir / name).write_bytes(b"ready")

    assert build_script._verify_selected_stt_model(
        selected=("test-model", model_dir, ("encoder.onnx", "decoder.onnx"))
    ) == model_dir


@pytest.mark.parametrize("nuitka_version", [(4, 0, 3), (4, 0, 8)])
def test_compiler_preflight_rejects_leaking_python_314_pair(nuitka_version):
    with pytest.raises(
        build_script.BuildEnvironmentPreflightError,
        match=r"Python 3\.14 with Nuitka 4\.0\.\d leaks memory",
    ):
        build_script._verify_compiler_runtime((3, 14), nuitka_version)


@pytest.mark.parametrize(
    ("python_version", "nuitka_version"),
    [((3, 13), (4, 0, 3)), ((3, 12), (4, 0, 3)), ((3, 14), (4, 1)), ((3, 14), (4, 3))],
)
def test_compiler_preflight_allows_unaffected_pairs(python_version, nuitka_version):
    build_script._verify_compiler_runtime(python_version, nuitka_version)


def test_compiler_preflight_reads_locked_nuitka_version():
    assert build_script._locked_nuitka_version() >= (4, 0)


def test_python_version_pin_avoids_leaking_compiler_pair():
    pinned = (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    major, minor = (int(part) for part in pinned.split(".")[:2])
    build_script._verify_compiler_runtime((major, minor))


def test_runtime_preflight_rejects_engine_from_same_version_cache(tmp_path):
    engine = (
        tmp_path
        / "Whisperstream-runtime"
        / "1.5.0"
        / "whisperstream-engine.exe"
    )

    with pytest.raises(
        build_script.BuildEnvironmentPreflightError,
        match=r"pid=42.*Exit Whisperstream 1\.5\.0 completely",
    ):
        build_script._verify_same_version_runtime_not_running(
            "1.5.0",
            platform_name="win32",
            local_app_data=tmp_path,
            processes=[
                {"pid": 42, "name": "whisperstream-engine.exe", "exe": str(engine)}
            ],
        )


def test_runtime_preflight_allows_other_versions_and_processes(tmp_path):
    other_engine = (
        tmp_path
        / "Whisperstream-runtime"
        / "1.4.0"
        / "whisperstream-engine.exe"
    )
    same_version_app = (
        tmp_path / "Whisperstream-runtime" / "1.5.0" / "Whisperstream.exe"
    )

    runtime_dir = build_script._verify_same_version_runtime_not_running(
        "1.5.0",
        platform_name="win32",
        local_app_data=tmp_path,
        processes=[
            {
                "pid": 10,
                "name": "whisperstream-engine.exe",
                "exe": str(other_engine),
            },
            {"pid": 11, "name": "Whisperstream.exe", "exe": str(same_version_app)},
            {"pid": 12, "name": "python.exe", "exe": str(tmp_path / "python.exe")},
        ],
    )

    assert runtime_dir == tmp_path / "Whisperstream-runtime" / "1.5.0"


def test_runtime_preflight_is_windows_only():
    assert (
        build_script._verify_same_version_runtime_not_running(
            "1.5.0",
            platform_name="linux",
        )
        is None
    )


def test_build_pipeline_runs_environment_and_sherpa_preflights_before_nuitka(
    monkeypatch,
):
    order: list[str] = []

    monkeypatch.setattr(build_script.b, "read_tauri_version", lambda: "1.5.0")
    monkeypatch.setattr(
        build_script,
        "_verify_build_environment",
        lambda version: order.append(f"environment:{version}"),
    )

    def fail_after_sherpa_preflight():
        order.append("sherpa")
        raise build_script.SherpaPreflightError("stop")

    monkeypatch.setattr(
        build_script,
        "_verify_installed_sherpa",
        fail_after_sherpa_preflight,
    )
    monkeypatch.setattr(
        build_script,
        "_step_nuitka",
        lambda *_args, **_kwargs: order.append("nuitka"),
    )

    with pytest.raises(build_script.SherpaPreflightError, match="stop"):
        build_script._run_build(
            portable=False,
            skip_nuitka=False,
            unsigned=True,
        )

    assert order == ["environment:1.5.0", "sherpa"]


def test_signed_build_checks_legal_sources_before_sherpa(monkeypatch):
    order: list[str] = []

    def fail_legal_gate(*_args, **_kwargs):
        order.append("legal")
        raise build_script.b.LegalReleaseGateError("placeholder")

    monkeypatch.setattr(
        build_script.b,
        "validate_legal_release_documents",
        fail_legal_gate,
    )
    monkeypatch.setattr(
        build_script,
        "_verify_installed_sherpa",
        lambda: order.append("preflight"),
    )

    with pytest.raises(build_script.b.LegalReleaseGateError, match="placeholder"):
        build_script._run_build(
            portable=False,
            skip_nuitka=False,
            unsigned=False,
        )

    assert order == ["legal"]


def test_signed_build_without_the_updater_provider_fails_before_sherpa(monkeypatch):
    order: list[str] = []
    for name in build_script.b.UPDATER_HOOK_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(build_script.b, "validate_legal_release_documents", lambda **_: None)
    monkeypatch.setattr(build_script, "_verify_installed_sherpa", lambda: order.append("preflight"))

    with pytest.raises(build_script.b.LegalReleaseGateError, match="provider"):
        build_script._run_build(portable=False, skip_nuitka=False, unsigned=False)

    assert order == []


def test_unsigned_build_skips_legal_release_gate(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(
        build_script.b,
        "validate_legal_release_documents",
        lambda *_args, **_kwargs: calls.append("legal"),
    )
    monkeypatch.setattr(build_script.b, "read_tauri_version", lambda: "1.5.0")
    monkeypatch.setattr(
        build_script,
        "_verify_build_environment",
        lambda version: calls.append(f"environment:{version}"),
    )

    def stop_at_preflight():
        calls.append("preflight")
        raise build_script.SherpaPreflightError("stop")

    monkeypatch.setattr(build_script, "_verify_installed_sherpa", stop_at_preflight)

    with pytest.raises(build_script.SherpaPreflightError, match="stop"):
        build_script._run_build(
            portable=False,
            skip_nuitka=False,
            unsigned=True,
        )

    assert calls == ["environment:1.5.0", "preflight"]


def test_legal_derivation_step_regenerates_eula_and_privacy(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        build_script.b,
        "run",
        lambda argv, **_kwargs: calls.append(argv),
    )

    build_script._step_derive_eula(10)

    assert [Path(argv[1]).name for argv in calls] == [
        "derive-eula-txt.py",
        "derive-privacy-txt.py",
    ]


def test_unsigned_pipeline_reaches_derivation_without_legal_gate(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(build_script, "_verify_build_environment", lambda _version: None)
    monkeypatch.setattr(build_script, "_verify_installed_sherpa", lambda: None)
    monkeypatch.setattr(
        build_script.b,
        "validate_legal_release_documents",
        lambda *_args, **_kwargs: calls.append("legal"),
    )
    monkeypatch.setattr(build_script.b, "read_tauri_version", lambda: "1.3.0")
    monkeypatch.setattr(build_script.b, "numeric_version", lambda _version: "1.3.0.0")
    for name in (
        "stamp_version",
        "disable_dev_tools",
        "disable_experimental",
        "restore_version_sentinel",
        "restore_dev_tools",
        "restore_experimental",
    ):
        monkeypatch.setattr(build_script.b, name, lambda *_args: None)
    monkeypatch.setattr(build_script, "_step_nuitka", lambda *_args: None)
    monkeypatch.setattr(
        build_script,
        "_step_smoke_and_sign",
        lambda *_args, **_kwargs: None,
    )

    def stop_after_derivation(_total):
        calls.append("derive")
        raise RuntimeError("stop")

    monkeypatch.setattr(build_script, "_step_derive_eula", stop_after_derivation)

    with pytest.raises(RuntimeError, match="stop"):
        build_script._run_build(
            portable=False,
            skip_nuitka=False,
            unsigned=True,
        )

    assert calls == ["derive"]


def test_build_cli_step_compiles_release_binary(monkeypatch):
    calls: list[tuple[list[str], Path | None]] = []
    monkeypatch.setattr(
        build_script.b,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs.get("cwd"))),
    )

    compiled = build_script._step_build_cli(12)

    assert calls == [
        (
            ["cargo", "build", "--release", "--bin", "whisperstream-cli"],
            build_script.b.REPO_ROOT / "src-tauri",
        )
    ]
    assert compiled == (
        build_script.b.REPO_ROOT
        / "src-tauri"
        / "target"
        / "release"
        / "whisperstream-cli.exe"
    )


def test_sign_and_stage_cli_signs_before_staging(monkeypatch, tmp_path):
    order: list[tuple[str, Path]] = []
    compiled = tmp_path / "whisperstream-cli.exe"
    compiled.write_bytes(b"fake cli binary")

    def fake_sign_file(_cfg, path, _description):
        order.append(("sign", Path(path)))

    def fake_copy(src, dst):
        order.append(("stage", Path(src)))
        return shutil.copyfile(src, dst)

    monkeypatch.setattr(build_script.b, "sign_file", fake_sign_file)
    monkeypatch.setattr(build_script.shutil, "copy", fake_copy)
    monkeypatch.setattr(build_script.b, "REPO_ROOT", tmp_path)

    staged = build_script._step_sign_and_stage_cli(object(), 12, compiled)

    assert order == [("sign", compiled), ("stage", compiled)]
    assert staged.parts[-2:] == ("bin", "whisperstream.exe")
    assert staged.read_bytes() == b"fake cli binary"


def test_sign_and_stage_cli_skips_signing_when_unsigned(monkeypatch, tmp_path):
    sign_calls: list[Path] = []
    compiled = tmp_path / "whisperstream-cli.exe"
    compiled.write_bytes(b"fake cli binary")

    monkeypatch.setattr(
        build_script.b,
        "sign_file",
        lambda *_args, **_kwargs: sign_calls.append(compiled),
    )
    monkeypatch.setattr(build_script.b, "REPO_ROOT", tmp_path)

    staged = build_script._step_sign_and_stage_cli(
        None, 12, compiled, unsigned=True
    )

    assert sign_calls == []
    assert staged.parts[-2:] == ("bin", "whisperstream.exe")
    assert staged.read_bytes() == b"fake cli binary"


def test_unsigned_pipeline_builds_and_stages_cli_before_frontend(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(build_script, "_verify_build_environment", lambda _version: None)
    monkeypatch.setattr(build_script, "_verify_installed_sherpa", lambda: None)
    monkeypatch.setattr(
        build_script.b,
        "validate_legal_release_documents",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(build_script.b, "read_tauri_version", lambda: "1.3.0")
    monkeypatch.setattr(build_script.b, "numeric_version", lambda _version: "1.3.0.0")
    for name in (
        "stamp_version",
        "disable_dev_tools",
        "disable_experimental",
        "restore_version_sentinel",
        "restore_dev_tools",
        "restore_experimental",
    ):
        monkeypatch.setattr(build_script.b, name, lambda *_args: None)
    for name in (
        "_step_nuitka",
        "_step_smoke_and_sign",
        "_step_derive_eula",
        "_step_frontend_deps",
        "_step_generate_licenses",
        "_step_stage_sidecar",
        "_step_stage_llama",
    ):
        monkeypatch.setattr(build_script, name, lambda *_args, **_kwargs: None)

    fake_compiled = Path("fake-whisperstream-cli.exe")
    monkeypatch.setattr(
        build_script,
        "_step_build_cli",
        lambda *_args: (order.append("build_cli"), fake_compiled)[1],
    )

    def fake_sign_and_stage(_cfg, _total, compiled, unsigned=False):
        order.append("sign_and_stage_cli")
        assert compiled == fake_compiled
        assert unsigned is True

    monkeypatch.setattr(build_script, "_step_sign_and_stage_cli", fake_sign_and_stage)

    def stop_at_frontend_build(*_args, **_kwargs):
        order.append("build_frontend")
        raise RuntimeError("stop")

    monkeypatch.setattr(build_script, "_step_build_frontend", stop_at_frontend_build)

    with pytest.raises(RuntimeError, match="stop"):
        build_script._run_build(portable=False, skip_nuitka=False, unsigned=True)

    assert order == ["build_cli", "sign_and_stage_cli", "build_frontend"]


def test_preflight_only_argument_does_not_start_package_build(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        build_script,
        "_verify_installed_sherpa",
        lambda: calls.append("preflight"),
    )
    monkeypatch.setattr(
        build_script,
        "_run_build",
        lambda *_args, **_kwargs: calls.append("build"),
    )

    assert build_script.main(["--verify-sherpa-only"]) == 0
    assert calls == ["preflight"]


@pytest.mark.parametrize(
    ("signed", "microsoft_store", "expected"),
    [
        (True, False, "Whisperstream_2.0.0_x64-setup.exe"),
        (False, False, "Whisperstream_2.0.0_x64-setup-UNSIGNED.exe"),
        (True, True, "Whisperstream_2.0.0_x64-microsoft-store-setup.exe"),
        (False, True, "Whisperstream_2.0.0_x64-microsoft-store-setup-UNSIGNED.exe"),
    ],
)
def test_windows_installer_is_copied_to_builds(monkeypatch, tmp_path, signed, microsoft_store, expected):
    nsis = tmp_path / "src-tauri/target/release/bundle/nsis"
    nsis.mkdir(parents=True)
    (nsis / "Whisperstream_2.0.0_x64-setup.exe").write_bytes(b"installer")
    monkeypatch.setattr(build_script.b, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_script.b, "BUILDS_DIR", tmp_path / "builds")

    copied = build_script._copy_windows_installer_to_builds(
        "2.0.0", signed=signed, microsoft_store=microsoft_store
    )

    assert copied == tmp_path / "builds" / "windows" / expected
    assert copied.read_bytes() == b"installer"
    assert (nsis / "Whisperstream_2.0.0_x64-setup.exe").is_file()
    assert [p.name for p in copied.parent.iterdir()] == [expected]

