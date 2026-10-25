"""Apple Silicon Claude plugin and fail-open hook transport owners.

Claude CLI processes, plugin roots, hook bodies, session tokens, routes, and
decision payloads remain private to these adapters. Shared results expose only
positive generations and content-free lifecycle state.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
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


class MacOSAgentPluginAdapter:
    """Use only Claude's supported CLI for user plugin/cache registration."""

    def __init__(
        self,
        *,
        app_dir: Path | None = None,
        reconcile: Callable[[Path], dict[str, Any]] | None = None,
        remove: Callable[[Path], dict[str, Any]] | None = None,
        cli_runner: Callable[[str, list[str]], object] | None = None,
        reconcile_cli: Callable[[], bool] | None = None,
    ) -> None:
        self._app_dir = app_dir
        self._reconcile = reconcile
        self._remove = remove
        self._cli_runner = cli_runner
        self._reconcile_cli = reconcile_cli
        self._lock = threading.RLock()
        self._generation = 0
        self._closed = False
        self._payloads: dict[tuple[str, int], dict[str, Any]] = {}
        self._last_error_type: str | None = None
        self._cli_processes: set[subprocess.Popen[str]] = set()

    def _resolved_app_dir(self) -> Path:
        if self._app_dir is not None:
            return self._app_dir
        from .config_manager import get_app_dir

        return get_app_dir()

    def _run_cli(self, executable: str, arguments: list[str]):
        if self._cli_runner is not None:
            return self._cli_runner(executable, arguments)
        process = subprocess.Popen(
            [executable, "plugin", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        with self._lock:
            if self._closed:
                self._terminate_cli_process(process)
            self._cli_processes.add(process)
        try:
            stdout, stderr = process.communicate(timeout=20.0)
            return subprocess.CompletedProcess(
                process.args, process.returncode, stdout, stderr
            )
        except subprocess.TimeoutExpired:
            self._terminate_cli_process(process)
            try:
                process.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                process.communicate()
            raise
        finally:
            with self._lock:
                self._cli_processes.discard(process)

    @staticmethod
    def _terminate_cli_process(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    def _default_reconcile(
        self, app_dir: Path, *, force: bool = False, keep_disabled: bool = False
    ) -> dict[str, Any]:
        from .claude_hud import install_claude_plugin, reconcile_claude_plugin

        if self._reconcile_cli is not None and not self._reconcile_cli():
            from .claude_hud import enable_claude_hud

            return enable_claude_hud(hook_platform="darwin", keep_disabled=keep_disabled)
        materialized = install_claude_plugin(app_dir, hook_platform="darwin")
        return reconcile_claude_plugin(
            app_dir,
            install_result=materialized,
            hook_platform="darwin",
            cli_runner=self._run_cli,
            force=force,
            keep_disabled=keep_disabled,
        )

    def _default_pause(self) -> dict[str, Any]:
        from .claude_hud import disable_claude_plugin_via_cli

        return disable_claude_plugin_via_cli(cli_runner=self._run_cli)

    def _default_remove(self, app_dir: Path) -> dict[str, Any]:
        from .claude_hud import remove_claude_plugin_via_cli

        return remove_claude_plugin_via_cli(
            app_dir,
            hook_platform="darwin",
            cli_runner=self._run_cli,
        )

    @property
    def last_error_type(self) -> str | None:
        with self._lock:
            return self._last_error_type

    def take_payload(self, request_id: str, generation: int) -> dict[str, Any] | None:
        with self._lock:
            payload = self._payloads.pop((request_id, generation), None)
            return dict(payload) if payload is not None else None

    def _remember(
        self, request: AgentLifecycleRequest, payload: dict[str, Any]
    ) -> None:
        with self._lock:
            self._payloads.clear()
            self._payloads[(request.request_id, request.generation)] = payload

    @staticmethod
    def _failure(payload: dict[str, Any]) -> AgentFailure:
        return (
            AgentFailure.CLI_UNAVAILABLE
            if payload.get("error") == "claude_cli_unavailable"
            else AgentFailure.NATIVE_ERROR
        )

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
            app_dir = self._resolved_app_dir()
            if request.action is AgentLifecycleAction.RECONCILE:
                if self._reconcile is not None:
                    payload = self._reconcile(app_dir)
                else:
                    payload = self._default_reconcile(
                        app_dir, force=request.force, keep_disabled=request.keep_disabled
                    )
                self._remember(request, payload)
                if payload.get("ok") is not True:
                    return AgentLifecycleResult(
                        request.request_id,
                        request.generation,
                        AgentLifecycleState.FAILED,
                        self._failure(payload),
                    )
                state = (
                    AgentLifecycleState.CURRENT
                    if payload.get("status") == "current"
                    else AgentLifecycleState.UPDATED
                )
                return AgentLifecycleResult(
                    request.request_id, request.generation, state
                )
            if request.action is AgentLifecycleAction.DISABLE:
                payload = self._default_pause()
                self._remember(request, payload)
                if payload.get("ok") is not True:
                    return AgentLifecycleResult(
                        request.request_id,
                        request.generation,
                        AgentLifecycleState.FAILED,
                        self._failure(payload),
                    )
                return AgentLifecycleResult(
                    request.request_id, request.generation, AgentLifecycleState.DISABLED
                )
            if request.action is AgentLifecycleAction.REMOVE:
                payload = (self._remove or self._default_remove)(app_dir)
                self._remember(request, payload)
                if payload.get("ok") is not True:
                    return AgentLifecycleResult(
                        request.request_id,
                        request.generation,
                        AgentLifecycleState.FAILED,
                        self._failure(payload),
                    )
                return AgentLifecycleResult(
                    request.request_id,
                    request.generation,
                    AgentLifecycleState.REMOVED,
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
            self._closed = True
            self._payloads.clear()
            processes = tuple(self._cli_processes)
        for process in processes:
            self._terminate_cli_process(process)


class MacOSAgentHookAdapter:
    """Bounded Darwin route owner; every failure returns exact empty JSON."""

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
            if not isinstance(json.loads(request.body_json), dict):
                return self._fail_open(request, AgentFailure.INVALID_EVENT)
            response = self._transport(
                request.body_json, request.event, request.deadline_seconds
            )
            if isinstance(response, dict):
                response_json = json.dumps(response, separators=(",", ":"))
            elif isinstance(response, str):
                response_json = response
                if not isinstance(json.loads(response_json), dict):
                    raise ValueError("hook response must be a JSON object")
            else:
                raise ValueError("hook transport returned an invalid response")
            # Notifications are passive compatibility signals. Even a broken
            # transport implementation cannot promote them to decision output.
            if request.event is AgentHookEvent.NOTIFICATION:
                response_json = "{}"
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
                AgentHookDisposition.FORWARDED
                if response_json == "{}"
                else AgentHookDisposition.RESOLVED,
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
        request: AgentHookRequest, failure: AgentFailure
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
            request_id, generation, AgentHookDisposition.EXPIRED
        )

    def cancel_all(self) -> None:
        """Scrub routes on disable, Lockdown, provider exit, or barge-in."""
        with self._lock:
            self._active.clear()

    def provider_exited(self) -> None:
        self.cancel_all()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._active.clear()


assert isinstance(MacOSAgentPluginAdapter(), AgentPluginAdapter)
assert isinstance(MacOSAgentHookAdapter(), AgentHookAdapter)


__all__ = ["MacOSAgentHookAdapter", "MacOSAgentPluginAdapter"]
