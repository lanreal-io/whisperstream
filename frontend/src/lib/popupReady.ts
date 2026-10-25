import { useEffect } from "react";
import { errorType } from "./logCapture";

/**
 * Emits `<label>:ready` after the first paint commits (double-RAF) so the
 * parent window's `showPopup()` can wait for actual render before calling
 * `WebviewWindow.show()`. This eliminates the Tauri v2 + WebView2
 * transparent-window paint race on Windows.
 *
 * Skips silently when:
 * - Not running inside a Tauri context (pure-Vite dev or Playwright).
 * - The current window label is `"main"` (main window never goes through
 *   `showPopup()` and should not emit `:ready`).
 */
export function usePopupReady(): void {
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        const { emit } = await import("@tauri-apps/api/event");

        const label = getCurrentWindow().label;

        if (label === "main" || !label.startsWith("popup-")) {
          console.log(`[Popup] ready skipped reason=main_window label=${label}`);
          return;
        }

        // Double-RAF: the inner callback fires after the paint that follows
        // the outer RAF, guaranteeing the opaque card has committed to the
        // compositor before we signal the parent.
        requestAnimationFrame(() => {
          requestAnimationFrame(async () => {
            if (cancelled) return;
            try {
              await emit(`${label}:ready`);
              console.log(`[Popup] ready emitted label=${label}`);
            } catch (err) {
              console.error(`[Popup] ready emit failed label=${label} error_type=${errorType(err)}`);
            }
          });
        });
      } catch {
        // Not in a Tauri context (pure-Vite dev, Playwright, etc.) — bail silently.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);
}
