import { useEffect, useRef } from "react";
import { motion } from "framer-motion";

/* ---------------------------------------------------------------------------
   First-visit local-cleanup nudge (Feature #2). A one-time, in-page frost-glass
   modal (NOT a Tauri popup window) shown the first time a user opens the
   Enhancement tab while on a cloud provider and without having run the GPU
   benchmark, offering to check whether their computer can run on-device
   cleanup. CTA opens the BenchmarkFlow at "intro".

   Shell tokens lifted verbatim from BenchmarkFlow / EnableLocalConfirmDialog per
   the project's "no shared base dialog component" decision.
--------------------------------------------------------------------------- */
const dialogShellStyle = {
  background: "var(--surface-modal)",
  backdropFilter: "blur(24px)",
  WebkitBackdropFilter: "blur(24px)",
  border: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass)",
};

const backdropStyle = {
  background: "var(--backdrop-strong)",
  backdropFilter: "blur(40px)",
  WebkitBackdropFilter: "blur(40px)",
};

const glassPillStyle = {
  background: "var(--surface-modal)",
  border: "1px solid var(--border-hairline-steel)",
  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
};

const gradientPillStyle = {
  background: "var(--accent-fill)",
  boxShadow:
    "var(--shadow-pearl)",
};

interface LocalNudgeDialogProps {
  /** Dismiss without checking. Persists `enhancement_local_nudge_seen` + closes. */
  onDismiss: () => void;
  /** Persists seen + opens BenchmarkFlow at "intro". */
  onCheck: () => void;
  loading?: boolean;
  unavailable?: boolean;
}

export function LocalNudgeDialog({ onDismiss, onCheck, loading = false, unavailable = false }: LocalNudgeDialogProps) {
  const dismissBtnRef = useRef<HTMLButtonElement>(null);

  // Esc dismisses; Enter is intentionally NOT bound (read-first, like the intro).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onDismiss();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onDismiss]);

  useEffect(() => {
    dismissBtnRef.current?.focus();
  }, []);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        onClick={onDismiss}
      />

      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[420px] rounded-[var(--radius-card)] p-7"
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="local-nudge-heading"
      >
        <h2
          id="local-nudge-heading"
          className="font-display text-[18px] font-normal text-slate text-center mb-3"
        >
          Try cleanup on your device
        </h2>

        <p className="text-[13px] text-steel mb-6 leading-relaxed text-center">
          Your computer may be able to clean up dictation on its own, with nothing
          leaving it. Want to check?
        </p>

        {unavailable && (
          <p role="status" className="text-[13px] text-steel mb-4 text-center">
            The model list is unavailable. Try again to check your hardware.
          </p>
        )}
        <div className="flex items-center justify-center gap-2.5">
          <button
            ref={dismissBtnRef}
            onClick={onDismiss}
            className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
            style={glassPillStyle}
          >
            Not now
          </button>
          <button
            onClick={onCheck}
            disabled={loading}
            className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
            style={gradientPillStyle}
          >
            {loading ? "Loading models…" : unavailable ? "Try again" : "Check my hardware"}
          </button>
        </div>
      </motion.div>
    </div>
  );
}
