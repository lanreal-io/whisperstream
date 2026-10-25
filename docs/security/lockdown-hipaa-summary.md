## Lockdown Mode and HIPAA

### About this document

Whisperstream is a local dictation app from Lanreal Technologies Inc. This summary describes the official Windows and macOS implementation; modified builds require their own assessment. This one-page summary explains the processing path controlled by Lockdown Mode, the safeguards it includes, and why Lanreal does not act as a business associate for that path. You can keep this document in your own compliance records.

### What Lockdown Mode covers

Lockdown Mode keeps Whisperstream transcription, cleanup, and saved history on your device. It does not govern applications or services that receive your text, or other software running on the device. Evaluate those separately as part of your compliance program.

Saving transcript history is optional. When you enable it, the password, recovery, encryption, idle-lock and access-log controls below apply. These controls are designed to support your assessment of certain HIPAA Security Rule technical safeguards in 45 CFR 164.312:

- **Access control (164.312(a)(1)).** Your transcript history is locked behind a password. A key derived from that password wraps the encryption key. Your password or recovery key is required to unlock the history.
- **Automatic logoff (164.312(a)(2)(iii)).** Saved history locks again automatically after a period of inactivity that you set.
- **Encryption at rest (164.312(a)(2)(iv)).** Saved transcript text and audio are encrypted at rest with AES-256-GCM.
- **Audit controls (164.312(b)).** A content-free access log records when your history is opened, locked, and unlocked, and when an unlock fails. It never records what your transcripts say.
- **Integrity (164.312(c)).** Authenticated encryption is designed to detect changes to encrypted transcript content.
- **Person authentication (164.312(d)).** Opening saved history requires your password, and repeated failed attempts trigger escalating delays.

### Why there is no Business Associate Agreement

For this defined Whisperstream processing path, Lanreal does not receive your microphone audio, transcript content, or dictated text and does not act as a business associate. This conclusion does not extend to other applications or services you use, or support or hosting arrangements that give someone access to protected health information.

### What this is not

- This is not a HIPAA certification. The U.S. Department of Health and Human Services does not certify or approve any product as HIPAA compliant.
- This is not a compliance guarantee or legal advice. It describes how the software handles your data so you can evaluate it for your own program.

### What remains your responsibility

Lockdown Mode addresses certain technical safeguards. As the covered entity or business associate, you remain responsible for the rest of your HIPAA program, including:

- **Administrative safeguards (164.308).** Your risk analysis, policies, and workforce training.
- **Physical safeguards (164.310).** Facility access and device controls.
- **Applications and services.** Assess applications and services that receive your text, and other software that can access sensitive information.
- **Unique user identification.** Whisperstream relies on your operating-system account to identify the user. Protected secrets use CurrentUser DPAPI on Windows and Keychain on macOS. The Lockdown password is a second gate on transcript history, not a separate identity for each person who shares the device.
- **Recovery and deletion.** If you lose both your password and recovery key, your history can't be restored. Reset deletes it instead.

### Footer

Version: October 2026.

This summary describes Lockdown Mode's defined Whisperstream processing path. It does not create any contract, warranty, or Business Associate Agreement.
