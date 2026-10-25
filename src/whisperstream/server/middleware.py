"""HTTP admission, origin checks, bounded request logging and error responses.

Middleware is installed in the existing order. Callbacks resolve current domain
state; this module never retains a config, key, content store or acceptance grant.
"""

from __future__ import annotations

import logging
import asyncio
import re
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import _build_flags
from ..audio_limits import MAX_UPLOAD_REQUEST_BYTES
from ..byok_consent import ConsentStorageError
from ..eula import AcceptanceGrant
from ..protected_secret_contract import ProtectedSecretError, ProtectedSecretFailure

_HUD_COMMAND_CLIENT_HEADER = "x-whisperstream-client"
_HUD_COMMAND_CLIENT_VALUE = "hud-v1"
_PERMISSION_ACTION_CLIENT_HEADER = "x-whisperstream-user-intent"
_PERMISSION_ACTION_CLIENT_VALUE = "permission-action-v1"


_EULA_BLOCK_CODE = "eula_acceptance_required"


# Explicit inventory of normal REST routes that can read, infer over, disclose,
# transmit, or mutate user content. The middleware checks
# this inventory before FastAPI parses a JSON or multipart body. Handlers still
# revalidate the captured exact grant at delayed stage boundaries.

_CONTENT_HTTP_ROUTES = frozenset(
    {
        ("POST", "/api/dictation/start"),
        ("POST", "/api/dictation/complete"),
        ("POST", "/api/agent-event"),
        ("POST", "/api/claude-event"),
        ("POST", "/api/claude-hook"),
        ("POST", "/api/codex-hook"),
        ("POST", "/api/agent-transport/claude"),
        ("POST", "/api/managed-sessions/register"),
        ("POST", "/api/managed-sessions/v2/register"),
        ("POST", "/api/enhancement/check"),
        ("POST", "/api/enhancement/preview"),
        ("POST", "/api/review"),
        ("POST", "/api/feedback"),
        ("POST", "/api/import"),
        ("POST", "/api/transcribe"),
    }
)


_CONTENT_HTTP_PREFIXES = frozenset({"/api/transcripts"})


def _is_content_http_route(
    method: str, path: str, *, experimental_enabled: Callable[[], bool]
) -> bool:
    """Return whether one normal REST request enters user-content work."""
    normalized_method = method.upper()
    bakeoff_content = bool(
        normalized_method == "POST"
        and _build_flags.EXPERIMENTAL_AVAILABLE
        and experimental_enabled()
        and (
            re.fullmatch(
                r"/api/experimental/transcripts/[^/]+/bakeoff", path
            )
            or re.fullmatch(r"/api/experimental/bakeoffs/[^/]+/retry", path)
        )
    )
    managed_session_content = bool(
        re.fullmatch(r"/api/managed-sessions/[^/]+/(?:reply|pending)", path)
        and normalized_method in {"GET", "POST"}
    )
    return (
        bakeoff_content
        or managed_session_content
        or (normalized_method, path) in _CONTENT_HTTP_ROUTES
        or any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in _CONTENT_HTTP_PREFIXES
        )
    )


_FRONTEND_LOG_MAX_REQUEST_BYTES = 256 * 1_024


_TRANSCRIPT_REST_PATH_RE = re.compile(
    r"^/api/transcripts/[^/]+(?P<audio>/audio)?$"
)


def _safe_rest_log_path(path: str) -> str:
    """Remove transcript record identifiers from REST diagnostics."""
    match = _TRANSCRIPT_REST_PATH_RE.fullmatch(path)
    if match is not None:
        return "/api/transcripts/{record_id}" + (match.group("audio") or "")
    if re.fullmatch(r"/api/dev/transcripts/[^/]+/enhance", path):
        return "/api/dev/transcripts/{record_id}/enhance"
    if re.fullmatch(r"/api/experimental/transcripts/[^/]+/reenhance", path):
        return "/api/experimental/transcripts/{record_id}/reenhance"
    if re.fullmatch(r"/api/experimental/transcripts/[^/]+/bakeoff", path):
        return "/api/experimental/transcripts/{record_id}/bakeoff"
    if re.fullmatch(r"/api/experimental/bakeoffs/[^/]+/cancel", path):
        return "/api/experimental/bakeoffs/{run_id}/cancel"
    if re.fullmatch(r"/api/experimental/bakeoffs/[^/]+/retry", path):
        return "/api/experimental/bakeoffs/{run_id}/retry"
    managed = re.fullmatch(
        r"/api/managed-sessions/[^/]+/(?P<action>reply|pending|delivered)",
        path,
    )
    if managed is not None:
        return f"/api/managed-sessions/{{session_handle}}/{managed.group('action')}"
    return path


