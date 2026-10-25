"""Profile configuration stays independent of enhancement execution."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from whisperstream.config_manager import ConfigManager, WhisperstreamConfig, _DEFAULT_POWER_MODE_MAP
from whisperstream.config_profiles import prune_profile_routing
from whisperstream.enhancement_prompts import CHAT_PROMPT, CODE_PROMPT, EMAIL_PROMPT


def test_profile_loading_never_imports_enhancer_execution(tmp_path):
    # A fresh interpreter makes the dependency assertion real even when another
    # collected test has already imported the enhancer in this pytest process.
    code = textwrap.dedent('''
        import importlib.abc
        import sys

        class RejectEnhancer(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "whisperstream.llm_enhancer":
                    raise AssertionError("configuration imported enhancer execution")

        sys.meta_path.insert(0, RejectEnhancer())
        from whisperstream.config_manager import WhisperstreamConfig, _new_install_config

        migrated = WhisperstreamConfig.from_dict({"enhancement_mode": "custom"})
        migrated.validate()
        assert migrated.default_profile_id == "custom"
        restored = WhisperstreamConfig.from_dict(migrated.to_dict())
        restored.validate()
        assert restored.profiles == migrated.profiles
        fresh = _new_install_config()
        fresh.validate()
        assert fresh.default_profile_id == "chat"
    ''')
    env = dict(os.environ, WHISPERSTREAM_DATA_DIR=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_deleted_profile_prunes_all_references_and_shortcuts_together():
    routing, warnings = prune_profile_routing(
        power_mode_map={"Editor.EXE": "code", "removed.exe": "gone"},
        power_mode_paths={"Editor.EXE": "synthetic/editor.exe", "removed.exe": "synthetic/removed.exe"},
        site_map={"EXAMPLE.TEST": "code", "removed.test": "gone"},
        profile_hotkeys={
            "code": "CTRL+1", "chat": "ctrl+1", "email": "CTRL+2", "gone": "ctrl+3",
        },
        profile_ids={"code", "chat", "email"},
        reserved_shortcuts=("ctrl+2",),
    )

    assert routing == {
        "power_mode_map": {"editor.exe": "code"},
        "power_mode_paths": {"editor.exe": "synthetic/editor.exe"},
        "site_map": {"example.test": "code"},
        "profile_hotkeys": {"code": "ctrl+1"},
    }
    assert len(warnings) == 3


@pytest.mark.parametrize(
    ("mode", "custom_prompt", "default", "expected_prompt"),
    [
        ("default", "", "chat", CHAT_PROMPT),
        ("chat", "", "chat", CHAT_PROMPT),
        ("code", "", "code", CODE_PROMPT),
        ("custom", "Synthetic cleanup instructions", "custom", "Synthetic cleanup instructions"),
        ("custom", "", "custom", CHAT_PROMPT),
        ("unknown", "", "chat", CHAT_PROMPT),
    ],
)
def test_legacy_profile_migration_preserves_mode_and_prompt(mode, custom_prompt, default, expected_prompt):
    config = WhisperstreamConfig.from_dict({
        "enhancement_mode": mode, "enhancement_custom_prompt": custom_prompt,
    })
    rows = {row["id"]: row for row in config.profiles}
    assert config.default_profile_id == default
    assert rows[default]["prompt"] == expected_prompt
    assert rows["email"]["prompt"] == EMAIL_PROMPT
    assert list(rows) == ["chat", "code", "email"] + (["custom"] if mode == "custom" else [])


def test_explicit_empty_profiles_only_receive_email_backfill():
    config = WhisperstreamConfig.from_dict({"profiles": [], "default_profile_id": ""})
    config.validate()
    assert [row["id"] for row in config.profiles] == ["email"]
    assert config.default_profile_id == ""


def test_edited_email_and_routing_survive_save_reload(tmp_path):
    config = WhisperstreamConfig.from_dict({
        "profiles": [{"id": "email", "name": "Renamed mail", "prompt": "Synthetic edited prompt", "signature": "Synthetic signature"}],
        "default_profile_id": "email",
        "email_signature": "Retired signature",
        "power_mode_map": {"synthetic.exe": "email"},
        "site_map": {"MAIL.GOOGLE.COM": "email"},
        "profile_hotkeys": {"email": "CTRL+ALT+1"},
    })
    manager = ConfigManager(config_path=tmp_path / "config.toml", models_root=tmp_path / "models")
    manager.save(config)
    restored = manager.load()
    assert restored.profiles == config.profiles
    assert restored.profiles[0]["signature"] == "Synthetic signature"
    assert restored.default_profile_id == "email"
    assert restored.power_mode_map == config.power_mode_map
    assert restored.site_map == config.site_map
    assert restored.profile_hotkeys == {"email": "ctrl+alt+1"}


def test_legacy_signature_transfers_after_email_backfill():
    config = WhisperstreamConfig.from_dict({"profiles": [], "email_signature": "Synthetic signature"})
    assert config.profiles[0]["signature"] == "Synthetic signature"


def test_routing_absence_and_empty_maps_keep_distinct_migrations():
    missing = WhisperstreamConfig.from_dict({})
    empty = WhisperstreamConfig.from_dict({"power_mode_map": {}, "site_map": {}, "profile_hotkeys": {}})
    assert missing.power_mode_map == _DEFAULT_POWER_MODE_MAP
    assert empty.power_mode_map == {}
    assert empty.site_map == missing.site_map == {
        "mail.google.com": "email", "outlook.office.com": "email", "outlook.live.com": "email",
    }
    assert missing.profile_hotkeys == empty.profile_hotkeys == {}


def test_email_route_backfills_never_replace_existing_targets():
    config = WhisperstreamConfig.from_dict({
        "power_mode_map": {"OUTLOOK.EXE": "code"},
        "site_map": {"MAIL.GOOGLE.COM": "code"},
    })
    assert config.power_mode_map == {"outlook.exe": "code", "thunderbird.exe": "email"}
    assert config.site_map["mail.google.com"] == "code"
    assert config.site_map["outlook.office.com"] == "email"
    restored = WhisperstreamConfig.from_dict(config.to_dict())
    assert restored.power_mode_map == config.power_mode_map
    assert restored.site_map == config.site_map


@pytest.mark.parametrize(
    ("profiles", "default", "message"),
    [
        ([{"id": ""}], "", "non-empty"),
        ([{"id": "same"}, {"id": "same"}], "same", "unique"),
        ([{"id": "present"}], "gone", "existing profile"),
    ],
)
def test_invalid_profile_identity_or_default_is_rejected(profiles, default, message):
    with pytest.raises(ValueError, match=message):
        WhisperstreamConfig(profiles=profiles, default_profile_id=default).validate()
