from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load_script("build_sherpa_wheels_tests", "build-sherpa-wheels.py")


def test_downstream_constants_pin_exact_reviewed_source() -> None:
    assert builder.UPSTREAM_VERSION == "1.13.3"
    assert builder.DOWNSTREAM_VERSION == "1.13.3+whisperstream.2"
    assert builder.UPSTREAM_COMMIT == "330609dab49be6ee8b30702918ca7abbbad1286a"
    assert builder.SUPPORTED_PYTHONS == ("3.12", "3.13", "3.14")
    assert builder.BUILD_REQUIREMENTS == (
        "setuptools==80.9.0",
        "wheel==0.45.1",
        "cmake==3.31.6",
    )
    assert (
        "-DSHERPA_ONNX_ENABLE_WHISPERSTREAM_SUPERTONIC_ONLY=ON"
        in builder.CMAKE_ARGS
    )


def test_help_describes_short_scratch_without_argparse_percent_error(
    capsys,
) -> None:
    with pytest.raises(SystemExit) as exc:
        builder.main(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "%TEMP%" in output
    assert "fallback" in output


def test_patch_preserves_default_exports_and_omits_unbuilt_tts_exports() -> None:
    patch = builder.PATCH_PATH.read_text(encoding="utf-8")

    assert (
        "diff --git a/sherpa-onnx/python/sherpa_onnx/__init__.py "
        "b/sherpa-onnx/python/sherpa_onnx/__init__.py"
    ) in patch
    assert "_optional_tts_config_names" in patch
    assert "OfflineTtsKittenModelConfig" in patch
    assert "OfflineTtsZipvoiceModelConfig" in patch
    assert "hasattr(_sherpa_onnx_native, _name)" in patch


def test_prepare_source_uses_detached_exact_commit_and_injects_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    commands: list[list[str]] = []

    def fake_runner(command, *, cwd=None, env=None):
        del env
        commands.append(list(command))
        if command[:2] == ["git", "init"]:
            source.mkdir(parents=True)
            (source / "CMakeLists.txt").write_text(
                'set(SHERPA_ONNX_VERSION "1.13.3")\n',
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0)

    real_run = subprocess.run

    def fake_subprocess_run(command, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(
                command, 0, builder.UPSTREAM_COMMIT + "\n", ""
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(builder.subprocess, "run", fake_subprocess_run)
    builder.prepare_source(source, runner=fake_runner)

    assert ["git", "config", "core.longpaths", "true"] in commands
    assert ["git", "fetch", "--depth", "1", "origin", builder.UPSTREAM_COMMIT] in commands
    assert ["git", "checkout", "--detach", "FETCH_HEAD"] in commands
    assert ["git", "apply", "--check", str(builder.PATCH_PATH)] in commands
    assert ["git", "apply", str(builder.PATCH_PATH)] in commands
    assert (
        f'set(SHERPA_ONNX_VERSION "{builder.DOWNSTREAM_VERSION}")'
        in (source / "CMakeLists.txt").read_text(encoding="utf-8")
    )


def test_build_command_uses_isolated_normal_package_and_clears_split_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "wheel"
    source.mkdir()
    captured: dict[str, object] = {}
    monkeypatch.setenv("SHERPA_ONNX_SPLIT_PYTHON_PACKAGE", "1")
    monkeypatch.setenv("SHERPA_ONNX_IS_FOR_PYPI", "1")

    def fake_runner(command, *, cwd=None, env=None):
        captured.update(command=list(command), cwd=cwd, env=env)
        output.mkdir(parents=True, exist_ok=True)
        (output / "sherpa_onnx-test.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(command, 0)

    wheel = builder.build_wheel(source, "3.12", output, runner=fake_runner)
    command = captured["command"]
    env = captured["env"]
    assert command[:4] == ["uv", "run", "--no-project", "--isolated"]
    assert command[command.index("--python") + 1] == "3.12"
    assert command[-3:-1] == ["bdist_wheel", "--dist-dir"]
    assert captured["cwd"] == source
    assert "SHERPA_ONNX_SPLIT_PYTHON_PACKAGE" not in env
    assert "SHERPA_ONNX_IS_FOR_PYPI" not in env
    assert env["SHERPA_ONNX_CMAKE_ARGS"] == " ".join(builder.CMAKE_ARGS)
    assert wheel.name == "sherpa_onnx-test.whl"
    assert (source / "build" / "sherpa_onnx" / "bin").is_dir()


def test_build_rejects_unsupported_python_before_running(tmp_path: Path) -> None:
    with pytest.raises(builder.BuildError, match="unsupported Python"):
        builder.build_wheel(
            tmp_path / "source",
            "3.15",
            tmp_path / "output",
            runner=lambda *args, **kwargs: pytest.fail("runner called"),
        )


def test_short_work_dir_validation_rejects_filetracker_risk() -> None:
    builder._validate_short_work_dir(Path(r"C:\ws-build\s-safe"))

    too_long = Path("C:\\" + ("nested-" * 20))
    with pytest.raises(builder.BuildError, match="MSBuild FileTracker"):
        builder._validate_short_work_dir(too_long)


def test_create_work_dir_uses_unique_configured_short_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "short-root"
    monkeypatch.setenv(builder.WORK_ROOT_ENV, str(root))
    monkeypatch.setattr(builder, "MAX_SOURCE_DIR_LENGTH", 1000)

    first, first_managed = builder.resolve_work_dir(None)
    second, second_managed = builder.resolve_work_dir(None)

    assert first_managed is True
    assert second_managed is True
    assert first.parent == root.resolve()
    assert second.parent == root.resolve()
    assert first != second

    builder._remove_tree(first)
    builder._remove_tree(second)


def test_explicit_long_work_dir_is_rejected_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / ("x" * 100)
    monkeypatch.setattr(
        builder,
        "MAX_SOURCE_DIR_LENGTH",
        len(str(candidate / "source")) - 1,
    )

    with pytest.raises(builder.BuildError, match="MSBuild FileTracker"):
        builder.resolve_work_dir(candidate)

    assert not candidate.exists()


def test_automatic_work_dir_falls_back_when_primary_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "primary"
    fallback = tmp_path / "fallback"
    real_mkdtemp = builder.tempfile.mkdtemp

    def selective_mkdtemp(*, prefix: str, dir: Path) -> str:
        if Path(dir) == primary.resolve():
            raise PermissionError("primary denied")
        return real_mkdtemp(prefix=prefix, dir=dir)

    monkeypatch.setattr(
        builder,
        "_automatic_work_roots",
        lambda: (primary, fallback),
    )
    monkeypatch.setattr(builder, "MAX_SOURCE_DIR_LENGTH", 1000)
    monkeypatch.setattr(builder.tempfile, "mkdtemp", selective_mkdtemp)

    work_dir, managed = builder.resolve_work_dir(None)

    assert managed is True
    assert work_dir.parent == fallback.resolve()
    builder._remove_tree(work_dir)


def test_publish_verified_outputs_rechecks_copies_and_writes_manifest(
    tmp_path: Path,
) -> None:
    built_dir = tmp_path / "built"
    built_dir.mkdir()
    wheel = (
        built_dir
        / "sherpa_onnx-1.13.3+whisperstream.2-cp312-cp312-win_amd64.whl"
    )
    wheel.write_bytes(b"verified wheel")
    output = tmp_path / "requested-output"
    output.mkdir()
    (output / "stale.whl").write_bytes(b"stale")
    calls: list[tuple[str, Path, str]] = []
    verifier = types.SimpleNamespace(
        verify_wheel=lambda path, expected_abi: calls.append(
            ("native", Path(path), expected_abi)
        ),
        verify_wheel_runtime=lambda path, expected_abi: calls.append(
            ("runtime", Path(path), expected_abi)
        ),
    )

    builder.publish_verified_outputs(
        output,
        [(wheel, "cp312")],
        verifier=verifier,
    )

    published = output / wheel.name
    assert published.read_bytes() == wheel.read_bytes()
    assert [kind for kind, _path, _abi in calls] == ["native", "runtime"]
    assert calls[0][1].name == wheel.name
    assert calls[0][1].parent.parent == output.parent
    assert calls[0][2] == "cp312"
    assert calls[0][1].parent != output
    assert not (output / "stale.whl").exists()
    manifest = json.loads((output / builder.MANIFEST_NAME).read_text("utf-8"))
    assert manifest["wheels"] == [
        {
            "filename": wheel.name,
            "size": wheel.stat().st_size,
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        }
    ]


def test_publish_verification_failure_preserves_previous_output(
    tmp_path: Path,
) -> None:
    wheel = (
        tmp_path
        / "sherpa_onnx-1.13.3+whisperstream.2-cp312-cp312-win_amd64.whl"
    )
    wheel.write_bytes(b"candidate")
    output = tmp_path / "requested-output"
    output.mkdir()
    previous = output / "previous.whl"
    previous.write_bytes(b"previous")

    def reject_copy(path: Path, *, expected_abi: str) -> None:
        del path, expected_abi
        raise RuntimeError("copy rejected")

    verifier = types.SimpleNamespace(
        verify_wheel=reject_copy,
        verify_wheel_runtime=lambda *args, **kwargs: pytest.fail(
            "runtime smoke should not run after native rejection"
        ),
    )
    with pytest.raises(RuntimeError, match="copy rejected"):
        builder.publish_verified_outputs(
            output,
            [(wheel, "cp312")],
            verifier=verifier,
        )

    assert previous.read_bytes() == b"previous"
    assert sorted(path.name for path in output.iterdir()) == ["previous.whl"]


def test_manifest_is_deterministic_and_records_hashes_and_provenance(
    tmp_path: Path,
) -> None:
    wheels = [
        tmp_path
        / "sherpa_onnx-1.13.3+whisperstream.2-cp313-cp313-win_amd64.whl",
        tmp_path
        / "sherpa_onnx-1.13.3+whisperstream.2-cp312-cp312-win_amd64.whl",
    ]
    wheels[0].write_bytes(b"cp313")
    wheels[1].write_bytes(b"cp312")

    first = builder.write_manifest(tmp_path, wheels)
    first_bytes = first.read_bytes()
    second = builder.write_manifest(tmp_path, list(reversed(wheels)))
    assert second.read_bytes() == first_bytes

    manifest = json.loads(first_bytes)
    assert manifest["version"] == builder.DOWNSTREAM_VERSION
    assert manifest["upstream"]["commit"] == builder.UPSTREAM_COMMIT
    assert manifest["patch"]["sha256"] == hashlib.sha256(
        builder.PATCH_PATH.read_bytes()
    ).hexdigest()
    assert [item["filename"] for item in manifest["wheels"]] == sorted(
        wheel.name for wheel in wheels
    )
    assert manifest["wheels"][0]["sha256"] == hashlib.sha256(b"cp312").hexdigest()


def test_replace_exact_refuses_ambiguous_version_declaration(tmp_path: Path) -> None:
    path = tmp_path / "CMakeLists.txt"
    old = 'set(SHERPA_ONNX_VERSION "1.13.3")'
    path.write_text(f"{old}\n{old}\n", encoding="utf-8")
    with pytest.raises(builder.BuildError, match="exactly one"):
        builder._replace_exact(path, old, "replacement")
