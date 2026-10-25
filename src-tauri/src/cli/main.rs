//! `whisperstream-cli`, installed on PATH as `whisperstream`. Wraps
//! `whisperstream claude <args>` so a Claude Code session can be replied to
//! by voice from Whisperstream long after a hook's grace window closes.
//!
//! This binary is intentionally separate from the Tauri shell (`whisperstream`
//! crate binary target, `src/main.rs`): it never opens a window, never loads
//! webview or model resources, and must keep working when Whisperstream itself
//! is absent, crashed, restarting, or updating (scope decision 9).

mod args;
mod bridge;
#[cfg(windows)]
mod console_guard;
#[cfg(unix)]
#[path = "console_guard_unix.rs"]
mod console_guard;
mod notices;
mod relay;

use std::env;
use std::process::ExitCode;

use args::Command;

fn main() -> ExitCode {
    // Best-effort bridge cleanup on a panic anywhere in this process. Console
    // restoration on unwind is already handled by `RawModeGuard`'s `Drop`
    // (relay.rs); this hook only covers the backend-side session record, and
    // only ever with a short timeout (Task 3.1's `deregister_active_best_effort`).
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        bridge::deregister_active_best_effort();
        default_hook(info);
    }));

    let argv: Vec<_> = env::args_os().skip(1).collect();
    match args::parse(argv) {
        Command::Help => {
            print!("{}", args::USAGE);
            ExitCode::SUCCESS
        }
        Command::UsageError => {
            eprint!("{}", args::USAGE);
            ExitCode::FAILURE
        }
        Command::Agent { agent, forwarded } => {
            let (bridge, status) = if args::is_remote_codex(agent, &forwarded) {
                (None, notices::BackendStatus::RemoteUnsupported)
            } else {
                bridge::Bridge::start(agent)
            };
            notices::print_startup_notices(agent, status);
            relay::run(agent, forwarded, bridge)
        }
    }
}
