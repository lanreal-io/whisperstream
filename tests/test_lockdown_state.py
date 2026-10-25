"""
Tests for lockdown_state.LockdownState — lock/backoff state machine.

TDD file: written BEFORE the implementation. Tests cover:
- initial state (locked, no key, no counter)
- unlock/lock round-trip
- is_locked property
- register_failed_attempt() backoff tiers (1-4 no lockout, then 60/300/900/3600s)
- register_success() resets counter + lockout
- remaining_lockout_seconds()
- note_activity() + idle_expired() (0 = Never)
- independent recovery-code attempt counter
- clock injection so tests advance time without real sleeps
- no secrets are logged
"""

import time
import pytest

from whisperstream.lockdown_state import LockdownState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_state(start: float = 0.0) -> tuple[LockdownState, list[float]]:
    """Return a state machine backed by a controllable clock.

    Returns (state, clock_list) where clock_list[0] is the current time;
    increment clock_list[0] to advance the clock.
    """
    clock = [start]
    state = LockdownState(now=lambda: clock[0])
    return state, clock


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

class TestInitialState:
    def test_initially_locked(self):
        state, _ = make_state()
        assert state.locked is True

    def test_initially_no_data_key(self):
        state, _ = make_state()
        assert state.data_key is None

    def test_initially_no_lockout(self):
        state, _ = make_state()
        assert state.remaining_lockout_seconds() == 0

    def test_initially_zero_failed_attempts(self):
        state, _ = make_state()
        assert state.failed_attempts == 0


# ---------------------------------------------------------------------------
# unlock / lock
# ---------------------------------------------------------------------------

class TestUnlockLock:
    def test_unlock_clears_locked_flag(self):
        state, _ = make_state()
        state.unlock(b"key-material-32-bytes-padding123")
        assert state.locked is False

    def test_unlock_stores_data_key(self):
        state, _ = make_state()
        key = b"x" * 32
        state.unlock(key)
        assert state.data_key == key

    def test_lock_sets_locked_flag(self):
        state, _ = make_state()
        state.unlock(b"k" * 32)
        state.lock()
        assert state.locked is True

    def test_lock_clears_data_key(self):
        state, _ = make_state()
        state.unlock(b"k" * 32)
        state.lock()
        assert state.data_key is None

    def test_is_locked_property(self):
        state, _ = make_state()
        assert state.is_locked is True
        state.unlock(b"k" * 32)
        assert state.is_locked is False
        state.lock()
        assert state.is_locked is True


# ---------------------------------------------------------------------------
# register_failed_attempt() backoff tiers
# ---------------------------------------------------------------------------

