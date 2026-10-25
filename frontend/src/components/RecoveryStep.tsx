/**
 * RecoveryStep: the shared recovery key step (design.md, history-key-recovery).
 *
 * Extracted from LockdownSetupModal's recovery step and the View recovery
 * dialog. Used by Lockdown setup and View recovery key (no optional password)
 * and by history setup, existing-user setup and Start fresh (with
 * `optionalPassword`). Order: gold key block (the only gold on the surface),
 * Copy / Print / Download with a mint tick once used, the collapsed optional
 * recovery password, then the "saved somewhere other than" checkbox. The caller
 * owns the heading, body copy and its action buttons.
 *
 * Gate: Continue needs one save action AND the checkbox. There is no visible
 * gate line; the caller renders `RecoveryGateButton` (aria-disabled, still
 * focusable) whose hover/focus tooltip carries the hint reported through
 * `onGateChange`.
 *
 * SECURITY: never log the recovery key or a password.
 */

import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { CustomCheckbox } from "./CustomCheckbox";
import { HoverTooltip } from "./HoverTooltip";
import { SecondaryButton } from "./SecondaryButton";
import { RecoveryPasswordFields } from "./RecoveryPasswordFields";
import {
  downloadRecoveryFile,
  printRecoveryFile,
  type RecoveryFileVariant,
  type RecoveryPlatform,
} from "../lib/lockdownRecovery";
import {
  checkNewPassword,
  passwordRuleMessage,
  type PasswordRuleError,
} from "../lib/passwordRule";

/** Locked gate hints (copy-review.md, Recovery step). */
export const RECOVERY_GATE_HINT_SAVE =
  "Copy, print, or download your recovery key to continue.";
export const RECOVERY_GATE_HINT_CONFIRM =
  "Confirm where you saved it to continue.";

export interface RecoveryPasswordDraft {
  password: string;
  confirm: string;
}

/**
 * Validate the optional recovery password on Done. A collapsed section (null)
 * or an expanded but empty one adds no password and is valid.
 */
export function checkOptionalRecoveryPassword(
  draft: RecoveryPasswordDraft | null,
): PasswordRuleError | null {
  if (!draft || (draft.password === "" && draft.confirm === "")) return null;
  return checkNewPassword(draft.password, draft.confirm);
}

type SaveAction = "copy" | "print" | "download";

export interface RecoveryStepProps {
  /** Shown once; never logged. */
  secret: string;
  /** Recovery-file name and text. */
  fileVariant: RecoveryFileVariant;
  /** Fills "this Mac" / "this PC" in the history recovery file. */
  platform?: RecoveryPlatform;
  /** "...other than this Mac/PC" (history) or "...this computer" (Lockdown). */
  checkboxLabel: string;
  /** Fires whenever the gate changes: open once a save action and the
   *  checkbox are both done; `hint` is the tooltip text while closed. */
  onGateChange: (open: boolean, hint: string | null) => void;
  /** History setup sets this; Lockdown setup and View recovery leave it unset. */
  optionalPassword?: {
    /** null while collapsed. */
    value: RecoveryPasswordDraft | null;
    onChange: (draft: RecoveryPasswordDraft | null) => void;
    error?: PasswordRuleError | null;
  };
}

const DONE_LABELS: Record<SaveAction, string> = {
  copy: "Copied",
  print: "Printed",
  download: "Saved",
};

const ANNOUNCE: Record<SaveAction, string> = {
  copy: "Recovery key copied.",
  print: "Recovery key sent to print.",
  download: "Recovery key saved.",
};

function MintTick() {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 14 14"
      fill="none"
      stroke="var(--color-mint)"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="shrink-0"
      aria-hidden="true"
    >
      <path d="M2.5 7.2l2.8 2.8 6-6.5" />
    </svg>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="shrink-0 text-steel transition-transform duration-200"
      style={{ transform: open ? "rotate(180deg)" : undefined }}
      aria-hidden="true"
    >
      <path d="M3 4.5l3 3 3-3" />
    </svg>
  );
}

