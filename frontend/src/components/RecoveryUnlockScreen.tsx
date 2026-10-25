import { passwordInputClassName as inputBase, passwordInputStyle as inputBaseStyle } from "./passwordInputRecipe";
/**
 * RecoveryUnlockScreen: the in-tab gate over transcript content, generalized
 * from Lockdown's TranscriptLockScreen (Surface C).
 *
 * variant "lockdown": Lockdown re-sealed the store. Password, with the recovery
 * key as the emergency path. One backend backoff counter covers both.
 *
 * variant "missing-key": standard history lost its Keychain/DPAPI key. Restore
 * with the recovery key, or the recovery password when one was set (the
 * password tab is hidden otherwise). Each secret has its own backend backoff.
 * Saving is paused until the user restores or starts fresh; Start fresh stays
 * reachable during backoff. When neither a recovery key nor a recovery
 * password was ever set, there is nothing to enter: the screen explains that
 * and offers only Start fresh.
 *
 * An in-tab, full-surface overlay (NOT a PopupShell window): the caller clears
 * the content underneath so nothing leaks behind it; other tabs stay usable.
 * The wrong-secret shake is the one sanctioned motion and is skipped under
 * reduced motion. Copy is from copy-review.md Locked Copy Decisions.
 *
 * NEVER log the password or the recovery key.
 */

import { useState, useEffect, useRef, useCallback } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { unlockLockdown } from "../lib/api/lockdown";
import {
  historyRecoveryErrorCode,
  unlockHistoryWithRecoveryKey,
  unlockHistoryWithRecoveryPassword,
} from "../lib/api/historyRecovery";
import { restoreErrorMessage, type HistoryPlatform } from "../lib/historyRecoveryCopy";
import { errorType } from "../lib/logCapture";
import { Badge } from "./Badge";
// The shield-seal glyph is shared with SealPanel so the Lockdown lock screen and
// the settings seal read as the SAME object.
import { ShieldSealGlyph } from "./SealPanel";

const gradientPillStyle: React.CSSProperties = {
  background: "var(--accent-fill)",
  boxShadow: "var(--shadow-pearl)",
};

// Focus defers to the global :focus-visible frost outline (no per-field ring).
const onInputFocus = (e: React.FocusEvent<HTMLInputElement>) => {
  e.currentTarget.style.outline = "";
};

/** Trailing lock glyph for the field (1.5 stroke, currentColor). */
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

/** Key glyph for the missing-key tile (gold: recovery). */
export function KeyGlyph({ size = 34, className }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <circle cx="8" cy="15" r="4" />
      <path d="M10.85 12.15L19 4M16 7l2.5 2.5M14 9l2 2" />
    </svg>
  );
}

