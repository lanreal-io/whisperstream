#!/usr/bin/env python3
"""Audit an internal Apple Silicon Whisperstream app bundle and compare repeats."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    from scripts.macos_local_signing import identity as local_signing_identity
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from macos_local_signing import identity as local_signing_identity

try:
    from scripts.macos_signing import default_developer_id_provider, load_developer_id
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from macos_signing import default_developer_id_provider, load_developer_id

# The official installer's Developer ID provider, or None in a source tree.
# main() loads it from --developer-id-provider.
developer_id: Any = None


def use_developer_id_provider(provider: Path | None) -> None:
    """Load the Developer ID provider the build names; None keeps local only."""
    global developer_id
    developer_id = load_developer_id(provider)


INTERNAL_IDENTIFIER = "io.lanreal.whisperstream"
REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = json.loads(
    (REPO_ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8")
)["version"]
EXPECTED_MINIMUM_MACOS = "14.0"
EXPECTED_USAGE_DESCRIPTION = (
    "Whisperstream uses the microphone only while you dictate. Your speech is "
    "transcribed on this Mac, and the audio never leaves it."
)
EXPECTED_ICON_FILE = "icon.icns"
EXPECTED_ICON_SOURCE_SHA256 = (
    "df338a8e7ea85b2488e2165ef040f8509c2b41f04d2c77e1f210a6b37821f79b"
)
EXPECTED_ICON_1024_SHA256 = (
    "c757ffcee7976121d9dcb4b9d054b84dcec24925906259bae5c2940392743df8"
)
EXPECTED_ICON_REPRESENTATIONS = {
    "ic04": (16, 16),
    "ic05": (32, 32),
    "ic11": (32, 32),
    "ic12": (64, 64),
    "ic07": (128, 128),
    "ic13": (256, 256),
    "ic08": (256, 256),
    "ic14": (512, 512),
    "ic09": (512, 512),
    "ic10": (1024, 1024),
}
EXPECTED_EXECUTABLES = {
    "whisperstream-cli",
    "whisperstream",
    "whisperstream-engine",
    "whisperstream-macos-stt-worker",
    "llama-server",
}
EXPECTED_RESOURCES = {
    "EULA.txt",
    "LICENSE",
    "NOTICE",
    "Chirp-MIT.txt",
    "PRIVACY.txt",
    "THIRD_PARTY_LICENSES.txt",
    "gemma-4-e2b-chat-template.jinja",
    EXPECTED_ICON_FILE,
}
FORBIDDEN_SUFFIXES = {".dll", ".exe", ".ico", ".msi", ".nsh", ".pyd"}
FORBIDDEN_NAME_MARKERS = {"x86_64-pc-windows-msvc"}
EXPECTED_LLAMA_DYLIBS = {
    "libggml-metal.0.12.0.dylib",
    "libggml-metal.dylib",
    "libggml.dylib",
    "libllama.dylib",
    "libllama-common.dylib",
}


SIGNING_MODES = ("local", "developer-id")


class BundleAuditError(RuntimeError):
    """A content-safe bundle audit failure."""


def _assert_required_resources(inventory: dict[str, Any]) -> None:
    resource_prefix = "Contents/Resources/"
    names = {Path(relative).name for relative in inventory if relative.startswith(resource_prefix)}
    if not EXPECTED_RESOURCES.issubset(names):
        raise BundleAuditError("required legal or template resource is missing")
    if resource_prefix + "bin/whisperstream" not in inventory:
        raise BundleAuditError("required CLI resource is missing")


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and process.returncode != 0:
        raise BundleAuditError(
            f"audit command failed tool={Path(argv[0]).name} exit_code={process.returncode}"
        )
    return process


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _icon_measurement(path: Path) -> dict[str, Any]:
    """Validate the ICNS container and its canonical app-icon representation."""
    if not path.is_file():
        raise BundleAuditError("bundle icon is missing from Contents/Resources")
    data = path.read_bytes()
    if len(data) < 8 or data[:4] != b"icns":
        raise BundleAuditError("bundle icon is not an ICNS container")
    if struct.unpack(">I", data[4:8])[0] != len(data):
        raise BundleAuditError("bundle icon ICNS length is invalid")

    chunks: dict[str, bytes] = {}
    offset = 8
    while offset < len(data):
        if offset + 8 > len(data):
            raise BundleAuditError("bundle icon ICNS chunk header is truncated")
        chunk_type = data[offset : offset + 4].decode("latin-1")
        chunk_size = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        if chunk_size < 8 or offset + chunk_size > len(data):
            raise BundleAuditError("bundle icon ICNS chunk length is invalid")
        if chunk_type in chunks:
            raise BundleAuditError("bundle icon ICNS has duplicate chunk types")
        chunks[chunk_type] = data[offset + 8 : offset + chunk_size]
        offset += chunk_size

    representations: dict[str, dict[str, Any]] = {}
    for chunk_type, expected_dimensions in EXPECTED_ICON_REPRESENTATIONS.items():
        payload = chunks.get(chunk_type)
        if payload is None:
            raise BundleAuditError("bundle icon is missing a required representation")
        if chunk_type in {"ic04", "ic05"}:
            if not payload.startswith(b"ARGB"):
                raise BundleAuditError("bundle icon legacy representation is invalid")
            dimensions = expected_dimensions
        else:
            if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
                raise BundleAuditError("bundle icon representation is not PNG")
            if payload[12:16] != b"IHDR":
                raise BundleAuditError("bundle icon PNG has no IHDR header")
            dimensions = struct.unpack(">II", payload[16:24])
            if dimensions != expected_dimensions:
                raise BundleAuditError("bundle icon representation dimensions are invalid")
        representations[chunk_type] = {
            "width": dimensions[0],
            "height": dimensions[1],
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    canonical = representations["ic10"]
    if canonical["sha256"] != EXPECTED_ICON_1024_SHA256:
        raise BundleAuditError("bundle icon canonical 1024 representation differs")
    return {
        "file": path.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "source_png_sha256": EXPECTED_ICON_SOURCE_SHA256,
        "representations": representations,
    }


def _inventory(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        result[relative] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    return result


def _tree_measurement(inventory: dict[str, dict[str, Any]]) -> dict[str, Any]:
    digest = hashlib.sha256()
    for relative, metadata in inventory.items():
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(str(metadata["bytes"]).encode("ascii") + b"\0")
        digest.update(metadata["sha256"].encode("ascii") + b"\n")
    return {
        "files": len(inventory),
        "bytes": sum(item["bytes"] for item in inventory.values()),
        "tree_sha256": digest.hexdigest(),
    }


def _mach_o_candidates(app: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in sorted(item for item in app.rglob("*") if item.is_file()):
        mode = path.stat().st_mode
        if not os.access(path, os.X_OK) and path.suffix not in {".dylib", ".so"}:
            continue
        description = _run(["file", "-b", str(path)]).stdout
        if "Mach-O" in description:
            candidates.append(path)
    return candidates


def _deployment_target(path: Path) -> str:
    output = _run(["otool", "-l", str(path)]).stdout
    blocks = output.split("cmd LC_BUILD_VERSION")
    for block in blocks[1:]:
        match = re.search(r"\n\s*minos\s+(\S+)", block)
        if match:
            return match.group(1)
    raise BundleAuditError("Mach-O file has no LC_BUILD_VERSION minimum")


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise BundleAuditError("invalid Mach-O deployment target") from exc


def _rpaths(path: Path) -> list[str]:
    output = _run(["otool", "-l", str(path)]).stdout
    result: list[str] = []
    for block in output.split("cmd LC_RPATH")[1:]:
        match = re.search(r"\n\s*path\s+(\S+)\s+\(offset", block)
        if match:
            result.append(match.group(1))
    return result


def _dependencies(path: Path, app: Path) -> list[str]:
    lines = _run(["otool", "-L", str(path)]).stdout.splitlines()[1:]
    dependencies = [line.strip().split(" (compatibility", 1)[0] for line in lines if line.strip()]
    rpaths = _rpaths(path)
    executable_dir = app / "Contents/MacOS"
    loader_dir = path.parent
    for dependency in dependencies:
        if dependency.startswith(("/System/Library/", "/usr/lib/")):
            continue
        if dependency.startswith("@loader_path/"):
            resolved = loader_dir / dependency.removeprefix("@loader_path/")
            if resolved.exists():
                continue
        elif dependency.startswith("@executable_path/"):
            resolved = executable_dir / dependency.removeprefix("@executable_path/")
            if resolved.exists():
                continue
        elif dependency.startswith("@rpath/"):
            tail = dependency.removeprefix("@rpath/")
            for rpath in rpaths:
                expanded = rpath.replace("@loader_path", str(loader_dir)).replace(
                    "@executable_path", str(executable_dir)
                )
                if (Path(expanded) / tail).exists():
                    break
            else:
                raise BundleAuditError("unresolved @rpath dependency in bundle")
            continue
        raise BundleAuditError("non-system Mach-O dependency escapes bundle")
    return dependencies


def _signature(app: Path) -> dict[str, Any]:
    verify = _run(["codesign", "--verify", "--deep", "--strict", str(app)])
    if verify.returncode != 0:
        raise BundleAuditError("app code signature verification failed")
    detail_process = _run(["codesign", "-dv", "--verbose=4", str(app)])
    details = detail_process.stderr + detail_process.stdout
    forbidden = ("Developer ID", "Apple Development", "Apple Distribution")
    if any(marker in details for marker in forbidden):
        raise BundleAuditError("distribution signing identity detected")
    kind = "adhoc"
    if "Signature=adhoc" not in details:
        digest = local_signing_identity()
        requirement = f'=certificate leaf = H"{digest}"'
        for target in [app, *_mach_o_candidates(app)]:
            if _run(["codesign", "--verify", "-R", requirement, str(target)], check=False).returncode:
                raise BundleAuditError("bundle code does not match the pinned local signing certificate")
        kind = "self-signed"
    if "TeamIdentifier=not set" not in details:
        raise BundleAuditError("internal app unexpectedly has a team identifier")

    entitlement_process = _run(
        ["codesign", "-d", "--entitlements", ":-", str(app)], check=False
    )
    entitlement_output = entitlement_process.stdout + entitlement_process.stderr
    entitlements: dict[str, Any] = {}
    xml_start = entitlement_output.find("<?xml")
    xml_end = entitlement_output.rfind("</plist>")
    if xml_start >= 0 and xml_end >= xml_start:
        payload = entitlement_output[xml_start : xml_end + len("</plist>")].encode("utf-8")
        parsed = plistlib.loads(payload)
        if not isinstance(parsed, dict):
            raise BundleAuditError("unexpected entitlements payload")
        entitlements = parsed
    if entitlements:
        raise BundleAuditError("internal bundle has unexpected entitlements")
    return {
        "kind": kind,
        "developer_id": False,
        "team_identifier_present": "TeamIdentifier=not set" not in details,
        "entitlements": entitlements,
    }


def _developer_id_signature(app: Path, team_id: str) -> dict[str, Any]:
    try:
        return developer_id.audit_signature(app, team_id, _mach_o_candidates(app))
    except developer_id.DeveloperIdError as exc:
        raise BundleAuditError(str(exc)) from exc


def _report_team_id(build_report: Path | None) -> str | None:
    if build_report is None:
        return None
    signing = json.loads(build_report.read_text(encoding="utf-8")).get("signing") or {}
    if signing.get("kind") != "developer-id":
        raise BundleAuditError("build report does not describe a Developer ID build")
    return signing.get("team_id")


def audit(
    app: Path,
    build_report: Path | None = None,
    *,
    signing: str = "local",
    team_id: str | None = None,
) -> dict[str, Any]:
    if signing not in SIGNING_MODES:
        raise BundleAuditError("invalid signing mode")
    if signing == "developer-id":
        if developer_id is None:
            raise BundleAuditError(
                "--signing developer-id needs the official installer's Developer ID "
                "provider (--developer-id-provider), which this audit was not given"
            )
        reported = _report_team_id(build_report)
        if team_id is None:
            team_id = reported
        elif reported is not None and reported != team_id:
            raise BundleAuditError("build report team differs from --team-id")
        if not team_id:
            raise BundleAuditError("Developer ID audit requires --team-id or a Developer ID build report")
    elif team_id is not None:
        raise BundleAuditError("--team-id requires --signing developer-id")
    app = app.resolve()
    if not app.is_dir() or app.suffix != ".app":
        raise BundleAuditError("app bundle path is invalid")
    contents = app / "Contents"
    plist_path = contents / "Info.plist"
    if not plist_path.is_file():
        raise BundleAuditError("Info.plist is missing")
    plist = plistlib.loads(plist_path.read_bytes())
    identity = {
        "identifier": plist.get("CFBundleIdentifier"),
        "short_version": plist.get("CFBundleShortVersionString"),
        "bundle_version": plist.get("CFBundleVersion"),
        "minimum_system_version": plist.get("LSMinimumSystemVersion"),
        "executable": plist.get("CFBundleExecutable"),
        "icon_file": plist.get("CFBundleIconFile"),
        "microphone_usage_description": plist.get("NSMicrophoneUsageDescription"),
    }
    expected_identity = {
        "identifier": INTERNAL_IDENTIFIER,
        "short_version": EXPECTED_VERSION,
        "minimum_system_version": EXPECTED_MINIMUM_MACOS,
        "icon_file": EXPECTED_ICON_FILE,
        "microphone_usage_description": EXPECTED_USAGE_DESCRIPTION,
    }
    for key, value in expected_identity.items():
        if identity.get(key) != value:
            raise BundleAuditError(f"bundle identity field mismatch field={key}")

    inventory = _inventory(app)
    names = {Path(relative).name for relative in inventory}
    forbidden_paths = [
        relative
        for relative in inventory
        if Path(relative).suffix.lower() in FORBIDDEN_SUFFIXES
        or any(marker in relative.lower() for marker in FORBIDDEN_NAME_MARKERS)
    ]
    if forbidden_paths:
        raise BundleAuditError("Windows resources entered macOS bundle")
    _assert_required_resources(inventory)
    if not EXPECTED_LLAMA_DYLIBS.issubset(names):
        raise BundleAuditError("required llama.cpp Metal libraries are missing")
    icon = _icon_measurement(contents / "Resources" / EXPECTED_ICON_FILE)

    executable_dir = contents / "MacOS"
    executable_names = {
        path.name for path in executable_dir.iterdir() if path.is_file() and os.access(path, os.X_OK)
    }
    if executable_names != EXPECTED_EXECUTABLES:
        raise BundleAuditError("unexpected executable inventory in Contents/MacOS")

    macho: dict[str, Any] = {}
    for path in _mach_o_candidates(app):
        relative = path.relative_to(app).as_posix()
        architectures = _run(["lipo", "-archs", str(path)]).stdout.split()
        if architectures != ["arm64"]:
            raise BundleAuditError("non-arm64 Mach-O architecture detected")
        minimum = _deployment_target(path)
        if _version_tuple(minimum) > _version_tuple(EXPECTED_MINIMUM_MACOS):
            raise BundleAuditError("Mach-O minimum exceeds bundle deployment target")
        dependencies = _dependencies(path, app)
        macho[relative] = {
            "architectures": architectures,
            "minimum_macos": minimum,
            "dependencies": dependencies,
            "rpaths": _rpaths(path),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
    expected_macho = {f"Contents/MacOS/{name}" for name in EXPECTED_EXECUTABLES}
    if not expected_macho.issubset(macho):
        raise BundleAuditError("expected bundle executable is not Mach-O")

    report: dict[str, Any] = {
        "schema_version": 1,
        "identity": identity,
        "bundle": _tree_measurement(inventory),
        "resources": sorted(EXPECTED_RESOURCES),
        "icon": icon,
        "forbidden_artifacts": [],
        "mach_o": macho,
        "signature": (
            _developer_id_signature(app, team_id) if signing == "developer-id" else _signature(app)
        ),
        "distribution": {
            "internal_only": signing != "developer-id",
            "developer_id": signing == "developer-id",
            "notarization_performed": signing == "developer-id",
            "upload_performed": False,
        },
    }
    if build_report is not None:
        build_data = json.loads(build_report.read_text(encoding="utf-8"))
        report["source_stage"] = build_data.get("source_stage")
        report["build_durations_seconds"] = build_data.get("durations_seconds")
        report["dependency_pins"] = build_data.get("dependency_pins")
    return report


def compare(
    first: dict[str, Any],
    second: dict[str, Any],
    first_app: Path,
    second_app: Path,
) -> dict[str, Any]:
    first_inventory = _inventory(first_app)
    second_inventory = _inventory(second_app)
    all_paths = sorted(set(first_inventory) | set(second_inventory))
    differing = [
        relative
        for relative in all_paths
        if first_inventory.get(relative) != second_inventory.get(relative)
    ]
    if first["identity"] != second["identity"]:
        raise BundleAuditError("repeat build identity differs")
    first_source = first.get("source_stage")
    second_source = second.get("source_stage")
    if first_source is not None and first_source != second_source:
        raise BundleAuditError("repeat build staged inputs differ")
    return {
        "equivalent_staged_inputs": first_source is not None and first_source == second_source,
        "stable_identity": True,
        "byte_identical": not differing,
        "differing_file_count": len(differing),
        "differing_files": differing,
        "first_tree_sha256": first["bundle"]["tree_sha256"],
        "second_tree_sha256": second["bundle"]["tree_sha256"],
    }


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--build-report", type=Path)
    parser.add_argument("--compare-app", type=Path)
    parser.add_argument("--compare-build-report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--signing",
        choices=SIGNING_MODES,
        default="local",
        help="local (default) or developer-id (requires the owner's team and a stapled ticket)",
    )
    parser.add_argument("--team-id", help="expected Developer ID team (default: from --build-report)")
    parser.add_argument(
        "--developer-id-provider",
        type=Path,
        default=default_developer_id_provider(),
        help=(
            "Developer ID provider module for --signing developer-id "
            "(default: $WHISPERSTREAM_DEVELOPER_ID_PROVIDER)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        use_developer_id_provider(args.developer_id_provider)
        signing = {"signing": args.signing, "team_id": args.team_id}
        result = audit(args.app, args.build_report, **signing)
        if args.compare_app:
            other = audit(args.compare_app, args.compare_build_report, **signing)
            result["repeat"] = compare(result, other, args.app.resolve(), args.compare_app.resolve())
    except (BundleAuditError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"internal bundle audit failed: {exc}", file=sys.stderr)
        return 1
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
