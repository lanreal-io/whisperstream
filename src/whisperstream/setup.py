from __future__ import annotations

import logging
import re
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

from .config_manager import ConfigManager
from .enhancement_models import get_local_bundled_model
from .model_downloader import (
    DownloadCancelled,
    REQUIRED_FILES,
    DOWNLOAD_REGISTRY,
    ModelId,
    download,
    download_file_with_sha256,
    fetch_manifest,
    get_manifest_files,
    model_id_for_parakeet,
)

# Re-exported so existing callers (server.py, stt_factory, tests) can keep
# importing these from whisperstream.setup without touching their imports. The
# canonical home is model_downloader.py.
#
# - DownloadCancelled / download_file_with_sha256: sentinel + hash verification helper.
# - REQUIRED_FILES: download config.
# - fetch_manifest / get_manifest_files: retired manifest compatibility helpers.

__all__ = [
    "DownloadCancelled",
    "download_file_with_sha256",
    "download_model",
    "download_llm_model",
    "cleanup_stale_models",
    "cleanup_stale_llm_models",
    "REQUIRED_FILES",
    "fetch_manifest",
    "get_manifest_files",
]


# Pattern matching parakeet model directory names (e.g. nemo-parakeet-tdt-0.6b-v2-int8).
# Used by cleanup_stale_models() to decide which directories under models_root
# are safe to delete. We deliberately constrain to the parakeet prefix so any
# unrelated directory a user may have created is never touched. The prefix is
# also what keeps cleanup from ever matching the Qwen dir, which (since the
# qwen3-asr-0.6b-int8 path move) now lives under models/ alongside the Parakeet
# dirs: "qwen3-asr-0.6b-int8" does not start with "nemo-parakeet-tdt-0.6b-".
_PARAKEET_DIR_PATTERN = re.compile(r"^nemo-parakeet-tdt-0\.6b-.+$")


def _registered_non_parakeet_stt_dir_names() -> frozenset[str]:
    """Registry-derived dir_names of STT models that are NOT Parakeet.

    Belt-and-suspenders for cleanup_stale_models: any registered STT model that
    now lives under models/ but isn't a Parakeet variant (today only Qwen,
    ``qwen3-asr-0.6b-int8``) is explicitly protected from the stale-Parakeet
    sweep, independent of ``_PARAKEET_DIR_PATTERN``. So even if the pattern were
    ever loosened, a sibling STT bundle the registry knows about can never be
    deleted by the Parakeet cleanup. Derived from DOWNLOAD_REGISTRY so adding a
    future non-Parakeet STT model under models/ protects it automatically.

    **Filtered to ``kind == "stt"``.** The registry also carries local-LLM GGUF
    entries (``kind == "gguf"``) whose ``dir_name`` is a placeholder ("local-llm")
    -- not a real subfolder of ``models/`` -- and which are managed by an
    entirely separate download/delete path. They must never enter this
    STT-dir-name protection set.
    """
    return frozenset(
        spec.dir_name
        for mid, spec in DOWNLOAD_REGISTRY.items()
        if spec.kind == "stt"
        and mid not in (ModelId.PARAKEET_V2_INT8, ModelId.PARAKEET_V3_INT8)
    )

# Pattern matching local LLM GGUF filenames. Used by cleanup_stale_llm_models()
# to decide which files under local-llm/ are safe to delete. Only .gguf files
# are touched; any other file the user placed there is left alone.
_LLM_FILE_PATTERN = re.compile(r".+\.gguf$")


