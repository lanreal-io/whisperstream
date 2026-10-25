/**
 * LockdownSetupModal — Surface B, the branched Enable-Lockdown setup flow.
 *
 * A guided MULTI-STEP MODAL on the EnableLocalConfirmDialog shell vocabulary
 * (frost card var(--surface-modal) + blur(24px), --radius-card corners, scale
 * 0.92->1 spring entrance, glass + gradient pill buttons, backdrop
 * var(--backdrop-strong) + blur(40px)). Every surface/border/shadow routes
 * through the globals.css theme tokens so the card flips in light AND dark. Per
 * design.md the dialog family has NO shared base — this card hand-rolls its
 * overlay/card from the shared tokens (do NOT DRY into a <Dialog>).
 *
 * Steps (design.md Surface B, round 01 — a keep/don't-save BRANCH):
 *  1. Acknowledge     — vetted HIPAA disclosure + mandatory checkbox gate.
 *  2. Transcripts      — "Keep encrypted history" vs "Don't save transcripts".
 *                        Keep reveals the reused Save-audio toggle + Retention
 *                        picker (locked SettingsPopover labels), seeded from
 *                        current config.
 *  3. Password (keep)  — PROGRESSIVE: only the Password field shows until it is
 *                        non-empty, then the Confirm field mounts. Continue
 *                        calls the password enable path (mints the recovery
 *                        code).
 *  4. Recovery (keep)  — the shared RecoveryStep (gold key block + Copy /
 *                        Print / Download + checkbox) without the optional
 *                        password; its gate hard-gates "Finish setup".
 *  5. Done             — full SealPanel (keep) or slim SealPanel (don't-save).
 *                        The SINGLE combined config write fires here. Don't-save
 *                        also runs the no-password enable here.
 *
 * The flow has no step-count indicator: the total is branch-dependent (3 vs 5),
 * so a running "Step X of Y" was inaccurate and was removed. NEVER log the
 * password or the recovery code. All copy is verbatim from copy-review.md
 * section B; the HIPAA-sensitive strings (Step 1, the recovery-file text) are
 * legally vetted — do not reword.
 */

import { useState, useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { HistoryKeychainExplanation } from "./HistoryRecoverySetupDialog";
import { CustomCheckbox } from "./CustomCheckbox";
import { HoverTooltip } from "./HoverTooltip";
import { SealPanel } from "./SealPanel";
import { Toggle } from "./Toggle";
import { Dropdown } from "./Dropdown";
import { RETENTION_OPTIONS } from "./SettingsPopover";
import { enableLockdown, acknowledgeLockdown, isHistoryDisabledByOrganizationError, HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE } from "../lib/api/lockdown";
import { type AppConfig } from "../lib/api/config";
import { RecoveryGateButton, RecoveryStep } from "./RecoveryStep";
import { RecoveryPasswordFields } from "./RecoveryPasswordFields";
import {
  checkNewPassword,
  passwordRuleErrorFromResponse,
  passwordRuleMessage,
  type PasswordRuleError,
} from "../lib/passwordRule";

/* ---------- Dialog shell tokens ----------
   Theme tokens (globals.css) mirroring the post-merge EnableLocalConfirmDialog
   shell so the card flips correctly in light AND dark. Per the "no shared base,
   copy the tokens" rule the values are routed through the
   shared surface/backdrop/border/shadow variables rather than hardcoded rgba.
   The card is widened (w-[520px]) so the tallest step fits the app window. */
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

/** Small mint check for the selected transcripts-choice card. --color-mint is
 *  reserved for hardened-status affirmation, which the selected card reads as. */
function MintCheck() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 14 14"
      fill="none"
      stroke="var(--color-mint)"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="shrink-0"
      aria-hidden="true"
    >
      <path d="M2.5 7.2l2.8 2.8 6-6.5" />
    </svg>
  );
}

/** Version of the Step-1 HIPAA acknowledgment copy. BUMP THIS whenever ANY
 *  acknowledgment string below changes in meaning (a bullet, the heading, the
 *  disclaimer): a bump invalidates every stored acceptance so Step 1 re-shows
 *  and the user re-acknowledges the new copy — the EULA-amendment analogue.
 *  Legal condition (b). The backend persists
 *  {ack_version, accepted-at, software_version}; only an exact version match
 *  lets the screen be skipped. */
