If Transcripts shows **History is disabled**, your organization has disabled transcript and audio saving. You cannot enable either setting or start password setup. Dictation remains available. See [Managed Lockdown for organizations](/docs/data/managed-lockdown-deployment).

## Choose what history saves

Open **Transcripts** to search and use saved dictations and imported audio. Open **Transcript settings** to choose what new records contain and how long they remain.

| Setting | New-install default | Effect |
|---|---|---|
| **Save transcripts** | On for Windows, off for Mac | Saves new completed dictations. Turning it off pauses new records without deleting existing history. |
| **Save audio** | On | Keeps the recording with each new transcript. It is available only while **Save transcripts** is on. |
| **Keep for** | 7 days | Keeps records for **1 day**, **7 days**, **30 days**, or **Forever**. |

On a Mac, nothing is saved until you turn on **Save transcripts** and complete the steps below. Turning **Save audio** off leaves existing audio in place. A shorter **Keep for** period asks for confirmation, then deletes older records and their audio. **Forever** turns off age-based deletion. Retention applies to dictations and imports.

## Keep a recovery key

Saved history is encrypted. Its everyday key is stored in Keychain on a Mac or in your Windows account on a PC, so history opens without a password. A recovery key is your backup if that key is ever lost.

### Turn on saving

1. Open **Transcript settings** and turn on **Save transcripts**.
2. In **Save transcripts on this Mac?** or **Save transcripts on this PC?**, choose **Enable saving**. On a Mac, macOS may ask to use Whisperstream's encryption key in Keychain. Allow access to continue.
3. Save your recovery key with **Copy**, **Print**, or **Download**.
4. Optional: select **Also add a recovery password** and enter it twice. Use at least 12 characters. Very common passwords are not accepted.
5. Select **I saved my recovery key somewhere other than this Mac** (**this PC** on Windows), then choose **Done**.

**Done** stays unavailable until you save the key and select the checkbox. Keep the key off this computer, for example in a password manager or on paper.

A recovery password is a second way to restore history. It doesn't lock history or add a password prompt.

If this computer loses its key and you also lose your recovery key and recovery password, your saved transcripts can't be recovered.

### Finish setup for existing history

History saved without a recovery key keeps saving, but **Transcripts** shows **Finish recovery setup** instead of your records. Choose **Set up recovery key**, then follow steps 3 to 5 above. Whisperstream also shows a one-time prompt at launch, **Add a recovery key for your transcripts**, that opens the same setup.

### Change recovery settings

Open **Transcript settings** and select **Recovery settings**.

- **Create new key** makes a replacement recovery key. Save it, select the checkbox, and choose **Done**. Your previous key stops working once you choose **Done**.
- **Add password**, **Change password**, and **Remove password** manage the recovery password. Removing it leaves your recovery key working.

## Restore history if the key is missing

If Keychain on this Mac or Windows on this PC no longer has the key, Transcripts shows **Restore your transcript history** and saving pauses. Your saved transcripts stay on this computer until you restore them or start fresh.

1. If you added a recovery password, choose **Recovery key** or **Recovery password**.
2. Enter it and choose **Restore history**.

Whisperstream puts the key back in Keychain or your Windows account. History opens again, saving resumes, and you won't need the recovery key on this computer again.

After too many incorrect attempts, Whisperstream shows **Too many attempts** with a countdown. Wait for it to finish, then try again.

If no recovery key was set up, the screen explains that your transcripts can't be restored and offers only **Start fresh**.

### Start fresh

Start fresh permanently deletes all saved transcripts and audio without opening them. It can't be undone.

1. Choose **Lost your key? Start fresh** (or **Lost both? Start fresh** if you added a recovery password).
2. Review **Start fresh without your history?** and choose **Continue**.
3. Type `DELETE`, then choose **Delete all saved transcripts**.
4. Save your new recovery key, select the checkbox, and choose **Done**.

If you find your old recovery key later, it won't work with the new history.

## Find and open a transcript

The Transcripts page shows the newest records first. Enter a word or phrase in **Search transcripts...** to narrow the list, then select a record to expand it.

Records label the recognized text and optional cleanup separately:

- **Original** is the transcript produced before enhancement.
- **Enhanced** is the cleaned-up version. Open the record and use **Show original** or **Show enhanced** to switch between them.

Version details reflect what happened during that recording:

- Success shows **Enhanced** and the profile that ran.
- Partial fallback keeps **Enhanced**, names the profile, and explains that some original wording was kept.
- Complete timeout or failure keeps **Original** and explains why the original was used.

Older records may show **Original** or **Enhanced** without a profile or explanation. Whisperstream does not guess missing details from current settings.

To change which profile applies to future recordings, see [Profiles and routing](/docs/enhancement/profiles-routing).

An expanded record lets you:

- Read the complete transcript.
- Use **Copy transcript** to copy the version currently shown.
- Play, pause, and seek through a saved recording.
- Select **Save a copy** to save that recording as a WAV file in a location you choose.
- Use **Delete transcript** to remove the record and its audio.

You can also copy a collapsed record. Copy uses the complete formatted text, not the shortened preview. It copies **Enhanced** when available and otherwise **Original**. After expansion, Copy follows the version shown.

If a record has no saved audio, its transcript remains available but the player and **Save a copy** action do not appear.

## Import an audio file

Turn on **Save transcripts** before importing audio. Then:

1. Open **Transcripts** and select **Add file**.
2. Drop audio files into the import area, or select **Choose files**.
3. Keep Whisperstream open while each file is processed.
4. Wait for the file to show **Done**, then return to the list and open its transcript.

WAV files always work. Other formats, such as MP3 or M4A, may not import on every setup. A file that shows **Failed** does not stop the remaining files in the queue.

Imported records follow your **Save audio** and **Keep for** choices. If **Save audio** is off, Whisperstream keeps the imported transcript without a playable recording.

Whisperstream does not upload the source audio. If online enhancement is enabled, it can send the resulting transcript to your configured provider.

## Delete saved history

Deletion actions require confirmation. Deleted records cannot be restored in Whisperstream.

- Use **Delete transcript** on one record to delete that transcript and its associated audio.
- Open **Transcript settings** and select **Delete all transcripts** to delete every record and saved recording.
- Choose a shorter **Keep for** period to delete records older than that period.

Turning **Save transcripts** off only pauses new records. Use one of the deletion actions when you want to remove records already saved.

If the Transcripts page asks for a transcript password, see [Lockdown Mode](/docs/data/lockdown-mode) for its history controls.
