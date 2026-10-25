"""Qwen3-ASR model catalog.

This module is DATA-ONLY — it describes the available Qwen ASR model
artifacts (ids, HuggingFace mirror repos, required file paths) so that
the downloader (qwen_download.py) and the loader (qwen_manager.py) can
share a single source of truth without duplicating the file list.

No download logic lives here.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
# Shape mirrors LOCAL_BUNDLED_MODELS in enhancement_models.py:
#   "id"      — stable identifier used by callers to look up this entry
#   "hf_repo" — HuggingFace repository mirroring the ONNX bundle. Its card
#                identifies a third-party ModelScope artifact and conversion
#                repository. The export grant and immutable pin remain open.
#   "files"   — relative paths of every file that must be present for the
#                model to be loadable by the child-owned Qwen worker adapter
#
# Canonical upstream artifact (manual fallback if HF mirror is down):
#   https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models
#   sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2  (~937 MB)
# ---------------------------------------------------------------------------

QWEN_MODELS: list[dict] = [
    {
        "id": "qwen3-asr-0.6b-int8",
        # Third-party export mirror, byte-identical to the k2-fsa GitHub
        # asr-models tarball (the manual fallback in qwen_download.py). Its
        # Apache-2.0 publisher declaration is supported by the converter author:
        # https://github.com/Wasser1462/Qwen3-ASR-onnx/issues/11#issuecomment-6162677694
        # model_sources.py owns the immutable revision and per-file verification.
        # The prior pantinor/... mirror shipped a different decoder build.
        "hf_repo": "csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
        # Total ~937 MB: conv_frontend 42 MB + encoder 174 MB + decoder 721 MB
        "files": [
            "conv_frontend.onnx",
            "encoder.int8.onnx",
            "decoder.int8.onnx",
            "tokenizer/vocab.json",
            "tokenizer/merges.txt",
            "tokenizer/tokenizer_config.json",
        ],
    },
]


# ---------------------------------------------------------------------------
# Lookup helper
# ---------------------------------------------------------------------------


def get_qwen_model(model_id: str) -> dict | None:
    """Return the ``QWEN_MODELS`` entry for *model_id*, or ``None`` if unknown."""
    for entry in QWEN_MODELS:
        if entry["id"] == model_id:
            return entry
    return None
