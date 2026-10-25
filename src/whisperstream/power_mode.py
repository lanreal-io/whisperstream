"""Per-app and per-site enhancement-profile routing.

Exports two pure helpers used by the dictation flow to pick which cleanup
profile runs:

- ``resolve_profile_for_app(app, config)`` — resolves by foreground app's
  process name against ``config.power_mode_map``.
- ``resolve_profile_for_target(app, config)`` — domain-aware entry point
  implementing the locked precedence **site -> app -> default**: if the
  foreground app carries a registrable domain that maps to an existing profile
  in ``config.site_map``, that profile wins; otherwise delegates to
  ``resolve_profile_for_app``.

A ``None`` return from either helper means "use the global default profile"
(``default_profile_id``); the caller threads the result through
``enhance(text, *, profile_id=...)``, where ``None`` already falls back to the
default.

This is a leaf module: it imports ONLY the dataclass/config types (and only
under ``TYPE_CHECKING``), never the enhancer, so it stays import-light and
circular-safe. Both functions are pure and never raise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .app_context_contract import (
    ContextSnapshot,
    EnhancementProfileRoute,
    ProfileRouteTier,
)

if TYPE_CHECKING:
    from .config_manager import WhisperstreamConfig
    from .foreground_window import RoutingTarget


def routing_target_application_id(app: object | None) -> str | None:
    """Return one adapter-supplied logical application key.

    ``RoutingTarget`` support is retained for compatibility tests and older
    utility callers. Production recording-start capture supplies the frozen
    ``ContextSnapshot`` boundary.
    """

    try:
        if isinstance(app, ContextSnapshot):
            value = app.application.logical_id if app.application else None
        else:
            value = getattr(app, "process_name", None)
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip().lower()
    except Exception:  # noqa: BLE001 - routing identity is best effort
        return None


def routing_target_host(app: object | None) -> str | None:
    """Return the host-only page identity without exposing a raw URL."""

    try:
        if isinstance(app, ContextSnapshot):
            value = (
                app.browser_page.normalized_host if app.browser_page else None
            )
        else:
            value = getattr(app, "domain", None)
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip().lower()
    except Exception:  # noqa: BLE001 - page identity fallthrough is required
        return None


def resolve_enhancement_route(
    app: object | None,
    config: "WhisperstreamConfig",
    *,
    manual_profile_id: str | None = None,
    manual_anchor: str | None = None,
) -> EnhancementProfileRoute | None:
    """Apply manual, site, app, then default precedence to one snapshot.

    The route contains only a logical profile id and allowlisted tier. ``None``
    retains the established meaning that no valid route/default exists.
    """

    try:
        generation = (
            app.generation if isinstance(app, ContextSnapshot) else 1
        )
        valid_profile_ids = {
            profile.get("id")
            for profile in config.profiles
            if isinstance(profile, dict) and profile.get("id")
        }
        application_id = routing_target_application_id(app)
        if (
            manual_profile_id in valid_profile_ids
            and application_id is not None
            and application_id == (manual_anchor or "").lower()
        ):
            return EnhancementProfileRoute(
                generation,
                ProfileRouteTier.MANUAL,
                manual_profile_id,
            )

        host = routing_target_host(app)
        site_profile_id = config.site_map.get(host) if host else None
        if site_profile_id in valid_profile_ids:
            return EnhancementProfileRoute(
                generation,
                ProfileRouteTier.SITE,
                site_profile_id,
            )

        app_profile_id = (
            config.power_mode_map.get(application_id)
            if application_id is not None
            else None
        )
        if app_profile_id in valid_profile_ids:
            return EnhancementProfileRoute(
                generation,
                ProfileRouteTier.APPLICATION,
                app_profile_id,
            )

        default_profile_id = getattr(config, "default_profile_id", None)
        if default_profile_id in valid_profile_ids:
            return EnhancementProfileRoute(
                generation,
                ProfileRouteTier.DEFAULT,
                default_profile_id,
            )
    except Exception:  # noqa: BLE001 - malformed config falls through raw/default
        pass
    return None


def routing_probe_requirements(
    config: "WhisperstreamConfig", *, manual_profile_id: str | None = None
) -> tuple[bool, bool]:
    """Return ``(process_needed, site_needed)`` for recording-start capture.

    Only rules pointing at an existing profile can affect routing. A live
    manual override also needs a process-name read so its app anchor can be
    checked. Site routing always implies the process read needed to identify a
    supported browser, while UIA remains a separate opt-in capability.
    """
    try:
        valid_profile_ids = {
            profile.get("id")
            for profile in config.profiles
            if isinstance(profile, dict) and profile.get("id")
        }
        app_needed = any(
            profile_id in valid_profile_ids
            for profile_id in config.power_mode_map.values()
        )
        site_needed = any(
            profile_id in valid_profile_ids
            for profile_id in config.site_map.values()
        )
        manual_needed = bool(
            manual_profile_id and manual_profile_id in valid_profile_ids
        )
        return app_needed or site_needed or manual_needed, site_needed
    except Exception:  # noqa: BLE001 - malformed config degrades to no probe
        return False, False


def resolve_profile_for_app(
    app: object | None, config: "WhisperstreamConfig"
) -> str | None:
    """Resolve the enhancement profile id for the dictation target app.

    Returns the mapped profile id when the foreground app's process name has a
    ``power_mode_map`` entry pointing at an EXISTING profile. Returns ``None``
    (meaning "use the global default profile") when:

    - ``app`` is ``None`` (no foreground app detected), or
    - ``app.process_name`` is falsy (empty/unknown), or
    - the process name is not mapped in ``config.power_mode_map``, or
    - the mapped value is stale: a profile id no longer present in
      ``config.profiles`` (profiles are user-deletable).

    Lookup is case-insensitive on ``app.process_name.lower()`` against
    ``config.power_mode_map`` (whose keys are stored lowercased). Pure; never
    raises.
    """
    application_id = routing_target_application_id(app)
    if application_id is None:
        return None

    profile_id = config.power_mode_map.get(application_id)
    if not profile_id:
        return None

    # Stale-mapping safety: a value pointing at a now-deleted profile id falls
    # back to the global default downstream.
    if not any(profile.get("id") == profile_id for profile in config.profiles):
        return None

    return profile_id


def resolve_profile_for_target(
    app: object | None, config: "WhisperstreamConfig"
) -> str | None:
    """Resolve the enhancement profile id using the locked site -> app -> default
    precedence.

    Step 1 — site rule: if ``app`` is not None AND ``app.domain`` is truthy AND
    ``config.site_map`` maps ``app.domain.lower()`` to a profile id that EXISTS in
    ``config.profiles``, return that profile id immediately.

    Step 2 — app rule / default: otherwise delegate to ``resolve_profile_for_app``
    (which returns the app-level profile id or ``None`` for the global default).

    Stale-mapping safety: a ``site_map`` value pointing at a deleted profile id
    falls through to Step 2 rather than returning ``None`` directly, so the app
    rule still applies for the same browser process.

    Pure; never raises.
    """
    route = resolve_enhancement_route(app, config)
    if route is None or route.tier is ProfileRouteTier.DEFAULT:
        return None
    return route.profile_id