def download_model(
    model_name: str,
    model_dir: Path,
    *,
    quantization: str | None = None,
    progress_callback: Callable[[int, int | None], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Thin shim: download a Parakeet model via the unified downloader.

    Resolves *model_name* (``nemo-parakeet-tdt-0.6b-v2`` / ``...-v3``) to its
    :class:`~whisperstream.model_downloader.ModelId` and delegates to
    :func:`whisperstream.model_downloader.download`, which owns the per-file
    pending / pinned Hugging Face SHA256-verified transfer / terminal-cancel /
    cumulative-progress / ``bpe.vocab`` flow. ``quantization`` is accepted for
    call-site compatibility but the registry is int8-only today.
    """
    download(
        model_id_for_parakeet(model_name),
        model_dir,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )


def cleanup_stale_models(
    models_root: Path,
    active_model_dir: Path,
    logger: logging.Logger | None = None,
) -> list[Path]:
    """Delete any parakeet model directory under *models_root* that is not
    *active_model_dir*.

    Used after a successful model swap to reclaim disk space (each bundle is
    ~600 MB, so leaving the old one lying around is wasteful). Matching is
    restricted to directories whose name matches the parakeet naming pattern
    so that unrelated directories are never touched. Returns the list of
    paths actually removed, for logging/testing.
    """
    log = logger or logging.getLogger(__name__)
    if not models_root.exists():
        return []
    try:
        active_resolved = active_model_dir.resolve()
    except OSError:
        active_resolved = active_model_dir
    # Registry-derived guard: never touch a registered non-Parakeet STT bundle
    # that shares models/ (today only the Qwen dir). Redundant with the prefix
    # pattern, kept so the safety property survives any future pattern change.
    protected_names = _registered_non_parakeet_stt_dir_names()
    removed: list[Path] = []
    for entry in models_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in protected_names:
            continue
        if not _PARAKEET_DIR_PATTERN.match(entry.name):
            continue
        try:
            if entry.resolve() == active_resolved:
                continue
        except OSError:
            if entry == active_model_dir:
                continue
        try:
            shutil.rmtree(entry)
            log.info("Removed stale model directory: %s", entry)
            removed.append(entry)
        except OSError as exc:
            log.warning("Failed to remove stale model directory %s: %s", entry, exc)
    return removed


def download_llm_model(
    model_id: str,
    gguf_path: Path,
    *,
    progress_callback: Callable[[int, int | None], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Download a GGUF local LLM to *gguf_path*. Callable from GUI or CLI.

    Thin shim over the unified, registry-driven
    :func:`whisperstream.model_downloader.download` -- mirrors
    ``qwen_download.download_qwen_model``'s shape. Resolves *model_id* (the
    ``LOCAL_BUNDLED_MODELS`` catalog id) to its
    :class:`~whisperstream.model_downloader.ModelId` and delegates to
    ``download()`` with ``dest_dir = gguf_path.parent`` (the shared
    ``local-llm/`` directory); ``download()`` writes
    its registered GGUF filename. Gemma may install the reviewed Google artifact
    instead; callers resolve the public model path again after download.
    The unified downloader owns pinned Hugging Face delivery,
    SHA256-verified delivery, atomic writes, per-file idempotency, terminal
    cancellation, and cumulative progress reporting -- the same machinery Parakeet and Qwen
    already share.

    Args:
        model_id: The ``LOCAL_BUNDLED_MODELS`` ``id`` field (e.g.
            ``"gemma4-e2b-instruct"``).  Raises ``ValueError`` if not found.
        gguf_path: Destination path for the GGUF file (typically
            ``get_local_llm_dir() / entry["gguf_filename"]``).
        progress_callback: Called as ``(downloaded_bytes, total_bytes)`` after
            each chunk. *total_bytes* may be ``None`` if the size could not be
            determined.
        cancel_event: When set between chunks, raises ``DownloadCancelled`` and
            removes any in-progress ``.tmp`` file.
    """
    entry = get_local_bundled_model(model_id)
    if entry is None:
        raise ValueError(
            f"download_llm_model: unknown model id {model_id!r}; "
            f"must be one of the ids in LOCAL_BUNDLED_MODELS"
        )

    enum_id = ModelId(model_id)
    download(
        enum_id,
        gguf_path.parent,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )


def cleanup_stale_llm_models(
    local_llm_dir: Path,
    active_gguf: Path,
    logger: logging.Logger | None = None,
) -> list[Path]:
    """Remove every ``*.gguf`` in *local_llm_dir* that is not *active_gguf*.

    Called after a successful model swap so the user's disk doesn't silently
    accumulate multiple GGUF files (each ~500 MB - 1.3 GB). Matching is
    restricted to files whose name ends in ``.gguf`` so that any other content
    in the directory is never touched.

    Returns the list of paths actually removed, for logging/testing.
    """
    log = logger or logging.getLogger(__name__)
    if not local_llm_dir.exists():
        return []
    try:
        active_resolved = active_gguf.resolve()
    except OSError:
        active_resolved = active_gguf
    removed: list[Path] = []
    for entry in local_llm_dir.iterdir():
        if not entry.is_file():
            continue
        if not _LLM_FILE_PATTERN.match(entry.name):
            continue
        try:
            if entry.resolve() == active_resolved:
                continue
        except OSError:
            if entry == active_gguf:
                continue
        try:
            entry.unlink()
            log.info("Removed stale GGUF: %s", entry.name)
            removed.append(entry)
        except OSError as exc:
            log.warning("Failed to remove stale GGUF %s: %s", entry.name, exc)
    return removed


def main() -> None:
    from .model_downloader import is_present, model_dir as resolve_model_dir

    config_manager = ConfigManager()
    config = config_manager.load()
    parakeet_id = model_id_for_parakeet(config.parakeet_model)
    model_dir = resolve_model_dir(parakeet_id)

    logger = logging.getLogger(__name__)
    if is_present(parakeet_id):
        logger.info("model setup skipped reason=already_present path=%s", model_dir)
        return

    download_model(config.parakeet_model, model_dir, quantization=config.parakeet_quantization)
    logger.info("model setup complete path=%s", model_dir)


if __name__ == "__main__":  # pragma: no cover
    main()
