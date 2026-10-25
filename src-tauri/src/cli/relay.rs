//! Raw-byte PTY relay: spawns the real `claude` executable inside a
//! `portable-pty` PTY and moves bytes verbatim between it and this process's
//! own console, in both directions, until the child exits.
//!
//! Promoted from the standalone `pty-proxy` spike crate (since retired). This is a
//! **promotion, not a rewrite**: the spike passed a manual parity test against
//! Claude Code 2.1.220 on Windows Terminal, so its shape is load-bearing. The one deliberate change is dropping
//! `crossterm`: Task 1.1 ruled it out of this crate's dependency graph, and
//! everything crossterm did here (raw mode, terminal size) is reproduced
//! below directly against the `windows` crate this workspace already pins,
//! so no second Win32 binding crate enters the tree.
//!
//! The spike's Ctrl-G "compose" demo (typing a message that gets wrapped in
//! bracketed paste and sent to the child) is not carried forward: it only
//! existed to prove bracketed-paste injection works at all. Production
//! injection arrives from Whisperstream over the bridge's reply long-poll
//! (Task 3.3, `bridge::Bridge::spawn_reply_thread`), not from this process's
//! own stdin, so the stdin thread here stays pure passthrough.
//!
//! # Threading topology
//!
//! A PTY relay is inherently concurrent: parent-stdin -> child and
//! child-stdout -> parent-stdout are independent blocking streams, and
//! Windows pipes do not support `select()`. Threads are the standard answer.
//!
//!   - READER  thread: child stdout -> our stdout, flushed on every chunk.
//!   - WRITER  thread: drains an mpsc channel -> the PTY master's one-shot writer.
//!   - STDIN   thread: raw stdin bytes -> the same channel, verbatim.
//!   - REPLY   thread: long-polls the backend for a dictated reply and feeds
//!                     the same channel a bracketed-paste frame (Task 3.3).
//!                     Lives in `bridge.rs`; it shares the channel rather than
//!                     the writer because `take_writer` is one-shot.
//!   - RESIZE  thread: polls terminal size; on change, calls `master.resize()`.
//!                     There is no SIGWINCH on Windows, and consuming stdin
//!                     through an event loop to detect resizes would break
//!                     the verbatim-forwarding guarantee above.

use std::env;
use std::ffi::{OsStr, OsString};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::Duration;

use anyhow::Result;
use portable_pty::{native_pty_system, CommandBuilder, MasterPty, PtySize};
#[cfg(windows)]
use windows::Win32::System::Console::{
    GetConsoleScreenBufferInfo, GetStdHandle, SetConsoleMode, CONSOLE_MODE,
    CONSOLE_SCREEN_BUFFER_INFO, ENABLE_ECHO_INPUT, ENABLE_LINE_INPUT, ENABLE_PROCESSED_INPUT,
    ENABLE_VIRTUAL_TERMINAL_INPUT, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE,
};

use crate::args::Agent;
use crate::bridge::{Bridge, PtyWriteBatch};
use crate::console_guard;

const RELAY_COMPLETION_POLL: Duration = Duration::from_millis(50);

#[derive(Debug, PartialEq, Eq)]
enum RelayCompletion<T> {
    ChildExited(T),
    ShutdownRequested,
}

fn wait_for_relay_completion<T, E>(
    mut try_wait: impl FnMut() -> Result<Option<T>, E>,
    mut shutdown_requested: impl FnMut() -> bool,
    poll_interval: Duration,
) -> Result<RelayCompletion<T>, E> {
    loop {
        if let Some(status) = try_wait()? {
            return Ok(RelayCompletion::ChildExited(status));
        }
        if shutdown_requested() {
            return Ok(RelayCompletion::ShutdownRequested);
        }
        thread::sleep(poll_interval);
    }
}

/// Locked copy. Do not paraphrase.
pub const CLAUDE_NOT_FOUND: &str = "\
Couldn't find claude on your PATH.
Install Claude Code, or open a new terminal if you just installed it.
";

