import {
  passwordInputClassName as inputBase,
  passwordInputRestingBorderColor,
  passwordInputStyle as inputBaseStyle,
} from "./passwordInputRecipe";
/**
 * ChangePasswordDialog — change-password action (Surface A item 3).
 * ViewRecoveryCodeDialog — view/regenerate the recovery key (Surface A item 3).
 *
 * Both live here because they are small and closely related (both gate on
 * the current password, both use the same shell tokens).
 *
 * The dialog family has NO shared base — each card
 * hand-rolls its overlay/card from the EnableLocalConfirmDialog tokens
 * (do NOT DRY into a <Dialog>).
 *
 * ChangePasswordDialog: current + new password fields -> POST /api/lockdown/change-password.
 * ViewRecoveryCodeDialog: password re-entry -> POST /api/lockdown/view-recovery which
 *   REGENERATES + returns a new key (regenerate-on-view decision, decisions.md);
 *   shows it on the shared RecoveryStep (Copy / Print / Download + checkbox
 *   gate, no optional password). The wire field stays `recovery_code`.
 *
 * All copy from copy-review.md `## Locked Copy Decisions`. No em dashes.
 */

import { useState, useEffect, useRef } from "react";
import { motion } from "framer-motion";
import { changeLockdownPassword, viewRecoveryCode } from "../lib/api/lockdown";
import { RecoveryPasswordFields } from "./RecoveryPasswordFields";
import { RecoveryAbandonPrompt, RecoveryGateButton, RecoveryStep } from "./RecoveryStep";
import {
  checkNewPassword,
  passwordRuleErrorFromResponse,
  passwordRuleMessage,
} from "../lib/passwordRule";

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

const glassPillStyle: React.CSSProperties = {
  background: "var(--surface-modal)",
  border: "1px solid var(--border-hairline-steel)",
  // Caught-light top rim, theme-invariant (mirrors EnableLocalConfirmDialog).
  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
};

const gradientPillStyle: React.CSSProperties = {
  background: "var(--accent-fill)",
  boxShadow: "var(--shadow-pearl)",
};

// Preserve the existing focus/reset behavior; the field surface is shared.
const onInputFocus = (e: React.FocusEvent<HTMLInputElement>) => {
  e.currentTarget.style.outline = "";
};
const onInputBlur = (e: React.FocusEvent<HTMLInputElement>) => {
  e.currentTarget.style.background = "var(--surface-control)";
  e.currentTarget.style.boxShadow = "inset 0 2px 4px rgba(0, 0, 0, 0.02)";
  e.currentTarget.style.borderColor = passwordInputRestingBorderColor;
};

