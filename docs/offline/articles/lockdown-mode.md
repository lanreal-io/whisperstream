## What Lockdown Mode changes

Lockdown Mode limits AI cleanup to **Local**, makes cloud providers and Ollama unavailable, and can protect saved history with a password.

It does not control the app receiving inserted text. While Lockdown is effective, Whisperstream suspends [Agent Speak](/docs/workflows/agent-speak) processing; it does not stop the connected coding agents themselves.

It is off by default. Open **Home**, find **Lockdown Mode**, and turn it on to begin.

## Turn on Lockdown Mode

1. If the acknowledgment screen appears, review the setup summary, select the acknowledgment, and choose **Continue**.
2. Choose **Keep encrypted history** or **Don't save transcripts**.

### Keep encrypted history

1. Choose whether **Save audio** is on.
2. Under **Keep for**, select **1 day**, **7 days**, **30 days**, or **Forever**.
3. Create and confirm a transcript password. Use at least 12 characters. Very common passwords are not accepted.
4. Save the recovery key with **Copy**, **Print**, or **Download**.
5. Select **I've saved my recovery key somewhere other than this computer**, then choose **Finish setup**.

Whisperstream shows the recovery key once. Keep it somewhere other than this computer. Your password or current recovery key can reopen locked history.

Passwords created before the 12-character rule still unlock history. The rule applies when you set or change a password.

### Don't save transcripts

This stops saving new records without creating a password or recovery key. Existing records are not deleted.

If you change your mind, open **Home > Lockdown Mode > Manage**, choose **Start saving transcripts**, and complete the password and recovery key steps.

## Lock and unlock transcript history

Password-backed history locks after inactivity. The default is **5 min**. To change it, open **Home > Lockdown Mode > Manage** and set **Lock transcripts after idle** to **Never**, **5 min**, **10 min**, **15 min**, **30 min**, or **1 hour**. This locks history, not the app.

Enter your password to unlock history, or choose **Use recovery key instead**. After unlocking with the recovery key, you set a new password.

**Manage** also offers **Change password** and **View recovery key**. **View recovery key** asks for your password, then **Generate a new key** replaces the current key, which stops working. Save the new key and select the checkbox before choosing **Done**.

## Use Local enhancement

While Lockdown Mode is on, **Local** is the only available enhancement provider. Entering Lockdown from another provider selects Local and turns enhancement off. An enabled, ready Local setup can remain enabled.

To use cleanup under Lockdown, complete the Local setup and then turn on **AI Enhancement**. See [Enhancement providers](/docs/enhancement/providers) for the Local download and speed-check steps.

## If your organization manages Lockdown

Home may show **Managed by your organization**. This removes turn-off and destructive-reset actions. Unless your organization also disables history, you can still manage transcript saving, your password, recovery key, and idle lock. When history is disabled, transcript and audio saving stay off and password setup is unavailable. You can still dictate. Contact your administrator if the device should no longer be managed.

For enterprise capabilities and deployment guidance, see [Managed Lockdown for organizations](/docs/data/managed-lockdown-deployment). Managed Lockdown is available on Windows only. On a Mac, you turn Lockdown Mode on and off yourself.

## Export safe diagnostics

Safe local diagnostics remain available in voluntary and managed Lockdown. **Export logs** creates a local ZIP only when requested and never uploads it. It excludes transcript and audio content, credentials, recovery keys, and the access log. See [Settings and data locations](/docs/reference/settings-data-locations) for steps.

## Turn off Lockdown Mode

1. Open **Home > Lockdown Mode > Manage**.
2. Choose **Turn off Lockdown Mode**.
3. Enter the transcript password if you chose **Keep encrypted history**.
4. Choose **Turn off**.

If you chose **Don't save transcripts**, no password is required. Turning Lockdown off makes cloud providers and Ollama available again. Existing transcripts remain on this device and no longer need the Lockdown password. Your Lockdown recovery key and password stay set up as recovery methods for standard history. See [Transcript history](/docs/data/transcript-history) to change them. **Local** stays selected and AI Enhancement stays off until you choose otherwise.

## Reset when recovery is impossible

If you cannot use either the transcript password or recovery key, open the turn-off dialog and choose **I forgot my password**. This is a destructive reset.

Type `DELETE`, then choose **Delete history and turn off Lockdown**. The reset deletes saved transcripts and audio, turns off Lockdown Mode, and the old records cannot be restored in Whisperstream.
