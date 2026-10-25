"""Machine-level Windows policy authority for managed Lockdown.

This module is deliberately silent: policy evaluation happens before ordinary
diagnostic logging is initialized. Callers may inspect the bounded ``state``
value after the Lockdown logging boundary is active.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal, Protocol

try:
    import winreg

    _WINREG_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by non-Windows builds
    winreg = None  # type: ignore[assignment]
    _WINREG_AVAILABLE = False


POLICY_PATH = r"SOFTWARE\Policies\Whisperstream"
POLICY_VALUE = "ManagedLockdown"
HISTORY_POLICY_VALUE = "DisableHistory"

PolicyState = Literal[
    "unsupported",
    "absent",
    "disabled",
    "enabled",
    "malformed",
    "inaccessible",
]


@dataclass(frozen=True)
class ManagedLockdownPolicy:
    """One startup's immutable policy evaluation."""

    enabled: bool
    state: PolicyState
    history_disabled: bool = False
    history_state: PolicyState = "absent"


class _Registry(Protocol):
    HKEY_LOCAL_MACHINE: object
    KEY_READ: int
    KEY_WOW64_64KEY: int
    REG_DWORD: int

    def OpenKey(self, root: object, path: str, reserved: int, access: int): ...
    def QueryValueEx(self, key: object, name: str): ...
    def CloseKey(self, key: object) -> None: ...


def _read_windows_policy(registry: _Registry) -> ManagedLockdownPolicy:
    """Read the authoritative value from the explicit 64-bit HKLM view.

    Absence means unmanaged. A present value must be an exact DWORD zero to
    disable the policy; nonzero DWORDs enable it and malformed present values
    fail closed. Access failures also fail closed because the process cannot
    prove an administrator policy is absent.
    """

    key = None
    try:
        key = registry.OpenKey(
            registry.HKEY_LOCAL_MACHINE, POLICY_PATH, 0,
            registry.KEY_READ | registry.KEY_WOW64_64KEY,
        )
    except FileNotFoundError:
        return ManagedLockdownPolicy(enabled=False, state="absent")
    except OSError:
        return ManagedLockdownPolicy(
            enabled=True, state="inaccessible",
            history_disabled=True, history_state="inaccessible",
        )

    def read_value(name: str) -> tuple[bool, PolicyState]:
        try:
            value, value_type = registry.QueryValueEx(key, name)
        except FileNotFoundError:
            return False, "absent"
        except OSError:
            return True, "inaccessible"
        if value_type != registry.REG_DWORD or type(value) is not int:
            return True, "malformed"
        return (False, "disabled") if value == 0 else (True, "enabled")

    try:
        enabled, state = read_value(POLICY_VALUE)
        history_disabled, history_state = read_value(HISTORY_POLICY_VALUE)
        return ManagedLockdownPolicy(enabled, state, history_disabled, history_state)
    finally:
        try:
            registry.CloseKey(key)
        except OSError:
            pass


def read_managed_lockdown_policy() -> ManagedLockdownPolicy:
    """Return the machine policy without logging or raising."""

    if sys.platform != "win32":
        return ManagedLockdownPolicy(enabled=False, state="unsupported")
    if not _WINREG_AVAILABLE or winreg is None:
        # A Windows build unable to inspect HKLM cannot establish absence.
        return ManagedLockdownPolicy(
            enabled=True, state="inaccessible",
            history_disabled=True, history_state="inaccessible",
        )
    return _read_windows_policy(winreg)


def history_disabled_by_policy(app: object) -> bool:
    """Read process authority, never a user-editable config field."""
    return getattr(getattr(app, "managed_lockdown_policy", None), "history_disabled", False) is True


HISTORY_DISABLED_MESSAGE = "Transcript and audio history are disabled by your organization."
