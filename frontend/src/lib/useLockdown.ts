/**
 * useLockdown — tracks the backend lockdown lock state in the frontend.
 *
 * Polls GET /api/lockdown/status on mount (and whenever `refresh` is called),
 * then stays live by subscribing to the `transcripts.locked` WS event so an
 * idle re-lock surfaces instantly without another poll.
 *
 * Exported shape (`LockStatus`) is kept intentionally minimal — it only carries
 * what the three consuming pages (Transcripts, Enhancement, Home) need to
 * render their lock-aware states:
 *   - `locked`                    whether the transcript surface is currently locked
 *   - `lockoutRemainingSeconds`   backoff countdown (0 = no lockout)
 *   - `refresh`                   call after a successful unlock / enable / disable
 *
 * Lock/backoff counters are ALWAYS backend-authoritative; this hook mirrors
 * them into React state, it does not own them.
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { getLockdownStatus } from "./api/lockdown";
import type { AppEvent } from "./socket/events";
import { broadcastLockdownChanged, errorType } from "./logCapture";

export interface LockStatus {
  /** Whether the transcript surface is currently locked. False when lockdown
   *  is not enabled (non-lockdown installs never lock). */
  locked: boolean;
  /** Seconds remaining in the current backoff lockout (0 = input enabled). */
  lockoutRemainingSeconds: number;
  /** Whether Lockdown Mode is set up on this install. */
  enabled: boolean;
  /** Whether a password-wrapped key envelope (lockdown.key.json) exists. The
   *  Home Manage / disable UI uses this to pick the full (keep-history) vs slim
   *  (no-envelope / history-off) variant. False for a no-envelope lockdown. */
  hasEnvelope: boolean;
  /** Whether transcript history is currently being saved. False in the
   *  no-envelope path. Only meaningful under lockdown. */
  historyEnabled: boolean;
  /** Authoritative machine restriction, independent of the Lockdown master. */
  historyDisabled?: boolean;
  /** The Step-1 acknowledgment version the user has on record (null = none).
   *  Compared against the bundled LOCKDOWN_ACK_VERSION to decide whether the
   *  acknowledgment screen can be skipped (exact match) or must re-show. */
  acknowledgedVersion: string | null;
  /** Whether an administrator machine policy forces Lockdown Mode on. The UI
   *  keeps the master control visible and ON but disabled, and removes every
   *  turn-off / destructive-reset action, so no surface offers a change the
   *  backend will refuse. Managed authority NEVER replaces the ordinary
   *  Lockdown explanations on dependent settings. */
  managed: boolean;
  /** Re-fetch the status from the backend. Call after unlock / enable / disable
   *  so the state reflects the new reality without waiting for the next event. */
  refresh: () => void;
}

/** Default / loading state: assume unlocked so pages never block rendering
 *  while the first fetch is in flight. */
const DEFAULT_STATUS: Omit<LockStatus, "refresh"> = {
  locked: false,
  lockoutRemainingSeconds: 0,
  enabled: false,
  hasEnvelope: false,
  historyEnabled: true,
  historyDisabled: false,
  acknowledgedVersion: null,
  managed: false,
};

export function useLockdown(
  subscribe: (handler: (event: AppEvent) => void) => () => void,
): LockStatus {
  const [status, setStatus] = useState<Omit<LockStatus, "refresh">>(DEFAULT_STATUS);

  // Stable fetch helper — never changes identity so it is safe to use as a
  // dependency or call from outside the hook via the returned `refresh`.
  const fetchStatus = useCallback(async () => {
    try {
      const s = await getLockdownStatus();
      setStatus({
        locked: s.locked,
        lockoutRemainingSeconds: s.lockout_remaining_seconds,
        enabled: s.enabled,
        hasEnvelope: s.has_envelope,
        historyEnabled: s.history_enabled,
        historyDisabled: s.history_disabled === true,
        acknowledgedVersion: s.acknowledged_version ?? null,
        // Require the exact boolean: a missing field on an older backend, or
        // any other truthy shape, must not read as managed authority.
        managed: s.managed === true,
      });
    } catch (err) {
      // Backend unreachable at startup — keep the safe default (unlocked).
      // The next reconnect/mount will retry.
      console.warn(
        `[Lockdown] status fetch failed error_type=${errorType(err)}`,
      );
    }
  }, []);

  // Poll on mount.
  useEffect(() => {
    fetchStatus();
  }, [fetchStatus]);

  // Subscribe to the transcripts.locked WS event for real-time idle re-lock.
  // The event arrives when the backend's idle timer fires; we immediately
  // mirror the locked state so the lock screen animates in without a round-trip.
  const subscribeRef = useRef(subscribe);
  subscribeRef.current = subscribe;

  useEffect(() => {
    const unlisten = subscribeRef.current((event) => {
      if (event.type === "transcripts.locked") {
        setStatus((prev) => ({ ...prev, locked: true, lockoutRemainingSeconds: 0 }));
        // Also re-fetch to pick up any server-side state changes (e.g. the
        // backend may have reset the backoff counter on idle lock).
        fetchStatus();
      } else if (event.type === "lockdown.changed") {
        // The main webview receives the backend transition over WebSocket and
        // fans the content-free boolean out to every other Tauri/browser
        // webview. Capture applies the transition idempotently.
        broadcastLockdownChanged(event.enabled);
        setStatus((prev) => {
          // Machine policy outranks any transition event: a managed install
          // stays on until the authoritative status says otherwise.
          const enabled = prev.managed || event.enabled;
          return {
            ...prev,
            enabled,
            locked: enabled ? prev.locked : false,
          };
        });
        fetchStatus();
      }
    });
    return unlisten;
  }, [fetchStatus]);

  return {
    ...status,
    refresh: fetchStatus,
  };
}
