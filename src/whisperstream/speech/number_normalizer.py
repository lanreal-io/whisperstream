"""Small, dependency-free English number normalization for Kokoro.

This module is an original implementation. It intentionally replaces the LGPL
``num2words`` dependency used by the upstream Misaki English frontend.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
)
_TEENS = (
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)
_SCALES = (
    (10**15, "quadrillion"),
    (10**12, "trillion"),
    (10**9, "billion"),
    (10**6, "million"),
    (10**3, "thousand"),
)
_ORDINAL_WORDS = {
    "one": "first",
    "two": "second",
    "three": "third",
    "four": "fourth",
    "five": "fifth",
    "six": "sixth",
    "seven": "seventh",
    "eight": "eighth",
    "nine": "ninth",
    "ten": "tenth",
    "eleven": "eleventh",
    "twelve": "twelfth",
    "twenty": "twentieth",
    "thirty": "thirtieth",
    "forty": "fortieth",
    "fifty": "fiftieth",
    "sixty": "sixtieth",
    "seventy": "seventieth",
    "eighty": "eightieth",
    "ninety": "ninetieth",
    "hundred": "hundredth",
    "thousand": "thousandth",
    "million": "millionth",
    "billion": "billionth",
    "trillion": "trillionth",
    "quadrillion": "quadrillionth",
}
_CURRENCIES = {
    "$": ("dollar", "cent"),
    "£": ("pound", "pence"),
    "€": ("euro", "cent"),
}
_ORDINAL_SUFFIX = re.compile(r"^(?P<number>\d+)(?:st|nd|rd|th)$", re.IGNORECASE)


@dataclass(frozen=True)
class NumberContext:
    """How a numeric token should be spoken."""

    currency: str | None = None
    digit_by_digit: bool = False
    time: bool = False
    version: bool = False
    year: bool | None = None


def _under_thousand(value: int) -> tuple[str, ...]:
    words: list[str] = []
    if value >= 100:
        words.extend((_ONES[value // 100], "hundred"))
        value %= 100
    if value >= 20:
        words.append(_TENS[value // 10])
        value %= 10
    elif value >= 10:
        words.append(_TEENS[value - 10])
        value = 0
    if value:
        words.append(_ONES[value])
    return tuple(words)


def cardinal_words(value: int) -> tuple[str, ...]:
    """Return US English cardinal words without optional ``and``."""

    if value == 0:
        return ("zero",)
    if value < 0:
        return ("minus", *cardinal_words(-value))
    words: list[str] = []
    for scale, name in _SCALES:
        if value >= scale:
            quotient, value = divmod(value, scale)
            words.extend(cardinal_words(quotient))
            words.append(name)
    if value:
        words.extend(_under_thousand(value))
    return tuple(words)


def ordinal_words(value: int) -> tuple[str, ...]:
    """Return US English ordinal words."""

    words = list(cardinal_words(value))
    if not words:
        return ()
    words[-1] = _ORDINAL_WORDS.get(words[-1], words[-1] + "th")
    return tuple(words)


def year_words(value: int) -> tuple[str, ...]:
    """Return the compact form normally used for four-digit years."""

    if not 1000 <= value <= 9999:
        return cardinal_words(value)
    first, last = divmod(value, 100)
    if last == 0:
        if value % 1000 == 0:
            return cardinal_words(value)
        return (*cardinal_words(first), "hundred")
    if last < 10:
        return (*cardinal_words(first), "oh", *_ONES[last : last + 1])
    return (*cardinal_words(first), *cardinal_words(last))


def _plural(unit: str, value: int) -> str:
    if abs(value) == 1 or unit == "pence":
        return unit
    return unit + "s"


def _currency_words(token: str, symbol: str) -> tuple[str, ...]:
    major_unit, minor_unit = _CURRENCIES[symbol]
    normalized = token.replace(",", "")
    major_text, dot, minor_text = normalized.partition(".")
    major = int(major_text or "0")
    minor = int((minor_text + "00")[:2]) if dot else 0
    result: list[str] = []
    if major or not minor:
        result.extend(cardinal_words(major))
        result.append(_plural(major_unit, major))
    if minor:
        if result:
            result.append("and")
        result.extend(cardinal_words(minor))
        result.append(_plural(minor_unit, minor))
    return tuple(result)


def _digits(text: str) -> tuple[str, ...]:
    return tuple(_ONES[int(char)] for char in text if char.isdigit())


def expand_number(
    token: str,
    *,
    context: NumberContext | None = None,
) -> tuple[str, ...]:
    """Expand one numeric token into words.

    The caller identifies semantic forms such as versions and clock times. That
    keeps this utility deterministic and avoids guessing from transcript
    content.
    """

    context = context or NumberContext()
    raw = token.strip()
    if not raw:
        raise ValueError("number token is empty")
    if context.currency:
        if context.currency not in _CURRENCIES:
            raise ValueError(f"unsupported currency: {context.currency}")
        return _currency_words(raw, context.currency)
    if context.digit_by_digit:
        words = _digits(raw)
        if not words:
            raise ValueError(f"not a numeric token: {token}")
        return words
    if context.time:
        hour_text, separator, minute_text = raw.partition(":")
        if not separator or not hour_text.isdigit() or not minute_text.isdigit():
            raise ValueError(f"not a clock time: {token}")
        hour, minute = int(hour_text), int(minute_text)
        minute_words = (
            ("oh", *_digits(minute_text[-1:]))
            if 0 < minute < 10
            else cardinal_words(minute)
        )
        return (*cardinal_words(hour), *minute_words)
    if context.version:
        parts = re.split(r"[._]", raw)
        if not parts or any(not part.isdigit() for part in parts):
            raise ValueError(f"not a numeric version: {token}")
        return tuple(word for part in parts for word in cardinal_words(int(part)))

    ordinal = _ORDINAL_SUFFIX.fullmatch(raw)
    if ordinal:
        return ordinal_words(int(ordinal.group("number")))

    sign: tuple[str, ...] = ()
    if raw.startswith(("-", "+")):
        if raw[0] == "-":
            sign = ("minus",)
        raw = raw[1:]
    raw = raw.replace(",", "")
    if raw.count(".") > 1:
        return (*sign, *_digits(raw))
    if "." in raw:
        whole, fraction = raw.split(".", 1)
        if not whole.isdigit() or not fraction.isdigit():
            raise ValueError(f"not a numeric token: {token}")
        return (
            *sign,
            *cardinal_words(int(whole or "0")),
            "point",
            *_digits(fraction),
        )
    if not raw.isdigit():
        raise ValueError(f"not a numeric token: {token}")
    if len(raw) > 1 and raw.startswith("0"):
        return (*sign, *_digits(raw))
    value = int(raw)
    use_year = context.year is True or (
        context.year is None and len(raw) == 4 and value >= 1000
    )
    return (*sign, *(year_words(value) if use_year else cardinal_words(value)))


__all__ = [
    "NumberContext",
    "cardinal_words",
    "expand_number",
    "ordinal_words",
    "year_words",
]