export function RecoveryStep({
  secret,
  fileVariant,
  platform,
  checkboxLabel,
  onGateChange,
  optionalPassword,
}: RecoveryStepProps) {
  const [done, setDone] = useState<Record<SaveAction, boolean>>({
    copy: false,
    print: false,
    download: false,
  });
  const [checked, setChecked] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const [downloadError, setDownloadError] = useState(false);
  const onGateChangeRef = useRef(onGateChange);
  onGateChangeRef.current = onGateChange;

  // A new key starts a new gate. The reset happens during render, not in an
  // effect: a mount-time effect could flush after an early save or checkbox
  // click and silently undo it.
  const [gateSecret, setGateSecret] = useState(secret);
  if (gateSecret !== secret) {
    setGateSecret(secret);
    setDone({ copy: false, print: false, download: false });
    setChecked(false);
    setAnnouncement("");
    setDownloadError(false);
  }

  const saved = done.copy || done.print || done.download;
  const open = saved && checked;
  const hint = open
    ? null
    : saved
      ? RECOVERY_GATE_HINT_CONFIRM
      : RECOVERY_GATE_HINT_SAVE;

  useEffect(() => {
    onGateChangeRef.current(open, hint);
  }, [open, hint]);

  const markDone = (action: SaveAction) => {
    setDone((prev) => ({ ...prev, [action]: true }));
    setAnnouncement(ANNOUNCE[action]);
  };

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(secret);
    } catch {
      // Clipboard may be unavailable; the key stays visible to copy by hand,
      // so the action still counts toward the gate (existing Lockdown rule).
    }
    markDone("copy");
  };

  const handlePrint = () => {
    printRecoveryFile(secret, { variant: fileVariant, platform });
    markDone("print");
  };

  const handleDownload = async () => {
    setDownloadError(false);
    try {
      const result = await downloadRecoveryFile(secret, {
        variant: fileVariant,
        platform,
      });
      if (result === "saved") markDone("download");
    } catch {
      setDownloadError(true);
    }
  };

  const groups = secret.split("-");
  const expanded = optionalPassword?.value != null;
  const pwError = optionalPassword?.error ?? null;

  const pill = (action: SaveAction, label: string, aria: string, onClick: () => void) => (
    <SecondaryButton
      type="button"
      onClick={onClick}
      aria-label={aria}
      className="h-8 px-3.5 rounded-full text-[12px] font-medium text-slate inline-flex items-center gap-1.5"
      style={
        done[action]
          ? { border: "1px solid color-mix(in srgb, var(--color-mint) 45%, transparent)" }
          : undefined
      }
    >
      {done[action] && <MintTick />}
      {done[action] ? DONE_LABELS[action] : label}
    </SecondaryButton>
  );

  return (
    <div data-component="RecoveryStep">
      {/* GOLD recovery block: the only gold on this surface. */}
      <div
        className="rounded-[var(--radius-row)] px-5 py-4 text-center"
        style={{
          background: "var(--color-gold-mist)",
          border: "1px solid color-mix(in srgb, var(--color-gold) 35%, transparent)",
        }}
      >
        <span
          className="block font-display text-[10px] font-semibold uppercase tracking-[0.12em] mb-1.5"
          style={{ color: "var(--color-gold-deep)" }}
          aria-hidden="true"
        >
          Recovery key
        </span>
        <code
          aria-label="Recovery key"
          className="block font-mono text-[15px] tracking-[0.08em] text-slate tabular-nums break-all"
        >
          {groups.map((group, i) => (
            <span key={i}>
              {i > 0 && "-"}
              <span>{group}</span>
            </span>
          ))}
        </code>
      </div>

      <div className="flex items-center justify-center gap-2 mt-3">
        {pill("copy", "Copy", "Copy recovery key", () => void handleCopy())}
        {pill("print", "Print", "Print recovery key", handlePrint)}
        {pill("download", "Download", "Download recovery key", () => void handleDownload())}
      </div>
      <span role="status" className="sr-only">
        {announcement}
      </span>
      {downloadError && (
        <p role="alert" className="text-[11px] text-rose mt-2 text-center">
          Couldn't save the file. Try again.
        </p>
      )}

      {optionalPassword && (
        <div
          className="mt-3.5 rounded-[var(--radius-row)]"
          style={{
            background: "var(--surface-raised)",
            border: "1px solid var(--border-hairline-steel)",
          }}
        >
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() =>
              optionalPassword.onChange(
                expanded ? null : { password: "", confirm: "" },
              )
            }
            className="w-full flex items-center justify-between gap-2.5 px-3.5 py-2.5 text-left cursor-pointer rounded-[var(--radius-row)]"
          >
            <span className="text-[12.5px] font-medium text-slate">
              Also add a recovery password{" "}
              <span className="text-[11px] font-normal text-steel">Optional</span>
            </span>
            <Chevron open={expanded} />
          </button>
          <AnimatePresence initial={false}>
            {expanded && optionalPassword.value && (
              <motion.div
                key="optional-password"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2, ease: [0.2, 0, 0, 1] }}
                style={{ overflow: "hidden" }}
              >
                <div className="px-3.5 pb-3.5">
                  <p className="text-[12px] text-steel leading-relaxed mb-2.5">
                    Another way to restore history. It doesn't lock anything.
                  </p>
                  <RecoveryPasswordFields
                    password={optionalPassword.value.password}
                    confirm={optionalPassword.value.confirm}
                    onPasswordChange={(password) =>
                      optionalPassword.onChange({
                        ...optionalPassword.value!,
                        password,
                      })
                    }
                    onConfirmChange={(confirm) =>
                      optionalPassword.onChange({
                        ...optionalPassword.value!,
                        confirm,
                      })
                    }
                    label="Recovery password"
                    confirmLabel="Confirm recovery password"
                    hint="At least 12 characters."
                    errorText={pwError ? passwordRuleMessage(pwError) : null}
                    errorField={
                      pwError === "mismatch" ? "confirm" : pwError ? "password" : null
                    }
                  />
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}

      <label
        className={`flex items-start gap-2.5 cursor-pointer ${optionalPassword ? "mt-3.5" : "mt-3"}`}
      >
        <span className="mt-0.5">
          <CustomCheckbox checked={checked} onChange={setChecked} />
        </span>
        <span className="text-[12px] text-slate leading-relaxed">
          {checkboxLabel}
        </span>
      </label>
    </div>
  );
}

