"""Synthetic archive regressions for the pinned Windows sidecar staging boundary."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch_llama_server as fetch
import generate_licenses as licenses


def archive(root, monkeypatch, *, omit=None, extra=None):
    payload = {name: name.encode() for name in ["llama-server.exe", *fetch.REQUIRED_DLLS]}
    payload["LICENSE-LLVM-OpenMP"] = (fetch._REPO_ROOT / "scripts/license-texts/llvm-openmp.txt").read_bytes()
    if omit:
        payload.pop(omit)
    payload.update(extra or {})
    target = root / "dist/cache/llama-cpp" / fetch.LLAMA_CPP_RELEASE_TAG / fetch.LLAMA_CPP_ARTIFACT
    target.parent.mkdir(parents=True)
    with zipfile.ZipFile(target, "w") as z:
        for name, data in payload.items():
            z.writestr(name, data)
    monkeypatch.setattr(fetch, "LLAMA_CPP_SHA256", hashlib.sha256(target.read_bytes()).hexdigest())
    return payload, target


def test_stages_complete_payload_and_removes_only_obsolete_runtime(tmp_path, monkeypatch):
    payload, _ = archive(tmp_path, monkeypatch)
    dest = tmp_path / "src-tauri/binaries"
    dest.mkdir(parents=True)
    (dest / "libomp140.x86_64.dll").write_bytes(b"restricted")
    (dest / "unrelated.dll").write_bytes(b"keep")
    fetch.stage_llama_server(tmp_path)
    assert not (dest / "libomp140.x86_64.dll").exists()
    assert (dest / "unrelated.dll").read_bytes() == b"keep"
    for name, data in payload.items():
        staged = "llama-server-x86_64-pc-windows-msvc.exe" if name == "llama-server.exe" else name
        assert (dest / staged).read_bytes() == data
    fetch.stage_llama_server(tmp_path)  # Verified cache is reusable.


@pytest.mark.parametrize("missing", ["llama-server.exe", "llama-server-impl.dll", "libomp.dll", "LICENSE-LLVM-OpenMP"])
def test_missing_input_fails_before_any_staging_even_with_stale_extraction(tmp_path, monkeypatch, missing):
    _, path = archive(tmp_path, monkeypatch, omit=missing)
    extracted = path.parent / "extracted"
    extracted.mkdir()
    (extracted / missing).write_bytes(b"stale must not mask missing input")
    dest = tmp_path / "src-tauri/binaries"
    dest.mkdir(parents=True)
    (dest / "llama-server-x86_64-pc-windows-msvc.exe").write_bytes(b"original")
    with pytest.raises(SystemExit):
        fetch.stage_llama_server(tmp_path)
    assert list(dest.iterdir()) == [dest / "llama-server-x86_64-pc-windows-msvc.exe"]
    assert next(dest.iterdir()).read_bytes() == b"original"


@pytest.mark.parametrize("extra", [
    {"libomp140.x86_64.dll": b"restricted"},
    {"LICENSE-LLVM-OpenMP": b"wrong license"},
])
def test_rejects_restricted_runtime_or_wrong_notice(tmp_path, monkeypatch, extra):
    archive(tmp_path, monkeypatch, extra=extra)
    with pytest.raises(SystemExit):
        fetch.stage_llama_server(tmp_path)
    assert not (tmp_path / "src-tauri/binaries").exists()


def test_checksum_mismatch_does_not_stage(tmp_path, monkeypatch):
    archive(tmp_path, monkeypatch)
    monkeypatch.setattr(fetch, "LLAMA_CPP_SHA256", "0" * 64)
    with pytest.raises(SystemExit):
        fetch.stage_llama_server(tmp_path)
    assert not (tmp_path / "src-tauri/binaries").exists()


def test_tauri_ships_every_required_runtime_and_notice():
    config = json.loads((fetch._REPO_ROOT / "src-tauri/tauri.windows.conf.json").read_text())
    resources = config["bundle"]["resources"]
    for name in [*fetch.REQUIRED_DLLS, "LICENSE-LLVM-OpenMP"]:
        assert f"binaries/{name}" in resources
    assert "binaries/libomp140.x86_64.dll" not in resources


def test_notice_keeps_openmp_separate_from_llama_mit():
    text, rows = licenses.TxtBuilder(), licenses.Rows()
    licenses._section7_bundled(text, rows)
    output = text.text()
    notice = (fetch._REPO_ROOT / "scripts/license-texts/llvm-openmp.txt").read_text()
    assert notice in output
    assert "libomp140.x86_64.dll" not in output
    assert licenses.LLAMA_CPP_RELEASE_TAG == fetch.LLAMA_CPP_RELEASE_TAG
    assert any(r[1] == "LLVM OpenMP" and r[3] == "Apache-2.0 WITH LLVM-exception" for r in rows.all_rows())


@pytest.mark.parametrize("bad", ["empty", "duplicate"])
def test_rejects_empty_or_duplicate_required_dll(tmp_path, monkeypatch, bad):
    _, path = archive(tmp_path, monkeypatch, extra={"libomp.dll": b""} if bad == "empty" else {})
    if bad == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            with zipfile.ZipFile(path, "a") as z:
                z.writestr("libomp.dll", b"duplicate")
    monkeypatch.setattr(fetch, "LLAMA_CPP_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(SystemExit):
        fetch.stage_llama_server(tmp_path)
    assert not (tmp_path / "src-tauri/binaries").exists()


def test_vendored_openmp_notice_matches_archive_pin():
    data = (fetch._REPO_ROOT / "scripts/license-texts/llvm-openmp.txt").read_bytes()
    assert hashlib.sha256(data).hexdigest() == fetch.OPENMP_LICENSE_SHA256
