"""Thin shim over the pinned Hugging Face downloader, with no company fallback."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from .model_downloader import ModelId, download


def download_cohere_model(
    dest_dir: Path,
    *,
    progress_callback: Callable[[int, int | None], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Download the four registered Cohere files from pinned Hugging Face sources."""
    download(
        ModelId.COHERE_TRANSCRIBE_INT8,
        dest_dir,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
