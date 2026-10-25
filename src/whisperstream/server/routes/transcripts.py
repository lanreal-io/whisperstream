"""Transcript history CRUD and in-memory audio response contracts."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Optional
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from ...history_security_contract import HistoryFailure, HistoryRecordRef, SecurityAvailability
from ...transcript_store import TranscriptIntegrityError
from ...access_log import log_access

def _parse_range_header(
    range_header: str | None, total: int
) -> tuple[int | None, int | None]:
    """Parse an HTTP ``Range: bytes=start-end`` header against a total size.

    Returns ``(start, end)`` inclusive byte offsets for a satisfiable single
    range. ``end`` is clamped to ``total - 1`` and open-ended ``bytes=start-``
    runs to the end. Returns ``(None, None)`` when the header is absent,
    malformed, non-byte, multi-range, or otherwise unsatisfiable so the caller
    falls back to a full ``200`` response.
    """
    if not range_header or total <= 0:
        return None, None
    value = range_header.strip()
    prefix = "bytes="
    if not value.lower().startswith(prefix):
        return None, None
    spec = value[len(prefix):].strip()
    # Single range only; reject multi-range (comma-separated) requests.
    if "," in spec or "-" not in spec:
        return None, None
    start_str, _, end_str = spec.partition("-")
    start_str = start_str.strip()
    end_str = end_str.strip()
    try:
        if start_str == "":
            # Suffix form `bytes=-N`: the last N bytes.
            if end_str == "":
                return None, None
            suffix = int(end_str)
            if suffix <= 0:
                return None, None
            start = max(0, total - suffix)
            end = total - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str != "" else total - 1
    except ValueError:
        return None, None
    if start < 0 or start >= total or end < start:
        return None, None
    end = min(end, total - 1)
    return start, end


class PurgeBody(BaseModel):
    retention_days: int  # 0 = Forever (no age purge); 1/7/30 = days to keep


def register_transcript_list_route(
    api: FastAPI, *, current_store: Callable[[], Any],
    request_grant: Callable, acceptance_is_current: Callable,
    blocked_response: Callable, locked_response: Callable,
    history_availability: Callable[[], SecurityAvailability],
    diagnostics_locked: Callable[[], bool], note_activity: Callable[[], None],
) -> None:
    @api.get("/api/transcripts")
    async def list_transcripts(
        request: Request,
        before: Optional[int] = None,
        limit: int = 20,
        q: Optional[str] = None,
    ):
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        grant = request_grant(request)
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        # Lockdown gate: content is unreachable while the surface is locked.
        locked_resp = locked_response()
        if locked_resp is not None:
            return locked_resp
        store = current_store()
        if store is None:
            # Store init failed (Task 2.1) — surface the not-decryptable shape
            # rather than a 500 so the frontend can show the banner.
            return {
                "items": [],
                "next_cursor": None,
                "decryptable": False,
                "availability": "unavailable",
            }
        availability = history_availability()
        if availability.value in {
            "key-error",
            "key-missing",
            "not-decryptable",
            "unavailable",
        }:
            return {
                "items": [],
                "next_cursor": None,
                "decryptable": False,
                "availability": availability.value,
            }
        # Access log + idle-timer reset for every authorized access.
        log_access("list", None, enabled=diagnostics_locked())
        note_activity()
        try:
            result = store.list(before=before, limit=limit, q=q)
        except TranscriptIntegrityError:
            return {
                "items": [],
                "next_cursor": None,
                "decryptable": False,
                "availability": SecurityAvailability.NOT_DECRYPTABLE.value,
            }
        except Exception:  # noqa: BLE001 - typed storage boundary
            return {
                "items": [],
                "next_cursor": None,
                "decryptable": False,
                "availability": SecurityAvailability.UNAVAILABLE.value,
            }
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        return {**result, "availability": availability.value}

def register_transcript_record_routes(
    api: FastAPI, *, current_store: Callable[[], Any],
    request_grant: Callable, acceptance_is_current: Callable,
    blocked_response: Callable, locked_response: Callable,
    history_availability: Callable[[], SecurityAvailability],
    diagnostics_locked: Callable[[], bool], note_activity: Callable[[], None],
    current_store_adapter: Callable[[], Any], unavailable_response: Callable,
    broadcast: Callable,
) -> None:
    @api.get("/api/transcripts/{id}")
    async def get_transcript(
        id: str, request: Request
    ):  # noqa: A002 - matches the store API
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        grant = request_grant(request)
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        locked_resp = locked_response()
        if locked_resp is not None:
            return locked_resp
        store = current_store()
        availability = history_availability()
        unavailable = unavailable_response(availability)
        if store is None or unavailable is not None:
            return unavailable or unavailable_response(
                SecurityAvailability.UNAVAILABLE
            )
        try:
            record = store.get(id)
        except TranscriptIntegrityError:
            return unavailable_response(
                SecurityAvailability.NOT_DECRYPTABLE
            )
        except Exception:  # noqa: BLE001 - typed storage boundary
            return unavailable_response(
                SecurityAvailability.UNAVAILABLE
            )
        if record is None:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        log_access("get", id, enabled=diagnostics_locked())
        note_activity()
        return record

    @api.delete("/api/transcripts/{id}")
    async def delete_transcript(
        id: str, request: Request
    ):  # noqa: A002 - matches the store API
        grant = request_grant(request)
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        locked_resp = locked_response()
        if locked_resp is not None:
            return locked_resp
        store = current_store()
        availability = history_availability()
        unavailable = unavailable_response(availability)
        if store is None or unavailable is not None:
            return unavailable or unavailable_response(
                SecurityAvailability.UNAVAILABLE
            )
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        delete_with_count = getattr(
            current_store_adapter(), "delete_with_count", None
        )
        if callable(delete_with_count):
            mutation = delete_with_count(HistoryRecordRef(id))
            if mutation.result.failure not in {
                HistoryFailure.NONE,
                HistoryFailure.NOT_FOUND,
            }:
                state = history_availability()
                return unavailable_response(
                    state
                    if state
                    in {
                        SecurityAvailability.KEY_ERROR,
                        SecurityAvailability.KEY_MISSING,
                        SecurityAvailability.NOT_DECRYPTABLE,
                        SecurityAvailability.UNAVAILABLE,
                    }
                    else SecurityAvailability.UNAVAILABLE
                )
            deleted = mutation.count == 1
        else:
            try:
                deleted = store.delete(id)
            except Exception:  # noqa: BLE001 - typed storage boundary
                return unavailable_response(
                    SecurityAvailability.UNAVAILABLE
                )
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        if deleted:
            log_access("delete", id, enabled=diagnostics_locked())
            note_activity()
            broadcast({"type": "transcripts.changed"})
        return {"deleted": deleted}

    @api.post("/api/transcripts/clear")
    async def clear_transcripts(request: Request):
        grant = request_grant(request)
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        locked_resp = locked_response()
        if locked_resp is not None:
            return locked_resp
        store = current_store()
        availability = history_availability()
        unavailable = unavailable_response(availability)
        if store is None or unavailable is not None:
            return unavailable or unavailable_response(
                SecurityAvailability.UNAVAILABLE
            )
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        clear_with_count = getattr(
            current_store_adapter(), "clear_with_count", None
        )
        if callable(clear_with_count):
            mutation = clear_with_count()
            if mutation.result.failure is not HistoryFailure.NONE:
                state = history_availability()
                return unavailable_response(
                    state
                    if state
                    in {
                        SecurityAvailability.KEY_ERROR,
                        SecurityAvailability.KEY_MISSING,
                        SecurityAvailability.NOT_DECRYPTABLE,
                        SecurityAvailability.UNAVAILABLE,
                    }
                    else SecurityAvailability.UNAVAILABLE
                )
            count = mutation.count
        else:
            try:
                count = store.clear()
            except Exception:  # noqa: BLE001 - typed storage boundary
                return unavailable_response(
                    SecurityAvailability.UNAVAILABLE
                )
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        log_access("clear", None, enabled=diagnostics_locked())
        note_activity()
        broadcast({"type": "transcripts.changed"})
        return {"cleared": count}

    @api.post("/api/transcripts/purge")
    async def purge_transcripts(body: PurgeBody, request: Request):
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        # Explicit purge trigger fired by the frontend AFTER the user confirms the
        # retention-shrink dialog. The config field `transcript_retention_days` is
        # saved through the normal config PUT path; this endpoint only runs the
        # age purge with the REQUESTED window so destruction happens behind the
        # confirm dialog. The cutoff is computed from `body.retention_days`, NOT
        # the saved config (which may not be persisted yet).
        grant = request_grant(request)
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        locked_resp = locked_response()
        if locked_resp is not None:
            return locked_resp
        store = current_store()
        availability = history_availability()
        unavailable = unavailable_response(availability)
        if store is None or unavailable is not None:
            return unavailable or unavailable_response(
                SecurityAvailability.UNAVAILABLE
            )
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)

        days = int(body.retention_days or 0)
        retention_with_count = getattr(
            current_store_adapter(),
            "enforce_retention_with_count",
            None,
        )
        if callable(retention_with_count):
            mutation = retention_with_count(days)
            if mutation.result.failure is not HistoryFailure.NONE:
                state = history_availability()
                return unavailable_response(
                    state
                    if state
                    in {
                        SecurityAvailability.KEY_ERROR,
                        SecurityAvailability.KEY_MISSING,
                        SecurityAvailability.NOT_DECRYPTABLE,
                        SecurityAvailability.UNAVAILABLE,
                    }
                    else SecurityAvailability.UNAVAILABLE
                )
            purged = mutation.count
        else:
            try:
                purged = 0
                if days > 0:
                    cutoff_ms = int(time.time() * 1000) - days * 86_400_000
                    purged = store.purge_older_than(cutoff_ms)
                    if not acceptance_is_current(grant):
                        return blocked_response(request.url.path)
                # Forever (0) skips the age purge but still sweeps orphan audio.
                store.sweep_orphans()
            except Exception:  # noqa: BLE001 - typed storage boundary
                return unavailable_response(
                    SecurityAvailability.UNAVAILABLE
                )
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        log_access("purge", None, enabled=diagnostics_locked())
        note_activity()
        broadcast({"type": "transcripts.changed"})
        return {"purged": purged}

    @api.get("/api/transcripts/{id}/audio")
    async def get_transcript_audio(id: str, request: Request):  # noqa: A002 - matches the store API
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # Also never log key material (the data key or any nonce/ciphertext bytes).
        # ============================================================================
        grant = request_grant(request)
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)
        # Audio is transcript-derived; the WAV is decrypted in memory per
        # request and NEVER written as plaintext to disk. WebView2 issues
        # aggressive Range requests for <audio>, so a correct 206 path is
        # required for seeking.
        locked_resp = locked_response()
        if locked_resp is not None:
            return locked_resp
        store = current_store()
        availability = history_availability()
        unavailable = unavailable_response(availability)
        if store is None or unavailable is not None:
            return unavailable or unavailable_response(
                SecurityAvailability.UNAVAILABLE
            )
        try:
            data = store.get_audio_bytes(id)
        except TranscriptIntegrityError:
            return unavailable_response(
                SecurityAvailability.NOT_DECRYPTABLE
            )
        except Exception:  # noqa: BLE001 - typed storage boundary
            return unavailable_response(
                SecurityAvailability.UNAVAILABLE
            )
        if data is None:
            # No audio / id missing / undecryptable.
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        if not acceptance_is_current(grant):
            return blocked_response(request.url.path)

        log_access("get_audio", id, enabled=diagnostics_locked())
        note_activity()
        total = len(data)
        range_header = request.headers.get("range") or request.headers.get("Range")
        start, end = _parse_range_header(range_header, total)
        if start is None:
            # No / invalid / malformed Range header -> full 200.
            return Response(
                content=data,
                status_code=200,
                media_type="audio/wav",
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(total),
                },
            )

        chunk = data[start : end + 1]
        return Response(
            content=chunk,
            status_code=206,
            media_type="audio/wav",
            headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(chunk)),
            },
        )
