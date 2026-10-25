## Install the Whisperstream CLI

Start Codex or Claude Code through the optional Whisperstream CLI when you want its session to remain available in the HUD after a turn ends. This lets you return to a coding session and dictate a reply later, instead of relying on Agent Speak's temporary reply window.

First, connect the coding agent you plan to use. See [Agent Speak](/docs/workflows/agent-speak) for connection, trust, and speech setup. Then:

1. Open **Agents** in Whisperstream, then select **Connections**.
2. Find **Whisperstream CLI** and select **Install**.
3. If the status asks you to, open a new terminal before using the command.

On a Mac, Install adds the `whisperstream` command to `~/.local/bin` and adds that folder to your PATH in your zsh and bash profiles. Open a new terminal window afterward.

Use **Uninstall** in the same place to remove the Whisperstream command from your PATH. This does not uninstall Codex, Claude Code, or Whisperstream.

## Start a coding agent session

Open Windows Terminal, or a terminal window on a Mac, and run the command for your coding agent:

```powershell
whisperstream codex
whisperstream claude
```

Arguments after the agent name pass to that coding agent unchanged. For example:

```powershell
whisperstream codex resume --last
whisperstream claude --model opus
```

On Windows, voice replies are tested in Windows Terminal only. They may work in another terminal. On a Mac, use the command in a zsh or bash terminal. If Whisperstream cannot connect the session, Codex or Claude Code continues normally.

Remote Codex sessions are not available for HUD replies. Running `whisperstream codex --remote` still starts Codex normally, with voice replies off.

## Return to a session in the HUD

Use your **View full response** shortcut to open the HUD. Each active CLI-started session appears under **Coding agent sessions**, identified by its coding agent and session name.

When several sessions are open, select a tab or use these shortcuts:

- `Ctrl+1` through `Ctrl+9` selects a session by position. On a Mac, use `Cmd+1` through `Cmd+9`.
- `Ctrl+Tab` moves to the next session.
- `Ctrl+Shift+Tab` moves to the previous session.

The selected session shows its latest response and whether it can accept a reply. If it is read-only, continue in its terminal and wait until the agent finishes before trying again.

To reply, select a session that can accept input and start dictation with your usual Push to Talk or Toggle shortcut. Review the draft in the HUD, then press Enter to send it. Press Escape to cancel the draft and close the HUD without ending the coding agent session. Agent Speak's automatic-send and speech settings still apply; configure them in [Agent Speak](/docs/workflows/agent-speak).

Closing the terminal ends that coding agent session and removes it from the HUD. Closing or restarting Whisperstream does not stop the coding agent. The session reconnects when Whisperstream is available again. If the provider is disconnected, the app version is incompatible, or a connection cannot be established, the coding agent continues normally and HUD replies remain off.
