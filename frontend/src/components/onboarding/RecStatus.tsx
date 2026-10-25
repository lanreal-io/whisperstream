import type { ReactNode } from "react";
import { motion } from "framer-motion";

// Recording status dot + label shared by the onboarding Test and Enhance
// steps. The dot does the heavy lifting visually; the label is secondary.
//   ready      grey
//   listening  pulsing frost
//   enhancing  pulsing frost (Enhance try-it, after release)
//   got_it     mint
//   miss       rose (Enhance try-it, nothing was transcribed)

export type RecState = "ready" | "listening" | "enhancing" | "got_it" | "miss";

const LABELS: Record<RecState, string> = {
  ready: "Ready",
  listening: "Listening…",
  enhancing: "Enhancing…",
  got_it: "Got it",
  miss: "",
};

const DOT: Record<RecState, { background: string; shadow: string }> = {
  ready: { background: "rgba(148, 163, 184, 0.5)", shadow: "none" },
  listening: { background: "var(--accent-fill)", shadow: "var(--shadow-frost-glow)" },
  enhancing: { background: "var(--accent-fill)", shadow: "var(--shadow-frost-glow)" },
  got_it: { background: "#7EC489", shadow: "0 0 8px rgba(126, 196, 137, 0.55)" },
  miss: { background: "var(--color-rose)", shadow: "none" },
};

export function RecStatus({
  state,
  children,
  live = false,
}: {
  state: RecState;
  /** Replaces the default label (the miss sentence carries a KeyChip). */
  children?: ReactNode;
  /** Announce changes politely (Enhance try-it owns one live region). */
  live?: boolean;
}) {
  const style = DOT[state];
  const pulsing = state === "listening" || state === "enhancing";
  return (
    <div
      className="flex items-center gap-2 text-[11px] text-steel/70"
      role={live ? "status" : undefined}
      aria-live={live ? "polite" : undefined}
      data-rec-state={state}
    >
      <motion.div
        aria-hidden="true"
        className="w-1.5 h-1.5 rounded-full shrink-0"
        style={{ background: style.background, boxShadow: style.shadow }}
        animate={pulsing ? { opacity: [1, 0.4, 1] } : { opacity: 1 }}
        transition={{ duration: 1.2, repeat: pulsing ? Infinity : 0, ease: "easeInOut" }}
      />
      {children ?? LABELS[state]}
    </div>
  );
}
