"""Thin shim over the pinned Hugging Face downloader, with no company fallback."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from .model_downloader import ModelId, download


def download_qwen_model(
    dest_dir: Path,
    *,
    progress_callback: Callable[[int, int | None], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Download all required Qwen3-ASR ONNX files into *dest_dir*.

    Thin shim over :func:`whisperstream.model_downloader.download` with
    ``ModelId.QWEN3_ASR_06B_INT8``. The model is delivered only from the Hugging Face
    manifest with per-file SHA-256 verification. If Hugging Face fails, a ``RuntimeError``
    names the GitHub ``asr-models`` tar.bz2 as a manual fallback.

    Idempotent (per-file pending: present, non-empty files are skipped) and
    cancel-terminal (``DownloadCancelled`` propagates immediately).

    Args:
        dest_dir: Directory to stage files into (typically
            ``model_dir(ModelId.QWEN3_ASR_06B_INT8)``). Created automatically
            with parents; ``tokenizer/`` is pre-created.
        progress_callback: Called as ``(downloaded_bytes, total_bytes)`` after
            each chunk. ``downloaded_bytes`` is cumulative across every pending
            file; ``total_bytes`` is the fixed SUM of all pending files' sizes
            so the reported fraction only ever climbs (``None`` if any size
            could not be probed).
        cancel_event: When set between chunks, the in-progress file is cleaned
            up and :class:`~whisperstream.model_downloader.DownloadCancelled`
            is raised.
    """
    download(
        ModelId.QWEN3_ASR_06B_INT8,
        dest_dir,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
