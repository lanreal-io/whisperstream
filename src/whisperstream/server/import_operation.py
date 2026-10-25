"""Single-flight audio import with exact selection and grant ownership."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor
from typing import TYPE_CHECKING, Optional, Protocol
from ..eula import AcceptanceGrant
from ..history_security_contract import (
    FileSelection, FileWorkflowAdapter, FileWorkflowKind, HistoryFailure,
)
from ..enhancement_outcomes import (
    EnhancementOutcome, EnhancementProfileSnapshot, EnhancementResult,
    TranscriptEnhancementProvenance,
)
from ..enhancement_security import enhancement_is_admitted
from ..transcript_store import history_write_available, run_retention_sweep
from ..access_log import log_access
from .callbacks import EventBroadcaster

if TYPE_CHECKING:
    from ..config_manager import WhisperstreamConfig
    from ..llm_enhancer import ErrorObserver, TimeoutObserver
    from ..stt_contracts import SttEngine
    from ..transcript_store import TranscriptStore


class ImportEnhancer(Protocol):
    """One typed enhancement call with the import's frozen profile and observers."""

    async def __call__(
        self,
        text: str,
        *,
        profile_id: str,
        on_timeout: TimeoutObserver,
        on_error: ErrorObserver,
    ) -> EnhancementResult:
        # TRANSCRIPT DATA - DO NOT LOG. Input and result text are private content.
        ...

# Locked, user-facing failure reason for an unreadable / unsupported import file
# (copy-review.md "Import queue - failed reason"). The loader's RuntimeError /
# FileNotFoundError is mapped to THIS string; the raw exception is never
# surfaced to the user.
_IMPORT_FAILED_REASON = "This format could not be transcribed. Convert it to WAV and try again."


