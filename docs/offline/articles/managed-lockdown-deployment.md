## What Managed Lockdown offers

Managed Lockdown lets an organization make Whisperstream's Lockdown master control administrator-owned on managed Windows devices. It is not available for Mac. It keeps Lockdown on and limits enhancement to **Local**, so cloud providers and Ollama remain unavailable.

When the administrator's management assignment remains in place, the control is reapplied across configuration changes, restarts, updates, app-data deletion, and reinstalls. Whisperstream shows **Managed by your organization** instead of offering the user a way to turn Lockdown off or perform its destructive reset.

Managed Lockdown is designed to resist changes by a standard Windows user. It is not a security boundary against someone who controls a local administrator account, device policy, or the installed application. Organizations should manage administrator access through their normal Windows security and endpoint-management practices.

## What users can still control

With only `ManagedLockdown=1`, users retain their transcript-history choices. With the optional `DisableHistory=1` policy, transcript and audio saving stay off and users cannot enable them. This option is intended for new installations and must be applied before first launch. It does not delete or migrate any existing history.

Without the history restriction, users can still:

- choose whether to save transcripts;
- manage their transcript password and recovery key;
- choose the transcript-history idle-lock interval; and
- export local support diagnostics. This remains available when history is disabled.

A newly managed setup starts with transcript history off. With `DisableHistory=1`, it stays off even if a user edits configuration or attempts password setup. ManagedLockdown alone preserves existing history choices and password settings. Existing-history migration and deletion are outside the no-history deployment scope. See [Lockdown Mode](/docs/data/lockdown-mode) for the user workflow and [Settings and data locations](/docs/reference/settings-data-locations) for **Download logs**.

## Before deployment

You need the Whisperstream Windows installer, local administrator rights or a device-management agent running as SYSTEM, and the required model files. Pilot the deployment on a test device with a standard Windows user before rolling it out.

Managed Lockdown uses the **64-bit machine registry**, not a per-user setting. Protect that location with normal Windows permissions. Do not grant standard users permission to create, change, or remove the policy, and control who has local administrator access.

> Important: Your IT team is responsible for making sure users cannot change the policy values. Check the permissions on `HKLM\SOFTWARE\Policies\Whisperstream` and keep your management tool's assignment in place. Whisperstream enforces the values it finds at startup, but it cannot stop someone with administrator rights from editing or removing them.

## Enable during installation

Run the installer from an elevated PowerShell window. Use Whisperstream 2.0 or later; older installers do not enforce `DisableHistory`. Change the filename below to match your installer:

```powershell
$installer = Start-Process `
  -FilePath '.\Whisperstream_2.0.0_x64-setup.exe' `
  -ArgumentList '/S', '/MANAGEDLOCKDOWN=1', '/DISABLEHISTORY=1' `
  -Wait -PassThru
$installer.ExitCode
```

`/S` installs silently. `/DISABLEHISTORY=1` writes and verifies the independent no-history machine policy. `/MANAGEDLOCKDOWN=1` writes and verifies the machine policy. Require a successful exit code and verify the registry value below. An invalid policy argument exits with code `87`; inability to write or verify policy exits with `740`. There is no per-user fallback.

Omitting either policy argument leaves that value unchanged. Both arguments accept only `1`; use the removal procedure to clear a restriction.

## Apply registry policy before first launch

In an elevated PowerShell window, run:

```powershell
reg.exe add "HKLM\SOFTWARE\Policies\Whisperstream" `
  /v ManagedLockdown /t REG_DWORD /d 1 /f /reg:64
if ($LASTEXITCODE -ne 0) { throw 'Policy write failed.' }
reg.exe add "HKLM\SOFTWARE\Policies\Whisperstream" `
  /v DisableHistory /t REG_DWORD /d 1 /f /reg:64
if ($LASTEXITCODE -ne 0) { throw 'History policy write failed.' }
```

Quit Whisperstream from its tray menu, then start it again. Closing its window alone may leave it running. External policy changes take effect at the next application start, before content-capable services start.

### Deploy with Intune, Group Policy, or another management tool

Configure the following machine values through your organization's Windows device-management tooling:

| Setting | Required value |
| --- | --- |
| Hive | `HKEY_LOCAL_MACHINE` |
| Key | `SOFTWARE\Policies\Whisperstream` |
| Registry view | 64-bit |
| Value names | `ManagedLockdown` and `DisableHistory` (two separate values) |
| Value type | `REG_DWORD` |
| Value data | `1` |

For Intune scripts or Win32 remediations, use device context with administrator or SYSTEM rights. For Group Policy Preferences, use Computer Configuration and the 64-bit registry view. The `reg.exe` command above explicitly selects that view even when invoked by a 32-bit management process. Schedule an application restart after applying policy.

For each value, any nonzero DWORD enables its restriction. `DisableHistory` is enforced independently: removing or setting `ManagedLockdown` to zero does not permit history while `DisableHistory` remains enabled. DWORD `0` or absence leaves that restriction inactive. An unreadable key enforces both restrictions. A present value of the wrong type or an unreadable policy key fails closed, so repair its type or permissions rather than expecting it to disable management.

## Provision models for offline use

Managed Lockdown still allows model downloads. For a device that must operate without internet access, provision the files before use with [Model recovery and manual installation](/docs/reference/model-recovery). That chapter lists exact formats, filenames, checksums, and folders for each supported model.

The default model folders belong to the Windows user running Whisperstream. `%LOCALAPPDATA%` in a SYSTEM deployment script refers to the system profile, not the intended user's profile. Copy files into each intended user's model folders, or deliberately configure the supported model location for that user. Keep filenames and directory structure intact, and give that user access to the files.

Include the transcription model selected during setup. Local enhancement is optional; if required, provision Stream-1 Mini, Gemma 4 E2B, or both. The Local setup check measures Stream-1 Mini; to use Gemma instead, choose Customizable in the model picker so only that model is checked. Complete files already in their recognized locations are reused. Verify the documented checksums before launch because presence alone does not establish file integrity.

## Verify the deployment

Query both explicit 64-bit values:

```powershell
reg.exe query "HKLM\SOFTWARE\Policies\Whisperstream" `
  /v ManagedLockdown /reg:64
if ($LASTEXITCODE -ne 0) { throw 'Policy query failed.' }
reg.exe query "HKLM\SOFTWARE\Policies\Whisperstream" `
  /v DisableHistory /reg:64
