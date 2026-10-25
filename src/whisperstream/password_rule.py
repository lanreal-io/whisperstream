"""Shared rule for NEW Lockdown and recovery passwords.

Applied only when a password is set or changed, never at unlock, so existing
shorter Lockdown passwords keep working. Length plus a common-password block,
with no composition rules (NIST SP 800-63B style). Never log the password.
"""
from __future__ import annotations

import re
from enum import StrEnum

from .common_passwords import COMMON_PASSWORDS

MIN_PASSWORD_LENGTH = 12


class PasswordRuleError(StrEnum):
    TOO_SHORT = "password_too_short"
    TOO_COMMON = "password_too_common"


def check_new_password(password: str) -> PasswordRuleError | None:
    """Return why *password* is not acceptable as a new password, or None.

    Surrounding and repeated whitespace doesn't count toward the length, so
    padding can't turn a short or common password into an accepted one. The
    exact password is still what gets wrapped and later unlocks.
    """

    meaningful = re.sub(r"\s+", " ", password).strip()
    if meaningful and max(len(word) for word in meaningful.split(" ")) <= 2:
        # "p a s s w o r d": spacing out letters adds no strength.
        meaningful = meaningful.replace(" ", "")
    if len(meaningful) < MIN_PASSWORD_LENGTH:
        return PasswordRuleError.TOO_SHORT
    folded = meaningful.lower()
    if folded in COMMON_PASSWORDS or folded.replace(" ", "") in COMMON_PASSWORDS:
        return PasswordRuleError.TOO_COMMON
    return None
