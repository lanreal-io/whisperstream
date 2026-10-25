/**
 * LockdownManageDialog — the when-ON management surface (Surface A, round 01).
 *
 * Opened by the "Manage" button on the Home Lockdown row once Lockdown Mode is
 * on. Replaces the old inline AnimatePresence sub-controls with a dialog on the
 * EnableLocalConfirmDialog shell (frost card, w-[440px], rounded-3xl p-7, spring
 * entrance, backdrop blur). The dialog family has NO
 * shared base — this card hand-rolls its overlay/card from the shared tokens
 * (do NOT DRY into a <Dialog>).
 *
 * TWO variants, keyed off `hasEnvelope` (from useLockdown status):
 *  - Full  (keep-history lockdown, an envelope exists): SealPanel "Protections
 *    active" + auto-lock idle DurationSelect + Change password / View recovery
 *    code + rose data-loss reminder + HIPAA PDF link + Turn off Lockdown Mode.
 *
 * A third axis, `managed`, crosses both variants: when an administrator machine
 * policy owns Lockdown Mode the turn-off action is removed and replaced with a
 * quiet policy note. Every other action (password, recovery code, idle lock,
 * start saving transcripts) stays available unless the independent no-history
 * policy prohibits saving. The administrator does not
 * choose or escrow the user's transcript password.
 *  - Slim  (no-envelope / history-off lockdown): slim SealPanel (two
 *    no-transcripts bullets) + HIPAA PDF link + Start saving transcripts + Turn
 *    off Lockdown Mode. No idle-lock / change-password / view-recovery (there is
 *    no password).
 *
 * All action buttons defer to callbacks so the PARENT (Home) owns the child
 * dialogs (change-password, view-recovery, disable) and the setup modal — this
 * component only renders the management surface and reports intent. All copy is
 * verbatim from copy-review.md section A. No em dashes.
 */

import { useEffect } from "react";
import { motion } from "framer-motion";
import { SealPanel } from "./SealPanel";
import { DurationSelect } from "./DurationSelect";
import { SecondaryButton } from "./SecondaryButton";
import { HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE } from "../lib/api/lockdown";

/* ---------- Dialog shell tokens ----------
   Copied from EnableLocalConfirmDialog.tsx per the "no shared base, copy the
   tokens" rule. */
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

/** The HIPAA summary PDF link, shared by both variants (matches the setup
 *  modal's link treatment: underline, frost-deep). */
function HipaaPdfLink() {
  return (
    <a
      href="/hipaa-summary.pdf"
      target="_blank"
      rel="noopener noreferrer"
      className="text-[12px] underline underline-offset-2 cursor-pointer transition-colors"
      style={{ color: "var(--color-frost-deep)" }}
    >
      Download the HIPAA summary (PDF)
    </a>
  );
}

/** Managed-policy explanation shown in place of the turn-off action when an
 *  administrator machine policy owns Lockdown Mode. A plain text line, not a
 *  framed row: `--surface-control` + `--border-hairline` is the system's
 *  *interactive* control recipe, and wearing it in the exact slot the turn-off
 *  button just vacated would read as a disabled button. Policy is also not an
 *  error state, so this stays steel rather than rose. Copy is verbatim from
 *  copy-review.md (managed Manage-dialog option B). */
function ManagedPolicyNote() {
  return (
    <p className="text-[11px] text-steel leading-relaxed">
      Your organization requires Lockdown Mode on this device.
    </p>
  );
}

/** Small X close glyph (top-right corner). */
function CloseIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M2.5 2.5l7 7M9.5 2.5l-7 7" />
    </svg>
  );
}

interface LockdownManageDialogProps {
  /** When false, the component renders nothing. */
  open: boolean;
  /** Whether an administrator machine policy forces Lockdown Mode on. When
   *  true the turn-off action is REMOVED (not disabled) from both variants so
   *  no surface offers a change the backend refuses, and a quiet policy note
   *  takes its place. Every other management action stays available. */
  managed?: boolean;
  /** Machine policy prohibits saving even if Lockdown itself is voluntary. */
  historyDisabled?: boolean;
  /** Whether a password-wrapped key envelope exists. Picks the full (true) vs
   *  slim (false) variant. Sourced from useLockdown status (has_envelope). */
  hasEnvelope: boolean;
  /** Current idle auto-lock window in seconds (0 = Never). Full variant only. */
  idleLockSeconds: number;
  /** Persist a new idle auto-lock window (writes lockdown_idle_lock_seconds).
   *  Full variant only. */
  onIdleLockChange: (seconds: number) => void;
  /** Open the change-password dialog (full variant). Parent closes Manage first. */
  onChangePassword: () => void;
  /** Open the view/regenerate recovery key dialog (full variant). */
  onViewRecovery: () => void;
  /** Open the setup modal so a no-envelope lockdown can add a password/envelope
   *  (slim variant). */
  onStartSaving: () => void;
  /** Open the disable dialog (both variants). */
  onDisable: () => void;
  /** Dismiss the dialog (Esc / backdrop / X). Nothing is persisted. */
  onClose: () => void;
}

