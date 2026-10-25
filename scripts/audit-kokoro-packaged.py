"""Audit source and frozen/package artifacts for forbidden Kokoro dependencies."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "espeak",
        "espeakng_loader",
        "kokoro_onnx",
        "misaki",
        "nltk",
        "num2words",
        "phonemizer",
        "piper",
        "piper_phonemize",
        "spacy",
        "torch",
        "transformers",
    }
)
FORBIDDEN_PATH_TOKENS = frozenset(
    re.sub(r"[^a-z0-9]", "", item.casefold())
    for item in (
        "espeak",
        "espeak-ng",
        "espeakng-loader",
        "kokoro-onnx",
        "misaki",
        "nltk",
        "num2words",
        "phonemizer",
        "piper",
        "piper-phonemize",
        "spacy",
        "torch",
        "transformers",
    )
)
# Raw binary checks target loader/import/native-symbol shapes rather than plain
# prose words. Product docstrings and license notices may name excluded
# packages while accurately stating that they are not distributed.
FORBIDDEN_BINARY_MARKERS = (
    b"espeak_ng_synthesize",
    b"espeak_texttophonemes",
    b"libespeak",
    b"espeak-ng.dll",
    b"piper_phonemize.dll",
    b"piper-phonemize.dll",
    b"torch_cpu.dll",
    b"torch_python.dll",
    b"libtorch.dll",
)
NATIVE_SUFFIXES = frozenset({".dll", ".exe", ".pyd", ".so"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_component(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _forbidden_path_parts(path: Path) -> list[str]:
    matches: set[str] = set()
    normalized_parts = [_normalized_component(part) for part in path.parts]
    for index, (part, normalized) in enumerate(
        zip(path.parts, normalized_parts, strict=True)
    ):
        if normalized == "transformers" and index > 0:
            # ONNX Runtime ships its own ``onnxruntime.transformers`` tools.
            # This namespace is not the Hugging Face transformers package.
            if normalized_parts[index - 1] == "onnxruntime":
                continue
        if any(
            normalized == token or normalized.startswith(token + "distinfo")
            for token in FORBIDDEN_PATH_TOKENS
        ):
            matches.add(part)
    return sorted(matches)


def _source_scan(source_root: Path) -> dict[str, object]:
    import_hits: dict[str, list[str]] = {}
    path_hits: dict[str, list[str]] = {}
    files = sorted(source_root.rglob("*.py"))
    for path in files:
        relative = path.relative_to(source_root.parent)
        parts = _forbidden_path_parts(relative)
        if parts:
            path_hits[relative.as_posix()] = parts
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".", 1)[0])
        matches = sorted(roots & FORBIDDEN_IMPORT_ROOTS)
        if matches:
            import_hits[relative.as_posix()] = matches
    return {
        "python_files_scanned": len(files),
        "forbidden_imports": import_hits,
        "forbidden_paths": path_hits,
        "status": "PASS" if not import_hits and not path_hits else "FAIL",
    }


def _compilation_report_scan(path: Path) -> dict[str, object]:
    root = ET.parse(path).getroot()
    module_names = {
        element.attrib["name"]
        for element in root.iter("module")
        if "name" in element.attrib
    }
    distributions = {
        element.attrib["name"]
        for element in root.iter("distribution-usage")
        if "name" in element.attrib
    }
    imported_roots = {
        name.split(".", 1)[0].casefold().replace("-", "_")
        for name in module_names | distributions
    }
    forbidden = sorted(imported_roots & FORBIDDEN_IMPORT_ROOTS)
    path_hits: dict[str, list[str]] = {}
    for element in root.iter():
        for field in ("source_path", "dest_path", "filename"):
            value = element.attrib.get(field)
            if not value:
                continue
            matches = _forbidden_path_parts(Path(value))
            if matches:
                path_hits[value] = matches
    return {
        "completion": root.attrib.get("completion"),
        "modules": len(module_names),
        "distributions": len(distributions),
        "forbidden_modules_or_distributions": forbidden,
        "forbidden_paths": path_hits,
        "status": (
            "PASS"
            if root.attrib.get("completion") == "yes"
            and not forbidden
            and not path_hits
            else "FAIL"
        ),
    }


def _raw_markers(path: Path) -> list[str]:
    payload = path.read_bytes().lower()
    return sorted(
        marker.decode("ascii")
        for marker in FORBIDDEN_BINARY_MARKERS
        if marker in payload
    )


def _file_scan(path: Path, label: str) -> dict[str, object]:
    markers = _raw_markers(path)
    return {
        "label": label,
        "filename": path.name,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "forbidden_binary_markers": markers,
        "status": "PASS" if not markers else "FAIL",
    }


def _tree_scan(root: Path, label: str) -> dict[str, object]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    path_hits: dict[str, list[str]] = {}
    native_hits: dict[str, list[str]] = {}
    native_files = 0
    total_bytes = 0
    for path in files:
        relative = path.relative_to(root)
        total_bytes += path.stat().st_size
        matches = _forbidden_path_parts(relative)
        if matches:
            path_hits[relative.as_posix()] = matches
        if path.suffix.casefold() in NATIVE_SUFFIXES:
            native_files += 1
            markers = _raw_markers(path)
            if markers:
                native_hits[relative.as_posix()] = markers
    return {
        "label": label,
        "root_name": root.name,
        "files_scanned": len(files),
        "bytes_scanned": total_bytes,
        "native_files_scanned": native_files,
        "forbidden_paths": path_hits,
        "forbidden_native_markers": native_hits,
        "status": "PASS" if not path_hits and not native_hits else "FAIL",
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--compilation-report", required=True, type=Path)
    parser.add_argument("--standalone", required=True, type=Path)
    parser.add_argument("--sidecar", required=True, type=Path)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--runtime-extraction", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    scans = {
        "source": _source_scan(args.source_root.resolve()),
        "compilation_report": _compilation_report_scan(
            args.compilation_report.resolve()
        ),
        "standalone": _tree_scan(args.standalone.resolve(), "nuitka-standalone"),
        "sidecar": _file_scan(args.sidecar.resolve(), "onefile-sidecar"),
        "installer": _file_scan(args.installer.resolve(), "nsis-installer"),
        "runtime_extraction": _tree_scan(
            args.runtime_extraction.resolve(),
            "fresh-onefile-runtime-extraction",
        ),
    }
    passed = all(item["status"] == "PASS" for item in scans.values())
    report = {
        "schema_version": 1,
        "forbidden_dependencies": sorted(FORBIDDEN_IMPORT_ROOTS),
        "scans": scans,
        "status": "PASS" if passed else "FAIL",
    }
    report_path = args.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"status={report['status']} report={report_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
