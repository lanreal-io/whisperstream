//! Argv contract for the `whisperstream` managed coding-agent wrapper.

use std::ffi::OsString;

pub const USAGE: &str = "\
Usage: whisperstream <claude|codex> [agent arguments]

Starts Claude Code or Codex so Whisperstream can send voice replies
after a turn ends.
All arguments are passed to the selected coding agent unchanged.

Examples:
  whisperstream claude --model opus
  whisperstream codex --model gpt-5.6
  whisperstream codex resume --last
";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Agent {
    Claude,
    Codex,
}

impl Agent {
    pub fn slug(self) -> &'static str {
        match self {
            Self::Claude => "claude",
            Self::Codex => "codex",
        }
    }

    pub fn display_name(self) -> &'static str {
        match self {
            Self::Claude => "Claude",
            Self::Codex => "Codex",
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Command {
    Agent {
        agent: Agent,
        forwarded: Vec<OsString>,
    },
    Help,
    UsageError,
}

pub fn parse<I>(args: I) -> Command
where
    I: IntoIterator<Item = OsString>,
{
    let mut iter = args.into_iter();
    match iter.next() {
        None => Command::UsageError,
        Some(first) if first == "--help" || first == "-h" => Command::Help,
        Some(first) if first == "claude" => Command::Agent {
            agent: Agent::Claude,
            forwarded: iter.collect(),
        },
        Some(first) if first == "codex" => Command::Agent {
            agent: Agent::Codex,
            forwarded: iter.collect(),
        },
        Some(_) => Command::UsageError,
    }
}

pub fn is_remote_codex(agent: Agent, forwarded: &[OsString]) -> bool {
    agent == Agent::Codex && forwarded.iter().any(|argument| argument == "--remote")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn osv(items: &[&str]) -> Vec<OsString> {
        items.iter().map(OsString::from).collect()
    }

    #[test]
    fn both_agents_forward_argv_verbatim_and_in_order() {
        for (slug, agent) in [("claude", Agent::Claude), ("codex", Agent::Codex)] {
            let forwarded = osv(&["--model", "value with spaces", "50% & exact"]);
            assert_eq!(
                parse(std::iter::once(OsString::from(slug)).chain(forwarded.clone())),
                Command::Agent { agent, forwarded }
            );
        }
    }

    #[test]
    fn either_agent_with_no_further_args_forwards_an_empty_vec() {
        assert_eq!(
            parse(osv(&["codex"])),
            Command::Agent {
                agent: Agent::Codex,
                forwarded: Vec::new(),
            }
        );
    }

    #[test]
    fn remote_detection_applies_only_to_codex() {
        assert!(is_remote_codex(Agent::Codex, &osv(&["--remote"])));
        assert!(is_remote_codex(Agent::Codex, &osv(&["resume", "--remote"])));
        assert!(!is_remote_codex(Agent::Claude, &osv(&["--remote"])));
    }

    #[test]
    fn bare_invocation_is_a_usage_error() {
        assert_eq!(parse(osv(&[])), Command::UsageError);
    }

    #[test]
    fn help_flags_are_recognized() {
        assert_eq!(parse(osv(&["--help"])), Command::Help);
        assert_eq!(parse(osv(&["-h"])), Command::Help);
    }

    #[test]
    fn unrecognized_first_token_is_a_usage_error() {
        assert_eq!(parse(osv(&["frobnicate"])), Command::UsageError);
    }

    #[test]
    fn usage_text_matches_locked_copy_byte_for_byte() {
        let expected = "Usage: whisperstream <claude|codex> [agent arguments]

Starts Claude Code or Codex so Whisperstream can send voice replies
after a turn ends.
All arguments are passed to the selected coding agent unchanged.

Examples:
  whisperstream claude --model opus
  whisperstream codex --model gpt-5.6
  whisperstream codex resume --last
";
        assert_eq!(USAGE, expected);
    }
}
