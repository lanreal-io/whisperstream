/**
 * StartFreshDialog: the destructive fallback when the transcript key is
 * missing and the user has lost every recovery method.
 *
 * Step 1 is neutral (glass buttons, focus on the safe action) and explains the
 * trade. Step 2 is LockdownDisableDialog's destroy pattern: rose halo, typed
 * DELETE arms the rose button, safe action below, focus on the field. On
 * success the caller opens the recovery key setup for the new history.
 *
 * Dialog shell is hand-rolled (no shared base).
 */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { motion } from "framer-motion";
import { resetHistory } from "../lib/api/historyRecovery";
import {
  passwordInputClassName as inputBase,
  passwordInputStyle as inputBaseStyle,
} from "./passwordInputRecipe";

export interface StartFreshDialogProps {
  open: boolean;
  /** Saved rows from recovery status; null when the count is unavailable. */
  recordCount: number | null;
  hasAudio: boolean;
  hasRecoveryPassword: boolean;
  onCancel: () => void;
  /** History and its keys are deleted. */
  onDeleted: () => void;
}

const shellStyle: React.CSSProperties = {
  background: "var(--surface-modal)",
  backdropFilter: "blur(24px)",
  WebkitBackdropFilter: "blur(24px)",
  border: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass)",
};

const glassPillStyle: React.CSSProperties = {
  background: "var(--surface-control)",
  border: "1px solid var(--border-hairline-steel)",
  boxShadow: "inset 0 1px 0 var(--rim-specular)",
};

function TrashGlyph() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 7h16M10 11v6M14 11v6M5 7l1 12a2 2 0 002 2h8a2 2 0 002-2l1-12M9 7V4h6v3" />
    </svg>
  );
}

function deleteBullet(n: number | null, hasAudio: boolean): string | null {
  if (n === null) return null;
  if (n === 1) {
    return hasAudio
      ? "Your 1 saved transcript and its audio are deleted."
      : "Your 1 saved transcript is deleted.";
  }
  return hasAudio
    ? `All ${n} saved transcripts and their audio are deleted.`
    : `All ${n} saved transcripts are deleted.`;
}

function confirmBody(n: number | null): string {
  if (n === 1) {
    return "This permanently deletes your 1 saved transcript without opening it. It can't be undone.";
  }
  // Without a count the sentence drops only the number.
  return n === null
    ? "This permanently deletes all saved transcripts without opening them. It can't be undone."
    : `This permanently deletes all ${n} saved transcripts without opening them. It can't be undone.`;
}

