"""Persistence for the Lockdown Mode acknowledgment (HIPAA disclosure gate).

Mirrors the EULA ``acceptance.json`` discipline (``eula.py``): a versioned,
atomically-written JSON record proving the user was shown and consciously
accepted a specific VERSION of the Step-1 acknowledgment at a specific time.

Legal basis (Condition-2 re-review 2026-07-12): the acknowledgment is a disclosure/disclaimer, not an
affirmative consent to a data event (Lockdown transmits no PHI), so it may be
persisted once and re-shown only on a material copy change — exactly like the
EULA. The approved conditions this module enforces:

- (a) the record captures the acknowledgment VERSION + accepted-at UTC timestamp
  (capital ``Z``, no fractional seconds) + software version;
- (b) a material copy change bumps ``LOCKDOWN_ACK_VERSION`` (owned by the
  frontend, next to the copy) so a stored older version no longer matches and
  Step 1 re-fires;
- (e) a missing, malformed, or lower/mismatched record FAILS CLOSED — the caller
  treats it as "not acknowledged" and shows Step 1 (see ``read_ack`` returning
  ``None`` on any read/parse error).

No side effects at import time; call the I/O helpers only.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from .eula import utc_now_iso8601

_logger = logging.getLogger(__name__)

ACK_FILENAME = "lockdown-acknowledgment.json"


def _ack_dir():
    """Return the app data directory holding the acknowledgment record.

    Delegates to ``config_manager.get_app_dir()`` (the canonical data dir, which
    honors ``WHISPERSTREAM_DATA_DIR``). Local import avoids a circular dependency
    and import-time I/O.
    """
    from .config_manager import get_app_dir  # noqa: PLC0415

    return get_app_dir()


def read_ack() -> Optional[Dict[str, Any]]:
    """Read and parse ``lockdown-acknowledgment.json``.

    Returns the parsed dict on success, or ``None`` if the file is missing or
    malformed. ``None`` MUST be treated by the caller as "not acknowledged"
    (fail closed -> show the acknowledgment), never as "acknowledged".
    """
    path = _ack_dir() / ACK_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def read_ack_version() -> Optional[str]:
    """Return the stored acknowledgment version string, or ``None``.

    ``None`` when there is no valid record OR the record has no string
    ``ack_version`` (fail closed). The caller compares this to the bundled
    ``LOCKDOWN_ACK_VERSION``; only an exact match means "already acknowledged".
    """
    record = read_ack()
    if record is None:
        return None
    version = record.get("ack_version")
    return version if isinstance(version, str) and version else None


def write_ack(ack_version: str, software_version: str) -> Dict[str, Any]:
    """Write the acknowledgment record atomically and return it.

    Stamps ``acknowledged_at`` as the current UTC time (capital ``Z``, no
    fractional seconds) to satisfy legal condition (a).

    Args:
        ack_version: The bundled acknowledgment version the user just accepted.
        software_version: App version string at the time of acceptance.

    Raises:
        OSError: On any filesystem error — the caller surfaces it as HTTP 500 so
            the client can retry; a failed write must never leave a partial file.
    """
    from .atomic_io import atomic_write_json  # noqa: PLC0415

    record: Dict[str, Any] = {
        "ack_version": ack_version,
        "acknowledged_at": utc_now_iso8601(),
        "software_version": software_version,
    }
    ack_dir = _ack_dir()
    ack_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(ack_dir / ACK_FILENAME, record, indent=2, trailing_newline=True)
    return record
