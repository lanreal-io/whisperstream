"""Regression coverage for license generation outside a package build."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_licenses as generator


def _notice(name, version, shipped):
    return (f"Package : {name}\nVersion : {version}\nLicense : MIT\n"
            f"URL     : https://example.org\nShipped : {shipped}\n")


@pytest.mark.parametrize("report", [None, '<module name="first"/>', "<empty/>"])
def test_python_shipped_evidence_and_shipping_interpreter(tmp_path, monkeypatch, capsys, report):
    monkeypatch.setattr(generator, "ROOT", tmp_path)
    (tmp_path / ".python-version").write_text("3.13\n")
    if report is not None:
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist/compilation-report.xml").write_text(report)
    # A dirty previous output must never replace committed evidence.
    (tmp_path / "THIRD_PARTY_LICENSES.txt").write_text(_notice("first", "1", "closure-only"))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["uv", "tree"]:
            output = "closure\n"
        elif argv[:2] == ["uv", "export"]:
            output = "first==1\nsecond==1\nchanged==2\nnew==1\n"
        elif argv[0] == "git":
            output = (_notice("first", "1", "yes") + _notice("second", "1", "closure-only")
                      + _notice("changed", "1", "yes"))
        else:
            output = json.dumps([{"Name": name, "Version": version, "License": "MIT"}
                                 for name, version in [("first", "1"), ("second", "1"),
                                                       ("changed", "2"), ("new", "1")]])
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(generator, "_run_capture", run)
    txt, rows = generator.TxtBuilder(), generator.Rows()
    if report == "<empty/>":
        with pytest.raises(generator.GeneratorError):
            generator._section1_python(txt, rows, tmp_path)
        return
    _, note = generator._section1_python(txt, rows, tmp_path)
    assert calls[0] == ["uv", "tree", "--locked", "--no-dev", "--python-version", "3.13"]
    assert "--locked" in calls[1]
    statuses = [line for line in txt.text().splitlines() if line.startswith("Shipped :")]
    assert statuses == ["Shipped : yes", "Shipped : closure-only"] + (
        ["Shipped : unknown"] * 2 if report is None else ["Shipped : closure-only"] * 2)
    if report is None:
        assert "retained from committed" in note
        assert "not found" in capsys.readouterr().err


def test_missing_git_evidence_is_unknown(monkeypatch):
    monkeypatch.setattr(generator, "_run_capture", lambda argv: subprocess.CompletedProcess(argv, 128, "", "no git"))
    assert generator._committed_shipped_values() == {}


def test_no_git_executable_is_unknown(monkeypatch):
    def missing(argv):
        raise FileNotFoundError("git")
    monkeypatch.setattr(generator, "_run_capture", missing)
    assert generator._committed_shipped_values() == {}


def test_commands_run_in_generator_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(generator, "ROOT", tmp_path)
    monkeypatch.setattr(generator.shutil, "which", lambda command: command)
    calls = []
    monkeypatch.setattr(generator.subprocess, "run", lambda *args, **kwargs: calls.append(kwargs))
    generator._run_capture(["uv", "tree"])
    assert calls[0]["cwd"] == str(tmp_path)


def _npm_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(generator, "ROOT", tmp_path)
    frontend = tmp_path / "frontend"
    package = frontend / "node_modules/@scope/pkg"
    package.mkdir(parents=True)
    manifest = {"dependencies": {"@scope/pkg": "1.0.0"}}
    packages = {"": manifest, "node_modules/@scope/pkg": {"version": "1.0.0"},
                "node_modules/other-platform": {"version": "1.0.0", "optional": True}}
    (frontend / "package.json").write_text(json.dumps(manifest))
    (frontend / "package-lock.json").write_text(json.dumps({"packages": packages}))
    (package / "package.json").write_text(json.dumps({"name": "@scope/pkg", "version": "1.0.0"}))
    return frontend, package


@pytest.mark.parametrize("fault", [None, "stale", "missing", "manifest"])
def test_npm_install_must_match_lock(tmp_path, monkeypatch, fault):
    frontend, package = _npm_fixture(tmp_path, monkeypatch)
    if fault == "stale":
        (package / "package.json").write_text('{"version":"0.9.0"}')
    elif fault == "missing":
        (package / "package.json").unlink()
    elif fault == "manifest":
        (frontend / "package.json").write_text('{"dependencies":{}}')
    if fault:
        with pytest.raises(generator.GeneratorError):
            generator._locked_npm_packages()
    else:
        packages = generator._locked_npm_packages()
        generator._validate_npm_inventory({"@scope/pkg@1.0.0": {"path": str(package)}}, packages)


@pytest.mark.parametrize("fault", ["extra", "version"])
def test_checker_cannot_emit_unlocked_packages(tmp_path, monkeypatch, fault):
    frontend, package = _npm_fixture(tmp_path, monkeypatch)
    packages = generator._locked_npm_packages()
    path = frontend / "node_modules/extra" if fault == "extra" else package
    key = "extra@1.0.0" if fault == "extra" else "@scope/pkg@0.9.0"
    with pytest.raises(generator.GeneratorError):
        generator._validate_npm_inventory({key: {"path": str(path)}}, packages)


def test_noop_regeneration_preserves_bytes_and_timestamp(tmp_path, monkeypatch):
    for name in ("OUT_TXT", "OUT_STAGED", "OUT_REPORT"):
        monkeypatch.setattr(generator, name, tmp_path / name)
    generator._write_outputs("Generated: old\nnotice\n", "_Generated: old_\nreport\n", "old")
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    generator._write_outputs("Generated: new\nnotice\n", "_Generated: new_\nreport\n", "new")
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    generator._write_outputs("Generated: new\nnotice\n", "_Generated: new_\nchanged\n", "new")
    assert generator.OUT_TXT.read_text().startswith("Generated: new")
    assert generator.OUT_STAGED.read_bytes() == generator.OUT_TXT.read_bytes()


def test_stale_install_aborts_before_changing_outputs(tmp_path, monkeypatch):
    _, package = _npm_fixture(tmp_path, monkeypatch)
    (package / "package.json").write_text('{"version":"0.9.0"}')
    monkeypatch.setattr(generator, "_require_tool", lambda *args: None)
    for name in ("OUT_TXT", "OUT_STAGED", "OUT_REPORT"):
        path = tmp_path / name
        path.write_text("existing evidence")
        monkeypatch.setattr(generator, name, path)
    with pytest.raises(generator.GeneratorError):
        generator.generate_licenses()
    assert all((tmp_path / name).read_text() == "existing evidence"
               for name in ("OUT_TXT", "OUT_STAGED", "OUT_REPORT"))
