"""Synthetic filesystem transactions through the relocation callback boundary."""

import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.model_downloader import DownloadCancelled
from whisperstream.server.model_relocation import ModelRelocation


@pytest.fixture
def relocation(tmp_path, monkeypatch):
    models = tmp_path / "current" / "models"
    local = tmp_path / "current" / "local-llm"
    models.mkdir(parents=True)
    local.mkdir(parents=True)
    (models / "synthetic.bin").write_bytes(b"synthetic speech bytes")
    (local / "synthetic.gguf").write_bytes(b"synthetic enhancement bytes")
    monkeypatch.setattr("whisperstream.config_manager.get_models_root", lambda: models)
    monkeypatch.setattr("whisperstream.config_manager.get_local_llm_dir", lambda: local)
    monkeypatch.setattr("shutil.disk_usage", lambda _path: SimpleNamespace(free=50 * 1024**3))
    config = WhisperstreamConfig(model_storage_dir=str(tmp_path / "current"))
    save, refresh, unload, reload, broadcast = (Mock() for _ in range(5))
    operation = ModelRelocation(
        current_config=lambda: config,
        save_config=save,
        refresh_models_root=refresh,
        activation_busy=lambda: False,
        has_resident_models=lambda: False,
        unload_models=unload,
        reload_models=reload,
        broadcast=broadcast,
        logger=logging.getLogger(__name__),
    )
    monkeypatch.setattr(operation, "settle", lambda: None)
    return operation, config, models, local, save, refresh, unload, reload, broadcast


def test_relocation_moves_generated_files_and_repoints_after_copy(relocation, tmp_path):
    operation, config, models, local, save, refresh, unload, reload, broadcast = relocation
    destination = tmp_path / "destination"
    original = {
        "models/synthetic.bin": (models / "synthetic.bin").read_bytes(),
        "local-llm/synthetic.gguf": (local / "synthetic.gguf").read_bytes(),
    }
    progress = []

    def save_after_copy(saved):
        assert saved is config
        assert all((destination / name).read_bytes() == data for name, data in original.items())

    save.side_effect = save_after_copy
    operation.run(str(destination), threading.Event(), lambda done, total: progress.append((done, total)))

    assert config.model_storage_dir == str(destination)
    save.assert_called_once_with(config)
    refresh.assert_called_once_with()
    unload.assert_called_once_with()
    reload.assert_called_once_with()
    assert progress[-1] == (sum(map(len, original.values())),) * 2
    assert models.exists()
    assert local.exists()
    assert not list(destination.glob(".ws_relocate_*"))
    broadcast.assert_called_once_with({"type": "download.complete", "kind": "relocate"})


@pytest.mark.parametrize("failure", [RuntimeError, DownloadCancelled])
def test_failed_or_cancelled_copy_preserves_source_and_reloads_original(
    relocation, tmp_path, monkeypatch, failure,
):
    operation, config, models, local, save, refresh, unload, reload, broadcast = relocation
    original_location = config.model_storage_dir
    destination = tmp_path / "destination"
    original = [(path, path.read_bytes()) for tree in (models, local) for path in tree.iterdir()]

    def interrupted_copy(_source, staging, **_kwargs):
        Path(staging).mkdir(parents=True)
        (Path(staging) / "partial.bin").write_bytes(b"partial")
        raise failure("synthetic copy interruption")

    monkeypatch.setattr("whisperstream.model_downloader.copy_tree_with_progress", interrupted_copy)
    with pytest.raises(failure):
        operation.run(str(destination), threading.Event(), Mock())

    assert all(path.read_bytes() == data for path, data in original)
    assert config.model_storage_dir == original_location
    save.assert_not_called()
    refresh.assert_not_called()
    unload.assert_called_once_with()
    reload.assert_called_once_with()
    assert not (destination / "models.tmp").exists()
    assert not (destination / "local-llm.tmp").exists()
    expected = "download.cancelled" if failure is DownloadCancelled else "download.error"
    assert broadcast.call_args.args[0]["type"] == expected


def test_unowned_destination_and_overlaps_are_blocked(relocation, tmp_path):
    operation, _, models, _, *_ = relocation
    destination = tmp_path / "destination"
    (destination / "models").mkdir(parents=True)
    personal = destination / "models" / "personal.txt"
    personal.write_text("keep")
    assert operation.classify(str(destination)) == {"outcome": "blocked", "reason": "foreign"}
    assert operation.classify(str(models / "nested"))["outcome"] == "blocked"
    assert personal.read_text() == "keep"
    assert not (models / "nested").exists()


def test_old_staging_and_source_extras_survive(relocation, tmp_path):
    operation, _, models, _, *_ = relocation
    destination = tmp_path / "destination"
    old_staging = destination / "models.tmp"
    old_staging.mkdir(parents=True)
    (old_staging / "personal.txt").write_text("keep staging")
    (models / "personal.txt").write_text("keep original")
    operation.run(str(destination), threading.Event(), Mock())
    assert (old_staging / "personal.txt").read_text() == "keep staging"
    assert (models / "personal.txt").read_text() == "keep original"
    assert not list(destination.glob(".ws_relocate_*"))


def test_swap_never_recursively_deletes_final(tmp_path):
    stage, final = tmp_path / "stage", tmp_path / "final"
    stage.mkdir()
    final.mkdir()
    (final / "personal.txt").write_text("keep")
    with pytest.raises(OSError):
        ModelRelocation.swap_into_place(stage, final)
    assert (final / "personal.txt").read_text() == "keep"
