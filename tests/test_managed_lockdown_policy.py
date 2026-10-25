"""Machine-policy and startup reconciliation tests for managed Lockdown."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class _RegistryKey:
    pass


class _FakeWinreg:
    HKEY_LOCAL_MACHINE = object()
    KEY_READ = 0x20019
    KEY_WOW64_64KEY = 0x0100
    REG_DWORD = 4

    def __init__(self, *, value=1, value_type=REG_DWORD, open_error=None, query_error=None):
        self.value = value
        self.value_type = value_type
        self.open_error = open_error
        self.query_error = query_error
        self.open_calls = []
        self.closed = []

    def OpenKey(self, root, path, reserved, access):  # noqa: N802 - mirrors winreg
        self.open_calls.append((root, path, reserved, access))
        if self.open_error is not None:
            raise self.open_error
        return _RegistryKey()

    def QueryValueEx(self, key, name):  # noqa: N802 - mirrors winreg
        if name == "DisableHistory":
            raise FileNotFoundError()
        if self.query_error is not None:
            raise self.query_error
        return self.value, self.value_type

    def CloseKey(self, key):  # noqa: N802 - mirrors winreg
        self.closed.append(key)


@pytest.mark.parametrize(
    ("registry", "enabled", "state"),
    [
        (_FakeWinreg(open_error=FileNotFoundError()), False, "absent"),
        (_FakeWinreg(query_error=FileNotFoundError()), False, "absent"),
        (_FakeWinreg(value=0), False, "disabled"),
        (_FakeWinreg(value=1), True, "enabled"),
        (_FakeWinreg(value=42), True, "enabled"),
        (_FakeWinreg(value="1", value_type=1), True, "malformed"),
        (_FakeWinreg(value=0, value_type=1), True, "malformed"),
        (_FakeWinreg(open_error=PermissionError()), True, "inaccessible"),
        (_FakeWinreg(query_error=OSError()), True, "inaccessible"),
    ],
)
def test_registry_policy_states_fail_closed(registry, enabled, state):
    from whisperstream.managed_policy import _read_windows_policy

    result = _read_windows_policy(registry)

    assert result.enabled is enabled
    assert result.state == state


def test_registry_policy_explicitly_uses_64_bit_hklm_view():
    from whisperstream.managed_policy import POLICY_PATH, _read_windows_policy

    registry = _FakeWinreg(value=1)
    result = _read_windows_policy(registry)

    assert result.enabled is True
    assert registry.open_calls == [
        (
            registry.HKEY_LOCAL_MACHINE,
            POLICY_PATH,
            0,
            registry.KEY_READ | registry.KEY_WOW64_64KEY,
        )
    ]
    assert len(registry.closed) == 1


def _write_config(path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_managed_load_repairs_tampered_config_and_persists(tmp_path):
    from whisperstream.config_manager import ConfigManager

    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        'lockdown_mode = false\n'
        'history_enabled = true\n'
        'enhancement_enabled = true\n'
        'enhancement_provider = "openai"\n'
        'enhancement_local_endpoint = "https://attacker.invalid/v1"\n',
    )

    config = ConfigManager(
        config_path=config_path,
        models_root=tmp_path / "models",
    ).load(managed_lockdown=True, lockdown_envelope_exists=False)

    assert config.lockdown_mode is True
    assert config.history_enabled is False
    assert config.enhancement_provider == "local"
    assert config.enhancement_enabled is False
    assert config.enhancement_local_endpoint == ""
    persisted = config_path.read_text(encoding="utf-8")
    assert "lockdown_mode = true" in persisted
    assert "history_enabled = false" in persisted
    assert 'enhancement_provider = "local"' in persisted
    assert "attacker.invalid" not in persisted


def test_managed_load_initializes_deleted_config_without_envelope(tmp_path):
    from whisperstream.config_manager import ConfigManager

    config_path = tmp_path / "config.toml"
    config = ConfigManager(
        config_path=config_path,
        models_root=tmp_path / "models",
    ).load(managed_lockdown=True, lockdown_envelope_exists=False)

    assert config.lockdown_mode is True
    assert config.history_enabled is False
    assert config.enhancement_provider == "local"
    assert config_path.exists()


def test_managed_load_recovers_malformed_config_fail_closed(tmp_path):
    from whisperstream.config_manager import ConfigManager

    config_path = tmp_path / "config.toml"
    _write_config(config_path, "= invalid toml =")

    config = ConfigManager(
        config_path=config_path,
        models_root=tmp_path / "models",
    ).load(managed_lockdown=True, lockdown_envelope_exists=False)

    assert config.lockdown_mode is True
    assert config.history_enabled is False
    assert config.enhancement_provider == "local"
    assert "lockdown_mode = true" in config_path.read_text(encoding="utf-8")


def test_managed_load_recovers_semantically_invalid_config_fail_closed(tmp_path):
    from whisperstream.config_manager import ConfigManager

    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        'lockdown_mode = false\nclipboard_clear_delay = "not-a-number"\n',
    )

    config = ConfigManager(
        config_path=config_path,
        models_root=tmp_path / "models",
    ).load(managed_lockdown=True, lockdown_envelope_exists=False)

    assert config.lockdown_mode is True
    assert config.history_enabled is False
    assert config.enhancement_provider == "local"
    assert "not-a-number" not in config_path.read_text(encoding="utf-8")


def test_policy_removal_does_not_turn_off_existing_lockdown(tmp_path):
    from whisperstream.config_manager import ConfigManager

    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        'lockdown_mode = true\n'
        'history_enabled = false\n'
        'enhancement_provider = "local"\n',
    )

    config = ConfigManager(
        config_path=config_path,
        models_root=tmp_path / "models",
    ).load(managed_lockdown=False, lockdown_envelope_exists=False)

    assert config.lockdown_mode is True


@pytest.mark.parametrize("history_enabled", [False, True])
def test_managed_load_preserves_envelope_backed_history_state(
    tmp_path, monkeypatch, history_enabled
):
    from whisperstream.config_manager import ConfigManager

    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    (tmp_path / "lockdown.key.json").write_text("{}", encoding="utf-8")
    _write_config(
        config_path,
        f"lockdown_mode = false\n"
        f"history_enabled = {str(history_enabled).lower()}\n"
        "mac_history_opt_in = true\n"
        'enhancement_provider = "local"\n',
    )

    config = ConfigManager(
        config_path=config_path,
        models_root=tmp_path / "models",
    ).load(managed_lockdown=True, lockdown_envelope_exists=True)

    assert config.lockdown_mode is True
    assert config.history_enabled is history_enabled


def test_managed_load_remains_safe_when_repair_cannot_be_persisted(
    tmp_path, monkeypatch
):
    from whisperstream.config_manager import ConfigManager

    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        'lockdown_mode = false\nhistory_enabled = true\n',
    )
    monkeypatch.setattr(
        "whisperstream.atomic_io.atomic_write_text",
        MagicMock(side_effect=PermissionError("read only")),
    )

    config = ConfigManager(
        config_path=config_path,
        models_root=tmp_path / "models",
    ).load(managed_lockdown=True, lockdown_envelope_exists=False)

    assert config.lockdown_mode is True
    assert config.history_enabled is False
    assert config.enhancement_provider == "local"


def test_main_evaluates_policy_before_initializing_crash_logger(monkeypatch):
    from whisperstream import main as main_module
    from whisperstream.managed_policy import ManagedLockdownPolicy

    events: list[str] = []
    policy = ManagedLockdownPolicy(enabled=True, state="enabled")

    def _read_policy():
        events.append("policy")
        return policy

    def _safe_logger(*args, **kwargs):
        events.append("safe_logger")
        return logging.getLogger("test.managed.startup")

    monkeypatch.setattr(main_module, "read_managed_lockdown_policy", _read_policy)
    monkeypatch.setattr(main_module, "get_logger", _safe_logger)
    monkeypatch.setattr(main_module, "_run_smoke_check", MagicMock())

    main_module.main(["--check"])

    assert events == ["policy", "safe_logger"]


def test_app_managed_startup_passes_policy_into_config_reconciliation(monkeypatch):
    from whisperstream import main as main_module
    from whisperstream.config_manager import WhisperstreamConfig
    from whisperstream.managed_policy import ManagedLockdownPolicy

    config = WhisperstreamConfig(
        lockdown_mode=True,
        history_enabled=False,
        enhancement_provider="local",
    )
    manager = MagicMock()
    manager.load.return_value = config
    safe_diagnostics = MagicMock()
    policy = ManagedLockdownPolicy(enabled=True, state="enabled")

    monkeypatch.setattr(main_module, "ConfigManager", MagicMock(return_value=manager))
    monkeypatch.setattr(main_module.lockdown_keystore, "is_lockdown_enabled", lambda _: False)
    monkeypatch.setattr(
        main_module,
        "get_logger",
        lambda *a, **k: logging.getLogger("test.managed.app"),
    )
    monkeypatch.setattr(main_module, "AudioCapture", MagicMock())
    monkeypatch.setattr(main_module, "AudioFeedback", MagicMock())
    monkeypatch.setattr(main_module, "KeyboardShortcutManager", MagicMock())
    monkeypatch.setattr(
        main_module,
        "SafeDiagnosticSink",
        MagicMock(return_value=safe_diagnostics),
    )

    app = main_module.WhisperstreamApp(
        headless=True,
        defer_model_load=True,
        managed_policy=policy,
    )

    assert app.managed_lockdown_policy is policy
    manager.load.assert_called_once_with(
        managed_lockdown=True,
        managed_history_disabled=False,
        lockdown_envelope_exists=False,
    )
    safe_diagnostics.emit.assert_called_once_with(
        "app_started",
        app_version=main_module._SOFTWARE_VERSION,
        managed=True,
        policy_state="enabled",
        history_enabled=False,
    )
    app.cleanup()


@pytest.mark.parametrize("master", [0, 1])
@pytest.mark.parametrize("value,kind,expected,state", [
    (0, 4, False, "disabled"), (1, 4, True, "enabled"),
    (42, 4, True, "enabled"), ("0", 1, True, "malformed"),
    (FileNotFoundError(), 4, False, "absent"), (PermissionError(), 4, True, "inaccessible"),
])
def test_history_policy_is_independent_and_fail_closed(master, value, kind, expected, state):
    from whisperstream.managed_policy import _read_windows_policy

    class Registry(_FakeWinreg):
        def QueryValueEx(self, key, name):
            if name == "ManagedLockdown":
                return master, self.REG_DWORD
            if isinstance(value, Exception):
                raise value
            return value, kind

    result = _read_windows_policy(Registry())
    assert result.enabled is bool(master)
    assert result.history_disabled is expected
    assert result.history_state == state


def test_unreadable_key_disables_history_too():
    from whisperstream.managed_policy import _read_windows_policy
    result = _read_windows_policy(_FakeWinreg(open_error=PermissionError()))
    assert result.history_disabled is True
    assert result.history_state == "inaccessible"


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("body", [None, 'history_enabled = true\nsave_audio = true\n', 'broken = ['])
def test_history_policy_reconciles_fresh_and_tampered_config(tmp_path, body, managed):
    from whisperstream.config_manager import ConfigManager
    path = tmp_path / "config.toml"
    if body is not None:
        _write_config(path, body)
    config = ConfigManager(config_path=path).load(
        managed_lockdown=managed, managed_history_disabled=True,
        lockdown_envelope_exists=True,
    )
    assert config.history_enabled is False
    assert config.save_audio is False
    assert config.lockdown_mode is managed
    assert "history_enabled = false" in path.read_text(encoding="utf-8")
    assert "save_audio = false" in path.read_text(encoding="utf-8")


def test_history_policy_prevents_fresh_store_creation(tmp_path, monkeypatch):
    from whisperstream import main
    from whisperstream.managed_policy import ManagedLockdownPolicy
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    app = main.WhisperstreamApp.__new__(main.WhisperstreamApp)
    app.managed_lockdown_policy = ManagedLockdownPolicy(True, "enabled", True, "enabled")
    factory = MagicMock(side_effect=AssertionError("must not open history"))
    monkeypatch.setattr(main, "TranscriptStore", factory)
    assert app.ensure_transcript_store_initialized(history_opt_in=True) is None
    factory.assert_not_called()
    assert list(tmp_path.iterdir()) == []
