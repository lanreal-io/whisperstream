import { useEffect, useRef } from "react";
import { motion } from "framer-motion";

/* ---------- Dialog shell tokens ----------
   Lifted verbatim from design.md `## Reference Code`. The frost-glass
   dialog vocabulary is shared by LowRamDialog / DataLeavesDialog /
   PrivacyModal. Per LOCKED Decision 4-A the dialog is minimal: headline +
   body + Cancel/Continue pills, no decorative top-center icon. */
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

interface EnableLocalConfirmDialogProps {
  /** When false, the component renders nothing. Mirrors LowRamDialog. */
  open: boolean;
  /** Primary action (gradient pill, right side): user accepts the
   *  on-device trade-off. Parent kicks off the existing download chain. */
  onContinue: () => void;
  /** Secondary action (glass pill, left side) AND Esc / backdrop click:
   *  user backs out. Toggle stays off, no download fires. */
  onCancel: () => void;
}

export function EnableLocalConfirmDialog({
  open,
  onContinue,
  onCancel,
}: EnableLocalConfirmDialogProps) {
  const cancelBtnRef = useRef<HTMLButtonElement>(null);

  // Esc dismisses. Enter is intentionally NOT bound — the user must
  // read the RAM/delay note before keyboard-confirming. Matches
  // LowRamDialog's "user must read first" convention.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  // Focus the Cancel button on open per Task 2.1 spec: the user
  // expressed intent (toggled on) but should be guided to read, not
  // mash Enter.
  useEffect(() => {
    if (!open) return;
    cancelBtnRef.current?.focus();
  }, [open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        onClick={onCancel}
      />

      {/* Glass card — 420px wide per LOCKED Decision 4-A. No decorative
          icon. Headline + body + centered Cancel/Continue pills. */}
      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[420px] rounded-[var(--radius-card)] p-7"
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="enable-local-heading"
      >
        <h2
          id="enable-local-heading"
          className="font-display text-[18px] font-normal text-slate text-center mb-3"
        >
          Enable enhancement
        </h2>

        <p className="text-[13px] text-steel mb-6 leading-relaxed text-center">
          Cleanup uses a local AI model, so it needs extra memory and adds
          a brief delay to each transcription.
        </p>

        <div className="flex items-center justify-center gap-2.5">
          <button
            ref={cancelBtnRef}
            onClick={onCancel}
            className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
            style={glassPillStyle}
          >
            Cancel
          </button>
          <button
            onClick={onContinue}
            className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
            style={gradientPillStyle}
          >
            Continue
          </button>
        </div>
      </motion.div>
    </div>
  );
}
