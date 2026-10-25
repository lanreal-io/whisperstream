"""Profile migration and routing-reference rules, without runtime or persistence.

ConfigManager keeps schema construction, ordering, diagnostics and atomic writes.
The transforms preserve existing collection mutation and absence semantics; they
never load secrets, select live routes, or import enhancer execution.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any, TypedDict

from .enhancement_prompts import CHAT_PROMPT, CODE_PROMPT, EMAIL_PROMPT

PROFILE_ICON_KEYS: frozenset[str] = frozenset(
    {
        "chat",
        "code",
        "mail",
        "document",
        "note",
        "terminal",
        "checklist",
        "calendar",
        "people",
        "sparkle",
        "search",
        "pen",
        "book",
        "globe",
    }
)
BUILTIN_PROFILE_IDS: frozenset[str] = frozenset({"chat", "code", "email"})


class RoutingFields(TypedDict):
    power_mode_map: dict[str, str]
    power_mode_paths: dict[str, str]
    site_map: dict[str, str]
    profile_hotkeys: dict[str, str]


def normalize_routing_fields(
    data: Mapping[str, Any], *, default_app_rules: Mapping[str, str],
) -> RoutingFields:
    """Normalize saved maps at the existing pre-profile migration point."""
    merged = dict(data)
    # Only absence seeds defaults. An explicitly empty app map stays empty.
    if "power_mode_map" not in data:
        merged["power_mode_map"] = dict(default_app_rules)
    # Lowercase keys, coerce values to str, drop blank key or value. Profile-id
    # membership is NOT validated here — that is validate()'s job.
    merged["power_mode_map"] = {
        str(k).lower(): str(v)
        for k, v in (merged.get("power_mode_map") or {}).items()
        if str(k).strip() and str(v).strip()
    }
    # Backfill Email apps only into nonempty maps, preserving each existing
    # target. Edition-specific bundled defaults come from the schema owner.
    if merged["power_mode_map"]:
        for _email_app, _email_app_target in default_app_rules.items():
            if (
                _email_app_target == "email"
                and _email_app not in merged["power_mode_map"]
            ):
                merged["power_mode_map"][_email_app] = "email"
    # power_mode_paths: NO seed-on-absence. Lowercase basename keys, coerce
    # path values to str, drop blanks. Pruning against power_mode_map keys is
    # validate()'s job.
    merged["power_mode_paths"] = {
        str(k).lower(): str(v)
        for k, v in (merged.get("power_mode_paths") or {}).items()
        if str(k).strip() and str(v).strip()
    }
    # Normalize saved hosts before the deliberate Email-domain backfill.
    merged["site_map"] = {
        str(k).lower(): str(v)
        for k, v in (merged.get("site_map") or {}).items()
        if str(k).strip() and str(v).strip()
    }
    # Unlike app defaults, mail hosts backfill even an explicitly empty map.
    # Existing targets always win; validation later prunes stale profile IDs.
    for _mail_domain in (
        "mail.google.com",
        "outlook.office.com",
        "outlook.live.com",
    ):
        if _mail_domain not in merged["site_map"]:
            merged["site_map"][_mail_domain] = "email"
    # Hotkeys never seed. Profile membership and global shortcut conflicts are
    # checked only after profiles and the remaining config are normalized.
    merged["profile_hotkeys"] = {
        str(k): str(v).lower()
        for k, v in (merged.get("profile_hotkeys") or {}).items()
        if str(k).strip() and str(v).strip()
    }

    return {
        "power_mode_map": merged["power_mode_map"],
        "power_mode_paths": merged["power_mode_paths"],
        "site_map": merged["site_map"],
        "profile_hotkeys": merged["profile_hotkeys"],
    }


def migrate_profiles(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return migrated profile fields, preserving key absence and prior edits."""
    merged = dict(data)
    # Migrate legacy mode/prompt only on absence, preserving migrated edits.
    if "profiles" not in merged:
        bundled: list[dict[str, str]] = [
            {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
            {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
            {"id": "email", "name": "Email", "prompt": EMAIL_PROMPT},
        ]
        mode = str(merged.get("enhancement_mode", "default")).lower()
        if mode == "custom":
            bundled.append(
                {
                    "id": "custom",
                    "name": "Custom",
                    # Mirrors _get_system_prompt's custom branch: an empty
                    # custom prompt behaves like chat.
                    "prompt": merged.get("enhancement_custom_prompt") or CHAT_PROMPT,
                }
            )
        # default/chat -> chat; code -> code; custom -> custom; anything
        # unrecognised -> chat (mirrors _get_system_prompt's fallback).
        if mode == "code":
            default_profile_id = "code"
        elif mode == "custom":
            default_profile_id = "custom"
        else:
            default_profile_id = "chat"
        merged["profiles"] = bundled
        merged["default_profile_id"] = default_profile_id
    else:
        # Backfill Email exactly once without replacing a renamed/edited row
        # or changing the default. A present empty list also receives Email.
        profiles_list = merged.get("profiles")
        if isinstance(profiles_list, list) and not any(
            isinstance(p, dict) and p.get("id") == "email" for p in profiles_list
        ):
            profiles_list.append(
                {"id": "email", "name": "Email", "prompt": EMAIL_PROMPT}
            )

    # Transfer the retired global signature only when Email lacks one. This
    # follows seeding/backfill and never replaces a saved per-profile signature.
    legacy_signature = merged.get("email_signature")
    if isinstance(legacy_signature, str) and legacy_signature.strip():
        for _p in merged.get("profiles", []) or []:
            if (
                isinstance(_p, dict)
                and _p.get("id") == "email"
                and not str(_p.get("signature", "")).strip()
            ):
                _p["signature"] = legacy_signature
                break

    return {key: merged[key] for key in ("profiles", "default_profile_id") if key in merged}


def validate_profiles(
    profiles: list[dict[str, Any]],
    default_profile_id: str,
    *,
    supported_languages: Collection[str],
) -> set[str]:
    """Validate identity/default references and normalize profile presentation."""
    # Enhancement profiles: each id non-empty + unique; default pointer (if
    # set) must reference an existing profile. An empty profiles list with
    # an empty default_profile_id is valid (the pre-seed state).
    seen_profile_ids: set[str] = set()
    for profile in profiles:
        profile_id = profile.get("id", "")
        if not profile_id:
            raise ValueError(
                f"profile id must be non-empty, got {profile_id!r}"
            )
        if profile_id in seen_profile_ids:
            raise ValueError(
                f"profile id must be unique, got duplicate {profile_id!r}"
            )
        seen_profile_ids.add(profile_id)
        language = profile.get("language")
        if language is not None:
            if not isinstance(language, str) or language.strip().lower() not in supported_languages:
                raise ValueError("Invalid dictation language for profile")
            profile["language"] = language.strip().lower()
        # A custom profile may opt into one curated glyph. Any missing,
        # empty, future, malformed, or built-in override is removed so the
        # frontend can render its truthful id/letter fallback. Mutating the
        # in-memory shape keeps both load and save fail-safe.
        icon = profile.get("icon")
        if (
            profile_id in BUILTIN_PROFILE_IDS
            or not isinstance(icon, str)
            or icon not in PROFILE_ICON_KEYS
        ):
            profile.pop("icon", None)

    if default_profile_id and default_profile_id not in seen_profile_ids:
        raise ValueError(
            f"default_profile_id must reference an existing profile, "
            f"got {default_profile_id!r}"
        )

    return seen_profile_ids


def prune_profile_routing(
    *,
    power_mode_map: dict[str, str],
    power_mode_paths: dict[str, str],
    site_map: dict[str, str],
    profile_hotkeys: dict[str, str],
    profile_ids: set[str],
    reserved_shortcuts: Iterable[str],
) -> tuple[RoutingFields, list[ValueError]]:
    """Prune stale references together; the caller owns content-free warnings."""
    warnings: list[ValueError] = []
    # Deleted profile IDs cannot remain app/site targets. Directly constructed
    # configs may still need the lowercase normalization used by disk loading.
    stale_keys = [
        k for k, v in power_mode_map.items()
        if v not in profile_ids or k != k.lower()
    ]
    if stale_keys:
        warnings.append(
            ValueError(
                f"power_mode_map dropped {len(stale_keys)} entries with unknown "
                f"profile ids or non-lowercase keys: {stale_keys}"
            )
        )
        # Rebuild: lowercase key, keep only entries with known profile ids.
        power_mode_map = {
            k.lower(): v
            for k, v in power_mode_map.items()
            if v in profile_ids
        }

    # Keep recorded paths only for surviving app rules. Paths never enter logs.
    power_mode_paths = {
        k.lower(): v
        for k, v in (power_mode_paths or {}).items()
        if k.lower() in power_mode_map
    }

    # Apply the same profile membership check to site rules in this pass.
    stale_site_keys = [
        k for k, v in site_map.items()
        if v not in profile_ids or k != k.lower()
    ]
    if stale_site_keys:
        warnings.append(
            ValueError(
                f"site_map dropped {len(stale_site_keys)} entries with unknown "
                f"profile ids or non-lowercase keys: {stale_site_keys}"
            )
        )
        # Rebuild: lowercase key, keep only entries with known profile ids.
        site_map = {
            k.lower(): v
            for k, v in site_map.items()
            if v in profile_ids
        }

    # Global shortcuts win; among profile duplicates the earlier entry wins.
    # Compare case-insensitively, including directly constructed config values.
    reserved = {(shortcut or "").strip().lower() for shortcut in reserved_shortcuts}
    reserved.discard("")
    dropped_hotkeys: list[str] = []
    kept_hotkeys: dict[str, str] = {}
    seen_shortcuts: set[str] = set()
    for profile_id, shortcut in profile_hotkeys.items():
        norm = (shortcut or "").strip().lower()
        if not norm:
            continue
        if profile_id not in profile_ids:
            dropped_hotkeys.append(profile_id)
            continue
        if norm in reserved or norm in seen_shortcuts:
            dropped_hotkeys.append(profile_id)
            continue
        seen_shortcuts.add(norm)
        kept_hotkeys[profile_id] = norm
    if dropped_hotkeys:
        warnings.append(
            ValueError(
                f"profile_hotkeys dropped {len(dropped_hotkeys)} entries with "
                f"unknown profile ids or conflicting shortcuts: {dropped_hotkeys}"
            )
        )
        profile_hotkeys = kept_hotkeys
    return {
        "power_mode_map": power_mode_map,
        "power_mode_paths": power_mode_paths,
        "site_map": site_map,
        "profile_hotkeys": profile_hotkeys,
    }, warnings
