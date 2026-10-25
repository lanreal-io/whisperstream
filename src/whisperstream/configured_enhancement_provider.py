"""Concrete cloud and Ollama adapters for the daily-use provider contract.

The adapter owns the bounded request task and its cancellation generation.  A
caller supplies the provider-specific HTTP transport as a closure, which keeps
API keys and endpoint strings inside the existing configuration/HTTP owner.
Neither value is copied into the native-free request or outcome records.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from .enhancement_provider_contract import (
    EnhancementFailure,
    EnhancementOutcome,
    EnhancementProviderKind,
    EnhancementProviderPolicy,
    EnhancementRequest,
    provider_policy,
)


ProviderTransport = Callable[[EnhancementRequest], Awaitable[str]]


class ProviderTransportFailure(RuntimeError):
    """Content-free typed failure raised by an HTTP transport."""

    def __init__(
        self,
        failure: EnhancementFailure,
        safe_detail: str | None = None,
    ) -> None:
        if failure in (EnhancementFailure.NONE, EnhancementFailure.CANCELLED):
            raise ValueError("transport failure must describe a provider error")
        self.failure = failure
        super().__init__(safe_detail or failure.value)


class EnhancementProviderFailure(RuntimeError):
    """Content-free failure used by the legacy enhancer raw-fallback boundary."""

    def __init__(self, failure: EnhancementFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


class ConfiguredEnhancementProviderAdapter:
    """Run exactly one attempt per request and preserve raw-only failure.

    The adapter deliberately accepts only availability booleans for endpoint
    and credential state.  It therefore cannot retain or expose either the
    protected secret or the configured endpoint.
    """

    def __init__(
        self,
        provider_id: str,
        kind: EnhancementProviderKind,
        transport: ProviderTransport,
        *,
        credential_available: bool,
        endpoint_available: bool,
        lockdown: bool,
    ) -> None:
        self._provider_id = provider_id
        self._policy = provider_policy(kind)
        self._transport = transport
        self._credential_available = credential_available
        self._endpoint_available = endpoint_available
        self._lockdown = lockdown
        self._active: dict[str, asyncio.Task[str]] = {}

    @property
    def policy(self) -> EnhancementProviderPolicy:
        return self._policy

    @staticmethod
    def _raw(request: EnhancementRequest, failure: EnhancementFailure) -> EnhancementOutcome:
        return EnhancementOutcome(
            request_id=request.request_id,
            failure=failure,
            use_raw=True,
        )

    async def enhance(self, request: EnhancementRequest) -> EnhancementOutcome:
        if request.provider_id != self._provider_id or request.provider_kind is not self._policy.kind:
            return self._raw(request, EnhancementFailure.UNSUPPORTED)
        if self._lockdown and not self._policy.allowed_in_lockdown:
            return self._raw(request, EnhancementFailure.BLOCKED_BY_POLICY)
        if self._policy.requires_protected_secret and not self._credential_available:
            return self._raw(request, EnhancementFailure.CREDENTIAL_UNAVAILABLE)
        if self._policy.requires_endpoint and not self._endpoint_available:
            return self._raw(request, EnhancementFailure.ENDPOINT_UNAVAILABLE)
        if request.request_id in self._active:
            return self._raw(request, EnhancementFailure.UNSUPPORTED)

        task = asyncio.create_task(self._transport(request))
        self._active[request.request_id] = task
        try:
            enhanced = await asyncio.wait_for(task, timeout=request.timeout_seconds)
        except asyncio.TimeoutError:
            return self._raw(request, EnhancementFailure.TIMEOUT)
        except asyncio.CancelledError:
            return self._raw(request, EnhancementFailure.CANCELLED)
        except ProviderTransportFailure as exc:
            return self._raw(request, exc.failure)
        except httpx.TimeoutException:
            return self._raw(request, EnhancementFailure.TIMEOUT)
        except httpx.HTTPError:
            return self._raw(request, EnhancementFailure.HTTP)
        except (KeyError, TypeError, ValueError):
            return self._raw(request, EnhancementFailure.INVALID_RESPONSE)
        except Exception:
            return self._raw(request, EnhancementFailure.INVALID_RESPONSE)
        finally:
            self._active.pop(request.request_id, None)

        if not isinstance(enhanced, str):
            return self._raw(request, EnhancementFailure.INVALID_RESPONSE)
        enhanced = enhanced.strip()
        if not enhanced:
            return self._raw(request, EnhancementFailure.EMPTY_RESPONSE)
        return EnhancementOutcome(
            request_id=request.request_id,
            failure=EnhancementFailure.NONE,
            use_raw=False,
            enhanced_text=enhanced,
        )

    async def cancel(self, request_id: str) -> EnhancementOutcome:
        task = self._active.get(request_id)
        if task is not None and not task.done():
            task.cancel()
        return EnhancementOutcome(
            request_id=request_id,
            failure=EnhancementFailure.CANCELLED,
            use_raw=True,
        )
