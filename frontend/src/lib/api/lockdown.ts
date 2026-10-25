import { fetchJSON, BASE } from "./client";
import { type AppConfig } from "./config";
import { broadcastLockdownChanged, prepareForLockdownEnable, recoverFromFailedLockdownEnable } from "../logCapture";


// ---------------------------------------------------------------------------
// Lockdown Mode (Task 4.1)
//
// Real cryptographic access control for the transcript history. The backend
// wraps the existing transcript data key under a password + a one-time recovery
// code and forces enhancement to the on-device LLM. Safe diagnostic logging
// remains available. NEVER log a password, the recovery code, or any response that
// carries one — the request/response logging helpers above don't capture
// bodies, and callers must not console.log these values.
// ---------------------------------------------------------------------------

/** Result of GET /api/lockdown/status. Carries NO key material. */
export interface LockdownStatus {
  /** Whether Lockdown Mode is on. True for BOTH the keep-history (envelope)
   *  path and the no-envelope, history-off path. */
  enabled: boolean;
  /** Whether the transcript surface is currently locked (only ever true for an
   *  envelope-backed lockdown; a no-envelope lockdown never gates). */
  locked: boolean;
  /** Seconds remaining in the current backoff lockout (0 = unlocked input). */
  lockout_remaining_seconds: number;
  /** Configured idle auto-lock window in seconds (0 = Never). */
  idle_lock_seconds: number;
  /** Whether a recovery code is on file. True only for the envelope path;
   *  false for a no-envelope (history-off) lockdown. */
  has_recovery_code: boolean;
  /** Whether a password-wrapped key envelope (lockdown.key.json) exists. The
   *  frontend uses this to pick the full (keep-history) vs slim (history-off)
   *  Manage / disable UI variant. Equals has_recovery_code today, but is the
   *  semantically correct signal for the envelope presence. */
  has_envelope: boolean;
  /** Whether transcript history is currently being saved. False in the
   *  no-envelope path (history-off is its intended pairing). */
  history_enabled: boolean;
  /** Independent administrator restriction on transcript and audio saving. */
  history_disabled?: boolean;
  /** The Step-1 acknowledgment VERSION the user has on record, or null when
   *  none is stored. The client compares this to its bundled LOCKDOWN_ACK_VERSION:
   *  an exact match means the acknowledgment was already accepted for the current
   *  copy and Step 1 may be skipped; null / any mismatch fails closed (re-show). */
  acknowledged_version: string | null;
  /** Whether an administrator machine policy forces Lockdown Mode on this
   *  device (HKLM\SOFTWARE\Policies\Whisperstream\ManagedLockdown). The user
   *  cannot turn Lockdown off: the backend rejects disable/reset with the
   *  stable `managed_by_organization` code before any mutation. Optional so an
   *  older backend (no field) reads as unmanaged. */
  managed?: boolean;
  /** Bounded machine-policy state token, or null when unmanaged. Diagnostic
   *  only — never render it; the user-visible authority copy is fixed. */
  managed_policy_state?: string | null;
}


/** The one user-visible message for a machine-policy refusal (copy-review.md,
 *  locked option B). Never surface a bare HTTP 403/409 instead. */
export const HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE =
  "Transcript and audio history are disabled by your organization.";

export function isHistoryDisabledByOrganizationError(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err ?? "");
  return message.includes("history_disabled_by_organization");
}

export const MANAGED_BY_ORGANIZATION_MESSAGE =
  "Lockdown Mode is managed by your organization and can't be turned off here.";


/** True when a failed Lockdown mutation was refused by machine policy. The
 *  backend answers 403 with the stable `managed_by_organization` error code and
 *  fetchJSON carries the raw body through in the Error message. */
export function isManagedByOrganizationError(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err ?? "");
  return message.includes("managed_by_organization");
}


/** Result of POST /api/lockdown/unlock. */
export interface LockdownUnlockResult {
  ok: boolean;
  /** Present on failure: seconds the input must stay disabled (backend backoff). */
  lockout_remaining_seconds?: number;
  /** Present on a successful recovery-code unlock: prompt the user to set a new
   *  password (the recovery code is the last-resort path). */
  needs_new_password?: boolean;
}


export async function getLockdownStatus(): Promise<LockdownStatus> {
  return fetchJSON(`${BASE}/api/lockdown/status`);
}


/** Options for enabling Lockdown Mode.
 *
 *  Two flavors, distinguished by `password`:
 *  - WITH a password -> keep-history path: the backend wraps the data key under
 *    the password + a fresh recovery code and returns the code (shown once).
 *  - WITHOUT a password -> no-envelope, history-off path: no key envelope is
 *    written, no recovery code is minted; the backend forces history off. The
 *    returned `recovery_code` is null.
 *  The optional history settings are persisted alongside lockdown_mode. NEVER
 *  log the password or the returned recovery code. */
export interface EnableLockdownOptions {
  keychainConsent?: boolean;
  password?: string;
  idleLockSeconds?: number;
  historyEnabled?: boolean;
  saveAudio?: boolean;
  transcriptRetentionDays?: number;
}


