import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MotionConfig, motion } from "framer-motion";
import { exportLogs } from "../lib/api/diagnostics";

/**
 * About > Report a bug. Asks for logs before handing the reporter to GitHub
 * Issues (or email for private sharing), because most reports can't be
 * diagnosed without them.
 *
 * Centered confirm-family card: the dialog family shares no base component,
 * so the shell tokens below are copied on purpose. The caller owns every
 * external handoff (and its fresh Lockdown check); this dialog only exports
 * logs locally through the same path as About's Export logs link.
 */

type Step = "ask" | "preparing" | "saved" | "error" | "denied";

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

const primaryPillStyle = {
  background: "var(--accent-fill)",
  boxShadow: "var(--shadow-pearl)",
};

const glassPillStyle = {
  background: "var(--surface-modal)",
  border: "1px solid var(--border-hairline-steel)",
  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
};

const primaryClass =
  "h-11 w-full rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200 disabled:cursor-default disabled:opacity-75";
const linkClass =
  "mt-3.5 px-2 py-1 text-[12px] text-steel hover:text-slate transition-colors cursor-pointer";

const halo: Record<"frost" | "mint" | "gold", string> = {
  frost: "var(--halo-frost)",
  mint: "var(--halo-mint)",
  gold: "var(--halo-gold)",
};

function StepIcon({ tone }: { tone: "frost" | "mint" | "gold" }) {
  return (
    <div className="relative mb-4 h-12 w-12 flex items-center justify-center" aria-hidden="true">
      <div
        className="absolute inset-0 rounded-full"
        style={{ background: `radial-gradient(circle, ${halo[tone]} 0%, transparent 70%)`, transform: "scale(1.6)" }}
      />
      {tone === "frost" && (
        <svg className="relative" width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="var(--color-frost-deep)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 4v11" />
          <path d="M7.5 10.5L12 15l4.5-4.5" />
          <path d="M4.5 17.5v1.5A1.5 1.5 0 006 20.5h12a1.5 1.5 0 001.5-1.5v-1.5" />
        </svg>
      )}
      {tone === "mint" && (
        <svg className="relative" width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="var(--color-mint)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="8.5" />
          <path d="M8.5 12.2l2.4 2.4 4.8-5" />
        </svg>
      )}
      {tone === "gold" && (
        <svg className="relative" width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="var(--color-gold)" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 4.5l8 14H4z" />
          <path d="M12 10v4" />
          <path d="M12 16.8v.2" />
        </svg>
      )}
    </div>
  );
}

function ExternalLinkIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M6.5 3.5h-3v9h9v-3" />
      <path d="M9.5 2.5h4v4" />
      <path d="M13.5 2.5L7.5 8.5" />
    </svg>
  );
}

interface ReportBugDialogProps {
  open: boolean;
  /** Continue to GitHub Issues, with or without exported logs. */
  onOpenIssues: () => void;
  /** Email logs to support instead of attaching them publicly. */
  onEmail: () => void;
  /** Escape, backdrop click and Close land here. */
  onClose: () => void;
  /** Step shown on open; the dev dialog gallery uses it to show later states. */
  initialStep?: Exclude<Step, "preparing">;
}

export function ReportBugDialog({ open, onOpenIssues, onEmail, onClose, initialStep = "ask" }: ReportBugDialogProps) {
  const [step, setStep] = useState<Step>(initialStep);
  const primaryRef = useRef<HTMLButtonElement>(null);
  const preparing = step === "preparing";

  useEffect(() => {
    if (open) setStep(initialStep);
  }, [open, initialStep]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !preparing) {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, preparing, onClose]);

  // Focus follows the step's primary action. Nothing leaves the device
  // without a second, deliberate click, so the primary is a safe target.
  useEffect(() => {
    if (open && step !== "preparing") primaryRef.current?.focus();
  }, [open, step]);

  if (!open) return null;

  const handleExport = async () => {
    if (preparing) return;
    setStep("preparing");
    try {
      const result = await exportLogs();
      setStep(result === "saved" ? "saved" : result === "denied" ? "denied" : "ask");
    } catch {
      setStep("error");
    }
  };

  const failed = step === "error" || step === "denied";
  const title =
    step === "saved" ? "Logs saved" : failed ? "Couldn't export logs" : "Include your logs";

  return createPortal(
    <MotionConfig reducedMotion="user">
      <div className="fixed inset-0 z-50 flex items-center justify-center">
        <motion.div
          data-testid="report-bug-backdrop"
          aria-hidden="true"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4 }}
          className="absolute inset-0"
          style={backdropStyle}
          onClick={preparing ? undefined : onClose}
        />

        <motion.div
          initial={{ scale: 0.92, opacity: 0, y: 12 }}
          animate={{ scale: 1, opacity: 1, y: 0 }}
          transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
          className="relative flex w-[420px] flex-col items-center rounded-[var(--radius-card)] p-7 text-center"
          style={dialogShellStyle}
          role="dialog"
          aria-modal="true"
          aria-labelledby="report-bug-heading"
          aria-describedby="report-bug-description"
        >
          <StepIcon tone={step === "saved" ? "mint" : failed ? "gold" : "frost"} />

          <h2 id="report-bug-heading" className="mb-3 font-display text-[18px] font-normal text-slate">
            {title}
          </h2>

          <p id="report-bug-description" className="mb-6 max-w-[330px] text-[13px] leading-relaxed text-steel">
            {step === "saved"
              ? "Attach the ZIP to your issue. GitHub attachments are public, so email them instead if you'd rather keep them private."
              : step === "denied"
                ? "Log export is unavailable in Lockdown Mode."
                : step === "error"
                  ? "Try again, or continue and describe the problem without them."
                  : "Logs are usually the only way I can see what went wrong. Export them first, then attach the ZIP to your issue."}
          </p>

          {(step === "ask" || preparing) && (
            <>
              <button
                ref={primaryRef}
                type="button"
                onClick={handleExport}
                disabled={preparing}
                className={primaryClass}
                style={primaryPillStyle}
              >
                {preparing ? "Preparing…" : "Export logs"}
              </button>
              <button type="button" onClick={onOpenIssues} disabled={preparing} className={`${linkClass} disabled:cursor-default disabled:opacity-60`}>
                Continue without logs
              </button>
              <p className="mt-4 text-[11px] text-steel">Whisperstream never uploads your logs.</p>
            </>
          )}

          {step === "saved" && (
            <>
              <button ref={primaryRef} type="button" onClick={onOpenIssues} className={primaryClass} style={primaryPillStyle}>
                <span className="inline-flex items-center gap-2">
                  Continue to GitHub Issues
                  <ExternalLinkIcon />
                </span>
              </button>
              <button
                type="button"
                onClick={onEmail}
                className="mt-2.5 h-10 w-full rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                style={glassPillStyle}
              >
                Email logs instead
              </button>
            </>
          )}

          {step === "error" && (
            <>
              <button ref={primaryRef} type="button" onClick={handleExport} className={primaryClass} style={primaryPillStyle}>
                Try again
              </button>
              <button type="button" onClick={onOpenIssues} className={linkClass}>
                Continue without logs
              </button>
            </>
          )}

          {step === "denied" && (
            <button
              ref={primaryRef}
              type="button"
              onClick={onClose}
              className="h-10 w-full rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
              style={glassPillStyle}
            >
              Close
            </button>
          )}
        </motion.div>
      </div>
    </MotionConfig>,
    document.body,
  );
}
