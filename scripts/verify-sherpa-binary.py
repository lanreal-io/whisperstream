"""Fail-closed verification for Whisperstream's downstream sherpa-onnx wheels.

The verifier deliberately checks both packaging facts and the native payload.
It is used by the wheel builder and later by the application packaging
preflight.  A missing inspection tool is an error, not a skipped check.
"""
from __future__ import annotations

import argparse
import email.parser
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Iterable


EXPECTED_NAME = "sherpa-onnx"
EXPECTED_VERSION = "1.13.3+whisperstream.2"
SUPPORTED_ABIS = ("cp312", "cp313", "cp314")
ABI_PYTHONS = {
    "cp312": "3.12",
    "cp313": "3.13",
    "cp314": "3.14",
}
EXPECTED_PLATFORM = "win_amd64"
FORBIDDEN_DISTRIBUTIONS = {"sherpa-onnx-core"}
FORBIDDEN_MARKERS = (
    b"espeak_ng_synthesize",
    b"espeak_texttophonemeswithterminator",
    b"espeak_synth",
    b"espeak-ng",
    b"espeak_ng",
    b"piper-phonemize",
    b"piper_phonemize",
)
FORBIDDEN_WORD_MARKERS = (b"espeak", b"piper")
NATIVE_SUFFIXES = (".pyd", ".dll")
RUNTIME_SMOKE = """
import sherpa_onnx

assert sherpa_onnx.__version__ == "1.13.3+whisperstream.2"
for name in (
    "OfflineTtsKittenModelConfig",
    "OfflineTtsKokoroModelConfig",
    "OfflineTtsMatchaModelConfig",
    "OfflineTtsPocketModelConfig",
    "OfflineTtsVitsModelConfig",
    "OfflineTtsZipvoiceModelConfig",
):
    assert not hasattr(sherpa_onnx, name), name
required = (
    "OfflineTts",
    "OfflineTtsConfig",
    "OfflineTtsModelConfig",
    "OfflineTtsSupertonicModelConfig",
)
for name in required:
    assert hasattr(sherpa_onnx, name), name
supertonic = sherpa_onnx.OfflineTtsSupertonicModelConfig()
model = sherpa_onnx.OfflineTtsModelConfig()
model.supertonic = supertonic
config = sherpa_onnx.OfflineTtsConfig()
config.model = model
assert config.model.supertonic is not None
"""


class VerificationError(RuntimeError):
    """The candidate artifact failed a required provenance or native check."""


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def _forbidden_markers(data: bytes) -> list[str]:
    lowered = data.lower()
    matches = [
        marker.decode("ascii")
        for marker in FORBIDDEN_MARKERS
        if marker in lowered
    ]
    for marker in FORBIDDEN_WORD_MARKERS:
        # A raw substring search for "espeak" also matches legitimate ASR
        # identifiers such as OfflineSpeakerDiarization and WeSpeaker.
        # Require a word-like boundary while still catching symbols, paths,
        # dependency names, and prose that name the forbidden component.
        pattern = rb"(?<![a-z0-9_])" + re.escape(marker)
        if re.search(pattern, lowered):
            matches.append(marker.decode("ascii"))
    return matches


def _find_dumpbin() -> Path:
    configured = os.environ.get("DUMPBIN")
    if configured:
        path = Path(configured).resolve()
        _require(path.is_file(), f"DUMPBIN does not exist: {path}")
        return path

    located = shutil.which("dumpbin")
    if located:
        return Path(located).resolve()

    roots = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
    ]
    matches: list[Path] = []
    for root in roots:
        matches.extend(
            root.glob(
                "Microsoft Visual Studio/*/*/VC/Tools/MSVC/*/bin/"
                "Hostx64/x64/dumpbin.exe"
            )
        )
    _require(
        bool(matches),
        "dumpbin.exe is required for native verification; install the Visual "
        "Studio C++ build tools or set DUMPBIN",
    )
    return sorted(matches)[-1].resolve()