def _is_allowed_websocket_origin(origin: str | None) -> bool:
    """Return whether a browser origin may connect to the shared WebSocket.

    Keep this allowlist narrower than the REST CORS policy: the WebSocket carries
    transcript-class events and browser clients can otherwise bypass CORS. Never
    log ``origin`` here or at the rejection site.
    """
    if not isinstance(origin, str) or not origin:
        return False
    if origin == "tauri://localhost":
        return True

    try:
        parsed = urlsplit(origin)
        port = parsed.port  # Access validates the numeric range.
    except ValueError:
        return False

    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False

    hostname = parsed.hostname
    if hostname == "tauri.localhost":
        return port is None
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    return parsed.scheme == "http" and hostname == "testserver" and port is None


class _LoopbackAdmissionMiddleware:
    """Reject browser/rebinding traffic before parsing bodies or logging URLs.

    Local tools without browser headers remain supported. This boundary does
    not isolate the application from other programs running as the same user.
    """

    def __init__(self, app: ASGIApp, logger: logging.Logger):
        self.app = app
        self.logger = logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        hosts = [v.decode("latin-1") for k, v in headers if k.lower() == b"host"]
        origins = [v.decode("latin-1") for k, v in headers if k.lower() == b"origin"]
        fetch_sites = [v for k, v in headers if k.lower() == b"sec-fetch-site"]
        test_transport = (
            (scope.get("client") or (None,))[0] == "testclient"
            and (scope.get("server") or (None,))[0] == "testserver"
        )
        valid_host = False
        if len(hosts) == 1:
            host = hosts[0]
            try:
                parsed = urlsplit("http://" + host)
                port = parsed.port
                valid_host = bool(
                    re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]+)?", host)
                    and (port is None or 0 < port <= 65535)
                ) or (test_transport and host == "testserver")
            except ValueError:
                pass
        valid_origin = len(origins) <= 1 and (
            not origins
            or (
                _is_allowed_websocket_origin(origins[0])
                and (origins[0] != "http://testserver" or test_transport)
            )
        )
        # Originless browser navigation can still expose a GET response through
        # embedding. Non-browser clients do not send Fetch Metadata headers.
        browser_without_origin = not origins and any(
            site not in {b"same-origin", b"same-site", b"none"}
            for site in fetch_sites
        )
        if not valid_host or not valid_origin or browser_without_origin:
            if scope["type"] == "websocket":
                self.logger.warning("websocket rejected reason=origin_policy")
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "request forbidden"}, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _TranscribeUploadBoundary:
    """Bound one multipart request before the parser spools it to disk."""

    def __init__(self, app: ASGIApp):
        self.app = app
        self.slot = threading.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != "/api/transcribe":
            return await self.app(scope, receive, send)
        lengths = [v for k, v in scope.get("headers", []) if k.lower() == b"content-length"]
        try:
            if len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or int(lengths[0]) > MAX_UPLOAD_REQUEST_BYTES)):
                await JSONResponse({"error": "audio_limit_exceeded"}, status_code=413)(scope, receive, send)
                return
        except ValueError:
            await JSONResponse({"error": "audio_limit_exceeded"}, status_code=413)(scope, receive, send)
            return
        if not self.slot.acquire(blocking=False):
            await JSONResponse({"error": "transcription_busy"}, status_code=429)(scope, receive, send)
            return
        try:
            body = bytearray()
            size = 0
            try:
                async with asyncio.timeout(120):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        size += len(message.get("body", b""))
                        if size > MAX_UPLOAD_REQUEST_BYTES:
                            await JSONResponse({"error": "audio_limit_exceeded"}, status_code=413)(scope, receive, send)
                            return
                        body.extend(message.get("body", b""))
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await JSONResponse({"error": "upload_timeout"}, status_code=408)(scope, receive, send)
                return
            iterator = iter([{"type": "http.request", "body": bytes(body), "more_body": False}])
            del body

            async def bounded_receive():
                message = next(iterator, None)
                return message if message is not None else await receive()

            await self.app(scope, bounded_receive, send)
        finally:
            self.slot.release()


def _is_trusted_hud_command_request(request: Request) -> bool:
    """Require the desktop HUD's non-simple header and a trusted browser origin.

    The loopback server is reachable from arbitrary websites. CORS only hides a
    response; it does not prevent a simple cross-origin POST from taking effect.
    The custom header forces browser preflight, while the explicit origin check
    rejects any hostile request that reaches the application anyway. Requests
    without an Origin remain available to local non-browser tooling and tests.
    """
    if (
        request.headers.get(_HUD_COMMAND_CLIENT_HEADER)
        != _HUD_COMMAND_CLIENT_VALUE
    ):
        return False
    origin = request.headers.get("origin")
    return origin is None or _is_allowed_websocket_origin(origin)


def _is_trusted_permission_action_request(request: Request) -> bool:
    """Require an explicit desktop action marker and trusted browser origin.

    The marker forces browser preflight and is accepted only by the request and
    recovery endpoints. Passive status never reads it, mints an intent, or
    imports the prompt-capable adapter.
    """

    if (
        request.headers.get(_PERMISSION_ACTION_CLIENT_HEADER)
        != _PERMISSION_ACTION_CLIENT_VALUE
    ):
        return False
    origin = request.headers.get("origin")
    return origin is None or _is_allowed_websocket_origin(origin)


