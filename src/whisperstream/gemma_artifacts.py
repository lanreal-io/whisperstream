"""Two verified Gemma artifacts behind one public model identity."""
from __future__ import annotations

import json
from pathlib import Path

from .atomic_io import atomic_write_json
from .model_sources import MODEL_SOURCES

PUBLIC_ID = "gemma4-e2b-instruct"
GOOGLE_ID = "gemma4-e2b-google-qat"
ARTIFACT_IDS = (PUBLIC_ID, GOOGLE_ID)
MARKER = ".gemma-artifact.json"


def artifact_path(directory: Path, artifact_id: str) -> Path:
    if artifact_id not in ARTIFACT_IDS:
        raise ValueError("Unknown Gemma artifact")
    return directory / next(iter(MODEL_SOURCES[artifact_id]["files"]))


def present(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def resolve(directory: Path) -> tuple[str, Path]:
    preferred = PUBLIC_ID
    try:
        value = json.loads((directory / MARKER).read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("artifact") in ARTIFACT_IDS:
            preferred = value["artifact"]
    except (OSError, ValueError):
        pass
    for artifact_id in dict.fromkeys((preferred, *ARTIFACT_IDS)):
        path = artifact_path(directory, artifact_id)
        if present(path):
            return artifact_id, path
    return PUBLIC_ID, artifact_path(directory, PUBLIC_ID)


def select(directory: Path, artifact_id: str) -> None:
    if not present(artifact_path(directory, artifact_id)):
        raise ValueError("Gemma artifact is missing")
    atomic_write_json(directory / MARKER, {"artifact": artifact_id})


def receipt(directory: Path) -> str:
    """Bind a hardware result to reviewed artifact plus installed file generation.

    The pin identifies the downloaded bytes; size/mtime detect a manual restore
    or replacement without rehashing gigabytes during each readiness check.
    """
    artifact_id, path = resolve(directory)
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size <= 0:
            return ""
    except OSError:
        return ""
    digest = MODEL_SOURCES[artifact_id]["files"][path.name]["sha256"]
    return f"{digest}:{stat.st_size}:{stat.st_mtime_ns}"
