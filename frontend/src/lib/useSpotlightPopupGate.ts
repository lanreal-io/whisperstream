import { useEffect, useState } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { WebviewWindow } from "@tauri-apps/api/webviewWindow";

const POPUPS = ["popup-eula", "popup-error", "popup-review", "popup-update"];
const RECOVERY_MS = 15000;

/** Show intent blocks immediately; physical completion and occasional recovery
 * replace repeated visibility queries. Disabled offers own no observers. */
export function useSpotlightPopupGate(enabled = true) {
  const [state, setState] = useState({ enabled: false, clear: !isTauri() });
  useEffect(() => {
    if (!enabled || !isTauri()) return;
    let cancelled = false;
    let running = false;
    let dirty = false;
    let inspectAll = false;
    let revision = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const active = new Set<string>();
    const pending = new Map<string, number>();
    const publish = () => {
      if (cancelled) return;
      setState({ enabled: true, clear: active.size === 0 && pending.size === 0 });
      clearTimeout(timer);
      if (active.size || pending.size) {
        const nextPending = Math.min(...pending.values());
        const delay = pending.size ? Math.max(0, nextPending - Date.now()) : RECOVERY_MS;
        timer = setTimeout(() => void inspect(false), Math.min(delay, RECOVERY_MS));
      }
    };
    const inspect = async (all: boolean) => {
      inspectAll ||= all;
      dirty = true;
      if (running || cancelled) return;
      running = true;
      try {
        while (dirty && !cancelled) {
          dirty = false;
          const owner = revision;
          const labels = inspectAll ? POPUPS : [...new Set([...active, ...pending.keys()])];
          inspectAll = false;
          const rows = await Promise.all(labels.map(async (label) => {
            try { return [label, (await (await WebviewWindow.getByLabel(label))?.isVisible()) ?? false] as const; }
            catch { return [label, true] as const; }
          }));
          if (cancelled) return;
          if (owner !== revision) { dirty = true; continue; }
          for (const [label, visible] of rows) {
            if ((pending.get(label) ?? 0) <= Date.now()) pending.delete(label);
            if (visible) active.add(label); else active.delete(label);
          }
          publish();
        }
      } finally { running = false; }
    };
    setState({ enabled: true, clear: false });
    const listeners = POPUPS.flatMap((label) => [
      listen(`${label}:show`, () => {
        revision += 1;
        pending.set(label, Date.now() + 2000);
        publish();
      }),
      listen(`${label}:shown`, () => {
        revision += 1;
        pending.delete(label);
        active.add(label);
        publish();
      }),
      listen(`${label}:hidden`, () => {
        revision += 1;
        pending.delete(label);
        active.delete(label);
        publish();
      }),
    ].map((listener) => listener.catch(() => () => {})));
    const recover = () => { void inspect(true); };
    const onVisibility = () => { if (document.visibilityState === "visible") recover(); };
    window.addEventListener("focus", recover);
    document.addEventListener("visibilitychange", onVisibility);
    void Promise.all(listeners).then(() => inspect(true));
    return () => {
      cancelled = true;
      clearTimeout(timer);
      window.removeEventListener("focus", recover);
      document.removeEventListener("visibilitychange", onVisibility);
      for (const listener of listeners) void listener.then((unlisten) => unlisten());
      setState({ enabled: false, clear: false });
    };
  }, [enabled]);
  return !enabled || !isTauri() || (state.enabled && state.clear);
}
