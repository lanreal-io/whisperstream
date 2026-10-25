"""Native source checks for the retained Windows DPAPI package probe."""

from __future__ import annotations

import json
import platform
import uuid

import pytest


@pytest.mark.skipif(platform.system() != "Windows", reason="native Windows DPAPI required")
def test_windows_protected_storage_probe_uses_real_dpapi_and_cleans(tmp_path) -> None:
    from whisperstream.windows_protected_storage_probe import run_probe

    namespace = str(uuid.uuid4())
    output = tmp_path / "report.json"
    data_dir = tmp_path / "owned" / "data"

    payload = run_probe(output=output, test_namespace=namespace, data_dir=data_dir)
    persisted = json.loads(output.read_text(encoding="utf-8"))

    assert payload == persisted
    assert persisted["verdict"] == "PASS"
    assert persisted["provider"] == "windows-dpapi-current-user-v1"
    assert persisted["plaintext_scan"]["matches"] == 0
    assert persisted["transcript_restart_record_count"] == 1
    assert persisted["cleanup_verified"] is True
    assert not data_dir.exists()
    assert persisted["secret_material_recorded"] is False