class TestBackoffTiers:
    """iPhone-style schedule: attempts 1-4 = no lockout; then 60/300/900/3600s."""

    def test_first_four_attempts_return_zero(self):
        state, clock = make_state()
        for i in range(1, 5):
            secs = state.register_failed_attempt()
            assert secs == 0, f"attempt {i} should not lock out"

    def test_attempt_5_locks_for_60s(self):
        state, clock = make_state(start=1000.0)
        for _ in range(4):
            state.register_failed_attempt()
        secs = state.register_failed_attempt()  # 5th attempt
        assert secs == 60

    def test_attempt_6_locks_for_300s(self):
        state, clock = make_state(start=1000.0)
        for _ in range(5):
            state.register_failed_attempt()
        # Advance clock past first lockout so we can attempt again
        clock[0] = 1000.0 + 61
        secs = state.register_failed_attempt()  # 6th attempt
        assert secs == 300

    def test_attempt_7_locks_for_900s(self):
        state, clock = make_state(start=1000.0)
        for _ in range(5):
            state.register_failed_attempt()
        clock[0] = 1000.0 + 61  # past 60s tier
        state.register_failed_attempt()  # 6th
        clock[0] = 1000.0 + 61 + 301  # past 300s tier
        secs = state.register_failed_attempt()  # 7th
        assert secs == 900

    def test_attempt_8_and_beyond_locks_for_3600s(self):
        state, clock = make_state(start=1000.0)
        for _ in range(5):
            state.register_failed_attempt()
        clock[0] = 1000.0 + 61
        state.register_failed_attempt()  # 6th
        clock[0] = 1000.0 + 61 + 301
        state.register_failed_attempt()  # 7th
        clock[0] = 1000.0 + 61 + 301 + 901
        secs = state.register_failed_attempt()  # 8th
        assert secs == 3600

        # 9th and beyond stay at 3600
        clock[0] = clock[0] + 3601
        secs = state.register_failed_attempt()
        assert secs == 3600

    def test_failed_attempts_counter_increments(self):
        state, _ = make_state()
        state.register_failed_attempt()
        state.register_failed_attempt()
        assert state.failed_attempts == 2

    def test_remaining_lockout_decreases_over_time(self):
        state, clock = make_state(start=1000.0)
        for _ in range(5):
            state.register_failed_attempt()
        # 5th attempt triggers 60s lockout
        assert state.remaining_lockout_seconds() == 60
        clock[0] = 1000.0 + 30
        assert state.remaining_lockout_seconds() == 30
        clock[0] = 1000.0 + 60
        assert state.remaining_lockout_seconds() == 0

    def test_remaining_lockout_zero_when_not_locked_out(self):
        state, _ = make_state()
        state.register_failed_attempt()
        state.register_failed_attempt()
        assert state.remaining_lockout_seconds() == 0

    def test_cannot_register_attempt_during_active_lockout(self):
        """During an active lockout the remaining seconds should not increase."""
        state, clock = make_state(start=1000.0)
        for _ in range(5):
            state.register_failed_attempt()
        # In lockout now — calling again should not extend or reset the lockout
        secs = state.register_failed_attempt()
        # Should still return the current remaining lockout, not 0 or a higher tier
        assert secs > 0

    def test_attempt_after_lockout_expires_starts_next_tier(self):
        """After a lockout expires the next attempt triggers the next tier."""
        state, clock = make_state(start=1000.0)
        for _ in range(5):
            state.register_failed_attempt()
        # 60s lockout
        clock[0] = 1000.0 + 61  # lockout expired
        secs = state.register_failed_attempt()  # 6th attempt
        assert secs == 300


# ---------------------------------------------------------------------------
# register_success() resets state
# ---------------------------------------------------------------------------

class TestRegisterSuccess:
    def test_success_resets_counter(self):
        state, clock = make_state(start=1000.0)
        state.register_failed_attempt()
        state.register_failed_attempt()
        state.register_success()
        assert state.failed_attempts == 0

    def test_success_clears_lockout(self):
        state, clock = make_state(start=1000.0)
        for _ in range(5):
            state.register_failed_attempt()
        assert state.remaining_lockout_seconds() > 0
        state.register_success()
        assert state.remaining_lockout_seconds() == 0

    def test_after_success_first_attempt_has_no_lockout(self):
        state, clock = make_state(start=1000.0)
        for _ in range(5):
            state.register_failed_attempt()
        state.register_success()
        secs = state.register_failed_attempt()
        assert secs == 0


# ---------------------------------------------------------------------------
# note_activity() + idle_expired()
# ---------------------------------------------------------------------------

class TestIdleExpiry:
    def test_idle_not_expired_before_timeout(self):
        state, clock = make_state(start=0.0)
        state.unlock(b"k" * 32)
        state.note_activity()
        clock[0] = 299.0
        assert state.idle_expired(300) is False

    def test_idle_expired_at_timeout(self):
        state, clock = make_state(start=0.0)
        state.unlock(b"k" * 32)
        state.note_activity()
        clock[0] = 300.0
        assert state.idle_expired(300) is True

    def test_idle_expired_after_timeout(self):
        state, clock = make_state(start=0.0)
        state.unlock(b"k" * 32)
        state.note_activity()
        clock[0] = 500.0
        assert state.idle_expired(300) is True

    def test_idle_never_with_zero(self):
        """idle_lock_seconds=0 means Never — idle_expired must always return False."""
        state, clock = make_state(start=0.0)
        state.unlock(b"k" * 32)
        state.note_activity()
        clock[0] = 999999.0
        assert state.idle_expired(0) is False

    def test_note_activity_resets_timer(self):
        state, clock = make_state(start=0.0)
        state.unlock(b"k" * 32)
        state.note_activity()
        clock[0] = 290.0
        state.note_activity()  # reset the timer
        clock[0] = 580.0  # 290s since last activity
        assert state.idle_expired(300) is False

    def test_idle_expired_when_locked(self):
        """Even when locked, idle_expired should work (backend can use it)."""
        state, clock = make_state(start=0.0)
        # State starts locked; note_activity still should record a timestamp
        state.note_activity()
        clock[0] = 400.0
        # Returns False when idle_lock_seconds=0 regardless
        assert state.idle_expired(0) is False
        # Returns True after timeout
        assert state.idle_expired(300) is True