pub const CODEX_NOT_FOUND: &str = "\
Couldn't find codex on your PATH.
Install Codex CLI, or open a new terminal if you just installed it.
";

/// Default `PATHEXT` used only if the environment variable is unset, which
/// does not happen on a real Windows install but keeps resolution total.
#[cfg(windows)]
const DEFAULT_PATHEXT: &str = ".COM;.EXE;.BAT;.CMD";

/// Restores the console's prior input mode on every *graceful* exit path out
/// of `relay`, including an early `?` return and a panic unwind on the thread
/// that holds it, because `Drop` runs on the same thread that owns the guard.
/// Hard-kill paths (Ctrl+C, Ctrl+Break, console window close) don't run a
/// `Drop` at all: the OS delivers those via a `SetConsoleCtrlHandler` callback
/// on its own injected thread rather than unwinding this one. See
/// `console_guard`, which owns that handler and the process-global prior-mode
/// state a thread that cannot borrow this guard still needs to read.
/// `TerminateProcess` is not reachable by either mechanism: nothing runs
/// inside a process the OS has already torn down (see spike-findings.md and
/// scope.md decision 8 -- this is exactly why the app-side rule in Task 3.4
/// forbids Whisperstream from ever sending this wrapper one).
struct RawModeGuard;

impl Drop for RawModeGuard {
    fn drop(&mut self) {
        console_guard::restore();
    }
}

/// Puts the console's input handle into the mode ConPTY needs: no line
/// buffering, no local echo, and no signal translation on our side (the
/// *child*, running inside the PTY, is the one that interprets Ctrl+C and
/// friends), plus `ENABLE_VIRTUAL_TERMINAL_INPUT` so the console delivers VT
/// escape sequences on stdin as raw bytes instead of translating them into
/// Win32 console key-event records that a byte-oriented relay cannot
/// forward. `console_guard::install` captures the prior mode (exactly once)
/// and registers the hard-kill handler before this function computes and
/// applies the raw-mode bits on top of that same captured value.
#[cfg(windows)]
fn enable_raw_console_input() -> Result<()> {
    console_guard::install()?;
    let base = console_guard::captured_mode().unwrap_or(0);
    let raw = CONSOLE_MODE(
        (base & !(ENABLE_ECHO_INPUT.0 | ENABLE_LINE_INPUT.0 | ENABLE_PROCESSED_INPUT.0))
            | ENABLE_VIRTUAL_TERMINAL_INPUT.0,
    );
    unsafe {
        let handle = GetStdHandle(STD_INPUT_HANDLE)?;
        SetConsoleMode(handle, raw)?;
    }
    Ok(())
}

/// Current console window size in `(cols, rows)`, read from the output
/// buffer's window rectangle. `portable-pty`'s `PtySize` wants exactly this
/// shape.
#[cfg(windows)]
fn terminal_size() -> Result<(u16, u16)> {
    unsafe {
        let handle = GetStdHandle(STD_OUTPUT_HANDLE)?;
        let mut info = CONSOLE_SCREEN_BUFFER_INFO::default();
        GetConsoleScreenBufferInfo(handle, &mut info)?;
        let cols = (info.srWindow.Right - info.srWindow.Left + 1) as u16;
        let rows = (info.srWindow.Bottom - info.srWindow.Top + 1) as u16;
        Ok((cols, rows))
    }
}

#[cfg(unix)]
fn enable_raw_console_input() -> Result<()> {
    console_guard::install()
}

