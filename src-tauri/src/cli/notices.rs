//! Provider-aware, fail-open startup notices printed before the child starts.

use std::env;

use crate::args::Agent;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendStatus {
    NotRunning,
    HudDisabled,
    ProtocolMismatch,
    RemoteUnsupported,
    Ready,
}

fn not_windows_terminal_notice(agent: Agent) -> String {
    format!(
        "Voice replies are tested in Windows Terminal only.\n\
They may still work here. {} works exactly as usual.\n",
        agent.display_name()
    )
}

fn provider_off_notice(agent: Agent) -> String {
    format!(
        "{} is not connected, so voice replies are off.\n\
Connect it in Whisperstream under Experimental.\n\
{} works exactly as usual.\n",
        agent.display_name(),
        agent.display_name()
    )
}

fn protocol_mismatch_notice(agent: Agent) -> String {
    format!(
        "{} voice replies need a newer Whisperstream version.\n\
{} works exactly as usual.\n",
        agent.display_name(),
        agent.display_name()
    )
}

fn remote_notice() -> String {
    "Voice replies are off for remote Codex sessions.\n\
Codex works exactly as usual.\n"
        .to_string()
}

pub fn is_windows_terminal() -> bool {
    env::var_os("WT_SESSION").is_some()
}

fn startup_notices(agent: Agent, is_windows_terminal: bool, backend: BackendStatus) -> Vec<String> {
    let mut notices = Vec::new();
    if !is_windows_terminal {
        notices.push(not_windows_terminal_notice(agent));
    }
    match backend {
        BackendStatus::NotRunning | BackendStatus::Ready => {}
        BackendStatus::HudDisabled => notices.push(provider_off_notice(agent)),
        BackendStatus::ProtocolMismatch => notices.push(protocol_mismatch_notice(agent)),
        BackendStatus::RemoteUnsupported => notices.push(remote_notice()),
    }
    notices
}

pub fn print_startup_notices(agent: Agent, backend: BackendStatus) {
    for notice in startup_notices(agent, cfg!(unix) || is_windows_terminal(), backend) {
        eprint!("{notice}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminal_notice_is_provider_aware_and_matches_locked_copy() {
        assert_eq!(
            not_windows_terminal_notice(Agent::Codex),
            "Voice replies are tested in Windows Terminal only.\n\
They may still work here. Codex works exactly as usual.\n"
        );
    }

    #[test]
    fn provider_disabled_notice_matches_locked_copy() {
        assert_eq!(
            provider_off_notice(Agent::Claude),
            "Claude is not connected, so voice replies are off.\n\
Connect it in Whisperstream under Experimental.\n\
Claude works exactly as usual.\n"
        );
    }

    #[test]
    fn remote_and_protocol_notices_match_locked_copy() {
        assert_eq!(
            remote_notice(),
            "Voice replies are off for remote Codex sessions.\n\
Codex works exactly as usual.\n"
        );
        assert_eq!(
            protocol_mismatch_notice(Agent::Codex),
            "Codex voice replies need a newer Whisperstream version.\n\
Codex works exactly as usual.\n"
        );
    }

    #[test]
    fn absent_app_adds_no_backend_notice() {
        assert!(startup_notices(Agent::Codex, true, BackendStatus::NotRunning).is_empty());
    }

    #[test]
    fn each_non_ready_boundary_emits_one_backend_notice() {
        for backend in [
            BackendStatus::HudDisabled,
            BackendStatus::ProtocolMismatch,
            BackendStatus::RemoteUnsupported,
        ] {
            assert_eq!(startup_notices(Agent::Codex, true, backend).len(), 1);
        }
    }
}
