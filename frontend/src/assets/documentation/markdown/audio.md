# Microphone, playback, and system audio

Whisperstream 2.0.0.

## Choose a microphone

Open **Audio** to choose the input device Whisperstream records and to control what you hear during a recording.

| Input choice | Behavior |
|---|---|
| **Use system default** | Follows the microphone currently selected as the system default input. This is the new-install default. |
| Named microphone | Keeps Whisperstream on that device while it is available. If it cannot be found or opened, Whisperstream uses the system default input. |

Selecting a microphone in Whisperstream does not change the system default for other apps. Choose **Use system default** if you regularly switch headsets. Choose a named microphone when you want Whisperstream to keep using one device.

To confirm the selection, make a short recording and review the completed transcript. If the result is empty or unclear, check that the intended microphone is connected and working in Windows, then try another input.

If no inputs appear, allow microphone access for desktop apps in Windows privacy settings and reconnect the device. Then select **Quit** from the Whisperstream tray menu and start Whisperstream again. On a Mac, check **Home > Permissions**, or turn on Whisperstream in **System Settings > Privacy & Security > Microphone**.

## Choose where audio plays

Recording chimes use the Windows default output that was active when chime playback started, normally when Whisperstream launched. To use another speaker or headset, change the Windows default output. If the chimes continue playing through the previous device, select **Quit** from the Whisperstream tray menu and start Whisperstream again.

Input and output are separate. Selecting a microphone in Whisperstream does not select where chimes or other Windows audio play.

Spoken Agent Speak responses from Codex and Claude Code check the current Windows default output before each response. For voice, speed, and volume controls, see [Agent Speak](agent-speak.md).

## Configure recording chimes

**Recording Chime** plays one sound when recording starts and another when it stops. It is on by default and helps confirm the recording boundary when the Whisperstream window is hidden.

Use **Chime Volume** to set the chimes from 0 to 100 percent. The starting value is 100 percent. Your Windows output volume also affects what you hear. Turn **Recording Chime** off when audible cues would be disruptive.

## Control other audio while recording

Use **Audio While Recording** to choose what happens to other Windows playback during a dictation.

| Setting | During recording |
|---|---|
| **None** | Leaves other audio unchanged. This is the new-install default. |
| **Lower** | Reduces the active Windows output to 20 percent of its previous level. |
| **Mute** | Mutes the active Windows output. |

Whisperstream restores the previous volume and mute state when recording ends. On a Mac, **Lower** and **Mute** are available only when the current output device supports volume control. Choose **Lower** when you still need to hear a call or media. Choose **Mute** when playback should be silent for the entire recording.

Test **Lower** or **Mute** with the headset and calling software you plan to use. If other audio remains quiet after recording, restore the Windows volume or mute state manually. If the problem repeats, leave **Audio While Recording** set to **None** and review [Settings and data locations](settings-data-locations.md) for local diagnostics.
