# Agent Speak for Codex and Claude Code

Whisperstream 2.0.0.

## Connect Agent Speak

Agent Speak shows Codex and Claude Code replies in Whisperstream's HUD. You can read a short written summary, hear it through local speech, and dictate a reply after an agent finishes.

Agent Speak connects to coding agents on your computer. While Lockdown Mode is effective, Whisperstream suspends Agent Speak processing. This does not stop a connected agent from using its configured model service independently.

Install Codex or Claude Code and open it at least once. Then:

1. Open **Agents** in Whisperstream and select **Enable Agents** or **Continue setup**.
2. Under **Connect your coding agents**, select **Connect** for Codex, Claude Code, or both.
3. For Codex CLI, run `/hooks` and trust Whisperstream. In the Codex app, open **Settings > Hooks > Whisperstream** and select **Trust all**. For Claude Code, run `/reload-plugins` if Whisperstream says its plugin was installed or updated.
4. Choose **Show summaries**, **Read replies aloud**, or both. Setup starts with both selected and requires at least one. If you choose speech, select a local voice model and voice, then adjust **Speed**. Continue starts the model download if needed.
5. Set the shortcut that opens the full response, then follow the final on-screen test with your connected agent.

The test checks your selected response mode and voice replies. Claude Code also presents a practice structured question. Codex does not provide structured questions to Agent Speak.

After you select **Connect**, Agent Speak stays on while Codex hook trust is pending. The Codex row changes to **Connected** automatically after trust is confirmed. After setup, use **General**, **Voice**, and **Connections** in Agents to change settings. To repair a connection, open **Connections**, select **Disconnect**, then **Connect** again. Follow any status instruction shown below the agent.

## Pause Agent Speak

To stop Agent Speak for a while without disconnecting anything, open **Agents > General** and turn off the **Agents** switch. Summaries, speech, and replies stop for every coding agent, and **Connections** shows each agent as **Paused**. Your connections, Codex hook trust, and voice and summary settings are kept. Turn the switch back on to resume with the same agents.

Pausing does not stop Codex or Claude Code themselves. To remove one agent's connection instead, use **Disconnect** in **Connections**.

## View responses and dictate replies

Turn on **Show summaries** to display the agent's short written summary when it finishes. This is independent of **Voice**, so you can read summaries without hearing them. New installations start with both settings off; completing setup saves your selections.

Use **View full response** to expand the complete reply, even with automatic summaries off. The default shortcut is **Ctrl+Alt+backtick**, and you can replace or clear it in Agent Speak settings. Existing saved shortcuts are not changed automatically.

**Reply window** controls how long your normal dictation shortcut answers the response that just finished. Choose 1 to 30 seconds, or **Off** to disable temporary replies. Setup selects 20 seconds with written summaries or 6 seconds with speech alone. In General, enabling summaries changes a 6-second window to 20 seconds; disabling them changes 20 back to 6. Other values stay unchanged.

During that window, start dictation with your usual Push to Talk or Toggle shortcut. Your draft appears in the HUD:

- With **Send replies automatically** off, review the draft and press Enter to send it. This is the default.
- With **Send replies automatically** on, Whisperstream sends it when dictation finishes.

Claude Code can also send structured questions to the HUD. Expand the question, then press the displayed number for your answer. Codex structured questions remain in Codex. Press Escape to close the response or defer a Claude Code question without answering it.

For replies after this temporary window closes, see [Coding agent sessions](coding-agent-sessions.md).

## Configure local speech

In **Agents > Voice**, turn on **Voice** to hear replies through local speech. Use **What to read** to choose **Spoken summary**, **Full response when short**, or **Full response**. Code blocks and raw links are skipped.

Choose a **Voice model**:

| Choice | Behavior |
|---|---|
| **Automatic** | Uses Higher quality when 2 GB of memory is free, otherwise Lower memory. Downloads both models. |
| **Higher quality** | Uses Kokoro, which uses up to 1.5 GB of memory while speaking. |
| **Lower memory** | Uses Supertonic, which starts faster and uses up to 400 MB of memory while speaking. |

Lower memory is the default on Windows, and Automatic is the default on Mac. Choosing Automatic or Higher quality downloads Kokoro from Hugging Face; it has no automatic backup source. Each voice model keeps its own voice choice.

Change **Voice model**, **Voice**, **Speed**, and **Volume** here. Voice previews are available in the picker. For the playback destination, see [Audio](audio.md).

A voice model stays loaded between replies and unloads after 30 minutes without speech to free memory. To change this, expand **Advanced** in **Agents > Voice** and set **Unload voice model when idle** to **Never**, **5 min**, **10 min**, **15 min**, **30 min**, or **1 hour**. After the model unloads, the next spoken reply may take a moment to start.

Use **Speak in** to choose when speech is requested:

| Choice | Behavior |
|---|---|
| **Every session** | The default once Voice is enabled. Requests speech from every connected session. |
| **When I ask** | Use `/whisperstream:speak` in a Claude Code session or `$speak` in a Codex session. |

In Claude Code, `/whisperstream:speak Project name` also names the session. Use `/whisperstream:name Project name` to name or rename it, or `/whisperstream:name` to clear that name. Named sessions show their names in the HUD, and their spoken summaries begin with the name.
