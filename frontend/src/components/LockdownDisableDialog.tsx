import {
  passwordInputClassName as inputBase,
  passwordInputRestingBorderColor,
  passwordInputStyle as inputBaseStyle,
} from "./passwordInputRecipe";
/**
 * LockdownDisableDialog — Surface E, the disable-lockdown confirm dialog.
 *
 * Opens when the user toggles the Lockdown Mode master switch OFF. Requires
 * the current password before disabling. Shows an inverse "what un-hardens"
 * checklist (steel, not rose — this is a legitimate user choice) and reassures
 * that transcripts stay on the device.
 *
 * On confirm: calls POST /api/lockdown/disable, then issues the combined config
 * write (lockdown_mode=false) via the parent's onConfigChange, and calls
 * lockStatus.refresh so the rest of the UI reflects the new reality.
 *
 * The dialog family has NO shared base — this card
 * hand-rolls its overlay/card from the EnableLocalConfirmDialog tokens (do NOT
 * DRY into a <Dialog>). Card width: w-[420px] (standard dialog width for E).
 *
 * All copy is verbatim from copy-review.md `## Locked Copy Decisions` (E1-E5).
 * No em dashes.
 */

import { useState, useEffect, useRef } from "react";
import { motion } from "framer-motion";
import { disableLockdown, isManagedByOrganizationError, resetLockdown, MANAGED_BY_ORGANIZATION_MESSAGE } from "../lib/api/lockdown";
import { type AppConfig } from "../lib/api/config";

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

/** E3 "what un-hardens" checklist items — verbatim from copy-review.md.
 *  The transcript-password line is dropped in the no-envelope (history-off)
 *  case, where there was never a transcript password. */
const UNHARDENS_ITEMS = [
  "Cloud AI providers become available again.",
  "Diagnostic logging turns back on.",
  "Transcripts will no longer require a password.",
];
const UNHARDENS_ITEMS_NO_ENVELOPE = [
  "Cloud AI providers become available again.",
  "Diagnostic logging turns back on.",
];

interface LockdownDisableDialogProps {
  /** When false, the component renders nothing. */
  open: boolean;
  /**
   * Whether a password-wrapped key envelope exists. When false (a no-envelope /
   * history-off lockdown), there is no key to unwrap: the dialog drops the
   * password field + the E2 password sentence, trims the checklist, and calls
   * disableLockdown() with no password. Defaults to true (envelope-backed),
   * preserving the original password-required behavior.
   */
  hasEnvelope?: boolean;
  /** Called on Cancel / Esc / backdrop click ("Keep on"). Nothing is persisted. */
  onCancel: () => void;
  /**
   * Called after a successful disable with the server-authoritative config the
   * disable endpoint returns (lockdown_mode cleared). The parent adopts it
   * verbatim. Disable deliberately does NOT restore a prior cloud provider or
   * re-enable AI cleanup — silently resuming off-device uploads with no
   * affirmative user action would be a privacy violation.
   */
  onDisabled: (syncedConfig?: AppConfig) => void;
}

