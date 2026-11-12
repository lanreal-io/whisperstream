"""Dependency integrity and loader selection, without opening audio devices."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "prepare_macos_portaudio", Path(__file__).resolve().parents[1] / "scripts/prepare_macos_portaudio.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


def package(tmp_path, monkeypatch):
    directory = tmp_path / "package"
    (directory / "portaudio-binaries").mkdir(parents=True)
    source = module.LOADER_ORIGINAL + b"        'portaudio',): pass\nexcept OSError: pass\n"
    monkeypatch.setattr(module, "SOUNDDEVICE_SHA256", hashlib.sha256(source).hexdigest())
    (directory.parent / "sounddevice.py").write_bytes(source)
    return directory


def test_install_replaces_hardlink_without_mutating_wheel_cache(tmp_path, monkeypatch):
    directory = package(tmp_path, monkeypatch)
    cached = tmp_path / "wheel-cache"
    cached.write_bytes(b"original")
    destination = directory / "portaudio-binaries/libportaudio.dylib"
    destination.hardlink_to(cached)
    built = tmp_path / "built.dylib"
    built.write_bytes(b"fixed")
    monkeypatch.setattr(module.subprocess, "check_output", lambda command, **kw: "" if command[0] == "nm" else str(destination))
    receipt = module.install(built, directory)
    assert cached.read_bytes() == b"original"
    assert destination.read_bytes() == b"fixed"
    assert receipt["library_sha256"] == hashlib.sha256(b"fixed").hexdigest()


@pytest.mark.parametrize("fault", ["library", "revision", "base_revision", "source_tree", "build_type", "assertions", "loader", "reinstalled_loader"])
def test_verify_rejects_stale_or_bypassed_library(tmp_path, monkeypatch, fault):
    directory = package(tmp_path, monkeypatch)
    library = directory / "portaudio-binaries/libportaudio.dylib"
    library.write_bytes(b"fixed")
    loader = directory.parent / "sounddevice.py"
    original = loader.read_bytes()
    loader.write_bytes(module.prepare_loader(original))
    receipt = {**module.source_identity(), "library_sha256": module.digest(library),
               "loader_sha256": module.digest(loader)}
    if fault == "library":
        library.write_bytes(b"reinstalled-old-wheel")
    elif fault == "reinstalled_loader":
        loader.write_bytes(original)
    elif fault != "loader":
        receipt[fault] = "wrong"
    (directory / module.RECEIPT_NAME).write_text(json.dumps(receipt))
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **kw: "/opt/homebrew/lib/libportaudio.dylib")
    with pytest.raises(RuntimeError):
        module.verify(directory)


def test_loader_patch_is_idempotent_and_rejects_unknown_source(tmp_path, monkeypatch):
    directory = package(tmp_path, monkeypatch)
    source = (directory.parent / "sounddevice.py").read_bytes()
    patched = module.prepare_loader(source)
    assert module.prepare_loader(patched) == patched
    with pytest.raises(RuntimeError, match="Unsupported"):
        module.prepare_loader(source + b"changed")


@pytest.mark.parametrize("symbols, rejected", [("                 U ___assert_rtn\n", True),
                                                  ("                 U _malloc\n", False)])
def test_native_assertion_gate(monkeypatch, symbols, rejected):
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **kw: symbols)
    if rejected:
        with pytest.raises(RuntimeError, match="assertions"):
            module.assert_no_native_assertions(Path("native.dylib"))
    else:
        module.assert_no_native_assertions(Path("native.dylib"))


def test_source_tree_mismatch_stops_before_compilation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kw: calls.append(command))
    def git(command, **kwargs):
        if "rev-parse" in command:
            return module.BASE_REVISION
        if "write-tree" in command:
            return "unexpected-tree"
        return ""
    monkeypatch.setattr(module.subprocess, "check_output", git)
    with pytest.raises(RuntimeError, match="source tree"):
        module.build_library(tmp_path)
    assert all(command[0] == "git" for command in calls)


def test_release_build_ignores_caller_assertion_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("CFLAGS", "-UNDEBUG")
    monkeypatch.setenv("CPPFLAGS", "-UNDEBUG")
    monkeypatch.setattr(module, "prepare_source", lambda root: root / "source")
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kw: calls.append((command, kw)))
    monkeypatch.setattr(module.subprocess, "check_output", lambda command, **kw: "arm64" if command[0] == "lipo" else "")
    module.build_library(tmp_path)
    configure, kwargs = calls[0]
    assert "-DCMAKE_BUILD_TYPE=Release" in configure
    assert "-DCMAKE_C_FLAGS_RELEASE=-O3 -DNDEBUG" in configure
    assert "CFLAGS" not in kwargs["env"] and "CPPFLAGS" not in kwargs["env"]
