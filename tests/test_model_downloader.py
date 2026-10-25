"""Tests for the unified registry-driven downloader in model_downloader.py.

Covers the registry shape and the cross-model invariants of ``download()``:
per-file pending idempotency, verified pinned delivery, terminal cancel, the
per-spec post_download hook (Parakeet bpe only), and the Qwen manual-fallback
hint on total failure.
"""

from __future__ import annotations

import hashlib
import io
import logging
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import whisperstream.model_downloader as model_downloader

from whisperstream.model_downloader import (
    DOWNLOAD_REGISTRY,
    REQUIRED_FILES,
    CumulativeProgress,
    DownloadCancelled,
    ModelId,
    ModelSpec,
    delete_unused_stt_models,
    download,
    download_archive,
    download_file_with_sha256,
    generate_bpe_vocab,
    is_present,
    model_dir,
    model_id_for_parakeet,
    stt_artifact_inventory,
)
from whisperstream.qwen_models import get_qwen_model


@pytest.mark.parametrize("model", [
    ModelId.PARAKEET_V2_INT8, ModelId.PARAKEET_V3_INT8,
    ModelId.QWEN3_ASR_06B_INT8, ModelId.COHERE_TRANSCRIBE_INT8,
    ModelId.GEMMA_4_E2B_Q4, ModelId.STREAM_1_MINI_Q4,
])
def test_manual_restore_skips_network_at_custom_storage(tmp_path, monkeypatch, model):
    """A copied bundle needs neither a download receipt nor a reachable host."""
    data = tmp_path / "data"
    storage = tmp_path / "custom models"
    data.mkdir()
    (data / "config.toml").write_text(
        f'model_storage_dir = "{storage.as_posix()}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(data))
    spec = DOWNLOAD_REGISTRY[model]
    destination = storage / ("local-llm" if spec.kind == "gguf" else "models")
    if spec.kind != "gguf":
        destination /= spec.dir_name
    for name in spec.files:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"token 0\n")
    with patch.object(model_downloader, "new_http_client", side_effect=AssertionError("network")):
        if spec.kind == "gguf":
            from whisperstream.enhancement_models import get_local_model, resolve_local_model_path
            from whisperstream.setup import download_llm_model
            entry = get_local_model(model.value)
            assert entry["artifact_available"]
            assert resolve_local_model_path(entry) == destination / spec.files[0]
            download_llm_model(model.value, destination / spec.files[0])
        else:
            assert model_dir(model) == destination
            assert is_present(model)
            download(model, destination)


def test_directory_cannot_satisfy_required_model_file(tmp_path):
    target = tmp_path / "encoder.int8.onnx"
    target.mkdir()
    assert model_downloader._compute_pending(tmp_path, (target.name,)) == [target.name]


# ---------------------------------------------------------------------------
# Pinned production delivery contract
# ---------------------------------------------------------------------------


_PRODUCTION_MANIFEST_MODELS = (
    ModelId.PARAKEET_V3_INT8,
    ModelId.PARAKEET_V2_INT8,
    ModelId.QWEN3_ASR_06B_INT8,
    ModelId.COHERE_TRANSCRIBE_INT8,
    ModelId.GEMMA_4_E2B_Q4,
)
_PRODUCTION_ARCHIVE_MODELS = (
    ModelId.SUPERTONIC_3_INT8,
    ModelId.KOKORO_82M_FP32,
)
_PRODUCTION_MODELS = _PRODUCTION_MANIFEST_MODELS + _PRODUCTION_ARCHIVE_MODELS
_HF_ORIGIN = "https://huggingface.co/"






















