"""Installer profiles for scripts/build.py; never upload or handle signing secrets here."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path


def _merge_configuration(target: dict, patch: dict) -> dict:
    """Apply Tauri's JSON Merge Patch rules, including array replacement."""
    result = dict(target)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            previous = result.get(key)
            result[key] = _merge_configuration(
                previous if isinstance(previous, dict) else {}, value
            )
        else:
            result[key] = value
    return result


def effective_windows_configuration(root: Path) -> dict:
    """Read base then Windows config without modifying either file."""
    config = {}
    for name in ("tauri.conf.json", "tauri.windows.conf.json"):
        path = root / "src-tauri" / name
        if name == "tauri.windows.conf.json" and not path.exists():
            continue
        patch = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(patch, dict):
            raise ValueError("Tauri configuration must be an object")
        config = _merge_configuration(config, patch)
    return config


def validate_source_configuration(root: Path) -> None:
    config = effective_windows_configuration(root)
    if "updater" in config.get("plugins", {}) or "createUpdaterArtifacts" in config.get("bundle", {}):
        raise ValueError("Source installers have no binary update feed or update artifacts")
    if config["identifier"] != "com.whisperstream.app":
        raise ValueError("Installer identity differs from the reviewed replacement identity")


@contextmanager
def installer_profile(root: Path, profile: str):
    """Validate the shared installer inputs under an exclusive checkout lock.

    Both profiles build from identical committed frontend/native inputs; the
    official profile adds signing and whatever the official build command
    supplies through the build hooks. The committed configuration therefore
    carries no update feed. The lock prevents concurrent scripted builds.
    After a hard process kill, remove the lock before retrying. Do not run a
    dev server or another build in this checkout while building.
    """
    if profile not in {"source", "official"}:
        raise ValueError("Unknown installer profile")
    lock = root / ".installer-profile.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(profile)
    try:
        validate_source_configuration(root)
        yield
    finally:
        lock.unlink()
