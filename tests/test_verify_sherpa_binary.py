from __future__ import annotations

import importlib.util
import subprocess
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verifier = _load_script("verify_sherpa_binary_tests", "verify-sherpa-binary.py")


def _dumpbin_ok(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, "clean native output", "")


def _make_wheel(
    tmp_path: Path,
    *,
    abi: str = "cp312",
    version: str = verifier.EXPECTED_VERSION,
    metadata_name: str = "sherpa-onnx",
    requires: tuple[str, ...] = (),
    wheel_tag: str | None = None,
    extension_bytes: bytes = b"MZ clean payload",
    extension_name: str | None = None,
    extra_files: dict[str, bytes] | None = None,
) -> Path:
    filename = f"sherpa_onnx-{version}-{abi}-{abi}-win_amd64.whl"
    wheel = tmp_path / filename
    dist_info = f"sherpa_onnx-{version}.dist-info"
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {metadata_name}",
        f"Version: {version}",
    ]
    metadata.extend(f"Requires-Dist: {requirement}" for requirement in requires)
    tag = wheel_tag or f"{abi}-{abi}-win_amd64"
    wheel_metadata = (
        "Wheel-Version: 1.0\n"
        "Generator: test\n"
        "Root-Is-Purelib: false\n"
        f"Tag: {tag}\n"
    )
    native_name = (
        extension_name
        or f"sherpa_onnx/lib/_sherpa_onnx.{abi}-win_amd64.pyd"
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", "\n".join(metadata) + "\n")
        archive.writestr(f"{dist_info}/WHEEL", wheel_metadata)
        archive.writestr(native_name, extension_bytes)
        archive.writestr("sherpa_onnx/__init__.py", b"")
        for path, payload in (extra_files or {}).items():
            archive.writestr(path, payload)
    return wheel


def test_clean_normal_abi_wheel_passes(tmp_path: Path) -> None:
    wheel = _make_wheel(tmp_path)
    verifier.verify_wheel(
        wheel,
        expected_abi="cp312",
        dumpbin=tmp_path / "dumpbin.exe",
        runner=_dumpbin_ok,
    )


def test_runtime_smoke_uses_exact_wheel_and_target_interpreter(
    tmp_path: Path,
) -> None:
    wheel = _make_wheel(tmp_path, abi="cp313")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runtime_ok(command, **kwargs):
        calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(command, 0, "runtime smoke ok", "")

    verifier.verify_wheel_runtime(wheel, expected_abi="cp313", runner=runtime_ok)

    command, kwargs = calls[0]
    assert command[:4] == ["uv", "run", "--no-project", "--isolated"]
    assert command[command.index("--python") + 1] == "3.13"
    assert command[command.index("--with") + 1] == str(wheel.resolve())
    assert command[-3:-1] == ["python", "-c"]
    assert "OfflineTtsSupertonicModelConfig" in command[-1]
    assert "OfflineTtsKittenModelConfig" in command[-1]
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True


def test_runtime_smoke_failure_is_fail_closed(tmp_path: Path) -> None:
    wheel = _make_wheel(tmp_path, abi="cp314")

    def runtime_failed(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(command, 1, "", "stale import")

    with pytest.raises(verifier.VerificationError, match="runtime import smoke failed"):
        verifier.verify_wheel_runtime(
            wheel,
            expected_abi="cp314",
            runner=runtime_failed,
        )


def test_speaker_identifiers_do_not_trigger_espeak_marker(tmp_path: Path) -> None:
    wheel = _make_wheel(
        tmp_path,
        extension_bytes=(
            b"MZ OfflineSpeakerDiarization WeSpeaker SpeakerEmbeddingExtractor"
        ),
    )

    def speaker_exports(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            "SherpaOnnxCreateSpeakerEmbeddingExtractor",
            "",
        )

    verifier.verify_wheel(
        wheel,
        expected_abi="cp312",
        dumpbin=tmp_path / "dumpbin.exe",
        runner=speaker_exports,
    )


@pytest.mark.parametrize(
    ("field", "kwargs", "match"),
    [
        ("version", {"version": "1.13.3"}, "wrong wheel version"),
        ("name", {"metadata_name": "sherpa-onnx-core"}, "wrong METADATA Name"),
        (
            "dependency",
            {"requires": ("sherpa-onnx-core==1.13.3",)},
            "forbidden split package",
        ),
        (
            "tag",
            {"wheel_tag": "cp312-abi3-win_amd64"},
            "WHEEL tags must be exactly",
        ),
        (
            "extension",
            {"extension_name": "sherpa_onnx/lib/_sherpa_onnx.pyd"},
            "ABI-matched",
        ),
        (
            "path",
            {"extra_files": {"sherpa_onnx/piper-phonemize/data": b"x"}},
            "wheel paths contain forbidden",
        ),
    ],
)
def test_wheel_packaging_failures_are_rejected(
    tmp_path: Path,
    field: str,
    kwargs: dict[str, object],
    match: str,
) -> None:
    del field
    wheel = _make_wheel(tmp_path, **kwargs)
    with pytest.raises(verifier.VerificationError, match=match):
        verifier.verify_wheel(
            wheel,
            dumpbin=tmp_path / "dumpbin.exe",
            runner=_dumpbin_ok,
        )


@pytest.mark.parametrize(
    "marker",
    [
        b"espeak_ng_Synthesize",
        b"espeak_TextToPhonemesWithTerminator",
        b"espeak_Synth",
        b"espeak-ng",
        b"piper-phonemize",
        b"unqualified espeak marker",
        b"unqualified piper marker",
    ],
)
def test_raw_native_markers_are_rejected(tmp_path: Path, marker: bytes) -> None:
    wheel = _make_wheel(tmp_path, extension_bytes=b"MZ..." + marker + b"...")
    with pytest.raises(verifier.VerificationError, match="forbidden raw marker"):
        verifier.verify_wheel(
            wheel,
            dumpbin=tmp_path / "dumpbin.exe",
            runner=_dumpbin_ok,
        )


@pytest.mark.parametrize("switch", ["/dependents", "/exports"])
def test_dumpbin_markers_are_rejected(tmp_path: Path, switch: str) -> None:
    wheel = _make_wheel(tmp_path)

    def contaminated(command, **kwargs):
        output = (
            "espeak_ng_Synthesize"
            if switch in command
            else "clean native output"
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    with pytest.raises(verifier.VerificationError, match=f"dumpbin {switch}"):
        verifier.verify_wheel(
            wheel,
            dumpbin=tmp_path / "dumpbin.exe",
            runner=contaminated,
        )


def test_dumpbin_failure_is_not_skipped(tmp_path: Path) -> None:
    wheel = _make_wheel(tmp_path)

    def failed(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "bad image")

    with pytest.raises(verifier.VerificationError, match="dumpbin /dependents failed"):
        verifier.verify_wheel(
            wheel,
            dumpbin=tmp_path / "dumpbin.exe",
            runner=failed,
        )


def test_cli_returns_nonzero_for_contaminated_native(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    native = tmp_path / "bad.pyd"
    native.write_bytes(b"MZ piper_phonemize")
    assert verifier.main([str(native), "--dumpbin", str(tmp_path / "dumpbin.exe")]) == 1
    assert "forbidden raw marker" in capsys.readouterr().err
