"""Pure TOML literals for the existing configuration format.

The ConfigManager save transaction owns validation, secrets and atomic writes.
"""

import re
from typing import Any


def toml_value(value: Any) -> str:
    """Format a Python value as a TOML literal."""
    if value is None or value == "":
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Preserve one decimal for round floats (e.g. 45.0 not 45)
        if value == int(value):
            return f"{value:.1f}"
        return str(value)
    if isinstance(value, str):
        if "\n" in value or "\r" in value:
            # Multi-line basic string: newlines are literal inside """...""".
            # Escape backslashes, then any run of 3+ quotes (which would
            # otherwise terminate the string early). Lone " is allowed.
            escaped = value.replace("\\", "\\\\")
            escaped = re.sub(
                r'"{3,}',
                lambda m: '\\"' * len(m.group(0)),
                escaped,
            )
            return f'"""\n{escaped}"""'
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"Unsupported TOML value type: {type(value).__name__}")


def toml_key(key: str) -> str:
    """Format a TOML key, quoting it if it contains special characters."""
    if re.match(r"^[A-Za-z0-9_-]+$", key):
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def toml_inline_array(values: Any) -> str:
    """Format a sequence as a TOML inline array literal."""
    return "[" + ", ".join(toml_value(v) for v in values) + "]"
