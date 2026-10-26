# Whisperstream Privacy Policy

**Version 1.1**

**Effective 2026-10-26**

This Privacy Policy explains how Lanreal Technologies Inc. ("Lanreal,"
"we," "us," or "our") collects, uses, shares, and retains personal
information in connection with the Whisperstream desktop application for
Microsoft Windows and Apple macOS (the "Software"). It also describes the rights you
have with respect to that personal information and how to exercise them.

## 1. Introduction and Scope

Whisperstream is a local, on-device dictation application for Microsoft
Windows and for macOS on Apple silicon. It captures microphone audio when
you press a push-to-talk hotkey, transcribes that audio on your own
computer using a speech recognition model that runs entirely on your
device, and types the resulting text into the focused window. **Speech recognition runs on your device.**
The source edition is free of charge and includes all features without activation. It does
not validate licenses or submit in-app ratings, reviews, feedback or feature
requests. This policy applies whether you build the Software from source or
install it with the official installer. Built from source, it makes no
application-update requests; installed from the official installer, it checks
for signed updates (Section 3.2).

App-managed model downloads use pinned public Hugging Face URLs, with GitHub
release archives available as a verified backup for supported speech models.
Kokoro model downloads use Hugging Face only. These requests disclose your IP address, ordinary HTTP
headers and the requested model URL to the serving hosts and their delivery
infrastructure. There is no remote model-availability manifest, and no build
requests an online cloud-model list.
Other data may leave your device when you deliver text to a destination
application, enable optional cloud cleanup, or open external help (Section 3.12).

Imported audio remains local, although its resulting transcript may enter
cloud cleanup if you enabled it. Agent Speak exchanges content with Codex or Claude Code over the local loopback
interface; the selected coding assistant separately controls transmissions to
its configured model provider. Each flow is described
in detail in this Privacy Policy.

This Privacy Policy covers the Whisperstream desktop application only.
It does not cover the marketing website at `whisperstream.io`, which is
governed by a separate website privacy notice published at
`https://whisperstream.io/privacy-website`. If you reached this document from the marketing
site, please refer to the website privacy notice for information about
how the site itself is operated.

Privacy notices for other Whisperstream editions are linked from
`https://whisperstream.io/privacy`.
This policy does not waive an earlier applicable privacy commitment or other
binding obligation.

In this Privacy Policy, 'personal information' means information about an identifiable individual, encompassing 'personal data' as defined by the GDPR (Article 4(1)), 'personal information' as defined by the CCPA (Cal. Civ. Code s.1798.140(v)), and 'personal information' as defined by Canadian federal privacy law (s.2(1)).

## 2. Who We Are

This policy describes Lanreal's handling of personal information and features
you operate on your device. Lanreal is responsible for processing it determines
for its own purposes, including official software delivery, agreement evidence
and information it receives or retains for correspondence, privacy requests and
claims. The company is:

**Lanreal Technologies Inc.**
18 King Street East, Suite 1400
Toronto, Ontario M5C 1C4
Canada

Lanreal is a corporation incorporated under the *Business Corporations
Act* (Ontario). Our registered office is in the Province of Ontario,
and our operations are governed by Canadian federal and provincial
law applicable therein.

**Privacy Officer designation.** In accordance with Canadian federal
privacy law (the Personal Information Protection and Electronic
Documents Act), Lanreal Technologies Inc. has designated a Privacy
Officer responsible for compliance with this Privacy Policy. The
Privacy Officer can be contacted at `support@whisperstream.io`.
Throughout the remainder of this document, "Canadian federal privacy
law" refers to the statute named above.

Where the GDPR applies, Lanreal acts as controller for its own processing
described above. Sections 4 and 6 distinguish it from your use of the installed
tool and your provider's services. Canadian and California responsibilities
depend on the actual activity and applicable law. We preserve all rights and
commitments in this policy, including the California rights we offer regardless
of whether statutory business thresholds apply.

All privacy-related communication, including data subject rights
requests, complaints, and general questions about this Privacy Policy,
should be directed to `support@whisperstream.io`. This is Lanreal's
single privacy contact channel for the Software.

**EU Representative.** Lanreal has not designated an EU representative. You may contact Lanreal directly at
`support@whisperstream.io` about your personal information. This direct contact
does not substitute for a representative where Article 27 requires one. We will update this notice if a
representative is appointed.

## 3. Information We Collect

The Software is intentionally narrow in the information it handles.
This section enumerates every category of information associated with
your use of the Software, in the order in which the underlying network
activity happens.

### 3.1 Official installer request

The free official installer is provided after you complete a contact form on
the Whisperstream website. Information you submit through that form is
collected by the website and governed by the separate website privacy notice
identified in Section 1. The installed Software does not transmit or require
that information. Building from source requires no form.

### 3.2 Application updates

How you get the Software changes only the requests in this section.

**Built from source.** The Software makes no automatic or manual
application-update requests. To update, choose
a newer source revision and rebuild it using that revision's instructions.

**Official installer.** After setup, the Software checks for a signed update at
most about once a day, and when you choose to check from About. It downloads
and installs an update only after you accept it, and verifies the update's
signature first. Update requests go to Lanreal storage hosted by Cloudflare
(R2). They disclose your IP address, ordinary HTTP headers including the
client library's User-Agent, and the requested file. They do not include audio,
transcripts, prompts, API keys, account or device identifiers, or your
settings. Lockdown stops these requests.

Opening the Source code link launches your browser at the repository without
attaching application data. GitHub receives the browser connection and any
account or service-usage information governed by your browser session and
GitHub's policy. Section 3.12 describes external handoffs.

Update choices do not affect local dictation with installed compatible models.
Model download routes are described in Section 3.3.

### 3.3 Speech-recognition model download data

During setup, or after you select a language or speech-recognition engine,
the Software downloads the required model if it is not already installed.
Parakeet, Qwen and Cohere model files use pinned public Hugging Face URLs.
Requests can redirect through Hugging Face's file-delivery infrastructure,
including its Xet/CDN delivery hosts. For Parakeet v2/v3, Qwen and Cohere
(except the macOS Core ML model described below),
if the source remains unavailable after retries, the Software tries the
matching upstream sherpa-onnx release archive at github.com and
release-assets.githubusercontent.com. The archive is verified against a
packaged checksum before extraction and required files are verified before
installation. Cancellation, disk, integrity and download-policy failures
do not trigger a backup. The serving hosts receive your IP
address, ordinary HTTP headers including the download library's User-Agent,
and the requested URL, which identifies the model.

On macOS, the multilingual Parakeet v3 model is a Core ML version downloaded
from a pinned Hugging Face repository and its delivery hosts, with no
automatic backup host. It is stored under
`~/Library/Application Support/FluidAudio/Models/`, or in the storage
location you choose for it, rather than in the Whisperstream application
data directory (Section 3.4).

These public downloads connect directly from your device to the upstream file
hosts and their delivery infrastructure. They do not use your browser session
or import your browser cookies or Hugging Face token cache. A delivery host may
set cookies within the download client's own connection session.
These model-download requests do not send upstream host logs to Lanreal. The
app keeps local diagnostic events about downloads and does not upload them
automatically. If you separately share a diagnostic export, Lanreal receives
it as correspondence under Section 3.13.
The Software does not attach a Hugging Face authentication token, Lanreal
account identifier, license key, device identifier, microphone audio,
transcript, generated speech or cloud-provider API key to these requests.
Each file is verified locally against a pinned cryptographic checksum before
installation. The model catalog is packaged with the app; opening model
settings does not request an online availability manifest. Already installed compatible models remain usable
without a replacement download.

Cohere Transcribe performs speech recognition on your device. Its model URL
identifies the Cohere artifact, but your selected language remains local and
is not included in the request. No audio or transcript is sent to Cohere or
Hugging Face or GitHub for speech recognition. Optional cloud cleanup is separate
(Section 6). Model-host roles and international transfers are described in
Sections 5 and 7.

### 3.4 Data that stays on your device

The following information is handled on your own computer and is not
transmitted to Lanreal. User-directed delivery to a destination
application and the optional outbound flows identified below are the
specific exceptions to local-only handling:

- **Audio recordings.** Microphone audio is held in random-access
  memory while each push-to-talk capture is transcribed on your device,
  and is never uploaded to any server. When local transcript history
  and audio saving are both on (on Windows both are
  on by default; on macOS transcript saving stays off until you turn it
  on, and audio saving is then on by default; both can be turned off),
  a copy of the recording is
  also stored, encrypted, on your own device, as described in the
  "Local transcript history" entry below. No audio data is ever
  transmitted off your device.

**Voice data is not biometric data.** The speech recognition model processes your voice solely to convert speech into text. It does not create a voiceprint, does not perform speaker identification or authentication, and does not extract or store any biometric identifier from your voice. Under the GDPR (Article 9), biometric data is subject to enhanced protections only when processed for the purpose of uniquely identifying a natural person. Because the Software processes voice exclusively for transcription and not for identification, the voice data handled by the Software does not constitute biometric data within the meaning of Article 9.

- **Transcribed text.** The text produced by the speech recognition
  model is typed into the window you have focused. If you keep the
  local transcript history enabled (it is on by default on Windows, is
  off on macOS until you turn it on, and can be turned off), the
  transcribed text (both the raw transcript and, if
  you use the optional AI cleanup feature, the enhanced version) is
  also stored, encrypted, on your own device, as described in the
  "Local transcript history" entry below. Transcribed text is never
  transmitted to Lanreal or retained by Lanreal in any form.
