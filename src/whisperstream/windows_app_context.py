"""Windows owner for app/browser context, inventories, icons, and favicons.

Process objects, HWND/PID values, UI Automation objects, executable paths, raw
address-bar values, and icon bytes remain inside the existing Windows helpers
called by this adapter.  The frozen contract receives only logical app identity,
an optional host-only page identity, and opaque display-asset tokens.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .app_context_contract import (
    ApplicationContextAdapter,
    ApplicationDescriptor,
    ApplicationInventoryScope,
    ApplicationRef,
    BrowserPageRef,
    ContextFailure,
    ContextRequirement,
    ContextSnapshot,
    ContextStatus,
)
from .app_directory import (
    list_installed_apps,
    resolve_app_by_path,
    resolve_apps,
)
from .browser_domain import is_known_browser, uia_available
from .browser_favicons import (
    favicon_data_uri_for_domain,
    normalize_favicon_domain,
)
from .foreground_window import detect_foreground_routing_target
from .system_info import list_running_apps


Inventory = Callable[[], list[dict[str, Any]]]
RoutingProbe = Callable[..., object | None]
_MAX_ICON_ASSETS = 512


def _asset_token(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"windows-{kind}:sha256:{digest}"


class WindowsApplicationContextAdapter:
    """Best-effort Windows context capture with adapter-private UI metadata."""

    def __init__(
        self,
        *,
        routing_probe: RoutingProbe = detect_foreground_routing_target,
        running_inventory: Inventory = list_running_apps,
        installed_inventory: Inventory = list_installed_apps,
        app_resolver: Callable[..., list[dict[str, Any]]] = resolve_apps,
        path_resolver: Callable[[str], dict[str, Any] | None] = resolve_app_by_path,
        favicon_resolver: Callable[[str | None], str | None] = (
            favicon_data_uri_for_domain
        ),
        domain_normalizer: Callable[[object], str | None] = normalize_favicon_domain,
        browser_classifier: Callable[[str | None], bool] = is_known_browser,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._routing_probe = routing_probe
        self._running_inventory = running_inventory
        self._installed_inventory = installed_inventory
        self._app_resolver = app_resolver
        self._path_resolver = path_resolver
        self._favicon_resolver = favicon_resolver
        self._domain_normalizer = domain_normalizer
        self._browser_classifier = browser_classifier
        self._token_factory = token_factory or (lambda: uuid.uuid4().hex)
        self._lock = threading.RLock()
        self._assets: OrderedDict[str, str] = OrderedDict()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def capture(
        self,
        generation: int,
        requirement: ContextRequirement,
    ) -> ContextSnapshot:
        """Capture process identity once and opt into browser UIA only as needed."""

        if self.closed:
            return ContextSnapshot(
                generation,
                requirement,
                ContextStatus.FAILED,
                failure=ContextFailure.NATIVE_ERROR,
            )
        try:
            target = self._routing_probe(
                include_browser_domain=(
                    requirement is ContextRequirement.APPLICATION_AND_BROWSER_PAGE
                )
            )
        except Exception:  # noqa: BLE001 - foreground capture never breaks dictation
            target = None
        process_name = str(getattr(target, "process_name", "") or "").strip().lower()
        if not process_name:
            return ContextSnapshot(
                generation,
                requirement,
                ContextStatus.UNAVAILABLE,
                failure=ContextFailure.NO_FOREGROUND_APPLICATION,
            )

        application = ApplicationRef(
            process_name,
            f"windows-app-instance:{self._token_factory()}",
        )
        domain = getattr(target, "domain", None)
        if isinstance(domain, str):
            domain = domain.strip().lower()
        else:
            domain = None
        if (
            requirement is ContextRequirement.APPLICATION_AND_BROWSER_PAGE
            and domain
        ):
            try:
                page = BrowserPageRef(application, domain)
            except ValueError:
                page = None
            if page is not None:
                return ContextSnapshot(
                    generation,
                    requirement,
                    ContextStatus.RESOLVED,
                    application,
                    page,
                )
        return ContextSnapshot(
            generation,
            requirement,
            (
                ContextStatus.APPLICATION_ONLY
                if requirement
                is ContextRequirement.APPLICATION_AND_BROWSER_PAGE
                else ContextStatus.RESOLVED
            ),
            application,
        )

    def _rows(self, scope: ApplicationInventoryScope) -> list[dict[str, Any]]:
        if self.closed:
            return []
        try:
            source = (
                self._running_inventory
                if scope is ApplicationInventoryScope.RUNNING
                else self._installed_inventory
            )
            rows = source()
            return [dict(row) for row in rows if isinstance(row, Mapping)]
        except Exception:  # noqa: BLE001 - picker inventory is best effort
            return []

    def inventory(
        self,
        scope: ApplicationInventoryScope,
    ) -> tuple[ApplicationDescriptor, ...]:
        descriptors: list[ApplicationDescriptor] = []
        for row in self._rows(scope):
            logical_id = str(row.get("process_name") or "").strip().lower()
            display_name = str(
                row.get("display_name") or row.get("name") or ""
            ).strip()
            if not logical_id or not display_name:
                continue
            icon = row.get("icon")
            icon_token = None
            if isinstance(icon, str) and icon:
                icon_token = _asset_token("icon", icon)
                with self._lock:
                    if not self._closed:
                        self._assets[icon_token] = icon
                        self._assets.move_to_end(icon_token)
                        while len(self._assets) > _MAX_ICON_ASSETS:
                            self._assets.popitem(last=False)
            descriptors.append(
                ApplicationDescriptor(
                    ApplicationRef(
                        logical_id,
                        f"windows-inventory:{scope.value}:{self._token_factory()}",
                    ),
                    display_name,
                    icon_token,
                )
            )
        return tuple(descriptors)

    def icon_payload(self, token: str) -> str | None:
        with self._lock:
            payload = None if self._closed else self._assets.get(token)
            if payload is not None:
                self._assets.move_to_end(token)
            return payload

    # The following adapter-owned presentation methods preserve the existing
    # REST payload exactly. Paths and data-URI bytes do not enter the shared
    # ContextSnapshot/ApplicationDescriptor contract.
    def running_apps(self) -> list[dict[str, object]]:
        return self._rows(ApplicationInventoryScope.RUNNING)

    def installed_apps(self) -> list[dict[str, object]]:
        return self._rows(ApplicationInventoryScope.INSTALLED)

    def resolve_apps(
        self,
        names: Iterable[object] | None,
        *,
        known_paths: Mapping[str, str] | None = None,
        default_names: set[str] | None = None,
    ) -> list[dict[str, object]]:
        if self.closed:
            return []
        try:
            return self._app_resolver(
                names,
                self.running_apps(),
                known_paths=known_paths or {},
                default_names=default_names or set(),
            )
        except Exception:  # noqa: BLE001 - picker resolution is best effort
            return []

    def resolve_path(self, path: str) -> dict[str, object] | None:
        if self.closed:
            return None
        try:
            return self._path_resolver(path)
        except Exception:  # noqa: BLE001 - browse resolution is best effort
            return None

    def resolve_favicons(self, domains: Iterable[object]) -> list[dict[str, object]]:
        if self.closed:
            return []
        out: list[dict[str, object]] = []
        try:
            for raw in domains:
                domain = self._domain_normalizer(raw)
                if domain is None:
                    continue
                icon = self._favicon_resolver(domain)
                out.append({"domain": domain, "icon": icon})
        except Exception:  # noqa: BLE001 - local cache reads never break settings
            return []
        return out

    def safe_capture_metadata(self, snapshot: ContextSnapshot) -> dict[str, object]:
        application = snapshot.application
        browser = bool(
            application is not None
            and self._browser_classifier(application.logical_id)
        )
        return {
            "browser": browser,
            "domain_read": (
                "n/a"
                if not browser
                else "hit" if snapshot.browser_page is not None else "miss"
            ),
        }

    def uia_available(self) -> bool:
        return bool(uia_available())

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._assets.clear()
        # These bounded caches contain potentially confidential local metadata.
        # The Windows app has one adapter owner, so release them at adapter close.
        try:
            from . import app_directory, app_icons, browser_favicons

            app_icons._ICON_CACHE.clear()
            browser_favicons._FAVICON_CACHE.clear()
            app_directory._START_MENU_INDEX = None
            app_directory._START_MENU_INDEX_EXPIRES_AT = 0.0
            app_directory._INSTALLED_INDEX = None
            app_directory._INSTALLED_INDEX_EXPIRES_AT = 0.0
            app_directory._INSTALLED_APPS = None
            app_directory._INSTALLED_APPS_EXPIRES_AT = 0.0
        except Exception:  # noqa: BLE001 - close remains idempotent/best effort
            pass


assert isinstance(WindowsApplicationContextAdapter(), ApplicationContextAdapter)


__all__ = ["WindowsApplicationContextAdapter"]
