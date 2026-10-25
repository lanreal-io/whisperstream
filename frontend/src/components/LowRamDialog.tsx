import { useEffect, useRef } from "react";
import { motion } from "framer-motion";

/* ---------- Memory ring icon ----------
   Gold-deep stroke (gold-mist aura) signals "caution," not "error." A
   warning triangle would read as a fatal problem; this is a heads-up. */
export function MemoryRingIcon() {
  return (
    <div className="relative w-12 h-12 mx-auto mb-4" aria-hidden="true">
      {/* Gold-mist aura */}
      <div
        className="absolute inset-0 rounded-full"
        style={{
          background:
            "radial-gradient(circle, var(--halo-gold) 0%, transparent 70%)",
          transform: "scale(1.6)",
        }}
      />
      <svg
        viewBox="0 0 48 48"
        fill="none"
        className="relative w-full h-full"
      >
        {/* Memory chip outline */}
        <motion.rect
          x="12"
          y="14"
          width="24"
          height="20"
          rx="3"
          fill="none"
          stroke="var(--color-gold-deep)"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
          initial={{ scale: 0.85, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ delay: 0.1, type: "spring", stiffness: 400, damping: 25 }}
          style={{ transformOrigin: "24px 24px" }}
        />
        {/* Pin marks across the bottom */}
        <motion.g
          stroke="var(--color-gold-deep)"
          strokeWidth="1.75"
          strokeLinecap="round"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.22 }}
        >
          <line x1="17" y1="34" x2="17" y2="38" />
          <line x1="22" y1="34" x2="22" y2="38" />
          <line x1="26" y1="34" x2="26" y2="38" />
          <line x1="31" y1="34" x2="31" y2="38" />
          <line x1="17" y1="10" x2="17" y2="14" />
          <line x1="22" y1="10" x2="22" y2="14" />
          <line x1="26" y1="10" x2="26" y2="14" />
          <line x1="31" y1="10" x2="31" y2="14" />
        </motion.g>
        {/* Inner ring for the "memory ring" identity */}
        <motion.circle
          cx="24"
          cy="24"
          r="4"
          fill="none"
          stroke="var(--color-gold-deep)"
          strokeWidth="1.75"
          initial={{ opacity: 0, scale: 0.6 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0.28, type: "spring", stiffness: 600, damping: 26 }}
          style={{ transformOrigin: "24px 24px" }}
        />
      </svg>
    </div>
  );
}

interface LowRamDialogProps {
  /** When false, the component renders nothing (mirrors the existing modal
   *  pattern where the parent guards with ``{open && <... />}`` or with a
   *  ``open`` prop). */
  open: boolean;
  /** Detected system RAM in GiB, rendered inline in the body. The model's
   *  required RAM is intentionally NOT surfaced: copy stays vague about
   *  the on-device model's footprint while still being transparent about
   *  the user's own hardware. */
  systemRamGb: number;
  /** Primary action (gradient pill, right side): expand Advanced and close. */
  onSwitchToCloud: () => void;
  /** Secondary action (glass pill, left side): proceed to DownloadDialog. */
  onContinue: () => void;
  /** Esc / backdrop click: close the dialog, no side effect. */
  onDismiss: () => void;
}

export function LowRamDialog({
  open,
  systemRamGb,
  onSwitchToCloud,
  onContinue,
  onDismiss,
}: LowRamDialogProps) {
  const continueBtnRef = useRef<HTMLButtonElement>(null);

  // Esc dismisses. Enter is intentionally NOT bound — the user must read
  // the dialog before they can keyboard-confirm into cloud setup. Matches
  // PrivacyModal's affirmative-action convention.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onDismiss();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onDismiss]);

  // Focus the secondary (Download anyway) button on mount per the focus spec
  // in dialog-design.md: we want the user to read the dialog before being
  // able to keyboard-confirm into cloud setup.
  useEffect(() => {
    if (!open) return;
    continueBtnRef.current?.focus();
  }, [open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={{
          background: "var(--backdrop-strong)",
          backdropFilter: "blur(40px)",
          WebkitBackdropFilter: "blur(40px)",
        }}
        onClick={onDismiss}
      />

      {/* Glass card — narrower than PrivacyModal (420 vs 460), no
          enumerated list to host. The shell-level glass treatment is
          duplicated verbatim from PrivacyModal / DataLeavesDialog per
          design.md's "no shared base component" decision. */}
      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[420px] rounded-[var(--radius-card)] p-7"
        style={{
          background: "var(--surface-modal)",
          backdropFilter: "blur(24px)",
          WebkitBackdropFilter: "blur(24px)",
          border: "1px solid var(--border-emphasis)",
          boxShadow: "var(--shadow-glass)",
        }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="low-ram-dialog-heading"
      >
        <MemoryRingIcon />

        <h2
          id="low-ram-dialog-heading"
          className="font-display text-[18px] font-normal text-slate text-center mb-2"
        >
          Your machine may not have enough memory
        </h2>

        <p className="text-[13px] text-steel mb-5 leading-relaxed text-center">
          Your machine has{" "}
          <span style={{ fontVariantNumeric: "tabular-nums" }}>{systemRamGb}</span>{" "}
          GB of RAM. The on-device model needs additional memory to run
          smoothly, so cleanup may be slow or stutter.
        </p>

        {/* justify-center: both buttons read as equals, gradient pill carries
            the recommendation. Different from PrivacyModal / Dialog B which
            use justify-end. */}
        <div className="flex items-center justify-center gap-2.5 mb-3">
          <button
            ref={continueBtnRef}
            onClick={onContinue}
            className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
            style={{
              background: "var(--surface-modal)",
              border: "1px solid var(--border-emphasis)",
              boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
            }}
          >
            Download anyway
          </button>
          <button
            onClick={onSwitchToCloud}
            className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
            style={{
              background: "var(--accent-fill)",
              boxShadow:
                "var(--shadow-pearl)",
            }}
          >
            Use Gemini's free tier
          </button>
        </div>
      </motion.div>
    </div>
  );
}