- **Local transcript history.** The Software keeps a searchable
  history of your past dictations on your own device so that you can
  review, copy, and replay them. This history is enabled by default on
  Windows, stays off on macOS until you turn it on, and can be fully
  disabled. It stores your transcribed text (the raw
  transcript and, where you used AI cleanup, the enhanced version)
  and, when audio saving is enabled, the recorded audio. The
  transcribed text and the audio are encrypted at rest using
  AES-256-GCM. The AES data key is protected by your operating system. On
  Windows it is protected with user-scoped DPAPI. Windows normally limits
  decryption to the protecting user's credentials on that computer; a
  roaming profile can permit decryption on another computer. On macOS it is
  stored as an item in your login Keychain that is not set to synchronize
  through iCloud Keychain. Neither protection isolates the key from
  processes acting with that user's authority. The raw transcript, enhanced transcript, saved
  audio, and waveform peaks used by the player are encrypted separately.
  Lightweight index metadata (such as a record identifier, timestamp,
  duration, model name, source type, and relative audio-file location)
  is stored unencrypted so the list can be sorted and displayed without
  decrypting its content. The history is kept in your application data
  directory, `%LOCALAPPDATA%\Whisperstream\` on Windows or
  `~/Library/Application Support/Whisperstream/` on macOS (a
  `transcripts.db` index and a `recordings` folder). History is not uploaded
  automatically. You may separately copy or export content, or explicitly
  request cloud reprocessing as described in Section 6. You control it: a
  master "Save transcripts" toggle and a separate "Save audio" toggle
  let you keep a text-only history or turn history off entirely, and
  a retention window (1, 7, or 30 days, or forever, with 7 days as
  the default) automatically removes older entries. You may also
  delete an individual entry or clear the entire history at any time.
  See Section 8 for retention and deletion details.
- **Local diagnostic logs.** In ordinary use, voluntary Lockdown Mode, and
  administrator-managed Lockdown Mode, the Software writes
  content-free operational events to a local `whisperstream.log` file
  in the `logs` folder of the application data directory. The active file and up to
  three backups are each limited to approximately 2 MiB. The Software
  excludes transcript, voice/audio, and Agent Speak content; passwords;
  recovery codes; API, transcript-encryption, and license keys or secrets;
  full email addresses; foreground process identity; imported-audio filenames
  and paths; browser hosts; and raw error text that could carry user content.
  Each application window also keeps a bounded in-memory frontend log
  buffer (up to 500 entries, 2 KiB per entry, and 128 KiB total). The
  backend re-sanitizes synchronized frontend entries and keeps at most
  128 KiB across windows in process memory. A supplemental Lockdown diagnostic
  file accepts only code-defined event and field names, retains at most 256
  events and 128 KiB, and contains no arbitrary user-supplied strings. These
  logs are never transmitted automatically. Lockdown does not pause, clear, or
  disable the diagnostic log, frontend capture, or support export.
- **Usage statistics.** The Software keeps aggregate counters of how
  many transcriptions you have run, how many words you have
  transcribed, and how much recording time you have accumulated.
  These counters are stored locally in the application data directory
  and are never transmitted.
- **Configuration.** Your settings, including your chosen hotkey,
  audio device, and word-override dictionary, are stored locally in
  a configuration file in the application data directory and are
  not transmitted, except that matching Boost terms from your
  Dictionary are included in optional cloud cleanup requests
  (Section 6.2).
- **Lockdown access log.** While Lockdown Mode is effective, the Software
  keeps a separate content-free access log that records the time and an
  allowlisted operation type for transcript-history access and Lockdown
  state changes, together with a sanitized short record identifier when
  applicable. It never records transcript or audio content, your password
  or recovery code, or health information. The active `access.log` file
  and up to five backups are each limited to approximately 2 MiB. This
  audit log is stored only in the `logs` folder of the application data
  directory, is
  not included in support exports, and is not created for ordinary
  transcript operations outside Lockdown Mode.

Lanreal does not automatically receive the local stores described in
this Section 3.4. Particular content may nevertheless leave your device
through a separate, user-directed flow described elsewhere in this
Policy: finished text is delivered to the destination application under
Section 3.10; a transcript may be sent to the cloud-cleanup provider you
configure under Section 6; an Agent Speak reply is returned to Codex or
Claude Code under Section 3.11. Those recipients
control their own copies under the terms identified in the applicable
section. If you choose to download a diagnostic archive, the backend builds
it in memory and streams it to the destination you select; it does not write
an app-managed ZIP file. That downloaded copy is under your control. If you
then send it to Lanreal as part of a bug report (for example, by attaching
it to an email to `support@whisperstream.io`), Lanreal processes the
information you send to diagnose the issue and retains it under the retention
criteria and prior applicable commitments described in Section 8.

A summary of the categories above, mapped to the CCPA categories of
personal information, is provided in Section 13 of this Privacy Policy.

### 3.5 GitHub star prompt

After sustained use, the Software may occasionally ask whether you are enjoying
it. Yes offers Star on GitHub, which opens the repository in your browser. Not
really offers Open an issue, which opens the GitHub issue chooser. The app
collects no rating, feedback text, identity, transcript or version, prefills
nothing and uploads nothing. A local `review_state.json` file records only a
session count, the dates on which the app was used, and when the prompt was
shown, dismissed or acted on, so the prompt stays occasional. It is never
transmitted. The prompt never appears in Lockdown Mode.
Starring or filing an issue is a separate action in your browser; GitHub's
handling is described in Sections 3.12 and 5.

### 3.6 Lockdown Mode (optional hardening)

Lockdown Mode is an optional setting you can turn on to harden the
Software for privacy-sensitive and HIPAA-conscious workflows. It is
off by default. Lockdown Mode is designed to support HIPAA-conscious
workflows; it is not a HIPAA certification, it does not make the
Software HIPAA compliant, and it does not replace your own compliance
obligations. When Lockdown Mode is on:

- **AI cleanup runs on your device only.** The cloud providers and
  self-specified endpoints described in Section 6 are disabled, and AI
  cleanup is forced to the on-device model, so Whisperstream does not
  send that transcript to a configured cleanup provider.
- **Your transcript history is locked behind a password.** A key
  derived from a password you choose wraps the transcript encryption key,
  and a key derived from a one-time recovery code wraps it separately.
  While Lockdown Mode is on, the transcript encryption key is stored at
  rest only in these two wrapped forms; the operating-system key
  protection described in Section 3.4 no longer holds a copy. The
  transcripts remain encrypted with AES-256-GCM (see Section 12 for the
  details). The recovery code is created when you turn Lockdown Mode on
  and is the only other way to unlock your history. The history locks again automatically after an idle
  period you set, and repeated failed unlock attempts trigger
  escalating delays.
- **Or you can choose to save nothing.** Instead of a
  password-protected history, you may turn Lockdown Mode on without
  saving transcripts at all, in which case no transcript history is
  written to your device.
- **Safe diagnostic logging remains available; the content-free access log is
  separate.** The bounded local diagnostics described in Section 3.4 remain
  active so you can troubleshoot problems and prepare a useful bug report.
  The separate local access log records allowlisted transcript-history and
  Lockdown operations (never their content) and is not included in support
  exports. Whisperstream never uploads either log automatically.

**Data-loss warning.** Your Lockdown Mode password and your recovery
code are held only on your own device. Lanreal never receives your
password, your recovery code, or your transcripts. If you set a
transcript password and lose both your password and your one-time
recovery code, you will not be able to unlock your transcript history
through the Software, and Lanreal cannot unlock or recover it for you.
There is no Lanreal-side backup. The Software offers a destructive reset
that removes the still-locked transcript history through ordinary database
and file deletion and turns off Lockdown Mode; it cannot restore the old
records or securely overwrite the storage device.

In Lockdown Mode, Whisperstream's audio, transcription, history, and AI
cleanup pipeline stays on your device and Lanreal never receives that
content. Lockdown Mode does not prevent the text from being delivered
to the destination application you selected. It blocks Whisperstream's
Agent Speak integration, but does not control the independent coding agent's
provider connection. Those destinations handle data under their own terms. The warranty and preserved-rights provisions are in Section 6 of the bundled
open-source license and processing notices. Lockdown Mode is not a HIPAA
certification. For this on-device workflow, Lanreal is not a business
associate and there is no Business Associate Agreement to sign. Turning Lockdown Mode off and choosing a
cloud AI provider would send transcribed text to that provider and
change this; Lockdown Mode blocks the cloud providers to keep the
Whisperstream pipeline on your device.

### 3.7 Optional voice feature model downloads

Whisperstream includes an optional voice feature that reads an
artificial-intelligence coding assistant's text replies aloud on your
device. The feature is off by default and does nothing until you turn
it on.

Supertonic 3 INT8 is the default voice engine in the open-source edition. A complete
Kokoro model already cached on your device remains usable in standard builds,
including through an existing Kokoro or Automatic selection. Only new Kokoro
downloads require experimental opt-in. If an existing Kokoro selection has no
complete cached model, it changes to Supertonic; the speech-enabled setting
and existing files are preserved. The engines have different voices and output
characteristics. The app requires the processing acknowledgment before it
processes content.

If a required voice model is absent, enabling the feature downloads it,
subject to the experimental opt-in required for new Kokoro downloads. Supertonic
and Kokoro files use pinned public Hugging Face URLs. Kokoro has no automatic
backup host. Compatible complete Kokoro caches remain usable. Kokoro files
include model weights, voice embeddings, an English lexicon and supporting
eSpeak language data under their respective licenses. These model requests use
the delivery infrastructure described in Section 3.3. The hosts receive your IP address, ordinary HTTP headers and
the model-specific URL. No spoken text, generated audio, license key, account
information, authentication token or device identifier is attached. Files are
verified against pinned checksums before installation.

The Voice Models run entirely on your device. The Software does not
transmit the text sent to the Voice Models or the audio they generate
off your device for speech synthesis. Lanreal does not receive that
text or audio and sends no separate feature-use telemetry. The model
host can retain the ordinary access-log record described in Section 8,
which can show that a model-specific file was requested. Turning the
feature off stops the Software from using the Voice Models. You can
remove a downloaded model from your device by deleting its applicable
model directory in the `models` folder of the application data directory
(Section 3.4). If you
selected a custom model storage location, delete the corresponding
`models` or `local-llm` directory from that custom location instead.

The Voice Models have different third-party license terms. The upstream Kokoro
model and lexicon use Apache License 2.0; its eSpeak language data is separately
GPL-3.0-or-later. Its official native runtime also includes separately licensed
eSpeak/Piper components. Kokoro is not subject to the Supertonic behavioural use
restrictions described below. If you download,
enable, or use Supertonic 3, your use of that model is subject to its
OpenRAIL-M behavioural use restrictions. Those restrictions are set out
in Section 5 of the bundled open-source license and processing notices and
reproduced in full in the `THIRD_PARTY_LICENSES.txt` file bundled with
the Software.

### 3.8 Optional on-device AI cleanup model download

Local cleanup can use Stream-1 Mini by Whisperstream, the default, or Gemma 4
E2B Instruct (Customizable). Stream-1 Mini is Lanreal's dictation-cleanup model,
fine-tuned from Qwen3-0.6B-Base. When you set up Local cleanup, the Software can download missing model
files for the models you select from pinned sources and
benchmark each model independently on your Device. The download hosts
receive the connection information described in Section 3.3, including
the requested model URL. Download requests do not include your transcript,
cleanup instructions, user-supplied context, license key, account
information, authentication token or device identifier. Files are verified
locally before installation. Stream-1 Mini downloads from Lanreal's
`whisperstream/stream-1-mini` Hugging Face repository at a pinned revision and
is verified against a pinned size and SHA-256 checksum. Unsloth Gemma files use
pinned Hugging Face URLs; if Unsloth remains unavailable after retries, Gemma can fall back to Google's
pinned QAT artifact on Hugging Face. That artifact has different bytes and
may produce different results. Cancellation, integrity, disk and policy
failures do not trigger fallback. Stream-1 Mini has no automatic backup. The
models and benchmarks run locally. Local cleanup with Stream-1 Mini processes
your dictation entirely on your Device; it does not send your dictation to
Lanreal or Hugging Face.
Cloud provider choices use the catalog packaged with the app; no build fetches
an updated list. User-configured cloud
processing remains governed by Section 6.

### 3.9 Acceptance state and other local edition records

If another Whisperstream edition previously ran under the same operating-system
user account,
this edition uses the same application data directory and continues to use its
settings, models, transcript history and legal-acceptance ledger. Licensing,
trial or activation records left by that edition, including, on Windows, under
`HKCU\Software\Whisperstream`, are not read, modified or transmitted and do not
limit functionality. They remain until you remove them as described in
Section 8.

The Software stores local records of the EULA version and
content hash you accepted, the acceptance time, the Software version, the
type of acceptance event, and any cloud-processing consent. EULA acceptance
evidence is kept in an append-only local ledger: accepting a later agreement
updates the current acceptance fields while preserving earlier exact
acceptance records in the same file. These records are used to identify the
current legal notice and consent state. They are not sent to Lanreal.

### 3.10 Imported audio, app/site routing, and text delivery

When you import audio, the Software reads and decodes each file in place,
transcribes it on your device, and stores the resulting entry under the same
history settings as a microphone dictation. The selected path is used only
while the import is active; its base filename may be shown in the active
progress interface but is not written to diagnostics. The Software does not
modify, delete, or make a plaintext path-based copy of your source file. If
cloud AI cleanup is enabled, the resulting transcript may be sent to the
provider you selected under Section 6. The imported audio itself is not
uploaded.

Foreground and browser information is read only when a configured app or
site rule, manual route, or destination-delivery operation can use it. App
routing uses local process identity. A configured browser site rule may cause
the Software to read only the normalized active host from a supported
browser's address bar; it does not collect the full URL through this route.
No foreground or host probe is performed when routing cannot affect the
current operation. These transient values are not written to diagnostics or
sent to Lanreal. The app paths and host rules you choose to save remain in
your local settings.

When you choose a site-routing rule, the Software may read matching favicon
data from the local Chrome, Edge, or Firefox favicon database (on macOS, also
Brave or Chromium) to display the rule. It first uses a read-only database connection. If the browser has
locked that database, it may create an attributable copy in the
operating-system temporary directory. The Software closes the copy and makes up to
three ordinary deletion attempts after use. At startup and before creating
another owned copy, it also tries to remove only attributable Whisperstream
copies older than 24 hours. A copy can remain longer after a crash or repeated
deletion failure; the Software does not broadly delete older unattributable
browser-database copies. It does not send the database, host, or favicon to
Lanreal or a network favicon service.

Whisperstream delivers finished text to the focused destination
application using a clipboard paste or simulated keystrokes. The
destination application then receives and controls that text under its
own privacy and retention practices. Lockdown Mode does not prevent this
user-directed delivery.

### 3.11 Agent Speak and coding-agent interactions

Agent Speak provides optional, default-off connections to Codex and Claude
Code. Its local plugins forward supported response text, permission context,
replies, limited session identifiers and display names to the loopback backend
for the HUD. Claude Code also supports structured questions and options;
Codex does not provide that question flow. Speech, written summaries and
provider connections have separate controls. Enabling one provider does not
enable the other. Interaction content is not automatically sent to Lanreal.

Active content stays in bounded process memory and is excluded from diagnostic
logs and support exports. Temporary hook questions and drafts expire under
their bounded interaction windows. Optional managed terminal sessions can
retain the selected session's response and draft while that session remains
registered; temporary hook expiry is not a retention limit for those sessions.
Disconnecting the applicable provider, entering Lockdown, revoking EULA
acceptance or shutting down clears the applicable active state. The coding
agent's own files and terminal session are separate and may remain.

For display names and supported integration, Whisperstream may read a limited
tail of an eligible local coding-agent transcript or reduce a working directory
to its base name. Names and paths can be sensitive. Display state remains
local and is excluded from diagnostics and exports. Local discovery and
managed-session capability files contain connection details, including a
per-run token. They are not content archives. Processes running as your
operating-system user may read them; this is not isolation from other same-user processes.

A reply you dictate is saved as an `agent_reply` entry only when Save
transcripts is on, using ordinary encrypted history retention. Other agent
interaction content is not written to Whisperstream transcript history.
Optional wrappers deliver your chosen reply into the selected agent's terminal;
the agent may then send it to its configured provider or retain it under its
own practices. Its terms and privacy notice govern that separate processing.
Lockdown blocks Whisperstream's agent integration and optional cloud cleanup,
but does not close the independent agent process or control its own network.

### 3.12 External help, source access, bug reports and feature requests

Report a bug opens the GitHub bug-report form in your browser. Request a feature
opens the GitHub feature-request form. Read documentation opens
`https://whisperstream.io/docs`; Source code opens the GitHub repository.
These actions occur only after a click and are unavailable in Lockdown Mode.
The app does not prefill a transcript, log bundle, account or device identifier,
message body, or diagnostic attachment, and does not automatically send a report.

