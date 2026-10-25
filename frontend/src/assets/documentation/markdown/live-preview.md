# Live Preview

Whisperstream 2.0.0.

## See words while you speak

Live Preview shows provisional speech recognition inside the recording indicator while you dictate. It can help you confirm that Whisperstream is following your speech and make longer recordings easier to track.

The preview is display only. It never inserts partial text into the active app, and it is not the final transcript.

## Turn on Live Preview

1. Open **Controls**.
2. Under **Recording Indicator**, turn on **Show indicator**.
3. Turn on **Live Preview**.
4. Start a recording and speak normally.

Live Preview is off by default. Its setting depends on **Show indicator**, because the preview appears inside that indicator.

## Read the preview

Words appear after Whisperstream has received enough speech to form a useful result. The preview is provisional, so unconfirmed words may be corrected, shortened, or replaced as you continue speaking.

Wait for the completed text in your target app before checking important names, numbers, or exact wording. The final transcript is produced separately after recording stops and may differ from the last preview you saw.

Very short recordings may finish before a useful preview appears. This does not necessarily mean the recording failed.

## Expand or collapse the preview

The compact recording indicator keeps the preview small while you work. Use the pointer control labeled **Expand preview** to open a larger, centered reading surface with the complete current preview. Use **Collapse preview** to return to the compact view.

The expanded view remains open while the current recording is processed. A new recording starts in the compact view.

## What happens when recording stops

When you stop recording, Whisperstream freezes the last preview and shows a spinner while it prepares the completed result. The recording indicator closes after processing finishes.

| What you see | What it means |
| --- | --- |
| Live Preview | Provisional, display-only words that can change while you speak |
| Completed text in your target app | The separately produced final transcript used after recording |

Live Preview does not control the completed result. Configure that in [Enhancement providers](enhancement-providers.md), [Dictionary](dictionary.md), or [Text insertion](text-insertion.md).

## If the preview does not appear

- Confirm that both **Show indicator** and **Live Preview** are on.
- Try a recording with a complete sentence. A very short recording may end before preview text appears.
- If Whisperstream detects a screen reader, it hides the visual recording indicator and Live Preview because changing provisional words are not announced. This does not change the completed dictation. See [Text insertion](text-insertion.md) for output compatibility options.

For microphone selection and input troubleshooting, see [Audio](audio.md).
