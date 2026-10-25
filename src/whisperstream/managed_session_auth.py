"""Per-run capability token gating the managed-session routes.

There is no authentication anywhere else in the backend: every existing
``/api/*`` route trusts loopback-only binding as its sole boundary. That is
not sufficient once a route can inject text into whatever window the user is
looking at (a managed-session reply). This module adds a per-process
capability token that callers must present on every managed-session request.

Honest ceiling: presenting the token requires reading a file this backend
wrote under the current user's own app-data directory. Any process running as
that same Windows user can already read that file, so this raises the bar
from "loopback-bound" to "the current user's own local processes can read the
current user's own files" -- it is **not** process isolation and must never
be described as such in logs, docs, or UI copy. This is the exact ceiling the
repo already accepts for DPAPI-sealed secrets (see ``key_encryption.py``):
DPAPI protects against a different Windows account or a copied file, not
against another process the same user is running.

The token is minted fresh on every backend start and never re-derived, so a
stale token captured from a previous run (or leaked in a log from an earlier
process) never authorizes anything against the current one. The token value
itself must never be logged; only mint/clear/compare *events* are logged.
"""
from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from fastapi import HTTPException, Request

from .atomic_io import atomic_write_json

TOKEN_FILENAME = "managed-session-token.json"
HEADER_NAME = "x-whisperstream-session-token"


class ManagedSessionAuth:
    """Holds the current per-run capability token and validates it.

    One instance is process-global (see the module-level ``auth`` singleton
    below); tests construct their own instance to avoid cross-test state.
    """

    def __init__(self) -> None:
        self._token: str | None = None

    @property
    def token(self) -> str | None:
        """The current token, or ``None`` before ``mint`` / after ``clear``."""
        return self._token

    def mint(self, app_dir: Path) -> str:
        """Mint a fresh 256-bit URL-safe token and persist it to *app_dir*.

        Replaces any previously minted value in this process so a restart
        (a new process, hence a new ``ManagedSessionAuth`` instance) always
        invalidates whatever token an earlier run wrote to disk. The write is
        atomic (mkstemp -> fsync -> ``os.replace``, via ``atomic_io``) but not
        best-effort here: callers that want a non-fatal startup path (the
        server's ``run()``) should catch ``OSError`` themselves, matching the
        existing ``claude-hud-port.json`` write pattern.
        """
        token = secrets.token_urlsafe(32)
        self._token = token
        path = Path(app_dir) / TOKEN_FILENAME
        atomic_write_json(path, {"token": token})
        return token

    def clear(self, app_dir: Path) -> None:
        """Invalidate the in-memory token and best-effort delete its file.

        Called on clean shutdown. A missing file (already removed, or the
        mint write failed earlier) is not an error.
        """
        self._token = None
        path = Path(app_dir) / TOKEN_FILENAME
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def is_valid(self, presented: str | None) -> bool:
        """Constant-time comparison against the current token.

        Returns ``False`` (never raises) when no token has been minted yet,
        when *presented* is empty/``None``, or on any mismatch.
        """
        if not presented or not self._token:
            return False
        return hmac.compare_digest(presented, self._token)

    async def require_capability_token(self, request: Request) -> None:
        """FastAPI dependency: raise 401 unless the header carries a valid token.

        Use as ``Depends(auth.require_capability_token)`` (or the module-level
        ``require_capability_token`` bound to the process-global singleton
        below) on every managed-session route.
        """
        presented = request.headers.get(HEADER_NAME)
        if not self.is_valid(presented):
            raise HTTPException(status_code=401, detail="unauthorized")


# Process-global instance used by the real server. Tests that want isolated
# state should construct their own ManagedSessionAuth() rather than reaching
# for this singleton.
auth = ManagedSessionAuth()


async def require_capability_token(request: Request) -> None:
    """FastAPI dependency bound to the process-global ``auth`` singleton."""
    await auth.require_capability_token(request)