The destination receives ordinary browser connection and session information.
If you submit an issue, GitHub and repository maintainers receive the account
identity, text and files you choose to share. Public issues and attachments can
be read or copied by others. GitHub's privacy statement is linked in Section 5;
website visits follow the separate website notice in Section 1. Email is sent
only when you send it in your mail application, which uses your configured mail
provider. Lanreal receives the sender details, message and attachments you send.

Download logs saves a local ZIP and uploads nothing. Review its contents before
sharing. The GitHub issue forms offer `support@whisperstream.io` for private
reports or logs, with an existing issue link when applicable. Report security
vulnerabilities by email instead of a public issue. Do not submit transcripts,
credentials or sensitive personal data. If we become aware of GDPR Article 9
special-category information in a submission to Lanreal, we delete or restrict
it as reasonably practicable, subject to applicable legal retention obligations.
Deleting a submission does not guarantee deletion of copies retained by GitHub
or other people.

Lanreal uses voluntary bug reports to investigate problems and feature requests
to evaluate and respond to proposed improvements, subject to the purpose limits,
rights and retention rules in Sections 4, 8 and 10. Posting a request does not
guarantee implementation or a response. Closing
an issue does not delete it, its attachments, or copies made by other people.

### 3.13 Correspondence and privacy requests

If you contact Lanreal, we receive the contact details, message and any
attachments you choose to send. We use this information to address your request
and resolve related claims. Do not send API keys, passwords or unnecessary sensitive content.
Local diagnostic exports are not automatically uploaded or sent to AI tools.

We use Gmail (Google) and Resend to send and receive email.

We also use individual consumer subscriptions to OpenAI (ChatGPT and Codex) and
Anthropic (Claude), not business or API plans, to help review some messages
and draft replies. This can make the message and attachments we submit
available to those providers. They are not Lanreal's processors for this use,
and no data processing agreement covers it. Each provider handles that content
under its own consumer terms and privacy policy, may retain it, and, depending
on those terms and our account settings, may use it to improve its models.

You can ask us not to use these AI tools for your message, in that message or
at any time; we then handle it without them. If you are in the European
Economic Area, the United Kingdom or Switzerland, we submit your message to
these AI tools only with your explicit consent (Section 7). This support
workflow is separate from the app's optional BYOK feature and its provider
permissions. Contact `support@whisperstream.io` about our handling of
correspondence.

## 4. Why We Process Your Information

The purposes and GDPR Article 6 bases below describe current operation. A
lawful basis for processing does not itself
establish an international-transfer mechanism; see Section 7.

- **Voluntary bug reports, feature requests and private contact.** Lanreal
  processes the details you choose to send to investigate problems, evaluate
  proposed improvements and respond to the matter you raise, under Article
  6(1)(f), its legitimate interest in maintaining and improving the Software.
  Public reports are optional; private contact remains available. Your objection
  and other rights are described in Section 10. GitHub and your mail provider
  separately control their service processing under their applicable policies.

- **Model-download connection information.** For public Hugging Face and GitHub
  downloads, the installed tool contacts the file host directly when you request
  a missing model. Those hosts collect connection information from your device
  and determine their own purposes and legal bases under their policies. Lanreal
  supplies the download tool and selects verified model files; it does not engage
  these hosts to process customer records on its behalf. Installed local models
  do not require recurring model-host communication for inference.
- **Official updates.** In the official installer build,
  Lanreal processes the connection information in Section 3.2, through
  Cloudflare, to deliver signed updates you accept, under Article 6(1)(f), its
  legitimate interest in keeping the Software secure and current. Builds from
  source make no such request.
- **Features you operate.** Local capture, transcription, cleanup, history
  and delivery serve the purposes for which you or your organization use the
  Software. You select the content and its use. We explain these operations
  for transparency; not every local file is processed for Lanreal's purposes.
  Organizations remain responsible for their own lawful basis and safeguards.
- **Agreement evidence and support.** The local acceptance record operates on
  your device as part of the Software, and the ordinary app does not upload it.
  Lanreal processes acceptance information only if you send it to us, under
  Article 6(1)(f), its legitimate interest in accurate agreement evidence.
  Information you separately send
  for support is processed for our legitimate interest in addressing your
  request under Article 6(1)(f), subject to Sections 3.13 and 8. Using the
  consumer AI tools described in Section 3.13 to help draft a reply relies on
  the same interest, subject to your right to ask us not to use them; Section 7
  describes the explicit consent we obtain for senders in the European Economic
  Area, the United Kingdom or Switzerland. Other surviving duties are addressed
  below.
- **Optional provider features.** You choose whether to use cloud cleanup and
  which provider or endpoint to engage. The installed tool constructs requests
  with built-in or configured instructions; connection checks are separate
  requests described in Section 6.2. Feature permission governs what the app
  may send. It does not supply a lawful basis for every person's information
  you submit or determine the provider's legal basis. The permission and
  withdrawal controls remain binding regardless of this role distinction.

- **Surviving rights and obligations.** Lanreal processes GDPR rights requests
  under Article 6(1)(c) to meet Articles 12-22; and necessary claim records under Article 6(1)(f) to establish,
  exercise or defend claims. Article 6(1)(c) applies only where the obligation
  has the Union or Member State legal foundation Article 6(3) requires. Other
  retention obligations require their own appropriate basis. Section 8
  describes retention criteria; retaining information does not erase rights
  or permit unrelated new uses.

**Obligation to provide personal data.** The open-source edition requires no license key,
hostname submission or activation to unlock functionality. Downloading
missing models necessarily exposes ordinary connection information to the
serving hosts. Optional cloud cleanup is not required for local dictation;
disabling it stops that cloud processing without removing local functionality.
No statutory obligation requires you to provide the optional information
described here. Lanreal does not engage in automated decision-making or
profiling producing legal or similarly significant effects under GDPR
Article 22.

You choose whether to contact us for ordinary support. If you withhold
information reasonably needed to understand a request, we may be unable to
assess or complete it. For privacy requests, we seek only information necessary
to locate the relevant records or, where needed, verify your identity. No
account is required. Mandatory legal rights and remedies remain preserved.

## 5. Who We Share It With (Service Providers and Other Recipients)

