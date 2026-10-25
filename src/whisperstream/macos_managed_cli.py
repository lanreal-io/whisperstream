"""Optional current-user command exposure for the native managed-session CLI."""
from __future__ import annotations

import os
from pathlib import Path

from .atomic_io import atomic_write_text

_START = "# >>> Whisperstream managed command >>>"
_END = "# <<< Whisperstream managed command <<<"
_BLOCK = f'{_START}\ncase ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac\n{_END}\n'


def _link() -> Path:
    return Path.home() / ".local" / "bin" / "whisperstream"


def _profiles() -> tuple[Path, ...]:
    home = Path.home()
    # Bash reads only the first existing login profile. Creating .bash_profile
    # above an existing .bash_login/.profile would hide the user's setup.
    bash = next(
        (home / name for name in (".bash_profile", ".bash_login", ".profile")
         if (home / name).exists() or (home / name).is_symlink()),
        home / ".bash_profile",
    )
    return home / ".zprofile", bash


def _removal_profiles() -> tuple[Path, ...]:
    # A later user-created profile may change Bash's precedence. Remove exact
    # owned blocks from any regular profile where an earlier install put one.
    home = Path.home()
    return tuple(home / name for name in (".zprofile", ".bash_profile", ".bash_login", ".profile"))


def _read_profile(path: Path) -> str:
    # Do not follow user profile symlinks to another owner's configuration.
    if path.is_symlink():
        raise OSError("shell profile is a symlink")
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _strip_owned_block(text: str) -> str:
    # An edited or ambiguous block is a conflict; never consume arbitrary text.
    if _START in text or _END in text:
        if text.count(_BLOCK) != 1 or text.count(_START) != 1 or text.count(_END) != 1:
            raise OSError("managed command profile block was edited")
        return text.replace(_BLOCK, "")
    return text


def status(target_dir: Path) -> dict:
    target = target_dir / "whisperstream"
    link = _link()
    available = target.is_file() and os.access(target, os.X_OK)
    installed = link.is_symlink() and link.resolve() == target.resolve()
    return {"ok": True, "status": "installed" if installed else "not_installed",
            "available": available, "installed": installed}


def install(target_dir: Path) -> dict:
    target = (target_dir / "whisperstream").resolve()
    before = status(target_dir)
    if not before["available"]:
        return {**before, "ok": False, "status": "unavailable"}
    link = _link()
    try:
        if (link.exists() or link.is_symlink()) and not before["installed"]:
            raise OSError("command path is occupied")
        originals = {path: _read_profile(path) for path in _profiles()}
        updates = {path: _strip_owned_block(text) for path, text in originals.items()}
        link.parent.mkdir(parents=True, exist_ok=True)
        if not before["installed"]:
            link.symlink_to(target)
        for path, text in updates.items():
            result = text + ("" if not text or text.endswith("\n") else "\n") + _BLOCK
            if result != originals[path]:
                atomic_write_text(path, result)
    except (OSError, UnicodeError):
        return {**status(target_dir), "ok": False, "status": "conflict"}
    return {**status(target_dir), "restart_required": True}


def uninstall(target_dir: Path) -> dict:
    before = status(target_dir)
    try:
        originals = {path: _read_profile(path) for path in _removal_profiles()
                     if path.exists() and not path.is_symlink()}
        updates = {path: _strip_owned_block(text) for path, text in originals.items()}
        for path, text in updates.items():
            if text != originals[path]:
                atomic_write_text(path, text)
        if before["installed"]:
            _link().unlink()
    except (OSError, UnicodeError):
        return {**status(target_dir), "ok": False, "status": "conflict"}
    return {**status(target_dir), "restart_required": before["installed"]}
