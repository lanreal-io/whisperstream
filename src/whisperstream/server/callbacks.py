"""Keyword-sensitive callbacks shared by server operation and route owners."""
from __future__ import annotations

from typing import Any, Protocol

from ..eula import AcceptanceGrant


class EventBroadcaster(Protocol):
    """Publish an event, optionally bound to one captured acceptance grant."""

    def __call__(
        self,
        event: dict[str, Any],
        *,
        acceptance_grant: AcceptanceGrant | None = None,
    ) -> None:
        # TRANSCRIPT DATA - DO NOT LOG. Events may contain transcript content.
        ...
