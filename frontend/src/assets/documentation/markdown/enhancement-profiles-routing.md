# Profiles, Email mode, app detection, and site detection

Whisperstream 2.0.0.

## Choose how enhancement rewrites text

An enhancement profile controls how a completed dictation is cleaned up. Set a default, use a Hotkey, or add app and website rules to choose the right style.

Whisperstream includes three built-in profiles:

| Profile | Use it for | Result |
|---|---|---|
| Chat | Messages, notes, and general prose | Cleans punctuation, casing, fillers, and dictated structure while preserving your meaning. |
| Code | Software-related dictation | Preserves technical intent and formats common code terms, filenames, identifiers, and acronyms. It does not write or refactor code. |
| Email | Dictated email | Organizes a greeting, body paragraphs, and a sign-off when you dictate one. |

Built-in profiles have fixed icons and instructions and cannot be deleted. You can rename them, set a default, assign rules or a Hotkey, and edit the Email signature.

Stream-1 Mini supports Chat and Email cleanup in English with built-in prompts. While it is selected, a custom profile's prompt is shown but cannot be edited; switch to Customizable or a cloud provider to edit it. Code profiles remain saved but insert original text with Stream-1 Mini.

Profile cleanup runs only when enhancement is on. To enable enhancement or choose where cleanup runs, see [Enhancement providers](enhancement-providers.md).

## Choose a default or switch manually

The default profile handles recordings that do not match an app or website rule.

1. Open **Enhancement** and select a profile.
2. Choose **Make default**.

Choose **Remove as default** to clear it. If there is no default and no other rule applies, Whisperstream inserts the original transcript without enhancement.

To switch manually, open a profile and assign its **Hotkey**. The Hotkey selects the profile without recording and remains active in the same application. Switch applications or choose **Resume auto-routing** to return to automatic routing.

## Create a custom profile

Use a custom profile when the built-in styles do not produce the result you want.

1. Open **Enhancement** and choose **Add profile**.
2. Enter a **Profile Name** and optionally choose an **Icon**. Keep the letter option to use the profile's first letter.
3. Write your **Cleanup prompt**, or choose a Blank, Chat, Code, or Email **Template** as a starting point.
4. Expand **Try it** and test representative dictation against the instructions.
5. Add any rules or Hotkey you need, then choose **Create**.

State the formatting, tone, and terms to preserve, then test before setting a default. Templates are editable examples, not copies of built-in instructions.

Custom profiles can be renamed, edited, or deleted. Deleting one also removes its rules.

## Configure Email and its signature

Email structures dictated thoughts. Its cleanup adds a greeting or sign-off only when you dictate one. A saved Signature is separate.

Open the built-in Email profile and enter an optional **Signature**, for example:

```text
Best,
Alex
```

Whisperstream appends it when Email runs and the dictation lacks a sign-off. Leave **Signature** blank to add nothing.

## Add app and website rules

Open a profile, find **Activated when using**, and choose **Add**.

- Select **App** to choose a running or installed application. If it is missing, choose **Browse...** and select its executable.
- Select **Website** and enter a host such as `mail.google.com`. Subdomains are separate rules, so `mail.google.com` and `google.com` can use different profiles.

Remove a rule from its profile when you no longer want it.

## See which profile is running

While enhancement will run, the recording indicator shows the effective profile's icon or initial during recording and processing. The mark covers manual, website, application, and default routing. It appears only for the active dictation and is absent when no profile applies.

For saved results, see [Transcript history](transcript-history.md). Provider timeouts and other fallback behavior belong in [Enhancement providers](enhancement-providers.md).

## Understand routing order

Whisperstream selects one profile for each recording in this order:

1. Active manual Hotkey choice.
2. Matching website rule.
3. Matching application rule.
4. Default profile.

If none applies, enhancement is skipped. Normally, app and website rules use the destination active when recording starts. Switching windows while speaking does not change the match.

When the wrong profile runs, choose **Resume auto-routing** first, then review the website rule, application rule, and default in that order.
