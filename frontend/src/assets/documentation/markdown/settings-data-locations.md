# Settings and data locations

Whisperstream 2.0.0.

## Find a setting

Whisperstream groups settings by feature. Use this map when you know what you want to change but not where to find it.

| Page | Settings and actions |
|---|---|
| **Home** | Language, speech model, theme, startup, model storage location, and Mac permissions. |
| **Audio** | Input device, recording chimes, chime volume, and Audio While Recording. |
| **Controls** | Recording shortcuts, last-transcript shortcuts, insertion method, and Live Preview. |
| **Dictionary** | Boost words, filtered words, word replacements, and CSV import. |
| **Enhancement** | Provider, model, API key, profile, and app or site routing. |
| **Transcripts** | History saving, audio saving, retention, search, and deletion. |
| **Agents** | Agent Speak, voices, and Whisperstream CLI setup for coding agent sessions. |
| **About** | Version, updates, support, offline documentation, legal documents, and log export. |

Changes save as you make them. A restart is usually unnecessary unless Whisperstream asks for one.

For the CLI installation and session workflow, see [Coding agent sessions](coding-agent-sessions.md).

## Find Whisperstream data

Whisperstream keeps its user data under the signed-in Windows account. Enter this path in File Explorer's address bar to open the default folder:

`%LOCALAPPDATA%\Whisperstream`

| Item | Location |
|---|---|
| Settings, usage data, and transcript history | `%LOCALAPPDATA%\Whisperstream` |
| Saved recording audio | `%LOCALAPPDATA%\Whisperstream\recordings` |
| Diagnostic logs | `%LOCALAPPDATA%\Whisperstream\logs` |
| Downloaded speech and voice models | `<model storage>\models` |
| Downloaded Local enhancement models | `<model storage>\local-llm` |

The model storage folder defaults to `%LOCALAPPDATA%\Whisperstream`. If you move it, only the two model folders use the new location. Settings, history, recordings, and logs stay in the default data folder.

### On a Mac

Whisperstream keeps its data under your macOS user account. In Finder, choose **Go > Go to Folder** and enter the path.

| Item | Location |
|---|---|
| Settings, usage data, and transcript history | `~/Library/Application Support/Whisperstream` |
| Saved recording audio | `~/Library/Application Support/Whisperstream/recordings` |
| Diagnostic logs | `~/Library/Application Support/Whisperstream/logs` |
| Downloaded Parakeet v3 model | `~/Library/Application Support/FluidAudio/Models` |
| Other speech and voice models | `~/Library/Application Support/Whisperstream/models` |
| Downloaded Local enhancement models | `~/Library/Application Support/Whisperstream/local-llm` |

The transcript encryption key is stored in Keychain.

## Move downloaded models

Move managed models when you want to free space on the drive that contains your Windows app data.

1. Open **Home** and expand **Advanced**.
2. Under **Model storage location**, select **Browse**.
3. Choose a stable local folder and review the folder check.
4. Select **Move files**. If compatible Whisperstream models are already there, select **Use this folder** instead.
5. Keep Whisperstream open until you see **Models moved**, then select **Done**.

Dictation and Local enhancement pause during the move and resume when it finishes. If the move fails, the original files remain in place. Select **Reset to default** to move managed models back to the standard location.

On a Mac, **Model storage location** moves only the Parakeet v3 model. Other speech, voice, and Local enhancement models stay in Whisperstream's data folder.

## Export logs

Open **About** and select **Export logs**, then choose where to save `whisperstream-logs.zip`. You can inspect the ZIP locally or share it with someone helping you investigate. Export it soon after a problem occurs so it contains the most useful recent information.

Log export remains available in voluntary and administrator-managed Lockdown.
Whisperstream never uploads the ZIP automatically. See [Lockdown Mode](lockdown-mode.md) for the controls that change in that mode.

## Choose what survives uninstall

The Windows uninstaller includes the checkbox **Also delete settings and downloaded files**.

- Leave it cleared to keep `%LOCALAPPDATA%\Whisperstream`. Your settings, transcript history, saved audio, logs, and models in the default location will be available after reinstalling under the same Windows account.
- Select it to remove that default data folder along with the app. If your custom model storage folder is outside the default folder, it remains, so delete that custom folder separately if you no longer need it.

On a Mac, moving Whisperstream to the Trash leaves its data in place. To remove it, quit Whisperstream and delete `~/Library/Application Support/Whisperstream`. Delete the Parakeet v3 model from `~/Library/Application Support/FluidAudio/Models`, or from the folder you moved it to, if you no longer need it.
