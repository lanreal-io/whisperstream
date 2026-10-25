"""BYOK consent record persistence.

Writes an append-only audit log at
``%LOCALAPPDATA%\\Whisperstream\\byok-consent.json`` each time the user
confirms the blocking first-enable dialog for a cloud-post-processing
provider. Keeps a tamper-evident trail so
Lanreal can demonstrate a tamper-evident trail of user consent events
(provider, timestamp, policy versions in force at the time, and a
SHA-256 of the dialog body the user saw).

This replaces the previous ``enhancement_privacy_acknowledged_providers``
array in ``config.toml``, which only stored provider names and had no
versioning or content binding.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any, Dict, List, Optional

from .atomic_io import atomic_write_json
from .config_manager import get_app_dir
from .enhancement_security import ConsentTarget, DISCLOSURE_VERSION


def get_consent_path() -> Path:
    return get_app_dir() / "byok-consent.json"


def _software_version() -> str:
    try:
        return _pkg_version("whisperstream")
    except PackageNotFoundError:
        return "unknown"


def hash_dialog_content(dialog_body_text: str) -> str:
    return hashlib.sha256(dialog_body_text.encode("utf-8")).hexdigest()


_lock = threading.Lock()


class ConsentStorageError(ValueError):
    """Consent evidence cannot be safely read or replaced."""


def _read_raw(*, for_write: bool = False) -> List[Dict[str, Any]]:
    path = get_consent_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError):
        if for_write:
            raise ConsentStorageError("consent_store_unavailable") from None
        return []
    if not isinstance(data, list):
        if for_write:
            raise ConsentStorageError("consent_store_unavailable")
        return []
    if for_write and any(not isinstance(record, dict) for record in data):
        raise ConsentStorageError("consent_store_unavailable")
    return [r for r in data if isinstance(r, dict)]


def _write_raw(records: List[Dict[str, Any]]) -> None:
    try:
        atomic_write_json(get_consent_path(), records, indent=2)
    except OSError:
        raise ConsentStorageError("consent_store_unavailable") from None


def list_records() -> List[Dict[str, Any]]:
    with _lock:
        return list(_read_raw())


def acknowledged_providers() -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for record in list_records():
        provider = record.get("provider")
        if isinstance(provider, str) and provider and provider not in seen:
            seen.add(provider)
            result.append(provider)
    return result


def has_consent(provider: str) -> bool:
    """Historical provider lookup only; runtime admission uses has_target_consent."""
    return provider in acknowledged_providers()


def has_target_consent(target: ConsentTarget) -> bool:
    """Only exact current disclosure/recipient evidence admits a request.

    Old provider-only records remain readable evidence, never an inferred grant
    for a destination or onward-processing disclosure they did not identify.
    """
    for record in reversed(list_records()):
        if record.get("provider") != target.provider:
            continue
        if record.get("event") == "revoked":
            return False
        if (
            record.get("recipient_identity") == target.identity
            and record.get("disclosure_version") == DISCLOSURE_VERSION
            and record.get("dialog_content_hash")
        ):
            return True
    return False


def record_consent(
    provider: str,
    dialog_body_text: str,
    *,
    now: Optional[datetime] = None,
    target: ConsentTarget | None = None,
    legal_identity: dict[str, str] | None = None,
) -> Dict[str, Any]:
    """Append a consent record for *provider*. Returns the written record."""
    if legal_identity is None:
        from .consent_disclosure import legal_resource_identity
        legal_identity = legal_resource_identity()
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    record = {
        "provider": provider,
        "consent_given_at": timestamp,
        "software_version": _software_version(),
        "dialog_content_hash": hash_dialog_content(dialog_body_text),
    }
    if target is not None:
        if target.provider != provider:
            raise ValueError("consent_provider_mismatch")
        record.update({
            "event": "accepted",
            "recipient_identity": target.identity,
            "recipient_endpoint": target.endpoint,
            "processing_model": target.model,
            "disclosure_version": DISCLOSURE_VERSION,
        })
    if legal_identity is not None:
        record.update(legal_identity)
    with _lock:
        records = _read_raw(for_write=True)
        records.append(record)
        _write_raw(records)
    return record


def remove_provider(provider: str) -> int:
    """Delete every record for *provider*. Returns the number removed.

    Called when the user clicks "Remove key" (PRIVACY.md §6.5 bullet 3):
    clearing the credential also clears the consent trail so re-enabling
    the same provider requires a fresh click-through, producing a new
    dated consent event.
    """
    with _lock:
        records = _read_raw(for_write=True)
        kept = [r for r in records if r.get("provider") != provider]
        removed = len(records) - len(kept)
        if removed:
            _write_raw(kept)
        return removed
