import { fetchJSON, BASE } from "./client";


// ---------------------------------------------------------------------------
// [History recovery] standard-history recovery key, recovery password,
// missing-key restore and Start fresh. Mirrors /api/history/* in
// src/whisperstream/server/app.py. NEVER log a recovery key or password.
// ---------------------------------------------------------------------------

export interface HistoryRecoveryStatus {
  /** Lockdown owns recovery while it is on; everything below is then false. */
  lockdown: boolean;
  has_recovery_key: boolean;
  has_recovery_password: boolean;
  /** Keychain/DPAPI lost the key for good; saving is paused until restore or Start fresh. */
  key_missing: boolean;
  /** The protected key loads but can't read saved history; the recovery key may restore it. */
  key_mismatch: boolean;
  /** History is on and readable but has no confirmed recovery key yet. */
  needs_setup: boolean;
  /** Saved transcript count, or null when it can't be read without content access. */
  record_count: number | null;
  has_audio: boolean;
  key_lockout_remaining_seconds: number;
  password_lockout_remaining_seconds: number;
}

/** Result of POST /api/history/recovery/unlock. Wrong secrets return ok:false with 200. */
export interface HistoryRecoveryUnlockResult {
  ok: boolean;
  error?: "recovery_key_invalid" | "recovery_password_invalid";
  lockout_remaining_seconds?: number;
}

/** Stable error codes the history recovery routes return in `{error}`. */
export type HistoryRecoveryErrorCode =
  | "password_too_short"
  | "password_too_common"
  | "history_initializing"
  | "lockdown_active"
  | "history_not_ready"
  | "recovery_setup_required"
  | "recovery_setup_failed"
  | "history_key_not_missing"
  | "restore_access_denied"
  | "restore_failed"
  | "history_key_unavailable"
  | "confirm_required"
  | "history_disabled_by_organization"
  | "history_locked"
  | "history_key_missing"
  | "recovery_key_stale"
  | "history_key_mismatch"
  | "recovery_already_set_up"
  | "recovery_generation_stale";

const KNOWN_CODES: readonly HistoryRecoveryErrorCode[] = [
  "password_too_short",
  "password_too_common",
  "history_initializing",
  "lockdown_active",
  "history_not_ready",
  "recovery_setup_required",
  "recovery_setup_failed",
  "history_key_not_missing",
  "restore_access_denied",
  "restore_failed",
  "history_key_unavailable",
  "confirm_required",
  "history_disabled_by_organization",
  "history_locked",
  "history_key_missing",
  "recovery_key_stale",
  "history_key_mismatch",
  "recovery_already_set_up",
  "recovery_generation_stale",
];

/** Extract the backend error code from a failed request, if it is one we know. */
export function historyRecoveryErrorCode(err: unknown): HistoryRecoveryErrorCode | null {
  const message = err instanceof Error ? err.message : String(err ?? "");
  const body = message.replace(/^\d+:\s*/, "");
  try {
    const parsed = JSON.parse(body) as { error?: unknown; error_code?: unknown };
    const code = parsed.error ?? parsed.error_code;
    if (typeof code === "string" && (KNOWN_CODES as readonly string[]).includes(code)) {
      return code as HistoryRecoveryErrorCode;
    }
  } catch {
    /* Non-JSON error body. */
  }
  return KNOWN_CODES.find((code) => message.includes(`"${code}"`)) ?? null;
}

const jsonPost = (body?: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body ?? {}),
});

export async function getHistoryRecoveryStatus(): Promise<HistoryRecoveryStatus> {
  return fetchJSON(`${BASE}/api/history/recovery/status`);
}

/**
 * A key shown for confirmation. `generation` names it, so confirming can only
 * promote this key, never one a later request created.
 */
export interface PendingRecoveryKey {
  ok: true;
  recovery_key: string;
  generation: string | null;
}

/** Create a pending recovery key and return it for one-time display. */
export async function setupHistoryRecovery(): Promise<PendingRecoveryKey> {
  return fetchJSON(`${BASE}/api/history/recovery/setup`, jsonPost());
}

/**
 * Confirm the user saved the key from `generation`, adding the optional
 * recovery password.
 */
export async function confirmHistoryRecovery(
  generation: string | null,
  password?: string,
): Promise<{ ok: true }> {
  return fetchJSON(
    `${BASE}/api/history/recovery/confirm`,
    jsonPost({ generation, ...(password ? { password } : {}) }),
  );
}

/** Stage a new recovery key; the previous key works until the new one is confirmed. */
export async function regenerateHistoryRecoveryKey(): Promise<PendingRecoveryKey> {
  return fetchJSON(`${BASE}/api/history/recovery/regenerate`, jsonPost());
}

/** Set, change or (with null) remove the recovery password. */
export async function setHistoryRecoveryPassword(
  password: string | null,
): Promise<{ ok: true; has_recovery_password: boolean }> {
  return fetchJSON(
    `${BASE}/api/history/recovery/password`,
    jsonPost(password ? { password } : {}),
  );
}

export async function unlockHistoryWithRecoveryKey(
  recoveryKey: string,
): Promise<HistoryRecoveryUnlockResult> {
  return fetchJSON(`${BASE}/api/history/recovery/unlock`, jsonPost({ recovery_key: recoveryKey }));
}

export async function unlockHistoryWithRecoveryPassword(
  password: string,
): Promise<HistoryRecoveryUnlockResult> {
  return fetchJSON(`${BASE}/api/history/recovery/unlock`, jsonPost({ password }));
}

/** Start fresh: delete all saved history and its keys. Requires the typed "DELETE". */
export async function resetHistory(confirm: string): Promise<{ ok: true }> {
  return fetchJSON(`${BASE}/api/history/reset`, jsonPost({ confirm }));
}
