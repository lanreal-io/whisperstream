import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { LOCKDOWN_ACK_VERSION } from "./LockdownSetupModal";

const dialogShellStyle: React.CSSProperties = {
  background: "var(--surface-modal)",
  backdropFilter: "blur(24px)",
  WebkitBackdropFilter: "blur(24px)",
  border: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass)",
};

const backdropStyle: React.CSSProperties = {
  background: "var(--backdrop-strong)",
  backdropFilter: "blur(40px)",
  WebkitBackdropFilter: "blur(40px)",
};

const glassPillStyle: React.CSSProperties = {
  background: "var(--surface-control)",
  border: "1px solid var(--border-hairline-steel)",
  boxShadow: "inset 0 1px 0 var(--rim-specular)",
};

const gradientPillStyle: React.CSSProperties = {
  background: "var(--accent-fill)",
  boxShadow: "var(--shadow-pearl)",
};

export function needsLockdownScopeReview(
  enabled: boolean,
  acknowledgedVersion: string | null,
): boolean {
  return (
    enabled &&
    acknowledgedVersion !== null &&
    acknowledgedVersion !== LOCKDOWN_ACK_VERSION
  );
}

interface LockdownScopeReviewDialogProps {
  open: boolean;
  onContinue: () => Promise<void>;
  onTurnOff: () => void;
  /** Whether an administrator machine policy forces Lockdown Mode on. A managed
   *  install never sees an offer to turn Lockdown off, so the required review
   *  becomes Continue-only. */
  managed?: boolean;
}

/** One-time review for an older acknowledgment version. This deliberately
 * reviews only the shortened approved scope statement. It never routes an
 * existing Lockdown user back through password setup. */
export function LockdownScopeReviewDialog({
  open,
  onContinue,
  onTurnOff,
  managed = false,
}: LockdownScopeReviewDialogProps) {
  const [submitting, setSubmitting] = useState(false);
  const primaryRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    setSubmitting(false);
    const focusId = setTimeout(() => primaryRef.current?.focus(), 60);

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        return;
      }
      if (event.key !== "Tab") return;

      const buttons = Array.from(
        dialogRef.current?.querySelectorAll<HTMLButtonElement>(
          "button:not([disabled])",
        ) ?? [],
      );
      if (buttons.length === 0) return;

      const first = buttons[0];
      const last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => {
      clearTimeout(focusId);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  if (!open) return null;

  const handleContinue = async () => {
    if (submitting) return;
    setSubmitting(true);
    try {
      await onContinue();
    } catch {
      // Keep the required review open when persistence fails. The next click
      // retries the same versioned acknowledgment without changing state.
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        data-testid="lockdown-scope-review-backdrop"
      />

      <motion.div
        ref={dialogRef}
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[420px] rounded-[var(--radius-card)] p-7"
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="lockdown-scope-review-heading"
        // The authority sentence is part of what this review says, so it joins
        // the accessible description instead of being announced only if the
        // user happens to read past the focused Continue button.
        aria-describedby={
          managed
            ? "lockdown-scope-review-description lockdown-scope-review-managed"
            : "lockdown-scope-review-description"
        }
      >
        <h2
          id="lockdown-scope-review-heading"
          className="font-display text-[18px] font-normal text-slate text-center mb-3"
        >
          Review Lockdown Mode
        </h2>
        <p
          id="lockdown-scope-review-description"
          className={`text-[13px] text-steel leading-relaxed text-center ${
            // The authority line belongs to this paragraph, not to the action
            // row, so close the gap between them and keep the full 24px break
            // before the buttons.
            managed ? "mb-2" : "mb-6"
          }`}
        >
          Lockdown applies to Whisperstream transcription, cleanup, and saved
          history. Safe diagnostic logs stay local unless you export and share
          them.
        </p>

        {managed && (
          <p
            id="lockdown-scope-review-managed"
            className="text-[12px] text-steel mb-6 leading-relaxed text-center"
          >
            Your organization requires Lockdown Mode on this device.
          </p>
        )}

        <div className="flex items-center justify-center gap-2.5">
          {!managed && (
            <button
              onClick={onTurnOff}
              className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
              style={glassPillStyle}
            >
              Turn off Lockdown Mode
            </button>
          )}
          <button
            ref={primaryRef}
            disabled={submitting}
            onClick={() => void handleContinue()}
            className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
            style={gradientPillStyle}
          >
            Continue
          </button>
        </div>
      </motion.div>
    </div>
  );
}

export default LockdownScopeReviewDialog;