This list covers the delivery hosts, chosen GitHub visits and reports, and
services holding records described in Section 3. Hugging Face and GitHub
serve public model downloads in every build. Cloudflare hosts the official
installer build's update requests.
Some act as processors on Lanreal's behalf; others determine
their own purposes for limited connection data as identified below. The
list covers those application flows. Section 6 separately identifies optional
cleanup providers and endpoints, the requests they receive and their direct
relationship with you. Changes to this list are communicated
via version bumps to this Privacy Policy (see Section 14), and are
also published in advance via the public provider page and RSS
feed described in Section 14. In accordance with Principle 4.1.3 of
the *Personal Information Protection and Electronic Documents Act*,
Lanreal uses contractual and other means to ensure that each listed recipient
acting as a processor on Lanreal's behalf provides a comparable level of
protection for personal information transferred for processing. Lanreal remains accountable for the protection of personal
information transferred to third-party processors. Lanreal does not use or disclose personal information for purposes other than those described in this Privacy Policy, unless required by law.

Correspondence under Section 3.13 uses Gmail (Google) and Resend for email.
Those services can receive the contact details, messages and attachments
supplied for that purpose. Their policies are at
`https://policies.google.com/privacy` and
`https://resend.com/legal/privacy-policy`. Lanreal remains responsible for its
handling of correspondence and for processors it engages.

OpenAI and Anthropic can receive messages and attachments that Lanreal submits
through its consumer subscriptions to help draft replies, as described in
Section 3.13. They are not engaged as Lanreal's processors for this use and
process that content as independent recipients under their consumer terms and
privacy policies, at `https://openai.com/policies/privacy-policy/` and
`https://www.anthropic.com/legal/privacy`, including any model-improvement use
those terms and Lanreal's account settings permit. Lanreal remains responsible
for its decision to submit the content. The app's BYOK provider permissions
are separate.

1. **Hugging Face, Inc.** (United States) -- public model delivery described in
   Sections 3.3, 3.7 and 3.8.
   Hugging Face and its delivery infrastructure receive the connection data
   described in Sections 3.3, 3.7 and 3.8, including downloads of Stream-1 Mini
   and Gemma for local cleanup. Hugging Face determines its own
   purposes for operating, securing and maintaining its services and acts as
   an independent controller for those uses. Its privacy policy is at
   `https://huggingface.co/privacy`. The installed tool requests files directly
   from the upstream host as described in Section 3.3. International processing
   and host-controlled retention are described in Sections 7 and 8.

2. **GitHub** (United States / Netherlands) -- speech-model backup delivery,
   source access, bug reports and feature requests you choose to open
   in your browser. GitHub receives the model connection data in Section 3.3
   or browser, account and submitted information described in Section 3.12.
   Its privacy statement describes independently controlled service usage
   data and is available at
   `https://github.com/site/privacy`.
   International processing and host-controlled retention are described in
   Sections 7 and 8. GitHub is not engaged as Lanreal's processor for public
   model-download connection records.

3. **Cloudflare, Inc.** (United States) -- R2 hosting for the official
   installer build's update checks and update downloads (Section 3.2).
   Cloudflare receives your IP address, ordinary HTTP headers
   and the requested file, and acts as Lanreal's processor for that delivery.
   Its privacy policy is at `https://www.cloudflare.com/privacypolicy/`.
   Builds from source do not contact it.

The application recipients above are distinct from the optional providers in
Section 6, correspondence processing in Section 3.13 and the legally permitted
disclosures in Sections 12b and 12c. These distinctions do not change our
no-advertising and no-data-broker commitments:
Lanreal does not disclose personal information to advertisers, data
brokers, or social media platforms, and Lanreal does not use
third-party analytics, crash reporting, or error-reporting services
(see Section 9 for the full enumeration of what the Software does
not do).

## 6. Optional Cloud Post-Processing (Bring Your Own Key)

The Software includes an optional AI cleanup feature that can route
transcribed text through a Large Language Model for post-processing
(for example, filler-word removal, spoken-correction handling,
punctuation and number normalization, style adjustment, or custom
formatting). This feature is **disabled by default** in every new
installation. When disabled, Whisperstream does not send text for optional
cleanup. Delivery to your chosen destination remains separate under Section 3.10.

When you enable AI cleanup, it can run on a Large Language Model hosted
**on your own device**, in which case your transcribed text is
processed locally and **does not leave your device**. Alternatively,
under Bring Your Own Key, you may configure the feature to route your
transcribed text to a third-party cloud provider that you select, using
credentials where the selected integration requires them. This Section 6 describes that optional
cloud path: the providers you may select, what is transmitted, and your
relationship with the provider. The on-device option involves no
transmission of transcribed text and is covered by the default-state
promises in Sections 3.4 and 9.

### 6.1 Supported providers

If you enable the optional post-processing feature, you may configure
the Software to route transcribed text to any one of the following
providers. Lanreal does not operate their services as a Lanreal cloud
cleanup service. Your contractual relationship is directly
with the provider you engage.

1. **OpenAI** -- a cloud-based Large Language Model provider. The contracting
   entity and applicable terms depend on your API account and location. See
   `https://openai.com/policies/services-agreement/`, the privacy policy at
   `https://openai.com/policies/privacy-policy/` and the API data controls at
   `https://platform.openai.com/docs/guides/your-data`. Review the agreement,
   data-processing terms and controls applicable to your own account; consumer
   ChatGPT terms do not by themselves describe your API arrangement.

2. **Anthropic, PBC** (Delaware, USA) -- a cloud-based Large Language
   Model provider. Users outside North America may contract with a
   different Anthropic regional entity (for example, Anthropic Ireland
   Limited for users in the European Economic Area). See Anthropic's
   privacy policy at `https://www.anthropic.com/legal/privacy` and
   Anthropic's Commercial Terms of Service at
   `https://www.anthropic.com/legal/commercial-terms` for information
   on how Anthropic handles content submitted to its API.

3. **Google, via the Gemini API.** The contracting entity and data terms depend
   on your account, location and applicable service arrangement. Review the
   Gemini API terms at `https://ai.google.dev/gemini-api/terms` and Google's
   privacy policy at `https://policies.google.com/privacy`. Where Google's
   Unpaid Services data terms apply, do not submit personal, confidential or
   sensitive information; Google may use inputs and outputs for improvement
   and human review. Paid-service treatment and the EEA, UK and Swiss exceptions
   differ. Confirm your applicable terms before sending dictation. Provider
   eligibility restrictions, including age, business/professional purpose,
   region and applicable paid-service requirements, also apply; permission to
   use Whisperstream does not establish eligibility for Gemini.

4. **A self-specified endpoint (Ollama-compatible or other
   OpenAI-compatible endpoint)** -- a Large Language Model server that
   you point the Software at by supplying its URL, together with a
   credential where the endpoint requires one. This covers both an
   Ollama server through its OpenAI-compatible API and other servers speaking
   that API. A localhost address means Whisperstream sends the request to a
   process on your device. That process may run the model locally or forward
   your text and instructions to a cloud service, including Ollama cloud models.
   Processing stays on your device only if the server and selected model operate
   locally without forwarding. Check their configuration, terms and privacy
   information before use. The bundled Local option is a separate feature.
   If you configure a non-local endpoint, Whisperstream sends the request to
   the server at that address, which may also forward it onward. The endpoint
   operator is responsible for its own handling; Lanreal remains responsible
   for its own decisions and the Software's stated safeguards.

### 6.2 What is transmitted

When the optional post-processing feature is enabled, for each
eligible transcript the Software transmits to the provider you
selected:

- **The transcribed text produced by the speech recognition model**,
  as an input to the post-processing request. Ordinary dictation cleanup
  skips transcripts of three words or fewer unless they contain a saved Boost
  term from your Dictionary. Explicit sample previews below
  are separate requests and can include shorter text.
- **An API key or credential, where required,** supplied for the selected
  provider, as an `Authorization` or equivalent request header to authenticate
  the request. An Ollama server may use a separate sign-in managed by Ollama;
  that sign-in is not a credential supplied to Whisperstream.
- **The selected model identifier,** so the provider can route the request.
- **Matching Boost terms from your Dictionary,** when a saved Boost word is
  relevant to the transcript, so the provider can prefer your spelling.
- **Any instruction prompt that you have configured** in the Software
  (for example, a system prompt describing the post-processing style
  you want), as part of the request payload.
- **Standard HTTP headers** automatically attached by the Software's HTTP
  library. The recipient also sees the connection IP address.

The Software does **not** transmit, as part of any optional
post-processing request, any of the following: your audio recording,
your license key, the Whisperstream organization identifier, your
device hostname, your operating system family, your local diagnostic
logs, your in-memory frontend log ring buffer, your usage statistics,
your configuration file, or any other category of information listed
in Section 3.4 as staying on your device. These remain on your device
regardless of whether the optional post-processing feature is enabled.

In addition to the per-transcript transmissions described above,
while the feature is enabled and required configuration is complete, the
Software periodically sends a short connectivity probe to the
provider you selected to check reachability and any required credential, so that the Software can display an
accurate connection status in its settings interface. The probe
transmits the API credential where required, standard HTTP headers and
a short fixed test prompt authored
by Lanreal. The recipient also sees the connection IP address. It does not
transmit any of your transcribed text, any
prompt you have configured, or any other user content. The probe
is covered by the same consent you give when you first enable the
feature under Section 6.5, and the Software will not send a probe
to any provider for which that consent has not been recorded.

### 6.3 Provider response handling

**Explicit previews and saved-text cleanup.** A prompt preview sends the sample
text and instructions you select, the model identifier, applicable credential
and ordinary connection information to your selected provider. Experimental
saved-transcript Re-enhance can send the selected saved raw transcript for
cleanup when that feature is available and you explicitly request it. These
actions do not automatically upload your history. The cloud consent, disabled
feature and Lockdown protections in this Section also apply to these actions.

After the provider processes your request, it returns the
post-processed text to the Software as the response body. For ordinary
dictation, the response is handled locally under the same delivery and
history settings as other transcribed text: it is typed into the focused
window and, if you keep local transcript history enabled, stored encrypted
on your own device as described in Section 3.4. Preview results return to
the tester; import and saved-text cleanup results return to their respective
app workflows. Lanreal does not receive or retain a copy. The Software does not automatically forward the
response to another cloud provider. Delivery, copying or export to your chosen
destination application remains the separate user-directed flow described in
Section 3.4.

### 6.4 Your credentials

