"""Audit the optional Kokoro archive and source modules for forbidden runtime payloads."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import tarfile


FORBIDDEN_PACKAGES = (
    "espeak",
    "espeak-ng",
    "espeakng-loader",
    "kokoro-onnx",
    "misaki",
    "nltk",
    "num2words",
    "phonemizer",
    "piper-phonemize",
    "spacy",
    "torch",
    "transformers",
)
SOURCE_MODULES = (
    "src/whisperstream/speech/kokoro_engine.py",
    "src/whisperstream/speech/kokoro_frontend.py",
    "src/whisperstream/speech/number_normalizer.py",
)
NATIVE_SUFFIXES = (".dll", ".exe", ".pyd", ".so")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return sorted(imported)


def _normalized_package(value: str) -> str:
    return value.casefold().replace("_", "-")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit the optional Kokoro archive runtime closure."
    )
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    repository = Path(__file__).resolve().parents[1]
    archive = args.archive.resolve()
    report_path = args.report.resolve()

    with tarfile.open(archive, "r:bz2") as bundle:
        archive_members = bundle.getmembers()
    members = sorted(member.name for member in archive_members)
    regular_files = sorted(
        member.name for member in archive_members if member.isfile()
    )
    runtime_member_names = [
        member
        for member in regular_files
        if "/licenses/" not in member
        and "/evidence/" not in member
        and not member.endswith("/NOTICE.txt")
        and not member.endswith("/export-manifest.json")
    ]
    forbidden_member_matches = {
        package: [
            member
            for member in runtime_member_names
            if _normalized_package(package) in _normalized_package(member)
        ]
        for package in FORBIDDEN_PACKAGES
    }
    forbidden_member_matches = {
        package: matches
        for package, matches in forbidden_member_matches.items()
        if matches
    }
    native_members = [
        member
        for member in runtime_member_names
        if member.casefold().endswith(NATIVE_SUFFIXES)
    ]

    source_results = []
    forbidden_imports: dict[str, list[str]] = {}
    normalized_forbidden = {
        _normalized_package(package) for package in FORBIDDEN_PACKAGES
    }
    for relative in SOURCE_MODULES:
        path = repository / relative
        imports = _imports(path)
        matches = [
            imported
            for imported in imports
            if _normalized_package(imported.split(".", 1)[0])
            in normalized_forbidden
        ]
        source_results.append(
            {
                "imports": imports,
                "path": relative,
                "sha256": _sha256(path),
            }
        )
        if matches:
            forbidden_imports[relative] = matches

    passed = not forbidden_member_matches and not forbidden_imports and not native_members
    report = {
        "archive": {
            "member_count": len(members),
            "runtime_member_count": len(runtime_member_names),
            "sha256": _sha256(archive),
            "size": archive.stat().st_size,
        },
        "forbidden_packages": list(FORBIDDEN_PACKAGES),
        "results": {
            "forbidden_imports": forbidden_imports,
            "forbidden_runtime_member_names": forbidden_member_matches,
            "native_runtime_members": native_members,
            "status": "PASS" if passed else "FAIL",
        },
        "schema_version": 1,
        "source_modules": source_results,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"status={report['results']['status']} "
        f"archive_members={len(members)} report={report_path}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
