"""Read-only, content-free evidence for each coding agent surface.

Installation, version support and a live reply connection are separate facts.
This module never starts a session, sends a prompt, updates a provider, or treats
a desktop package version as its embedded coding engine's version. Raw process
output and installation paths stay local and are never logged or returned.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys

from .macos_user_path import ensure_user_path


CLAUDE_MOD_MINIMUM = "2.1.287"
# This is our tested baseline, not a claim about the first upstream release.
CODEX_QUEUE_MINIMUM = "0.160.0"
_MINIMUM = {"claude": CLAUDE_MOD_MINIMUM, "codex": CODEX_QUEUE_MINIMUM}
_PACKAGE_NAMES = {"claude": "Claude", "codex": "OpenAI.Codex"}
_VERSION_OUTPUT = re.compile(
    r"(?:codex-cli\s+)?(\d{1,6}\.\d{1,6}\.\d{1,6})(?:\s+\(Claude Code\))?"
)
_PACKAGE_VERSION = re.compile(r"\d{1,6}(?:\.\d{1,6}){2,3}")


def release_version(output: object) -> str | None:
    """Accept only a complete release-version line, never an error's substring."""
    if not isinstance(output, str) or len(output) > 128:
        return None
    match = _VERSION_OUTPUT.fullmatch(output.strip())
    return match[1] if match else None


def meets_minimum(version: str, minimum: str) -> bool:
    return tuple(map(int, version.split("."))) >= tuple(map(int, minimum.split(".")))


def _run(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or len(result.stdout) > 65536:
        return None
    return result.stdout.strip()


def _evidence(agent: str, surface: str) -> dict:
    if agent not in _MINIMUM:
        raise ValueError("unsupported provider")
    return {
        "surface": surface,
        "state": "check_failed",
        "app_version": None,
        "engine_version": None,
        "minimum_version": _MINIMUM[agent],
        "reply_verified": False,
        "transport": "legacy",
    }


def inspect_cli(agent: str) -> dict:
    result = _evidence(agent, "cli")
    ensure_user_path()
    executable = shutil.which(agent)
    if not executable:
        result["state"] = "not_installed"
        return result
    version = release_version(_run([executable, "--version"]))
    if version is None:
        return result
    result["engine_version"] = version
    if not meets_minimum(version, _MINIMUM[agent]):
        result["state"] = "update_required"
        return result
    if agent == "codex":
        usage = _run([executable, "queue", "--help"])
        if usage is None or "--thread" not in usage or "--message" not in usage:
            result["state"] = "capability_unavailable"
            return result
    # Native queue has no conditional idle/generation guard. Its command being
    # present is not evidence that a live app/CLI chat is safely addressable.
    result["state"] = "supported" if agent == "claude" else "legacy"
    result["transport"] = "claude_mod" if agent == "claude" else "legacy"
    return result


def desktop_codex_executable() -> str | None:
    """Find the known embedded engine for app-only plugin lifecycle commands."""
    package = _desktop_package("codex")
    location = package.get("location") if package else None
    if isinstance(location, str) and Path(location).is_absolute():
        return _codex_engine(Path(location))
    return None


def _codex_engine(location: Path) -> str | None:
    candidates = (
        ("Contents/Resources/codex-cli/bin/codex",
         "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
         "Contents/Resources/codex")
        if sys.platform == "darwin" else ("app/resources/codex.exe",)
    )
    for relative in candidates:
        engine = location / relative
        if engine.is_file():
            return str(engine)
    return None


def _mac_desktop_package(agent: str) -> dict | None:
    name, identity = {
        "claude": ("Claude.app", "com.anthropic.claudefordesktop"),
        "codex": ("Codex.app", "com.openai.codex"),
    }[agent]
    failed = False
    for directory in (Path.home() / "Applications", Path("/Applications")):
        bundle = directory / name
        if not bundle.exists():
            continue
        try:
            metadata = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
            version = metadata.get("CFBundleShortVersionString")
            if (metadata.get("CFBundleIdentifier") == identity
                and isinstance(version, str) and _PACKAGE_VERSION.fullmatch(version)):
                return {"version": version, "location": str(bundle)}
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
            pass
        failed = True
    return None if failed else {}


def _desktop_package(agent: str) -> dict | None:
    """None means detection failed/unsupported; an empty dict means absent."""
    if sys.platform == "darwin":
        return _mac_desktop_package(agent)
    if sys.platform != "win32":
        return None
    name = _PACKAGE_NAMES[agent]  # code-authored allowlist, never user input
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = (
        "$ErrorActionPreference='Stop'; "
        f"$items=@(Get-AppxPackage -Name '{name}' | "
        "Select-Object @{n='version';e={$_.Version.ToString()}},"
        "@{n='location';e={$_.InstallLocation}}); "
        "ConvertTo-Json -InputObject $items -Compress"
    )
    raw = _run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script])
    try:
        packages = json.loads(raw) if raw is not None else None
    except (ValueError, TypeError):
        return None
    if not isinstance(packages, list):
        return None
    if not packages:
        return {}
    valid = [p for p in packages if isinstance(p, dict)
             and isinstance(p.get("version"), str)
             and _PACKAGE_VERSION.fullmatch(p["version"])]
    return max(valid, key=lambda p: tuple(map(int, p["version"].split(".")))) if valid else None


def inspect_desktop(agent: str) -> dict:
    result = _evidence(agent, "desktop")
    package = _desktop_package(agent)
    if package is None:
        return result
    if not package:
        result["state"] = "not_installed"
        return result
    version = package.get("version")
    if not isinstance(version, str) or not _PACKAGE_VERSION.fullmatch(version):
        return result
    result["app_version"] = version
    result["state"] = "unknown_engine"
    if agent == "codex":
        # Only this known embedded CLI may be queried. Never launch an app's UI
        # executable with --version, and never substitute the standalone CLI.
        location = package.get("location")
        if isinstance(location, str) and Path(location).is_absolute():
            engine = _codex_engine(Path(location))
            if engine:
                result["engine_version"] = release_version(_run([engine, "--version"]))
        # Even a newer embedded engine doesn't prove shared-daemon attachment.
        # Keep existing desktop hooks until a supported transport is verified.
        result["state"] = "legacy"
        result["minimum_version"] = None
    return result


def inspect_provider(agent: str) -> dict:
    return {"surfaces": [inspect_cli(agent), inspect_desktop(agent)]}
