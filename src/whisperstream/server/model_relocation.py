"""Model relocation filesystem transaction and verified staging."""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from ..config_manager import WhisperstreamConfig

_RESIDENT_MODELS_RESTART_REQUIRED = "resident_models_restart_required"

# Extra headroom required on the destination drive during a relocation so that
# both the old tree (staying in place as rollback) and the new copy can coexist
# until the old tree is deleted.  3.5 GB covers a full Parakeet + GGUF bundle.
RELOCATE_HEADROOM_BYTES = int(3.5 * 1024 ** 3)

# Seconds the relocation worker pauses after unloading the models before it
# touches their files. On Windows the ONNX mmap / llama-server handle release
# can lag the gc.collect()/supervisor.stop() calls, so a brief settle absorbs
# that lag before the copy + atomic swap (Task 2.3 step 2).
_RELOCATE_SETTLE_SECONDS = 1.5


def _has_links(path: Path, *, recursive: bool = False) -> bool:
    """Do not traverse a symlink or Windows junction during relocation."""
    def linked(entry: Path) -> bool:
        return entry.is_symlink() or entry.is_junction()
    if any(linked(entry) for entry in (path, *path.parents)):
        return True
    pending = [path] if recursive and path.is_dir() else []
    while pending:
        directory = pending.pop()
        for entry in directory.iterdir():
            if linked(entry):
                return True
            if entry.is_dir():
                pending.append(entry)
    return False