export function LockdownDisableDialog({
  open,
  hasEnvelope = true,
  onCancel,
  onDisabled,
}: LockdownDisableDialogProps) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [shake, setShake] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  // "password" is the normal turn-off step; "reset" is the forgot-password
  // last-resort step that DESTROYS the encrypted history (envelope-backed only).
  const [mode, setMode] = useState<"password" | "reset">("password");
  // Typed confirmation for the destructive reset — must equal "DELETE".
  const [confirmText, setConfirmText] = useState("");
  const resetArmed = confirmText.trim().toUpperCase() === "DELETE";

  // Reset state on open.
  useEffect(() => {
    if (!open) return;
    setPassword("");
    setError(null);
    setShake(false);
    setSubmitting(false);
    setMode("password");
    setConfirmText("");
    // Defer autofocus so the spring entrance has mounted the input. Only the
    // envelope-backed variant renders a password field to focus.
    if (!hasEnvelope) return;
    const id = setTimeout(() => inputRef.current?.focus(), 60);
    return () => clearTimeout(id);
  }, [open, hasEnvelope]);

  // Esc: in the destructive reset step, back out to password entry (the safe
  // path) rather than closing; otherwise dismiss ("Keep on").
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        if (mode === "reset") {
          setMode("password");
          setError(null);
          setConfirmText("");
        } else {
          onCancel();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel, mode]);

  if (!open) return null;

  const handleConfirm = async () => {
    // Envelope-backed lockdown requires the password; a no-envelope lockdown
    // has no key to unwrap, so confirm proceeds with no password.
    if (submitting || (hasEnvelope && !password)) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await disableLockdown(hasEnvelope ? password : undefined);
      if (result.ok) {
        // Adopt the sanitized config the backend returns. No prior-provider
        // restore: the user re-opts into cloud themselves (privacy).
        onDisabled(result.config);
      } else {
        // E5 wrong-password error + shake.
        setError("Incorrect password. Try again.");
        setShake(true);
        setTimeout(() => setShake(false), 400);
      }
    } catch (err) {
      // A machine policy refusal is not a typing mistake. Managed installs
      // remove every route to this dialog, so this only fires if the policy
      // arrived while it was open — say what actually happened.
      if (isManagedByOrganizationError(err)) {
        setError(MANAGED_BY_ORGANIZATION_MESSAGE);
        return;
      }
      setError("Incorrect password. Try again.");
      setShake(true);
      setTimeout(() => setShake(false), 400);
    } finally {
      setSubmitting(false);
    }
  };

  // Forgot-password last resort: DESTROY the (unrecoverable) encrypted history
  // and turn Lockdown off. Gated on the typed "DELETE" confirmation; the backend
  // enforces the same token. Nothing is decrypted — the data is erased still
  // encrypted. On success the parent adopts the returned lockdown-off config.
  const handleReset = async () => {
    if (submitting || !resetArmed) return;
    setSubmitting(true);
    setError(null);
    try {
      // fetchJSON throws on any non-2xx (the backend returns 400 for a bad/absent
      // confirmation or a non-envelope lockdown), so a resolved response is a
      // successful reset — adopt the returned lockdown-off config.
      const result = await resetLockdown(confirmText.trim());
      onDisabled(result.config);
    } catch (err) {
      setError(
        isManagedByOrganizationError(err)
          ? MANAGED_BY_ORGANIZATION_MESSAGE
          : "Couldn't reset. Try again.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        onClick={onCancel}
      />

      {/* Glass card — w-[420px], standard dialog width for Surface E. */}
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
        aria-labelledby="disable-lockdown-heading"
      >
        {mode === "reset" ? (
          <>
            {/* Forgot-password destructive reset (envelope-backed only). Copy is
                legally reviewed (sign-off 2026-07-12): erased still-encrypted,
                never decrypted. Do NOT reword without a legal re-review. */}
            <h2
              id="disable-lockdown-heading"
              className="font-display text-[18px] font-normal text-slate text-center mb-3"
            >
              Delete your transcript history?
            </h2>
            <p className="text-[13px] text-steel mb-2 leading-relaxed text-center">
              This deletes your locked history without opening it and turns off
              Lockdown Mode. It can't be undone.
            </p>
            <div className="relative mb-4">
              <input
                autoFocus
                value={confirmText}
                onChange={(e) => {
                  setConfirmText(e.target.value);
                  if (error) setError(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && resetArmed && !submitting) handleReset();
                }}
                placeholder="Type DELETE to confirm"
                aria-label="Type DELETE to confirm the reset"
                className={inputBase}
                style={inputBaseStyle}
                onFocus={onInputFocus}
                onBlurCapture={onInputBlur}
              />
            </div>
            {error && <p className="text-[11px] text-rose mb-3">{error}</p>}
            <div className="flex flex-col items-stretch gap-2.5">
              <button
                disabled={submitting || !resetArmed}
                onClick={handleReset}
                className="px-5 py-2 rounded-full text-[13px] font-medium cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
                style={{
                  background: "color-mix(in srgb, var(--color-rose) 15%, transparent)",
                  border: "1px solid color-mix(in srgb, var(--color-rose) 40%, transparent)",
                  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.6 * var(--rim-scale)))",
                  color: "var(--color-rose)",
                }}
              >
                Delete history and turn off Lockdown
              </button>
              <button
                onClick={() => {
                  setMode("password");
                  setError(null);
                  setConfirmText("");
                }}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                style={glassPillStyle}
              >
                Enter password instead
              </button>
            </div>
          </>
        ) : (
          <>
        {/* E1 heading — verbatim from copy-review.md. */}
        <h2
          id="disable-lockdown-heading"
          className="font-display text-[18px] font-normal text-slate text-center mb-3"
        >
          Turn off Lockdown Mode?
        </h2>

        {/* E2 body — password prompt + transcripts-stay reassurance. Dropped
            entirely in the no-envelope (history-off) case: there is no password
            to enter and no saved transcripts to reassure about. */}
        {hasEnvelope && (
          <p className="text-[13px] text-steel mb-4 leading-relaxed text-center">
            Enter your password to confirm. Your transcripts stay on this device,
            they just won't require a password anymore.
          </p>
        )}

        {/* Single password field (the password-field idiom). Envelope-backed
            lockdown only — a no-envelope lockdown has no key to unwrap. */}
        {hasEnvelope && (
          <>
            <div className="relative mb-4">
              <input
                ref={inputRef}
                id="disable-lockdown-password"
                type="password"
                value={password}
                onChange={(e) => {
                  setPassword(e.target.value);
                  if (error) setError(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && password && !submitting) handleConfirm();
                }}
                placeholder="Password"
                aria-label="Confirm password"
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

            {/* E5 wrong-password error line (rose). */}
            {error && <p className="text-[11px] text-rose mb-3">{error}</p>}
          </>
        )}

        {/* E3 "what un-hardens" checklist (steel, NOT rose — legitimate user
            choice). The transcript-password line is trimmed in the no-envelope
            case (there was never a transcript password). */}
        <ul className="space-y-1.5 mb-5">
          {(hasEnvelope ? UNHARDENS_ITEMS : UNHARDENS_ITEMS_NO_ENVELOPE).map((text) => (
            <li key={text} className="flex items-start gap-2 text-[12px] text-steel">
              <span
                aria-hidden="true"
                className="mt-[6px] w-1 h-1 rounded-full bg-steel/50 shrink-0"
              />
              <span>{text}</span>
            </li>
          ))}
        </ul>

        {/* E4 buttons: "Keep on" (cancel, glass) / "Turn off" (confirm, destructive). */}
        <div className="flex items-center justify-center gap-2.5">
          <button
            onClick={onCancel}
            className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
            style={glassPillStyle}
          >
            Keep on
          </button>
          <button
            disabled={submitting || (hasEnvelope && !password)}
            onClick={handleConfirm}
            className="px-5 py-2 rounded-full text-[13px] font-medium cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
            style={{
              background: "color-mix(in srgb, var(--color-rose) 15%, transparent)",
              border: "1px solid color-mix(in srgb, var(--color-rose) 40%, transparent)",
              // Caught-light top rim, theme-invariant (system.md leave-literal).
              boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.6 * var(--rim-scale)))",
              color: "var(--color-rose)",
            }}
          >
            Turn off
          </button>
        </div>

        {/* Forgot-password escape hatch (envelope-backed only): a subtle link to
            the destructive reset, for a user who lost BOTH the password and the
            one-time recovery code and would otherwise be stuck in Lockdown Mode
            forever. Low emphasis — the password path is the norm. */}
        {hasEnvelope && (
          <div className="mt-3 text-center">
            <button
              onClick={() => {
                setMode("reset");
                setError(null);
                setConfirmText("");
              }}
              className="text-[11px] text-steel/60 hover:text-steel underline underline-offset-2 cursor-pointer transition-colors"
            >
              I forgot my password
            </button>
          </div>
        )}
          </>
        )}
      </motion.div>
    </div>
  );
}

export default LockdownDisableDialog;
