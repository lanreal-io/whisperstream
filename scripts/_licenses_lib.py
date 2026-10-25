"""Pure helper functions for the license generator.

These are extracted from generate-licenses.sh and ported to Python with exact
semantic equivalence. No subprocess, no coreutils -- pure Python stdlib only.

This module is imported by scripts/generate_licenses.py (Task 3.2) and tested
by tests/test_licenses_lib.py (Task 3.1).
"""

import re
from typing import Tuple


# ---------------------------------------------------------------------------
# classify_flag
# ---------------------------------------------------------------------------

def classify_flag(spdx: str) -> str:
    """Return a flag label for a given SPDX license expression.

    Matches the bash cascade in generate-licenses.sh lines 146-163.
    The check is done on an uppercase version of the input.
    Precedence (first match wins): AGPL > LGPL > GPL > MPL > Custom.
    Empty string or "UNKNOWN" (case-insensitive) -> "UNKNOWN".
    Any other license with no copyleft match -> "".

    Args:
        spdx: A license identifier string, e.g. "MIT", "LGPL-2.1-or-later".

    Returns:
        One of "AGPL", "LGPL", "GPL", "MPL", "Custom", "UNKNOWN", or "".
    """
    if not spdx or spdx.upper() == "UNKNOWN":
        return "UNKNOWN"

    up = spdx.upper()

    if "AGPL" in up:
        return "AGPL"
    if "LGPL" in up:
        return "LGPL"
    if "GPL" in up:
        return "GPL"
    if "MPL" in up:
        return "MPL"
    if "CUSTOM" in up:
        return "Custom"

    return ""


# ---------------------------------------------------------------------------
# normalize_pkg_name
# ---------------------------------------------------------------------------

def normalize_pkg_name(name: str) -> str:
    """Normalize a package name to lowercase with dashes replaced by underscores.

    Replaces the ``tr '[:upper:]' '[:lower:]'`` and ``tr '-' '_'`` chain used
    in generate-licenses.sh to build the runtime-closure set for intersection
    with pip-licenses output.

    Args:
        name: A raw package name, e.g. "PyYAML", "python-dateutil".

    Returns:
        Lowercased name with hyphens replaced by underscores.
    """
    return name.lower().replace("-", "_")


# ---------------------------------------------------------------------------
# runtime_closure_from_uv_export
# ---------------------------------------------------------------------------

def runtime_closure_from_uv_export(text: str) -> "set[str]":
    """Parse the output of ``uv export --no-dev --no-hashes --no-emit-project``
    and return the set of normalized runtime package names.

    Replicates the pipeline in generate-licenses.sh lines 215-222:
        grep -E '^[a-zA-Z]' | awk -F '==' '{print $1}' | tr -d '\\r'
        | tr '[:upper:]' '[:lower:]' | tr '-' '_' | sort -u

    Only named requirements are kept. Both version pins and PEP 508 direct
    URLs contribute their distribution name, without extras or markers.

    Args:
        text: The full text output from ``uv export``.

    Returns:
        A set of normalized package names (no duplicates).
    """
    result: set[str] = set()
    for line in text.splitlines():
        # grep -E '^[a-zA-Z]' -- only lines starting with an ASCII letter
        if not line or not line[0].isascii() or not line[0].isalpha():
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?\s*(?:==|@)", line)
        if match:
            result.add(normalize_pkg_name(match.group(1)))
    return result


# ---------------------------------------------------------------------------
# split_npm_key
# ---------------------------------------------------------------------------

def split_npm_key(key: str) -> Tuple[str, str]:
    """Split a license-checker key of the form ``name@version`` into
    ``(name, version)``, correctly handling scoped npm packages.

    A scoped package starts with ``@`` (e.g. ``@scope/name@1.2.3``).
    A plain package is e.g. ``lodash@4.17.21``.

    Matches the bash logic in generate-licenses.sh lines 485-494:

    .. code-block:: bash

        if [[ "$key" == @* ]]; then
            rest="${key#@}"
            name="@${rest%@*}"
            version="${rest##*@}"
        else
            name="${key%@*}"
            version="${key##*@}"
        fi

    Args:
        key: A license-checker JSON key, e.g. "lodash@4.17.21" or
             "@radix-ui/react-dialog@1.0.5".

    Returns:
        A tuple of (name, version).
    """
    if key.startswith("@"):
        # Scoped: strip leading '@', split on the LAST '@', re-prepend '@'.
        rest = key[1:]  # "${key#@}"
        at_idx = rest.rfind("@")
        if at_idx == -1:
            # Malformed -- no version separator; return key as-is with empty version.
            return (key, "")
        name = "@" + rest[:at_idx]   # "@${rest%@*}" -- remove the LAST @...
        version = rest[at_idx + 1:]  # "${rest##*@}" -- everything after last @
        return (name, version)
    else:
        # Plain: split on the LAST '@'.
        at_idx = key.rfind("@")
        if at_idx == -1:
            return (key, "")
        name = key[:at_idx]
        version = key[at_idx + 1:]
        return (name, version)