export function LockdownManageDialog({
  open,
  managed = false,
  historyDisabled = false,
  hasEnvelope,
  idleLockSeconds,
  onIdleLockChange,
  onChangePassword,
  onViewRecovery,
  onStartSaving,
  onDisable,
  onClose,
}: LockdownManageDialogProps) {
  // Esc dismisses.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        onClick={onClose}
      />

      {/* Glass card — w-[440px]. */}
      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[440px] rounded-[var(--radius-card)] p-7"
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="manage-lockdown-heading"
      >
        {/* Header — feature title + dismiss. */}
        <div className="flex items-center justify-between mb-4">
          <h2
            id="manage-lockdown-heading"
            className="font-display text-[16px] font-normal text-slate"
          >
            Lockdown Mode
          </h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="w-7 h-7 -mr-1 rounded-full flex items-center justify-center text-steel/60 hover:text-slate transition-colors cursor-pointer"
          >
            <CloseIcon />
          </button>
        </div>

        {hasEnvelope ? (
          /* ---------------- Full variant (keep-history, envelope) ---------- */
          <div className="space-y-4">
            {/* Seal panel — hardened-status checklist (2c strings verbatim). */}
            <SealPanel
              compact
              items={[
                historyDisabled
                  ? "On-device transcription and cleanup. History is disabled."
                  : "On-device transcription and cleanup. Saved history locked.",
              ]}
            />

            {/* Auto-lock idle timeout row (A7 strings verbatim). */}
            <div className="flex items-center justify-between gap-4">
              <div className="min-w-0">
                <div className="text-[13px] font-medium text-slate">
                  Lock transcripts after idle
                </div>
                <div className="text-[11px] text-steel mt-0.5">
                  Transcripts re-lock after this much inactivity.
                </div>
              </div>
              <DurationSelect
                value={idleLockSeconds}
                onChange={onIdleLockChange}
                ariaLabel="Lock transcripts after idle"
              />
            </div>

            {/* Action buttons (A8 strings verbatim). */}
            <div className="flex items-center gap-2">
              <SecondaryButton
                onClick={onChangePassword}
                className="h-9 px-3.5 rounded-full text-[12px] font-medium text-slate"
              >
                Change password
              </SecondaryButton>
              <SecondaryButton
                onClick={onViewRecovery}
                className="h-9 px-3.5 rounded-full text-[12px] font-medium text-slate"
              >
                View recovery key
              </SecondaryButton>
            </div>

            {/* Data-loss reminder (A9 string verbatim, rose). */}
            <p
              className="text-[11px] leading-relaxed"
              style={{ color: "color-mix(in srgb, var(--color-rose) 80%, transparent)" }}
            >
              If you lose both your password and recovery key, your history
              can't be restored. Reset deletes it instead.
            </p>

            {/* HIPAA summary PDF link. */}
            <div>
              <HipaaPdfLink />
            </div>

            {/* Footer: managed policy note, or disable (rose destructive). */}
            <div className="pt-1">
              {managed ? (
                <ManagedPolicyNote />
              ) : (
                <SecondaryButton
                  variant="destructive"
                  onClick={onDisable}
                  className="w-full h-10 rounded-full text-[13px] font-medium"
                  style={{ color: "var(--color-rose)" }}
                >
                  Turn off Lockdown Mode
                </SecondaryButton>
              )}
            </div>
          </div>
        ) : (
          /* ---------------- Slim variant (no-envelope, history-off) -------- */
          <div className="space-y-4">
            {/* Slim seal panel — the two no-transcripts bullets. */}
            <SealPanel
              compact
              items={[
                historyDisabled
                  ? "On-device transcription and cleanup. History is disabled."
                  : "On-device transcription and cleanup. Saved history locked.",
              ]}
            />

            {/* HIPAA summary PDF link. */}
            <div>
              <HipaaPdfLink />
            </div>

            {/* Machine policy removes the unavailable password-setup action. */}
            {historyDisabled ? (
              <p className="text-[12px] text-steel leading-relaxed">
                {HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE}
              </p>
            ) : <SecondaryButton
              onClick={onStartSaving}
              className="w-full h-9 px-3.5 rounded-full text-[12px] font-medium text-slate"
            >
              Start saving transcripts
            </SecondaryButton>}

            {/* Footer: managed policy note, or disable (rose destructive). */}
            <div className="pt-1">
              {managed ? (
                <ManagedPolicyNote />
              ) : (
                <SecondaryButton
                  variant="destructive"
                  onClick={onDisable}
                  className="w-full h-10 rounded-full text-[13px] font-medium"
                  style={{ color: "var(--color-rose)" }}
                >
                  Turn off Lockdown Mode
                </SecondaryButton>
              )}
            </div>
          </div>
        )}
      </motion.div>
    </div>
  );
}

export default LockdownManageDialog;
