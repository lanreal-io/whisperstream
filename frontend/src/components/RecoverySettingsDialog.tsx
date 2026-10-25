/**
 * RecoverySettingsDialog: standard history's recovery settings (copy-review
 * section 7), reached from "Recovery settings" after setup. Reuses the View
 * recovery and Change password shells: Create new key (confirm, then the
 * shared RecoveryStep without the optional password) and add / change /
 * remove the recovery password.
 *
 * Create new key stages a pending key; the old key keeps working until Done
 * (after the gate) confirms the new one. Esc or the backdrop before the gate
 * shows the "return to your key" prompt, as Lockdown setup does; leaving
 * without confirming only discards the pending key.
 *
 * Dialog shell is hand-rolled. Never log keys or
 * passwords.
 */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { motion } from "framer-motion";
import {
  RECOVERY_GATE_HINT_SAVE,
  RecoveryAbandonPrompt,
  RecoveryGateButton,
  RecoveryStep,
} from "./RecoveryStep";
import { RecoveryPasswordFields } from "./RecoveryPasswordFields";
import { SecondaryButton } from "./SecondaryButton";
import {
  confirmHistoryRecovery,
  regenerateHistoryRecoveryKey,
  setHistoryRecoveryPassword,
} from "../lib/api/historyRecovery";
import {
  checkNewPassword,
  passwordRuleErrorFromResponse,
  passwordRuleMessage,
  type PasswordRuleError,
} from "../lib/passwordRule";
import { placeFor, type HistoryPlatform } from "../lib/historyRecoveryCopy";

type View = "main" | "key-confirm" | "key" | "password" | "remove";
type Success = "added" | "changed" | "removed";

const SUCCESS: Record<Success, string> = {
  added: "Recovery password added.",
  changed: "Recovery password changed.",
  removed: "Recovery password removed.",
};

export interface RecoverySettingsDialogProps {
  open: boolean;
  platform: HistoryPlatform;
  hasRecoveryPassword: boolean;
  onClose: () => void;
  /** Recovery state changed; refresh status. */
  onChanged: () => void;
}

const shellStyle: React.CSSProperties = {
  background: "var(--surface-modal)",
  backdropFilter: "blur(24px)",
  WebkitBackdropFilter: "blur(24px)",
  border: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass)",
};

// The modal card is near-solid white in light theme, so white-alpha borders
// vanish against it. The themed steel hairline is SecondaryButton's
// "structural line"; in dark it is a faint white edge.
const STEEL_EDGE = "1px solid var(--border-hairline-steel)";

const glassPillStyle: React.CSSProperties = {
  background: "var(--surface-control)",
  border: STEEL_EDGE,
  boxShadow: "inset 0 1px 0 var(--rim-specular)",
};

// Setting row: raised fill + specular rim (SealPanel's nested-row treatment)
// with the steel edge so the row reads on the white modal card.
const rowStyle: React.CSSProperties = {
  background: "var(--surface-raised)",
  border: STEEL_EDGE,
  boxShadow: "inset 0 1px 0 var(--rim-specular)",
};

const gradientPillStyle: React.CSSProperties = {
  background: "var(--accent-fill)",
  boxShadow: "var(--shadow-pearl)",
};

const pillClass = "px-5 py-2 rounded-full text-[13px] font-medium cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed";
const chipClass = "h-8 px-3.5 rounded-full text-[12px] font-medium whitespace-nowrap";

