"""Shared code must never import from the edition-private ``private/`` tree."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMPORT = re.compile(r"^\s*(from|import)\s+private(\.|\s|$)", re.M)
PATH_SETUP = re.compile(r"""sys\.path\.\w+\([^)]*["']private\b""")


def test_shared_python_never_imports_private():
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "*.py"], cwd=ROOT, text=True, encoding="utf-8",
    ).split("\0")
    offenders = []
    for name in filter(None, tracked):
        if name.startswith("private/"):
            continue
        path = ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if IMPORT.search(text) or PATH_SETUP.search(text):
            offenders.append(name)
    assert offenders == []
