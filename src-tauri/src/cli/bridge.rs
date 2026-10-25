//! HTTP bridge from the wrapper to the Whisperstream backend: capability-
//! token discovery, per-run correlation-token minting, registration,
//! reply long-polling, and deregistration (Task 3.1).
//!
//! Scope decision 9 is a hard constraint on every function in this module:
//! a backend that is absent, crashed, restarting, or updating must never
//! delay, degrade, or fail the Claude session. Every network call carries a
//! short timeout and every error is swallowed after `Bridge::start` reports
//! the one status its caller turns into a startup notice. Nothing here ever
//! panics or blocks the relay.

use std::env;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::json;

use crate::args::Agent;
use crate::notices::BackendStatus;

/// Env var the correlation token is exported under into the Claude child's
/// environment. Task 2.4's `forward-hook.ps1` reads this and echoes it back
/// as a header on every hook it forwards, which is how the backend
/// correlates a hook event to this managed session.
pub const SESSION_TOKEN_ENV_VAR: &str = "WHISPERSTREAM_SESSION_TOKEN";

/// Mirrors `managed_session_auth.HEADER_NAME` in `server.py`.
const CAPABILITY_TOKEN_HEADER: &str = "x-whisperstream-session-token";

const CAPABILITY_TOKEN_FILENAME: &str = "managed-session-token.json";
const DEFAULT_PORT: u16 = 38420;

const REQUEST_TIMEOUT: Duration = Duration::from_secs(3);
/// Used only on the hard-kill and panic paths, where a slow network call
/// risks delaying console restoration or process teardown. Best-effort
/// cleanup is worth attempting but never worth blocking exit for long.
const FAST_TIMEOUT: Duration = Duration::from_secs(1);

/// Client-side ceiling on one long-poll. Must exceed the backend's own hold
/// (`PENDING_LONG_POLL_SECONDS`, 25s, capped at 30s in `managed_sessions.py`)
/// so an ordinary empty poll returns as "no reply available" rather than as a
/// client timeout.
const LONG_POLL_TIMEOUT: Duration = Duration::from_secs(35);

/// Registration is a cheap local probe. Retry promptly enough that a CLI
/// started before the app becomes usable without the user restarting Claude.
const REGISTRATION_BACKOFF: Duration = Duration::from_secs(1);

/// Bracketed-paste framing. Wrapping the payload tells Claude the bytes are
/// pasted, not typed, which is what makes a multi-line dictated reply land as
/// a single message instead of submitting on its first newline. Proven against
/// Claude Code 2.1.220 by the spike (`spike-findings.md`).
const BRACKETED_PASTE_START: &[u8] = b"\x1b[200~";
const BRACKETED_PASTE_END: &[u8] = b"\x1b[201~";

/// Carriage return, the submit byte a terminal sends for Enter. Written after
/// the paste closes, never inside it.
const SUBMIT_BYTE: u8 = b'\r';

/// Codex 0.149 has a Windows-specific raw-input paste detector: a burst flushes
/// after 60ms idle and keeps Enter in "insert newline" mode for 120ms after the
/// last character. Stay comfortably beyond both windows before submitting.
///
/// Do not use explicit bracketed paste here. Codex issue #28167 documents that
/// its explicit-paste path can keep a following Enter in paste mode for several
/// seconds. Raw ConPTY input is the path Codex itself supports on Windows.
const CODEX_RAW_PASTE_SETTLE_DELAY: Duration = Duration::from_millis(250);

#[derive(Debug, PartialEq, Eq)]
pub(crate) struct PtyWriteBatch {
    pub(crate) frames: Vec<Vec<u8>>,
    pub(crate) inter_frame_delay: Duration,
}

impl PtyWriteBatch {
    pub(crate) fn immediate(bytes: Vec<u8>) -> Self {
        Self {
            frames: vec![bytes],
            inter_frame_delay: Duration::ZERO,
        }
    }
}

/// The one bridge instance live for this run, reachable from contexts that
/// cannot borrow anything on the relay's stack: the console-ctrl handler
/// (Task 1.3, an OS-injected thread) and the panic hook installed in
/// `main.rs`. Both are best-effort last-chance cleanup, so losing this race
/// against a concurrent normal deregister is harmless (the store's
/// deregister is idempotent).
static ACTIVE: Mutex<Option<Arc<Bridge>>> = Mutex::new(None);

fn remember_active(bridge: &Arc<Bridge>) {
    if let Ok(mut slot) = ACTIVE.lock() {
        *slot = Some(Arc::clone(bridge));
    }
}

/// Best-effort deregistration for the hard-kill and panic paths. Safe to
/// call even when no bridge was ever established (a no-op) and safe to call
/// more than once (the second call finds nothing, since the first `take`s
/// the slot).
pub fn deregister_active_best_effort() {
    let bridge = ACTIVE.lock().ok().and_then(|mut slot| slot.take());
    if let Some(bridge) = bridge {
        bridge.deregister_with_timeout(FAST_TIMEOUT);
    }
}

#[derive(Clone)]
struct BridgeConnection {
    base_url: String,
    capability_token: String,
    session_handle: String,
}