export const LOCKDOWN_ACK_VERSION = "3";

type Step = "ack" | "transcripts" | "keychain" | "password" | "recovery" | "done";
type Branch = "keep" | "dont-save";

interface LockdownSetupModalProps {
  historyDisabled?: boolean;
  /** First Mac history enable must explain and explicitly consent to Keychain. */
  requiresKeychainConsent?: boolean;
  /** When false, the component renders nothing. */
  open: boolean;
  /** Idle auto-lock window (seconds, 0 = Never) persisted alongside
   *  lockdown_mode, and sent to the keep-path enable call. */
  idleLockSeconds: number;
  /** Current save_audio config, used to SEED the keep-path audio toggle (do not
   *  force a new default). Defaults to true (the backend default) if omitted. */
  currentSaveAudio?: boolean;
  /** Current transcript_retention_days config, used to SEED the keep-path
   *  retention picker. Defaults to 7 (the backend default) if omitted. */
  currentTranscriptRetentionDays?: number;
  /** Which step to open on. "ack" (default) is the full flow from the master
   *  toggle. "transcripts" is the deep-link used when the user starts saving
   *  transcripts while already in a no-envelope lockdown — it lands on the
   *  "Keep a transcript history?" step with the Keep option pre-selected
   *  (Step 1 is still shown FIRST if it has not been acknowledged yet). */
  initialStep?: "ack" | "transcripts";
  /** Whether the Step-1 HIPAA acknowledgment has already been accepted for the
   *  current LOCKDOWN_ACK_VERSION. When true the acknowledgment screen is
   *  skipped (legal-reviewed: shown once, re-shown on a copy-version bump). */
  ackAccepted?: boolean;
  /** Backs out the whole flow. Persists nothing unless an enable call already
   *  fired server-side. */
  onClose: () => void;
  /** Adopt the backend-sanitized config returned by the enable call. The enable
   *  endpoint is authoritative (it forces provider local, turns AI cleanup off
   *  when required, and persists history settings), so the client REPLACES its
   *  config with what the server returns instead of echoing a stale copy back. */
  onConfigSynced: (config: AppConfig) => void;
  /** Re-fetch backend lock status after enable / acknowledge so the rest of the
   *  UI reflects the new reality (useLockdown.refresh). */
  onLockRefresh?: () => void;
}

