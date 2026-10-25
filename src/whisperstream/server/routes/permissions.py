"""Explicit permission actions and prompt-free shortcut readiness HTTP routes."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..middleware import _is_trusted_permission_action_request

if TYPE_CHECKING:
    from ...permission_actions import PermissionActionService
    from ...shortcut_recovery import ShortcutRecovery


class PermissionActionBody(BaseModel):
    kind: Literal["microphone", "accessibility", "input-monitoring"]


def register_permission_routes(
    api: FastAPI,
    *,
    current_shortcut_recovery: Callable[[], ShortcutRecovery | None],
    permission_actions: Callable[[], PermissionActionService],
) -> None:
    """Resolve native owners only when an admitted request needs them."""
    @api.get("/api/shortcuts/readiness")
    async def shortcut_readiness():
        recovery = current_shortcut_recovery()
        if recovery is None:
            return {"state": "disabled", "failure": None}
        return await asyncio.to_thread(recovery.snapshot)

    @api.post("/api/shortcuts/reconcile")
    async def reconcile_shortcuts(request: Request):
        if not _is_trusted_permission_action_request(request):
            return JSONResponse(status_code=403, content={"error": "explicit_permission_intent_required"})
        recovery = current_shortcut_recovery()
        if recovery is None:
            return {"state": "disabled", "failure": None}
        # Registers only the active tap. This never invokes a permission
        # requester, opens Settings, or starts microphone capture.
        return await asyncio.to_thread(recovery.reconcile)

    @api.get("/api/permissions")
    async def get_permissions():
        # Importing the shared selector is safe, but keep selection itself
        # behind the explicit query endpoint so ordinary backend/file-only
        # construction never imports a platform permission adapter.
        from ...permission_status import query_permissions  # noqa: PLC0415

        return query_permissions().as_dict()

    @api.post("/api/permissions/request")
    async def request_permission(
        body: PermissionActionBody,
        request: Request,
    ):
        if not _is_trusted_permission_action_request(request):
            return JSONResponse(
                status_code=403,
                content={"error": "explicit_permission_intent_required"},
            )
        from ...permission_contract import PermissionKind  # noqa: PLC0415

        outcome = await asyncio.to_thread(
            permission_actions().request_permission,
            PermissionKind(body.kind),
            explicit_user_action=True,
        )
        return outcome.as_dict()

    @api.post("/api/permissions/recovery")
    async def recover_permission(
        body: PermissionActionBody,
        request: Request,
    ):
        if not _is_trusted_permission_action_request(request):
            return JSONResponse(
                status_code=403,
                content={"error": "explicit_permission_intent_required"},
            )
        from ...permission_contract import PermissionKind  # noqa: PLC0415

        outcome = await asyncio.to_thread(
            permission_actions().open_permission_settings,
            PermissionKind(body.kind),
            explicit_user_action=True,
        )
        return outcome.as_dict()