/// A bridge session that lives for the wrapper's whole run. Its correlation
/// token exists even while Whisperstream is absent, so the Claude child always
/// inherits a stable identity and the reply thread can register it later.
pub struct Bridge {
    agent: Agent,
    data_dir: PathBuf,
    correlation_token: String,
    pid: u32,
    cwd: Option<String>,
    connection: Mutex<Option<BridgeConnection>>,
    /// Serializes the startup registration attempt with the independent
    /// lifecycle monitor. Registration is local and bounded, but
    /// allowing two attempts to race could let an older capability token win
    /// after the app has already rotated it.
    registration: Mutex<()>,
    /// Set once a reply long-poll reports `shutdown_requested: true`
    /// (Task 3.4). **A request, never a kill:** Whisperstream never
    /// `TerminateProcess`es this wrapper (scope decision 8) -- it asks over
    /// this channel, and it is this process's own job to drain itself
    /// through the normal `RawModeGuard` exit path once it observes the
    /// flag. Sticky by design: a request that arrived once should still be
    /// honored even if a later poll's snapshot did not carry it again.
    shutdown_requested: AtomicBool,
}

impl Bridge {
    /// Resolve the data dir, mint the wrapper's stable correlation token, and
    /// make one bounded registration attempt for the startup notice.
    ///
    /// Never blocks longer than one bounded HTTP request, and never panics:
    /// every fallible step degrades to `BackendStatus::NotRunning` rather
    /// than propagating an error.
    pub fn start(agent: Agent) -> (Option<Arc<Bridge>>, BackendStatus) {
        let Some(dir) = resolve_data_dir(
            env::var_os("WHISPERSTREAM_DATA_DIR"),
            if cfg!(target_os = "macos") {
                env::var_os("HOME").map(|home| PathBuf::from(home).join("Library/Application Support").into_os_string())
            } else {
                env::var_os("LOCALAPPDATA")
            },
        ) else {
            return (None, BackendStatus::NotRunning);
        };

        let Some(correlation_token) = mint_correlation_token() else {
            return (None, BackendStatus::NotRunning);
        };

        let bridge = Arc::new(Bridge {
            agent,
            data_dir: dir,
            correlation_token,
            pid: std::process::id(),
            cwd: env::current_dir()
                .ok()
                .and_then(|cwd| cwd.to_str().map(str::to_owned)),
            connection: Mutex::new(None),
            registration: Mutex::new(()),
            shutdown_requested: AtomicBool::new(false),
        });
        remember_active(&bridge);
        let status = bridge.try_register();
        (Some(bridge), status)
    }

    /// The correlation token, exported into the child's environment
    /// (`SESSION_TOKEN_ENV_VAR`) by the caller before spawning Claude.
    pub fn correlation_token(&self) -> &str {
        &self.correlation_token
    }

    /// True once a reply long-poll has reported `shutdown_requested`
    /// (Task 3.4). A caller observing this is expected to drain the relay
    /// through its own normal `RawModeGuard` exit path -- this method only
    /// reports the request, it never acts on it itself.
    pub fn shutdown_was_requested(&self) -> bool {
        self.shutdown_requested.load(Ordering::SeqCst)
    }

    /// Spawns the reply-delivery thread (Task 3.3).
    ///
    /// Long-polls the backend for an accepted reply and, when one arrives,
    /// writes it to the **PTY master** wrapped in bracketed paste followed by
    /// the submit byte. `tx` is a clone of the relay's writer channel, so the
    /// bytes take the identical path the stdin pump uses and never contend for
    /// the one-shot PTY writer; nothing here is ever written to our own stdout,
    /// and nothing is routed through the child's stdin as if typed.
    pub fn spawn_reply_thread(
        self: &Arc<Self>,
        running: Arc<AtomicBool>,
        tx: mpsc::Sender<PtyWriteBatch>,
    ) {
        self.spawn_registration_monitor(Arc::clone(&running));
        let bridge = Arc::clone(self);
        thread::spawn(move || {
            while running.load(Ordering::SeqCst) {
                let Some(connection) = bridge.current_connection() else {
                    thread::sleep(REGISTRATION_BACKOFF);
                    continue;
                };
                let poll_started = Instant::now();
                match poll_pending(
                    &connection.base_url,
                    &connection.capability_token,
                    &connection.session_handle,
                    LONG_POLL_TIMEOUT,
                ) {
                    Ok(outcome) if outcome.shutdown_requested => {
                        bridge.shutdown_requested.store(true, Ordering::SeqCst);
                        break;
                    }
                    Ok(PendingOutcome {
                        reply: Some(payload),
                        ..
                    }) => {
                        if tx.send(reply_write_batch(bridge.agent, &payload)).is_err() {
                            // The relay is gone; there is nothing left to
                            // deliver into.
                            break;
                        }
                        let _ = report_delivered(
                            &connection.base_url,
                            &connection.capability_token,
                            &connection.session_handle,
                            REQUEST_TIMEOUT,
                        );
                    }
                    // A normal empty long-poll already consumed roughly 25s.
                    // If a fail-closed backend boundary returns immediately,
                    // enforce a client-side floor so stale wrappers cannot
                    // busy-loop and starve the app.
                    Ok(PendingOutcome { reply: None, .. }) => {
                        let backoff = empty_poll_backoff(poll_started.elapsed());
                        if !backoff.is_zero() {
                            thread::sleep(backoff);
                        }
                    }
                    Err(()) => {
                        bridge.clear_connection(&connection.session_handle);
                        thread::sleep(REGISTRATION_BACKOFF);
                    }
                }
            }
        });
    }