const gradientPillStyle: CSSProperties = {
  background: "var(--accent-fill)",
  boxShadow: "var(--shadow-pearl)",
};

/**
 * The caller's gated Continue/Done button. It stays focusable while gated
 * (aria-disabled, not disabled) so hover and focus both reveal the hint, which
 * is linked with aria-describedby and removed once the gate opens.
 */
export function RecoveryGateButton({
  hint,
  hintId,
  busy = false,
  onClick,
  children,
  className = "px-5 py-2",
}: {
  hint: string | null;
  hintId: string;
  busy?: boolean;
  onClick: () => void;
  children: ReactNode;
  className?: string;
}) {
  const gated = hint !== null;
  const inactive = gated || busy;
  return (
    <HoverTooltip
      content={hint}
      bubbleId={hintId}
      side="top"
      width="w-60"
      focusVisibleClassName="group-focus-within:opacity-100"
    >
      <button
        type="button"
        aria-disabled={inactive ? true : undefined}
        aria-describedby={gated ? hintId : undefined}
        onClick={() => {
          if (!inactive) onClick();
        }}
        className={`${className} rounded-full text-[13px] font-medium text-white transition-all duration-200 ${
          inactive ? "opacity-40 cursor-not-allowed" : "cursor-pointer"
        }`}
        style={gradientPillStyle}
      >
        {children}
      </button>
    </HoverTooltip>
  );
}

/**
 * Shown when Esc or the backdrop tries to close a new-key screen before the
 * gate is met (Lockdown setup's "return to your key" pattern). The message is
 * the current gate hint; the caller focuses `buttonRef` when it appears.
 */
export function RecoveryAbandonPrompt({
  message,
  onReturn,
  buttonRef,
}: {
  message: string;
  onReturn: () => void;
  buttonRef?: React.Ref<HTMLButtonElement>;
}) {
  return (
    <div
      role="alert"
      className="mt-5 rounded-[var(--radius-row)] px-4 py-3"
      style={{ background: "var(--surface-raised)", border: "1px solid var(--border-standard)" }}
    >
      <p className="text-[12px] text-slate mb-3 text-center leading-relaxed">{message}</p>
      <div className="flex items-center justify-center">
        <button
          ref={buttonRef}
          type="button"
          onClick={onReturn}
          className="px-4 py-1.5 rounded-full text-[12px] font-medium text-slate cursor-pointer transition-all duration-200"
          style={{
            background: "var(--surface-control)",
            border: "1px solid var(--border-emphasis)",
            boxShadow: "inset 0 1px 0 var(--rim-specular)",
          }}
        >
          Return to recovery key
        </button>
      </div>
    </div>
  );
}
