import { useEffect, useRef } from "react";
import { motion } from "framer-motion";

/* ---------- Dialog shell tokens ----------
   Copied from EnableLocalConfirmDialog.tsx ("Copy
   EnableLocalConfirmDialog.tsx as the template" — the dialog family deliberately
   does NOT share a base; the repeated card/backdrop tokens are copied on
   purpose). Same frost-glass shell + backdrop + entrance spring + focus-Cancel
   + Esc/backdrop-cancel convention. The ONE delta: the primary pill is the
   DESTRUCTIVE rose treatment (not the frost gradient), so "Delete" never reads
   like the benign "Continue". Used by the three transcripts confirms
   (retention-shrink, clear-all, per-card delete). */
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

/* DESTRUCTIVE primary — rose, NOT the frost gradient (design.md LOCKED).
   color-mix() blends var(--color-rose) so dark theme brightens correctly
   (Light --color-rose = #C48B8B; Dark --color-rose = #D9A0A0). */
const destructivePillStyle = {
  background: "color-mix(in srgb, var(--color-rose) 18%, transparent)",
  border: "1px solid color-mix(in srgb, var(--color-rose) 45%, transparent)",
  color: "color-mix(in srgb, var(--color-rose) 70%, #3A1515)",
};

interface TranscriptConfirmDialogProps {
  /** When false, the component renders nothing. Mirrors EnableLocalConfirmDialog. */
  open: boolean;
  /** Dialog headline (LOCKED copy from copy-review.md). */
  headline: string;
  /** One-line consequence body (LOCKED copy from copy-review.md). */
  body: string;
  /** Destructive verb on the rose primary pill (e.g. "Delete all", "Delete
   *  older", "Delete"). */
  confirmLabel: string;
  /** Primary action (rose pill, right side): user confirms the deletion. */
  onConfirm: () => void;
  /** Secondary action (glass pill, left side) AND Esc / backdrop click: user
   *  backs out. Nothing is deleted. */
  onCancel: () => void;
}

export function TranscriptConfirmDialog({
  open,
  headline,
  body,
  confirmLabel,
  onConfirm,
  onCancel,
}: TranscriptConfirmDialogProps) {
  const cancelBtnRef = useRef<HTMLButtonElement>(null);

  // Esc dismisses. Enter is intentionally NOT bound — the user must read the
  // consequence before keyboard-confirming a destructive action. Matches the
  // EnableLocalConfirmDialog "read before you confirm" convention.
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

  // Focus the Cancel button on open so a stray Enter does not delete; the user
  // is guided to read the consequence first.
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

      {/* Glass card — 420px wide. No decorative icon. Headline + body + centered
          Cancel / destructive pills. */}
      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[420px] rounded-[var(--radius-card)] p-7"
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="transcript-confirm-heading"
      >
        <h2
          id="transcript-confirm-heading"
          className="font-display text-[18px] font-normal text-slate text-center mb-3"
        >
          {headline}
        </h2>

        <p className="text-[13px] text-steel mb-6 leading-relaxed text-center">
          {body}
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
            onClick={onConfirm}
            className="px-5 py-2 rounded-full text-[13px] font-medium cursor-pointer transition-all duration-200"
            style={destructivePillStyle}
          >
            {confirmLabel}
          </button>
        </div>
      </motion.div>
    </div>
  );
}
