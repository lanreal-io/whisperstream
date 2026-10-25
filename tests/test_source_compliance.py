"""Synthetic filesystem tests; no real release or compliance attestation."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import zipfile

import pytest


spec = importlib.util.spec_from_file_location(
    "source_compliance", Path(__file__).parents[1] / "scripts" / "source_compliance.py"
)
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def entry(source, **fields):
    return {"source": str(source), "sha256": sc.digest(source), **fields}


@pytest.fixture
def plan(tmp_path):
    artifact = write(tmp_path / "app.exe", "synthetic executable, not a release")
    evidence = write(tmp_path / "validation.json", json.dumps({
        "status": "passed", "artifact_sha256": sc.digest(artifact),
        "modified_components": ["pynput"], "commands": ["synthetic-test-only"],
        "result": "synthetic fixture", "requires_publisher_secrets": False,
    }))
    files = [
        entry(write(tmp_path / "notices.txt", "license"), path="LICENSES.txt", role="notices"),
        entry(write(tmp_path / "instructions.md", "steps"), path="BUILD.md", role="build-instructions"),
        entry(write(tmp_path / "app.c", "application"), path="app/app.c", role="application-code"),
        entry(write(tmp_path / "library.py", "library"), path="pynput/library.py", role="source", component="pynput"),
    ]
    data = {"schema_version": 1, "release_id": "synthetic-test",
            "artifact": entry(artifact), "relink_validation": entry(evidence),
            "public_files_reviewed": True,
            "components": [{"id": "pynput", "version": "test", "license": "LGPL-3.0",
                            "modified": False, "relink_required": True}], "files": files}
    validation = json.loads(evidence.read_text())
    validation["materials_sha256"] = sc.materials_hash(sc.public_materials(data))
    evidence.write_text(json.dumps(validation))
    data["relink_validation"] = entry(evidence)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def save(plan):
    path, data = plan
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_assemble_roundtrip_manifest_and_no_private_paths(plan, tmp_path):
    output = sc.assemble(plan[0], tmp_path / "materials.zip")
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("source-materials.json"))
        assert manifest["artifact_sha256"] == plan[1]["artifact"]["sha256"]
        assert str(tmp_path) not in json.dumps(manifest)
        for item in manifest["files"]:
            assert hashlib.sha256(archive.read(item["path"])).hexdigest() == item["sha256"]
        assert "validation.json" not in archive.namelist()


@pytest.mark.parametrize("name", ["../x", "/x", "C:/x", "a\\x", "a//x", "a/./x", "x.",
                                "NUL", "con.txt", "x/AUX", "a?.txt", "x/COM1.log", "bad\x01"])
def test_reject_unsafe_archive_paths(plan, name):
    plan[1]["files"][0]["path"] = name
    with pytest.raises(sc.InputError):
        sc.validate_plan(save(plan))


@pytest.mark.parametrize("name", [".env", ".env.release", "signing.key", "cert.pfx", "IP_ASSIGNMENT(signed).pdf", "capture.json"])
def test_reject_private_files(plan, name):
    plan[1]["files"][0]["path"] = name
    with pytest.raises(sc.InputError, match="Private"):
        sc.validate_plan(save(plan))


def test_reject_duplicate_casefold_names(plan):
    plan[1]["files"][1]["path"] = "licenses.TXT"
    with pytest.raises(sc.InputError, match="Duplicate"):
        sc.validate_plan(save(plan))


@pytest.mark.parametrize("name", ["LICENSES.txt/child", "source-materials.json/child", "APP"])
def test_reject_archive_prefix_collisions(plan, name):
    plan[1]["files"][1]["path"] = name
    with pytest.raises(sc.InputError, match="Duplicate"):
        sc.validate_plan(save(plan))


def test_private_metadata_not_published(plan, tmp_path):
    plan[1]["components"][0]["private_notes"] = "secret customer/local path"
    plan[1]["files"][0]["internal_token"] = "do not publish"
    output = sc.assemble(save(plan), tmp_path / "materials.zip")
    with zipfile.ZipFile(output) as archive:
        manifest = archive.read("source-materials.json")
        assert b"private_notes" not in manifest and b"internal_token" not in manifest


def test_reject_replaced_materials_with_updated_plan_but_stale_proof(plan):
    source = Path(plan[1]["files"][2]["source"])
    source.write_text("different application code")
    plan[1]["files"][2]["sha256"] = sc.digest(source)
    with pytest.raises(sc.InputError, match="exact materials"):
        sc.validate_plan(save(plan))


def test_materials_digest_before_validation_receipt_exists(plan):
    del plan[1]["relink_validation"]
    _, _, manifest = sc.validate_plan(save(plan), require_validation=False)
    assert len(manifest["materials_sha256"]) == 64
    assert manifest["validation_receipt_sha256"] is None


def test_reject_tampered_input(plan):
    Path(plan[1]["files"][0]["source"]).write_text("changed")
    with pytest.raises(sc.InputError, match="SHA256"):
        sc.validate_plan(plan[0])


def test_reject_missing_component_source(plan):
    plan[1]["files"].pop()
    with pytest.raises(sc.InputError, match="Source missing"):
        sc.validate_plan(save(plan))


def test_reject_missing_relink_coverage(plan):
    plan[1]["components"][0]["id"] = "pygame"
    plan[1]["files"][-1]["component"] = "pygame"
    with pytest.raises(sc.InputError, match="Relink validation missing"):
        sc.validate_plan(save(plan))


@pytest.mark.parametrize("change", ["stale", "failed", "secrets"])
def test_reject_bad_validation(plan, change):
    path = Path(plan[1]["relink_validation"]["source"])
    data = json.loads(path.read_text())
    if change == "stale":
        data["artifact_sha256"] = "0" * 64
    elif change == "failed":
        data["status"] = "failed"
    else:
        data["requires_publisher_secrets"] = True
    path.write_text(json.dumps(data))
    plan[1]["relink_validation"] = entry(path)
    with pytest.raises(sc.InputError, match="successful relink"):
        sc.validate_plan(save(plan))


def test_no_overwrite(plan, tmp_path):
    output = write(tmp_path / "existing.zip", "keep")
    with pytest.raises(sc.InputError, match="already exists"):
        sc.assemble(plan[0], output)
    assert output.read_text() == "keep"


@pytest.mark.parametrize("late_artifact_change", [False, True])
def test_capture_private_outputs_and_dirty_source(tmp_path, monkeypatch, late_artifact_change):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    source = write(repo / "source.py", "original")
    subprocess.run(["git", "-C", str(repo), "add", "source.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "-c", "core.hooksPath=disabled-test-hooks", "commit", "-m", "fixture"], check=True, capture_output=True)
    source.write_text("changed for synthetic build")
    build = repo / "dist" / "entry.build"
    standalone = repo / "dist" / "entry.dist"
    write(build / "module.c", "generated")
    write(standalone / "engine.exe", "standalone")
    artifact = write(repo / "dist" / "app.exe", "final")
    report = write(repo / "dist" / "compilation-report.xml", "<report/>")
    if late_artifact_change:
        original_copy = sc.copy_checked

        def copy_then_mutate(source, destination):
            result = original_copy(source, destination)
            if source == report:
                artifact.write_text("changed after artifact copy")
            return result

        monkeypatch.setattr(sc, "copy_checked", copy_then_mutate)
        with pytest.raises(sc.InputError, match="Artifact changed"):
            sc.capture(repo, artifact, tmp_path / "private", [], report)
        assert not (tmp_path / "private").exists()
        assert not list(tmp_path.glob("source-capture-*"))
        return
    output = sc.capture(repo, artifact, tmp_path / "private", [], report)
    receipt = json.loads((output / "capture.json").read_text())
    assert receipt["visibility"] == "private-do-not-publish"
    assert receipt["working_tree_dirty"] is True
    assert (output / "working-tree/source.py").read_text() == source.read_text()
    assert receipt["artifact_sha256"] == sc.digest(artifact)
    assert "nuitka/entry.build/module.c" in receipt["files"]
    assert "nuitka/entry.dist/engine.exe" in receipt["files"]


def test_capture_traversal_errors_not_silently_ignored(tmp_path, monkeypatch):
    def broken_walk(root, *, followlinks, onerror=None):
        if onerror:
            onerror(PermissionError("synthetic inaccessible directory"))
        return iter(())

    monkeypatch.setattr(sc.os, "walk", broken_walk)
    with pytest.raises(sc.InputError, match="complete build directory"):
        list(sc.tree_files(tmp_path))


def test_capture_rejects_installer_only(tmp_path):
    artifact = write(tmp_path / "app.exe", "final")
    report = write(tmp_path / "report.xml", "report")
    with pytest.raises(sc.InputError, match=".build inputs missing"):
        sc.capture(tmp_path, artifact, tmp_path / "private", [], report)


def test_capture_rejects_recursive_output(tmp_path):
    artifact = write(tmp_path / "app.exe", "final")
    report = write(tmp_path / "report.xml", "report")
    build, standalone = tmp_path / "x.build", tmp_path / "x.dist"
    build.mkdir()
    standalone.mkdir()
    with pytest.raises(sc.InputError, match="inside"):
        sc.capture(tmp_path, artifact, build / "capture", [build, standalone], report)


def test_reject_symlink_input_when_supported(plan, tmp_path):
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(Path(plan[1]["files"][0]["source"]))
    except OSError:
        pytest.skip("Symlink permission unavailable")
    plan[1]["files"][0]["source"] = str(link)
    with pytest.raises(sc.InputError, match="Linked"):
        sc.validate_plan(save(plan))
