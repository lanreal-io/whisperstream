"""Give a Finder-launched Mac app the user's shell PATH for agent CLIs.

launchd starts GUI apps with only the system directories on PATH, so CLIs
installed by Homebrew, npm or the Claude installer are invisible to
``shutil.which`` and to scripts that resolve ``node`` through ``env``. A
terminal-launched dev backend inherits the shell PATH and hides the problem.

The user's login shell is asked for its PATH once per process and merged into
this process's environment, so later lookups and child processes both see it.
Never log the PATH, the shell, or probe output: they can identify the user.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys
import threading


logger = logging.getLogger(__name__)

_MARKER = "__WHISPERSTREAM_PATH__"
_PROBE_TIMEOUT_SECONDS = 5.0
_lock = threading.Lock()
_merged = False


def _fallback_dirs() -> list[str]:
    """Common install locations, used even when the shell probe fails."""
    home = Path.home()
    return [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(home / ".local" / "bin"),
        str(home / ".npm-global" / "bin"),
        str(home / ".bun" / "bin"),
        str(home / ".volta" / "bin"),
    ]


def _login_shell() -> str | None:
    shell = os.environ.get("SHELL")
    if not shell:
        try:
            import pwd  # POSIX-only; this module is imported on Windows too.

            shell = pwd.getpwuid(os.getuid()).pw_shell
        except (ImportError, KeyError, OSError):
            return None
    if not shell or not os.path.isabs(shell) or not os.access(shell, os.X_OK):
        return None
    return shell


def _probe_login_shell_path() -> str | None:
    """Ask an interactive login shell for PATH; rc files differ by installer."""
    shell = _login_shell()
    if shell is None:
        return None
    # printenv prints the colon-joined form in every shell, including fish.
    command = f"printf %s {_MARKER}; /usr/bin/printenv PATH; printf %s {_MARKER}"
    environment = {**os.environ, "DISABLE_AUTO_UPDATE": "true"}
    try:
        result = subprocess.run(
            [shell, "-i", "-l", "-c", command],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT_SECONDS,
            env=environment,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = result.stdout.split(_MARKER)
    if len(parts) < 3:
        return None
    value = parts[1].strip()
    return value or None


def merged_path(current: str, shell_path: str | None) -> str:
    """Shell entries first, then the inherited ones, then known install dirs."""
    entries: list[str] = []
    for source in (shell_path or "", current):
        entries.extend(source.split(os.pathsep))
    entries.extend(path for path in _fallback_dirs() if os.path.isdir(path))
    unique: list[str] = []
    for entry in entries:
        if entry and os.path.isabs(entry) and entry not in unique:
            unique.append(entry)
    return os.pathsep.join(unique)


def ensure_user_path() -> None:
    """Merge the user's shell PATH into this process once (macOS only)."""
    global _merged
    if sys.platform != "darwin" or _merged:
        return
    with _lock:
        if _merged:
            return
        shell_path = _probe_login_shell_path()
        current = os.environ.get("PATH", "")
        updated = merged_path(current, shell_path)
        added = len(updated.split(os.pathsep)) - len(
            [entry for entry in current.split(os.pathsep) if entry]
        )
        os.environ["PATH"] = updated
        _merged = True
        logger.info(
            "[UserPath] merged source=%s added=%d",
            "login_shell" if shell_path else "fallback",
            max(added, 0),
        )