The API key or credential you supply for any of the providers listed
in Section 6.1 is stored locally on your device. On Windows it is stored
under `%LOCALAPPDATA%\Whisperstream\` and encrypted at rest using Windows
user-scoped DPAPI (`CryptProtectData`). Windows normally limits decryption
to the protecting user's credentials on that computer; a roaming profile can
permit decryption on another computer. On macOS it is stored as an item in
your login Keychain that is not set to synchronize through iCloud Keychain.
Neither protection isolates the value from processes acting with that user's
authority.

The stored credential is transmitted only to the provider you
selected, only over HTTPS with certificate verification enabled, and
only as a request header on the post-processing requests described in
Section 6.2. The Software does not transmit the credential to Lanreal,
to any service provider or recipient listed in Section 5, or to any recipient
other than the provider you selected. Lanreal does not receive, store,
or have any ability to recover your credential.

### 6.5 How to enable, disable, and delete

- **Enabling.** The optional post-processing feature is disabled by
  default in every new installation. To enable it, you must
  explicitly select a provider or endpoint and supply a valid credential
  where that integration requires one, through the Software's settings interface. Before
  the feature begins transmitting transcribed text, the Software
  will display a blocking consent dialog that identifies the
  provider you selected, describes what data will leave your
  device, links to this Privacy Policy and to the provider's own
  privacy policy, and requires an affirmative click to continue.
  The Software will not begin transmitting transcribed text to any
  provider until you have completed all of those steps and
  confirmed the blocking consent dialog.
- **Disabling.** To disable the feature, toggle it off in the same
  settings interface. After you disable it, the Software will not
  transmit any further transcribed text to any provider, and will
  resume the default behaviour described in Sections 3.4 and 9 of
  this Privacy Policy.
- **Deleting your credential.** To delete a stored credential, use
  the "remove key" control in the same settings interface. This
  removes the protected credential (the encrypted Windows copy or the
  macOS Keychain item) in its entirety.
- **Lockdown Mode override.** While Lockdown Mode (Section 3.6) is on,
  all of the cloud providers and self-specified endpoints listed in
  Section 6.1 are disabled and AI cleanup is forced to the on-device
  model, so no transcribed text leaves your device through the cleanup
  pipeline, regardless of any provider you may have configured.

### 6.6 Relationship to Section 5 (service providers and other recipients)

You choose whether to use cloud cleanup, select and engage the provider or
endpoint, and supply its required credentials. Its agreement governs its service
to you. It may process content on your or your organization's behalf and may
separately process information for its own purposes under its actual terms.

Whisperstream constructs requests on your device, supplies named-provider
destination mappings and uses built-in or configured instructions. Connection
checks send the separate test request described in Section 6.2. Requests travel
directly from your device to the selected destination, without a Lanreal server
copy. Lanreal does not engage your provider to operate a Lanreal cloud
cleanup service. We therefore describe it as your provider, not our processor
for that service. These facts do not exclude any responsibility law assigns to
Lanreal for its own decisions or information handling. Lanreal remains responsible
for its privacy commitments and the Software's stated safeguards.

Your feature permission does not authorize unrelated content uses, sale or
cross-context advertising. Review your provider's terms, privacy information,
retention and processing locations before use. You must have the right to submit
the content; an organization's requirements also apply when acting for it.

## 7. International Data Transfers

The hosts and services described in Section 5 can process personal information
in Canada, the United States and other countries. The practices below distinguish
direct public-host connections from information Lanreal processes or discloses
for its own purposes. Applicable international-transfer duties follow those
actual processing roles under GDPR Articles 44-49 and the relevant UK, Swiss and
Canadian rules.

- **Direct public-model downloads.** The installed tool requests model files
  directly from the upstream host when you request a missing model. Hugging Face
  and GitHub collect the connection records described in Section 3.3 for their
  own service purposes. Lanreal does not engage them to process customer records
  on its behalf. Their delivery infrastructure can process those records in the
  United States and other countries. Their policies linked in Section 5 describe
  international processing, applicable safeguards and ways to contact them.
- **Hugging Face, Inc. (United States).** Hugging Face identifies a French
  establishment and international infrastructure in its privacy information;
  that does not mean every download remains in the European Economic Area.
  Review `https://huggingface.co/privacy` for its independently controlled
  processing and rights routes.
- **GitHub (United States / Netherlands).** Public release downloads, browser
  visits and voluntary reports follow GitHub's privacy statement at
  `https://github.com/site/privacy`. GitHub describes international safeguards,
  including participation in the EU-U.S. Data Privacy Framework, its UK
  Extension and the Swiss-U.S. Data Privacy Framework for covered data. Those
  safeguards apply within their stated scope; they do not describe every
  recipient or all processing by Lanreal. Public reports also disclose the
  account and submitted information described in Section 3.12.
- **Cloudflare, Inc. (United States).** In the official installer build, update
  requests (Section 3.2) reach Cloudflare's global
  network, which can process connection records in the United States and other
  countries. Cloudflare acts as Lanreal's processor, and Lanreal remains
  responsible for applicable transfer duties for that processing.
- **Email.** Google and Resend can process correspondence and supplied
  attachments in the United States and other countries under the applicable
  account terms, settings and privacy policies linked in Section 5. Their
  service disclosures describe processing locations and available safeguards.
  Lanreal remains responsible for applicable transfer duties for its handling
  of that information.
- **Consumer AI drafting tools.** OpenAI and Anthropic can process messages
  Lanreal submits through its consumer subscriptions (Section 3.13) in the
  United States and other countries. No Standard Contractual Clauses or other
  safeguard arranged by Lanreal covers this use, and the providers may use the
  content under their own consumer terms. For senders in the European Economic
  Area, the United Kingdom or Switzerland, Lanreal therefore submits a message
  to these tools only with the sender's explicit consent, given after being
  informed of these risks (GDPR Article 49(1)(a) and the corresponding UK and
  Swiss rules). Without that consent, the message is handled without them.

**Transfers to Canada.** The European Commission's Canadian adequacy decision
covers recipients subject to Canada's Personal Information Protection and
Electronic Documents Act. Lanreal relies on that decision for EEA transfers to
it that fall within that scope. It does not cover onward transfers from Lanreal
to other countries; those require their own applicable transfer assessment.

If you are located in the European Economic Area, the United Kingdom,
or Switzerland and wish to receive further information about these
transfer mechanisms, including copies of the relevant Standard
Contractual Clauses where available, please contact Lanreal at
`support@whisperstream.io`.

**Foreign law enforcement access.** Because personal information may be
transferred to service providers or other recipients in the United States
for processing (as described in Section 5), that information may be
accessible to the courts, law enforcement agencies, and national security
authorities of the United States under the laws of that jurisdiction.

**Optional provider features.** A remote provider receives the requests
described in Section 6. Processing locations and onward transfers depend on the
provider, account and endpoint, not just its headquarters. Your provider's
agreement and disclosures describe its safeguards; Lanreal does not arrange
those contracts on your behalf.

International-transfer duties follow actual processing roles. An individual's
direct submission of their own information differs from an organization's
disclosure of other people's information. Organizations must assess transfer
requirements for their use. Any duties applicable to Lanreal's own processing
remain its responsibility. Ordinary EULA acceptance or feature permission alone
does not establish an international-transfer safeguard or explicit consent to
a derogation.

## 8. How Long We Keep It (Retention)

**Necessary retention and surviving duties.** Lanreal reviews retained personal
information and deletes or anonymizes information no longer needed for a
disclosed purpose or applicable legal obligation. Necessary rights-request and
claim records may remain for those purposes. Privacy
rights, security safeguards and breach-notification duties continue while we
retain personal information. Contact `support@whisperstream.io` about retained
information.

We retain correspondence and request-handling records under Section 3.13 for
as long as necessary to address the request and document any remaining rights
or claim obligation, taking account of applicable limitation periods,
legal retention requirements and prior applicable commitments. We delete or
anonymize information no longer necessary for those purposes. Resolving a
request does not itself mean all related records or provider backups are
immediately deleted. Email services can retain supplied information under
their applicable account terms, settings and privacy policies linked in
Section 5; processor-held copies remain subject to our responsibilities and
instructions described below. OpenAI and Anthropic retain content submitted
through Lanreal's consumer subscriptions under their own terms and Lanreal's
account settings. Lanreal can delete those conversations from its accounts,
after which the providers' retention rules apply; content already used to
improve a model may not be removable from it.

Retention depends on who controls the information. The periods and deletion
rules below describe local Software data and information for which Lanreal is
responsible, including information processed on our behalf by service providers.
Using a service provider does not remove that responsibility. We require
appropriate retention and return or deletion through applicable agreements and
instructions, subject to lawful retention requirements. Where a recipient
separately controls information for its own purposes, its privacy policy describes
its retention practices. We distinguish those roles when handling your request.

**Retention for information the Software controls directly:**

- **Audio recordings:** not retained by Lanreal, and never
  transmitted off your device. Microphone audio is held in
  random-access memory while each capture is transcribed. If you keep
  audio saving enabled in the local transcript history (on by default
  once history is on, and separately disableable), a copy of the recording is also stored,
  encrypted, on your own device, and is removed on the same schedule
  as the rest of your history (see "Local transcript history" below).
- **Transcribed text:** not retained by Lanreal. The output of the
  speech recognition model is typed into the focused window. If you
  keep the local transcript history enabled (on by default on Windows;
  off on macOS until you turn it on), the
  transcribed text is also stored, encrypted, on your own device and
  removed on the schedule you set (see "Local transcript history"
  below). Section 6 describes optional provider requests. Ordinary dictation
  skips cloud cleanup for transcripts of three words or fewer unless they
  contain a saved Boost term from your Dictionary; explicit
  previews and selected saved-text cleanup are separate requests described in
  Section 6.3. Lanreal does not retain a copy. A provider may retain submitted
  content after the request ends under its terms, account settings and privacy
  policy (see Section 6.1).
- **Local transcript history (your transcribed text, encrypted waveform
  peaks, and, optionally, your audio):** retained on your own device only,
  encrypted at rest (AES-256-GCM with the data key protected by Windows
  DPAPI or the macOS login Keychain), and never transmitted to Lanreal. You control how
  long it is kept: a retention window of 1, 7, or 30 days, or forever, with
  7 days as the default, removes older entries, and you may delete any
  individual entry or clear the entire history at any time. Retention and
  orphan-audio cleanup run at startup, after live and imported saves, after
  a retention change, and after a successful Lockdown unlock. Cleanup that
  requires the history key is deferred while the history is locked and
  retried after unlock. Deletion uses ordinary SQLite deletion and file
  unlinking, not secure disk overwrite. A failed file deletion may be
  retried by a later cleanup; database free pages or journals, filesystem
  remnants, backups, shadow copies, and exported copies remain outside the
  Software's deletion control. If the history cannot be decrypted on your
  current user account (for example, because its protected data key cannot
  be opened in the current Windows or macOS environment), it is not readable
  and no new entries are written to it. On macOS, the protected key is a
  login Keychain item labelled "Whisperstream transcript encryption key";
  deleting the app or its data directory does not remove it, and you can
  delete it in Keychain Access.
- **Local diagnostic logs:** the active file and up to three 2 MiB backups
  are retained locally by size-based rotation in ordinary use, voluntary
  Lockdown, and administrator-managed Lockdown. The supplemental Lockdown
  diagnostic file retains at most 256 events and 128 KiB. These files are
  never transmitted automatically. You may delete the local log directory at
  any time.