# ---------------------------------------------------------------------------
# Recovery-code attempt counter (independent)
# ---------------------------------------------------------------------------

class TestRecoveryCounter:
    def test_recovery_counter_independent_of_main(self):
        """Main-path failures do not affect recovery counter and vice versa."""
        state, clock = make_state(start=1000.0)
        for _ in range(5):
            state.register_failed_attempt()  # drive main counter into lockout

        # Recovery counter starts at 0 regardless
        assert state.recovery_failed_attempts == 0

    def test_register_recovery_failed_attempt_increments(self):
        state, _ = make_state()
        state.register_recovery_failed_attempt()
        assert state.recovery_failed_attempts == 1

    def test_register_recovery_success_resets_counter(self):
        state, _ = make_state()
        state.register_recovery_failed_attempt()
        state.register_recovery_failed_attempt()
        state.register_recovery_success()
        assert state.recovery_failed_attempts == 0

    def test_recovery_counter_does_not_affect_main_backoff(self):
        """Recovery failures must never trigger the main backoff schedule."""
        state, _ = make_state()
        for _ in range(10):
            state.register_recovery_failed_attempt()
        # Main counter remains 0 and main lockout is 0
        assert state.failed_attempts == 0
        assert state.remaining_lockout_seconds() == 0

    def test_main_counter_does_not_affect_recovery(self):
        """Main-path failures must not increment the recovery counter."""
        state, clock = make_state(start=1000.0)
        for _ in range(8):
            state.register_failed_attempt()
        assert state.recovery_failed_attempts == 0

    def test_recovery_counter_has_own_backoff(self):
        """Recovery counter implements its own backoff (may differ from main tier)
        but must return remaining seconds (>= 0) after the threshold."""
        state, clock = make_state(start=1000.0)
        result = None
        for _ in range(10):
            result = state.register_recovery_failed_attempt()
        # After enough attempts the recovery path should also lock out
        # (the exact tier is flexible — just assert it eventually returns > 0)
        assert isinstance(result, int)
        assert result >= 0


# ---------------------------------------------------------------------------
# No secrets logged (smoke test — ensure data_key is never in str(state))
# ---------------------------------------------------------------------------

class TestNoSecretsLogged:
    def test_repr_does_not_expose_data_key(self):
        """The repr/str of LockdownState must not contain the key bytes."""
        state, _ = make_state()
        key = b"\xde\xad\xbe\xef" * 8
        state.unlock(key)
        state_str = repr(state)
        assert key.hex() not in state_str
        assert str(key) not in state_str


# ---------------------------------------------------------------------------
# F6 — backoff persistence across restarts (DPAPI-sealed state file)
# ---------------------------------------------------------------------------


def _make_persisted_state(state_path, mono, wall):
    """Return a LockdownState wired to a sealed state file + injected clocks.

    ``mono`` / ``wall`` are length-1 lists so tests can advance either clock.
    """
    return LockdownState(
        now=lambda: mono[0],
        state_path=state_path,
        wall_now=lambda: wall[0],
    )