def _inspect_with_dumpbin(
    native_path: Path,
    *,
    dumpbin: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    tool = dumpbin or _find_dumpbin()
    for switch in ("/dependents", "/exports"):
        proc = runner(
            [str(tool), "/nologo", switch, str(native_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        _require(
            proc.returncode == 0,
            f"dumpbin {switch} failed for {native_path.name}: "
            f"{(proc.stderr or proc.stdout).strip()}",
        )
        output = (proc.stdout + "\n" + proc.stderr).encode(
            "utf-8", errors="replace"
        )
        markers = _forbidden_markers(output)
        _require(
            not markers,
            f"{native_path.name} dumpbin {switch} contains forbidden marker(s): "
            f"{', '.join(markers)}",
        )


def verify_native_binary(
    native_path: Path,
    *,
    dumpbin: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Verify one unpacked Windows native library."""
    _require(native_path.is_file(), f"native binary not found: {native_path}")
    _require(
        native_path.suffix.lower() in NATIVE_SUFFIXES,
        f"unsupported native artifact: {native_path.name}",
    )
    markers = _forbidden_markers(native_path.read_bytes())
    _require(
        not markers,
        f"{native_path.name} contains forbidden raw marker(s): "
        f"{', '.join(markers)}",
    )
    _inspect_with_dumpbin(native_path, dumpbin=dumpbin, runner=runner)


def _single_member(names: Iterable[str], suffix: str) -> str:
    matches = [name for name in names if name.endswith(suffix)]
    _require(
        len(matches) == 1,
        f"wheel must contain exactly one {suffix}; found {len(matches)}",
    )
    return matches[0]


def _parse_wheel_identity(wheel_path: Path) -> tuple[str, str, str, str, str]:
    match = re.fullmatch(
        r"(?P<name>.+)-(?P<version>[^-]+)-(?P<python>[^-]+)-"
        r"(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl",
        wheel_path.name,
    )
    _require(bool(match), f"invalid wheel filename: {wheel_path.name}")
    assert match is not None
    return (
        match["name"],
        match["version"],
        match["python"],
        match["abi"],
        match["platform"],
    )


def verify_wheel(
    wheel_path: Path,
    *,
    expected_abi: str | None = None,
    dumpbin: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Verify metadata, tags, contents, and every Windows native payload."""
    _require(wheel_path.is_file(), f"wheel not found: {wheel_path}")
    name, version, python_tag, abi_tag, platform_tag = _parse_wheel_identity(
        wheel_path
    )
    inferred_abi = expected_abi or python_tag
    _require(inferred_abi in SUPPORTED_ABIS, f"unsupported ABI: {inferred_abi}")
    _require(_canonical_name(name) == EXPECTED_NAME, f"wrong wheel name: {name}")
    _require(version == EXPECTED_VERSION, f"wrong wheel version: {version}")
    _require(
        (python_tag, abi_tag, platform_tag)
        == (inferred_abi, inferred_abi, EXPECTED_PLATFORM),
        "wheel must use the exact normal ABI tag "
        f"{inferred_abi}-{inferred_abi}-{EXPECTED_PLATFORM}",
    )

    try:
        archive = zipfile.ZipFile(wheel_path)
    except zipfile.BadZipFile as exc:
        raise VerificationError(f"invalid wheel ZIP: {wheel_path}") from exc

    with archive:
        names = archive.namelist()
        _require(len(names) == len(set(names)), "wheel contains duplicate paths")
        lowered_names = "\n".join(names).lower().encode("utf-8")
        markers = _forbidden_markers(lowered_names)
        _require(
            not markers,
            f"wheel paths contain forbidden marker(s): {', '.join(markers)}",
        )

        metadata_name = _single_member(names, ".dist-info/METADATA")
        wheel_metadata_name = _single_member(names, ".dist-info/WHEEL")
        metadata_bytes = archive.read(metadata_name)
        metadata = email.parser.BytesParser().parsebytes(metadata_bytes)
        _require(
            _canonical_name(metadata.get("Name", "")) == EXPECTED_NAME,
            f"wrong METADATA Name: {metadata.get('Name')!r}",
        )
        _require(
            metadata.get("Version") == EXPECTED_VERSION,
            f"wrong METADATA Version: {metadata.get('Version')!r}",
        )
        requirements = metadata.get_all("Requires-Dist", [])
        requirement_names = {
            _canonical_name(match.group(1))
            for requirement in requirements
            if (
                match := re.match(
                    r"\s*([A-Za-z0-9_.-]+)",
                    requirement,
                )
            )
        }
        forbidden_requirements = requirement_names & FORBIDDEN_DISTRIBUTIONS
        _require(
            not forbidden_requirements,
            "wheel depends on forbidden split package(s): "
            f"{', '.join(sorted(forbidden_requirements))}",
        )
        metadata_markers = _forbidden_markers(metadata_bytes)
        _require(
            not metadata_markers,
            "wheel metadata contains forbidden marker(s): "
            f"{', '.join(metadata_markers)}",
        )

        wheel_metadata = archive.read(wheel_metadata_name).decode("utf-8")
        tags = {
            line.removeprefix("Tag:").strip()
            for line in wheel_metadata.splitlines()
            if line.startswith("Tag:")
        }
        expected_tag = f"{inferred_abi}-{inferred_abi}-{EXPECTED_PLATFORM}"
        _require(
            tags == {expected_tag},
            f"WHEEL tags must be exactly {expected_tag}; found {sorted(tags)}",
        )
        _require(
            "Root-Is-Purelib: false" in wheel_metadata,
            "native wheel incorrectly declares Root-Is-Purelib",
        )

        extension_pattern = re.compile(
            rf"^sherpa_onnx/lib/_sherpa_onnx\.{inferred_abi}-"
            rf"win_amd64\.pyd$"
        )
        extensions = [name for name in names if extension_pattern.fullmatch(name)]
        _require(
            len(extensions) == 1,
            "wheel must contain exactly one ABI-matched _sherpa_onnx extension",
        )
        native_names = [
            name for name in names if Path(name).suffix.lower() in NATIVE_SUFFIXES
        ]
        _require(bool(native_names), "wheel contains no Windows native payload")

        with tempfile.TemporaryDirectory(prefix="verify-sherpa-") as temp:
            temp_root = Path(temp)
            for index, native_name in enumerate(native_names):
                native_path = temp_root / f"{index}-{Path(native_name).name}"
                native_path.write_bytes(archive.read(native_name))
                verify_native_binary(
                    native_path,
                    dumpbin=dumpbin,
                    runner=runner,
                )


def verify_wheel_runtime(
    wheel_path: Path,
    *,
    expected_abi: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Install one wheel under its target interpreter and smoke its API."""
    _require(expected_abi in ABI_PYTHONS, f"unsupported ABI: {expected_abi}")
    resolved = wheel_path.resolve()
    _require(resolved.is_file(), f"wheel not found: {resolved}")
    command = [
        "uv",
        "run",
        "--no-project",
        "--isolated",
        "--python",
        ABI_PYTHONS[expected_abi],
        "--with",
        str(resolved),
        "python",
        "-c",
        RUNTIME_SMOKE,
    ]
    proc = runner(
        command,
        check=False,
        text=True,
        capture_output=True,
    )
    _require(
        proc.returncode == 0,
        f"runtime import smoke failed for {resolved.name}: "
        f"{(proc.stderr or proc.stdout).strip()}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify an espeak-free Whisperstream sherpa wheel or native binary."
    )
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument(
        "--expected-abi",
        choices=SUPPORTED_ABIS,
        help="Required wheel ABI (normally inferred from its filename).",
    )
    parser.add_argument("--dumpbin", type=Path)
    args = parser.parse_args(argv)

    try:
        for artifact in args.artifacts:
            resolved = artifact.resolve()
            if resolved.suffix.lower() == ".whl":
                verify_wheel(
                    resolved,
                    expected_abi=args.expected_abi,
                    dumpbin=args.dumpbin,
                )
            else:
                _require(
                    args.expected_abi is None,
                    "--expected-abi only applies to wheels",
                )
                verify_native_binary(resolved, dumpbin=args.dumpbin)
            print(f"verified: {resolved}")
    except VerificationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