- **Lockdown access log (Section 3.4):** while Lockdown Mode is effective,
  the active content-free file and up to five 2 MiB backups are retained
  locally by size-based rotation. The Software does not assign it a
  time-based purge period. It is never transmitted or included in the support
  archive. You may delete it with the local log directory.
- **Frontend diagnostic buffers:** each application window keeps at most
  500 entries, 2 KiB per entry, and 128 KiB total in memory; the backend
  keeps at most 128 KiB of re-sanitized frontend entries across windows.
  Ring eviction or process/window exit releases that state. Lockdown
  transitions do not clear the rings.
- **Local usage statistics and configuration:** retained locally on your
  device until you delete the relevant records or the application data
  directory (`%LOCALAPPDATA%\Whisperstream\` on Windows,
  `~/Library/Application Support/Whisperstream/` on macOS). Uninstalling
  the Software does not automatically delete them.
- **Legal-acceptance state and other local edition records (Section 3.9):**
  retained in the application data directory and, for records another
  edition left on Windows, the current user's Windows registry, until you
  remove those records. Uninstalling does not necessarily remove them.
  Licensing, trial or activation records left by another edition may persist
  across reinstallations, but this edition does not read them or use them to
  restrict functionality. Exact EULA acceptance events remain in the
  append-only local ledger so accepting updated terms does not erase prior
  evidence.
- **Agent Speak interaction content (Section 3.11):** responses, questions,
  permissions, display state and drafts remain in bounded process memory.
  Temporary hook interactions expire; managed-session response/draft state can
  persist while the session remains registered. Applicable disconnect,
  disablement, Lockdown, acceptance revocation or shutdown clears that state.
  Content and derived fingerprints are excluded from diagnostics and exports.
  Dictated replies follow encrypted transcript-history retention when enabled.
  The independent coding agent's storage follows its own practices.
- **Credentials you supply for the optional cloud post-processing
  feature (Section 6):** retained locally on your device until you
  remove the credential through the Software's settings interface
  (see Section 6.5). On Windows it is DPAPI-encrypted in the application
  data directory and is also removed if you uninstall the Software and
  delete the `%LOCALAPPDATA%\Whisperstream\` directory. On macOS it is a
  login Keychain item labelled "Whisperstream AI provider API key";
  deleting the app or its data directory does not remove it, and you can
  delete it in Keychain Access. Lanreal does not
  retain, store, or have any visibility into this credential.
- **Diagnostic archives and logs:** the support ZIP is assembled in backend
  memory and streamed to your selected download destination, with no
  app-managed ZIP retention. The ZIP may contain recent sanitized backend and
  frontend diagnostics, bounded system information, and supplemental
  closed-schema Lockdown events. It excludes the separate Lockdown access log,
  transcript history, audio, imported source files, and Agent Speak stores.
  Your downloaded copy remains under your control. The Software never uploads
  the ZIP automatically; Lanreal receives it only if you separately choose to
  send it.
  Logs you voluntarily send to Lanreal with a bug report follow the retention
  criteria for correspondence above, including any earlier applicable
  commitment requiring shorter retention. They are deleted or anonymized when
  no longer needed for those permitted purposes. The protected credential you supply for the optional cloud
  post-processing feature (Section 6.4) is not part of the bug-report
  log export, so it is not inadvertently transmitted when you submit a
  bug report.
- **Star prompt state (Section 3.5):** a local `review_state.json` record of
  session counts, usage dates and prompt outcomes, retained until you delete
  the application data directory. It is never transmitted.

**Third-party retention: processor-held and independently controlled records:**

- **GitHub connection data and voluntary public reports:** GitHub controls retention
  under its privacy statement linked in Section 5; the Software does not
  control or delete those host-held logs. Closing a public issue does not delete
  its content or attachments. Lanreal-held copies remain subject to the purpose
  limits above; copies made by third parties are outside Lanreal's control.
  Contact Lanreal under Section 10 about a report it controls, or GitHub about
  information GitHub separately controls.
- **Hugging Face public-download connection logs:** Hugging Face controls
  retention under `https://huggingface.co/privacy`. Its policy describes
  retention according to service, security and legal needs. The Software does
  not control or delete those host-held records, and Lanreal promises no fixed
  public-download log period. Retention statements for Inference Endpoints
  describe a different service.
- **Cloudflare request records (official installer build):** Cloudflare
  retains connection records for update requests under
  its service terms. Lanreal uses any it can access only to operate and secure
  delivery.

**Retention by optional cloud post-processing providers (Section 6).**
If you have enabled the optional cloud post-processing feature and
selected a cloud provider, your transcribed text and any instruction
prompt you have configured are submitted to that provider as part of
each eligible request. How long the provider retains that submitted
content, whether it is used to train the provider's models, and
whether it is logged for abuse detection are governed entirely by
the provider's own terms of service and privacy policy, which vary by
provider and by account tier (for example, some providers offer a
"zero-data-retention" enterprise tier that differs from their default
consumer retention). Lanreal has no visibility into and no control
over retention decisions at the provider, and Lanreal does not make
representations about them. Please review the provider's published
policies (see the links in Section 6.1) before enabling the feature.

## 9. What We Don't Do

This section enumerates categories of information that the Software
never collects, never transmits, and never retains. It is intended as
an explicit companion to Section 3, so that you can see not just what
the Software does, but also what it does not do.

- **No audio recordings leave your device.** Microphone audio is
  processed by the speech recognition model on your device and is never
  uploaded to any server for any reason. If you keep audio saving
  enabled in the local transcript history (Section 3.4), a copy of the
  recording is stored encrypted on your own device only; it still
  never leaves your device.
- **By default, no transcribed text leaves your device.** In the
  default configuration of the Software, the output of the speech
  recognition model is typed into the focused window, and the Software
  does not upload transcriptions to any server. The transcribed text
  may be stored in the local, encrypted transcript history on your own
  device (Section 3.4). It is not automatically uploaded. Section 6 describes
  explicit provider requests, including shorter preview samples and selected
  saved-text cleanup. Ordinary dictation skips cloud cleanup for transcripts
  of three words or fewer unless they contain a saved Boost term from your
  Dictionary. Remote requests go only to your selected provider
  over HTTPS; the provider may retain submitted content after the request ends.
  Explicit copying, export and destinations are separate user-directed actions. No transcribed text is transmitted to
  Lanreal or to the service providers listed in Section 5 as part of
  Whisperstream's transcription or cloud-cleanup pipeline. Text you
  deliberately submit to a destination application, Codex or Claude Code,
  or a GitHub issue is controlled by that destination.
- **No passive transmission of microphone input, typing, keystrokes, or clipboard contents.** The Software does not intercept or transmit your
  keyboard or clipboard activity.
- **No telemetry, no analytics, error reporting, crash reporting, no
  usage tracking, and no feature-flag service.** The Software does
  not integrate Sentry, PostHog, Mixpanel, Google Analytics,
  Plausible, or any comparable service. No automatic event streams,
  per-action tracking, or behavioural profiling are sent to any
  server. In the open-source edition, model-download connection data leaves your device
  through the model-host routes in Sections 3.3, 3.7 and 3.8. Optional cloud
  post-processing is separately controlled under Section 6. Built from source,
  the Software makes no application-update requests; the
  official installer build makes only those described in Section 3.2.
  External help is described in Section 3.12. This edition sends no licensing,
  rating, review or feedback requests. Text you deliver
  to other applications and Agent Speak's local assistant interactions retain
  the boundaries described in Sections 3.10 and 3.11.
  Communication with the local backend uses only the loopback
  interface and does not leave your device.
  There is no "opt out" for telemetry because there is no telemetry
  to opt out of; model downloads and user-initiated flows follow the controls
  described in Section 3. The content-free local access log described
  in Section 3.4 is likewise stored only on your device and is never
  transmitted.
- **No advertising identifiers.** The Software does not collect,
  generate, or transmit any advertising identifier (such as IDFA,
  AAID, or similar).
- **No local network scanning.** The Software does not scan your
  local network, enumerate devices on it, or probe for other
  services.
- **No persistent loopback-accessible endpoints beyond the local
  backend process.** The local backend process binds exclusively to
  the loopback interface (`127.0.0.1`) so that no other device on
  your network can reach it, and it exposes no outbound telemetry.
- **No sale of personal information.** Lanreal does not sell or rent
  personal information. See Section 13 for the CCPA-specific
  disclosure on sale and sharing.
- **No profiling or automated decision-making.** The Software does
  not profile you, score you, rank you, or make any automated
  decision that produces legal or similarly significant effects
  within the meaning of GDPR Article 22.
- **No cookies or tracking technologies.** The Software does not use cookies, web beacons, pixel tags, local storage tokens, browser fingerprinting, or any other tracking technology to identify you, track your behaviour, or build a profile about you. The local communication between the Software's frontend and backend processes (over the loopback interface) does not use persistent cookies or session identifiers.

## 10. Your Rights

Depending on where you live, you may have one or more of the rights
listed in this section. Lanreal extends all of these rights to every
Whisperstream user, regardless of jurisdiction, so that you do not
have to prove residency in order to exercise a right.

**Rights summary.** The most commonly exercised rights, with the
statutory basis for each, are:

- **Right to know** what personal information Lanreal holds about
  you and how we use it (GDPR Article 15 / CCPA Section 1798.110 /
  Canadian federal privacy law Principle 4.9).
- **Right to delete or erase** personal information Lanreal holds
  about you (GDPR Article 17 / CCPA Section 1798.105).
- **Right to correct** inaccurate personal information (GDPR
  Article 16 / CCPA Section 1798.106 / Canadian federal privacy
  law Principle 4.9.5).
- **Right to data portability** in a structured, commonly-used,
  machine-readable format (GDPR Article 20).
- **Right to withdraw consent** where consent is the applicable basis
  (GDPR Article 7(3)). The app permission for optional cloud
  post-processing can always be withdrawn as described in Section 6.
- **Right to complain** to a privacy regulator (GDPR Article 77,
  CCPA, Canadian federal privacy law; see Section 15 for the
  supervisory authorities).

To exercise any of these rights, email Lanreal's Privacy Officer at
`support@whisperstream.io`. There is no charge, no account required,
and no in-app rights-request form. Lanreal will respond within the applicable
Canadian 30-day, GDPR one-calendar-month and California 45-calendar-day periods,
subject to the shorter commitments and permitted extensions described in the
timelines at the end of this Section 10.

**Rights under the European Union General Data Protection Regulation
(GDPR Articles 15 to 22):**

- **Right of access (Article 15):** you may ask Lanreal to confirm
  whether we process personal information about you and, if so, to
  provide a copy of that information.
- **Right to rectification (Article 16):** you may ask Lanreal to
  correct any inaccurate personal information we hold about you.
- **Right to erasure (Article 17):** you may ask Lanreal to delete
  personal information we hold about you, subject to the statutory
  exceptions in Article 17(3).
- **Right to restriction of processing (Article 18):** you may ask
  Lanreal to restrict the processing of your personal information in
  the circumstances set out in Article 18(1).
- **Right to data portability (Article 20):** you may ask Lanreal to
  provide your personal information in a structured, commonly used,
  machine-readable format where the processing is based on consent
  under Article 6(1)(a) or contract under Article 6(1)(b) and is carried
  out by automated means.
- **Right to object (Article 21):** you may object at any time, on
  grounds relating to your particular situation, to processing of
  your personal information that is based on Article 6(1)(f) (our
  legitimate interests). If you believe that any processing by Lanreal that is based on
  Article 6(1)(f) affects you, you may object on grounds relating to
  your particular situation, and Lanreal will cease the processing
  unless we demonstrate compelling legitimate grounds that override
  your interests, rights, and freedoms.
- **Rights in relation to automated decision-making (Article 22):**
  not applicable. Lanreal does not engage in automated
  decision-making that produces legal or similarly significant
  effects within the meaning of Article 22.
- **Right to withdraw consent (Article 7(3)):** applies where consent is the
  applicable GDPR basis. Independently of processing-role classification, the
  app always preserves the cloud-permission withdrawal control: disable the
  feature through the Software's settings interface as described in
  Section 6.5, which stops all further transmission of transcribed
  text to the provider. Withdrawal of consent does not affect the lawfulness of processing already
  performed under your consent before withdrawal. Canadian consent and withdrawal
  requirements are separately explained below.

**Rights under the California Consumer Privacy Act (CCPA):**

- **Right to know** what categories of personal information we have
  collected about you, the sources from which we collected it, the
  business or commercial purposes for collecting it, and the
  categories of third parties with whom we shared it. You may also request
  the specific pieces of personal information we have collected about you.
- **Right to delete** personal information we collected from you,
  subject to the statutory exceptions in Cal. Civ. Code Section
  1798.105(d).
- **Right to correct** inaccurate personal information we maintain
  about you.
- **Right to limit use of sensitive personal information:** Lanreal does
  not request or use sensitive personal information for a purpose that
  triggers this right. A voluntary support message or GitHub issue could
  nevertheless contain sensitive personal information despite the instruction
  in Section 3.12; Lanreal handles such incidental content as described there.
- **Right to opt out of the sale or sharing of personal information:**
  not applicable. Lanreal does not sell or share personal
  information for cross-context behavioural advertising, as
  described in Section 13 below. The disclosure that this right is
  not applicable is nonetheless provided.
- **Right to non-discrimination** for exercising any CCPA right.
  Lanreal will not deny you the Software, charge you a different
  price, or provide a different level of quality because you
  exercised a right under the CCPA.

**Rights under Canadian federal privacy law:**

GDPR legal bases do not replace consent where Canadian law requires it. We
explain material new purposes or disclosures before they begin and obtain the
form of consent required in the circumstances. Built from source, the Software
makes no application-update requests; the official installer build installs an
update only after you accept it (Section 3.2). You can disable optional cloud
cleanup. Declining a missing-model download prevents installation of that model; installed compatible models remain
usable. You may contact our Privacy Officer to withdraw consent to processing
under our control, subject to legal or contractual restrictions that we explain.

- **Right of access** to personal information Lanreal holds about
  you.
- **Right to correction** of inaccurate personal information.
- **Right to challenge compliance** with this Privacy Policy and
  with Canadian federal privacy law generally. Challenges are
  directed to the Privacy Officer in the first instance (see
  Section 15).

**How to exercise your rights.** To exercise any of the rights above,
email Lanreal's Privacy Officer at `support@whisperstream.io` with a
description of your request and enough information for us to verify
your identity and locate your personal information (for example, the
email address or GitHub account you used to contact us). Lanreal does not
operate an in-app rights-request button in this version of the
Software; the email channel above is the designated rights mechanism.

**Verification.** Because Lanreal does not maintain a user account
database, identity verification for most requests will consist of
confirming that the email address or account you use to contact us
matches a record we can locate, or confirming enough details about
your interactions with us that we can associate your request with
specific personal information we hold.

**Accuracy and unresolved challenges.** We take reasonable steps to keep
personal information under our control sufficiently accurate, complete and current
for its purpose. We do not routinely refresh historical information without a
continuing purpose. If an accuracy challenge cannot be resolved to your
satisfaction, we record the substance of the challenge alongside the information.

**Recipient follow-through.** Where GDPR Article 19 applies, we communicate
applicable corrections, erasures and restrictions to each recipient to whom
we disclosed the personal information, unless doing so is impossible or requires
disproportionate effort. On request, we identify those recipients to you.

**Response timeline.** Lanreal will acknowledge your request within a
reasonable period and will respond substantively within **30 days**
of receipt, or within one calendar month if that is earlier for a GDPR request.
Access and correction requests are free. GDPR Article 12(3) permits up to two
further calendar months where necessary because of complexity or the number of
requests, with reasons given within the first calendar month. Lanreal also
preserves its existing shorter 30-day notification and 60-additional-day
commitments where they apply. Any Canadian extension must separately satisfy
Canadian law and be explained within the initial 30 days.

**CCPA response timeline.** For California requests to know, delete or correct,
Lanreal confirms receipt within 10 business days and explains verification,
handling and the expected response time. The substantive response is due within
45 calendar days, with up to 45 additional days where permitted and explained
within the initial period. This does not lengthen an earlier or shorter response
commitment that also applies to your request.

**If Lanreal does not take action.** We explain the reasons and your complaint
and judicial remedies without delay, within 30 days of receipt or, for a GDPR
request, one calendar month if earlier.

**Authorized agents.** California residents may use an authorized
agent to submit a rights request on their behalf. If you do so, we
may require the agent to provide written authorization from you and
to verify their own identity.

## 11. Children's Privacy

The Software is not directed to children. Lanreal does not knowingly
collect personal information from children under the age of 13 in
the United States (consistent with the Children's Online Privacy
Protection Act, or COPPA), or from children under the age of 16 in
the European Economic Area (consistent with GDPR Article 8). If you
are a parent or guardian and you believe your child has provided
personal information to Lanreal through the Software, please contact
us at `support@whisperstream.io` and we will take reasonable steps to
delete that information. The Software does not implement age verification because it does not require account creation, does not collect age or date-of-birth information, and is not directed to children.

## 12. Security Safeguards

Lanreal implements security safeguards proportional to the
sensitivity of the personal information the Software processes.
This source edition limits model-download requests to ordinary connection
information and keeps inference local except for separately enabled cloud
cleanup. In particular:

- The Software requires no Lanreal account registration or activation.
- External network requests initiated by the Software use HTTPS with the
  certificate-verification behaviour of the
  underlying HTTP library. The Software does not disable
  certificate verification.
- Downloaded model artifacts are verified against pinned cryptographic
  checksums before installation.
- The local backend process binds exclusively to `127.0.0.1` on a
  local port, so that no other device on your network
  can reach it.
- Local configuration, logs, and usage statistics are stored in the
  application data directory of your user account with the access
  controls your operating system provides.
- The local transcript history described in Section 3.4 separately
  encrypts the raw transcript, enhanced transcript, waveform peaks, and,
  if enabled, audio using AES-256-GCM. The AES data key is itself sealed
  using Windows user-scoped DPAPI or stored in the macOS login Keychain.
  Windows normally limits decryption to the protecting user's credentials on
  that computer; a roaming profile can permit decryption on another computer.
  The macOS Keychain item is not set to synchronize through iCloud Keychain.
  Neither protection isolates the key from processes acting with that user's
  authority. Lightweight index metadata
  needed for lookup and playback remains unencrypted. Existing plaintext
  waveform peaks from an older database schema are transactionally migrated
  to separately authenticated ciphertext when the data key is available;
  if the history is locked, migration is deferred and retried after unlock.
  The replacement of a legacy database value uses ordinary SQLite updating
  and is not represented as secure erasure of storage remnants.
- When Lockdown Mode (Section 3.6) is on, the Software adds a password
  layer to the transcript history. A key derived from a password you
  choose (using the scrypt key-derivation function) and a separate key
  derived from a one-time recovery code each wrap the transcript
  encryption key using AES-256-GCM, in place of the operating-system
  key protection described above, while the transcript content remains
  encrypted at rest as described above. The
  transcript history locks automatically after an idle period you set,
  and repeated failed unlock attempts trigger escalating delays.
  Lanreal never receives your password or your recovery code.
- For the optional cloud post-processing feature described in
  Section 6, credentials (API keys) that you supply are protected at
  rest. On Windows they are encrypted using user-scoped DPAPI
  (`CryptProtectData`) and stored under
  `%LOCALAPPDATA%\Whisperstream\`. Windows normally limits decryption to
  the protecting user's credentials on that computer; a roaming profile can
  permit decryption on another computer. On macOS they are stored in your
  login Keychain and are not set to synchronize through iCloud Keychain.
  Neither protection isolates credentials from processes acting with that
  user's authority. The protected credential has the access controls your
  operating system provides, and is not part of the bug-report log
  export described in Section 8, so it is not inadvertently
  transmitted when you submit a bug report to Lanreal.
- When the optional cloud post-processing feature is enabled, each
  request to the provider you selected travels over HTTPS with
  certificate verification enabled. Your credential is transmitted
  only as a request header on those requests, and only to the
  provider you selected. The Software does not transmit the
  credential to Lanreal, to any service provider or recipient listed in Section 5,
  or to any other recipient.

No security program can guarantee absolute protection against all
threats. If you become aware of a security issue affecting the
Software, please contact Lanreal at `support@whisperstream.io`.

## 12a. Data Breach Notification

Lanreal assesses the reporting, individual-notification and record-keeping
requirements of each applicable law separately. One jurisdiction's threshold
does not limit another's requirements.

(a) Under Canadian federal privacy law, where a breach involving information
under Lanreal's control creates a real risk of significant harm, we report to
the Office of the Privacy Commissioner of Canada and notify affected individuals
as soon as feasible after determining the breach occurred, in accordance with
section 10.1 and the Breach of Security Safeguards Regulations (SOR/2018-64).

(b) We use available appropriate direct contact details for individual notice.
Where the Regulations permit indirect notice, we use a public communication or
similar measure reasonably expected to reach affected people. This can include
a prominent website notice and other appropriate channels; absence of a direct
email address does not by itself determine the notification method.

(c) Where GDPR Article 33 applies, we notify the competent supervisory authority
without undue delay and, where feasible, within 72 hours of awareness, unless
the breach is unlikely to result in a risk to individuals' rights and freedoms.
Where Article 34 requires individual notice for likely high risk, we notify
affected people without undue delay. Existing stronger notification commitments
remain unaffected.

(d) California and other applicable breach-notification laws are assessed and
met independently, including their timing and content requirements.

Under section 10.3 of the Canadian Act and regulation 6, Lanreal keeps a record
of every breach involving personal information under its control for at least
24 months after determining it occurred, and makes the record available to the
Office of the Privacy Commissioner of Canada on request.

## 12b. Business Transfers

If Lanreal Technologies Inc. is involved in a merger, acquisition, reorganization, sale of assets, or bankruptcy, personal information we hold may be transferred as part of that transaction. If such a transfer occurs, the acquiring entity's use of your personal information will remain subject to the commitments made in this Privacy Policy. We will notify you of any such transfer and of any changes to the applicable privacy terms by updating this Privacy Policy (see Section 14) and, where feasible, by notice on the Whisperstream website. Where required by applicable law, we will obtain your consent before transferring your personal information to the acquiring entity.

## 12c. Law Enforcement and Government Requests

Lanreal may disclose personal information to law enforcement, regulatory authorities, courts, or other governmental bodies where we are legally required to do so (for example, in response to a valid subpoena, warrant, or court order), or where applicable law permits disclosure and it is necessary to protect a person's life, health or security. We satisfy the conditions of the applicable legal permission, including any required notification. Lanreal will attempt to notify you of any such disclosure before it occurs, unless notification is prohibited by law or court order. Lanreal does not voluntarily provide personal information to any government surveillance programme.

## 12d. Survival

Sections 8 (How Long We Keep It), 10 (Your Rights), 11 (Children's Privacy), 12 (Security Safeguards), 12a (Data Breach Notification), 12b (Business Transfers), 12c (Law Enforcement and Government Requests), 13 (CCPA Notice for California Residents), 14 (Changes to This Policy), 15 (How to File a Complaint), and 16 (Contact Us) of this Privacy Policy, and any obligations arising from a data breach that occurred before termination, survive after you stop using or uninstall the Software for as long as Lanreal retains any personal information about you.

## 13. CCPA Notice for California Residents

This section describes personal information handled in the preceding twelve
months under the California Consumer Privacy Act and California Privacy Rights
Act (the "CCPA"). We offer the California rights in Section 10 regardless of
whether statutory business thresholds apply.

**Categories, sources and purposes.** The categories below correspond to
Cal. Civ. Code Section 1798.140. Current app flows are described in Section 3.

- **Identifiers:** IP addresses and ordinary connection information for model
  downloads and, in the official installer build, update requests; and contact/account details in voluntary
  reports and correspondence. Sources are the device/network connection and the
  person submitting information.
- **Personal information under Section 1798.80(e):** names and similar contact
  details supplied in submissions or correspondence. Other information
  in this category is not requested but can appear in a voluntary message.
- **Characteristics of protected classifications:** not requested; they can appear in information
  someone chooses to submit. The mere availability of a freeform field does
  not mean every category was received.
- **Commercial information:** not requested; it can appear in a voluntary
  message or attachment.
- **Biometric information:** Lanreal does not receive voiceprints or biometric
  identifiers. On-device speech recognition transcribes audio without using it
  to identify the speaker; local audio and history remain under your control.
- **Internet or other electronic network activity information:** the connections for delivery, chosen external
  visits and submissions described in Section 3. Optional cloud cleanup sends
  requests directly to the provider you choose under Section 6.
- **Geolocation data:** the app requests no location data. Network recipients can
  infer an approximate location from an IP address under their own policies.
- **Audio, electronic, visual, thermal, olfactory or similar information:** messages and attachments
  voluntarily supplied to Lanreal or GitHub. Local dictation audio
  and history are not automatically sent to Lanreal.
- **Professional or employment-related information:** not requested; it can
  appear in a voluntary message or attachment.
- **Education information:** not requested; it can appear in a voluntary
  message or attachment.
- **Inferences:** Lanreal does not create profiles of your characteristics,
  preferences or behaviour from these records.
- **Sensitive personal information:** not requested for ordinary support or
  reports. Incidental sensitive content is handled under Section 3.12 and
  applicable legal requirements.

We use these records for requested software delivery, responding to voluntary
reports and support, handling privacy requests, and resolving claims under
Section 4. Retention criteria for
each category, including local data, are in Section 8. The local transcript
history, audio and access log are not automatically uploaded. If you separately
send content to Lanreal, it is handled as correspondence under Section 3.13.

**Recipients and disclosures for business purposes.** The following disclosures
apply to the records and purposes described in Sections 3-5:

- Hugging Face and GitHub independently receive connection information
  for direct public-model delivery. GitHub also receives chosen browser visits
  and voluntary public reports, including the account details and content supplied.
- Cloudflare receives connection information as Lanreal's service provider for
  the official installer build's update requests.
- Google (Gmail) and Resend can receive identifiers and electronic message and
  attachment content supplied for email handling under Section 3.13.
  Additional categories depend on what you choose to include.
- OpenAI and Anthropic can receive identifiers and message and attachment
  content that Lanreal submits through consumer subscriptions to help draft
  replies (Section 3.13). They receive it as third parties under their own
  terms, not as Lanreal's service providers or contractors. Lanreal pays for
  these subscriptions and receives no payment or other valuable consideration
  for the content. You can ask us not to use these tools for your message.
- Optional cleanup providers receive the user-directed requests in Section 6;
  those are separate from Lanreal's handling of correspondence.

**Sale, sharing and sensitive information.** Lanreal does not sell personal
information for money or other valuable consideration or share it for
cross-context behavioural advertising, and has not done so in the preceding
twelve months. We do not knowingly sell or share information about consumers
under 16. These statements do not imply age verification. There is no
"Do Not Sell or Share My Personal Information" link for these practices.
Lanreal does not request or use sensitive personal information for a purpose
that triggers the CCPA right to limit.

**Requests.** Email `support@whisperstream.io` to exercise your rights to know,
delete, correct or receive non-discriminatory treatment. Section 10 describes
verification, authorized agents, response timelines and exceptions. Lanreal
operates exclusively online and has a direct relationship with the consumers
from whom it collects personal information; email is
its designated CCPA request method under Cal. Civ. Code Section 1798.130(a)(1)(A).
We update applicable preceding-twelve-month disclosures at least annually.

## 14. Changes to This Policy

We provide applicable privacy information when collecting information directly.
For information obtained from another source, applicable GDPR Article 14
information is provided within one month and sooner where first communication
or disclosure requires it, unless a documented legal exception applies. Before
using retained information for a new purpose, we explain that purpose and the
relevant additional information. Bundling this policy does not replace any
required notice at collection or separate consent.

Lanreal may revise this Privacy Policy and publish a new version and effective
date. Privacy Policy versions are independent of app and software-notice
versions. A change is material when it changes how Lanreal collects, uses,
shares or retains personal information; the version number alone does not
determine that.

**How you learn about changes.** The policy bundled with your source revision
is the policy supplied with that revision. Rebuilding newer source includes its
bundled policy; the app does not automatically fetch newer policy documents.
Existing packages retain the documents supplied with that package. If updated
terms change rights or obligations, content processing remains blocked until
the user reviews and affirmatively accepts the applicable terms. Closing,
deferring, building, installing or continued use does not record acceptance.
The most recent published version of this Privacy Policy is also available at
`https://whisperstream.io/privacy`, which also links to the notices for other
Whisperstream editions.

**Service-provider and recipient changes.** If Lanreal adds, removes,
or replaces a party listed in Section 5, or materially changes that
party's role or purpose, the change will be reflected in a new version
of this Privacy Policy. Lanreal also publishes a public provider list at
`https://whisperstream.io/subprocessors` and a companion RSS feed
at `https://whisperstream.io/subprocessors/feed.xml`, both of which
mirror Section 5 and the human-readable changelog Lanreal maintains
for provider additions, removals, replacements, and material updates. Except for
changes required for security, legal, or fraud-prevention reasons,
Lanreal commits to publishing each prospective provider change
to that page and feed at least thirty (30) days before it takes
effect, so that you have a meaningful opportunity to exercise the
right to object to processing under Article 21 of the General Data
Protection Regulation, or the equivalent right of withdrawal of
consent and accountability-based recourse under Canadian federal
privacy law and the *Personal Information Protection and Electronic
Documents Act*, before the change becomes operative. The page and
feed are the canonical advance-notice channel; Section 5 of this
Privacy Policy remains the authoritative source of truth for the
current list.

## 15. How to File a Complaint

You may contact Lanreal at `support@whisperstream.io` about a privacy concern.
You do not have to complete our process before exercising a statutory right
to complain directly to a regulator. Lanreal's Privacy Officer will investigate your complaint
and respond within the timeline described in Section 10. Lanreal
commits to investigating all privacy complaints received and, if a
complaint is found justified, taking appropriate measures including
amending policies and practices where necessary.

Regulator routes include:

- **European Economic Area:**
  you have the right under GDPR Article 77 to lodge a complaint with
  the data protection supervisory authority of the Member State of
  your habitual residence, place of work, or the place of the alleged
  infringement.
- **United Kingdom:** you may raise a concern with the Information
  Commissioner's Office at `https://ico.org.uk`.
- **Switzerland:** you may report a suspected data-protection violation to
  the Federal Data Protection and Information Commissioner at
  `https://www.edoeb.admin.ch` under the applicable Swiss rules.
- **California:** you may contact the California Privacy Protection
  Agency or the California Attorney General's Office for information
  about your rights under the CCPA.
- **Canada (federal):** you may file a complaint with the Office of
  the Privacy Commissioner of Canada (the 'OPC') at
  `https://www.priv.gc.ca/en/report-a-concern/`, toll-free
  1-800-282-1376. The OPC has the authority to investigate your
  complaint under section 11 of the Personal Information Protection
  and Electronic Documents Act, issue a Report of Findings, and
  make formal recommendations. If the matter remains unresolved
  after the OPC's investigation, section 14 of the Act grants you
  the right to apply to the Federal Court of Canada for a hearing.
- **Canada (provincial):** if you are a resident of Quebec, British
  Columbia, or Alberta, your provincial regulator may have parallel
  jurisdiction. You may contact Quebec's Commission d'accès à
  l'information at `https://www.cai.gouv.qc.ca`, the Office of the
  Information and Privacy Commissioner for British Columbia at
  `https://www.oipc.bc.ca`, or the Office of the Information and
  Privacy Commissioner of Alberta at `https://www.oipc.ab.ca`.

Nothing in this section limits any other right or remedy available to
you under applicable law.

## 16. Contact Us

Questions, requests, and complaints about this Privacy Policy and
about Lanreal's handling of personal information should be directed
to Lanreal's Privacy Officer:

**Privacy Officer**
Lanreal Technologies Inc.
18 King Street East, Suite 1400
Toronto, Ontario M5C 1C4
Canada

Email: `support@whisperstream.io`

Lanreal's Privacy Officer handles questions and rights requests about our
processing and this policy's commitments. Section 2 explains our role. You may
also contact your selected provider about information it handles directly;
referral to a provider does not limit a request or remedy against Lanreal.

**Accessibility.** This Privacy Policy is available in Markdown format bundled with the Software, in plain text format installed with the Software, and in HTML format at `https://whisperstream.io/privacy`. If you need this Privacy Policy in an alternative format to accommodate a disability, please contact us at `support@whisperstream.io`.

Copyright (c) Lanreal Technologies Inc. All rights reserved.
