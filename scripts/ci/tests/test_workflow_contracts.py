"""Keep manual dispatch, bounded runtime and CI evidence contracts intact."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("lane", ["convention", "python", "frontend", "docs"])
def test_dispatch_contract(lane):
    source = (ROOT / f".github/workflows/{lane}-check.yml").read_text()
    assert "workflow_dispatch:" in source
    assert "pull_request:" not in source
    assert "  push:" not in source
    assert "  schedule:" not in source
    assert "run-name: ${{ github.workflow }} / ${{ inputs.ci_base_sha || 'manual' }}" in source
    assert "ci_head_sha:" in source
    assert '"$EXPECTED_SHA" != "$GITHUB_SHA"' in source
    assert "timeout-minutes:" in source
    assert "cancel-in-progress: true" in source


def test_public_python_dependencies():
    source = (ROOT / ".github/workflows/python-check.yml").read_text()
    assert "uv sync --locked --extra dev --python 3.13" in source
    assert "uv run --locked python scripts/check-sherpa-source.py" in source
    assert "uv run pytest tests/ --ignore=tests/e2e/ -q -n auto" in source
    assert "secrets." not in source


def test_docs_checks_do_not_deploy():
    source = (ROOT / ".github/workflows/docs-check.yml").read_text()
    assert "npm --prefix docs/offline/toolchain run docs:check" in source
    assert "node scripts/check-offline-docs.mjs" in source
    assert "secrets." not in source
