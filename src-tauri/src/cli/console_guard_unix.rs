//! Exact termios restoration for the Unix managed-session relay.
use anyhow::Result;
use std::sync::OnceLock;

static PRIOR: OnceLock<libc::termios> = OnceLock::new();

pub fn restore() {
    if let Some(prior) = PRIOR.get() {
        unsafe { libc::tcsetattr(libc::STDIN_FILENO, libc::TCSANOW, prior); }
    }
}

extern "C" fn terminate(signal: libc::c_int) {
    // Only async-signal-safe native calls; network bridge cleanup is left to
    // its liveness expiry on this exceptional path.
    restore();
    unsafe {
        libc::signal(signal, libc::SIG_DFL);
        libc::raise(signal);
    }
}

pub fn install() -> Result<()> {
    let mut prior: libc::termios = unsafe { std::mem::zeroed() };
    if unsafe { libc::tcgetattr(libc::STDIN_FILENO, &mut prior) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let prior = PRIOR.get_or_init(|| prior);
    for signal in [libc::SIGHUP, libc::SIGTERM, libc::SIGINT] {
        let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
        action.sa_sigaction = terminate as *const () as usize;
        unsafe { libc::sigemptyset(&mut action.sa_mask); }
        if unsafe { libc::sigaction(signal, &action, std::ptr::null_mut()) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    let mut raw = *prior;
    unsafe { libc::cfmakeraw(&mut raw); }
    if unsafe { libc::tcsetattr(libc::STDIN_FILENO, libc::TCSANOW, &raw) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(())
}
