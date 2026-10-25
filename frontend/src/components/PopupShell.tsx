import { useEffect, useState, type ReactNode } from "react";
import { motion } from "framer-motion";
import { usePopupReady } from "../lib/popupReady";

interface PopupShellProps {
  children: ReactNode;
  width: number; // card width in px
}

export function PopupShell({ children, width }: PopupShellProps) {
  const uiQaStateId =
    import.meta.env.DEV &&
    window.location.pathname === "/debug/ui-qa"
      ? new URLSearchParams(window.location.search).get("state")
      : null;
  const [uiQaReady, setUiQaReady] = useState(false);

  // Set transparent backgrounds on mount so the Tauri transparent window
  // shows through behind the glass card (same pattern as HUD.tsx).
  useEffect(() => {
    document.documentElement.style.setProperty("background", "transparent", "important");
    document.body.style.setProperty("background", "transparent", "important");
  }, []);

  // Emit `<label>:ready` after first paint so showPopup() can wait for
  // actual render before calling WebviewWindow.show(). Must be called
  // AFTER the transparency effect above so the document-style mutations
  // enter the same commit before the double-RAF fires.
  usePopupReady();

  useEffect(() => {
    if (uiQaStateId === null) return;
    let secondFrame = 0;
    const firstFrame = requestAnimationFrame(() => {
      secondFrame = requestAnimationFrame(() => setUiQaReady(true));
    });
    return () => {
      cancelAnimationFrame(firstFrame);
      if (secondFrame !== 0) cancelAnimationFrame(secondFrame);
    };
  }, [uiQaStateId]);

  return (
    <div
      className="w-full h-full flex items-center justify-center"
      style={{ background: "transparent" }}
      data-ui-qa-root={uiQaStateId ?? undefined}
      data-ui-qa-state={uiQaStateId ?? undefined}
      data-ui-qa-ready={
        uiQaStateId === null ? undefined : uiQaReady ? "true" : "false"
      }
    >
      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative rounded-3xl p-7"
        style={{
          width,
          // Popups float over the desktop without a backdrop. Keep their fill
          // distinct from in-app modals so the light card remains visible over
          // white windows while both themes stay near-opaque and legible.
          background: "var(--surface-popup)",
          border: "1px solid var(--border-emphasis)",
          boxShadow:
            "inset 0 1px 0 var(--rim-specular), inset 0 -1px 0 rgba(255, 255, 255, 0.2)",
        }}
        data-ui-qa-surface={uiQaStateId === null ? undefined : "popup-card"}
        data-ui-qa-floating={uiQaStateId === null ? undefined : "popup-card"}
      >
        {children}
      </motion.div>
    </div>
  );
}
