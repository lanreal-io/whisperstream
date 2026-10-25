"""Source-contract tests for managed Lockdown deployment controls."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "src-tauri" / "installer" / "hooks.nsh"
POLICY_SCRIPT = ROOT / "scripts" / "set-managed-lockdown-policy.ps1"
TAURI_CONFIG = ROOT / "src-tauri" / "tauri.windows.conf.json"


def test_installer_accepts_only_the_explicit_managed_lockdown_switch() -> None:
    hooks = HOOKS.read_text(encoding="utf-8")

    assert '${GetOptions} $R0 "/MANAGEDLOCKDOWN=" $R1' in hooks
    assert '${If} $R1 == "1"' in hooks
    assert "The /MANAGEDLOCKDOWN option accepts only the value 1." in hooks
    exact_match = hooks.index('${If} $R1 == "1"')
    assert exact_match < hooks.index("${Else}", exact_match)
    assert "        87" in hooks
    assert "WHISPERSTREAM_APPLY_MANAGED_LOCKDOWN_POLICY" in hooks
    assert "!insertmacro WHISPERSTREAM_APPLY_MANAGED_LOCKDOWN_POLICY" in hooks


def test_installer_writes_and_verifies_only_the_64_bit_machine_policy() -> None:
    hooks = HOOKS.read_text(encoding="utf-8")

    assert "SetRegView 64" in hooks
    assert (
        'WriteRegDWORD HKLM "SOFTWARE\\Policies\\Whisperstream" '
        '"ManagedLockdown" 1'
    ) in hooks
    assert (
        'ReadRegDWORD $R3 HKLM "SOFTWARE\\Policies\\Whisperstream" '
        '"ManagedLockdown"'
    ) in hooks
    assert "SetErrorLevel ${ERRORCODE}" in hooks
    assert hooks.count("          740") == 6
    assert "Quit" in hooks
    assert "WriteRegDWORD HKCU" not in hooks
    assert "WriteRegStr HKCU" not in hooks


def test_uninstaller_never_removes_customer_owned_policy() -> None:
    hooks = HOOKS.read_text(encoding="utf-8")
    postuninstall = hooks.split("!macro NSIS_HOOK_POSTUNINSTALL", 1)[1]

    assert "DeleteRegValue" not in postuninstall
    assert "DeleteRegKey" not in postuninstall
    assert "Policies\\Whisperstream" not in postuninstall


def test_tauri_keeps_the_installer_hook_wired() -> None:
    config = TAURI_CONFIG.read_text(encoding="utf-8")

    assert '"installerHooks": "installer/hooks.nsh"' in config


def test_policy_script_uses_explicit_64_bit_hklm_view_and_is_idempotent() -> None:
    script = POLICY_SCRIPT.read_text(encoding="utf-8")

    assert '[ValidateSet("Enable", "Remove")]' in script
    assert "WindowsBuiltInRole]::Administrator" in script
    assert "RegistryHive]::LocalMachine" in script
    assert "RegistryView]::Registry64" in script
    assert 'CreateSubKey("SOFTWARE\\Policies\\Whisperstream"' in script
    assert '.SetValue(\n                "ManagedLockdown",\n                1,' in script
    assert 'DeleteValue("ManagedLockdown", $false)' in script
    assert 'GetValue("ManagedLockdown"' in script
    assert "exit 0" in script


def test_installer_history_switch_verifies_machine_value():
    hooks = HOOKS.read_text(encoding="utf-8")
    assert '${GetOptions} $R0 "/DISABLEHISTORY=" $R1' in hooks
    assert 'WriteRegDWORD HKLM "SOFTWARE\\Policies\\Whisperstream" "DisableHistory" 1' in hooks
    assert 'ReadRegDWORD $R3 HKLM "SOFTWARE\\Policies\\Whisperstream" "DisableHistory"' in hooks
    assert hooks.index('The /DISABLEHISTORY option accepts only the value 1.') < hooks.index('WriteRegDWORD HKLM')


def test_management_script_history_option_is_explicit():
    script = POLICY_SCRIPT.read_text(encoding="utf-8")
    assert '[switch] $DisableHistory' in script
    assert script.count('if ($DisableHistory)') == 2
    assert 'SetValue("DisableHistory", 1, [Microsoft.Win32.RegistryValueKind]::DWord)' in script
    assert 'DeleteValue("DisableHistory", $false)' in script
