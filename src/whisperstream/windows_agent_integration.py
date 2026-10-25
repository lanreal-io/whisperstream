"""Windows Claude plugin and hook transport owners.

Provider CLI paths, plugin roots, hook bodies, session tokens, route identities,
and decision payloads never enter the shared lifecycle result objects.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from .agent_integration_contract import (
    AgentFailure,
    AgentHookAdapter,
    AgentHookDisposition,
    AgentHookEvent,
    AgentHookRequest,
    AgentHookResult,
    AgentInteractionKind,
    AgentLifecycleAction,
    AgentLifecycleRequest,
    AgentLifecycleResult,
    AgentLifecycleState,
    AgentPluginAdapter,
    AgentProvider,
)


class WindowsAgentPluginAdapter:
    """Reconcile the owned Claude plugin through its existing Windows owner."""

    def __init__(
        self,
        *,
        enable: Callable[..., dict[str, Any]] | None = None,
        disable: Callable[[], dict[str, Any]] | None = None,
        reconcile_cli: Callable[[], bool] | None = None,
        pause: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._enable = enable
        self._disable = disable
        self._pause = pause
        self._reconcile_cli = reconcile_cli
        self._lock = threading.RLock()
        self._generation = 0
        self._closed = False
        self._payloads: dict[tuple[str, int], dict[str, Any]] = {}
        self._last_error_type: str | None = None

    @staticmethod
    def _default_enable(**kwargs: object) -> dict[str, Any]:
        from .claude_hud import enable_claude_hud

        return enable_claude_hud(**kwargs)

    @staticmethod
    def _default_disable() -> dict[str, Any]:
        from .claude_hud import disconnect_claude_plugin

        return disconnect_claude_plugin()

    @staticmethod
    def _default_pause() -> dict[str, Any]:
        from .claude_hud import disable_claude_plugin_via_cli

        return disable_claude_plugin_via_cli()

    def _remember(
        self,
        request: AgentLifecycleRequest,
        payload: dict[str, Any],
    ) -> None:
        # Bounded to the latest operation. Paths remain available only to the
        # server's existing public-status sanitizer and are never logged.
        with self._lock:
            self._payloads.clear()
            self._payloads[(request.request_id, request.generation)] = payload

    def payload_for(
        self,
        request_id: str,
        generation: int,
    ) -> dict[str, Any] | None:
        with self._lock:
            payload = self._payloads.get((request_id, generation))
            return dict(payload) if payload is not None else None

    def take_payload(
        self,
        request_id: str,
        generation: int,
    ) -> dict[str, Any] | None:
        """Return then scrub the adapter-private lifecycle payload."""

        with self._lock:
            payload = self._payloads.pop((request_id, generation), None)
            return dict(payload) if payload is not None else None

    @property
    def last_error_type(self) -> str | None:
        with self._lock:
            return self._last_error_type

    def apply(self, request: AgentLifecycleRequest) -> AgentLifecycleResult:
        if request.provider is not AgentProvider.CLAUDE_CODE:
            return AgentLifecycleResult(
                request.request_id,
                request.generation,
                AgentLifecycleState.FAILED,
                AgentFailure.PLUGIN_UNAVAILABLE,
            )
        with self._lock:
            self._last_error_type = None
            if self._closed:
                return AgentLifecycleResult(
                    request.request_id,
                    request.generation,
                    AgentLifecycleState.FAILED,
                    AgentFailure.CLEANUP_FAILED,
                )
            if request.generation < self._generation:
                return AgentLifecycleResult(
                    request.request_id,
                    request.generation,
                    AgentLifecycleState.FAILED,
                    AgentFailure.STALE_GENERATION,
                )
            self._generation = request.generation

        try:
            if request.action is AgentLifecycleAction.RECONCILE:
                options: dict[str, Any] = {
                    "reconcile_cli": self._reconcile_cli() if self._reconcile_cli else True
                }
                if request.force:
                    options["force"] = True
                if request.keep_disabled:
                    options["keep_disabled"] = True
                payload = (self._enable or self._default_enable)(**options)
                self._remember(request, payload)
                if payload.get("ok") is not True:
                    error = str(payload.get("error") or "")
                    failure = (
                        AgentFailure.CLI_UNAVAILABLE
                        if error == "claude_cli_unavailable"
                        else AgentFailure.NATIVE_ERROR
                    )
                    return AgentLifecycleResult(
                        request.request_id,
                        request.generation,
                        AgentLifecycleState.FAILED,
                        failure,
                    )
                state = (
                    AgentLifecycleState.CURRENT
                    if payload.get("status") == "current"
                    else AgentLifecycleState.UPDATED
                )
                return AgentLifecycleResult(
                    request.request_id,
                    request.generation,
                    state,
                )
            if request.action in {AgentLifecycleAction.REMOVE, AgentLifecycleAction.DISABLE}:
                remove = request.action is AgentLifecycleAction.REMOVE
                payload = (
                    (self._disable or self._default_disable)()
                    if remove
                    else (self._pause or self._default_pause)()
                )
                self._remember(request, payload)
                if payload.get("ok") is False:
                    return AgentLifecycleResult(
                        request.request_id,
                        request.generation,
                        AgentLifecycleState.FAILED,
                        AgentFailure.NATIVE_ERROR,
                    )
                return AgentLifecycleResult(
                    request.request_id,
                    request.generation,
                    AgentLifecycleState.REMOVED if remove else AgentLifecycleState.DISABLED,
                )
            if request.action is AgentLifecycleAction.SHUTDOWN:
                self.close()
                return AgentLifecycleResult(
                    request.request_id,
                    request.generation,
                    AgentLifecycleState.REMOVED,
                )
            return AgentLifecycleResult(
                request.request_id,
                request.generation,
                AgentLifecycleState.FAILED,
                AgentFailure.NATIVE_ERROR,
            )
        except Exception as exc:
            with self._lock:
                self._last_error_type = type(exc).__name__
            return AgentLifecycleResult(
                request.request_id,
                request.generation,
                AgentLifecycleState.FAILED,
                AgentFailure.NATIVE_ERROR,
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._payloads.clear()


class WindowsAgentHookAdapter:
    """Bounded, fail-open hook transport with generation-scoped cancellation."""

    _INTERACTIONS = {
        AgentHookEvent.NOTIFICATION: AgentInteractionKind.ACTIVITY,
        AgentHookEvent.PERMISSION_REQUEST: AgentInteractionKind.PERMISSION,
        AgentHookEvent.PRE_TOOL_USE: AgentInteractionKind.QUESTION,
        AgentHookEvent.STOP: AgentInteractionKind.RESPONSE,
        AgentHookEvent.SUBAGENT_STOP: AgentInteractionKind.RESPONSE,
        AgentHookEvent.USER_PROMPT_SUBMIT: AgentInteractionKind.COMPOSE,
    }

    def __init__(
        self,
        *,
        transport: Callable[[str, AgentHookEvent, float], object] | None = None,
    ) -> None:
        self._transport = transport
        self._lock = threading.RLock()
        self._active: dict[str, int] = {}
        self._closed = False

    def forward(self, request: AgentHookRequest) -> AgentHookResult:
        if request.session.provider is not AgentProvider.CLAUDE_CODE:
            return self._fail_open(request, AgentFailure.INVALID_EVENT)
        with self._lock:
            if self._closed or self._transport is None:
                return self._fail_open(request, AgentFailure.ROUTE_UNAVAILABLE)
            previous = self._active.get(request.request_id)
            if previous is not None and request.generation < previous:
                return self._fail_open(request, AgentFailure.STALE_GENERATION)
            self._active[request.request_id] = request.generation
        try:
            # Validate JSON without retaining or normalizing its content here.
            if not isinstance(json.loads(request.body_json), dict):
                return self._fail_open(request, AgentFailure.INVALID_EVENT)
            response = self._transport(
                request.body_json,
                request.event,
                request.deadline_seconds,
            )
            if isinstance(response, dict):
                response_json = json.dumps(response, separators=(",", ":"))
            elif isinstance(response, str):
                response_json = response
                if not isinstance(json.loads(response_json), dict):
                    raise ValueError("hook response must be a JSON object")
            else:
                raise ValueError("hook transport returned an invalid response")
            with self._lock:
                if self._active.get(request.request_id) != request.generation:
                    return AgentHookResult(
                        request.request_id,
                        request.generation,
                        AgentHookDisposition.EXPIRED,
                    )
                self._active.pop(request.request_id, None)
            return AgentHookResult(
                request.request_id,
                request.generation,
                (
                    AgentHookDisposition.FORWARDED
                    if response_json == "{}"
                    else AgentHookDisposition.RESOLVED
                ),
                self._INTERACTIONS[request.event],
                response_json,
            )
        except TimeoutError:
            return self._fail_open(request, AgentFailure.TIMEOUT)
        except (json.JSONDecodeError, ValueError):
            return self._fail_open(request, AgentFailure.INVALID_EVENT)
        except Exception:
            return self._fail_open(request, AgentFailure.ROUTE_UNAVAILABLE)
        finally:
            with self._lock:
                if self._active.get(request.request_id) == request.generation:
                    self._active.pop(request.request_id, None)

    @staticmethod
    def _fail_open(
        request: AgentHookRequest,
        failure: AgentFailure,
    ) -> AgentHookResult:
        return AgentHookResult(
            request.request_id,
            request.generation,
            AgentHookDisposition.FAILED_OPEN,
            failure=failure,
        )

    def cancel(self, request_id: str, generation: int) -> AgentHookResult:
        with self._lock:
            if self._active.get(request_id) == generation:
                self._active.pop(request_id, None)
        return AgentHookResult(
            request_id,
            generation,
            AgentHookDisposition.EXPIRED,
        )

    def cancel_all(self) -> None:
        """Expire every adapter-owned route without closing future forwarding."""

        with self._lock:
            self._active.clear()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._active.clear()


assert isinstance(WindowsAgentPluginAdapter(), AgentPluginAdapter)
assert isinstance(WindowsAgentHookAdapter(), AgentHookAdapter)


__all__ = ["WindowsAgentHookAdapter", "WindowsAgentPluginAdapter"]
