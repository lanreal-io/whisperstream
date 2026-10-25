"""Strict bundled cloud enhancement model catalog.

The source edition makes no catalog request; it validates and serves only the
packaged ``cloud-models.json``.
"""
from __future__ import annotations

import json
import re
import time
from importlib import resources
from typing import Any, Callable


SCHEMA_VERSION = 1
PROVIDERS = ("gemini", "openai", "anthropic")
MAX_MANIFEST_BYTES = 64 * 1024
MAX_MODELS_PER_PROVIDER = 16
MEMORY_TTL_SECONDS = 6 * 3600
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_memory_cache: tuple[float, dict[str, list[dict[str, Any]]]] | None = None


class ManifestValidationError(ValueError):
    """Raised when catalog data violates the bounded manifest contract."""


def clear_memory_cache() -> None:
    """Clear only the process cache. Used by tests and explicit refreshes."""
    global _memory_cache
    _memory_cache = None


def load_bundled_manifest(*, app_version: str = "0.0.0") -> dict[str, list[dict[str, Any]]]:
    payload = resources.files("whisperstream").joinpath("cloud-models.json").read_bytes()
    return parse_manifest_bytes(payload, app_version=app_version)


def parse_manifest_bytes(
    payload: bytes,
    *,
    app_version: str,
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(payload, bytes) or len(payload) > MAX_MANIFEST_BYTES:
        raise ManifestValidationError("manifest size is invalid")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestValidationError("manifest is not valid UTF-8 JSON") from exc
    return validate_manifest(document, app_version=app_version)


def validate_manifest(
    document: Any,
    *,
    app_version: str,
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(document, dict) or set(document) != {"schema_version", "providers"}:
        raise ManifestValidationError("document keys are invalid")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ManifestValidationError("schema version is unsupported")
    providers = document["providers"]
    if not isinstance(providers, dict) or set(providers) != set(PROVIDERS):
        raise ManifestValidationError("provider set is invalid")

    current_version = _parse_version(app_version, field="app version")
    seen_ids: set[str] = set()
    result: dict[str, list[dict[str, Any]]] = {}
    for provider in PROVIDERS:
        rows = providers[provider]
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_MODELS_PER_PROVIDER:
            raise ManifestValidationError("provider model count is invalid")
        validated: list[dict[str, Any]] = []
        for row in rows:
            validated_row = _validate_row(row)
            model_key = validated_row["id"].casefold()
            if model_key in seen_ids:
                raise ManifestValidationError("model IDs must be unique")
            seen_ids.add(model_key)
            minimum = validated_row.get("minimum_app_version")
            if minimum is None or current_version >= _parse_version(minimum, field="minimum version"):
                validated.append(validated_row)
        if not validated or sum(item.get("recommended") is True for item in validated) != 1:
            raise ManifestValidationError("each compatible provider catalog needs one recommendation")
        result[provider] = validated
    return result


def _validate_row(row: Any) -> dict[str, Any]:
    allowed = {"id", "label", "recommended", "minimum_app_version"}
    if not isinstance(row, dict) or not {"id", "label"} <= set(row) or not set(row) <= allowed:
        raise ManifestValidationError("model row keys are invalid")
    model_id = row["id"]
    label = row["label"]
    if not isinstance(model_id, str) or not 1 <= len(model_id) <= 128 or model_id.strip() != model_id:
        raise ManifestValidationError("model ID is invalid")
    if not isinstance(label, str) or not 1 <= len(label) <= 80 or label.strip() != label:
        raise ManifestValidationError("model label is invalid")
    if any(ord(char) < 32 for char in model_id + label):
        raise ManifestValidationError("model text contains control characters")
    recommended = row.get("recommended", False)
    if not isinstance(recommended, bool):
        raise ManifestValidationError("recommended must be boolean")
    minimum = row.get("minimum_app_version")
    if minimum is not None:
        _parse_version(minimum, field="minimum version")

    cleaned: dict[str, Any] = {"id": model_id, "label": label}
    if recommended:
        cleaned["recommended"] = True
    if minimum is not None:
        cleaned["minimum_app_version"] = minimum
    return cleaned


def _parse_version(value: Any, *, field: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{field} is invalid")
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise ManifestValidationError(f"{field} is invalid")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


async def resolve_cloud_model_catalog(
    *,
    app_version: str,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, list[dict[str, Any]]]:
    """Resolve the bundled catalog for *app_version*, memory-cached."""
    global _memory_cache
    current_time = now()
    if _memory_cache is not None and current_time - _memory_cache[0] < MEMORY_TTL_SECONDS:
        return _copy_catalog(_memory_cache[1])

    bundled = load_bundled_manifest(app_version=app_version)
    _memory_cache = (current_time, bundled)
    return _copy_catalog(bundled)


def _copy_catalog(catalog: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    return {provider: [dict(row) for row in rows] for provider, rows in catalog.items()}