    /// Watches the app's per-run capability token independently of the reply
    /// long-poll. A poll against a backend that is shutting down can remain
    /// blocked until its 35-second client timeout on Windows. Token rotation
    /// lets this monitor replace that stale connection promptly, while
    /// `clear_connection`'s handle check prevents the old poll from erasing
    /// the replacement when it eventually returns.
    fn spawn_registration_monitor(
        self: &Arc<Self>,
        running: Arc<AtomicBool>,
    ) -> thread::JoinHandle<()> {
        let bridge = Arc::clone(self);
        thread::spawn(move || {
            while running.load(Ordering::SeqCst) {
                if !bridge.registration_is_current() {
                    let _ = bridge.try_register();
                    // The relay may have exited while the bounded register
                    // request was in flight. Clean up a connection published
                    // after its normal deregistration raced past us.
                    if !running.load(Ordering::SeqCst) {
                        bridge.deregister_with_timeout(FAST_TIMEOUT);
                        break;
                    }
                }
                thread::sleep(REGISTRATION_BACKOFF);
            }
        })
    }

    /// Deregisters on the normal child-exit path.
    pub fn deregister(&self) {
        self.deregister_with_timeout(REQUEST_TIMEOUT);
    }

    fn deregister_with_timeout(&self, timeout: Duration) {
        let connection = self.connection.lock().ok().and_then(|mut slot| slot.take());
        if let Some(connection) = connection {
            let _ = deregister(
                &connection.base_url,
                &connection.capability_token,
                &self.correlation_token,
                timeout,
            );
        }
    }

    fn try_register(&self) -> BackendStatus {
        let Ok(_registration) = self.registration.lock() else {
            return BackendStatus::NotRunning;
        };
        let Some(capability_token) = read_capability_token(&self.data_dir) else {
            return BackendStatus::NotRunning;
        };
        let base_url = format!(
            "http://127.0.0.1:{}",
            discover_port(self.agent, &self.data_dir)
        );
        let (session_handle, status) = register(
            self.agent,
            &base_url,
            &capability_token,
            &self.correlation_token,
            self.pid,
            self.cwd.as_deref(),
            REQUEST_TIMEOUT,
        );
        let Some(session_handle) = session_handle else {
            return if status == BackendStatus::Ready {
                BackendStatus::NotRunning
            } else {
                status
            };
        };
        // A restart can rotate the file while the bounded request is in
        // flight. Never publish a connection authenticated for an obsolete
        // backend run.
        if read_capability_token(&self.data_dir).as_deref() != Some(capability_token.as_str()) {
            return BackendStatus::NotRunning;
        }
        let Ok(mut slot) = self.connection.lock() else {
            return BackendStatus::NotRunning;
        };
        *slot = Some(BridgeConnection {
            base_url,
            capability_token,
            session_handle,
        });
        BackendStatus::Ready
    }

    fn registration_is_current(&self) -> bool {
        let Some(capability_token) = read_capability_token(&self.data_dir) else {
            return false;
        };
        let base_url = format!(
            "http://127.0.0.1:{}",
            discover_port(self.agent, &self.data_dir)
        );
        self.current_connection().is_some_and(|connection| {
            connection.capability_token == capability_token && connection.base_url == base_url
        })
    }

    fn current_connection(&self) -> Option<BridgeConnection> {
        self.connection.lock().ok().and_then(|slot| slot.clone())
    }

    fn clear_connection(&self, session_handle: &str) {
        if let Ok(mut slot) = self.connection.lock() {
            if slot
                .as_ref()
                .is_some_and(|connection| connection.session_handle == session_handle)
            {
                *slot = None;
            }
        }
    }
}

fn empty_poll_backoff(elapsed: Duration) -> Duration {
    REGISTRATION_BACKOFF.saturating_sub(elapsed)
}

/// Resolves `<data_dir>`, honoring `WHISPERSTREAM_DATA_DIR` and falling back
/// to `%LOCALAPPDATA%\Whisperstream`, exactly as `forward-hook.ps1` does.
/// Takes its inputs as parameters (rather than reading the environment
/// directly) so tests never have to mutate process-global env state.
fn resolve_data_dir(
    ws_data_dir: Option<OsString>,
    local_app_data: Option<OsString>,
) -> Option<PathBuf> {
    if let Some(dir) = ws_data_dir {
        if !dir.is_empty() {
            return Some(PathBuf::from(dir));
        }
    }
    local_app_data
        .filter(|v| !v.is_empty())
        .map(|local| PathBuf::from(local).join("Whisperstream"))
}

#[derive(Deserialize)]
struct PortFile {
    port: serde_json::Value,
}

/// Reads `<dir>/claude-hud-port.json`, falling back to the default port on
/// any read/parse failure or out-of-range value, exactly as
/// `forward-hook.ps1`'s `$Port = 38420` fallback does.
fn discover_port(agent: Agent, dir: &Path) -> u16 {
    let filename = match agent {
        Agent::Claude => "claude-hud-port.json",
        Agent::Codex => "codex-hud-port.json",
    };
    read_json::<PortFile>(&dir.join(filename))
        .and_then(|f| match f.port {
            serde_json::Value::Number(n) => n.as_u64().and_then(|v| u16::try_from(v).ok()),
            serde_json::Value::String(s) => s.parse::<u16>().ok(),
            _ => None,
        })
        .filter(|port| *port >= 1)
        .unwrap_or(DEFAULT_PORT)
}

