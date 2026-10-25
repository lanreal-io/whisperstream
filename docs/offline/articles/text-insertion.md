## Choose an input style

Open **Controls** and choose an **Input Style**. Start with Clipboard for standard apps and terminals. Choose Keystroke for screen readers, remote or virtual apps, games, and other destinations where paste is blocked or unreliable.

| Input style | What the destination receives | Best starting point |
|---|---|---|
| Clipboard | Text pasted from the clipboard | Standard apps, editors, and terminals |
| Keystroke | The completed text one character at a time | Screen readers, RDP, Citrix, virtual apps, and game chat |

Test the exact field where you plan to dictate. Applications can handle paste and simulated typing differently, even between fields in the same app.

## Use Clipboard

Select **Controls** > **Input Style** > **Clipboard**. This is usually the faster option because Whisperstream pastes text instead of typing it one character at a time. On a Mac, it pastes with Command-V.

Clipboard temporarily replaces copied text while Whisperstream inserts the dictation. On **Home**, **Restore clipboard after dictation** controls whether Whisperstream restores the previous clipboard text afterward.

- Turn it on when you want to keep text you copied before dictating.
- Turn it off when you want the dictated text to remain available for another paste.
- Copied images and files cannot be restored. Only clipboard text is preserved.

If the destination blocks paste or no text appears, switch to Keystroke.

## Use Keystroke

Select **Controls** > **Input Style** > **Keystroke**. Whisperstream types the completed text character by character and does not change the clipboard.

Use Keystroke first for:

- NVDA, JAWS, and other screen readers;
- Remote Desktop Protocol (RDP);
- Citrix and other virtual applications;
- game chat fields; and
- destinations where Clipboard is blocked or unreliable.

On Windows, if Whisperstream detects a screen reader before you choose an input style, it selects Keystroke automatically. A later manual choice takes precedence. Whisperstream does not detect VoiceOver on a Mac, so choose Keystroke yourself if you need it.

Keystroke can be slower, especially for long dictations. If a remote or virtual app drops characters or puts them out of order, open **Controls** > **Advanced** > **Typing Delay**. Start with **None**, then increase the delay one step at a time until the full phrase arrives correctly.

## Account for destination restrictions

- **RDP, Citrix, and virtual apps:** Test inside the remote app, not only in a local field. Clipboard redirection and keyboard handling can differ.
- **Games:** Test a harmless chat field first. Some games and anti-cheat systems ignore simulated input, so neither style is guaranteed to work. Do not change game or operating-system protections to force delivery.
- **Mac apps:** Both input styles need Accessibility access. See [Allow access on a Mac](/docs#allow-access-on-a-mac).
- **Elevated apps:** Windows may block input from an app running at a lower privilege level. If the destination runs as administrator and neither style works, run Whisperstream at the same privilege level and test again.

## Test the destination

1. Focus the exact field where text should appear.
2. Dictate a short phrase with punctuation and any line breaks you need.
3. Confirm that every character appears once and in order.
4. If Clipboard fails, switch to Keystroke.
5. If Keystroke loses characters, increase **Typing Delay** one step and repeat.

If neither style works after these checks, treat that field as unsupported.