#[cfg(unix)]
fn terminal_size() -> Result<(u16, u16)> {
    let mut size: libc::winsize = unsafe { std::mem::zeroed() };
    if unsafe { libc::ioctl(libc::STDOUT_FILENO, libc::TIOCGWINSZ, &mut size) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok((size.ws_col.max(1), size.ws_row.max(1)))
}

/// Resolves the selected agent executable on `PATH`, honoring `PATHEXT`.
/// `portable_pty::CommandBuilder` does its own PATH+PATHEXT search
/// internally when handed a bare program name, and that internal search is
/// exactly what the spike relied on when it passed a literal command name
/// straight through. We duplicate the same search here, ahead of spawning,
/// purely so a missing agent can be reported with our own
/// locked message instead of a raw `CreateProcessW` error surfacing from
/// deep inside `portable-pty`.
fn which_agent(agent: Agent) -> Option<PathBuf> {
    let path = env::var_os("PATH")?;
    #[cfg(unix)]
    {
        which_program_in(agent.slug(), &path, OsStr::new(""))
    }
    #[cfg(windows)]
    {
    let pathext = env::var_os("PATHEXT").unwrap_or_else(|| DEFAULT_PATHEXT.into());
    which_program_in(agent.slug(), &path, &pathext)
    }
}

#[cfg(unix)]
fn which_program_in(program: &str, path: &OsStr, _pathext: &OsStr) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    env::split_paths(path).map(|dir| dir.join(program)).find(|candidate| {
        candidate.is_file() && candidate.metadata()
            .map(|meta| meta.permissions().mode() & 0o111 != 0).unwrap_or(false)
    })
}

#[cfg(test)]
fn which_in(path: &OsStr, pathext: &OsStr) -> Option<PathBuf> {
    which_program_in("claude", path, pathext)
}

/// Resolve a Windows command using executable extensions before an
/// extensionless fallback. npm installs both a POSIX shim and `codex.CMD` in
/// the same directory; choosing the bare shim first hands CreateProcessW a
/// non-Windows script and fails before the PTY can paint.
#[cfg(windows)]
fn which_program_in(program: &str, path: &OsStr, pathext: &OsStr) -> Option<PathBuf> {
    for dir in env::split_paths(path) {
        for ext in env::split_paths(pathext) {
            let ext = ext.to_string_lossy();
            let ext = ext.trim_start_matches('.');
            if ext.is_empty() {
                continue;
            }
            let candidate = dir.join(format!("{program}.{ext}"));
            if candidate.is_file() {
                return Some(candidate);
            }
        }
        let bare = dir.join(program);
        if bare.is_file() {
            return Some(bare);
        }
    }
    None
}

#[derive(Debug, PartialEq, Eq)]
struct ResolvedCommand {
    program: PathBuf,
    prefix_args: Vec<OsString>,
}

/// Convert a resolved path into the process shape portable-pty can spawn.
/// CreateProcessW cannot execute batch scripts directly, so `.cmd` and `.bat`
/// enter through the system command processor with expansion disabled.
fn resolved_command(path: &Path) -> ResolvedCommand {
    let is_command_script = path
        .extension()
        .and_then(OsStr::to_str)
        .is_some_and(|extension| {
            extension.eq_ignore_ascii_case("cmd") || extension.eq_ignore_ascii_case("bat")
        });
    if cfg!(windows) && is_command_script {
        ResolvedCommand {
            program: PathBuf::from("cmd.exe"),
            prefix_args: vec![
                OsString::from("/D"),
                OsString::from("/S"),
                OsString::from("/V:OFF"),
                OsString::from("/C"),
                path.as_os_str().to_owned(),
            ],
        }
    } else {
        ResolvedCommand {
            program: path.to_owned(),
            prefix_args: Vec::new(),
        }
    }
}

/// Resolves `claude` and, if found, relays it inside a PTY until it exits,
/// returning the process's own exit code as our exit code. If `claude`
/// cannot be resolved, prints the locked not-found message and returns a
/// failure code *without* touching console mode: nothing has been captured
/// yet, so there is nothing to restore.
///
/// `bridge` is the Task 3.1 backend bridge. It exists even when the backend is
/// initially absent, so its stable correlation token is always exported into
/// the child and its reply thread can register after Whisperstream starts.
pub fn run(agent: Agent, forwarded_args: Vec<OsString>, bridge: Option<Arc<Bridge>>) -> ExitCode {
    let agent_path = match which_agent(agent) {
        Some(path) => path,
        None => {
            eprint!(
                "{}",
                match agent {
                    Agent::Claude => CLAUDE_NOT_FOUND,
                    Agent::Codex => CODEX_NOT_FOUND,
                }
            );
            return ExitCode::FAILURE;
        }
    };

    match relay(&agent_path, forwarded_args, bridge) {
        Ok(code) => ExitCode::from((code & 0xFF) as u8),
        Err(err) => {
            eprintln!("whisperstream: {err:#}");
            ExitCode::FAILURE
        }
    }
}

