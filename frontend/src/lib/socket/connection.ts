

export const RECONNECT_MAX_MS = 30000;
import { type EventHandler, parseAppSocketEvent, logBenchmarkEvent } from "./events";

import { isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useRef, useState, useCallback } from "react";
import { BASE } from "../api/client";


export const WS_URL = BASE.replace(/^http/, "ws") + "/ws";

export const RECONNECT_BASE_MS = 1000;

/** A connection must stay open this long before the reconnect backoff resets.
 *  A server that accepts and immediately closes would otherwise be retried at
 *  the base delay forever (~1 Hz reconnect storm). */
export const STABLE_CONNECTION_MS = 5000;

/** Native signal that the system or its displays woke (macOS shell). */
export const SYSTEM_WAKE_EVENT = "system-wake";


export function useAppSocket(enabled = true) {
  const [connected, setConnected] = useState(false);
  const everConnectedRef = useRef<boolean>(false);
  const [everConnected, setEverConnected] = useState<boolean>(false);
  const [reconnectAttemptState, setReconnectAttemptState] = useState<number>(0);
  const [lastDisconnectAt, setLastDisconnectAt] = useState<number | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const connectionGeneration = useRef(0);
  const handlersRef = useRef<Set<EventHandler>>(new Set());
  const reconnectAttempt = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stableTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const send = useCallback((msg: Record<string, unknown>): boolean => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }, []);

  const subscribe = useCallback((handler: EventHandler) => {
    handlersRef.current.add(handler);
    return () => {
      handlersRef.current.delete(handler);
    };
  }, []);

  useEffect(() => {
    if (!enabled) {
      setConnected(false);
      return;
    }
    let disposed = false;
    // Tracks which message types have been seen this session. Reset on
    // reconnect so re-attach starts fresh. Prevents per-receive log spam
    // that would wrap the 500-entry ring buffer in <60s on heavy sessions.
    // (console.debug is not intercepted by logCapture.ts, so per-receive at
    // DEBUG would be invisible in exports — per-type-first-receive at INFO is
    // the right tradeoff.)
    const seenTypes = new Set<string>();

    function clearStableTimer() {
      if (stableTimer.current !== null) {
        clearTimeout(stableTimer.current);
        stableTimer.current = null;
      }
    }

    function connect() {
      if (disposed) return;

      const ws = new WebSocket(WS_URL);
      connectionGeneration.current += 1;
      wsRef.current = ws;
      let opened = false;

      ws.onopen = () => {
        if (disposed || wsRef.current !== ws) return;
        console.log(`[WS] connected url=${WS_URL}`);
        setConnected(true);
        if (!everConnectedRef.current) {
          everConnectedRef.current = true;
          setEverConnected(true);
        }
        setLastDisconnectAt(null);
        opened = true;
        clearStableTimer();
        // Reset the backoff only once this connection has proven stable.
        stableTimer.current = setTimeout(() => {
          stableTimer.current = null;
          if (disposed || wsRef.current !== ws) return;
          reconnectAttempt.current = 0;
          setReconnectAttemptState(0);
        }, STABLE_CONNECTION_MS);
      };

      ws.onmessage = (e) => {
        if (disposed || wsRef.current !== ws) return;
        try {
          const event = parseAppSocketEvent(JSON.parse(e.data));
          if (event === null) return;
          if (!seenTypes.has(event.type)) {
            seenTypes.add(event.type);
            console.log(`[WS] first message_type=${event.type}`);
          }
          // Benchmark payloads are deliberately content-free. Keep the exact
          // received event in exported logs so model/device routing can be
          // reconstructed without transcript or generated text.
          logBenchmarkEvent(event);
          handlersRef.current.forEach((h) => h(event));
        } catch {
          // ignore malformed messages
        }
      };

      ws.onclose = () => {
        if (disposed || wsRef.current !== ws) return;
        // Only clear state if this is still the active WebSocket.
        // StrictMode double-mount can cause a stale WS onclose to fire
        // after a newer WS is already assigned, clobbering the good reference.
        if (wsRef.current === ws) {
          clearStableTimer();
          setConnected(false);
          // Record when a connected session ends (or the first attempt fails),
          // not each failed reconnect attempt's close. The backoff counter can
          // stay above zero across a brief open, so `opened` marks the start
          // of a new outage.
          if (opened || reconnectAttempt.current === 0) {
            setLastDisconnectAt(Date.now());
          }
          wsRef.current = null;
        }
        // Reset seen-types so the next connection session starts fresh.
        seenTypes.clear();
        if (!disposed) {
          const delay = Math.min(
            RECONNECT_BASE_MS * 2 ** reconnectAttempt.current,
            RECONNECT_MAX_MS
          );
          reconnectAttempt.current++;
          setReconnectAttemptState(reconnectAttempt.current);
          console.warn(`[WS] disconnected reconnect_in_ms=${delay} attempt=${reconnectAttempt.current}`);
          reconnectTimer.current = setTimeout(connect, delay);
        }
      };

      ws.onerror = () => {
        console.error("[WS] connection error");
        ws.close();
      };
    }

    // A hidden webview's backoff timer can stay suspended long after the
    // backend is reachable again: a socket dropped across display sleep once
    // left the hidden HUD disconnected for an hour. Retry immediately on
    // wake, network return, or the page becoming visible or focused.
    function reconnectNow(reason: string) {
      if (disposed) return;
      const state = wsRef.current?.readyState;
      if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) return;
      if (reconnectTimer.current !== null) {
        clearTimeout(reconnectTimer.current);
        reconnectTimer.current = null;
      }
      reconnectAttempt.current = 0;
      setReconnectAttemptState(0);
      console.log(`[WS] reconnect_now reason=${reason}`);
      connect();
    }
    const onOnline = () => reconnectNow("online");
    const onFocus = () => reconnectNow("focus");
    const onVisibility = () => {
      if (document.visibilityState === "visible") reconnectNow("visible");
    };
    window.addEventListener("online", onOnline);
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);
    let unlistenWake: (() => void) | null = null;
    if (isTauri()) {
      listen(SYSTEM_WAKE_EVENT, () => reconnectNow("system_wake"))
        .then((unlisten) => {
          if (disposed) unlisten();
          else unlistenWake = unlisten;
        })
        .catch(() => console.warn("[WS] system wake listener unavailable"));
    }

    connect();

    return () => {
      disposed = true;
      window.removeEventListener("online", onOnline);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
      unlistenWake?.();
      setConnected(false);
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      clearStableTimer();
      const owned = wsRef.current;
      wsRef.current = null;
      owned?.close();
    };
  }, [enabled]);

  return { connected, send, subscribe, everConnected, connectionGeneration, reconnectAttempt: reconnectAttemptState, lastDisconnectAt };
}
