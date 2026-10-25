"""Admission and source selection for the single-flight import operation."""
from __future__ import annotations

from collections.abc import Callable
from fastapi import FastAPI, Request
from typing import Annotated
from pydantic import BaseModel, Field, StringConstraints
from ..import_operation import ImportOperation
from ...history_security_contract import FileSelection, FileWorkflowKind, HistoryFailure, SecurityAvailability
from ...transcript_store import history_write_available

class ImportBody(BaseModel):
    paths: list[
        Annotated[str, StringConstraints(min_length=1, max_length=32_767)]
    ] = Field(default_factory=list, max_length=100)
    selection_tokens: list[
        Annotated[str, StringConstraints(min_length=1, max_length=256)]
    ] = Field(default_factory=list, max_length=100)


def register_import_route(
    api: FastAPI, operation: ImportOperation, *, current_config: Callable,
    current_store: Callable, current_file_workflows: Callable,
    request_grant: Callable, acceptance_is_current: Callable,
    blocked_response: Callable, locked_response: Callable,
    history_availability: Callable, unavailable_response: Callable,
) -> None:
    @api.post("/api/import")
    async def import_audio(body: ImportBody, request: Request):
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        # Path-based batch import (the Tauri picker / drag-drop deliver file
        # PATHS, not multipart uploads — this avoids the /api/transcribe
        # `.wav`-suffix bug and large transfers). Files are processed
        # SEQUENTIALLY in a worker thread (Parakeet is a single model); the
        # POST returns immediately and per-file results stream over the
        # WebSocket as `import.progress` events, which is how the frontend
        # import queue consumes them.
        grant = request_grant(request)
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        locked_resp = locked_response()
        if locked_resp is not None:
            return locked_resp
        store = current_store()
        if (
            bool(getattr(current_config(), "history_enabled", False))
            and store is not None
            and not history_write_available(store)
        ):
            availability = history_availability()
            return unavailable_response(
                availability
                if availability
                in {
                    SecurityAvailability.KEY_ERROR,
                    SecurityAvailability.KEY_MISSING,
                    SecurityAvailability.NOT_DECRYPTABLE,
                    SecurityAvailability.UNAVAILABLE,
                }
                else SecurityAvailability.NOT_DECRYPTABLE
            )
        paths = [p for p in (body.paths or []) if isinstance(p, str) and p.strip()]
        selection_tokens = [
            token
            for token in (body.selection_tokens or [])
            if isinstance(token, str) and token.strip()
        ]
        if bool(paths) == bool(selection_tokens):
            return {"started": False}
        file_workflows = current_file_workflows()
        register_tokens = getattr(file_workflows, "register_tokens", None)
        register_paths = getattr(file_workflows, "register_paths", None)
        if selection_tokens and callable(register_tokens):
            batch = register_tokens(FileWorkflowKind.IMPORT_AUDIO, selection_tokens)
            if batch.failure is not HistoryFailure.NONE:
                return {"started": False}
            import_sources: list[FileSelection | str] = list(batch.selections)
        elif paths and callable(register_paths):
            batch = register_paths(FileWorkflowKind.IMPORT_AUDIO, paths)
            if batch.failure is not HistoryFailure.NONE:
                return {"started": False}
            import_sources = list(batch.selections)
        else:
            return {"started": False}
        if not acceptance_is_current(grant):
            operation.release_sources(import_sources)
            return blocked_response(request.url.path)
        return {"started": operation.try_start(import_sources, grant)}