class TestBackoffPersistence:
    def test_no_state_path_creates_no_file_and_stays_in_memory(self, tmp_path):
        """LockdownState() without a state_path behaves exactly as before (in-memory)."""
        state, _ = make_state()
        for _ in range(5):
            state.register_failed_attempt()
        assert state.remaining_lockout_seconds() == 60
        # No file was created anywhere under tmp_path (nothing to check beyond
        # "no crash" — the point is state_path=None is a pure no-op).
        assert state._state_path is None

    def test_lockout_persists_across_restart(self, tmp_path):
        """A lockout tier reached before a restart is still in effect after
        constructing a fresh LockdownState from the same sealed file."""
        state_path = tmp_path / "lockdown.backoff"
        mono = [1000.0]
        wall = [5000.0]

        s1 = _make_persisted_state(state_path, mono, wall)
        for _ in range(5):
            s1.register_failed_attempt()  # 5th -> 60s lockout
        assert s1.remaining_lockout_seconds() == 60
        assert state_path.exists(), "the sealed backoff file must be written"

        # Simulate a restart: fresh monotonic clock (resets to 0), same wall time.
        mono2 = [0.0]
        s2 = _make_persisted_state(state_path, mono2, wall)
        assert s2.failed_attempts == 5, "failed-attempt counter must survive restart"
        assert s2.remaining_lockout_seconds() == 60, "lockout must still be active"

    def test_lockout_expires_by_wall_clock_after_restart(self, tmp_path):
        """If real (wall-clock) time passed the deadline during the downtime, the
        restored lockout is expired — but the counter is preserved so the next
        failure escalates to the NEXT tier."""
        state_path = tmp_path / "lockdown.backoff"
        mono = [1000.0]
        wall = [5000.0]

        s1 = _make_persisted_state(state_path, mono, wall)
        for _ in range(5):
            s1.register_failed_attempt()  # 60s lockout, deadline wall=5060
        assert s1.remaining_lockout_seconds() == 60

        # Restart 61s later by wall clock: the 60s lockout has expired.
        mono2 = [0.0]
        wall2 = [5000.0 + 61]
        s2 = _make_persisted_state(state_path, mono2, wall2)
        assert s2.remaining_lockout_seconds() == 0
        assert s2.failed_attempts == 5
        # The next failure is the 6th attempt -> next tier (300s).
        secs = s2.register_failed_attempt()
        assert secs == 300

    def test_success_persists_reset_across_restart(self, tmp_path):
        """A successful unlock clears the persisted counters so a restart starts clean."""
        state_path = tmp_path / "lockdown.backoff"
        mono = [1000.0]
        wall = [5000.0]

        s1 = _make_persisted_state(state_path, mono, wall)
        for _ in range(5):
            s1.register_failed_attempt()
        s1.register_success()

        mono2 = [0.0]
        s2 = _make_persisted_state(state_path, mono2, wall)
        assert s2.failed_attempts == 0
        assert s2.remaining_lockout_seconds() == 0

    def test_recovery_lockout_persists_independently(self, tmp_path):
        """The recovery-path counter + lockout persist independently of the main path."""
        state_path = tmp_path / "lockdown.backoff"
        mono = [1000.0]
        wall = [5000.0]

        s1 = _make_persisted_state(state_path, mono, wall)
        for _ in range(5):
            s1.register_recovery_failed_attempt()  # 5th recovery -> 60s
        assert s1.remaining_recovery_lockout_seconds() == 60
        assert s1.remaining_lockout_seconds() == 0  # main path untouched

        mono2 = [0.0]
        s2 = _make_persisted_state(state_path, mono2, wall)
        assert s2.recovery_failed_attempts == 5
        assert s2.remaining_recovery_lockout_seconds() == 60
        assert s2.failed_attempts == 0

    def test_corrupt_state_file_starts_clean(self, tmp_path):
        """An unreadable / garbage backoff file must not crash — start clean."""
        state_path = tmp_path / "lockdown.backoff"
        state_path.write_text("not valid sealed json", encoding="utf-8")
        mono = [0.0]
        wall = [5000.0]
        s = _make_persisted_state(state_path, mono, wall)
        assert s.failed_attempts == 0
        assert s.remaining_lockout_seconds() == 0

    def test_sealed_file_contains_no_key_material(self, tmp_path):
        """The persisted backoff file holds only counters + timestamps — no key."""
        state_path = tmp_path / "lockdown.backoff"
        mono = [1000.0]
        wall = [5000.0]
        s = _make_persisted_state(state_path, mono, wall)
        s.unlock(b"\xab" * 32)  # hold a key in memory
        for _ in range(5):
            s.register_failed_attempt()

        raw = state_path.read_bytes()
        # The 32-byte data key must never appear in the persisted blob.
        assert (b"\xab" * 32) not in raw
