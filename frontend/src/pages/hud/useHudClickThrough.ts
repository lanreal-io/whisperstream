import { useEffect } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { cursorPosition, getCurrentWindow } from "@tauri-apps/api/window";

/** How often the cursor is checked while a reply surface is on screen. A
 *  window that ignores cursor events receives none, so this polls the global
 *  cursor instead of listening for pointer moves. */
export const HUD_CLICK_THROUGH_POLL_MS = 50;
/** Slop around the painted card that still counts as over it. */
export const HUD_CLICK_THROUGH_SLOP = 4;

/**
 * Reply surfaces keep a fixed window taller than their card (geometry.ts,
 * `replySurfaceWindowHeight`). This makes the empty part of that window pass
 * clicks to whatever is underneath (owner requirement, 2026-10-24): the
 * window ignores cursor events whenever the cursor is outside the painted
 * card (`[data-claude-card-measure]`), and takes them again as soon as it is
 * over the card. Disabling it, or leaving the surface, restores ordinary hit
 * testing.
 */
export function useHudClickThrough(enabled: boolean): void {
  useEffect(() => {
    if (!enabled || !isTauri()) return;
    const hudWindow = getCurrentWindow();
    let ignoring = false;
    let stopped = false;
    let busy = false;

    const apply = async (next: boolean) => {
      if (next === ignoring) return;
      ignoring = next;
      try {
        await hudWindow.setIgnoreCursorEvents(next);
      } catch {
        ignoring = !next;
      }
    };

    const tick = async () => {
      if (busy || stopped) return;
      busy = true;
      try {
        const card = document.querySelector<HTMLElement>("[data-claude-card-measure]");
        if (card === null) {
          await apply(false);
          return;
        }
        const [cursor, origin, scale] = await Promise.all([
          cursorPosition(),
          hudWindow.outerPosition(),
          hudWindow.scaleFactor(),
        ]);
        if (stopped) return;
        const x = (cursor.x - origin.x) / scale;
        const y = (cursor.y - origin.y) / scale;
        const rect = card.getBoundingClientRect();
        const over =
          x >= rect.left - HUD_CLICK_THROUGH_SLOP &&
          x <= rect.right + HUD_CLICK_THROUGH_SLOP &&
          y >= rect.top - HUD_CLICK_THROUGH_SLOP &&
          y <= rect.bottom + HUD_CLICK_THROUGH_SLOP;
        await apply(!over);
      } catch {
        // Unknown cursor: keep the card clickable rather than unreachable.
        await apply(false);
      } finally {
        busy = false;
      }
    };

    const interval = window.setInterval(() => void tick(), HUD_CLICK_THROUGH_POLL_MS);
    void tick();
    return () => {
      stopped = true;
      window.clearInterval(interval);
      if (ignoring) void hudWindow.setIgnoreCursorEvents(false).catch(() => undefined);
    };
  }, [enabled]);
}
