"""Content-free version and local application inventory endpoints."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import FastAPI
from pydantic import BaseModel, Field, StringConstraints

from ...app_context_contract import ApplicationContextAdapter
from ...config_manager import WhisperstreamConfig

class ResolveAppsBody(BaseModel):
    names: list[str] = []


class ResolveAppPathBody(BaseModel):
    path: str = ""


_MAX_SITE_ICON_DOMAINS = 128
_MAX_SITE_ICON_HOST_LENGTH = 253
_SiteIconHost = Annotated[
    str,
    StringConstraints(max_length=_MAX_SITE_ICON_HOST_LENGTH),
]


class SiteIconsBody(BaseModel):
    domains: list[_SiteIconHost] = Field(
        default_factory=list,
        max_length=_MAX_SITE_ICON_DOMAINS,
    )


def register_version_route(api: FastAPI, software_version: str) -> None:
    @api.get("/api/version")
    async def get_version():
        # Use software_version (build-time stamped via _version.py, with
        # tauri.conf.json fallback for dev) so the About page matches the
        # shipped version even when pyproject.toml drifts.
        return {"version": software_version}

def register_application_inventory_routes(
    api: FastAPI,
    *,
    application_context: ApplicationContextAdapter,
    current_config: Callable[[], WhisperstreamConfig],
    logger: logging.Logger,
) -> None:
    @api.get("/api/apps/running")
    def get_running_apps():
        # Each entry: {"process_name": str, "display_name": str,
        # "icon": str|None} where icon is a base64 PNG data-URI
        # ("data:image/png;base64,...") or null when unavailable.
        try:
            return application_context.running_apps()
        except Exception as exc:
            logger.warning("running apps list failed reason=%s", type(exc).__name__)
            return []

    @api.post("/api/apps/resolve")
    def resolve_apps_endpoint(body: ResolveAppsBody):
        # Each entry: {"process_name": str, "installed": bool,
        # "display_name": str, "icon": str|None}. Install-aware gating for
        # the profile editor: an uninstalled mapped app resolves to
        # installed=False (hidden by the frontend); routing is never
        # touched here. Never logs names or icon bytes.
        try:
            from ...config_manager import _DEFAULT_POWER_MODE_MAP

            cfg = current_config()
            return application_context.resolve_apps(
                body.names,
                known_paths=getattr(cfg, "power_mode_paths", {}) or {},
                default_names=set(_DEFAULT_POWER_MODE_MAP),
            )
        except Exception as exc:
            logger.warning("apps resolve failed reason=%s", type(exc).__name__)
            return []

    @api.get("/api/apps/installed")
    def get_installed_apps():
        # Each entry: {"process_name": str, "display_name": str,
        # "icon": str|None, "installed": True}. The full installed-app list
        # for the Power Mode picker (Start Menu + App Paths + Uninstall
        # registry). Module-cached after the first build. Never logs names or
        # icon bytes; degrades to [] on any failure.
        try:
            return application_context.installed_apps()
        except Exception as exc:
            logger.warning("installed apps list failed reason=%s", type(exc).__name__)
            return []

    @api.post("/api/apps/resolve-path")
    def resolve_app_path_endpoint(body: ResolveAppPathBody):
        # Resolve a user-browsed exe path to {"process_name", "display_name",
        # "icon", "installed": True}, or null for a missing / non-exe path.
        # Backs the picker's "Browse..." affordance. Never logs the path or
        # icon bytes.
        try:
            return application_context.resolve_path(body.path)
        except Exception as exc:
            logger.warning("apps resolve-path failed reason=%s", type(exc).__name__)
            return None

    @api.post("/api/sites/icons")
    def resolve_site_icons_endpoint(body: SiteIconsBody):
        # Each entry: {"domain": str, "icon": str|None} where icon is a
        # favicon data-URI ("data:image/<png|x-icon|svg+xml>;base64,...")
        # read LOCALLY from the installed browsers' favicon caches (NO
        # network call), or null when no cached favicon exists. Mirrors the
        # /api/apps/resolve never-raise -> [] guard. Never logs domains or
        # icon bytes.
        try:
            return application_context.resolve_favicons(body.domains)
        except Exception as exc:
            logger.warning("site icons resolve failed reason=%s", type(exc).__name__)
            return []
