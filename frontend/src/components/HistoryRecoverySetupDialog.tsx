/**
 * HistoryRecoverySetupDialog: turning on standard transcript history, and the
 * recovery key setup that follows (design.md, Direction B).
 *
 * Step 1 (reason "enable" only) is the intro: platform disclosure, then "Enable
 * saving" calls the enable route (Keychain/DPAPI). Step 2 is the shared
 * RecoveryStep with the optional recovery password; Done confirms the pending
 * key. Existing users ("required") and Start fresh ("start-fresh") open on step
 * 2. The X cancels on every step; cancelling after saving is on leaves the
 * existing-user gate in place, which is intended.
 *
 * Under Lockdown (`passwordProtected`) only the intro shows: Lockdown owns the
 * recovery key, so enabling closes the dialog.
 *
 * Dialog shells are hand-rolled (no shared base).
 * SECURITY: never log the recovery key or a password.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { motion } from "framer-motion";
import {
  RECOVERY_GATE_HINT_SAVE,
  RecoveryGateButton,
  RecoveryStep,
  checkOptionalRecoveryPassword,
  type RecoveryPasswordDraft,
} from "./RecoveryStep";
import { enableTranscriptHistory } from "../lib/api/transcripts";
import {
  confirmHistoryRecovery,
  historyRecoveryErrorCode,
  setupHistoryRecovery,
} from "../lib/api/historyRecovery";
import {
  passwordRuleErrorFromResponse,
  type PasswordRuleError,
} from "../lib/passwordRule";
import {
  enableErrorMessage,
  placeFor,
  type HistoryPlatform,
} from "../lib/historyRecoveryCopy";
import { SecondaryButton } from "./SecondaryButton";

/** Shared Keychain explanation, not a shared dialog shell: Lockdown setup
 *  renders it in its own Keychain step. */
export function HistoryKeychainExplanation({ saveAudio }: { saveAudio: boolean }) {
  return (
    <div className="space-y-3 text-[13px] text-steel leading-relaxed">
      <p>Save encrypted transcripts on this Mac. Turn saving off anytime.</p>
      <p>
        macOS may ask to use Whisperstream's encryption key in Keychain.
        This permission covers only that key.
      </p>
      <p className="text-[12px]">
        {saveAudio
          ? "Save audio is on, so recordings will also be saved."
          : "Save audio is off, so only text will be saved."}
      </p>
    </div>
  );
}

export type HistoryRecoverySetupReason = "enable" | "required" | "start-fresh";

export interface HistoryRecoverySetupDialogProps {
  open: boolean;
  platform: HistoryPlatform;
  reason: HistoryRecoverySetupReason;
  saveAudio: boolean;
  /** Lockdown with a password: intro only, no recovery key step. */
  passwordProtected?: boolean;
  /** Saving turned on (sync local config). */
  onEnabled?: () => void;
  /** Enable found the protected key gone; show the missing-key screen. */
  onKeyMissing?: () => void;
  /** Setup finished (key confirmed, or enabled with recovery already set). */
  onComplete: () => void;
  /** X, Esc or backdrop. Saving stays as it is. */
  onCancel: () => void;
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

const gradientPillStyle: React.CSSProperties = {
  background: "var(--accent-fill)",
  boxShadow: "var(--shadow-pearl)",
};

function CloseIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
      <path d="M2 2l6 6M8 2L2 8" />
    </svg>
  );
}

const GATE_HINT_ID = "history-recovery-setup-gate";