#[derive(Deserialize)]
struct CapabilityTokenFile {
    token: String,
}

/// Reads the per-run capability token `managed_session_auth.py` mints at
/// backend startup. `None` on any read/parse failure or an empty value --
/// all treated as "no backend to talk to" by the caller.
fn read_capability_token(dir: &Path) -> Option<String> {
    read_json::<CapabilityTokenFile>(&dir.join(CAPABILITY_TOKEN_FILENAME))
        .map(|f| f.token)
        .filter(|t| !t.is_empty())
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Option<T> {
    let contents = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&contents).ok()
}

/// Mints a random per-run correlation token as 64 hex characters (32 bytes
/// of OS randomness). `None` only if the OS RNG itself is unavailable, which
/// the caller treats as "no bridge" rather than panicking.
fn mint_correlation_token() -> Option<String> {
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).ok()?;
    Some(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

/// `POST /api/managed-sessions/register`. Maps every outcome onto
/// `BackendStatus`: connection refused (no backend listening) ->
/// `NotRunning`; HTTP 409 `hud_disabled` -> `HudDisabled`; HTTP 200 ->
/// `Ready` (with the minted `session_handle`, unused before Task 3.3's
/// delivery path). Any other outcome (unexpected status, malformed body)
/// also degrades to `NotRunning`: scope decision 9 means doubt here must
/// never block Claude, only skip the voice-reply bridge.
fn register(
    agent: Agent,
    base_url: &str,
    capability_token: &str,
    correlation_token: &str,
    pid: u32,
    cwd: Option<&str>,
    timeout: Duration,
) -> (Option<String>, BackendStatus) {
    let mut body = json!({ "token": correlation_token, "pid": pid });
    if agent == Agent::Codex {
        body["agent"] = json!(agent.slug());
    }
    if let Some(cwd) = cwd {
        body["cwd"] = json!(cwd);
    }
    let Ok(bytes) = serde_json::to_vec(&body) else {
        return (None, BackendStatus::NotRunning);
    };

    let path = match agent {
        Agent::Claude => "/api/managed-sessions/register",
        Agent::Codex => "/api/managed-sessions/v2/register",
    };
    let url = format!("{base_url}{path}");
    let result = ureq::post(&url)
        .timeout(timeout)
        .set("Content-Type", "application/json")
        .set(CAPABILITY_TOKEN_HEADER, capability_token)
        .send_bytes(&bytes);

    match result {
        Ok(response) if response.status() == 200 => {
            let parsed = response
                .into_string()
                .ok()
                .and_then(|body| serde_json::from_str::<serde_json::Value>(&body).ok());
            let provider_confirmed = agent == Agent::Claude
                || parsed.as_ref().is_some_and(|value| {
                    value.get("protocol_version").and_then(|item| item.as_u64()) == Some(2)
                        && value.get("agent").and_then(|item| item.as_str()) == Some(agent.slug())
                });
            if !provider_confirmed {
                return (None, BackendStatus::ProtocolMismatch);
            }
            let session_handle = parsed.and_then(|value| {
                value
                    .get("session_handle")
                    .and_then(|handle| handle.as_str().map(str::to_owned))
            });
            (session_handle, BackendStatus::Ready)
        }
        Ok(_) => (None, BackendStatus::NotRunning),
        Err(ureq::Error::Status(409, _)) => (None, BackendStatus::HudDisabled),
        Err(ureq::Error::Status(404, _)) if agent == Agent::Codex => {
            (None, BackendStatus::ProtocolMismatch)
        }
        Err(_) => (None, BackendStatus::NotRunning),
    }
}

/// Wraps `payload` in bracketed paste and appends the submit byte.
///
/// The payload is written verbatim: no escaping, no quoting, no prefixed
/// space. Task 2.7's guard already refused every payload that would need
/// mutating (hazardous leading characters, an embedded `ESC[201~` terminator,
/// stray C0 controls), and scope decision 6 says block rather than guess - a
/// dictated reply that arrives altered is worse than one that does not arrive.
fn frame_bracketed_paste(payload: &str) -> Vec<u8> {
    let bytes = payload.as_bytes();
    let mut framed = Vec::with_capacity(
        BRACKETED_PASTE_START.len() + bytes.len() + BRACKETED_PASTE_END.len() + 1,
    );
    framed.extend_from_slice(BRACKETED_PASTE_START);
    framed.extend_from_slice(bytes);
    framed.extend_from_slice(BRACKETED_PASTE_END);
    framed.push(SUBMIT_BYTE);
    framed
}

/// One channel item is one uninterrupted writer batch. Codex receives a raw
/// Windows paste burst and Enter as distinct PTY writes with a processing
/// boundary, without allowing concurrent stdin bytes to interleave between
/// them. Claude retains its proven bracketed-paste framing unchanged.
fn reply_write_batch(agent: Agent, payload: &str) -> PtyWriteBatch {
    if agent == Agent::Claude {
        return PtyWriteBatch::immediate(frame_bracketed_paste(payload));
    }
    let normalized = payload.replace("\r\n", "\n").replace('\r', "\n");
    PtyWriteBatch {
        frames: vec![normalized.into_bytes(), vec![SUBMIT_BYTE]],
        inter_frame_delay: CODEX_RAW_PASTE_SETTLE_DELAY,
    }
}

#[derive(Deserialize, Default)]
struct PendingResponse {
    #[serde(default)]
    reply: Option<String>,
    #[serde(default)]
    shutdown_requested: bool,
}

#[derive(Debug, PartialEq, Eq)]
struct PendingOutcome {
    reply: Option<String>,
    shutdown_requested: bool,
}

/// `GET /api/managed-sessions/{handle}/pending`. Blocks up to the backend's
/// own hold, then returns the pending reply and graceful-shutdown state.
///
/// Every failure mode - a refused connection, a timeout, a malformed body, a
/// handle the backend has forgotten - collapses to `Err(())`, which the caller
/// treats as "nothing to deliver, back off and try again". Fail-closed is the
/// only correct default for a delivery channel: not delivering is a
/// disappointment, delivering the wrong thing corrupts the user's terminal.
fn poll_pending(
    base_url: &str,
    capability_token: &str,
    session_handle: &str,
    timeout: Duration,
) -> Result<PendingOutcome, ()> {
    let url = format!("{base_url}/api/managed-sessions/{session_handle}/pending");
    let response = ureq::get(&url)
        .timeout(timeout)
        .set(CAPABILITY_TOKEN_HEADER, capability_token)
        .call()
        .map_err(|_| ())?;
    let text = response.into_string().map_err(|_| ())?;
    let parsed: PendingResponse = serde_json::from_str(&text).map_err(|_| ())?;
    Ok(PendingOutcome {
        reply: parsed.reply.filter(|reply| !reply.is_empty()),
        shutdown_requested: parsed.shutdown_requested,
    })
}

/// `POST /api/managed-sessions/{handle}/delivered`. Reports a completed write
/// so the backend immediately marks the session active. Best-effort: Claude's
/// own `UserPromptSubmit` hook provides the same state transition if this
/// report is lost.
fn report_delivered(
    base_url: &str,
    capability_token: &str,
    session_handle: &str,
    timeout: Duration,
) -> Result<(), ()> {
    let url = format!("{base_url}/api/managed-sessions/{session_handle}/delivered");
    ureq::post(&url)
        .timeout(timeout)
        .set("Content-Type", "application/json")
        .set(CAPABILITY_TOKEN_HEADER, capability_token)
        .send_bytes(b"{}")
        .map(|_| ())
        .map_err(|_| ())
}

/// `POST /api/managed-sessions/deregister`. Best-effort, matching Task 3.4's
/// "request, never a kill" framing for the app-side session record: this
/// simply asks the store to drop the record and never signals the process.
fn deregister(
    base_url: &str,
    capability_token: &str,
    correlation_token: &str,
    timeout: Duration,
) -> Result<(), ()> {
    let body = json!({ "token": correlation_token });
    let bytes = serde_json::to_vec(&body).map_err(|_| ())?;
    let url = format!("{base_url}/api/managed-sessions/deregister");
    ureq::post(&url)
        .timeout(timeout)
        .set("Content-Type", "application/json")
        .set(CAPABILITY_TOKEN_HEADER, capability_token)
        .send_bytes(&bytes)
        .map(|_| ())
        .map_err(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::io::{Read as _, Write as _};
    use std::net::TcpListener;

    #[test]
    fn resolve_data_dir_prefers_whisperstream_data_dir() {
        let dir = resolve_data_dir(
            Some("D:\\custom".into()),
            Some("C:\\Users\\x\\AppData\\Local".into()),
        );
        assert_eq!(dir, Some(PathBuf::from("D:\\custom")));
    }

    #[test]
    fn resolve_data_dir_falls_back_to_local_app_data() {
        let dir = resolve_data_dir(None, Some("C:\\Users\\x\\AppData\\Local".into()));
        assert_eq!(
            dir,
            Some(PathBuf::from("C:\\Users\\x\\AppData\\Local").join("Whisperstream"))
        );
    }

    #[test]
    fn resolve_data_dir_ignores_an_empty_override() {
        let dir = resolve_data_dir(Some("".into()), Some("C:\\Users\\x\\AppData\\Local".into()));
        assert_eq!(
            dir,
            Some(PathBuf::from("C:\\Users\\x\\AppData\\Local").join("Whisperstream"))
        );
    }

    #[test]
    fn resolve_data_dir_none_when_nothing_is_set() {
        assert_eq!(resolve_data_dir(None, None), None);
    }

    fn tempdir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("whisperstream-cli-bridge-test-{name}"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn discover_port_defaults_when_the_file_is_absent() {
        let dir = tempdir("port-absent");
        assert_eq!(discover_port(Agent::Claude, &dir), DEFAULT_PORT);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn discover_port_reads_a_numeric_port() {
        let dir = tempdir("port-numeric");
        fs::write(
            dir.join("claude-hud-port.json"),
            r#"{"port": 40123, "pid": 1}"#,
        )
        .unwrap();
        assert_eq!(discover_port(Agent::Claude, &dir), 40123);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn discover_port_reads_a_string_port() {
        let dir = tempdir("port-string");
        fs::write(dir.join("claude-hud-port.json"), r#"{"port": "40124"}"#).unwrap();
        assert_eq!(discover_port(Agent::Claude, &dir), 40124);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn codex_discovers_its_provider_specific_port_file() {
        let dir = tempdir("codex-port");
        fs::write(dir.join("claude-hud-port.json"), r#"{"port":40123}"#).unwrap();
        fs::write(dir.join("codex-hud-port.json"), r#"{"port":40124}"#).unwrap();
        assert_eq!(discover_port(Agent::Codex, &dir), 40124);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn discover_port_defaults_on_malformed_json() {
        let dir = tempdir("port-malformed");
        fs::write(dir.join("claude-hud-port.json"), "not json").unwrap();
        assert_eq!(discover_port(Agent::Claude, &dir), DEFAULT_PORT);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn read_capability_token_is_none_when_the_file_is_absent() {
        let dir = tempdir("token-absent");
        assert_eq!(read_capability_token(&dir), None);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn read_capability_token_reads_the_minted_value() {
        let dir = tempdir("token-present");
        fs::write(
            dir.join(CAPABILITY_TOKEN_FILENAME),
            r#"{"token": "abc123"}"#,
        )
        .unwrap();
        assert_eq!(read_capability_token(&dir), Some("abc123".to_string()));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn read_capability_token_is_none_for_an_empty_token() {
        let dir = tempdir("token-empty");
        fs::write(dir.join(CAPABILITY_TOKEN_FILENAME), r#"{"token": ""}"#).unwrap();
        assert_eq!(read_capability_token(&dir), None);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn mint_correlation_token_is_64_hex_chars_and_varies() {
        let a = mint_correlation_token().unwrap();
        let b = mint_correlation_token().unwrap();
        assert_eq!(a.len(), 64);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    /// Serves exactly one raw HTTP response to the first connection it
    /// accepts, then stops. Good enough to exercise the three outcomes
    /// `register` maps onto `BackendStatus` without depending on a real
    /// Whisperstream backend.
    fn spawn_stub_server(raw_response: &'static str) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                // Drain the request before responding. Writing (and
                // implicitly closing) the stream while request bytes the
                // client already sent are still unread in the kernel's
                // receive buffer can make the OS send a hard RST instead of
                // a graceful close, which the client sees as a connection
                // error rather than the response -- a flaky race, not a
                // real failure. Read until the client goes quiet (a short
                // read timeout with nothing arriving) rather than assuming
                // one `read` call captures the whole request.
                stream
                    .set_read_timeout(Some(Duration::from_millis(200)))
                    .ok();
                let mut buf = [0u8; 4096];
                loop {
                    match stream.read(&mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => continue,
                    }
                }
                let _ = stream.write_all(raw_response.as_bytes());
            }
        });
        format!("http://{addr}")
    }

    #[test]
    fn register_maps_200_to_ready_and_returns_the_session_handle() {
        let base = spawn_stub_server(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 24\r\nConnection: close\r\n\r\n{\"session_handle\":\"abc\"}",
        );
        let (handle, status) = register(
            Agent::Claude,
            &base,
            "cap",
            "corr",
            123,
            Some("C:\\proj"),
            REQUEST_TIMEOUT,
        );
        assert_eq!(status, BackendStatus::Ready);
        assert_eq!(handle, Some("abc".to_string()));
    }

    #[test]
    fn register_maps_409_to_hud_disabled() {
        let base = spawn_stub_server(
            "HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\nContent-Length: 21\r\nConnection: close\r\n\r\n{\"error\":\"hud_disab\"}",
        );
        let (handle, status) = register(
            Agent::Claude,
            &base,
            "cap",
            "corr",
            123,
            None,
            REQUEST_TIMEOUT,
        );
        assert_eq!(status, BackendStatus::HudDisabled);
        assert_eq!(handle, None);
    }

    #[test]
    fn codex_register_requires_an_explicit_v2_provider_echo() {
        let confirmed = spawn_stub_server(
            "HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{\"protocol_version\":2,\"agent\":\"codex\",\"session_handle\":\"abc\"}",
        );
        let (handle, status) = register(
            Agent::Codex,
            &confirmed,
            "cap",
            "corr",
            123,
            None,
            REQUEST_TIMEOUT,
        );
        assert_eq!(status, BackendStatus::Ready);
        assert_eq!(handle.as_deref(), Some("abc"));

        let ambiguous = spawn_stub_server(
            "HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{\"session_handle\":\"ghost\"}",
        );
        let (handle, status) = register(
            Agent::Codex,
            &ambiguous,
            "cap",
            "corr",
            123,
            None,
            REQUEST_TIMEOUT,
        );
        assert_eq!(status, BackendStatus::ProtocolMismatch);
        assert_eq!(handle, None);
    }

    #[test]
    fn codex_register_maps_a_missing_v2_endpoint_to_protocol_mismatch() {
        let base = spawn_stub_server(
            "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        );
        let (handle, status) = register(
            Agent::Codex,
            &base,
            "cap",
            "corr",
            123,
            None,
            REQUEST_TIMEOUT,
        );
        assert_eq!(status, BackendStatus::ProtocolMismatch);
        assert_eq!(handle, None);
    }

    #[test]
    fn register_maps_connection_refused_to_not_running() {
        // Bind then immediately drop, freeing the port with nothing
        // listening on it: the next connect attempt is refused.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let base = format!("http://{addr}");
        let (handle, status) = register(
            Agent::Claude,
            &base,
            "cap",
            "corr",
            123,
            None,
            REQUEST_TIMEOUT,
        );
        assert_eq!(status, BackendStatus::NotRunning);
        assert_eq!(handle, None);
    }

    #[test]
    fn register_maps_an_unexpected_status_to_not_running() {
        let base = spawn_stub_server(
            "HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        );
        let (handle, status) = register(
            Agent::Claude,
            &base,
            "cap",
            "corr",
            123,
            None,
            REQUEST_TIMEOUT,
        );
        assert_eq!(status, BackendStatus::NotRunning);
        assert_eq!(handle, None);
    }

    #[test]
    fn bridge_can_register_after_starting_before_the_app() {
        let dir = tempdir("late-registration");
        let bridge = Bridge {
            agent: Agent::Claude,
            data_dir: dir.clone(),
            correlation_token: "corr".to_string(),
            pid: 123,
            cwd: Some("C:\\proj".to_string()),
            connection: Mutex::new(None),
            registration: Mutex::new(()),
            shutdown_requested: AtomicBool::new(false),
        };
        assert_eq!(bridge.try_register(), BackendStatus::NotRunning);
        assert!(bridge.current_connection().is_none());

        let base = spawn_stub_server(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 32\r\nConnection: close\r\n\r\n{\"session_handle\":\"late-handle\"}",
        );
        let port = base.rsplit(':').next().unwrap();
        fs::write(
            dir.join(CAPABILITY_TOKEN_FILENAME),
            r#"{"token":"late-capability"}"#,
        )
        .unwrap();
        fs::write(
            dir.join("claude-hud-port.json"),
            format!(r#"{{"port":{port}}}"#),
        )
        .unwrap();

        assert_eq!(bridge.try_register(), BackendStatus::Ready);
        let connection = bridge.current_connection().unwrap();
        assert_eq!(connection.capability_token, "late-capability");
        assert_eq!(connection.session_handle, "late-handle");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn registration_monitor_reconnects_when_the_app_rotates_its_token() {
        let dir = tempdir("registration-monitor-restart");
        let bridge = Arc::new(Bridge {
            agent: Agent::Claude,
            data_dir: dir.clone(),
            correlation_token: "corr".to_string(),
            pid: 123,
            cwd: Some("C:\\proj".to_string()),
            connection: Mutex::new(Some(BridgeConnection {
                base_url: "http://127.0.0.1:1".to_string(),
                capability_token: "old-capability".to_string(),
                session_handle: "old-handle".to_string(),
            })),
            registration: Mutex::new(()),
            shutdown_requested: AtomicBool::new(false),
        });

        let base = spawn_stub_server(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 35\r\nConnection: close\r\n\r\n{\"session_handle\":\"restart-handle\"}",
        );
        let port = base.rsplit(':').next().unwrap();
        fs::write(
            dir.join(CAPABILITY_TOKEN_FILENAME),
            r#"{"token":"new-capability"}"#,
        )
        .unwrap();
        fs::write(
            dir.join("claude-hud-port.json"),
            format!(r#"{{"port":{port}}}"#),
        )
        .unwrap();

        let running = Arc::new(AtomicBool::new(true));
        let monitor = bridge.spawn_registration_monitor(Arc::clone(&running));
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        while std::time::Instant::now() < deadline {
            if bridge.current_connection().is_some_and(|connection| {
                connection.capability_token == "new-capability"
                    && connection.session_handle == "restart-handle"
            }) {
                break;
            }
            thread::sleep(Duration::from_millis(20));
        }
        running.store(false, Ordering::SeqCst);
        monitor.join().unwrap();

        let connection = bridge.current_connection().unwrap();
        assert_eq!(connection.capability_token, "new-capability");
        assert_eq!(connection.session_handle, "restart-handle");
        let _ = fs::remove_dir_all(&dir);
    }

    // -- Task 3.3: bracketed-paste framing ----------------------------------

    #[test]
    fn frame_bracketed_paste_wraps_the_payload_and_appends_the_submit_byte() {
        assert_eq!(
            frame_bracketed_paste("hello"),
            b"\x1b[200~hello\x1b[201~\r".to_vec()
        );
    }

    #[test]
    fn frame_bracketed_paste_keeps_a_multiline_payload_inside_one_paste() {
        // The whole point of bracketed paste: the embedded newline must stay
        // inside the paste so it is one message, not two submits.
        let framed = frame_bracketed_paste("line one\nline two");
        assert_eq!(framed, b"\x1b[200~line one\nline two\x1b[201~\r".to_vec());
        assert_eq!(framed.iter().filter(|b| **b == SUBMIT_BYTE).count(), 1);
    }

    #[test]
    fn frame_bracketed_paste_never_mutates_the_payload() {
        // No escaping, no quoting, no prefixed space -- Task 2.7's guard
        // already refused everything that would need mutating.
        let payload = "reply with \"quotes\", a $var and 50% of it";
        let framed = frame_bracketed_paste(payload);
        let start = BRACKETED_PASTE_START.len();
        let end = framed.len() - BRACKETED_PASTE_END.len() - 1;
        assert_eq!(&framed[start..end], payload.as_bytes());
    }

    #[test]
    fn frame_bracketed_paste_puts_the_submit_byte_outside_the_paste() {
        let framed = frame_bracketed_paste("hi");
        assert_eq!(*framed.last().unwrap(), SUBMIT_BYTE);
        assert!(framed[..framed.len() - 1].ends_with(BRACKETED_PASTE_END));
    }

    #[test]
    fn codex_normalizes_newlines_and_submits_in_a_second_write() {
        assert_eq!(
            reply_write_batch(Agent::Codex, "one\r\ntwo\rthree\nfour"),
            PtyWriteBatch {
                frames: vec![b"one\ntwo\nthree\nfour".to_vec(), b"\r".to_vec(),],
                inter_frame_delay: Duration::from_millis(250),
            }
        );
    }

    #[test]
    fn claude_keeps_the_proven_single_write_frame() {
        assert_eq!(
            reply_write_batch(Agent::Claude, "one\r\ntwo"),
            PtyWriteBatch {
                frames: vec![b"\x1b[200~one\r\ntwo\x1b[201~\r".to_vec()],
                inter_frame_delay: Duration::ZERO,
            }
        );
    }

    #[test]
    fn poll_pending_parses_a_waiting_reply() {
        let base = spawn_stub_server(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 18\r\nConnection: close\r\n\r\n{\"reply\":\"hello\"}\n",
        );
        assert_eq!(
            poll_pending(&base, "cap", "handle-1", REQUEST_TIMEOUT),
            Ok(PendingOutcome {
                reply: Some("hello".to_string()),
                shutdown_requested: false,
            })
        );
    }

    #[test]
    fn poll_pending_maps_an_empty_hold_to_no_reply() {
        let base = spawn_stub_server(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 14\r\nConnection: close\r\n\r\n{\"reply\":null}",
        );
        assert_eq!(
            poll_pending(&base, "cap", "handle-1", REQUEST_TIMEOUT),
            Ok(PendingOutcome {
                reply: None,
                shutdown_requested: false,
            })
        );
    }

    #[test]
    fn poll_pending_treats_an_empty_string_reply_as_no_reply() {
        let base = spawn_stub_server(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 12\r\nConnection: close\r\n\r\n{\"reply\":\"\"}",
        );
        assert_eq!(
            poll_pending(&base, "cap", "handle-1", REQUEST_TIMEOUT),
            Ok(PendingOutcome {
                reply: None,
                shutdown_requested: false,
            })
        );
    }

    #[test]
    fn poll_pending_parses_a_shutdown_request() {
        let base = spawn_stub_server(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 40\r\nConnection: close\r\n\r\n{\"reply\":null,\"shutdown_requested\":true}",
        );
        assert_eq!(
            poll_pending(&base, "cap", "handle-1", REQUEST_TIMEOUT),
            Ok(PendingOutcome {
                reply: None,
                shutdown_requested: true,
            })
        );
    }

    #[test]
    fn poll_pending_fails_closed_on_a_refused_connection() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let base = format!("http://{addr}");
        assert!(poll_pending(&base, "cap", "handle-1", REQUEST_TIMEOUT).is_err());
    }

    #[test]
    fn poll_pending_fails_closed_on_a_malformed_body() {
        let base = spawn_stub_server(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 8\r\nConnection: close\r\n\r\nnot json",
        );
        assert!(poll_pending(&base, "cap", "handle-1", REQUEST_TIMEOUT).is_err());
    }

    #[test]
    fn report_delivered_swallows_a_connection_refused_error() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let base = format!("http://{addr}");
        assert!(report_delivered(&base, "cap", "handle-1", REQUEST_TIMEOUT).is_err());
    }

    #[test]
    fn long_poll_timeout_exceeds_the_backends_own_hold() {
        // `managed_sessions.MAX_PENDING_LONG_POLL_SECONDS` is 30s. A client
        // timeout at or under that would surface every ordinary empty poll as
        // a connection error and trigger the error backoff.
        assert!(LONG_POLL_TIMEOUT > Duration::from_secs(30));
    }

    #[test]
    fn immediate_empty_poll_is_rate_limited_but_a_real_long_poll_is_not() {
        assert_eq!(
            empty_poll_backoff(Duration::from_millis(5)),
            Duration::from_millis(995)
        );
        assert_eq!(empty_poll_backoff(Duration::from_secs(25)), Duration::ZERO);
    }

    #[test]
    fn deregister_swallows_a_connection_refused_error() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let base = format!("http://{addr}");
        assert!(deregister(&base, "cap", "corr", REQUEST_TIMEOUT).is_err());
    }

    #[test]
    fn bridge_start_is_not_running_when_the_data_dir_cannot_be_resolved() {
        // With neither env var resolvable there is nothing to discover, so
        // `Bridge::start` must degrade gracefully rather than panic. Exercised
        // directly through `resolve_data_dir`, which `start` calls first.
        assert_eq!(resolve_data_dir(None, None), None);
    }

    #[test]
    fn deregister_active_best_effort_is_a_no_op_with_no_active_bridge() {
        // Whatever earlier tests in this process may have left behind, this
        // must never panic.
        deregister_active_best_effort();
    }
}