export function StartFreshDialog({
  open,
  recordCount,
  hasAudio,
  hasRecoveryPassword,
  onCancel,
  onDeleted,
}: StartFreshDialogProps) {
  const [step, setStep] = useState<"explain" | "confirm">("explain");
  const [typed, setTyped] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const safeRef = useRef<HTMLButtonElement>(null);
  const fieldRef = useRef<HTMLInputElement>(null);
  const armed = typed.trim().toUpperCase() === "DELETE";

  useEffect(() => {
    if (!open) return;
    setStep("explain");
    setTyped("");
    setDeleting(false);
    setError(null);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const id = window.setTimeout(() => {
      if (step === "explain") safeRef.current?.focus();
      else fieldRef.current?.focus();
    }, 30);
    return () => window.clearTimeout(id);
  }, [open, step]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !deleting) {
        event.preventDefault();
        onCancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, deleting, onCancel]);

  if (!open) return null;

  const handleDelete = async () => {
    if (!armed || deleting) return;
    setDeleting(true);
    setError(null);
    try {
      await resetHistory("DELETE");
      onDeleted();
    } catch {
      setError("Couldn't delete your history. Try again.");
    } finally {
      setDeleting(false);
    }
  };

  const bullet = deleteBullet(recordCount, hasAudio);

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        className="absolute inset-0"
        style={{ background: "var(--backdrop-strong)", backdropFilter: "blur(40px)", WebkitBackdropFilter: "blur(40px)" }}
        onClick={() => {
          if (!deleting) onCancel();
        }}
      />
      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30 }}
        className="relative w-[440px] max-w-full max-h-[calc(100dvh-32px)] overflow-y-auto rounded-[var(--radius-card)] p-7"
        style={shellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="start-fresh-heading"
      >
        {step === "explain" ? (
          <>
            <h2 id="start-fresh-heading" className="font-display text-[18px] font-normal text-slate text-center mb-3">
              Start fresh without your history?
            </h2>
            <p className="text-[13px] text-steel leading-relaxed text-center mb-3">
              {hasRecoveryPassword
                ? "Without the recovery key or recovery password, your saved transcripts can't be opened by anyone, including us."
                : "Without the recovery key, your saved transcripts can't be opened by anyone, including us."}
            </p>
            <ul className="space-y-1.5 max-w-[340px] mx-auto">
              {[bullet, "Saving starts again with a new recovery key.", "If you find your key later, it won't work on the new history."]
                .filter((b): b is string => b !== null)
                .map((b) => (
                  <li key={b} className="flex items-start gap-2 text-[12px] text-steel leading-relaxed">
                    <span aria-hidden="true" className="mt-[6px] w-1 h-1 rounded-full shrink-0 bg-steel/50" />
                    <span>{b}</span>
                  </li>
                ))}
            </ul>
            <div className="flex items-center justify-center gap-2.5 mt-6">
              <button
                ref={safeRef}
                type="button"
                onClick={onCancel}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer"
                style={glassPillStyle}
              >
                Go back
              </button>
              <button
                type="button"
                onClick={() => setStep("confirm")}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer"
                style={glassPillStyle}
              >
                Continue
              </button>
            </div>
          </>
        ) : (
          <>
            <div
              className="w-[52px] h-[52px] rounded-full mx-auto mb-3 flex items-center justify-center"
              style={{
                background: "radial-gradient(circle, color-mix(in srgb, var(--color-rose) 16%, transparent), transparent 70%)",
                color: "var(--color-rose)",
              }}
            >
              <TrashGlyph />
            </div>
            <h2 id="start-fresh-heading" className="font-display text-[18px] font-normal text-slate text-center mb-3">
              Delete all saved transcripts?
            </h2>
            <p className="text-[13px] text-steel mb-4 leading-relaxed text-center">{confirmBody(recordCount)}</p>
            <input
              ref={fieldRef}
              value={typed}
              disabled={deleting}
              onChange={(e) => {
                setTyped(e.target.value);
                if (error) setError(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") void handleDelete();
              }}
              placeholder="Type DELETE to confirm"
              aria-label="Type DELETE to confirm"
              autoComplete="off"
              spellCheck={false}
              className={inputBase}
              style={inputBaseStyle}
            />
            {error && <p role="alert" className="text-[11px] text-rose mt-2 ml-1">{error}</p>}
            <div className="flex flex-col items-stretch gap-2.5 mt-4">
              <button
                type="button"
                disabled={!armed || deleting}
                onClick={() => void handleDelete()}
                className="px-5 py-2 rounded-full text-[13px] font-medium cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
                style={{
                  background: "color-mix(in srgb, var(--color-rose) 15%, transparent)",
                  border: "1px solid color-mix(in srgb, var(--color-rose) 40%, transparent)",
                  boxShadow: "inset 0 1px 0 var(--rim-specular)",
                  color: "var(--color-rose)",
                }}
              >
                {deleting ? "Deleting..." : "Delete all saved transcripts"}
              </button>
              <button
                type="button"
                disabled={deleting}
                onClick={onCancel}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer disabled:opacity-40"
                style={glassPillStyle}
              >
                Keep my transcripts
              </button>
            </div>
          </>
        )}
      </motion.div>
    </div>,
    document.body,
  );
}
