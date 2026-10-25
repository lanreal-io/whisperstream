//! Console-mode restoration for the wrapper's hard-kill paths: Ctrl+C,
//! Ctrl+Break, and console window close.
//!
//! `RawModeGuard` in `relay.rs` restores the console on every *graceful* exit
//! path, including an early `?` return and a panic unwind, because `Drop`
//! runs on the same thread that owns the guard. None of that runs when the
//! console itself delivers a ctrl event: `SetConsoleCtrlHandler`'s callback
//! executes on an OS-injected thread that cannot borrow anything on the
//! relay's stack, so the prior console mode has to live in a process-global
//! both sides can reach. This module owns that state and the handler.
//!
//! `TerminateProcess` is not one of the events this module -- or anything
//! else in-process -- can catch: nothing runs inside a process the OS has
//! already torn down. That is exactly why scope.md decision 8 and Task 3.4's
//! app-side rule forbid Whisperstream from ever sending this wrapper one.
//! This handler plus `RawModeGuard`'s `Drop` are the only recovery paths that
//! can exist; a hard kill skips both.
//!
//! The handler also best-effort deregisters this run's managed-session
//! bridge (Task 3.1), console restoration first: a hard-kill path's job is
//! to make the console usable again as fast as possible, and the backend
//! record is a lower-priority cleanup with its own short timeout.

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

use anyhow::Result;
use windows::core::BOOL;
use windows::Win32::System::Console::{
    GetConsoleMode, GetStdHandle, SetConsoleCtrlHandler, SetConsoleMode, CONSOLE_MODE,
    CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT, CTRL_C_EVENT, STD_INPUT_HANDLE,
};

/// The console input mode captured before the relay puts the console into
/// raw mode. Meaningless until `CAPTURED` is true.
static PRIOR_MODE: AtomicU32 = AtomicU32::new(0);

/// Whether `PRIOR_MODE` holds a real captured value yet. A plain `AtomicBool`
/// rather than a sentinel value inside `PRIOR_MODE` itself, so "captured" and
/// "the value" can never disagree.
static CAPTURED: AtomicBool = AtomicBool::new(false);

/// Records `mode` as the console's prior state, exactly once per process.
/// Every call after the first is a no-op. Re-entering this on an
/// already-broken console -- one this same process already put into raw mode
/// -- would otherwise overwrite the good captured value with the broken one,
/// and every later restore would then "restore" into the broken state.
pub fn capture(mode: u32) {
    if CAPTURED
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_ok()
    {
        PRIOR_MODE.store(mode, Ordering::SeqCst);
    }
}

/// The captured prior mode, if `capture` has run.
pub fn captured_mode() -> Option<u32> {
    CAPTURED
        .load(Ordering::SeqCst)
        .then(|| PRIOR_MODE.load(Ordering::SeqCst))
}

/// Writes the captured prior mode back to the console's input handle, if one
/// was captured. Safe to call from any thread, including the ctrl-handler
/// callback below; a no-op if nothing was ever captured (e.g. `claude` was
/// never found and the relay never got as far as raw mode).
pub fn restore() {
    if let Some(mode) = captured_mode() {
        unsafe {
            if let Ok(handle) = GetStdHandle(STD_INPUT_HANDLE) {
                let _ = SetConsoleMode(handle, CONSOLE_MODE(mode));
            }
        }
    }
}

/// Captures the console's current input mode (idempotently, see `capture`)
/// and registers the console control handler. Safe to call more than once:
/// Windows simply chains additional handlers, and `capture`'s once-only guard
/// means a repeated call here can never re-capture or corrupt the saved mode.
pub fn install() -> Result<()> {
    unsafe {
        let handle = GetStdHandle(STD_INPUT_HANDLE)?;
        let mut mode = CONSOLE_MODE(0);
        GetConsoleMode(handle, &mut mode)?;
        capture(mode.0);
        SetConsoleCtrlHandler(Some(handler), true)?;
    }
    Ok(())
}

/// Runs on an OS-injected thread, separate from the thread that owns
/// `RawModeGuard`, on `CTRL_C_EVENT`, `CTRL_BREAK_EVENT`, and
/// `CTRL_CLOSE_EVENT`. Restores the console and returns `FALSE` so the
/// default handler (and any other registered handler) still runs afterward --
/// this handler's job is only to restore the console before teardown
/// proceeds, never to block it.
unsafe extern "system" fn handler(ctrl_type: u32) -> BOOL {
    if matches!(ctrl_type, CTRL_C_EVENT | CTRL_BREAK_EVENT | CTRL_CLOSE_EVENT) {
        restore();
        crate::bridge::deregister_active_best_effort();
    }
    BOOL(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capture_records_prior_mode_exactly_once_across_repeated_calls() {
        capture(0x1111);
        capture(0x2222);
        capture(0x3333);
        assert_eq!(captured_mode(), Some(0x1111));
    }
}
