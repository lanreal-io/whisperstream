import { useCallback, useEffect, useRef, useState } from "react";
import {
  getHistoryRecoveryStatus,
  type HistoryRecoveryStatus,
} from "./api/historyRecovery";
import type { AppEvent } from "./socket/events";

export interface HistoryRecoveryHandle {
  /** Latest standard-history recovery status; null until loaded, when
   *  disabled, or after a failed read. Carries no key material. */
  status: HistoryRecoveryStatus | null;
  refresh: () => Promise<HistoryRecoveryStatus | null>;
}

/**
 * Standard-history recovery status (needs setup, key missing, counts). Loads on
 * mount and on every `transcripts.changed` event while `enabled`. Disabled
 * under Lockdown and organization history policy, where it reports null.
 */
export function useHistoryRecovery(
  subscribe: (handler: (event: AppEvent) => void) => () => void,
  enabled: boolean,
): HistoryRecoveryHandle {
  const [status, setStatus] = useState<HistoryRecoveryStatus | null>(null);
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    if (!enabledRef.current) {
      setStatus(null);
      return null;
    }
    const mine = ++generation.current;
    try {
      const next = await getHistoryRecoveryStatus();
      if (mine === generation.current) setStatus(next);
      return next;
    } catch {
      if (mine === generation.current) setStatus(null);
      return null;
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [enabled, refresh]);

  useEffect(() => {
    if (!enabled) return;
    return subscribe((event) => {
      if (event.type === "transcripts.changed") void refresh();
    });
  }, [enabled, subscribe, refresh]);

  return { status: enabled ? status : null, refresh };
}