/** Format remaining backoff seconds as a ticking m:ss. */
function formatBackoff(totalSeconds: number): string {
  const s = Math.max(0, Math.ceil(totalSeconds));
  const minutes = Math.floor(s / 60);
  const seconds = s % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

type Mode = "password" | "recovery";

export interface RecoveryUnlockScreenProps {
  variant?: "lockdown" | "missing-key";
  /** Platform wording for the missing-key variant. */
  platform?: HistoryPlatform;
  /** Missing-key only: show the Recovery password tab. */
  hasRecoveryPassword?: boolean;
  /** Missing-key only: false when no recovery key was ever set up. */
  hasRecoveryKey?: boolean;
  /** Seconds left in the backend backoff when the screen mounts. Lockdown: the
   *  shared counter. Missing-key: the recovery key counter. */
  initialLockoutSeconds?: number;
  /** Missing-key only: the recovery password counter. */
  initialPasswordLockoutSeconds?: number;
  /** Called after a successful unlock or restore. */
  onUnlocked: () => void;
  /** Missing-key only: open Start fresh. */
  onStartFresh?: () => void;
}

export function RecoveryUnlockScreen({
  variant = "lockdown",
  platform = "mac",
  hasRecoveryPassword = false,
  hasRecoveryKey = true,
  initialLockoutSeconds = 0,
  initialPasswordLockoutSeconds = 0,
  onUnlocked,
  onStartFresh,
}: RecoveryUnlockScreenProps) {
  const reducedMotion = useReducedMotion() ?? false;
  const missingKey = variant === "missing-key";
  // Key lost before recovery was ever set up: nothing can restore it here.
  const noRecovery = missingKey && !hasRecoveryKey && !hasRecoveryPassword;

  // Lockdown opens on its password; a missing key opens on the recovery key.
  const [mode, setMode] = useState<Mode>(missingKey ? "recovery" : "password");
  const [value, setValue] = useState("");
  // Wrong-secret or restore-failure line; cleared on edit and on lockout.
  const [error, setError] = useState<string | null>(null);
  // Remaining backoff per secret. Lockdown keeps both in step (one counter).
  const [lockouts, setLockouts] = useState<Record<Mode, number>>(() =>
    missingKey
      ? { recovery: initialLockoutSeconds, password: initialPasswordLockoutSeconds }
      : { recovery: initialLockoutSeconds, password: initialLockoutSeconds },
  );
  const [shake, setShake] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const remainingSecs = lockouts[mode];
  const lockedOut = remainingSecs > 0;
  const anyLockout = lockouts.password > 0 || lockouts.recovery > 0;
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!lockedOut) inputRef.current?.focus();
  }, [mode, lockedOut]);

  // Tick every active countdown down in place once per second.
  useEffect(() => {
    if (!anyLockout) return;
    const id = window.setInterval(() => {
      setLockouts((prev) => ({
        password: Math.max(0, prev.password - 1),
        recovery: Math.max(0, prev.recovery - 1),
      }));
    }, 1000);
    return () => window.clearInterval(id);
  }, [anyLockout]);

  const setLockoutFor = useCallback(
    (which: Mode, seconds: number) => {
      setLockouts((prev) =>
        missingKey ? { ...prev, [which]: seconds } : { password: seconds, recovery: seconds },
      );
    },
    [missingKey],
  );

  const shakeTimerRef = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(shakeTimerRef.current), []);

  const triggerShake = useCallback(() => {
    if (reducedMotion) return;
    setShake(true);
    window.clearTimeout(shakeTimerRef.current);
    shakeTimerRef.current = window.setTimeout(() => setShake(false), 320);
  }, [reducedMotion]);

  const wrongSecretMessage = !missingKey
    ? "Incorrect password. Try again."
    : mode === "recovery"
      ? "That recovery key didn't match. Check it and try again."
      : "Incorrect recovery password. Try again.";

  const submit = useCallback(async () => {
    const secret = value.trim();
    if (!secret || lockedOut || submitting) return;
    setSubmitting(true);
    try {
      const result = missingKey
        ? mode === "recovery"
          ? await unlockHistoryWithRecoveryKey(secret)
          : await unlockHistoryWithRecoveryPassword(value)
        : await unlockLockdown(
            mode === "password" ? { password: secret } : { recovery_code: secret },
          );
      if (result.ok) {
        setValue("");
        setError(null);
        setLockouts({ password: 0, recovery: 0 });
        onUnlocked();
        return;
      }
      const lockout = result.lockout_remaining_seconds ?? 0;
      if (lockout > 0) {
        setLockoutFor(mode, lockout);
        setError(null);
      } else {
        setError(wrongSecretMessage);
      }
      setValue("");
      triggerShake();
    } catch (err) {
      console.error(
        `[${missingKey ? "HistoryRecovery" : "Lockdown"}] unlock request failed error_type=${errorType(err)}`,
      );
      if (missingKey) {
        // The key is already back (another window restored it): just refresh.
        if (historyRecoveryErrorCode(err) === "history_key_not_missing") {
          onUnlocked();
          return;
        }
        setError(restoreErrorMessage(err, platform));
        return;
      }
      setError(wrongSecretMessage);
      triggerShake();
    } finally {
      setSubmitting(false);
    }
  }, [value, lockedOut, submitting, missingKey, mode, onUnlocked, triggerShake, setLockoutFor, wrongSecretMessage, platform]);

  const switchMode = useCallback((next: Mode) => {
    setMode(next);
    setValue("");
    setError(null);
  }, []);

  const keyField = (
    <input
      ref={inputRef}
      type="text"
      autoFocus
      disabled={lockedOut}
      value={value}
      onChange={(e) => {
        setValue(e.target.value);
        if (error) setError(null);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") void submit();
      }}
      placeholder="XXXX-XXXX-XXXX-XXXX-XXXX"
      aria-label="Recovery key"
      autoCapitalize="characters"
      autoComplete="off"
      spellCheck={false}
      className={inputBase + " pr-9 font-mono tracking-[0.06em] disabled:opacity-40 disabled:cursor-not-allowed"}
      style={{
        ...inputBaseStyle,
        ...(error ? { border: "1px solid var(--color-rose)" } : {}),
      }}
      onFocus={onInputFocus}
    />
  );

  const passwordField = (
    <input
      ref={inputRef}
      type="password"
      autoFocus
      disabled={lockedOut}
      value={value}
      onChange={(e) => {
        setValue(e.target.value);
        if (error) setError(null);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") void submit();
      }}
      placeholder={missingKey ? "Recovery password" : "Password"}
      aria-label={missingKey ? "Recovery password" : "Transcript password"}
      className={inputBase + " pr-9 disabled:opacity-40 disabled:cursor-not-allowed"}
      style={{
        ...inputBaseStyle,
        ...(error ? { border: "1px solid var(--color-rose)" } : {}),
      }}
      onFocus={onInputFocus}
    />
  );

  const body = !missingKey
    ? mode === "password"
      ? "Enter your password to view your history."
      : "Enter the recovery key you saved when you turned on Lockdown Mode. You'll set a new password next."
    : platform === "mac"
      ? hasRecoveryPassword
        ? "Keychain on this Mac no longer has the key for your saved transcripts. Enter your recovery key or recovery password to restore it."
        : "Keychain on this Mac no longer has the key for your saved transcripts. Enter your recovery key to restore it."
      : hasRecoveryPassword
        ? "Windows on this PC no longer has the key for your saved transcripts. Enter your recovery key or recovery password to restore it."
        : "Windows on this PC no longer has the key for your saved transcripts. Enter your recovery key to restore it.";

  if (noRecovery) {
    return (
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ type: "tween", duration: 0.15, ease: [0.2, 0, 0, 1] }}
        className="absolute inset-0 z-10 flex flex-col items-center justify-center px-8"
      >
        <div className="flex flex-col items-center w-full max-w-[340px] text-center">
          <div
            className="w-[88px] h-[88px] rounded-[var(--radius-card)] flex items-center justify-center mb-5"
            style={{
              background: "var(--surface-raised)",
              border: "1px solid var(--border-hairline)",
              boxShadow: "var(--shadow-glass-sm)",
              color: "var(--color-gold-deep)",
            }}
          >
            <KeyGlyph />
          </div>
          <Badge label="Key missing" tone="gold" className="mb-2.5" />
          <p className="text-[13px] text-slate mb-1 leading-relaxed">
            {platform === "mac"
              ? "Keychain on this Mac no longer has the key for your saved transcripts."
              : "Windows on this PC no longer has the key for your saved transcripts."}
          </p>
          <p className="text-[12px] text-steel mb-5 leading-relaxed">
            No recovery key was set up, so they can't be restored.
          </p>
          {onStartFresh && (
            <button
              type="button"
              onClick={onStartFresh}
              className="px-6 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer"
              style={gradientPillStyle}
            >
              Start fresh
            </button>
          )}
          <span className="inline-flex items-center gap-1.5 mt-4 text-[11px] text-steel">
            <span aria-hidden="true" className="w-1.5 h-1.5 rounded-full" style={{ background: "var(--color-gold)" }} />
            Saving is paused until you restore or start fresh.
          </span>
        </div>
      </motion.div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ type: "tween", duration: 0.15, ease: [0.2, 0, 0, 1] }}
      className="absolute inset-0 z-10 flex flex-col items-center justify-center px-8"
    >
      <motion.div
        animate={shake ? { x: [-6, 6, -4, 4, 0] } : { x: 0 }}
        transition={{ duration: 0.3 }}
        className={`flex flex-col items-center w-full ${missingKey ? "max-w-[340px]" : "max-w-[320px]"}`}
      >
        <div
          className="w-[88px] h-[88px] rounded-[var(--radius-card)] flex items-center justify-center mb-5"
          style={{
            // Themed empty-state icon container: raised surface + hairline
            // border + glass-sm, all token-driven.
            background: "var(--surface-raised)",
            border: "1px solid var(--border-hairline)",
            boxShadow: "var(--shadow-glass-sm)",
          }}
        >
          {missingKey ? (
            <span style={{ color: lockedOut ? "color-mix(in srgb, var(--color-rose) 80%, transparent)" : "var(--color-gold-deep)" }}>
              <KeyGlyph />
            </span>
          ) : (
            <ShieldSealGlyph size={36} className={lockedOut ? "text-rose/70" : "text-steel"} />
          )}
        </div>

        {missingKey && <Badge label="Key missing" tone="gold" className="mb-2.5" />}

        <h2 className="text-[18px] font-medium text-slate mb-1 text-center">
          {missingKey ? "Restore your transcript history" : "Transcripts are locked"}
        </h2>
        <p className="text-[12px] text-steel mb-5 text-center leading-relaxed">{body}</p>

        {missingKey && hasRecoveryPassword && (
          <div
            role="group"
            aria-label="Restore with"
            className="inline-flex p-[3px] rounded-full mb-3"
            style={{ background: "var(--surface-control)", border: "1px solid var(--border-hairline)" }}
          >
            {(["recovery", "password"] as const).map((m) => (
              <button
                key={m}
                type="button"
                aria-pressed={mode === m}
                onClick={() => switchMode(m)}
                className={`px-3 py-1 rounded-full text-[11.5px] font-medium cursor-pointer transition-colors duration-200 ${
                  mode === m ? "text-slate" : "text-steel hover:text-slate"
                }`}
                style={
                  mode === m
                    ? { background: "var(--surface-hover)", boxShadow: "inset 0 1px 0 var(--rim-specular)" }
                    : undefined
                }
              >
                {m === "recovery" ? "Recovery key" : "Recovery password"}
              </button>
            ))}
          </div>
        )}

        <div className="relative w-full">
          {mode === "password" ? passwordField : keyField}
          <LockIcon className="absolute right-4 top-1/2 -translate-y-1/2 text-steel/40 pointer-events-none" />
        </div>

        {/* Wrong secret / restore failure: rose text always carries meaning. */}
        {error && !lockedOut && (
          <p className="text-[11px] text-rose mt-2 self-start" role="alert">
            {error}
          </p>
        )}

        {/* Live backoff countdown, announced politely and updated in place. */}
        {lockedOut && (
          <p className="text-[11px] text-rose mt-2 self-start" role="status" aria-live="polite">
            {`Too many attempts. Try again in ${formatBackoff(remainingSecs)}.`}
          </p>
        )}

        <button
          type="button"
          disabled={lockedOut || !value.trim() || submitting}
          onClick={() => void submit()}
          className="mt-4 px-6 py-2 rounded-full text-[13px] font-medium text-white disabled:opacity-40 disabled:cursor-not-allowed"
          style={gradientPillStyle}
        >
          {missingKey ? (submitting ? "Restoring..." : "Restore history") : "Unlock"}
        </button>

        {/* Lockdown: the recovery-key path stays available throughout. */}
        {!missingKey && mode === "password" && (
          <button
            type="button"
            onClick={() => switchMode("recovery")}
            className="mt-3 text-[11px] text-steel/70 hover:text-slate transition-colors cursor-pointer"
          >
            Use recovery key instead
          </button>
        )}

        {/* Missing key: Start fresh stays reachable during backoff. */}
        {missingKey && onStartFresh && (
          <button
            type="button"
            onClick={onStartFresh}
            className="mt-3 text-[11px] text-steel/70 hover:text-slate transition-colors cursor-pointer"
          >
            {hasRecoveryPassword ? "Lost both? Start fresh" : "Lost your key? Start fresh"}
          </button>
        )}

        {missingKey && (
          <span className="inline-flex items-center gap-1.5 mt-4 text-[11px] text-steel">
            <span aria-hidden="true" className="w-1.5 h-1.5 rounded-full" style={{ background: "var(--color-gold)" }} />
            Saving is paused until you restore or start fresh.
          </span>
        )}
      </motion.div>
    </motion.div>
  );
}