/// Spawns `claude_path` inside a PTY sized to the current console and pumps
/// bytes verbatim in both directions until the child exits. See the module
/// doc for the full thread topology and why each thread exists.
fn relay(
    claude_path: &Path,
    forwarded_args: Vec<OsString>,
    bridge: Option<Arc<Bridge>>,
) -> Result<i32> {
    let (cols, rows) = terminal_size()?;

    enable_raw_console_input()?;
    // See `RawModeGuard`'s doc comment: this must be bound immediately after
    // the mode switch succeeds, and before anything else that could fail,
    // so every subsequent early return restores the console.
    let _guard = RawModeGuard;

    let pty_system = native_pty_system();
    let pair = pty_system.openpty(PtySize {
        rows,
        cols,
        pixel_width: 0,
        pixel_height: 0,
    })?;

    let resolved = resolved_command(claude_path);
    let mut cmd = CommandBuilder::new(&resolved.program);
    // portable-pty otherwise defaults to HOME on Unix. Preserve the repository
    // from which the user launched the wrapper on both platforms.
    cmd.cwd(env::current_dir()?);
    cmd.args(resolved.prefix_args);
    cmd.args(forwarded_args);
    // Task 2.4's forward-hook.ps1 reads this and echoes it back as a header
    // on every hook it forwards, which is how the backend correlates a hook
    // event to this managed session.
    if let Some(bridge) = &bridge {
        cmd.env(
            crate::bridge::SESSION_TOKEN_ENV_VAR,
            bridge.correlation_token(),
        );
    }
    let mut child = pair.slave.spawn_command(cmd)?;

    // Critical portable-pty gotcha: drop the slave immediately after spawn,
    // or the reader thread below never observes EOF when the child exits.
    drop(pair.slave);

    // `try_clone_reader` clones; `take_writer` is one-shot. Both must happen
    // before the master is wrapped in the mutex, since only `resize()` needs it.
    let mut reader = pair.master.try_clone_reader()?;
    let mut writer = pair.master.take_writer()?;
    let master: Arc<Mutex<Box<dyn MasterPty + Send>>> = Arc::new(Mutex::new(pair.master));

    let (tx, rx) = mpsc::channel::<PtyWriteBatch>();
    let running = Arc::new(AtomicBool::new(true));

    // REPLY: long-polls for a dictated reply and writes it to the PTY master
    // through the same channel the stdin pump feeds (Task 3.3), so the
    // one-shot writer stays single-owner. The poll also carries liveness and
    // graceful-shutdown state. When the backend is absent or restarting, this
    // an independent lifecycle monitor re-registers without touching the PTY
    // relay or waiting for a stale long-poll to time out.
    if let Some(bridge) = &bridge {
        bridge.spawn_reply_thread(Arc::clone(&running), tx.clone());
    }

    // READER: child stdout -> our stdout.
    //
    // Do NOT use `io::copy`. Rust's `Stdout` is a `LineWriter` that only
    // flushes on '\n'; a TUI's first frame paints with escape sequences and
    // no newline, so an unflushed copy would sit behind a live process
    // forever. Flush every chunk explicitly.
    thread::spawn(move || {
        let mut buf = [0u8; 8192];
        let stdout = std::io::stdout();
        loop {
            match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    let mut lock = stdout.lock();
                    if lock.write_all(&buf[..n]).is_err() || lock.flush().is_err() {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
    });

    // WRITER: single owner of the one-shot PTY writer, fed by a channel so a
    // future injection path (Task 3.3) and the stdin pump below never
    // contend for the writer directly.
    thread::spawn(move || {
        while let Ok(batch) = rx.recv() {
            for (index, bytes) in batch.frames.into_iter().enumerate() {
                if index > 0 && !batch.inter_frame_delay.is_zero() {
                    thread::sleep(batch.inter_frame_delay);
                }
                if writer.write_all(&bytes).is_err() {
                    return;
                }
                let _ = writer.flush();
            }
        }
    });

    // RESIZE: poll, because there is no SIGWINCH on Windows and consuming
    // stdin through an event loop to detect resizes would break verbatim
    // forwarding.
    let resize_master = Arc::clone(&master);
    let resize_running = Arc::clone(&running);
    thread::spawn(move || {
        let mut last = (cols, rows);
        while resize_running.load(Ordering::SeqCst) {
            thread::sleep(Duration::from_millis(150));
            if let Ok(current) = terminal_size() {
                if current != last {
                    last = current;
                    if let Ok(m) = resize_master.lock() {
                        let _ = m.resize(PtySize {
                            rows: current.1,
                            cols: current.0,
                            pixel_width: 0,
                            pixel_height: 0,
                        });
                    }
                }
            }
        }
    });

    // STDIN: raw bytes forwarded verbatim, never parsed into key events and
    // re-encoded. ConPTY emits a Device Status Report (`ESC[6n`) at startup
    // and blocks until it sees the terminal's reply arrive on stdin; a
    // parser that does not recognize that reply as a "key" swallows it,
    // ConPTY never unblocks, and the child never paints (diagnosed with
    // `conpty_diag`, see spike-findings.md). The same class of bug applies to
    // focus events, device attribute reports, and mouse reports, so this
    // thread does no interpretation of any kind.
    let stdin_tx = tx.clone();
    let stdin_running = Arc::clone(&running);
    thread::spawn(move || {
        let mut stdin = std::io::stdin();
        let mut buf = [0u8; 4096];
        while stdin_running.load(Ordering::SeqCst) {
            match stdin.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    if stdin_tx
                        .send(PtyWriteBatch::immediate(buf[..n].to_vec()))
                        .is_err()
                    {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
    });

    // `Child::wait` blocks forever and cannot observe the app's graceful-exit
    // request. Poll the child and the bridge together instead. A request exits
    // this function normally: local PTY handles are dropped, RawModeGuard
    // restores the parent console, and no TerminateProcess-style API is used.
    let completion = wait_for_relay_completion(
        || child.try_wait(),
        || {
            bridge
                .as_ref()
                .is_some_and(|bridge| bridge.shutdown_was_requested())
        },
        RELAY_COMPLETION_POLL,
    )?;
    running.store(false, Ordering::SeqCst);
    drop(tx);

    // Normal-exit deregistration (Task 3.1). Hard-kill and panic paths run
    // their own best-effort deregister (`console_guard`'s handler, the panic
    // hook in `main.rs`) since neither reaches this line.
    if let Some(bridge) = &bridge {
        bridge.deregister();
    }

    Ok(match completion {
        RelayCompletion::ChildExited(status) => status.exit_code() as i32,
        RelayCompletion::ShutdownRequested => 0,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn claude_not_found_message_matches_locked_copy_byte_for_byte() {
        let expected = "Couldn't find claude on your PATH.\n\
Install Claude Code, or open a new terminal if you just installed it.\n";
        assert_eq!(CLAUDE_NOT_FOUND, expected);
    }

    #[test]
    fn codex_not_found_message_matches_locked_copy_byte_for_byte() {
        let expected = "Couldn't find codex on your PATH.\n\
Install Codex CLI, or open a new terminal if you just installed it.\n";
        assert_eq!(CODEX_NOT_FOUND, expected);
    }

    #[test]
    fn which_in_returns_none_when_claude_is_absent_from_every_path_dir() {
        let dir = std::env::temp_dir().join("whisperstream-cli-test-which-absent");
        fs::create_dir_all(&dir).unwrap();
        let path = std::env::join_paths([&dir]).unwrap();
        assert_eq!(which_in(&path, OsStr::new(".EXE")), None);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn which_in_finds_a_bare_extensionless_claude() {
        let dir = std::env::temp_dir().join("whisperstream-cli-test-which-bare");
        fs::create_dir_all(&dir).unwrap();
        let claude = dir.join("claude");
        fs::write(&claude, b"").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&claude, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let path = std::env::join_paths([&dir]).unwrap();
        assert_eq!(which_in(&path, OsStr::new(".EXE")), Some(claude));
        let _ = fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn which_in_finds_claude_via_pathext() {
        let dir = std::env::temp_dir().join("whisperstream-cli-test-which-pathext");
        fs::create_dir_all(&dir).unwrap();
        let claude = dir.join("claude.CMD");
        fs::write(&claude, b"").unwrap();
        let path = std::env::join_paths([&dir]).unwrap();
        assert_eq!(which_in(&path, OsStr::new(".EXE;.CMD")), Some(claude));
        let _ = fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn codex_resolution_prefers_pathext_cmd_over_the_posix_bare_shim() {
        let dir = std::env::temp_dir().join("whisperstream-cli-test-codex-cmd-first");
        fs::create_dir_all(&dir).unwrap();
        let shim = dir.join("codex");
        let command = dir.join("codex.CMD");
        fs::write(&shim, b"#!/bin/sh").unwrap();
        fs::write(&command, b"@echo off").unwrap();
        let path = std::env::join_paths([&dir]).unwrap();

        assert_eq!(
            which_program_in("codex", &path, OsStr::new(".EXE;.CMD")),
            Some(command)
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn direct_executable_resolution_honors_pathext_order() {
        let dir = std::env::temp_dir().join("whisperstream-cli-test-codex-exe-first");
        fs::create_dir_all(&dir).unwrap();
        let executable = dir.join("codex.EXE");
        let command = dir.join("codex.CMD");
        fs::write(&executable, b"").unwrap();
        fs::write(&command, b"").unwrap();
        let path = std::env::join_paths([&dir]).unwrap();

        assert_eq!(
            which_program_in("codex", &path, OsStr::new(".EXE;.CMD")),
            Some(executable)
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn command_scripts_use_cmd_exe_while_native_executables_spawn_directly() {
        let command = resolved_command(Path::new(r"C:\bin\codex.CMD"));
        assert_eq!(command.program, PathBuf::from("cmd.exe"));
        assert_eq!(
            command.prefix_args,
            vec![
                OsString::from("/D"),
                OsString::from("/S"),
                OsString::from("/V:OFF"),
                OsString::from("/C"),
                OsString::from(r"C:\bin\codex.CMD"),
            ]
        );

        let native = resolved_command(Path::new(r"C:\bin\codex.EXE"));
        assert_eq!(native.program, PathBuf::from(r"C:\bin\codex.EXE"));
        assert!(native.prefix_args.is_empty());
    }

    #[test]
    fn relay_wait_returns_a_child_exit_before_a_simultaneous_shutdown_request() {
        let result = wait_for_relay_completion(|| Ok::<_, ()>(Some(17)), || true, Duration::ZERO);
        assert_eq!(result, Ok(RelayCompletion::ChildExited(17)));
    }

    #[test]
    fn relay_wait_observes_a_shutdown_request_without_waiting_for_child_exit() {
        let result =
            wait_for_relay_completion(|| Ok::<_, ()>(None::<i32>), || true, Duration::ZERO);
        assert_eq!(result, Ok(RelayCompletion::ShutdownRequested));
    }

    #[test]
    fn relay_wait_keeps_polling_until_the_child_exits() {
        let mut polls = 0;
        let result = wait_for_relay_completion(
            || {
                polls += 1;
                Ok::<_, ()>((polls == 3).then_some(9))
            },
            || false,
            Duration::ZERO,
        );
        assert_eq!(result, Ok(RelayCompletion::ChildExited(9)));
        assert_eq!(polls, 3);
    }
}
