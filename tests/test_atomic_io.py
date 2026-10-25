"""Tests for whisperstream.atomic_io (atomic state writes)."""
import json

import pytest

from whisperstream.atomic_io import atomic_write_json, atomic_write_text
from whisperstream import atomic_io


def test_write_text_creates_file_and_parents(tmp_path):
    target = tmp_path / "sub" / "dir" / "f.txt"
    atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_write_text_overwrites_existing(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_write_text_leaves_no_tmp(tmp_path):
    target = tmp_path / "f.txt"
    atomic_write_text(target, "x")
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]


def test_failed_replace_preserves_destination_and_removes_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_text("original", encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("synthetic replacement failure")

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replacement failure"):
        atomic_write_text(target, "replacement")

    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [target]


def test_write_json_compact_roundtrips(tmp_path):
    target = tmp_path / "d.json"
    atomic_write_json(target, {"a": 1, "b": [1, 2]})
    text = target.read_text(encoding="utf-8")
    assert json.loads(text) == {"a": 1, "b": [1, 2]}
    assert "\n" not in text  # indent=None => single line


def test_write_json_indent(tmp_path):
    target = tmp_path / "d.json"
    atomic_write_json(target, {"a": 1}, indent=2)
    text = target.read_text(encoding="utf-8")
    assert "\n" in text
    assert json.loads(text) == {"a": 1}


def test_write_json_trailing_newline_matches_eula_format(tmp_path):
    # eula records are json.dumps(indent=2, ensure_ascii=True) + "\n"
    target = tmp_path / "d.json"
    atomic_write_json(target, {"a": 1}, indent=2, trailing_newline=True)
    assert target.read_text(encoding="utf-8").endswith("}\n")


def test_write_json_non_serializable_writes_nothing(tmp_path):
    target = tmp_path / "d.json"
    with pytest.raises(TypeError):
        atomic_write_json(target, {"bad": object()})
    # serialisation fails before any file/temp is created
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
