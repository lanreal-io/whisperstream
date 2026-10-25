"""Exact transport capabilities and credential-free thinking preferences.

Unknown endpoints keep their defaults. Model aliases are explicit contracts, not
substring matches; local sampling remains owned by enhancement_policy.
"""
from __future__ import annotations

import hashlib
import json
import re

from .enhancement_security import EnhancementAdmissionError, consent_target

_LEVELS = frozenset({"off", "on", "minimal", "low", "medium", "high"})
_KEY = re.compile(r"[0-9a-f]{64}\Z")
_EFFORT = ("low", "medium", "high")


def normalize_thinking_levels(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, level in value.items():
        if isinstance(key, str) and _KEY.fullmatch(key) and isinstance(level, str) and level in _LEVELS:
            result[key] = level
            if len(result) == 128:
                break
    return result


def normalize_workspace_id(value) -> str:
    # IDs are routing metadata, never part of diagnostics or preference keys.
    if not isinstance(value, str):
        raise ValueError("Invalid Anthropic workspace setting")
    value = value.strip()
    if len(value) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("Invalid Anthropic workspace setting")
    return value


def _capability(provider, endpoint, model, metadata):
    if provider == "anthropic":
        if model in {"claude-haiku-5-5", "claude-sonnet-5"}:
            return "configurable", ("off", *_EFFORT), "off", "anthropic"
        if model in {"claude-opus-5-5", "claude-sonnet-5-5"}:
            return "mandatory", _EFFORT, "low", "anthropic"
        if model in {"claude-haiku-4-5", "claude-haiku-4-5-20251001",
                     "claude-sonnet-4-5", "claude-sonnet-4-5-20250929",
                     "claude-sonnet-4-6", "claude-opus-4-6"}:
            return "fixed", (), "off", "anthropic-disabled"
    if provider == "openai":
        if model in {"gpt-6-luna", "gpt-6-sol", "gpt-5.6-luna", "gpt-5.6-terra"}:
            return "configurable", ("off", *_EFFORT), "off", "openai"
        if model in {"gpt-6.1-sol", "gpt-6-astra"}:
            return "mandatory", _EFFORT, "low", "openai"
    if provider == "gemini":
        if model in {"gemini-2.5-flash", "gemini-2.5-flash-lite"}:
            return "configurable", ("off", *_EFFORT), "off", "openai"
        if model in {"gemini-2.5-pro", "gemini-3.1-pro-preview", "gemini-3.7-flash"}:
            return "mandatory", _EFFORT, "low", "openai"
        if model == "gemini-3.5-flash-lite":
            return "mandatory", ("minimal", *_EFFORT), "minimal", "openai"
    if provider == "custom" and endpoint == "https://openrouter.ai/api/v1":
        if model == "anthropic/claude-haiku-5.5":
            return "configurable", ("off", "on"), "off", "openrouter"
    from .enhancement_prompts import STREAM_MINI_PROMPT_KEYS
    if provider == "local" and metadata.get("prompt_key") in STREAM_MINI_PROMPT_KEYS:
        return "fixed", (), "off", "local"
    return "unknown", (), "default", "unknown"


def _resolve(config, metadata=None):
    try:
        target = consent_target(config)
    except EnhancementAdmissionError:
        return {"kind": "unknown", "options": [], "default": "default",
                "effective": "default", "preference_key": ""}, "unknown"
    model = target.model
    if target.provider == "local":
        model = config.enhancement_local_model
        if metadata is None:
            from .enhancement_models import get_local_model
            metadata = get_local_model(model)
    kind, options, default, wire = _capability(target.provider, target.endpoint, model, metadata or {})
    key = hashlib.sha256(json.dumps([target.provider, target.endpoint, model], ensure_ascii=True).encode()).hexdigest()
    selected = normalize_thinking_levels(config.enhancement_thinking_levels).get(key)
    effective = selected if selected in options else default
    return {"kind": kind, "options": list(options), "default": default,
            "effective": effective, "preference_key": key}, wire


def resolve_thinking(config, metadata=None) -> dict:
    return _resolve(config, metadata)[0]


def thinking_options(config) -> dict:
    descriptor, wire = _resolve(config)
    level = descriptor["effective"]
    if wire == "anthropic-disabled" or (wire == "anthropic" and level == "off"):
        return {"thinking": {"type": "disabled"}}
    if wire == "anthropic":
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": level}}
    if wire == "openai":
        return {"reasoning_effort": "none" if level == "off" else level}
    if wire == "openrouter":
        return {"reasoning": {"enabled": level == "on"}}
    return {}