# ---------------------------------------------------------------------------
# obligation_for_flag
# ---------------------------------------------------------------------------

def obligation_for_flag(flag: str) -> str:
    """Return the human-readable obligation string for a flag label.

    Matches the case block in generate-licenses.sh lines 1077-1085.
    Note: no em dashes per project style (periods/commas instead).

    Args:
        flag: One of "GPL", "AGPL", "LGPL", "MPL", "UNKNOWN", "Custom",
              or any other string.

    Returns:
        A short obligation description string.
    """
    if flag == "GPL":
        return (
            "Strong copyleft. Cannot ship statically linked in a proprietary "
            "binary. Review before release."
        )
    if flag == "AGPL":
        return (
            "Network copyleft. Cannot use in a proprietary SaaS or bundled app "
            "without triggering source release. Remove."
        )
    if flag == "LGPL":
        return (
            "Weak copyleft. Provide the applicable source, notices and a compliant "
            "relinking/recombination route with usable application inputs or an "
            "eligible shared-library mechanism. Verify recipient modification rights."
        )
    if flag == "MPL":
        return (
            "File-level copyleft. Executable distribution requires access to covered "
            "preferred-form source and recipient directions, including unmodified "
            "covered files. Preserve notices and review the actual conveyed files."
        )
    if flag == "UNKNOWN":
        return (
            "Unknown or missing license metadata. Investigate upstream before shipping."
        )
    if flag == "UNVERIFIED":
        return "Redistribution grant is unverified. Establish the applicable rights before release."
    if flag == "Custom":
        return (
            "Custom license terms. Read the full text and confirm redistribution "
            "is permitted."
        )
    return f"Review obligations for {flag}."


# ---------------------------------------------------------------------------
# md_escape
# ---------------------------------------------------------------------------

def md_escape(s: str) -> str:
    """Escape pipe characters so they do not break Markdown tables.

    Matches the bash function in generate-licenses.sh lines 1030-1033:

    .. code-block:: bash

        md_escape() {
            printf '%s' "$1" | sed 's/|/\\\\|/g'
        }

    Args:
        s: Any string that may contain pipe characters.

    Returns:
        The input with every ``|`` replaced by ``\\|``.
    """
    return s.replace("|", r"\|")


# ---------------------------------------------------------------------------
# unsentinel
# ---------------------------------------------------------------------------

def unsentinel(s: str) -> str:
    """Convert the ``-`` sentinel value back to an empty string for display.

    In the TSV rows file, empty fields are stored as ``"-"`` to avoid blank
    tab-separated fields. This mirrors the bash function in
    generate-licenses.sh lines 136-143.

    Args:
        s: A field value, possibly the ``"-"`` sentinel.

    Returns:
        ``""`` if s is ``"-"``, otherwise s unchanged.
    """
    if s == "-":
        return ""
    return s


# ---------------------------------------------------------------------------
# Manual override data
# ---------------------------------------------------------------------------

# Python packages whose PyPI metadata is missing or wrong. Keyed by the
# normalized package name (lowercase, dashes -> underscores).
# Each entry has "license" (display string) and "ltext" (the note text that
# appears in the THIRD_PARTY_LICENSES.txt body for that package).
MANUAL_PYTHON_OVERRIDES: "dict[str, dict[str, str]]" = {
    "pycaw": {
        "license": "MIT License",
        "ltext": (
            "(Manually overridden: pycaw's PyPI metadata is missing a license field "
            "but the upstream repository at https://github.com/AndreMiras/pycaw "
            "ships under the MIT License.)"
        ),
    },
}

# Rust crates whose license metadata needs a manual election. Keyed by the
# crate name exactly as reported by cargo-about / cargo-license.
# Value is the elected license string that replaces the upstream metadata.
# Important: do NOT include a copyleft substring in the elected string or
# classify_flag will re-flag it as copyleft.
MANUAL_RUST_OVERRIDES: "dict[str, str]" = {
    # Upstream tri-license: Apache-2.0 OR LGPL-2.1-or-later OR MIT. Elect MIT.
    "r-efi": "MIT (elected from upstream tri-license)",
}