export function RecoverySettingsDialog({
  open,
  platform,
  hasRecoveryPassword,
  onClose,
  onChanged,
}: RecoverySettingsDialogProps) {
  const [view, setView] = useState<View>("main");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<Success | null>(null);
  const [secret, setSecret] = useState("");
  // Names the shown key so confirming promotes exactly this one.
  const [generation, setGeneration] = useState<string | null>(null);
  const [gateHint, setGateHint] = useState<string | null>(RECOVERY_GATE_HINT_SAVE);
  const [abandon, setAbandon] = useState(false);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [pwError, setPwError] = useState<PasswordRuleError | null>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);
  const returnRef = useRef<HTMLButtonElement>(null);
  const place = placeFor(platform);

  useEffect(() => {
    if (!open) return;
    setView("main");
    setBusy(false);
    setError(null);
    setSuccess(null);
    setSecret("");
    setAbandon(false);
  }, [open]);

  const go = (next: View) => {
    setView(next);
    setError(null);
    setAbandon(false);
    if (next === "password") {
      setPassword("");
      setConfirm("");
      setPwError(null);
      window.setTimeout(() => firstFieldRef.current?.focus(), 30);
    }
  };

  // Esc and the backdrop: back out of a sub-view, close from main, and never
  // drop an unsaved new key.
  const dismissRef = useRef<() => void>(() => {});
  dismissRef.current = () => {
    if (busy) return;
    if (view === "key") {
      if (gateHint !== null) {
        setAbandon(true);
        window.setTimeout(() => returnRef.current?.focus(), 0);
        return;
      }
      onClose();
      return;
    }
    if (view === "main") onClose();
    else go("main");
  };

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        dismissRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (!open) return null;

  const createKey = async () => {
    setBusy(true);
    setError(null);
    try {
      const pending = await regenerateHistoryRecoveryKey();
      setSecret(pending.recovery_key);
      setGeneration(pending.generation);
      setGateHint(RECOVERY_GATE_HINT_SAVE);
      setView("key");
    } catch {
      setError("Couldn't create a new recovery key. Try again.");
    } finally {
      setBusy(false);
    }
  };

  // Promote the pending key; only now does the previous key stop working.
  const confirmNewKey = async () => {
    setBusy(true);
    setError(null);
    try {
      await confirmHistoryRecovery(generation);
      onChanged();
      go("main");
    } catch {
      setError("Couldn't create a new recovery key. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const savePassword = async () => {
    const rule = checkNewPassword(password, confirm);
    if (rule) {
      setPwError(rule);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await setHistoryRecoveryPassword(password);
      setSuccess(hasRecoveryPassword ? "changed" : "added");
      onChanged();
      go("main");
    } catch (err) {
      const backendRule = passwordRuleErrorFromResponse(err);
      if (backendRule) setPwError(backendRule);
      else setError("Couldn't save your recovery password. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const removePassword = async () => {
    setBusy(true);
    setError(null);
    try {
      await setHistoryRecoveryPassword(null);
      setSuccess("removed");
      onChanged();
      go("main");
    } catch {
      setError("Couldn't remove your recovery password. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const heading = (text: string) => (
    <h2 id="recovery-settings-heading" className="font-display text-[18px] font-normal text-slate text-center mb-2">
      {text}
    </h2>
  );
  const errorLine = error && (
    <p role="alert" className="text-[12px] text-rose mt-3 text-center">{error}</p>
  );

  let content: React.ReactNode;
  if (view === "main") {
    content = (
      <>
        {heading("Recovery")}
        <div className="mt-4 space-y-2.5">
          <div
            className="flex items-center justify-between gap-3 rounded-[var(--radius-row)] px-4 py-3"
            style={rowStyle}
          >
            <span className="text-[13px] text-slate">Recovery key</span>
            <SecondaryButton type="button" onClick={() => go("key-confirm")} className={`${chipClass} text-slate`}>
              Create new key
            </SecondaryButton>
          </div>
          <div
            className="flex items-center justify-between gap-3 rounded-[var(--radius-row)] px-4 py-3"
            style={rowStyle}
          >
            <span className="flex flex-col">
              <span className="text-[13px] text-slate whitespace-nowrap">Recovery password</span>
              <span className="text-[11px] text-steel">{hasRecoveryPassword ? "Set" : "Not set"}</span>
            </span>
            <span className="flex items-center gap-2">
              {hasRecoveryPassword ? (
                <>
                  <SecondaryButton type="button" onClick={() => go("password")} className={`${chipClass} text-slate`}>
                    Change password
                  </SecondaryButton>
                  <SecondaryButton
                    type="button"
                    variant="destructive"
                    onClick={() => go("remove")}
                    className={chipClass}
                    style={{ color: "var(--color-rose)" }}
                  >
                    Remove password
                  </SecondaryButton>
                </>
              ) : (
                <SecondaryButton type="button" onClick={() => go("password")} className={`${chipClass} text-slate`}>
                  Add password
                </SecondaryButton>
              )}
            </span>
          </div>
        </div>
        <p role="status" className="text-[12px] text-mint mt-3 text-center min-h-[18px]">
          {success ? SUCCESS[success] : ""}
        </p>
        <div className="flex items-center justify-center mt-3">
          <button type="button" onClick={onClose} className={`${pillClass} text-white`} style={gradientPillStyle}>
            Done
          </button>
        </div>
      </>
    );
  } else if (view === "key-confirm") {
    content = (
      <>
        {heading("Create a new recovery key?")}
        <p className="text-[13px] text-steel leading-relaxed text-center">Your current recovery key will stop working.</p>
        {errorLine}
        <div className="flex items-center justify-center gap-2.5 mt-6">
          <button type="button" disabled={busy} onClick={() => go("main")} className={`${pillClass} text-slate`} style={glassPillStyle}>
            Cancel
          </button>
          <button type="button" disabled={busy} onClick={() => void createKey()} className={`${pillClass} text-white`} style={gradientPillStyle}>
            {busy ? "Working..." : "Create new key"}
          </button>
        </div>
      </>
    );
  } else if (view === "key") {
    content = (
      <>
        {heading("Your new recovery key")}
        <p className="text-[12px] text-steel mb-4 leading-relaxed text-center">Your previous recovery key no longer works.</p>
        <RecoveryStep
          secret={secret}
          fileVariant="history"
          platform={platform}
          checkboxLabel={`I saved my recovery key somewhere other than ${place}.`}
          onGateChange={(_open, hint) => {
            setGateHint(hint);
            if (hint === null) setAbandon(false);
          }}
        />
        <div className="flex items-center justify-center mt-5">
          <RecoveryGateButton hint={gateHint} hintId="recovery-settings-gate" className="px-6 py-2" busy={busy} onClick={() => void confirmNewKey()}>
            {busy ? "Working..." : "Done"}
          </RecoveryGateButton>
        </div>
        {errorLine}
        {abandon && gateHint !== null && (
          <RecoveryAbandonPrompt message={gateHint} onReturn={() => setAbandon(false)} buttonRef={returnRef} />
        )}
      </>
    );
  } else if (view === "password") {
    content = (
      <>
        {heading(hasRecoveryPassword ? "Change recovery password" : "Add a recovery password")}
        {!hasRecoveryPassword && (
          <p className="text-[13px] text-steel leading-relaxed text-center mb-4">
            Another way to restore history. It doesn't lock anything.
          </p>
        )}
        <div className={hasRecoveryPassword ? "mt-4" : undefined}>
          <RecoveryPasswordFields
            ref={firstFieldRef}
            idPrefix="recovery-settings"
            password={password}
            confirm={confirm}
            onPasswordChange={(value) => {
              setPassword(value);
              setPwError(null);
            }}
            onConfirmChange={(value) => {
              setConfirm(value);
              setPwError(null);
            }}
            onSubmit={() => void savePassword()}
            label="Recovery password"
            confirmLabel="Confirm recovery password"
            hint="At least 12 characters."
            errorText={pwError ? passwordRuleMessage(pwError) : null}
            errorField={pwError === "mismatch" ? "confirm" : pwError ? "password" : null}
          />
        </div>
        {errorLine}
        <div className="flex items-center justify-center gap-2.5 mt-6">
          <button type="button" disabled={busy} onClick={() => go("main")} className={`${pillClass} text-slate`} style={glassPillStyle}>
            Cancel
          </button>
          <button type="button" disabled={busy} onClick={() => void savePassword()} className={`${pillClass} text-white`} style={gradientPillStyle}>
            {busy ? "Saving..." : "Save password"}
          </button>
        </div>
      </>
    );
  } else {
    content = (
      <>
        {heading("Remove your recovery password?")}
        <p className="text-[13px] text-steel leading-relaxed text-center">Your recovery key still works.</p>
        {errorLine}
        <div className="flex items-center justify-center gap-2.5 mt-6">
          <button type="button" disabled={busy} onClick={() => go("main")} className={`${pillClass} text-slate`} style={glassPillStyle}>
            Keep password
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void removePassword()}
            className={pillClass}
            style={{
              background: "color-mix(in srgb, var(--color-rose) 15%, transparent)",
              border: "1px solid color-mix(in srgb, var(--color-rose) 40%, transparent)",
              boxShadow: "inset 0 1px 0 var(--rim-specular)",
              color: "var(--color-rose)",
            }}
          >
            Remove password
          </button>
        </div>
      </>
    );
  }

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        className="absolute inset-0"
        style={{ background: "var(--backdrop-strong)", backdropFilter: "blur(40px)", WebkitBackdropFilter: "blur(40px)" }}
        onClick={() => dismissRef.current()}
      />
      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30 }}
        className={`relative ${view === "key" || view === "main" ? "w-[520px]" : "w-[440px]"} max-w-full max-h-[calc(100dvh-32px)] overflow-y-auto rounded-[var(--radius-card)] p-7`}
        style={shellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="recovery-settings-heading"
      >
        {content}
      </motion.div>
    </div>,
    document.body,
  );
}
