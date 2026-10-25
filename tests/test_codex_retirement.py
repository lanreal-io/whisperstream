"""Focused tests for the one-release Codex retirement migration."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from whisperstream.codex_retirement import retire_codex_integration


def _forwarder_dir(app_dir: Path) -> Path:
    path = app_dir / "codex-hooks"
    path.mkdir(parents=True)
    (path / "forwarder.ps1").write_text("hook", encoding="utf-8")
    (path / "notify-forwarder.ps1").write_text("notify", encoding="utf-8")
    return path


def _whisperstream_hook(forwarder: Path) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": "exit 0",
                "commandWindows": (
                    f'powershell -NoProfile -File "{forwarder}"'
                ),
                "timeout": 10,
            }
        ]
    }


def test_retirement_preserves_user_hooks_and_restores_chained_notify(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    generated = _forwarder_dir(app_dir)
    hook_forwarder = generated / "forwarder.ps1"
    notify_forwarder = generated / "notify-forwarder.ps1"
    ours = _whisperstream_hook(hook_forwarder)
    user_hook = {
        "hooks": [
            {
                "type": "command",
                "command": "user-command",
                "timeout": 3,
            }
        ]
    }
    (codex_home / "hooks.json").write_text(
        json.dumps(
            {
                "description": "keep me",
                "hooks": {
                    "Stop": [user_hook, ours],
                    "PermissionRequest": [ours],
                    "UserPromptSubmit": [ours],
                    "SessionStart": [user_hook],
                },
            }
        ),
        encoding="utf-8",
    )
    previous_notify = ["python", "user-notify.py"]
    (generated / "codex-notify-chain.json").write_text(
        json.dumps({"argv": previous_notify}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        (
            "notify = "
            + json.dumps(
                ["powershell", "-NoProfile", "-File", str(notify_forwarder)]
            )
            + "\n# keep this comment\n[features]\nweb_search = true\n"
        ),
        encoding="utf-8",
    )

    assert retire_codex_integration(
        app_dir=app_dir,
        codex_home=codex_home,
    )
    hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    assert hooks == {
        "description": "keep me",
        "hooks": {
            "Stop": [user_hook],
            "SessionStart": [user_hook],
        },
    }
    config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert tomllib.loads(config_text)["notify"] == previous_notify
    assert "# keep this comment" in config_text
    assert not generated.exists()

    assert retire_codex_integration(
        app_dir=app_dir,
        codex_home=codex_home,
    )


def test_retirement_removes_unchained_notify_and_preserves_other_config(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    generated = _forwarder_dir(app_dir)
    notify_forwarder = generated / "notify-forwarder.ps1"
    (codex_home / "config.toml").write_text(
        (
            "notify = "
            + json.dumps(
                ["powershell", "-NoProfile", "-File", str(notify_forwarder)]
            )
            + "\nmodel = \"user-model\"\n"
        ),
        encoding="utf-8",
    )

    assert retire_codex_integration(
        app_dir=app_dir,
        codex_home=codex_home,
    )
    assert tomllib.loads(
        (codex_home / "config.toml").read_text(encoding="utf-8")
    ) == {"model": "user-model"}
    assert not generated.exists()


def test_retirement_leaves_user_notify_that_no_longer_points_to_whisperstream(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    generated = _forwarder_dir(app_dir)
    user_notify = ["python", "replacement.py"]
    (codex_home / "config.toml").write_text(
        "notify = " + json.dumps(user_notify) + "\n",
        encoding="utf-8",
    )

    assert retire_codex_integration(
        app_dir=app_dir,
        codex_home=codex_home,
    )
    assert tomllib.loads(
        (codex_home / "config.toml").read_text(encoding="utf-8")
    )["notify"] == user_notify
    assert not generated.exists()


def test_retirement_keeps_forwarders_when_user_config_cannot_be_cleaned(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    generated = _forwarder_dir(app_dir)
    invalid = "notify = [\n"
    (codex_home / "config.toml").write_text(invalid, encoding="utf-8")

    assert not retire_codex_integration(
        app_dir=app_dir,
        codex_home=codex_home,
    )
    assert (codex_home / "config.toml").read_text(encoding="utf-8") == invalid
    assert (generated / "forwarder.ps1").exists()
    assert (generated / "notify-forwarder.ps1").exists()