/** Lock glyph — same trailing-icon recipe as LockdownSetupModal.tsx. */
function LockIcon({ className }: { className?: string }) {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <rect x="2" y="5.5" width="8" height="5.5" rx="1.5" />
      <path d="M4 5.5V3.5a2 2 0 014 0v2" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// ChangePasswordDialog
// ---------------------------------------------------------------------------

interface ChangePasswordDialogProps {
  /** When false, the component renders nothing. */
  open: boolean;
  /** Called on Cancel / Esc / backdrop click. Nothing is persisted. */
  onCancel: () => void;
  /** Called after a successful password change. */
  onChanged: () => void;
}

export function ChangePasswordDialog({
  open,
  onCancel,
  onChanged,
}: ChangePasswordDialogProps) {
  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [error, setError] = useState<string | null>(null);
  // Which field the error belongs to: the current password (wrong password)
  // or the new pair (rule errors).
  const [errorField, setErrorField] = useState<
    "current" | "password" | "confirm" | null
  >(null);
  const [shake, setShake] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState(false);
  const firstFieldRef = useRef<HTMLInputElement>(null);

  // Reset on open.
  useEffect(() => {
    if (!open) return;
    setCurrentPw("");
    setNewPw("");
    setConfirmPw("");
    setError(null);
    setErrorField(null);
    setShake(false);
    setSubmitting(false);
    setSuccess(false);
    const id = setTimeout(() => firstFieldRef.current?.focus(), 60);
    return () => clearTimeout(id);
  }, [open]);

  // Esc dismisses.
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

  if (!open) return null;

  const ruleError = checkNewPassword(newPw, confirmPw);
  const canSubmit = currentPw.length > 0 && ruleError === null && !submitting;

  const clearError = () => {
    if (error) {
      setError(null);
      setErrorField(null);
    }
  };

  const wrongPassword = () => {
    setError("Incorrect password. Try again.");
    setErrorField("current");
    setShake(true);
    setTimeout(() => setShake(false), 400);
  };

  const handleSubmit = async () => {
    if (!canSubmit) return;

    setSubmitting(true);
    setError(null);
    setErrorField(null);
    try {
      const result = await changeLockdownPassword(currentPw, newPw);
      if (result.ok) {
        setSuccess(true);
        // Brief success moment, then close.
        setTimeout(() => {
          onChanged();
        }, 800);
      } else {
        wrongPassword();
      }
    } catch (err) {
      // The backend owns the common-password block for new passwords.
      const rule = passwordRuleErrorFromResponse(err);
      if (rule) {
        setError(passwordRuleMessage(rule));
        setErrorField(rule === "mismatch" ? "confirm" : "password");
      } else {
        wrongPassword();
      }
    } finally {
      setSubmitting(false);
    }
  };

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

      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={
          shake
            ? { scale: 1, opacity: 1, y: 0, x: [-6, 6, -4, 4, 0] }
            : { scale: 1, opacity: 1, y: 0, x: 0 }
        }
        transition={
          shake
            ? { duration: 0.3 }
            : { type: "spring", stiffness: 400, damping: 30, delay: 0.08 }
        }
        className="relative w-[420px] rounded-[var(--radius-card)] p-7"
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="change-password-heading"
      >
        <h2
          id="change-password-heading"
          className="font-display text-[18px] font-normal text-slate text-center mb-3"
        >
          Change password
        </h2>

        {success ? (
          <p className="text-[13px] text-steel text-center leading-relaxed">
            Password changed successfully.
          </p>
        ) : (
          <>
            <div className="mb-3">
              {/* Current password */}
              <div>
                <label
                  htmlFor="change-current-pw"
                  className="block text-[11px] text-steel mb-1.5 ml-1"
                >
                  Current password
                </label>
                <div className="relative">
                  <input
                    ref={firstFieldRef}
                    id="change-current-pw"
                    type="password"
                    value={currentPw}
                    onChange={(e) => {
                      setCurrentPw(e.target.value);
                      clearError();
                    }}
                    aria-label="Current password"
                    className={inputBase + " pr-8"}
                    style={{
                      ...inputBaseStyle,
                      ...(error && errorField === "current"
                        ? { border: "1px solid var(--color-rose)" }
                        : {}),
                    }}
                    onFocus={onInputFocus}
                    onBlurCapture={onInputBlur}
                  />
                  <LockIcon className="absolute right-4 top-1/2 -translate-y-1/2 text-steel/40 pointer-events-none" />
                </div>
              </div>
            </div>

            <RecoveryPasswordFields
              idPrefix="change"
              password={newPw}
              confirm={confirmPw}
              onPasswordChange={(value) => {
                setNewPw(value);
                clearError();
              }}
              onConfirmChange={(value) => {
                setConfirmPw(value);
                clearError();
              }}
              onSubmit={() => {
                if (canSubmit) void handleSubmit();
              }}
              label="New password"
              confirmLabel="Confirm new password"
              hint="Use at least 12 characters."
              errorText={error}
              errorField={
                errorField === "password" || errorField === "confirm"
                  ? errorField
                  : null
              }
            />

            <div className="flex items-center justify-center gap-2.5 mt-6">
              <button
                onClick={onCancel}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                style={glassPillStyle}
              >
                Cancel
              </button>
              <button
                disabled={!canSubmit}
                onClick={handleSubmit}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
                style={gradientPillStyle}
              >
                {submitting ? "Saving..." : "Change password"}
              </button>
            </div>
          </>
        )}
      </motion.div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ViewRecoveryCodeDialog
// ---------------------------------------------------------------------------

interface ViewRecoveryCodeDialogProps {
  /** When false, the component renders nothing. */
  open: boolean;
  /** Called on Cancel / Esc / backdrop click. */
  onCancel: () => void;
  /** Called after a new key has been successfully generated. Optional. */
  onRegenerated?: () => void;
}

export function ViewRecoveryCodeDialog({
  open,
  onCancel,
  onRegenerated,
}: ViewRecoveryCodeDialogProps) {
  // Phase: "password" = enter password, "code" = show the new key.
  const [phase, setPhase] = useState<"password" | "code">("password");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [shake, setShake] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [recoveryCode, setRecoveryCode] = useState("");
  // RecoveryStep's gate: null once a save action and the checkbox are done.
  const [gateHint, setGateHint] = useState<string | null>(null);
  // Esc / backdrop before the new key is saved: remind instead of closing,
  // because the previous key already stopped working.
  const [abandonPrompt, setAbandonPrompt] = useState(false);
  const returnRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const dismissRef = useRef<() => void>(() => {});
  dismissRef.current = () => {
    if (phase === "code" && gateHint !== null) {
      setAbandonPrompt(true);
      window.setTimeout(() => returnRef.current?.focus(), 0);
      return;
    }
    onCancel();
  };

  // Reset on open.
  useEffect(() => {
    if (!open) return;
    setPhase("password");
    setPassword("");
    setError(null);
    setShake(false);
    setSubmitting(false);
    setRecoveryCode("");
    setGateHint(null);
    setAbandonPrompt(false);
    const id = setTimeout(() => inputRef.current?.focus(), 60);
    return () => clearTimeout(id);
  }, [open]);

  // Esc dismisses.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        dismissRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (!open) return null;

  const handleViewCode = async () => {
    if (!password || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      // viewRecoveryCode REGENERATES a fresh code (regenerate-on-view decision).
      const result = await viewRecoveryCode(password);
      if (result.ok && result.recovery_code) {
        setRecoveryCode(result.recovery_code);
        setPhase("code");
        onRegenerated?.();
      } else {
        // Wrong password (result.ok === false or recovery_code === null).
        setError("Incorrect password. Try again.");
        setShake(true);
        setTimeout(() => setShake(false), 400);
      }
    } catch {
      setError("Incorrect password. Try again.");
      setShake(true);
      setTimeout(() => setShake(false), 400);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        onClick={() => dismissRef.current()}
      />

      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={
          shake
            ? { scale: 1, opacity: 1, y: 0, x: [-6, 6, -4, 4, 0] }
            : { scale: 1, opacity: 1, y: 0, x: 0 }
        }
        transition={
          shake
            ? { duration: 0.3 }
            : { type: "spring", stiffness: 400, damping: 30, delay: 0.08 }
        }
        className={`relative ${
          // The key phase uses the wide card, like Lockdown setup's recovery step.
          phase === "code" ? "w-[520px]" : "w-[420px]"
        } max-w-[calc(100vw-32px)] max-h-[calc(100dvh-32px)] overflow-y-auto rounded-[var(--radius-card)] p-7`}
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="view-recovery-code-heading"
      >
        {phase === "password" ? (
          /* --- Phase 1: enter password to generate a new key --- */
          <>
            <h2
              id="view-recovery-code-heading"
              className="font-display text-[18px] font-normal text-slate text-center mb-3"
            >
              View recovery key
            </h2>

            {/* Explain that viewing mints a fresh code (design Surface A item 3). */}
            <p className="text-[13px] text-steel mb-5 leading-relaxed text-center">
              Enter your password to generate a new recovery key. This
              invalidates your current key.
            </p>

            <div className="relative mb-2">
              <input
                ref={inputRef}
                id="view-recovery-code-password"
                type="password"
                value={password}
                onChange={(e) => {
                  setPassword(e.target.value);
                  if (error) setError(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && password && !submitting)
                    handleViewCode();
                }}
                placeholder="Password"
                aria-label="Transcript password"
                className={inputBase + " pr-8"}
                style={{
                  ...inputBaseStyle,
                  ...(error ? { borderColor: "var(--color-rose)" } : {}),
                }}
                onFocus={onInputFocus}
                onBlurCapture={onInputBlur}
              />
              <LockIcon className="absolute right-4 top-1/2 -translate-y-1/2 text-steel/40 pointer-events-none" />
            </div>

            {/* Wrong-password error (reuses E5 copy for consistency). */}
            {error && (
              <p className="text-[11px] text-rose mb-3">{error}</p>
            )}

            <div className="flex items-center justify-center gap-2.5 mt-6">
              <button
                onClick={onCancel}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                style={glassPillStyle}
              >
                Cancel
              </button>
              <button
                disabled={!password || submitting}
                onClick={handleViewCode}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
                style={gradientPillStyle}
              >
                {submitting ? "Generating..." : "Generate a new key"}
              </button>
            </div>
          </>
        ) : (
          /* --- Phase 2: the new key on the shared RecoveryStep (full Copy /
             Print / Download + checkbox gate, no optional password). --- */
          <>
            <h2
              id="view-recovery-code-heading"
              className="font-display text-[18px] font-normal text-slate text-center mb-2"
            >
              Your new recovery key
            </h2>
            <p className="text-[12px] text-steel mb-4 leading-relaxed text-center">
              Your previous recovery key no longer works.
            </p>

            <RecoveryStep
              secret={recoveryCode}
              fileVariant="lockdown"
              checkboxLabel="I've saved my recovery key somewhere other than this computer."
              onGateChange={(_open, hint) => {
                setGateHint(hint);
                if (hint === null) setAbandonPrompt(false);
              }}
            />

            <div className="flex items-center justify-center mt-5">
              <RecoveryGateButton
                hint={gateHint}
                hintId="view-recovery-gate"
                className="px-6 py-2"
                onClick={onCancel}
              >
                Done
              </RecoveryGateButton>
            </div>

            {abandonPrompt && gateHint !== null && (
              <RecoveryAbandonPrompt
                message={gateHint}
                onReturn={() => setAbandonPrompt(false)}
                buttonRef={returnRef}
              />
            )}
          </>
        )}
      </motion.div>
    </div>
  );
}

export default ChangePasswordDialog;