// Overload 1 (keep-history): a password always mints a recovery code.
export async function enableLockdown(
  password: string,
  idleLockSeconds?: number,
): Promise<{ recovery_code: string; config: AppConfig }>;

// Overload 2 (options form): supports the no-password / history-off path, where
// recovery_code comes back null.
export async function enableLockdown(
  opts: EnableLockdownOptions,
): Promise<{ recovery_code: string | null; config: AppConfig }>;

export async function enableLockdown(
  passwordOrOpts: string | EnableLockdownOptions = {},
  idleLockSeconds?: number,
): Promise<{ recovery_code: string | null; config: AppConfig }> {
  const opts: EnableLockdownOptions =
    typeof passwordOrOpts === "string"
      ? { password: passwordOrOpts, idleLockSeconds }
      : passwordOrOpts;
  const payload: Record<string, unknown> = {};
  if (opts.password) payload.password = opts.password;
  if (opts.keychainConsent !== undefined)
    payload.keychain_consent = opts.keychainConsent;
  if (opts.idleLockSeconds !== undefined)
    payload.idle_lock_seconds = opts.idleLockSeconds;
  if (opts.historyEnabled !== undefined)
    payload.history_enabled = opts.historyEnabled;
  if (opts.saveAudio !== undefined) payload.save_audio = opts.saveAudio;
  if (opts.transcriptRetentionDays !== undefined)
    payload.transcript_retention_days = opts.transcriptRetentionDays;
  prepareForLockdownEnable();
  try {
    const result = await fetchJSON<{
      recovery_code: string | null;
      config: AppConfig;
    }>(`${BASE}/api/lockdown/enable`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    broadcastLockdownChanged(true);
    return result;
  } catch (error) {
    await recoverFromFailedLockdownEnable();
    throw error;
  }
}


/** Disable Lockdown Mode. A keep-history (envelope-backed) lockdown requires the
 *  current password; a no-envelope (history-off) lockdown has no key to unwrap,
 *  so `password` may be omitted. Re-seals the transcript store to DPAPI-only and
 *  leaves diagnostic logging unchanged. */
export async function disableLockdown(
  password?: string,
): Promise<{ ok: boolean; config?: AppConfig }> {
  const result = await fetchJSON<{ ok: boolean; config?: AppConfig }>(`${BASE}/api/lockdown/disable`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(password ? { password } : {}),
  });
  if (result.ok) broadcastLockdownChanged(false);
  return result;
}


/** Last-resort reset for a user who forgot BOTH the password and the recovery
 *  code. DESTROYS the encrypted transcript history (it is erased still-encrypted,
 *  never decrypted) and turns Lockdown Mode off. Requires the typed confirmation
 *  "DELETE" (the backend rejects anything else). Returns the sanitized config for
 *  the client to adopt, like disable. Only valid for an envelope-backed lockdown. */
export async function resetLockdown(
  confirm: string,
): Promise<{ ok: boolean; config?: AppConfig; error?: string }> {
  const result = await fetchJSON<{ ok: boolean; config?: AppConfig; error?: string }>(`${BASE}/api/lockdown/reset`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm }),
  });
  if (result.ok) broadcastLockdownChanged(false);
  return result;
}


/** Persist that the user accepted the Step-1 HIPAA acknowledgment for the given
 *  acknowledgment version. The backend stamps a UTC timestamp + software version
 *  (EULA-style). Called once when the user passes Step 1; subsequent enables skip
 *  the screen while the stored version matches the bundled LOCKDOWN_ACK_VERSION. */
export async function acknowledgeLockdown(
  ackVersion: string,
): Promise<{ ok: boolean; ack_version?: string }> {
  return fetchJSON(`${BASE}/api/lockdown/acknowledge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ack_version: ackVersion }),
  });
}


/** Unlock the transcript surface with EITHER a password OR a recovery code
 *  (exactly one). A wrong secret increments the backend backoff and returns
 *  `lockout_remaining_seconds`. A successful recovery-code unlock sets
 *  `needs_new_password`. NEVER log the secret. */
export async function unlockLockdown(
  secret: { password: string } | { recovery_code: string },
): Promise<LockdownUnlockResult> {
  return fetchJSON(`${BASE}/api/lockdown/unlock`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(secret),
  });
}


/** Change the Lockdown password. Verifies the current password server-side;
 *  the recovery code is left intact. NEVER log either password. */
export async function changeLockdownPassword(
  currentPassword: string,
  newPassword: string,
): Promise<{ ok: boolean }> {
  return fetchJSON(`${BASE}/api/lockdown/change-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
}


/** View the recovery code. The original one-time code is not re-displayable, so
 *  this REGENERATES a fresh code (gated on the current password) and
 *  invalidates the old one. Returns `recovery_code: null` on a wrong password.
 *  NEVER log the returned code. */
export async function viewRecoveryCode(
  password: string,
): Promise<{ ok: boolean; recovery_code: string | null }> {
  return fetchJSON(`${BASE}/api/lockdown/view-recovery`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
}
