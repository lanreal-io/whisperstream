"""The frozen entry point tolerates a build without the internal Mac probe."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ENTRY = Path(__file__).resolve().parents[1] / "whisperstream_entry.py"


def _load_entry():
    spec = importlib.util.spec_from_file_location("_entry_under_test", ENTRY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_phase4_probe_reports_instead_of_raising(monkeypatch, capsys) -> None:
    entry = _load_entry()
    # A None entry makes the import raise ModuleNotFoundError for that name,
    # exactly as in a checkout that omits the module.
    monkeypatch.setitem(sys.modules, "whisperstream.macos_phase4_probe", None)
    monkeypatch.setattr(sys, "argv", ["whisperstream-engine", "--macos-phase4-probe"])

    assert entry._run() == 2
    assert "verification probe" in capsys.readouterr().err


def test_child_guardian_dispatch_precedes_backend_import(monkeypatch):
    from types import SimpleNamespace
    entry = _load_entry()
    received = []
    monkeypatch.setitem(sys.modules, "whisperstream.main", None)
    monkeypatch.setitem(sys.modules, "whisperstream.macos_child_process",
                        SimpleNamespace(guardian_main=lambda args: received.append(args) or 7))
    monkeypatch.setattr(sys, "argv", ["whisperstream-engine", "--macos-child-guardian", "server", "--port", "123"])
    assert entry._run() == 7
    assert received == [["server", "--port", "123"]]
