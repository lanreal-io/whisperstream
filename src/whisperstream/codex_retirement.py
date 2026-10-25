"""One-release startup cleanup for Whisperstream's retired Codex hooks.

This module exists only to remove files and user-config entries written by the
former Codex integration. It must not grow into a live adapter or installer.
Remove it after the upgrade window has covered the last release that shipped
the integration.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tomllib
from pathlib import Path

from .atomic_io import atomic_write_json, atomic_write_text

logger = logging.getLogger(__name__)

_FORWARDER_DIR = "codex-hooks"
_HOOK_FORWARDER_FILE = "forwarder.ps1"
_NOTIFY_FORWARDER_FILE = "notify-forwarder.ps1"
_NOTIFY_CHAIN_FILE = "codex-notify-chain.json"
_HOOK_EVENTS = ("Stop", "PermissionRequest", "UserPromptSubmit")


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured and configured.strip():
        return Path(configured)
    return Path.home() / ".codex"


def _expected_hook_entry(forwarder_path: Path) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": "exit 0",
                "commandWindows": (
                    f'powershell -NoProfile -File "{forwarder_path}"'
                ),
                "timeout": 10,
            }
        ]
    }


def _cleanup_hooks_file(path: Path, forwarder_path: Path) -> None:
    if not path.exists():
        return
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("hooks.json root is not an object")

    hooks = loaded.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json hooks value is not an object")

    updated = copy.deepcopy(loaded)
    updated_hooks = updated["hooks"]
    expected = _expected_hook_entry(forwarder_path)
    changed = False
    for event_name in _HOOK_EVENTS:
        entries = updated_hooks.get(event_name)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise ValueError("hooks.json event value is not an array")
        filtered = [entry for entry in entries if entry != expected]
        if len(filtered) == len(entries):
            continue
        changed = True
        if filtered:
            updated_hooks[event_name] = filtered
        else:
            del updated_hooks[event_name]

    if not changed:
        return
    if not updated_hooks:
        del updated["hooks"]
    atomic_write_json(path, updated, indent=2, trailing_newline=True)


def _root_notify_line_span(text: str) -> tuple[int, int] | None:
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if stripped.lstrip().startswith("["):
            return None
        if stripped.lstrip().startswith("notify"):
            try:
                parsed = tomllib.loads(stripped)
            except tomllib.TOMLDecodeError as exc:
                raise ValueError("root notify is not a single-line value") from exc
            if "notify" in parsed:
                return offset, offset + len(line)
        offset += len(line)
    return None


def _toml_string_array(argv: list[str]) -> str:
    return json.dumps(argv)


def _replace_root_notify(
    text: str,
    span: tuple[int, int],
    replacement: list[str] | None,
) -> str:
    start, end = span
    line = "" if replacement is None else f"notify = {_toml_string_array(replacement)}\n"
    return text[:start] + line + text[end:]


def _read_chained_notify(path: Path) -> list[str] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    argv = loaded.get("argv")
    if (
        isinstance(argv, list)
        and argv
        and all(isinstance(value, str) for value in argv)
    ):
        return argv
    return None


def _cleanup_legacy_notify(
    config_path: Path,
    notify_forwarder: Path,
    chain_path: Path,
) -> None:
    if not config_path.exists():
        return
    text = config_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    expected = ["powershell", "-NoProfile", "-File", str(notify_forwarder)]
    if parsed.get("notify") != expected:
        return

    span = _root_notify_line_span(text)
    if span is None:
        raise ValueError("Whisperstream notify is not a root-level single-line value")
    replacement = _read_chained_notify(chain_path)
    updated = _replace_root_notify(text, span, replacement)
    tomllib.loads(updated)
    atomic_write_text(config_path, updated)


def _delete_generated_files(forwarder_dir: Path) -> None:
    for name in (
        _HOOK_FORWARDER_FILE,
        _NOTIFY_FORWARDER_FILE,
        _NOTIFY_CHAIN_FILE,
    ):
        (forwarder_dir / name).unlink(missing_ok=True)
    try:
        forwarder_dir.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        # Preserve a non-empty directory containing files Whisperstream does
        # not own.
        pass


def retire_codex_integration(
    *,
    app_dir: Path | None = None,
    codex_home: Path | None = None,
) -> bool:
    """Best-effort, idempotent cleanup of the retired integration.

    Generated forwarders are removed only after both external configuration
    surfaces were read and cleaned successfully. Any malformed or unwritable
    user configuration is left untouched so a later startup can retry.
    """
    try:
        if app_dir is None:
            from .config_manager import get_app_dir

            app_dir = get_app_dir()
        if codex_home is None:
            codex_home = _codex_home()

        forwarder_dir = Path(app_dir) / _FORWARDER_DIR
        hook_forwarder = forwarder_dir / _HOOK_FORWARDER_FILE
        notify_forwarder = forwarder_dir / _NOTIFY_FORWARDER_FILE
        _cleanup_hooks_file(Path(codex_home) / "hooks.json", hook_forwarder)
        _cleanup_legacy_notify(
            Path(codex_home) / "config.toml",
            notify_forwarder,
            forwarder_dir / _NOTIFY_CHAIN_FILE,
        )
        _delete_generated_files(forwarder_dir)
        logger.info("[CodexRetirement] cleanup complete")
        return True
    except Exception as exc:  # noqa: BLE001 - startup cleanup must never block
        logger.warning(
            "[CodexRetirement] cleanup incomplete reason=%s",
            type(exc).__name__,
        )
        return False
