import { useEffect, useRef } from "react";
import { motion } from "framer-motion";

/* ---------- Disk drive icon ----------
   A single storage cylinder with a high fill level: the volume is nearly
   full, which is the dialog's whole message. (The previous three-platter
   stack read as a pile of gold coins at 48px.) Gold-deep stroke (gold
   halo) signals "caution," matching LowRamDialog. Even though this dialog
   is a hard block (the download would physically fail), red would read as
   "something broke" rather than "you need to act," so we stay in the same
   caution palette as the RAM dialog. */
function DiskDriveIcon() {
  return (
    <div className="relative w-12 h-12 mx-auto mb-4" aria-hidden="true">
      {/* Gold halo */}
      <div
        className="absolute inset-0 rounded-full"
        style={{
          background:
            "radial-gradient(circle, var(--halo-gold) 0%, transparent 70%)",
          transform: "scale(1.6)",
        }}
      />
      <svg viewBox="0 0 48 48" fill="none" className="relative w-full h-full">
        {/* Cylinder outline — settles first */}
        <motion.g
          initial={{ opacity: 0, y: -3 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1, type: "spring", stiffness: 400, damping: 25 }}
          stroke="var(--color-gold-deep)"
          strokeWidth="1.75"
          strokeLinecap="round"
        >
          <ellipse cx="24" cy="15" rx="11" ry="3.25" />
          <line x1="13" y1="15" x2="13" y2="35" />
          <line x1="35" y1="15" x2="35" y2="35" />
          <path d="M13 35 A11 3.25 0 0 0 35 35" />
        </motion.g>

        {/* Fill level — rises in last. The volume reads ~80% full, the
            diegetic "not enough space" hint. */}
        <motion.path
          d="M13 20.5 A11 3.25 0 0 0 35 20.5 L35 35 A11 3.25 0 0 1 13 35 Z"
          fill="var(--color-gold-deep)"
          fillOpacity="0.3"
          initial={{ opacity: 0, scaleY: 0.7 }}
          animate={{ opacity: 1, scaleY: 1 }}
          transition={{ delay: 0.26, type: "spring", stiffness: 600, damping: 26 }}
          style={{ transformOrigin: "24px 35px" }}
        />
        {/* Fill-level surface line */}
        <motion.path
          d="M13 20.5 A11 3.25 0 0 0 35 20.5"
          stroke="var(--color-gold-deep)"
          strokeWidth="1.5"
          strokeLinecap="round"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.3, duration: 0.25 }}
        />
      </svg>
    </div>
  );
}

interface LowDiskSpaceDialogProps {
  /** When false, the component renders nothing (mirrors LowRamDialog). */
  open: boolean;
  /** Free disk space in GB, rendered inline in the body. */
  freeDiskGb: number;
  /** Required disk space in GB. Defaults to 3, parameterized so future
   *  model swaps (or per-quantization variants) can adjust without a
   *  copy edit. Rendered inline in the body. */
  requiredDiskGb?: number;
  /** Primary action (gradient pill, top of stack): open a folder picker
   *  so the user can re-target the download to a roomier drive. */
  onChooseFolder: () => void;
  /** Secondary action (glass pill, middle of stack, conditional): switch
   *  to a cloud provider instead of downloading locally. Rendered only
   *  when showCloudOption is true. */
  onSwitchToCloud: () => void;
  /** Tertiary action / Esc / backdrop: close the dialog, no side effect.
   *  Unlike LowRamDialog this dialog has no "proceed anyway" path — the
   *  download would physically fail, so dismiss === free-up-space-and-retry.
   *  Keeps the focused dismissBtnRef to preserve the read-before-act
   *  convention. */
  onDismiss: () => void;
  /** When false, the Gemini fallback is hidden. Use for the mandatory
   *  Parakeet/ASR download (no cloud fallback for speech). Defaults to true. */
  showCloudOption?: boolean;
}

export function LowDiskSpaceDialog({
  open,
  freeDiskGb,
  requiredDiskGb = 3,
  onChooseFolder,
  onSwitchToCloud,
  onDismiss,
  showCloudOption = true,
}: LowDiskSpaceDialogProps) {
  const dismissBtnRef = useRef<HTMLButtonElement>(null);

  // Esc dismisses. Enter is intentionally NOT bound — matches LowRamDialog
  // and PrivacyModal's affirmative-action convention.
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

  // Focus the secondary (Free up space) button on mount. Same focus spec
  // as LowRamDialog: user must read before they can keyboard-confirm into
  // cloud setup.
  useEffect(() => {
    if (!open) return;
    dismissBtnRef.current?.focus();
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

      {/* Glass card — same shell as LowRamDialog (420px, rounded-3xl, p-7,
          identical glass / inset / shadow stack). The shell-level glass
          treatment is duplicated verbatim from LowRamDialog /
          PrivacyModal / DataLeavesDialog per design.md's "no shared base
          component" decision. */}
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
        aria-labelledby="low-disk-dialog-heading"
      >
        <DiskDriveIcon />

        <h2
          id="low-disk-dialog-heading"
          className="font-display text-[18px] font-normal text-slate text-center mb-2"
        >
          Not enough space to download
        </h2>

        <p className="text-[13px] text-steel mb-5 leading-relaxed text-center">
          Whisperstream needs about{" "}
          <span style={{ fontVariantNumeric: "tabular-nums" }}>
            {requiredDiskGb}
          </span>{" "}
          GB for the on-device model, but this drive only has{" "}
          <span style={{ fontVariantNumeric: "tabular-nums" }}>
            {freeDiskGb.toFixed(1)}
          </span>{" "}
          GB free.
        </p>

        {/* Vertical 3-action stack. Primary (gradient) is the folder choice —
            the recommended path for a user who wants on-device models.
            Cloud option (glass) is conditional: present for GGUF, absent for
            the mandatory ASR download (speech has no cloud fallback).
            Dismiss (tertiary text) keeps the focus ref so the user must read
            before keyboard-confirming; this preserves the read-before-act
            convention from the original 2-up layout. */}
        <div className="flex flex-col gap-2.5 mb-3">
          <button
            onClick={onChooseFolder}
            className="w-full py-2.5 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
            style={{
              background: "var(--accent-fill)",
              boxShadow:
                "var(--shadow-pearl)",
            }}
          >
            Choose a different folder
          </button>

          {showCloudOption && (
            <button
              onClick={onSwitchToCloud}
              className="w-full py-2.5 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
              style={{
                background: "var(--surface-modal)",
                border: "1px solid var(--border-emphasis)",
                boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
              }}
            >
              Use Gemini's free tier
            </button>
          )}

          <button
            ref={dismissBtnRef}
            onClick={onDismiss}
            className="w-full py-2 rounded-full text-[13px] font-medium text-steel hover:text-slate hover:bg-white/40 cursor-pointer transition-all duration-200"
          >
            Free up space and retry
          </button>
        </div>
      </motion.div>
    </div>
  );
}