export function LockdownSetupModal({
  open,
  requiresKeychainConsent = false,
  historyDisabled = false,
  idleLockSeconds,
  currentSaveAudio,
  currentTranscriptRetentionDays,
  initialStep = "ack",
  ackAccepted = false,
  onClose,
  onConfigSynced,
  onLockRefresh,
}: LockdownSetupModalProps) {
  const [step, setStep] = useState<Step>("ack");
  const [branch, setBranch] = useState<Branch | null>(null);
  const [keychainConsent, setKeychainConsent] = useState(false);

  // Step 1 — acknowledgment gate.
  const [ackChecked, setAckChecked] = useState(false);

  // Step 2 — transcripts choice + the reused audio/retention sub-controls.
  const [choice, setChoice] = useState<Branch | null>(null);
  const [saveAudio, setSaveAudio] = useState<boolean>(currentSaveAudio ?? true);
  const [retentionDays, setRetentionDays] = useState<number>(
    currentTranscriptRetentionDays ?? 7,
  );

  // Step 3 — password local state.
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [pwError, setPwError] = useState<PasswordRuleError | null>(null);

  // Step 4 — recovery key + the hard gate (owned by RecoveryStep: any of
  // Copy / Print / Download plus the checkbox). Null hint = gate open.
  const [recoveryCode, setRecoveryCode] = useState("");
  const [gateHint, setGateHint] = useState<string | null>(null);
  // The confirmed-abandon prompt (B8) shown when Esc/backdrop is hit on Step 4.
  const [abandonPrompt, setAbandonPrompt] = useState(false);

  // Async state shared by the two enable calls (keep-path Step 3, don't-save Done).
  const [enabling, setEnabling] = useState(false);
  const [enableError, setEnableError] = useState<string | null>(null);

  const firstFieldRef = useRef<HTMLInputElement>(null);
  const ackCancelRef = useRef<HTMLButtonElement>(null);
  const recoveryReminderRef = useRef<HTMLButtonElement>(null);
  // Always-current dismiss handler, so the Esc listener can subscribe once.
  const dismissRef = useRef<() => void>(() => {});

  // Reset everything whenever the modal opens fresh (re-seed from current
  // config). Keyed on `open` ONLY: initialStep / ackAccepted / the seed props
  // are read once here, at open time. They are intentionally NOT dependencies —
  // persisting the acknowledgment mid-flow flips ackAccepted, and re-running
  // this reset then would kick the user back to the transcripts step.
  useEffect(() => {
    if (!open) return;
    // Skip Step 1 only when it has already been accepted for the current copy
    // version (fail closed otherwise). The deep-link ("start saving" while
    // already locked down) pre-selects the Keep option.
    const skipAck = ackAccepted === true;
    const deepLinkKeep = initialStep === "transcripts";
    setStep(skipAck ? "transcripts" : "ack");
    setBranch(null);
    setKeychainConsent(false);
    // Pre-check the gate when it was already accepted (only matters if the user
    // navigates Back to the skipped Step 1).
    setAckChecked(skipAck);
    setChoice(historyDisabled ? "dont-save" : deepLinkKeep ? "keep" : null);
    setSaveAudio(currentSaveAudio ?? true);
    setRetentionDays(currentTranscriptRetentionDays ?? 7);
    setPassword("");
    setConfirm("");
    setPwError(null);
    setRecoveryCode("");
    setGateHint(null);
    setAbandonPrompt(false);
    setEnabling(false);
    setEnableError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Autofocus the safe action on acknowledgment, or the password field when
  // that step mounts.
  useEffect(() => {
    if (open && step === "ack") {
      const id = setTimeout(() => ackCancelRef.current?.focus(), 60);
      return () => clearTimeout(id);
    }
    if (open && step === "password") {
      const id = setTimeout(() => firstFieldRef.current?.focus(), 60);
      return () => clearTimeout(id);
    }
  }, [open, step]);

  useEffect(() => {
    if (open && step === "recovery" && abandonPrompt) {
      const id = setTimeout(() => recoveryReminderRef.current?.focus(), 0);
      return () => clearTimeout(id);
    }
  }, [abandonPrompt, open, step]);

  const passwordValid = checkNewPassword(password, confirm) === null;

  // The hard gate (Decision 5): a save action AND the checkbox.
  const turnOnEnabled = recoveryCode !== "" && gateHint === null;

  // --- Step 5 (Done): finalize, then close. ---
  // Don't-save: run the no-password (no-envelope) enable HERE and adopt the
  // returned config. Keep-path: the backend was already enabled AND its config
  // adopted at the password step, so Done just closes.
  const handleDone = async () => {
    if (enabling) return;

    if (branch === "dont-save") {
      setEnabling(true);
      setEnableError(null);
      try {
        // No-password / history-off enable: no envelope, no recovery code. The
        // backend returns the sanitized config (provider forced local, AI
        // cleanup turned off if it was on with a cloud provider); adopt it so
        // the app reflects the server truth without a stale follow-up PUT.
        const { config } = await enableLockdown({ historyEnabled: false });
        onConfigSynced(config);
      } catch (err) {
        setEnableError(
          isHistoryDisabledByOrganizationError(err)
            ? HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE
            : err instanceof Error
            ? err.message
            : "Could not turn on Lockdown Mode.",
        );
        setEnabling(false);
        return;
      }
      setEnabling(false);
    }
    onLockRefresh?.();
    onClose();
  };

  // Esc / backdrop behavior is step-dependent:
  //  - Step 4 (recovery): show the B8 abandon prompt (lockdown is already
  //    enabled server-side, so don't silently drop).
  //  - Step 5 (done): commit (same as clicking Done) so the config write always
  //    lands once the user reaches the success screen.
  //  - Any other step: cancel outright (nothing persisted).
  dismissRef.current = () => {
    if (step === "recovery") {
      setAbandonPrompt(true);
      return;
    }
    if (step === "done") {
      void handleDone();
      return;
    }
    onClose();
  };

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

  // --- Step 1 -> acknowledgment accepted: persist it (so future enables can
  // skip Step 1 until the copy version is bumped) then advance. Fire-and-forget:
  // advancing must not block on the write, and a failed write simply re-shows
  // Step 1 next time (fail closed). ---
  const handleAckContinue = () => {
    acknowledgeLockdown(LOCKDOWN_ACK_VERSION)
      .then(() => onLockRefresh?.())
      .catch(() => undefined);
    setStep("transcripts");
  };

  // --- Step 2 -> branch resolution ---
  const handleTranscriptsContinue = () => {
    if (!choice) return;
    const effectiveChoice = historyDisabled ? "dont-save" : choice;
    setBranch(effectiveChoice);
    setStep(effectiveChoice === "keep"
      ? (requiresKeychainConsent ? "keychain" : "password")
      : "done");
  };

  // --- Step 3: validate + mint the recovery code, advance to Step 4 ---
  const handlePasswordContinue = async () => {
    if (requiresKeychainConsent && !keychainConsent) {
      setStep("keychain");
      return;
    }
    const ruleError = checkNewPassword(password, confirm);
    if (ruleError) {
      setPwError(ruleError);
      return;
    }
    setPwError(null);
    setEnableError(null);
    setEnabling(true);
    try {
      // The keep-history enable mints + returns the one-time recovery code AND
      // the sanitized config. History-on + save_audio + retention are persisted
      // in this same call, so no follow-up config PUT is needed; adopt the
      // returned config so the app reflects the server truth immediately.
      const { recovery_code, config } = await enableLockdown({
        password,
        ...(keychainConsent ? { keychainConsent: true } : {}),
        idleLockSeconds,
        historyEnabled: true,
        saveAudio,
        transcriptRetentionDays: retentionDays,
      });
      onConfigSynced(config);
      setRecoveryCode(recovery_code ?? "");
      setStep("recovery");
    } catch (err) {
      // The backend owns the common-password block for new passwords.
      const ruleError = passwordRuleErrorFromResponse(err);
      if (ruleError) {
        setPwError(ruleError);
        return;
      }
      setEnableError(
        isHistoryDisabledByOrganizationError(err) ? HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE : err instanceof Error ? err.message : "Could not turn on Lockdown Mode.",
      );
    } finally {
      setEnabling(false);
    }
  };

  const handleTurnOn = () => {
    if (!turnOnEnabled) return;
    setStep("done");
  };

  // Step-1 acknowledgment copy (vetted, legal section 1).
  const ackBullets = [
    "Transcription and cleanup run on this device.",
    "Saved transcript text and audio are encrypted behind your password.",
    "Safe diagnostic logs stay local unless you export and share them.",
    "History locks after inactivity.",
  ];

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
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[520px] max-w-[calc(100vw-32px)] max-h-[calc(100dvh-32px)] overflow-y-auto rounded-[var(--radius-card)] px-7 py-6"
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="lockdown-setup-heading"
        aria-describedby={step === "ack" ? "lockdown-setup-scope" : undefined}
      >
        {/* ---------------- Step 1: Acknowledge ---------------- */}
        {step === "ack" && (
          <>
            <h2
              id="lockdown-setup-heading"
              className="font-display text-[18px] font-normal text-slate text-center mb-2"
            >
              Before you turn on Lockdown Mode
            </h2>
            <p className="text-[12px] text-steel mb-2.5 leading-relaxed text-center">
              Lockdown Mode changes how Whisperstream handles your dictation and
              history. Here is what turns on:
            </p>

            <p
              id="lockdown-setup-scope"
              className="text-[12px] text-slate mb-2.5 leading-relaxed text-center"
            >
              Lockdown applies to Whisperstream transcription, cleanup, and saved
              history.
            </p>

            <ul className="space-y-1.5 mb-3">
              {ackBullets.map((b) => (
                <li
                  key={b}
                  className="flex items-start gap-2 text-[12px] text-steel leading-relaxed"
                >
                  <span
                    aria-hidden="true"
                    className="mt-[6px] w-1 h-1 rounded-full shrink-0 bg-steel/50"
                  />
                  <span>{b}</span>
                </li>
              ))}
            </ul>

            <p className="text-[12px] text-steel mb-3 leading-relaxed">
              Lockdown Mode is designed to support HIPAA-conscious workflows. It
              is not a HIPAA certification, and it does not replace your own
              compliance obligations.
            </p>

            {/* Mandatory acknowledgment gate (vetted long label). */}
            <label className="flex items-start gap-2.5 mb-2.5 cursor-pointer">
              <span className="mt-0.5">
                <CustomCheckbox checked={ackChecked} onChange={setAckChecked} />
              </span>
              <span className="text-[12px] text-slate leading-relaxed">
                I understand what Lockdown Mode covers and that it does not
                replace my compliance obligations.
              </span>
            </label>

            <div className="mb-4">
              <a
                href="/hipaa-summary.pdf"
                target="_blank"
                rel="noopener noreferrer"
                className="text-[12px] underline underline-offset-2 cursor-pointer transition-colors"
                style={{ color: "var(--color-frost-deep)" }}
              >
                Download the HIPAA summary (PDF)
              </a>
            </div>

            <div className="flex items-center justify-center gap-2.5">
              <button
                ref={ackCancelRef}
                onClick={onClose}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                style={glassPillStyle}
              >
                Cancel
              </button>
              {/* Disabled <button> swallows hover, so the HoverTooltip span is
                  the hover target and the button drops pointer-events while
                  disabled — the bubble explains the acknowledgment gate. */}
              <HoverTooltip
                content={
                  ackChecked ? null : "Accept the acknowledgment to continue."
                }
                side="auto"
                width="w-56"
              >
                <button
                  disabled={!ackChecked}
                  onClick={handleAckContinue}
                  className={
                    "px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed" +
                    (ackChecked ? "" : " pointer-events-none")
                  }
                  style={gradientPillStyle}
                >
                  Continue
                </button>
              </HoverTooltip>
            </div>
          </>
        )}

        {/* ---------------- Step 2: Transcripts choice ---------------- */}
        {step === "transcripts" && (
          <>
            <h2
              id="lockdown-setup-heading"
              className="font-display text-[18px] font-normal text-slate text-center mb-2"
            >
              {historyDisabled ? "History is disabled" : "Keep a transcript history?"}
            </h2>
            <p className="text-[12px] text-steel mb-4 leading-relaxed text-center">
              {historyDisabled
                ? HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE
                : "Lockdown can save your transcripts, encrypted and locked behind a password, or skip saving them entirely."}
            </p>

            {!historyDisabled && <div className="space-y-2.5 mb-2">
              {/* Keep card + its reused sub-controls, grouped so the Save-audio
                  toggle + Retention picker attach directly beneath the Keep
                  choice (issue 4), with Don't-save staying below. */}
              <div>
                <ChoiceCard
                  title="Keep encrypted history"
                  desc="Transcripts are saved on this device, encrypted, and locked behind a password."
                  selected={choice === "keep"}
                  onSelect={() => setChoice("keep")}
                />

                {/* Keep -> reveal the reused Save-audio toggle + Retention
                    picker, seeded from current config (locked SettingsPopover
                    labels). */}
                <AnimatePresence initial={false}>
                  {choice === "keep" && (
                    <motion.div
                      key="keep-options"
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: "auto", opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
                      style={{ overflow: "hidden" }}
                    >
                      <div
                        className="mt-2 rounded-[var(--radius-row)] px-4 py-3 space-y-3"
                        style={{
                          background: "var(--surface-raised)",
                          border: "1px solid var(--border-standard)",
                        }}
                      >
                        <div className="flex items-center justify-between gap-3">
                          <span className="text-[13px] text-slate">
                            Save audio
                          </span>
                          <Toggle checked={saveAudio} onChange={setSaveAudio} />
                        </div>
                        <div className="flex items-center justify-between gap-3">
                          <span className="text-[13px] text-slate">Keep for</span>
                          <Dropdown
                            value={retentionDays}
                            options={RETENTION_OPTIONS.map((o) => ({ ...o }))}
                            onChange={(days) => setRetentionDays(days)}
                            ariaLabel="Keep transcripts for"
                            triggerClassName="w-[110px]"
                          />
                        </div>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>

              <ChoiceCard
                title="Don't save transcripts"
                desc="Nothing is kept after you dictate. There is no history to lock."
                selected={choice === "dont-save"}
                onSelect={() => setChoice("dont-save")}
              />
            </div>}

            <div className="flex items-center justify-center gap-2.5 mt-6">
              <button
                onClick={() => setStep("ack")}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                style={glassPillStyle}
              >
                Back
              </button>
              <button
                disabled={!choice}
                onClick={handleTranscriptsContinue}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
                style={gradientPillStyle}
              >
                Continue
              </button>
            </div>
          </>
        )}

        {step === "keychain" && (
          <>
            <h2 id="lockdown-setup-heading" className="font-display text-[18px] font-normal text-slate text-center mb-4">Save transcripts on this Mac?</h2>
            <HistoryKeychainExplanation saveAudio={saveAudio} />
            <div className="flex items-center justify-center gap-3 mt-6">
              <button autoFocus onClick={() => setStep("transcripts")} className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer" style={glassPillStyle}>Back</button>
              <button onClick={() => { setKeychainConsent(true); setStep("password"); }} className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer" style={gradientPillStyle}>Continue to password</button>
            </div>
          </>
        )}

        {/* ---------------- Step 3: Password (keep-path, progressive) ------- */}
        {step === "password" && (
          <>
            <h2
              id="lockdown-setup-heading"
              className="font-display text-[18px] font-normal text-slate text-center mb-2"
            >
              Set a transcript password
            </h2>
            <p className="text-[13px] text-steel mb-5 leading-relaxed text-center">
              This password locks your transcript history. You'll enter it to
              open transcripts after the app locks.
            </p>

            <RecoveryPasswordFields
              ref={firstFieldRef}
              idPrefix="lockdown"
              progressive
              password={password}
              confirm={confirm}
              onPasswordChange={(value) => {
                setPassword(value);
                if (pwError) setPwError(null);
              }}
              onConfirmChange={(value) => {
                setConfirm(value);
                if (pwError) setPwError(null);
              }}
              onSubmit={() => {
                if (passwordValid && !enabling) void handlePasswordContinue();
              }}
              label="Password"
              confirmLabel="Confirm password"
              hint="Use at least 12 characters."
              errorText={pwError ? passwordRuleMessage(pwError) : null}
              errorField={pwError === "mismatch" ? "confirm" : pwError ? "password" : null}
            />

            {enableError && (
              <p className="text-[11px] text-rose mt-2 ml-1">{enableError}</p>
            )}

            <div className="flex items-center justify-center gap-2.5 mt-6">
              <button
                onClick={() => setStep("transcripts")}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                style={glassPillStyle}
              >
                Back
              </button>
              <button
                disabled={!passwordValid || enabling}
                onClick={handlePasswordContinue}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
                style={gradientPillStyle}
              >
                {enabling ? "Working..." : "Continue"}
              </button>
            </div>
          </>
        )}

        {/* ---------------- Step 4: Save your recovery key ---------------- */}
        {step === "recovery" && (
          <>
            <h2
              id="lockdown-setup-heading"
              className="font-display text-[18px] font-normal text-slate text-center mb-3"
            >
              Save your recovery key
            </h2>

            <RecoveryStep
              secret={recoveryCode}
              fileVariant="lockdown"
              checkboxLabel="I've saved my recovery key somewhere other than this computer."
              onGateChange={(_open, hint) => setGateHint(hint)}
            />

            {/* B3 intro body (steel) and B4 data-loss warning (rose). */}
            <p className="text-[12px] text-steel mt-4 mb-2 leading-relaxed text-center">
              This one-time key is the only way back into your transcripts if
              you forget your password. Save it somewhere safe. We can't show it
              again.
            </p>
            <p className="text-[12px] text-rose mb-5 leading-relaxed text-center">
              If you lose both your password and recovery key, your history
              can't be restored. Reset deletes it instead.
            </p>

            <div className="flex items-center justify-center">
              {/* B7 HARD GATE: aria-disabled until a save action and the
                  checkbox; the hover/focus hint names what is missing. */}
              <RecoveryGateButton
                hint={gateHint}
                hintId="lockdown-setup-recovery-gate"
                onClick={handleTurnOn}
              >
                Finish setup
              </RecoveryGateButton>
            </div>

            {/* B8 abandon prompt (Esc / backdrop on the recovery step). */}
            {abandonPrompt && (
              <div
                className="mt-5 rounded-[var(--radius-row)] px-4 py-3"
                style={{
                  background: "var(--surface-raised)",
                  border: "1px solid var(--border-standard)",
                }}
                role="alert"
              >
                <p className="text-[12px] text-slate mb-3 text-center leading-relaxed">
                  Lockdown Mode is already on. Save your recovery key before
                  finishing.
                </p>
                <div className="flex items-center justify-center">
                  <button
                    ref={recoveryReminderRef}
                    onClick={() => setAbandonPrompt(false)}
                    className="px-4 py-1.5 rounded-full text-[12px] font-medium text-slate cursor-pointer transition-all duration-200"
                    style={glassPillStyle}
                  >
                    Return to recovery key
                  </button>
                </div>
              </div>
            )}
          </>
        )}

        {/* ---------------- Step 5: Done ---------------- */}
        {step === "done" && (
          <>
            <h2
              id="lockdown-setup-heading"
              className="font-display text-[18px] font-normal text-slate text-center mb-4"
            >
              Lockdown Mode is on
            </h2>

            <div className="mb-5">
              {branch === "dont-save" ? (
                // Slim seal panel — the two no-transcripts bullets.
                <SealPanel
                  compact
                  items={[
                    "Transcription and cleanup run on this device.",
                  ]}
                />
              ) : (
                // Full seal panel — the keep-history protections.
                <SealPanel
                  compact
                  items={[
                    "On-device transcription and cleanup. Saved history locked.",
                  ]}
                />
              )}
            </div>

            {enableError && (
              <p className="text-[11px] text-rose mb-3 ml-1 text-center">
                {enableError}
              </p>
            )}

            <div className="flex items-center justify-center">
              <button
                onClick={() => void handleDone()}
                disabled={enabling}
                className="px-6 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
                style={gradientPillStyle}
              >
                {enabling ? "Working..." : "Done"}
              </button>
            </div>
          </>
        )}
      </motion.div>
    </div>
  );
}

/** One selectable transcripts-choice card (frost control vocabulary). Selected
 *  = denser 0.45 frost + a frost ring + a mint check. */
function ChoiceCard({
  title,
  desc,
  selected,
  onSelect,
}: {
  title: string;
  desc: string;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      className="w-full text-left rounded-[var(--radius-row)] px-4 py-3 cursor-pointer transition-all duration-200"
      style={{
        // Selected lifts to the hover surface (denser) with a frost accent
        // edge + ring; unselected rests on the raised surface. Both flip per
        // theme via the tokens.
        background: selected ? "var(--surface-hover)" : "var(--surface-raised)",
        border: selected
          ? "1px solid var(--border-accent)"
          : "1px solid var(--border-standard)",
        boxShadow: selected
          ? "0 0 0 1px var(--border-accent), inset 0 1px 0 var(--rim-specular)"
          : "inset 0 1px 0 var(--rim-specular)",
      }}
    >
      <div className="flex items-center justify-between gap-3">
        <span className="text-[13px] font-medium text-slate">{title}</span>
        {selected && <MintCheck />}
      </div>
      <p className="text-[12px] text-steel mt-1 leading-relaxed">{desc}</p>
    </button>
  );
}

export default LockdownSetupModal;
