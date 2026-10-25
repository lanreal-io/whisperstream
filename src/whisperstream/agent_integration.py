"""Lazy platform selection for the frozen agent-integration contract."""

from __future__ import annotations

import platform
from collections.abc import Callable

from .agent_integration_contract import (
    AgentFailure,
    AgentHookAdapter,
    AgentHookDisposition,
    AgentHookRequest,
    AgentHookResult,
    AgentLifecycleRequest,
    AgentLifecycleResult,
    AgentLifecycleState,
    AgentPluginAdapter,
)


class UnavailableAgentPluginAdapter:
    def apply(self, request: AgentLifecycleRequest) -> AgentLifecycleResult:
        return AgentLifecycleResult(
            request.request_id,
            request.generation,
            AgentLifecycleState.FAILED,
            AgentFailure.PLUGIN_UNAVAILABLE,
        )

    def close(self) -> None:
        return None


class UnavailableAgentHookAdapter:
    def forward(self, request: AgentHookRequest) -> AgentHookResult:
        return AgentHookResult(
            request.request_id,
            request.generation,
            AgentHookDisposition.FAILED_OPEN,
            failure=AgentFailure.ROUTE_UNAVAILABLE,
        )

    def cancel(self, request_id: str, generation: int) -> AgentHookResult:
        return AgentHookResult(
            request_id,
            generation,
            AgentHookDisposition.EXPIRED,
        )

    def close(self) -> None:
        return None

    def cancel_all(self) -> None:
        return None


def create_agent_integration_adapters(
    *,
    transport: Callable[..., object] | None = None,
    reconcile_cli: Callable[[], bool] | None = None,
    system: str | None = None,
    machine: str | None = None,
) -> tuple[AgentPluginAdapter, AgentHookAdapter]:
    resolved_system = system or platform.system()
    resolved_machine = (machine or platform.machine()).lower()
    if resolved_system == "Windows" and resolved_machine in {"amd64", "x86_64"}:
        from .windows_agent_integration import (
            WindowsAgentHookAdapter,
            WindowsAgentPluginAdapter,
        )

        return (
            WindowsAgentPluginAdapter(reconcile_cli=reconcile_cli),
            WindowsAgentHookAdapter(transport=transport),
        )
    if resolved_system == "Darwin" and resolved_machine in {"arm64", "aarch64"}:
        from .macos_agent_integration import (
            MacOSAgentHookAdapter,
            MacOSAgentPluginAdapter,
        )

        return (
            MacOSAgentPluginAdapter(reconcile_cli=reconcile_cli),
            MacOSAgentHookAdapter(transport=transport),
        )
    return UnavailableAgentPluginAdapter(), UnavailableAgentHookAdapter()


assert isinstance(UnavailableAgentPluginAdapter(), AgentPluginAdapter)
assert isinstance(UnavailableAgentHookAdapter(), AgentHookAdapter)


__all__ = [
    "UnavailableAgentHookAdapter",
    "UnavailableAgentPluginAdapter",
    "create_agent_integration_adapters",
]
