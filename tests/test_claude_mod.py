"""Run the shipped module, with deterministic host calls and event races."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_shipped_claude_module():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the Claude mod contract")
    path = Path(__file__).parent / "fixtures/claude_mod/transport.test.mjs"
    result = subprocess.run([node, "--test", str(path)], capture_output=True,
                            text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
