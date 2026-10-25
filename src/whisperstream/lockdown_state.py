"""
lockdown_state.py — In-memory lock + exponential-backoff state machine.

DO NOT LOG: This module holds the unlocked data key in memory.
  - Never log `self._data_key` or any key material.
  - Never log passwords, recovery codes, or attempt sequences.
  - This module is the lock gate for transcript access; any log of key material
    here would constitute a PHI-adjacent security event.

Persistence (F6): the failed-attempt counters + lockout deadlines are OPTIONALLY
persisted to a small backend-managed state file (``state_path``), protected
through the same shared boundary as ``transcript.key`` and never stored in
``config.toml``. This makes the escalating unlock backoff survive a backend
restart (an unauthenticated local caller can otherwise restart the process to
reset the lockout). The file holds ONLY counters + wall-clock epoch deadlines —
no data key, password, recovery code, or transcript content. When ``state_path``
is None the state is purely in-memory (unchanged legacy behavior).

Clock injection: pass a `now` callable (default: time.monotonic) so tests can
advance time deterministically without real sleeps. All in-memory comparisons
use the injected `now` clock; persisted deadlines use a separate `wall_now`
(default: time.time) because a monotonic clock is not comparable across process
restarts. On load, persisted wall-clock deadlines are rebased onto `now`.

Backoff schedule (iPhone-style, per design Surface C):
  Attempts 1-4: no lockout (returns 0)
  Attempt 5:    60s  (1 minute)
  Attempt 6:    300s (5 minutes)
  Attempt 7:    900s (15 minutes)
  Attempt 8+:   3600s (1 hour)

Recovery-code path has its own independent counter and backoff schedule so it
cannot be cross-throttled by main-path failures (or vice versa).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Optional


# Backoff tiers indexed by (attempt_number - 1) starting from the 5th attempt.
# attempts 1-4 -> no lockout (None means 0)
# attempt 5  -> index 0 -> 60s
# attempt 6  -> index 1 -> 300s
# attempt 7  -> index 2 -> 900s
# attempt 8+ -> last tier -> 3600s
_MAIN_BACKOFF_TIERS: list[int] = [60, 300, 900, 3600]
_LOCKOUT_FREE_THRESHOLD = 4  # attempts below or equal to this have no lockout

# Recovery path keeps its own counters so password failures cannot lock it out;
# its tiers currently match the password path.
_RECOVERY_BACKOFF_TIERS: list[int] = [60, 300, 900, 3600]
_RECOVERY_LOCKOUT_FREE_THRESHOLD = 4


def _compute_lockout(attempt_number: int, tiers: list[int], threshold: int) -> int:
    """Return the lockout duration in seconds for the given attempt number.

    Returns 0 if the attempt is within the threshold (no lockout).
    Returns the last tier value if the attempt exceeds the tier list.
    """
    if attempt_number <= threshold:
        return 0
    tier_index = attempt_number - threshold - 1  # 0-indexed into tiers
    tier_index = min(tier_index, len(tiers) - 1)
    return tiers[tier_index]


class LockdownState:
    """In-memory lock and exponential-backoff state machine for the transcript surface.

    This is the single runtime gate between the frontend/API and the encrypted
    transcript store. It is NOT serialized to config.toml (only `lockdown_mode`
    and `lockdown_idle_lock_seconds` live in config; counters and lockout
    timestamps are backend-side state).

    Parameters
    ----------
    now:
        A callable returning the current time as a float (seconds). Defaults
        to `time.monotonic`. Inject a fake clock in tests to advance time
        without real sleeps.
    state_path:
        Optional path to a protected backoff state file (F6). When provided,
        the failed-attempt counters + lockout deadlines are loaded on init and
        rewritten on every failure/success so the lockout survives a restart.
        When None (default) the state is purely in-memory. NEVER contains key
        material or secrets — only counters + wall-clock epoch deadlines.
    wall_now:
        A callable returning wall-clock epoch seconds (default: `time.time`),
        used ONLY to persist/restore lockout deadlines across restarts (the
        `now` clock is monotonic and not comparable across processes). Inject a
        fake wall clock in tests alongside `now`.
    """

    def __init__(
        self,
        now: Callable[[], float] = time.monotonic,
        *,
        state_path: Optional[Path] = None,
        wall_now: Callable[[], float] = time.time,
    ) -> None:
        self._now = now
        self._wall_now = wall_now
        self._state_path: Optional[Path] = (
            Path(state_path) if state_path is not None else None
        )

        # DO NOT LOG: key material — never pass _data_key to any logger.
        self._data_key: Optional[bytes] = None
        self._locked: bool = True

        self._last_activity_ts: float = self._now()

        # Main-path backoff
        self._failed_attempts: int = 0
        self._lockout_until_ts: float = 0.0

        # Recovery-code path backoff (independent)
        self._recovery_failed_attempts: int = 0
        self._recovery_lockout_until_ts: float = 0.0

        # F6: restore persisted counters + lockout deadlines (if any).
        self._load_persisted()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def locked(self) -> bool:
        """True when the transcript surface is locked."""
        return self._locked

    @property
    def is_locked(self) -> bool:
        """Alias for `locked`."""
        return self._locked

    @property
    def data_key(self) -> Optional[bytes]:
        """The held 32-byte AES data key, or None when locked.

        DO NOT LOG this value.
        """
        return self._data_key

    @property
    def failed_attempts(self) -> int:
        """Current count of consecutive main-path failed unlock attempts."""
        return self._failed_attempts

    @property
    def recovery_failed_attempts(self) -> int:
        """Current count of consecutive recovery-code failed unlock attempts."""
        return self._recovery_failed_attempts

    @property
    def last_activity_ts(self) -> float:
        """Monotonic timestamp of the last recorded activity."""
        return self._last_activity_ts

    @property
    def lockout_until_ts(self) -> float:
        """Monotonic timestamp at which the main-path lockout expires."""
        return self._lockout_until_ts

    # ------------------------------------------------------------------
    # Lock / unlock
    # ------------------------------------------------------------------

    def unlock(self, data_key: bytes) -> None:
        """Unlock the transcript surface with the provided data key.

        DO NOT LOG data_key.
        """
        # DO NOT LOG: data_key is key material.
        self._data_key = data_key
        self._locked = False
        self._last_activity_ts = self._now()

    def lock(self) -> None:
        """Lock the transcript surface and discard the held data key."""
        # DO NOT LOG: _data_key is key material.
        self._data_key = None
        self._locked = True

    # ------------------------------------------------------------------
    # Main-path failed-attempt / success accounting
    # ------------------------------------------------------------------

    def register_failed_attempt(self) -> int:
        """Record a failed main-path unlock attempt.

        Returns
        -------
        int
            Remaining lockout seconds (0 if no active lockout is in effect).
            If already in lockout, returns the remaining seconds without
            advancing the attempt counter further.
        """
        now = self._now()

        # If already in an active lockout, report remaining seconds without
        # incrementing the counter.
        remaining = max(0, int(self._lockout_until_ts - now))
        if remaining > 0:
            return remaining

        self._failed_attempts += 1
        duration = _compute_lockout(
            self._failed_attempts, _MAIN_BACKOFF_TIERS, _LOCKOUT_FREE_THRESHOLD
        )
        if duration > 0:
            self._lockout_until_ts = now + duration
        self._persist()
        return duration

    def register_success(self) -> None:
        """Reset the main-path failed-attempt counter and clear any lockout."""
        self._failed_attempts = 0
        self._lockout_until_ts = 0.0
        self._persist()

    def remaining_lockout_seconds(self) -> int:
        """Return how many seconds remain in the current main-path lockout.

        Returns 0 if no lockout is active.
        """
        now = self._now()
        return max(0, int(self._lockout_until_ts - now))

    # ------------------------------------------------------------------
    # Recovery-code path (independent accounting)
    # ------------------------------------------------------------------

    def register_recovery_failed_attempt(self) -> int:
        """Record a failed recovery-code unlock attempt.

        Returns the remaining lockout seconds on the recovery path (0 if none).
        This counter is independent of the main-path counter.
        """
        now = self._now()

        # If already in an active recovery lockout, report remaining seconds.
        remaining = max(0, int(self._recovery_lockout_until_ts - now))
        if remaining > 0:
            return remaining

        self._recovery_failed_attempts += 1
        duration = _compute_lockout(
            self._recovery_failed_attempts,
            _RECOVERY_BACKOFF_TIERS,
            _RECOVERY_LOCKOUT_FREE_THRESHOLD,
        )
        if duration > 0:
            self._recovery_lockout_until_ts = now + duration
        self._persist()
        return duration

    def register_recovery_success(self) -> None:
        """Reset the recovery-code failed-attempt counter and clear its lockout."""
        self._recovery_failed_attempts = 0
        self._recovery_lockout_until_ts = 0.0
        self._persist()

    def remaining_recovery_lockout_seconds(self) -> int:
        """Return how many seconds remain in the current recovery-path lockout."""
        now = self._now()
        return max(0, int(self._recovery_lockout_until_ts - now))

    # ------------------------------------------------------------------
    # F6: protected persistence of the backoff counters + deadlines
    # ------------------------------------------------------------------

    def _rebase_deadline(self, epoch_deadline: Any) -> float:
        """Convert a persisted wall-clock deadline to the internal `now` domain.

        Returns the `now`-domain timestamp at which the lockout expires, or
        ``0.0`` when the deadline is absent, unparseable, or already in the past.
        """
        try:
            remaining = float(epoch_deadline) - self._wall_now()
        except (TypeError, ValueError):
            return 0.0
        if remaining <= 0:
            return 0.0
        return self._now() + remaining

    def _load_persisted(self) -> None:
        """Restore counters + lockout deadlines from the sealed store, if any.

        No-op when no ``state_path`` is configured or the file is absent /
        unreadable / undecryptable — fail OPEN to a clean in-memory state (a lost
        lockout is never worse than the pre-F6 behavior). Never logs any field.
        """
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            sealed = self._state_path.read_text(encoding="utf-8").strip()
            if not sealed:
                return
            from .key_encryption import decrypt_api_key

            raw = decrypt_api_key(sealed)
            if not raw:
                # enc: blob unusable on this account / platform — start clean.
                return
            data = json.loads(raw)
        except Exception:
            return
        try:
            self._failed_attempts = max(0, int(data.get("failed_attempts", 0)))
            self._recovery_failed_attempts = max(
                0, int(data.get("recovery_failed_attempts", 0))
            )
            self._lockout_until_ts = self._rebase_deadline(
                data.get("lockout_until_epoch", 0.0)
            )
            self._recovery_lockout_until_ts = self._rebase_deadline(
                data.get("recovery_lockout_until_epoch", 0.0)
            )
        except Exception:
            # Partial / garbage payload — start clean rather than crash.
            self._failed_attempts = 0
            self._recovery_failed_attempts = 0
            self._lockout_until_ts = 0.0
            self._recovery_lockout_until_ts = 0.0

    def _persist(self) -> None:
        """Seal the current counters + lockout deadlines to disk.

        No-op when no ``state_path`` is configured. Lockout deadlines are stored
        as wall-clock epochs so they survive a restart (the internal `now` domain
        is not comparable across processes). Protected through the same shared
        boundary as ``transcript.key``; the payload holds ONLY counters + epoch
        timestamps — no key material, password, recovery code, or transcript
        content. Write failures are swallowed so persistence can never break an
        unlock flow.
        """
        if self._state_path is None:
            return
        main_remaining = self.remaining_lockout_seconds()
        recovery_remaining = self.remaining_recovery_lockout_seconds()
        payload = {
            "failed_attempts": self._failed_attempts,
            "lockout_until_epoch": (
                self._wall_now() + main_remaining if main_remaining > 0 else 0.0
            ),
            "recovery_failed_attempts": self._recovery_failed_attempts,
            "recovery_lockout_until_epoch": (
                self._wall_now() + recovery_remaining
                if recovery_remaining > 0
                else 0.0
            ),
        }
        try:
            from .atomic_io import atomic_write_text
            from .key_encryption import encrypt_api_key

            sealed = encrypt_api_key(json.dumps(payload))
            atomic_write_text(self._state_path, sealed)
        except Exception:
            # Best-effort; a failed write must never break the unlock path.
            return

    # ------------------------------------------------------------------
    # Idle auto-lock
    # ------------------------------------------------------------------

    def note_activity(self) -> None:
        """Record the current time as the last activity timestamp."""
        self._last_activity_ts = self._now()

    def idle_expired(self, idle_lock_seconds: int) -> bool:
        """Return True if the transcript surface should auto-lock due to idle.

        Parameters
        ----------
        idle_lock_seconds:
            The configured idle timeout in seconds. 0 means Never — this
            method always returns False when `idle_lock_seconds` is 0.
        """
        if idle_lock_seconds == 0:
            return False
        elapsed = self._now() - self._last_activity_ts
        return elapsed >= idle_lock_seconds

    # ------------------------------------------------------------------
    # Repr (must not expose key material)
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """Safe repr — never includes the data key or key material."""
        return (
            f"LockdownState("
            f"locked={self._locked}, "
            f"failed_attempts={self._failed_attempts}, "
            f"lockout_remaining={self.remaining_lockout_seconds()}s, "
            f"recovery_failed_attempts={self._recovery_failed_attempts}"
            f")"
        )
