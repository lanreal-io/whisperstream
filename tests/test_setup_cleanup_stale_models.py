"""Tests for setup.cleanup_stale_models — the post-swap disk reclaim helper.

After a successful model swap (English <-> Multilingual), we delete the old
model bundle so the user isn't silently accumulating 600 MB per language
family. These tests cover the happy path (delete inactive siblings), the
safety guards (non-matching dirs + the active dir are preserved), and the
no-op edge cases.
"""

from __future__ import annotations

from pathlib import Path

from whisperstream.setup import cleanup_stale_models


def _make_fake_model_dir(root: Path, name: str) -> Path:
    """Create a directory that looks like a downloaded parakeet bundle."""
    d = root / name
    d.mkdir(parents=True)
    for fname in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"):
        (d / fname).write_bytes(b"stub")
    return d


def test_removes_inactive_parakeet_dir(tmp_path: Path) -> None:
    active = _make_fake_model_dir(tmp_path, "nemo-parakeet-tdt-0.6b-v2-int8")
    inactive = _make_fake_model_dir(tmp_path, "nemo-parakeet-tdt-0.6b-v3-int8")

    removed = cleanup_stale_models(tmp_path, active)

    assert active.exists()
    assert not inactive.exists()
    assert removed == [inactive]


def test_preserves_active_dir(tmp_path: Path) -> None:
    active = _make_fake_model_dir(tmp_path, "nemo-parakeet-tdt-0.6b-v3-int8")

    removed = cleanup_stale_models(tmp_path, active)

    assert active.exists()
    assert (active / "encoder.int8.onnx").exists()
    assert removed == []


def test_ignores_non_parakeet_dirs(tmp_path: Path) -> None:
    active = _make_fake_model_dir(tmp_path, "nemo-parakeet-tdt-0.6b-v2-int8")
    # Any directory that doesn't match the parakeet naming pattern must be left alone.
    unrelated = tmp_path / "whisper-large-v3"
    unrelated.mkdir()
    (unrelated / "marker.txt").write_bytes(b"keep me")

    removed = cleanup_stale_models(tmp_path, active)

    assert unrelated.exists()
    assert (unrelated / "marker.txt").exists()
    assert active.exists()
    assert removed == []


def test_noop_when_models_root_missing(tmp_path: Path) -> None:
    active = tmp_path / "models" / "nemo-parakeet-tdt-0.6b-v2-int8"
    # models_root itself doesn't exist
    removed = cleanup_stale_models(tmp_path / "does-not-exist", active)
    assert removed == []


def test_removes_multiple_stale_dirs(tmp_path: Path) -> None:
    active = _make_fake_model_dir(tmp_path, "nemo-parakeet-tdt-0.6b-v2-int8")
    stale_a = _make_fake_model_dir(tmp_path, "nemo-parakeet-tdt-0.6b-v3-int8")
    stale_b = _make_fake_model_dir(tmp_path, "nemo-parakeet-tdt-0.6b-v1-int8")

    removed = cleanup_stale_models(tmp_path, active)

    assert active.exists()
    assert not stale_a.exists()
    assert not stale_b.exists()
    assert set(removed) == {stale_a, stale_b}


def test_ignores_loose_files_at_models_root(tmp_path: Path) -> None:
    active = _make_fake_model_dir(tmp_path, "nemo-parakeet-tdt-0.6b-v2-int8")
    (tmp_path / "README.txt").write_bytes(b"hands off")

    cleanup_stale_models(tmp_path, active)

    assert (tmp_path / "README.txt").exists()
    assert active.exists()


def test_never_deletes_qwen_dir_under_models_root(tmp_path: Path) -> None:
    """SAFETY: the Qwen dir now lives under models/ alongside the Parakeet dirs.

    cleanup_stale_models must sweep only the stale Parakeet dir and leave the
    Qwen bundle (a registered non-Parakeet STT model) fully intact, even when
    Parakeet is the active backend and Qwen is therefore "inactive".
    """
    from whisperstream.model_downloader import DOWNLOAD_REGISTRY, ModelId

    qwen_name = DOWNLOAD_REGISTRY[ModelId.QWEN3_ASR_06B_INT8].dir_name
    assert qwen_name == "qwen3-asr-0.6b-int8"

    active = _make_fake_model_dir(tmp_path, "nemo-parakeet-tdt-0.6b-v3-int8")
    stale_parakeet = _make_fake_model_dir(tmp_path, "nemo-parakeet-tdt-0.6b-v2-int8")
    # A real-looking Qwen bundle (flat ONNX + internal tokenizer/ subdir).
    qwen_dir = tmp_path / qwen_name
    qwen_dir.mkdir(parents=True)
    (qwen_dir / "encoder.int8.onnx").write_bytes(b"stub")
    (qwen_dir / "tokenizer").mkdir()
    (qwen_dir / "tokenizer" / "vocab.json").write_bytes(b"stub")

    removed = cleanup_stale_models(tmp_path, active)

    assert active.exists()
    assert not stale_parakeet.exists(), "Stale Parakeet dir should have been swept"
    assert qwen_dir.exists(), "Qwen dir must NEVER be swept by the Parakeet cleanup"
    assert (qwen_dir / "tokenizer" / "vocab.json").exists()
    assert removed == [stale_parakeet]


