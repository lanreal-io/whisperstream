from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "audit-kokoro-packaged.py"
)
_SPEC = importlib.util.spec_from_file_location("audit_kokoro_packaged", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_AUDIT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_AUDIT)


def test_source_scan_rejects_forbidden_import(tmp_path: Path) -> None:
    source = tmp_path / "src"
    package = source / "whisperstream"
    package.mkdir(parents=True)
    (package / "clean.py").write_text("import numpy\n", encoding="utf-8")
    (package / "bad.py").write_text("from torch import nn\n", encoding="utf-8")

    result = _AUDIT._source_scan(package)

    assert result["status"] == "FAIL"
    assert result["forbidden_imports"] == {
        "whisperstream/bad.py": ["torch"]
    }


def test_compilation_report_requires_clean_completed_closure(
    tmp_path: Path,
) -> None:
    report = tmp_path / "compilation-report.xml"
    report.write_text(
        """<?xml version="1.0"?>
<nuitka-compilation-report completion="yes">
  <module name="whisperstream.speech.kokoro_engine" />
  <module name="onnxruntime" />
  <module name="transformers.models.bart" />
</nuitka-compilation-report>
""",
        encoding="utf-8",
    )

    result = _AUDIT._compilation_report_scan(report)

    assert result["status"] == "FAIL"
    assert result["forbidden_modules_or_distributions"] == ["transformers"]


def test_tree_scan_checks_paths_and_native_loader_markers(
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean"
    (clean / "onnxruntime" / "transformers").mkdir(parents=True)
    (clean / "runtime.pyd").write_bytes(b"ordinary native payload")
    (
        clean / "onnxruntime" / "transformers" / "optimizer.py"
    ).write_text("OPTIONAL_IMPORT = 'torch._C'\n", encoding="utf-8")
    assert _AUDIT._tree_scan(clean, "clean")["status"] == "PASS"

    bad = tmp_path / "bad"
    (bad / "nltk").mkdir(parents=True)
    (bad / "nltk" / "weights.json").write_text("{}", encoding="utf-8")
    (bad / "runtime.pyd").write_bytes(b"prefix torch_cpu.dll suffix")

    result = _AUDIT._tree_scan(bad, "bad")

    assert result["status"] == "FAIL"
    assert result["forbidden_paths"] == {"nltk/weights.json": ["nltk"]}
    assert result["forbidden_native_markers"] == {
        "runtime.pyd": ["torch_cpu.dll"]
    }