def test_integrity_failure_is_terminal_and_atomic(tmp_path: Path) -> None:
    """Corrupt bytes never replace the destination or leave scratch data."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.iter_bytes = MagicMock(return_value=iter([b"corrupt payload"]))
    stream_context = MagicMock()
    stream_context.__enter__ = MagicMock(return_value=response)
    stream_context.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.stream = MagicMock(return_value=stream_context)
    dest = tmp_path / "model.bin"

    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        download_file_with_sha256(
            client,
            f"{_HF_ORIGIN}immutable/model.bin",
            dest,
            hashlib.sha256(b"expected payload").hexdigest(),
            on_chunk=lambda _size: None,
        )

    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".tmp").exists()


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_registry_has_four_stt_specs() -> None:
    """The registry carries exactly 4 STT models, kind-filtered from the GGUFs."""
    stt_ids = {mid for mid, spec in DOWNLOAD_REGISTRY.items() if spec.kind == "stt"}
    assert stt_ids == {
        ModelId.PARAKEET_V3_INT8,
        ModelId.PARAKEET_V2_INT8,
        ModelId.QWEN3_ASR_06B_INT8,
        ModelId.COHERE_TRANSCRIBE_INT8,
    }
    for mid, spec in DOWNLOAD_REGISTRY.items():
        assert isinstance(spec, ModelSpec)
        assert spec.id == mid.value




def test_parakeet_specs_use_required_files_and_bpe_hook() -> None:
    v3 = DOWNLOAD_REGISTRY[ModelId.PARAKEET_V3_INT8]
    v2 = DOWNLOAD_REGISTRY[ModelId.PARAKEET_V2_INT8]

    # Both Parakeet variants share the transducer's REQUIRED_FILES.
    assert v3.files == REQUIRED_FILES
    assert v2.files == REQUIRED_FILES
    # bpe.vocab post-hook, no manual fallback hint.
    for spec in (v2, v3):
        assert spec.post_download is generate_bpe_vocab
        assert spec.manual_fallback_hint is None


def test_qwen_spec_is_derived_from_qwen_models_catalog() -> None:
    """The Qwen spec mirrors qwen_models.get_qwen_model (one source of truth)."""
    spec = DOWNLOAD_REGISTRY[ModelId.QWEN3_ASR_06B_INT8]
    entry = get_qwen_model("qwen3-asr-0.6b-int8")
    assert entry is not None

    assert spec.id == "qwen3-asr-0.6b-int8"
    assert spec.files == tuple(entry["files"])
    assert spec.post_download is None  # no bpe.vocab for Qwen
    assert spec.manual_fallback_hint is not None
    assert "tar.bz2" in spec.manual_fallback_hint


def test_registry_dir_names_pin_on_disk_layout() -> None:
    """dir_name is the single destination source; Parakeet UNCHANGED, Qwen moved."""
    from whisperstream.model_downloader import model_dir

    reg = DOWNLOAD_REGISTRY
    # Parakeet dir_names MUST equal the historical config_manager.model_dir
    # output (slug + -int8) so installed bundles are not re-downloaded.
    assert reg[ModelId.PARAKEET_V3_INT8].dir_name == "nemo-parakeet-tdt-0.6b-v3-int8"
    assert reg[ModelId.PARAKEET_V2_INT8].dir_name == "nemo-parakeet-tdt-0.6b-v2-int8"
    # Qwen NEW location under models/ (was top-level qwen3-asr/).
    assert reg[ModelId.QWEN3_ASR_06B_INT8].dir_name == "qwen3-asr-0.6b-int8"

    # model_dir(id) == get_models_root() / dir_name for every model.
    from whisperstream.config_manager import get_models_root

    root = get_models_root()
    for mid, spec in reg.items():
        assert model_dir(mid) == root / spec.dir_name


def test_model_id_for_parakeet_maps_v2_and_v3() -> None:
    assert model_id_for_parakeet("nemo-parakeet-tdt-0.6b-v2") is ModelId.PARAKEET_V2_INT8
    assert model_id_for_parakeet("nemo-parakeet-tdt-0.6b-v3") is ModelId.PARAKEET_V3_INT8
    # Unknown names default to v3.
    assert model_id_for_parakeet("something-else") is ModelId.PARAKEET_V3_INT8


# ---------------------------------------------------------------------------
# delete_model(): the single STT delete path
# ---------------------------------------------------------------------------


def test_delete_model_rmtrees_the_registry_dir(tmp_path: Path) -> None:
    """delete_model removes exactly model_dir(model), with its nested contents."""
    from whisperstream.model_downloader import delete_model

    target = tmp_path / "qwen3-asr-0.6b-int8"
    (target / "tokenizer").mkdir(parents=True)
    (target / "encoder.int8.onnx").write_bytes(b"stub")
    (target / "tokenizer" / "vocab.json").write_bytes(b"stub")

    with patch(
        "whisperstream.model_downloader.model_dir", return_value=target
    ):
        delete_model(ModelId.QWEN3_ASR_06B_INT8)

    assert not target.exists()


def test_delete_model_is_idempotent_when_absent(tmp_path: Path) -> None:
    """Deleting a model whose dir is absent is a no-op (no error)."""
    from whisperstream.model_downloader import delete_model

    missing = tmp_path / "not-there"
    with patch(
        "whisperstream.model_downloader.model_dir", return_value=missing
    ):
        # Must not raise.
        delete_model(ModelId.PARAKEET_V3_INT8)

    assert not missing.exists()


@pytest.mark.parametrize(
    "model",
    [ModelId.GEMMA_4_E2B_Q4, ModelId.SUPERTONIC_3_INT8],
)
def test_delete_model_rejects_non_stt_registry_rows(model: ModelId) -> None:
    """The STT deletion primitive cannot be used against GGUF or TTS rows."""
    from whisperstream.model_downloader import delete_model

    with pytest.raises(ValueError, match="registered STT model"):
        delete_model(model)


# ---------------------------------------------------------------------------
# download(): per-file pending idempotency
# ---------------------------------------------------------------------------


def test_present_files_are_skipped_only_pending_downloaded(tmp_path: Path) -> None:
    """A present, non-empty file is skipped; only the missing ones are fetched."""
    # Pre-create one Parakeet file as non-empty.
    present = REQUIRED_FILES[0]
    (tmp_path / present).write_bytes(b"cached")

    pending_seen: list[list[str]] = []

    def fake_fetch(spec, dest_dir, pending, progress_callback, cancel_event, logger):
        pending_seen.append(list(pending))

    with (
        patch("whisperstream.model_downloader._download_from_hf", side_effect=fake_fetch),
        patch("whisperstream.model_downloader.generate_bpe_vocab"),
    ):
        download(ModelId.PARAKEET_V3_INT8, tmp_path)

    assert pending_seen, "_download_from_hf was never called"
    fetched = pending_seen[0]
    assert present not in fetched, "an already-present file was re-downloaded"
    assert set(fetched) == set(REQUIRED_FILES[1:])


def test_all_present_returns_without_downloading(tmp_path: Path) -> None:
    """When every file is present, no download request or post hook runs."""
    for rel in REQUIRED_FILES:
        (tmp_path / rel).write_bytes(b"cached")

    with (
        patch("whisperstream.model_downloader._download_from_hf") as mock_fetch,
        patch("whisperstream.model_downloader.generate_bpe_vocab") as mock_bpe,
    ):
        download(ModelId.PARAKEET_V3_INT8, tmp_path)

    mock_fetch.assert_not_called()
    mock_bpe.assert_not_called()


# ---------------------------------------------------------------------------
# download(): GGUF specs + misconfiguration guard
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# download(): post_download hook + manual fallback hint
# ---------------------------------------------------------------------------


def test_post_download_runs_for_parakeet_only(tmp_path: Path) -> None:
    """generate_bpe_vocab runs for Parakeet (post_download set) and not Qwen."""

    def fake_fetch(spec, dest_dir, pending, progress_callback, cancel_event, logger):
        # Land a real tokens.txt so the Parakeet bpe hook has input.
        (dest_dir / "tokens.txt").write_text("<blk> 0\nhi 1\n", encoding="utf-8")

    # Parakeet: bpe.vocab produced.
    pdir = tmp_path / "parakeet"
    with patch("whisperstream.model_downloader._download_from_hf", side_effect=fake_fetch):
        download(ModelId.PARAKEET_V3_INT8, pdir)
    assert (pdir / "bpe.vocab").exists()

    # Qwen: post_download is None, so no bpe.vocab even with a tokens.txt present.
    qdir = tmp_path / "qwen"
    with patch("whisperstream.model_downloader._download_from_hf", side_effect=fake_fetch):
        download(ModelId.QWEN3_ASR_06B_INT8, qdir)
    assert not (qdir / "bpe.vocab").exists()






# ---------------------------------------------------------------------------
# CumulativeProgress.probe — grand-total computation
#
# Regression: a server may omit Content-Length for one file. The old
# all-or-nothing rule turned the whole multi-GB total into None on that one
# miss, which left the download progress bar stuck at 0% for the entire
# download.
# ---------------------------------------------------------------------------


def test_probe_sums_known_sizes_when_one_file_lacks_content_length() -> None:
    """A single unknown Content-Length must NOT poison the whole total."""
    # 3rd file (the merges.txt analogue) probes as None; the rest are known.
    sizes = [900_000_000, 80_000_000, None, 2_000_000]
    with patch(
        "whisperstream.model_downloader.probe_content_length",
        side_effect=sizes,
    ):
        cp = CumulativeProgress.probe(
            None, ["big", "mid", "merges", "small"], logger=logging.getLogger("t")
        )
    assert cp.total_bytes == 982_000_000  # sum of the three known sizes


def test_probe_total_is_none_only_when_every_size_is_unknown() -> None:
    """Total is indeterminate (None) only when NO file size is known."""
    with patch(
        "whisperstream.model_downloader.probe_content_length",
        side_effect=[None, None],
    ):
        cp = CumulativeProgress.probe(None, ["a", "b"], logger=logging.getLogger("t"))
    assert cp.total_bytes is None


def test_probe_on_chunk_accumulates_against_the_known_total() -> None:
    """on_chunk reports cumulative (downloaded, total); the unknown file's bytes
    can push downloaded past the total (the frontend clamps the fraction)."""
    reports: list[tuple[int, int | None]] = []
    with patch(
        "whisperstream.model_downloader.probe_content_length",
        side_effect=[10, None],
    ):
        cp = CumulativeProgress.probe(
            None,
            ["known", "unknown"],
            logger=logging.getLogger("t"),
            progress_callback=lambda d, t: reports.append((d, t)),
        )
    cp.on_chunk(4)
    cp.on_chunk(6)
    cp.on_chunk(5)  # the unknown file's bytes carry downloaded past total
    assert reports == [(4, 10), (10, 10), (15, 10)]


# ---------------------------------------------------------------------------
# delete_unused_stt_models -- explicit protected-artifact cleanup
# ---------------------------------------------------------------------------


def test_delete_unused_stt_models_keeps_only_protected(tmp_path: Path) -> None:
    """Every registered STT model dir is removed except protected IDs."""
    stt_ids = [mid for mid, spec in DOWNLOAD_REGISTRY.items() if spec.kind == "stt"]
    with patch("whisperstream.config_manager.get_models_root", return_value=tmp_path):
        for mid in stt_ids:
            d = model_dir(mid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "stub.bin").write_bytes(b"x")
        active = ModelId.PARAKEET_V3_INT8
        removed = delete_unused_stt_models({active})
        assert set(removed) == set(stt_ids) - {active}
        assert model_dir(active).exists()
        for mid in stt_ids:
            if mid != active:
                assert not model_dir(mid).exists()


def test_delete_unused_stt_models_noops_when_only_protected_present(
    tmp_path: Path,
) -> None:
    """Only a protected model on disk means nothing is removed."""
    with patch("whisperstream.config_manager.get_models_root", return_value=tmp_path):
        active = ModelId.QWEN3_ASR_06B_INT8
        d = model_dir(active)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stub.bin").write_bytes(b"x")
        removed = delete_unused_stt_models({active})
        assert removed == []
        assert model_dir(active).exists()


def test_delete_unused_stt_models_protects_configured_and_loaded_exact_ids(
    tmp_path: Path,
) -> None:
    """Bulk cleanup removes partial inactive STT dirs and both protected IDs survive."""
    with patch("whisperstream.config_manager.get_models_root", return_value=tmp_path):
        for model, spec in DOWNLOAD_REGISTRY.items():
            target = model_dir(model)
            if spec.kind == "gguf":
                target.mkdir(parents=True, exist_ok=True)
                (target / "keep.gguf").write_bytes(b"gguf")
            elif spec.kind == "tts":
                target.mkdir(parents=True, exist_ok=True)
                (target / "keep.onnx").write_bytes(b"tts")
            else:
                target.mkdir(parents=True, exist_ok=True)
                (target / "partial.bin").write_bytes(b"stt")

        removed = delete_unused_stt_models(
            {
                ModelId.PARAKEET_V2_INT8,
                ModelId.PARAKEET_V3_INT8,
            }
        )

        assert removed == [
            ModelId.QWEN3_ASR_06B_INT8,
            ModelId.COHERE_TRANSCRIBE_INT8,
        ]
        assert model_dir(ModelId.PARAKEET_V2_INT8).exists()
        assert model_dir(ModelId.PARAKEET_V3_INT8).exists()
        assert not model_dir(ModelId.QWEN3_ASR_06B_INT8).exists()
        assert (model_dir(ModelId.GEMMA_4_E2B_Q4) / "keep.gguf").exists()
        assert (model_dir(ModelId.SUPERTONIC_3_INT8) / "keep.onnx").exists()


def test_delete_unused_stt_models_rejects_invalid_protected_id() -> None:
    """Protection accepts exact registered STT IDs only."""
    with pytest.raises(ValueError, match="registered STT model"):
        delete_unused_stt_models({ModelId.GEMMA_4_E2B_Q4})


# ---------------------------------------------------------------------------
# stt_artifact_inventory -- exact retained-artifact storage status
# ---------------------------------------------------------------------------


def test_stt_artifact_inventory_reports_every_exact_artifact_and_real_bytes(
    tmp_path: Path,
) -> None:
    """Complete and partial bundles are distinct while every byte is counted."""
    with patch("whisperstream.config_manager.get_models_root", return_value=tmp_path):
        v3_dir = model_dir(ModelId.PARAKEET_V3_INT8)
        for index, name in enumerate(REQUIRED_FILES, start=1):
            target = v3_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" * index)

        # A partial v2 bundle consumes disk but must not be reported downloaded.
        v2_dir = model_dir(ModelId.PARAKEET_V2_INT8)
        v2_partial = v2_dir / REQUIRED_FILES[0]
        v2_partial.parent.mkdir(parents=True, exist_ok=True)
        v2_partial.write_bytes(b"partial")

        qwen_dir = model_dir(ModelId.QWEN3_ASR_06B_INT8)
        qwen_spec = DOWNLOAD_REGISTRY[ModelId.QWEN3_ASR_06B_INT8]
        for name in qwen_spec.files:
            target = qwen_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"qq")

        status = stt_artifact_inventory(
            configured_artifact_id=ModelId.PARAKEET_V3_INT8.value,
            loaded_artifact_id=ModelId.QWEN3_ASR_06B_INT8.value,
        )

    assert status["configured_artifact_id"] == ModelId.PARAKEET_V3_INT8.value
    assert status["loaded_artifact_id"] == ModelId.QWEN3_ASR_06B_INT8.value
    artifacts = {row["id"]: row for row in status["artifacts"]}
    assert set(artifacts) == {
        ModelId.PARAKEET_V3_INT8.value,
        ModelId.PARAKEET_V2_INT8.value,
        ModelId.QWEN3_ASR_06B_INT8.value,
        ModelId.COHERE_TRANSCRIBE_INT8.value,
    }
    assert artifacts[ModelId.PARAKEET_V3_INT8.value] == {
        "id": ModelId.PARAKEET_V3_INT8.value,
        "backend": "parakeet",
        "downloaded": True,
        "active": False,
        "size_bytes": sum(range(1, len(REQUIRED_FILES) + 1)),
    }
    assert artifacts[ModelId.PARAKEET_V2_INT8.value] == {
        "id": ModelId.PARAKEET_V2_INT8.value,
        "backend": "parakeet",
        "downloaded": False,
        "active": False,
        "size_bytes": len(b"partial"),
    }
    assert artifacts[ModelId.QWEN3_ASR_06B_INT8.value] == {
        "id": ModelId.QWEN3_ASR_06B_INT8.value,
        "backend": "qwen",
        "downloaded": True,
        "active": True,
        "size_bytes": len(qwen_spec.files) * 2,
    }
    # Both configured and loaded artifacts are protected. The partial inactive
    # v2 directory is reclaimable even though it is not a complete download.
    assert status["unused_size_bytes"] == len(b"partial")


def test_stt_artifact_inventory_ignores_non_stt_registry_entries(tmp_path: Path) -> None:
    """GGUF and TTS registry rows never leak into the STT status contract."""
    with patch("whisperstream.config_manager.get_models_root", return_value=tmp_path):
        status = stt_artifact_inventory(
            configured_artifact_id=ModelId.PARAKEET_V2_INT8,
            loaded_artifact_id=None,
        )

    assert all(
        DOWNLOAD_REGISTRY[ModelId(row["id"])].kind == "stt"
        for row in status["artifacts"]
    )
    assert status["loaded_artifact_id"] is None


# ---------------------------------------------------------------------------
# local_gguf_available — hide-until-published availability gate
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# TTS voice model (kind="tts", archive-distributed) -- agent-speech Task 1.2.
#
# Supertonic 3 and Kokoro each use exact, checksum-pinned upstream files.
# ---------------------------------------------------------------------------

_SUPERTONIC_ROOT = "supertonic-3-int8-2026-05-11"


def _make_supertonic_archive(
    *,
    root: str = _SUPERTONIC_ROOT,
    extra: dict[str, bytes] | None = None,
) -> bytes:
    """Build an in-memory .tar.bz2 mirroring the real Supertonic bundle shape."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tf:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name=f"{root}/{name}" if root else name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        add("duration_predictor.int8.onnx", b"fake-duration-bytes")
        add("text_encoder.int8.onnx", b"fake-text-encoder-bytes")
        add("vector_estimator.int8.onnx", b"fake-vector-estimator-bytes")
        add("vocoder.int8.onnx", b"fake-vocoder-bytes")
        add("tts.json", b"{}")
        add("unicode_indexer.bin", b"fake-indexer-bytes")
        add("voice.bin", b"fake-voice-bytes")
        for name, data in (extra or {}).items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _fake_archive_http_client(payload: bytes, *, chunks=None) -> MagicMock:  # noqa: ANN001
    """A MagicMock standing in for ``new_http_client()``'s context-managed client.

    Supports the ``head`` probe (``probe_content_length``) and ``stream`` (used by
    ``download_file_with_sha256``). ``chunks`` lets a test control exactly how the
    payload is split across ``iter_bytes`` iterations (for mid-stream cancellation).
    """
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    head_resp = MagicMock()
    head_resp.raise_for_status = MagicMock()
    head_resp.headers = {"Content-Length": str(len(payload))}
    client.head = MagicMock(return_value=head_resp)

    body_chunks = chunks if chunks is not None else [payload]

    def fake_stream(method, url, *args, **kwargs):  # noqa: ANN001
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.iter_bytes = MagicMock(return_value=iter(body_chunks))
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=resp)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    client.stream = MagicMock(side_effect=fake_stream)
    return client