def install_middleware(
    api: FastAPI,
    *,
    logger: logging.Logger,
    current_acceptance_grant: Callable[[], AcceptanceGrant],
    acceptance_is_current: Callable[[AcceptanceGrant], bool],
    content_blocked_response: Callable[[str], JSONResponse],
    history_startup_busy_response: Callable[[], JSONResponse | None],
    experimental_enabled: Callable[[], bool],
) -> None:
    @api.exception_handler(ProtectedSecretError)
    async def protected_secret_error_handler(
        _request: Request,
        error: ProtectedSecretError,
    ) -> JSONResponse:
        status_code = (
            503
            if error.failure is ProtectedSecretFailure.UNAVAILABLE
            else 500
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "error": "protected_secret_error",
                "error_code": "secure_storage_failed",
                "operation": error.operation.value,
                "failure": error.failure.value,
            },
        )

    @api.exception_handler(ConsentStorageError)
    async def consent_storage_failed(request: Request, exc: ConsentStorageError):
        return JSONResponse(
            status_code=503,
            content={"error": "consent_store_unavailable", "error_code": "consent_store_unavailable"},
        )

    api.add_middleware(
        CORSMiddleware,
        allow_origins=["tauri://localhost"],
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\[::1\]|tauri\.localhost)(:\d+)?",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- REST logging middleware ---
    # Logs every request/response at INFO with method, path, status, and
    # duration. Log ingest/export avoid recursive sync spam; the frequent
    # accessibility status probe is kept quiet to preserve useful context.
    _EXEMPT_PATHS = frozenset(
        {
            "/api/logs/frontend",
            "/api/logs/export",
            "/api/accessibility/screen-reader",
        }
    )
    _middleware_logger = logger

    class _RestLoggingMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            method = request.method
            if method == "POST" and path in {
                "/api/logs/frontend",
                "/api/logs/export",
            }:
                try:
                    content_length = int(request.headers.get("content-length", "0"))
                except ValueError:
                    content_length = _FRONTEND_LOG_MAX_REQUEST_BYTES + 1
                if content_length > _FRONTEND_LOG_MAX_REQUEST_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "frontend log payload too large"},
                    )
            if path in _EXEMPT_PATHS:
                return await call_next(request)
            safe_path = _safe_rest_log_path(path)
            quiet_success = (method, path) in {
                ("GET", "/api/agent-hook/verify"),
                ("POST", "/api/agent-transport/claude"),
            }
            if not quiet_success:
                _middleware_logger.info(
                    "api request method=%s path=%s", method, safe_path
                )
            t0 = time.perf_counter()
            try:
                response = await call_next(request)
            except Exception as exc:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                _middleware_logger.error(
                    "api unhandled_exception method=%s path=%s "
                    "duration_ms=%d error_type=%s",
                    method,
                    safe_path,
                    duration_ms,
                    type(exc).__name__,
                )
                raise
            duration_ms = int((time.perf_counter() - t0) * 1000)
            status = response.status_code
            if status >= 500:
                _middleware_logger.error(
                    "api error method=%s path=%s status=%d",
                    method,
                    safe_path,
                    status,
                )
            elif not quiet_success or status >= 400:
                _middleware_logger.info(
                    "api response method=%s path=%s status=%d duration_ms=%d",
                    method,
                    safe_path,
                    status,
                    duration_ms,
                )
            return response

    api.add_middleware(_RestLoggingMiddleware)
    # EULA admission (added below) remains outside the upload body reader.
    api.add_middleware(_TranscribeUploadBoundary)

    # Gate classified content routes before FastAPI reads JSON/multipart
    # bodies. The exact grant is attached to request.state so the handler
    # can reject a later revocation instead of silently adopting a new
    # acceptance that arrived after this request began.

    class _EulaContentBoundaryMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            if not _is_content_http_route(request.method, path, experimental_enabled=experimental_enabled):
                return await call_next(request)
            grant = current_acceptance_grant()
            if not acceptance_is_current(grant):
                logger.info(
                    "content request blocked method=%s path=%s reason=%s",
                    request.method,
                    _safe_rest_log_path(path),
                    _EULA_BLOCK_CODE,
                )
                return content_blocked_response(path)
            request.state.eula_acceptance_grant = grant
            return await call_next(request)

    api.add_middleware(_EulaContentBoundaryMiddleware)

    # Key rotation/reset must not overtake a background Keychain read and
    # allow it to publish an obsolete key. Status and ordinary app startup
    # remain available while history waits for the OS.
    class _HistoryStartupBoundaryMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.method == "POST" and request.url.path.startswith("/api/lockdown/"):
                busy = history_startup_busy_response()
                if busy is not None:
                    return busy
            return await call_next(request)

    api.add_middleware(_HistoryStartupBoundaryMiddleware)
    # Last added is outermost, including WebSocket upgrades and CORS preflight.
    api.add_middleware(_LoopbackAdmissionMiddleware, logger=logger)
