"""Shared new-password rule: 12 characters and no common passwords."""

from __future__ import annotations

import pytest

from whisperstream.password_rule import (
    MIN_PASSWORD_LENGTH,
    PasswordRuleError,
    check_new_password,
)


def test_minimum_is_twelve() -> None:
    assert MIN_PASSWORD_LENGTH == 12
    assert len("plum tide ow") == 12
    assert check_new_password("plum tide ow") is None
    assert check_new_password("short pass") is PasswordRuleError.TOO_SHORT
    assert check_new_password("eleven char") is PasswordRuleError.TOO_SHORT


@pytest.mark.parametrize("password", ["password1234", "PASSWORD1234", " password1234 "])
def test_common_passwords_are_rejected_case_insensitively(password: str) -> None:
    assert check_new_password(password) is PasswordRuleError.TOO_COMMON


@pytest.mark.parametrize(
    "password",
    ["correct horse battery", "no digits or symbols here", "twelve chars"],
)
def test_no_composition_rules(password: str) -> None:
    assert check_new_password(password) is None


@pytest.mark.parametrize(
    "password",
    [" " * 12, " " * 40, "password    ", "\tpassword\n\n\n\n", "short  pass  "],
)
def test_padding_does_not_count_toward_length(password: str) -> None:
    # Security review 2026-10-23 finding 7: padding passed the length check
    # while the blocklist compared the trimmed value.
    assert check_new_password(password) is PasswordRuleError.TOO_SHORT


def test_common_password_split_by_spaces_is_rejected() -> None:
    assert check_new_password("password 1234") is PasswordRuleError.TOO_COMMON


@pytest.mark.parametrize("password", ["p a s s w o r d", "p a s s w o r d 1 2", "pa ss wo rd 12"])
def test_spaced_out_letters_do_not_count_as_length(password: str) -> None:
    assert check_new_password(password) is PasswordRuleError.TOO_SHORT


def test_spaced_out_common_password_is_rejected() -> None:
    assert check_new_password("p a s s w o r d 1 2 3 4") is PasswordRuleError.TOO_COMMON