def test_tts_specs_are_not_swept_by_stt_only_registry_count() -> None:
    """kind='tts' must NOT join the kind='stt' set counted elsewhere."""
    stt_ids = {mid for mid, spec in DOWNLOAD_REGISTRY.items() if spec.kind == "stt"}
    assert ModelId.SUPERTONIC_3_INT8 not in stt_ids
    assert ModelId.KOKORO_82M_FP32 not in stt_ids












def test_delete_unused_stt_models_does_not_remove_the_tts_voice_dir(
    tmp_path: Path,
) -> None:
    """The STT-only sweep must never touch the kind='tts' voice model directory."""
    with patch("whisperstream.config_manager.get_models_root", return_value=tmp_path):
        stt_ids = [mid for mid, spec in DOWNLOAD_REGISTRY.items() if spec.kind == "stt"]
        for mid in stt_ids:
            d = model_dir(mid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "stub.bin").write_bytes(b"x")

        tts_dir = model_dir(ModelId.SUPERTONIC_3_INT8)
        tts_dir.mkdir(parents=True, exist_ok=True)
        (tts_dir / "duration_predictor.int8.onnx").write_bytes(b"x")

        active = ModelId.PARAKEET_V3_INT8
        removed = delete_unused_stt_models({active})

        assert ModelId.SUPERTONIC_3_INT8 not in removed
        assert tts_dir.exists()
        assert (tts_dir / "duration_predictor.int8.onnx").exists()
