"""Real-adapter consumer integration for the native Apple Silicon phase."""

from __future__ import annotations

import ast
import inspect
import uuid
from pathlib import Path

import pytest

from whisperstream import macos_protected_storage_probe
from whisperstream.macos_protected_storage_probe import run_probe


def test_probe_binds_explicit_enabled_state_for_access_log_calls() -> None:
    tree = ast.parse(inspect.getsource(macos_protected_storage_probe.run_probe))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "log_access"
    ]

    assert len(calls) == 3
    for call in calls:
        enabled = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "enabled"),
            None,
        )
        assert isinstance(enabled, ast.Constant)
        assert enabled.value is True


@pytest.mark.skip(
    reason=(
        "real Keychain access is manual-only; use the isolated "
        "protected-storage probe"
    ),
)
def test_real_keychain_consumers_restart_fail_closed_and_cleanup(tmp_path: Path) -> None:
    namespace = str(uuid.uuid4())
    data_dir = tmp_path / "unit04-consumer-data"
    output = tmp_path / "unit04-consumer-report.json"

    report = run_probe(
        output=output,
        test_namespace=namespace,
        data_dir=data_dir,
    )

    assert report["verdict"] == "PASS"
    assert report["cleanup_verified"] is True
    assert report["data_directory_absent_after_cleanup"] is True
    assert report["secret_material_recorded"] is False
    assert report["plaintext_scan"]["matches"] == 0
    assert not data_dir.exists()


def test_probe_requires_a_fresh_data_directory(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(RuntimeError, match="already exists"):
        run_probe(
            output=tmp_path / "unused.json",
            test_namespace=str(uuid.uuid4()),
            data_dir=existing,
        )