class ImportOperation:
    """Own reservation, cancellation, source release and sequential processing."""

    def __init__(
        self, *,
        current_config: Callable[[], WhisperstreamConfig],
        current_engine: Callable[[], SttEngine | None],
        current_store: Callable[[], TranscriptStore | None],
        current_executor: Callable[[], Executor | None],
        current_file_workflows: Callable[[], FileWorkflowAdapter],
        snapshot_profile: Callable[[str | None], EnhancementProfileSnapshot | None],
        enhance: ImportEnhancer,
        provenance: Callable[
            [EnhancementProfileSnapshot | None, EnhancementOutcome],
            TranscriptEnhancementProvenance | None,
        ],
        apply_signature: Callable[[str, str | None], str],
        process_text: Callable[[str], str],
        current_acceptance_grant: Callable[[], AcceptanceGrant],
        acceptance_is_current: Callable[[AcceptanceGrant], bool],
        diagnostics_locked: Callable[[], bool],
        broadcast: EventBroadcaster,
        logger: logging.Logger,
        history_disabled: Callable[[], bool] = lambda: False,
    ) -> None:
        self._history_disabled = history_disabled
        self._current_config = current_config
        self._current_engine = current_engine
        self._current_store = current_store
        self._current_executor = current_executor
        self._current_file_workflows = current_file_workflows
        self._snapshot_profile = snapshot_profile
        self._enhance = enhance
        self._provenance = provenance
        self._apply_signature = apply_signature
        self._process_text = process_text
        self._current_acceptance_grant = current_acceptance_grant
        self._acceptance_is_current = acceptance_is_current
        self._diagnostics_locked = diagnostics_locked
        self.broadcast = broadcast
        self._logger = logger
        self._import_state_lock = threading.Lock()
        self._import_active = False
        self._import_cancel_event: threading.Event | None = None

    @property
    def active(self) -> bool:
        with self._import_state_lock:
            return self._import_active

    def try_start(self, sources: list[FileSelection | str], grant: AcceptanceGrant) -> bool:
        executor = self._current_executor()
        if executor is None:
            self.release_sources(sources)
            return False
        cancel_event = threading.Event()
        with self._import_state_lock:
            if self._import_active:
                self.release_sources(sources)
                return False
            self._import_active = True
            self._import_cancel_event = cancel_event
        try:
            future = executor.submit(
                self.run, sources, grant, cancel_event
            )
            future.add_done_callback(
                lambda _future: self.finish(
                    cancel_event, sources
                )
            )
        except Exception:
            with self._import_state_lock:
                if self._import_cancel_event is cancel_event:
                    self._import_active = False
                    self._import_cancel_event = None
            self.release_sources(sources)
            return False
        return True

    def release_sources(
        self, sources: list[FileSelection | str]
    ) -> None:
        file_workflows = self._current_file_workflows()
        release_many = getattr(file_workflows, "release_many", None)
        if callable(release_many):
            release_many(
                [source for source in sources if isinstance(source, FileSelection)]
            )

    def finish(
        self,
        cancel_event: threading.Event,
        sources: list[FileSelection | str] | None = None,
    ) -> None:
        """Release the single-flight slot owned by one exact import batch."""
        if sources:
            self.release_sources(sources)
        with self._import_state_lock:
            if self._import_cancel_event is cancel_event:
                self._import_active = False
                self._import_cancel_event = None

    def cancel(self) -> None:
        """Permanently revoke the active import without retaining its paths."""
        with self._import_state_lock:
            cancel_event = self._import_cancel_event
        if cancel_event is not None:
            cancel_event.set()

    @staticmethod
    def display_name(path: str) -> str:
        """Return a basename only, never an absolute or directory path."""
        trimmed = path.rstrip("/\\")
        if not trimmed:
            return ""
        name = os.path.basename(trimmed)
        # On non-Windows test hosts, os.path does not treat backslashes as
        # separators. Split both forms so privacy does not depend on platform.
        name = name.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        return "" if name.endswith(":") else name

    def run(
        self,
        sources: list[FileSelection | str],
        acceptance_grant: AcceptanceGrant | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Normalize native sources to opaque selections and release them exactly."""

        self._current_file_workflows()
        selections: list[FileSelection] = []
        normalized: list[FileSelection | str]
        if sources and all(isinstance(source, str) for source in sources):
            register_paths = getattr(self._current_file_workflows(), "register_paths", None)
            if callable(register_paths):
                batch = register_paths(
                    FileWorkflowKind.IMPORT_AUDIO,
                    [source for source in sources if isinstance(source, str)],
                )
                if batch.failure is not HistoryFailure.NONE:
                    return
                selections = list(batch.selections)
                normalized = list(selections)
            else:
                normalized = list(sources)
        elif all(isinstance(source, FileSelection) for source in sources):
            selections = [
                source for source in sources if isinstance(source, FileSelection)
            ]
            normalized = list(selections)
        else:
            return

        try:
            self._run_selections(
                normalized,
                acceptance_grant=acceptance_grant,
                cancel_event=cancel_event,
            )
        finally:
            self.release_sources(list(selections))

    def _run_selections(
        self,
        sources: list[FileSelection | str],
        acceptance_grant: AcceptanceGrant | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Transcribe + persist a batch of audio files, one at a time.

        Runs on a worker thread (spawned by POST /api/import) so a long batch
        never blocks the event loop. Files are processed SEQUENTIALLY because
        Parakeet is a single in-memory model. Per file we broadcast
        `import.progress` events (transcribing → done | failed) and, after each
        successful save, a `transcripts.changed` so open clients refresh.

        A per-file failure (unreadable / unsupported format — the loader raises a
        friendly RuntimeError when ffmpeg is absent, or FileNotFoundError for a
        missing path) is caught, mapped to the LOCKED user-facing reason, and the
        batch CONTINUES to the next file. The raw exception is never surfaced.
        """
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        from ..main import _load_audio_file
        from ..waveform_peaks import compute_peaks, wav_bytes_from_float32

        grant = acceptance_grant or self._current_acceptance_grant()

        def authorized() -> bool:
            return bool(
                self._acceptance_is_current(grant)
                and not (cancel_event is not None and cancel_event.is_set())
            )

        if not authorized():
            return
        for ordinal, source in enumerate(sources, start=1):
            # Stop the remaining queue on revocation. Do not emit filenames or
            # failed rows for work that is no longer authorized.
            if not authorized():
                return
            name = (
                source.display_name
                if isinstance(source, FileSelection)
                else self.display_name(source)
            )
            self.broadcast(
                {"type": "import.progress", "file": name, "status": "transcribing"},
                acceptance_grant=grant,
            )

            # --- Load (decode) the file. ffmpeg-absent / missing-path failures
            # become the locked friendly reason, never a raw exception. ---
            try:
                if not authorized():
                    return
                if isinstance(source, FileSelection):
                    read_with = getattr(self._current_file_workflows(), "read_with", None)
                    if not callable(read_with):
                        raise RuntimeError("file workflow unavailable")
                    failure, loaded = read_with(
                        source, _load_audio_file, self._logger
                    )
                    if failure is not HistoryFailure.NONE or loaded is None:
                        raise RuntimeError("selected audio unavailable")
                    waveform, sample_rate = loaded
                else:
                    waveform, sample_rate = _load_audio_file(source, self._logger)
                if not authorized():
                    return
            except (RuntimeError, FileNotFoundError) as exc:
                if not authorized():
                    return
                self._logger.warning(
                    "import file failed ordinal=%d status=failed error_type=%s",
                    ordinal,
                    type(exc).__name__,
                )
                self.broadcast(
                    {
                        "type": "import.progress",
                        "file": name,
                        "status": "failed",
                        "reason": _IMPORT_FAILED_REASON,
                    },
                    acceptance_grant=grant,
                )
                continue
            except Exception as exc:
                if not authorized():
                    return
                # Any other decode failure also degrades to the friendly reason
                # rather than crashing the worker or surfacing internals.
                self._logger.warning(
                    "import file failed ordinal=%d status=failed error_type=%s",
                    ordinal,
                    type(exc).__name__,
                )
                self.broadcast(
                    {
                        "type": "import.progress",
                        "file": name,
                        "status": "failed",
                        "reason": _IMPORT_FAILED_REASON,
                    },
                    acceptance_grant=grant,
                )
                continue

            # --- Transcribe + enhance (mirrors /api/transcribe's consent gate). ---
            try:
                if not authorized():
                    return
                # Older focused test/tool stubs may expose only ``parakeet``.
                # Real app instances resolve the backend-neutral property.
                engine = self._current_engine()
                if engine is None:
                    raise RuntimeError("Model not loaded")

                start = time.perf_counter()
                raw_text = engine.transcribe(
                    waveform,
                    sample_rate=sample_rate,
                    language=self._current_config().language,
                )
                duration_ms = int((time.perf_counter() - start) * 1000)
                if not authorized():
                    return

                enhanced_text: Optional[str] = None
                enhancement_provenance = None
                if self._current_config().enhancement_enabled:
                    # Imports always use the global default, never transient
                    # foreground/manual routing. Freeze its id + display name
                    # before provider work so a concurrent config edit cannot
                    # rewrite the historical identity or redirect the prompt.
                    profile_snapshot = self._snapshot_profile(None)
                    if profile_snapshot is not None:
                        # Use effective_provider() so lockdown forces dispatch
                        # to "local" even when a cloud provider is stored.
                        provider = (
                            self._current_config().effective_provider() or ""
                        ).lower()
                        # Check the same destination-bound permission as live dictation.
                        if enhancement_is_admitted(self._current_config()):
                            try:
                                if not authorized():
                                    return
                                result = asyncio.run(
                                    self._enhance(
                                        raw_text,
                                        profile_id=profile_snapshot.profile_id,
                                        on_timeout=lambda _provider: None,
                                        on_error=lambda _provider: None,
                                    )
                                )
                                if not authorized():
                                    return
                                enhancement_provenance = (
                                    self._provenance(
                                        profile_snapshot, result.outcome
                                    )
                                )
                                if result.outcome == "success":
                                    # A signature is successful enhanced output,
                                    # not a mutation of a skipped/fallback Original.
                                    enhanced_text = self._apply_signature(
                                        result.text, profile_snapshot.profile_id
                                    )
                            except Exception as exc:
                                if not authorized():
                                    return
                                enhancement_provenance = (
                                    self._provenance(
                                        profile_snapshot, "error"
                                    )
                                )
                                self._logger.warning(
                                    "import enhancement failed ordinal=%d error_type=%s",
                                    ordinal,
                                    type(exc).__name__,
                                )
                        else:
                            self._logger.warning(
                                "import enhancement requested but no consent record "
                                "for provider=%s ordinal=%d action=skip",
                                provider,
                                ordinal,
                            )

                # Final text uses the same processing pipeline as live dictation.
                if not authorized():
                    return
                final_text = self._process_text(
                    enhanced_text if enhanced_text is not None else raw_text
                )
                if not authorized():
                    return

                # --- Persist (honoring global history + save_audio). ---
                store = self._current_store()
                history_on = bool(getattr(self._current_config(), "history_enabled", False))
                if store is not None and history_on and not self._history_disabled():
                    if not history_write_available(store):
                        raise RuntimeError("history store unavailable")
                    save_audio = bool(getattr(self._current_config(), "save_audio", False))
                    peaks = None
                    audio_bytes = None
                    # Peaks are saved only when audio is saved.
                    if save_audio and getattr(waveform, "size", 0) > 0:
                        if not authorized():
                            return
                        peaks = compute_peaks(waveform)
                        audio_bytes = wav_bytes_from_float32(waveform, 16000)
                    if not authorized():
                        return
                    if not history_write_available(store):
                        raise RuntimeError("history store unavailable")
                    if self._history_disabled():
                        return
                    rid = store.save(
                        raw_text=raw_text,
                        enhanced_text=final_text if enhanced_text is not None else None,
                        duration_ms=duration_ms,
                        model=self._current_config().parakeet_model,
                        source="import",
                        audio_bytes=audio_bytes,
                        peaks=peaks,
                        enhancement_provenance=enhancement_provenance,
                    )
                    if not authorized():
                        return
                    if rid is None:
                        raise RuntimeError("history store unavailable")
                    # F7: under lockdown, emit a content-free access-log entry
                    # for the import save so the required audit trail
                    # (45 CFR 164.312(b)) covers EVERY write path, matching the
                    # dictation persist path. NEVER log key material or transcript
                    # content — only action=save + the record id.
                    if (
                        getattr(self._current_config(), "lockdown_mode", False)
                        and rid is not None
                    ):
                        log_access(
                            "save", rid, enabled=self._diagnostics_locked()
                        )
                    if rid is not None:
                        # Imports participate in the same opportunistic
                        # retention/orphan lifecycle as microphone saves. This
                        # only touches app-owned history; the user-selected
                        # source file is never modified or unlinked.
                        run_retention_sweep(self._current_config(), store)

                self.broadcast(
                    {"type": "import.progress", "file": name, "status": "done"},
                    acceptance_grant=grant,
                )
                # A new record landed (or would have, history permitting); refresh
                # any open list. Harmless when history is off (an empty refetch).
                self.broadcast({"type": "transcripts.changed"})
            except Exception as exc:
                if not authorized():
                    return
                # Transcription / persistence failure: report failed and continue.
                self._logger.warning(
                    "import file failed ordinal=%d status=failed error_type=%s",
                    ordinal,
                    type(exc).__name__,
                )
                self.broadcast(
                    {
                        "type": "import.progress",
                        "file": name,
                        "status": "failed",
                        "reason": _IMPORT_FAILED_REASON,
                    },
                    acceptance_grant=grant,
                )