export function HistoryRecoverySetupDialog({
  open,
  platform,
  reason,
  saveAudio,
  passwordProtected = false,
  onEnabled,
  onKeyMissing,
  onComplete,
  onCancel,
}: HistoryRecoverySetupDialogProps) {
  const [step, setStep] = useState<"intro" | "key">("intro");
  const [enabling, setEnabling] = useState(false);
  const [enableError, setEnableError] = useState<string | null>(null);
  const [secret, setSecret] = useState("");
  // Names the shown key so confirming promotes exactly this one.
  const generation = useRef<string | null>(null);
  const [loadingKey, setLoadingKey] = useState(false);
  const [keyError, setKeyError] = useState<string | null>(null);
  const [gateHint, setGateHint] = useState<string | null>(RECOVERY_GATE_HINT_SAVE);
  const [draft, setDraft] = useState<RecoveryPasswordDraft | null>(null);
  const [pwError, setPwError] = useState<PasswordRuleError | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [doneError, setDoneError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const primaryRef = useRef<HTMLButtonElement>(null);
  const keyRequest = useRef(0);
  const place = placeFor(platform);
  const busy = enabling || confirming;
  const showIntro = reason === "enable";

  const loadKey = useCallback(async () => {
    const mine = ++keyRequest.current;
    setLoadingKey(true);
    setKeyError(null);
    setSecret("");
    try {
      const pending = await setupHistoryRecovery();
      if (mine === keyRequest.current) {
        generation.current = pending.generation;
        setSecret(pending.recovery_key);
      }
    } catch {
      if (mine === keyRequest.current) {
        setKeyError("Couldn't set up your recovery key. Try again.");
      }
    } finally {
      if (mine === keyRequest.current) setLoadingKey(false);
    }
  }, []);

  // Reset whenever the dialog opens; non-enable reasons go straight to the key.
  useEffect(() => {
    if (!open) {
      keyRequest.current++;
      return;
    }
    setStep(showIntro ? "intro" : "key");
    setEnabling(false);
    setEnableError(null);
    setSecret("");
    setKeyError(null);
    setGateHint(RECOVERY_GATE_HINT_SAVE);
    setDraft(null);
    setPwError(null);
    setConfirming(false);
    setDoneError(null);
    if (!showIntro) void loadKey();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, reason]);

  useEffect(() => {
    if (open && step === "intro") primaryRef.current?.focus();
  }, [open, step]);

  // Esc cancels (like the X) unless a request is in flight; Tab stays inside.
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (!busy) onCancel();
      } else if (event.key === "Tab") {
        const focusable = Array.from(
          dialogRef.current?.querySelectorAll<HTMLElement>(
            "button:not(:disabled), input:not(:disabled)",
          ) ?? [],
        );
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onCancel]);

  if (!open) return null;

  const handleEnable = async () => {
    if (enabling) return;
    setEnabling(true);
    setEnableError(null);
    try {
      const result = await enableTranscriptHistory(!passwordProtected);
      onEnabled?.();
      if (passwordProtected || !result.needs_recovery_setup) {
        onComplete();
        return;
      }
      setStep("key");
      void loadKey();
    } catch (err) {
      const code = historyRecoveryErrorCode(err);
      if (code === "history_key_missing") {
        onKeyMissing?.();
        return;
      }
      // Under Lockdown the key comes from the unlocked envelope, so an
      // unexplained failure means history is locked.
      setEnableError(
        passwordProtected && (code === null || code === "history_key_unavailable")
          ? "Saving is still off. Unlock your history and try again."
          : enableErrorMessage(err, platform),
      );
    } finally {
      setEnabling(false);
    }
  };

  const handleDone = async () => {
    if (confirming || !secret) return;
    const failure = checkOptionalRecoveryPassword(draft);
    if (failure) {
      setPwError(failure);
      return;
    }
    const password = draft && draft.password ? draft.password : undefined;
    setConfirming(true);
    setDoneError(null);
    try {
      await confirmHistoryRecovery(generation.current, password);
      onComplete();
    } catch (err) {
      const rule = passwordRuleErrorFromResponse(err);
      if (rule) setPwError(rule);
      else setDoneError("Couldn't set up your recovery key. Try again.");
    } finally {
      setConfirming(false);
    }
  };

  const keyTitle =
    reason === "required"
      ? "Save your recovery key"
      : reason === "start-fresh"
        ? "Save your new recovery key"
        : "Keep a recovery key";
  const keyBody =
    reason === "required"
      ? `If ${place} ever loses its key and you don't have your recovery key, saved transcripts can't be opened.`
      : reason === "start-fresh"
        ? "Your old history was deleted. This key protects everything you save from now on."
        : `History opens on ${place} without a password. This key is your backup if ${place} ever loses its encryption key.`;
  const checkboxLabel = `I saved my recovery key somewhere other than ${place}.`;
  const audioLine = saveAudio
    ? "Save audio is on, so recordings will also be saved."
    : "Save audio is off, so only text will be saved.";

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        className="absolute inset-0"
        style={{ background: "var(--backdrop-strong)", backdropFilter: "blur(40px)", WebkitBackdropFilter: "blur(40px)" }}
        onClick={() => {
          if (!busy) onCancel();
        }}
      />
      <motion.div
        ref={dialogRef}
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30 }}
        className={`relative ${step === "key" ? "w-[520px]" : "w-[440px]"} max-w-full max-h-[calc(100dvh-32px)] overflow-y-auto rounded-[var(--radius-card)] px-7 py-6`}
        style={shellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="history-setup-heading"
        aria-busy={busy}
      >
        <button
          type="button"
          aria-label="Cancel setup"
          disabled={busy}
          onClick={onCancel}
          className="absolute top-3.5 right-3.5 w-7 h-7 rounded-full flex items-center justify-center text-steel hover:text-slate cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
          style={glassPillStyle}
        >
          <CloseIcon />
        </button>

        {step === "intro" ? (
          <>
            <h2 id="history-setup-heading" className="font-display text-[18px] font-normal text-slate text-center mb-4 px-6">
              {platform === "mac" ? "Save transcripts on this Mac?" : "Save transcripts on this PC?"}
            </h2>
            {passwordProtected ? (
              <div className="space-y-3 text-[13px] text-steel leading-relaxed">
                <p>Transcripts stay encrypted on this Mac, protected by your Lockdown password. Turn saving off anytime.</p>
                <p>{audioLine}</p>
              </div>
            ) : (
              <div className="space-y-3 text-[13px] text-steel leading-relaxed">
                {platform === "mac" && (
                  <p>macOS may ask to use Whisperstream's encryption key in Keychain. This permission covers only that key.</p>
                )}
                <p>
                  {platform === "mac"
                    ? "If this Mac loses its Keychain key and you also lose your recovery key and recovery password, your saved transcripts can't be recovered."
                    : "If this PC loses its encryption key and you also lose your recovery key and recovery password, your saved transcripts can't be recovered."}
                </p>
                <p className="text-[12px]">{audioLine}</p>
              </div>
            )}
            {enabling && platform === "mac" && !passwordProtected && (
              <p role="status" className="text-[12px] text-steel mt-4">
                Waiting for Keychain. Allow access to Whisperstream's encryption key if asked.
              </p>
            )}
            {enableError && <p role="alert" className="text-[12px] text-rose mt-4">{enableError}</p>}
            <div className="flex items-center justify-center mt-6">
              <button
                ref={primaryRef}
                type="button"
                disabled={enabling}
                onClick={() => void handleEnable()}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                style={gradientPillStyle}
              >
                {enabling ? "Enabling..." : "Enable saving"}
              </button>
            </div>
          </>
        ) : (
          <>
            <h2 id="history-setup-heading" className="font-display text-[18px] font-normal text-slate text-center mb-2 px-6">
              {keyTitle}
            </h2>
            <p className="text-[12px] text-steel mb-4 leading-relaxed text-center" style={{ textWrap: "balance" } as React.CSSProperties}>{keyBody}</p>

            {secret ? (
              <RecoveryStep
                secret={secret}
                fileVariant="history"
                platform={platform}
                checkboxLabel={checkboxLabel}
                onGateChange={(_open, hint) => setGateHint(hint)}
                optionalPassword={{
                  value: draft,
                  onChange: (next) => {
                    setDraft(next);
                    setPwError(null);
                  },
                  error: pwError,
                }}
              />
            ) : keyError ? (
              <div className="flex flex-col items-center gap-3 py-4">
                <p role="alert" className="text-[12px] text-rose text-center">{keyError}</p>
                <SecondaryButton
                  type="button"
                  onClick={() => void loadKey()}
                  className="h-8 px-3.5 rounded-full text-[12px] font-medium text-slate"
                >
                  Try again
                </SecondaryButton>
              </div>
            ) : (
              <div
                aria-hidden={!loadingKey}
                className="h-[86px] rounded-[var(--radius-row)] animate-pulse"
                style={{ background: "var(--color-gold-mist)" }}
              />
            )}

            {doneError && <p role="alert" className="text-[12px] text-rose mt-3 text-center">{doneError}</p>}

            <div className="flex items-center justify-center gap-2.5 mt-5">
              {showIntro && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setStep("intro")}
                  className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                  style={glassPillStyle}
                >
                  Back
                </button>
              )}
              <RecoveryGateButton
                hint={secret ? gateHint : RECOVERY_GATE_HINT_SAVE}
                hintId={GATE_HINT_ID}
                busy={confirming}
                onClick={() => void handleDone()}
              >
                {confirming ? "Working..." : "Done"}
              </RecoveryGateButton>
            </div>
          </>
        )}
      </motion.div>
    </div>,
    document.body,
  );
}
