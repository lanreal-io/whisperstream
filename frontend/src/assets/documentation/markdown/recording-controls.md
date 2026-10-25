# Recording controls and last-transcript actions

Whisperstream 2.0.0.

## Choose how recording starts and stops

Whisperstream gives **Push to Talk** and **Toggle** separate global shortcuts. You can assign either one or use both, then choose the gesture that suits each dictation. The shortcuts work while Whisperstream is running, even when its main window is not focused.

| Control | How it works | New-install default |
|---|---|---|
| **Push to Talk** | Hold the shortcut to record, then release it to stop. | Right Shift |
| **Toggle** | Press once to start recording, then press again to stop. | Not set |

Push to Talk works well for short dictations and precise start and stop control. Toggle is useful for longer dictations when you do not want to hold a key.

On a Mac, shortcuts use the same keys and appear with macOS symbols. Ctrl is Control (⌃) and Alt is Option (⌥), so Ctrl+Alt+V appears as ⌃⌥V. Shortcuts need Accessibility access; see [Allow access on a Mac](install-get-started.md#allow-access-on-a-mac).

## Assign or clear a shortcut

Open **Controls**, then find **Hotkeys**.

1. Select the shortcut field beside **Push to Talk** or **Toggle**.
2. Press the key or key combination you want.
3. Release all keys to save it.

While the field is listening, press Escape to cancel without changing the saved shortcut. To clear a shortcut, select its field and use the × control that appears.

Whisperstream warns about shortcuts that may conflict with the operating system or another Whisperstream action, but it does not block them. If the system or another app responds instead, choose a different combination. When both recording shortcuts are cleared, Controls shows **No recording shortcut is set. Set Push to Talk or Toggle to start dictating.**

## Start a dictation

- With **Push to Talk**, place the text cursor in the destination, hold the shortcut, wait for the start chime to finish or the indicator to appear, speak, then release it.
- With **Toggle**, place the text cursor in the destination, press the shortcut, wait for the start chime to finish or the indicator to appear, speak, then press it again.
- With the desktop control, select **Start dictation**, speak, then select the checkmark (**Finish and type**).

After you stop recording, Whisperstream processes and inserts the final text.

## Discard a recording

- During a Push to Talk or Toggle recording, press Escape.
- During a recording started from the desktop control, select the × (**Cancel dictation**).

Discarding stops the recording without transcribing, inserting, or saving it. Escape does not control a recording started from the desktop control.

## Keep the indicator or recording control visible

The recording indicator is on for new installations. To hide or restore it, open **Controls**, find **Recording Indicator**, and turn **Show indicator** off or on. The indicator shows when Whisperstream is recording and when it is processing the result. Whisperstream hides it while a screen reader is active, even when **Show indicator** is on.

To leave a compact recording control on the desktop between dictations, turn on **Keep control visible** in the same section. This setting is off by default and requires **Show indicator**. Point to the compact control to reveal **Start dictation**, then select it.

For provisional words inside the indicator, see [Live Preview](live-preview.md).

## Reuse the most recent dictation

Assign these actions under **Controls > Hotkeys**, then place the cursor where you want the text and press the shortcut.

| Action | What it inserts | New-install default |
|---|---|---|
| **Paste Last Transcript** | The most recent final transcript, including AI enhancement when it ran. | Ctrl+Alt+V |
| **Paste Last Transcript (Original)** | The same dictation before AI enhancement, after normal Dictionary and text-processing rules. | Not set |

These actions are available only after a dictation finishes and while Whisperstream remains running. Restarting the app clears both remembered values. For a lasting record, use [Transcript and audio history](transcript-history.md).

For delivery settings and application compatibility, see [Text insertion and compatibility](text-insertion.md).