if ($LASTEXITCODE -ne 0) { throw 'History policy query failed.' }
```

Expect both values to be `REG_DWORD` with data `0x1`. Then:

1. Quit and restart Whisperstream as the intended standard user.
2. On Home, confirm Lockdown is on and labeled **Managed by your organization**. The user must not be able to disable it or use its destructive reset.
3. Confirm enhancement is Local and cloud providers, Ollama, custom remote endpoints, and non-loopback Local overrides are unavailable.
4. Confirm the intended transcription model is recognized. Test dictation and, if needed, the Local enhancement check. Test offline after provisioning if network isolation is required.
5. On a fresh profile, confirm Transcripts shows **History is disabled** and **Transcript and audio history are disabled by your organization.** Save transcripts and Save audio must stay off; no history/password setup action should be offered. Dictate a synthetic test phrase, restart, and confirm no history entry or recording was saved. Verify attempts to enable saving through configuration or the local API are rejected or overridden.
6. Restart again and confirm management persists. Include update, uninstall/reinstall, and policy-removal checks in your disposable pilot environment before organization-wide rollout.

Avoid deleting production app data to test policy persistence. It can destroy user history even though the machine policy remains.

## Remove management

First remove any device-management assignment that would restore the value. Then run this command as an administrator. Remove both named values to lift both restrictions. Removing only `ManagedLockdown` leaves history disabled:

```powershell
reg.exe delete "HKLM\SOFTWARE\Policies\Whisperstream" `
  /v ManagedLockdown /f /reg:64
if ($LASTEXITCODE -ne 0) { throw 'Policy removal failed.' }
reg.exe delete "HKLM\SOFTWARE\Policies\Whisperstream" `
  /v DisableHistory /f /reg:64
if ($LASTEXITCODE -ne 0) { throw 'History policy removal failed.' }
```

Quit and restart Whisperstream. The user regains authority over Lockdown, but an already-enabled Lockdown state stays on until they deliberately turn it off. Removing policy does not automatically start saving history, decrypt history or remove its password.

Updates, uninstall, reinstall, and app-data deletion leave this organization-owned registry policy in place. Uninstalling Whisperstream is not a policy-removal procedure.

## Troubleshoot deployment

- **The managed label is missing:** verify the value is under HKLM in the 64-bit view, is a DWORD set to `1`, and was written successfully. Quit the tray process and relaunch.
- **The policy returns after removal:** remove or change the management assignment that is reapplying it.
- **Management remains after a registry edit:** check for an unreadable key or a malformed value, then restart. Invalid present policy fails closed.
- **A model is missing or a download starts:** check the actual user's data directory, required filenames, and complete file set against the recovery chapter. Files placed under the deployment agent's profile are not the user's cache.
- **A user lost access to history:** use their recovery key. Managed Lockdown provides no administrator password escrow or central unlock, and blocks the destructive Lockdown reset while managed. Applying or removing policy does not recover encrypted history.

## Network requirements

Managed Lockdown is not an internet-disconnection switch. Optional model acquisition uses HTTPS with `huggingface.co`, `*.huggingface.co`, `hf.co`, and `*.hf.co`. Parakeet v2/v3, Qwen and Cohere downloads can also use exact upstream sherpa-onnx release archives at `github.com`, redirected to `release-assets.githubusercontent.com`, after the Hugging Face source remains unavailable. The app verifies its shipped archive checksum before extraction and the required file checksums before installation. Customizable can automatically try its pinned official Google Gemma backup through Hugging Face when its primary source is unavailable. Gemma and Stream-1 Mini have no automatic GitHub backup. Upstream Kokoro uses Hugging Face with no automatic backup source.

These requests send download and connection metadata, not transcript or audio content. Whisperstream makes no license-validation, application-update or in-app review/feedback submission requests. About external help links are user-initiated and unavailable in Lockdown. Local log and documentation exports remain available. Use the manual recovery instructions to provision supported model files without downloading them through the app.

For an offline deployment, retain and provision the model files and apply your organization's network controls. During a pilot, inspect process-scoped traffic while exercising model acquisition, local enhancement, and attempted remote enhancement. Local app and model-server communication uses loopback.

## Understand the boundary

Managed Lockdown controls Whisperstream's transcription, enhancement, and history behavior. It does not control the destination app that receives inserted text. Apply the organization's own storage, sync, sharing, and network rules to that app.

While Lockdown is effective, Whisperstream suspends Agent Speak processing. This does not stop Codex or Claude Code themselves or govern their configured services.

This no-history option controls saved transcript/audio history. It does not disable content-free diagnostics or promise that every file workflow is memory-only; imported/uploaded audio can require temporary decoding files.

Safe diagnostics remain local unless a user deliberately saves and shares an export. The export excludes transcript and audio content, credentials, recovery keys, and the Lockdown access log.
