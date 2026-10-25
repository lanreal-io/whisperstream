import { useEffect, useRef } from "react";
import { getScreenReaderStatus } from "./api/system";
import type { EventHandler } from "./socket/events";

/** Upper bound for one screen-reader status request. */
export const SCREEN_READER_STATUS_TIMEOUT_MS = 5_000;

export interface ScreenReaderStatusFollower {
  /**
   * Fetch the backend's cached status. Requests never overlap: while one is in
   * flight, `force` queues exactly one follow-up (used after a reconnect, when
   * the in-flight answer may predate the outage); otherwise the call joins it.
   */
  refresh(force?: boolean): void;
  /** Apply a pushed `accessibility.screen_reader` value. */
  push(active: boolean): void;
  /** True once any fetch or push has delivered a value. */
  known(): boolean;
  dispose(): void;
}

/**
 * Backend-owned screen-reader state for one window: fetch, then follow pushes.
 * A failed or timed-out fetch retains the last known state. A fetch that was
 * overtaken by a push is discarded so an older answer cannot undo a newer one.
 */
export function createScreenReaderStatusFollower(
  apply: (active: boolean) => void,
  timeoutMs = SCREEN_READER_STATUS_TIMEOUT_MS,
): ScreenReaderStatusFollower {
  let disposed = false;
  let inFlight = false;
  let rerun = false;
  let pushEpoch = 0;
  let hasValue = false;
  let controller: AbortController | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const refresh = (force = false) => {
    if (disposed) return;
    if (inFlight) {
      if (force) rerun = true;
      return;
    }
    inFlight = true;
    const epoch = pushEpoch;
    const request = new AbortController();
    controller = request;
    timer = setTimeout(() => request.abort(), timeoutMs);
    void getScreenReaderStatus({ signal: request.signal })
      .then((status) => {
        if (disposed || epoch !== pushEpoch) return;
        if (typeof status?.active !== "boolean") return;
        hasValue = true;
        apply(status.active);
      })
      .catch(() => {
        // Retain the last known state while the backend is unavailable.
      })
      .finally(() => {
        if (timer !== null) clearTimeout(timer);
        timer = null;
        controller = null;
        inFlight = false;
        if (rerun && !disposed) {
          rerun = false;
          refresh();
        }
      });
  };

  return {
    refresh,
    push(active) {
      if (disposed) return;
      pushEpoch += 1;
      hasValue = true;
      apply(active);
    },
    known: () => hasValue,
    dispose() {
      disposed = true;
      rerun = false;
      if (timer !== null) clearTimeout(timer);
      timer = null;
      controller?.abort();
      controller = null;
    },
  };
}

/**
 * Follow backend screen-reader state over the app socket, only while
 * `enabled` (the persistent recording control preference is on). Becoming
 * enabled fetches once and subscribes to `accessibility.screen_reader`
 * pushes; every transition to connected while enabled, including the first,
 * refetches (coalesced with any in-flight request). While disabled it makes no requests,
 * holds no subscription, and aborts any in-flight request.
 * `apply` and `subscribe` are read through refs, so callers need not memoize.
 */
export function useScreenReaderStatusSync({
  enabled,
  connected,
  subscribe,
  apply,
}: {
  enabled: boolean;
  connected: boolean;
  subscribe: (handler: EventHandler) => () => void;
  apply: (active: boolean) => void;
}): void {
  const applyRef = useRef(apply);
  const subscribeRef = useRef(subscribe);
  const followerRef = useRef<ScreenReaderStatusFollower | null>(null);
  // A socket already open at mount is not a transition; the mount fetch covers it.
  const connectedRef = useRef(connected);

  useEffect(() => {
    applyRef.current = apply;
    subscribeRef.current = subscribe;
  });

  useEffect(() => {
    if (!enabled) return;
    const follower = createScreenReaderStatusFollower((active) =>
      applyRef.current(active),
    );
    followerRef.current = follower;
    follower.refresh();
    const unsubscribe = subscribeRef.current((event) => {
      if (event.type === "accessibility.screen_reader") {
        follower.push(event.active);
      }
    });
    return () => {
      unsubscribe();
      follower.dispose();
      if (followerRef.current === follower) followerRef.current = null;
    };
  }, [enabled]);

  useEffect(() => {
    const wasConnected = connectedRef.current;
    connectedRef.current = connected;
    if (!connected || wasConnected) return;
    // Every transition to connected, including the first: a change pushed
    // while disconnected was lost and the event has no connect-time replay.
    // An in-flight request may predate the connection, so force one follow-up.
    followerRef.current?.refresh(true);
  }, [connected]);
}
