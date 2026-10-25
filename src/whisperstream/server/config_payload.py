"""Config API projection and protected credential decoding."""
from __future__ import annotations

import platform
from typing import Any, Dict

_KEY_FINGERPRINT_FIELDS = (
    "enhancement_gemini_api_key",
    "enhancement_openai_api_key",
    "enhancement_anthropic_api_key",
    "enhancement_custom_api_key",
)


def _inject_key_fingerprints(payload: Dict[str, Any], config: Any) -> None:
    """Add response-only ``<field>_fingerprint`` keys to a config payload.

    ``ConfigManager.client_dict()`` replaces key fields with a fixed presence
    marker, so neither plaintext nor an opaque provider reference reaches the
    frontend. This masked hint (first 4 + bullets + last 4, computed from the
    in-memory plaintext) lets the UI show *which* key is set without the secret
    ever leaving the backend. ``WhisperstreamConfig.from_dict`` filters to known
    dataclass fields, so the unknown ``*_fingerprint`` keys are dropped on the
    client's echo PUT.
    """
    from whisperstream.key_encryption import fingerprint_api_key  # noqa: PLC0415
    from ..vocabulary import supports_vocabulary
    payload["enhancement_vocabulary_supported"] = supports_vocabulary(config)
    for field_name in _KEY_FINGERPRINT_FIELDS:
        value = getattr(config, field_name, "")
        if not isinstance(value, str):
            value = ""
        payload[field_name + "_fingerprint"] = fingerprint_api_key(value)


def _config_payload(config: Any, config_manager: Any) -> Dict[str, Any]:
    """Build the client-facing config dict, identical to what PUT /api/config
    returns: fixed key-presence markers + the computed ``data_dir`` + masked
    key fingerprints.

    The lockdown enable/disable endpoints return this so the client can adopt
    the server-sanitized config verbatim (``setConfig``) instead of echoing its
    own stale copy back in a follow-up PUT. Keeping the shape byte-for-byte the
    same as the PUT response means the frontend's existing save-result handling
    applies unchanged.
    """
    from ..config_manager import get_app_dir  # noqa: PLC0415

    payload = _client_config_dict(config, config_manager)
    if platform.system() == "Darwin":
        payload.pop("data_dir", None)
        payload["model_storage_dir"] = ""
    else:
        payload["data_dir"] = str(get_app_dir())
    _inject_key_fingerprints(payload, config)
    return payload


def _client_config_dict(config: Any, config_manager: Any) -> Dict[str, Any]:
    """Use the real manager sanitizer, with a narrow mock-app fallback."""

    from ..config_manager import ConfigManager  # noqa: PLC0415

    if isinstance(config_manager, ConfigManager):
        return config_manager.client_dict(config)
    # Endpoint tests use a MagicMock app rather than ConfigManager. Invoke the
    # pure sanitizer directly so their response contract stays realistic.
    return ConfigManager.client_dict(config)


def _config_from_client_dict(
    data: Dict[str, Any],
    *,
    current: Any,
    config_manager: Any,
) -> Any:
    """Decode client config through the real manager or the mock-app seam."""

    from ..config_manager import (  # noqa: PLC0415
        PROTECTED_API_KEY_MARKER,
        ConfigManager,
        WhisperstreamConfig,
        _API_KEY_FIELDS,
    )

    if isinstance(config_manager, ConfigManager):
        return config_manager.config_from_client_dict(data, current=current)
    merged = dict(data)
    for field_name in _API_KEY_FIELDS:
        if (
            field_name not in merged
            or merged.get(field_name) == PROTECTED_API_KEY_MARKER
        ):
            current_value = getattr(current, field_name, "")
            # The mock-app endpoint seam may omit protected-key attributes.
            # MagicMock fabricates child objects for those lookups, and placing
            # one in the real dataclass makes ``asdict`` attempt to copy the
            # mock's internal RLock. Production values are always strings, so
            # preserve only that contract here.
            merged[field_name] = (
                current_value if isinstance(current_value, str) else ""
            )
    return WhisperstreamConfig.from_dict(merged)


def _coerce_enhancement_under_lockdown(config: Any) -> None:
    """Force the enhancement provider to on-device and, when required, turn AI
    cleanup OFF — the server-authoritative half of enabling Lockdown Mode.

    Lockdown blocks cloud/Ollama egress, so the provider is always coerced to
    ``"local"``. But the app REQUIRES a passed GPU benchmark before the local
    engine may run: ``enhancement_enabled`` can only become true for
    ``provider="local"`` by going through that benchmark gate. So if AI cleanup
    is currently ON with a NON-local provider, we must not silently flip it to
    local-and-ON (that would run an unbenchmarked local engine); instead we turn
    the toggle OFF and let the user re-enable local through the normal benchmark
    flow. A provider that is ALREADY ``"local"`` was necessarily benchmarked in
    order to be ON, so it is left ON.

    Idempotent: on an already-local config it only re-affirms ``"local"``.
    """
    if config.enhancement_enabled and config.enhancement_provider != "local":
        config.enhancement_enabled = False
    config.enhancement_provider = "local"