class ModelRelocation:
    """Classify, stage, verify, swap and reload one admitted relocation."""

    def __init__(
        self, *,
        current_config: Callable[[], WhisperstreamConfig],
        save_config: Callable[[WhisperstreamConfig], None],
        refresh_models_root: Callable[[], Path],
        activation_busy: Callable[[], bool],
        has_resident_models: Callable[[], bool],
        unload_models: Callable[[], None],
        reload_models: Callable[[], None],
        broadcast: Callable[[dict[str, Any]], None],
        logger: logging.Logger,
    ) -> None:
        self._current_config = current_config
        self._save_config = save_config
        self._refresh_models_root = refresh_models_root
        self._activation_busy = activation_busy
        self._has_resident_models = has_resident_models
        self._unload_models = unload_models
        self._reload_models = reload_models
        self.broadcast = broadcast
        self._logger = logger

    def classify(self, destination: str) -> Dict[str, Any]:
        """Classify a relocation *destination* before moving model files.

        Inspects the destination directory and returns exactly one outcome:

        - ``"ok"`` — empty/usable folder with enough room.  Includes
          ``total_bytes`` (sum of all bytes in the current model trees) and
          ``free_bytes`` on the destination drive.
        - ``"reuse"`` — destination already contains a COMPLETE valid model set
          (``models/<parakeet-dir>/`` with all ``setup.REQUIRED_FILES`` present
          AND at least one ``.gguf`` under ``local-llm/``).  The caller can
          re-point config with no copy.
        - ``"blocked"`` with ``reason``:
          - ``"foreign"`` — partial or non-Whisperstream model files present.
          - ``"unwritable"`` — write probe into the destination failed.
          - ``"no_room"`` — free space on the destination drive is below
            ``total_bytes + RELOCATE_HEADROOM_BYTES``.

        Free space is always probed on the DESTINATION drive, never the current
        models location.  Does not unload any models.  Shared by the
        ``POST /api/models/relocate/check`` endpoint and the relocation worker.
        """
        import re as _re
        import shutil
        import tempfile
        from pathlib import Path as _Path
        from ..config_manager import get_local_llm_dir, get_models_root
        from ..setup import REQUIRED_FILES

        _PARAKEET_DIR_PATTERN = _re.compile(r"^nemo-parakeet-tdt-0\.6b-.+$")
        _LLM_FILE_PATTERN = _re.compile(r".+\.gguf$")

        dest = _Path(destination).expanduser()
        sources = (get_models_root(), get_local_llm_dir())
        finals = (dest / "models", dest / "local-llm")
        try:
            if any(_has_links(path, recursive=True) for path in (*sources, *finals)):
                return {"outcome": "blocked", "reason": "foreign"}
            for index, source in enumerate(sources):
                source = source.resolve()
                for final_index, final in enumerate(finals):
                    final = final.resolve()
                    if source == final and index == final_index:
                        continue
                    if source == final or source in final.parents or final in source.parents:
                        return {"outcome": "blocked", "reason": "foreign"}
        except OSError:
            return {"outcome": "blocked", "reason": "foreign"}

        # ------------------------------------------------------------------
        # 1. Inspect destination for existing model content
        # ------------------------------------------------------------------
        dest_models = dest / "models"
        dest_llm = dest / "local-llm"

        has_parakeet_content = False
        has_complete_parakeet = False
        has_gguf_content = False
        has_any_content = False

        if dest_models.exists():
            try:
                for entry in dest_models.iterdir():
                    has_any_content = True
                    if entry.is_dir() and _PARAKEET_DIR_PATTERN.match(entry.name):
                        has_parakeet_content = True
                        if all((entry / f).exists() for f in REQUIRED_FILES):
                            has_complete_parakeet = True
            except OSError:
                return {"outcome": "blocked", "reason": "foreign"}

        if dest_llm.exists():
            try:
                for entry in dest_llm.iterdir():
                    has_any_content = True
                    if entry.is_file() and _LLM_FILE_PATTERN.match(entry.name):
                        has_gguf_content = True
                        break
            except OSError:
                return {"outcome": "blocked", "reason": "foreign"}

        # Any Whisperstream model content present: reuse (complete) or foreign (partial)
        if has_any_content:
            if has_complete_parakeet and has_gguf_content:
                return {"outcome": "reuse"}
            return {"outcome": "blocked", "reason": "foreign"}

        # ------------------------------------------------------------------
        # 2. No model content — probe writability
        # ------------------------------------------------------------------
        try:
            dest.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=dest, prefix=".ws_probe_", delete=True):
                pass
        except (OSError, IOError):
            return {"outcome": "blocked", "reason": "unwritable"}

        # ------------------------------------------------------------------
        # 3. Sum all bytes in the current model trees
        # ------------------------------------------------------------------
        current_models_root = get_models_root()
        current_llm_dir = get_local_llm_dir()

        total_bytes = self.sum_tree_bytes((current_models_root, current_llm_dir))

        # ------------------------------------------------------------------
        # 4. Check free space on the DESTINATION drive
        # ------------------------------------------------------------------
        probe_path = dest
        while not probe_path.exists() and probe_path.parent != probe_path:
            probe_path = probe_path.parent
        if not probe_path.exists():
            probe_path = _Path(".")

        try:
            usage = shutil.disk_usage(probe_path)
            free_bytes = usage.free
        except OSError:
            # Fail-open: if we cannot determine free space, proceed as ok.
            free_bytes = total_bytes + RELOCATE_HEADROOM_BYTES + 1

        if free_bytes < total_bytes + RELOCATE_HEADROOM_BYTES:
            return {"outcome": "blocked", "reason": "no_room"}

        return {"outcome": "ok", "total_bytes": total_bytes, "free_bytes": free_bytes}

    @staticmethod
    def sum_tree_bytes(trees: Iterable[Path]) -> int:
        """Sum the size of every regular file under each path in *trees*."""
        total = 0
        for tree in trees:
            if not tree.exists():
                continue
            try:
                for f in tree.rglob("*"):
                    if f.is_file():
                        try:
                            total += f.stat().st_size
                        except OSError:
                            pass
            except OSError:
                pass
        return total

    def settle(self) -> None:
        """Pause briefly after unload so lagging Windows ONNX / llama-server
        handle release completes before the file copy + swap (Task 2.3 step 2).

        Kept as its own method so tests can stub it (no real sleep).
        """
        time.sleep(_RELOCATE_SETTLE_SECONDS)

    def verify_staging(
        self,
        src_models: Path,
        staging_models: Path,
        src_llm: Path,
        staging_llm: Path,
    ) -> None:
        """Verify a freshly copied staging tree before the atomic swap.

        Raises ``RuntimeError`` if any source file is missing from staging or
        differs in size.  As a belt-and-suspenders check that a *complete* model
        survived the copy, it also raises if the source held a complete Parakeet
        model / a GGUF but staging does not.

        Those whole-model checks are deliberately gated on what the SOURCE
        actually contained: we move whatever models are present, not a fixed
        model set.  An empty or partial model directory is a legitimate state to
        move as-is (e.g. a ``local-llm/`` directory left behind by a failed
        enhancement download, or a ``models/`` directory before Parakeet has been
        fetched) and must not abort the whole move.  The per-file loop above
        already guarantees every source file reached staging, so a genuinely
        dropped model is still caught.  The originals are untouched, so a raise
        here rolls back cleanly.
        """
        from ..setup import REQUIRED_FILES

        for src_tree, stage_tree in (
            (src_models, staging_models),
            (src_llm, staging_llm),
        ):
            if not src_tree.exists():
                continue
            for src_file in src_tree.rglob("*"):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(src_tree)
                stage_file = stage_tree / rel
                if not stage_file.exists():
                    raise RuntimeError(f"staging missing file: {rel}")
                if stage_file.stat().st_size != src_file.stat().st_size:
                    raise RuntimeError(f"staging size mismatch: {rel}")

        def _has_complete_parakeet(root: Path) -> bool:
            return root.exists() and any(
                entry.is_dir() and all((entry / f).exists() for f in REQUIRED_FILES)
                for entry in root.iterdir()
            )

        def _has_gguf(root: Path) -> bool:
            return root.exists() and any(
                p.is_file() and p.suffix == ".gguf" for p in root.iterdir()
            )

        if _has_complete_parakeet(src_models) and not _has_complete_parakeet(staging_models):
            raise RuntimeError("staging missing Parakeet model files")

        if _has_gguf(src_llm) and not _has_gguf(staging_llm):
            raise RuntimeError("staging missing GGUF model")

    @staticmethod
    def swap_into_place(staging: Path, final: Path) -> None:
        """Rename owned staging onto an absent or empty, unlinked final."""

        if not staging.exists():
            return
        if _has_links(staging, recursive=True) or _has_links(final, recursive=True):
            raise OSError("linked relocation tree")
        if final.exists():
            final.rmdir()  # A nonempty final must never be recursively removed.
        os.replace(staging, final)

    def run(
        self,
        destination: str,
        cancel_event: threading.Event,
        report_progress: Callable[[int, int], None],
    ) -> None:
        """Move both model trees to *destination*, streaming progress via WS.

        The riskiest path in the model-dir feature.  Mirrors
        ``_run_model_download``: reuses ``self._download_thread`` /
        ``self._download_cancel_event`` so the existing
        ``POST /api/download/cancel`` aborts it.  Broadcasts ``download.*``
        events tagged ``"kind": "relocate"`` (the same shape as the
        ``"local_llm"`` discriminator).
        """
        import shutil
        import tempfile
        from pathlib import Path as _Path
        from ..config_manager import (
            get_app_dir,
            get_local_llm_dir,
            get_models_root,
        )
        from ..model_downloader import DownloadCancelled, copy_tree_with_progress

        dest = _Path(destination).expanduser()
        staging_root: Path | None = None

        # Source trees captured BEFORE any config change so rollback + cleanup
        # always reference the original location.
        src_models = get_models_root()
        src_llm = get_local_llm_dir()
        self._logger.info("[Relocate] start status=accepted")

        def _cleanup_staging() -> None:
            if staging_root is not None and not _has_links(staging_root):
                shutil.rmtree(staging_root, ignore_errors=True)

        unloaded = False
        try:
            busy_probe = self._activation_busy
            if callable(busy_probe) and busy_probe() is True:
                raise RuntimeError("dictation_active")
            # Recheck the process-lifetime residency gate on the worker thread
            # so a resident acquired after the HTTP preflight cannot be moved
            # out from under llama-server on Windows.
            if self._has_resident_models():
                raise RuntimeError(_RESIDENT_MODELS_RESTART_REQUIRED)
            # Step 1: re-run classification; reuse short-circuits the copy.
            classification = self.classify(destination)
            outcome = classification.get("outcome")
            if outcome == "blocked":
                reason = classification.get("reason", "unknown")
                raise RuntimeError(f"destination is not usable ({reason})")
            is_reuse = outcome == "reuse"
            self._logger.info(
                f"[Relocate] classified outcome={outcome} "
                f"total_bytes={classification.get('total_bytes')} "
                f"free_bytes={classification.get('free_bytes')}"
            )

            if not is_reuse:
                # Exclusive ownership prevents an old or user-created .tmp
                # tree from being mistaken for this transaction's staging.
                staging_root = Path(tempfile.mkdtemp(prefix=".ws_relocate_", dir=dest))
                staging_models = staging_root / "models"
                staging_llm = staging_root / "local-llm"
                # Step 2: release model handles, then settle for Windows.
                # Mark ownership as detached before closing so any close error
                # still follows the existing reload-on-failure path.
                unloaded = True
                self._unload_models()
                self.settle()

                # Step 3: copy both trees into staging with aggregate progress.
                total_bytes = classification.get("total_bytes")
                if not total_bytes:
                    total_bytes = self.sum_tree_bytes((src_models, src_llm))

                moved = 0

                def _on_bytes(n: int) -> None:
                    nonlocal moved
                    moved += n
                    report_progress(moved, total_bytes or 0)

                self._logger.info(
                    f"[Relocate] copy start total_bytes={total_bytes}"
                )
                if src_models.exists():
                    copy_tree_with_progress(
                        src_models,
                        staging_models,
                        on_bytes=_on_bytes,
                        cancel_event=cancel_event,
                    )
                if src_llm.exists():
                    copy_tree_with_progress(
                        src_llm,
                        staging_llm,
                        on_bytes=_on_bytes,
                        cancel_event=cancel_event,
                    )

                self._logger.info(
                    f"[Relocate] copy complete moved_bytes={moved}"
                )

                # Step 4: verify staging, then atomically swap into place. The
                # originals stay untouched (they are the rollback safety net).
                self.verify_staging(
                    src_models, staging_models, src_llm, staging_llm
                )
                self.swap_into_place(staging_models, dest / "models")
                self.swap_into_place(staging_llm, dest / "local-llm")

            # Step 5: ONLY now (files in place) repoint config. Empty string
            # when the destination IS the default data dir (reset-to-default).
            cfg = self._current_config()
            try:
                default_base = get_app_dir().resolve()
                dest_resolved = dest.resolve()
            except OSError:
                default_base = get_app_dir()
                dest_resolved = dest
            cfg.model_storage_dir = "" if dest_resolved == default_base else str(dest)
            self._save_config(cfg)
            self._refresh_models_root()
            self._logger.info(
                "[Relocate] config saved destination=%s",
                "default" if not cfg.model_storage_dir else "custom",
            )

            # Step 6: reload both models at the new location.
            self._reload_models()
            self._logger.info("[Relocate] models reloaded at new location")

            # Legacy roots have no ownership ledger. Preserve originals rather
            # than deleting unrelated files or files added during the copy.
            # Users may remove the old copies after verifying the new location.
            _cleanup_staging()

            # Step 8: done.
            self.broadcast({"type": "download.complete", "kind": "relocate"})
            self._logger.info("[Relocate] complete status=success")

        except DownloadCancelled:
            # Cancel can only fire during the copy phase. Discard staging and
            # reload at the OLD location (config was never touched).
            self._logger.info("[Relocate] cancelled")
            _cleanup_staging()
            if unloaded:
                try:
                    self._reload_models()
                except Exception as reload_exc:
                    self._logger.error(
                        "[Relocate] reload after cancel failed error_type=%s",
                        type(reload_exc).__name__,
                    )
            self.broadcast({"type": "download.cancelled", "kind": "relocate"})
            raise
        except Exception as exc:
            # Any failure before step 5: originals + config untouched. Discard
            # staging and reload at the OLD location.
            self._logger.error(
                "[Relocate] failed error_type=%s", type(exc).__name__
            )
            _cleanup_staging()
            if unloaded:
                try:
                    self._reload_models()
                except Exception as reload_exc:
                    self._logger.error(
                        "[Relocate] reload after error failed error_type=%s",
                        type(reload_exc).__name__,
                    )
            self.broadcast({
                "type": "download.error",
                "kind": "relocate",
                "message": str(exc),
            })
            raise

    # ------------------------------------------------------------------
    # Local LLM download
    # ------------------------------------------------------------------
