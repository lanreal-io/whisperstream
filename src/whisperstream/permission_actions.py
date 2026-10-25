"""Lazy orchestration for explicit permission request and recovery actions.

The public status selector lives in :mod:`permission_status` and never imports
this module.  Action intents are minted and consumed inside the backend process
after a trusted caller supplies the explicit-user-action marker; intent tokens
never cross the HTTP boundary or enter serialized outcomes.
"""

from __future__ import annotations

import importlib
import platform
import threading
from dataclasses import dataclass
from typing import Callable

from .permission_contract import (
    PermissionAction,
    PermissionActionFailure,
    PermissionActionOutcome,
    PermissionActionStatus,
    PermissionIntentRegistry,
    PermissionKind,
    PermissionRecoveryProvider,
    PermissionRequester,
    PermissionState,
)


@dataclass(frozen=True, slots=True)
class PermissionActionProviders:
    requester: PermissionRequester
    recovery: PermissionRecoveryProvider


def _failure_outcome(
    kind: PermissionKind,
    action: PermissionAction,
    failure: PermissionActionFailure,
    *,
    unsupported: bool = False,
) -> PermissionActionOutcome:
    return PermissionActionOutcome(
        kind=kind,
        action=action,
        status=(
            PermissionActionStatus.UNSUPPORTED
            if unsupported
            else PermissionActionStatus.FAILED
        ),
        before=PermissionState.UNKNOWN,
        after=PermissionState.UNKNOWN,
        failure=failure,
    )


class _UnavailablePermissionRequester:
    def __init__(self, registry: PermissionIntentRegistry) -> None:
        self._registry = registry

    def request_permission(self, intent) -> PermissionActionOutcome:
        self._registry.consume(intent)
        return _failure_outcome(
            intent.kind,
            PermissionAction.REQUEST,
            PermissionActionFailure.NOT_REQUESTABLE,
            unsupported=True,
        )


class _UnavailablePermissionRecoveryProvider:
    def __init__(self, registry: PermissionIntentRegistry) -> None:
        self._registry = registry

    def open_permission_settings(self, intent) -> PermissionActionOutcome:
        self._registry.consume(intent)
        return _failure_outcome(
            intent.kind,
            PermissionAction.OPEN_SETTINGS,
            PermissionActionFailure.RECOVERY_UNAVAILABLE,
            unsupported=True,
        )


def _unavailable_providers(
    registry: PermissionIntentRegistry,
) -> PermissionActionProviders:
    return PermissionActionProviders(
        requester=_UnavailablePermissionRequester(registry),
        recovery=_UnavailablePermissionRecoveryProvider(registry),
    )


def _load_providers(
    registry: PermissionIntentRegistry,
    *,
    system: str,
) -> PermissionActionProviders:
    if system != "Darwin":
        return _unavailable_providers(registry)
    try:
        module = importlib.import_module(
            ".macos_permission_actions", package=__package__
        )
        factory = getattr(module, "make_macos_permission_action_providers")
        providers = factory(registry)
    except Exception:  # noqa: BLE001 - lazy native action boundary
        return _unavailable_providers(registry)
    if not isinstance(providers, PermissionActionProviders):
        return _unavailable_providers(registry)
    if not isinstance(providers.requester, PermissionRequester):
        return _unavailable_providers(registry)
    if not isinstance(providers.recovery, PermissionRecoveryProvider):
        return _unavailable_providers(registry)
    return providers


class PermissionActionService:
    """Mint one local intent for one explicitly initiated action."""

    def __init__(
        self,
        *,
        system: str | None = None,
        registry: PermissionIntentRegistry | None = None,
        provider_factory: Callable[
            [PermissionIntentRegistry], PermissionActionProviders
        ]
        | None = None,
    ) -> None:
        self._system = platform.system() if system is None else system
        self._registry = registry or PermissionIntentRegistry()
        self._provider_factory = provider_factory
        self._providers: PermissionActionProviders | None = None
        self._providers_lock = threading.Lock()

    def _action_providers(self) -> PermissionActionProviders:
        providers = self._providers
        if providers is not None:
            return providers
        with self._providers_lock:
            providers = self._providers
            if providers is None:
                if self._provider_factory is None:
                    providers = _load_providers(self._registry, system=self._system)
                else:
                    providers = self._provider_factory(self._registry)
                if not isinstance(providers, PermissionActionProviders):
                    providers = _unavailable_providers(self._registry)
                self._providers = providers
            return providers

    def request_permission(
        self,
        kind: PermissionKind,
        *,
        explicit_user_action: bool,
    ) -> PermissionActionOutcome:
        if explicit_user_action is not True:
            return _failure_outcome(
                kind,
                PermissionAction.REQUEST,
                PermissionActionFailure.INTENT_REQUIRED,
            )
        intent = self._registry.issue(kind, PermissionAction.REQUEST)
        try:
            return self._action_providers().requester.request_permission(intent)
        except Exception:  # noqa: BLE001 - never expose native action detail
            return _failure_outcome(
                kind,
                PermissionAction.REQUEST,
                PermissionActionFailure.NATIVE_ERROR,
            )

    def open_permission_settings(
        self,
        kind: PermissionKind,
        *,
        explicit_user_action: bool,
    ) -> PermissionActionOutcome:
        if explicit_user_action is not True:
            return _failure_outcome(
                kind,
                PermissionAction.OPEN_SETTINGS,
                PermissionActionFailure.INTENT_REQUIRED,
            )
        intent = self._registry.issue(kind, PermissionAction.OPEN_SETTINGS)
        try:
            return self._action_providers().recovery.open_permission_settings(intent)
        except Exception:  # noqa: BLE001 - never expose native action detail
            return _failure_outcome(
                kind,
                PermissionAction.OPEN_SETTINGS,
                PermissionActionFailure.NATIVE_ERROR,
            )


__all__ = ["PermissionActionProviders", "PermissionActionService"]
