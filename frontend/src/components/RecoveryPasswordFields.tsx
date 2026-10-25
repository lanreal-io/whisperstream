/**
 * RecoveryPasswordFields: the shared new-password + confirm pair, extracted
 * from LockdownSetupModal's password step.
 *
 * Used by Lockdown setup ("Password"), Lockdown Change password ("New
 * password") and RecoveryStep's optional section ("Recovery password"). The
 * caller owns the values, validation timing and submit; this component owns the
 * field recipe, the optional progressive Confirm field and the single hint /
 * error line under the pair. Never log a password.
 */

import { forwardRef, useId } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  passwordInputClassName as inputBase,
  passwordInputStyle as inputBaseStyle,
} from "./passwordInputRecipe";

/** Trailing lock glyph, same recipe as the Lockdown password fields. */
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

// Focus is the single global :focus-visible frost outline; blur restores the
// shared surface in case a caller tinted it.
const onInputFocus = (e: React.FocusEvent<HTMLInputElement>) => {
  e.currentTarget.style.outline = "";
};

export interface RecoveryPasswordFieldsProps {
  password: string;
  confirm: string;
  onPasswordChange: (value: string) => void;
  onConfirmChange: (value: string) => void;
  label: string;
  confirmLabel: string;
  /** Steel helper shown while there is no error. */
  hint: string;
  /** Resolved error text; replaces the hint and is announced. */
  errorText?: string | null;
  /** Which field the error belongs to, for the rose edge. */
  errorField?: "password" | "confirm" | null;
  /** Mount Confirm only once Password is non-empty (Lockdown setup). */
  progressive?: boolean;
  /** Enter in the Confirm field. */
  onSubmit?: () => void;
  /** Stable id prefix; defaults to a generated one. */
  idPrefix?: string;
}

export const RecoveryPasswordFields = forwardRef<
  HTMLInputElement,
  RecoveryPasswordFieldsProps
>(function RecoveryPasswordFields(
  {
    password,
    confirm,
    onPasswordChange,
    onConfirmChange,
    label,
    confirmLabel,
    hint,
    errorText = null,
    errorField = null,
    progressive = false,
    onSubmit,
    idPrefix,
  },
  ref,
) {
  const generated = useId();
  const base = idPrefix ?? `recovery-pw-${generated}`;
  const passwordId = `${base}-password`;
  const confirmId = `${base}-confirm`;
  const messageId = `${base}-message`;
  const showConfirm = !progressive || password.length > 0;

  const fieldStyle = (field: "password" | "confirm") =>
    errorText && errorField === field
      ? { ...inputBaseStyle, border: "1px solid var(--color-rose)" }
      : inputBaseStyle;

  const confirmField = (
    <div>
      <label
        htmlFor={confirmId}
        className="block text-[11px] text-steel mb-1.5 ml-1"
      >
        {confirmLabel}
      </label>
      <div className="relative">
        <input
          id={confirmId}
          type="password"
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => onConfirmChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") onSubmit?.();
          }}
          aria-describedby={messageId}
          aria-invalid={errorText && errorField === "confirm" ? true : undefined}
          className={inputBase + " pr-8"}
          style={fieldStyle("confirm")}
          onFocus={onInputFocus}
        />
        <LockIcon className="absolute right-4 top-1/2 -translate-y-1/2 text-steel/40 pointer-events-none" />
      </div>
    </div>
  );

  return (
    <div>
      <div className="space-y-3">
        <div>
          <label
            htmlFor={passwordId}
            className="block text-[11px] text-steel mb-1.5 ml-1"
          >
            {label}
          </label>
          <div className="relative">
            <input
              ref={ref}
              id={passwordId}
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => onPasswordChange(e.target.value)}
              aria-describedby={messageId}
              aria-invalid={
                errorText && errorField === "password" ? true : undefined
              }
              className={inputBase + " pr-8"}
              style={fieldStyle("password")}
              onFocus={onInputFocus}
            />
            <LockIcon className="absolute right-4 top-1/2 -translate-y-1/2 text-steel/40 pointer-events-none" />
          </div>
        </div>

        {progressive ? (
          <AnimatePresence initial={false}>
            {showConfirm && (
              <motion.div
                key="confirm"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2, ease: [0.2, 0, 0, 1] }}
                style={{ overflow: "hidden" }}
              >
                <div className="pt-1">{confirmField}</div>
              </motion.div>
            )}
          </AnimatePresence>
        ) : (
          confirmField
        )}
      </div>

      {errorText ? (
        <p id={messageId} role="alert" className="text-[11px] text-rose mt-3 ml-1">
          {errorText}
        </p>
      ) : (
        <p id={messageId} className="text-[11px] text-steel mt-3 ml-1">
          {hint}
        </p>
      )}
    </div>
  );
});
