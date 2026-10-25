import { useEffect, useRef } from "react";
import { MotionConfig, motion } from "framer-motion";

/**
 * Targeted speech-model deletion confirm.
 *
 * Hand-rolled overlay + card: the dialog family deliberately shares no base
 * component, so the family-A tokens below are copied
 * on purpose. The destructive primary follows `TranscriptConfirmDialog`, the
 * app's canonical delete confirm.
 *
 * Copy is locked. The
 * safe action is `Keep model` rather than a bare Cancel, and it owns mount
 * focus, so a stray Enter can never delete. Enter is deliberately not bound to
 * the destructive action.
 *
 * Deletion itself is guarded on the backend (configured and loaded artifacts
 * are refused) and in the picker (no trash on the active model), so this only
 * ever opens for a removable one.
 */

const dialogShellStyle = {
  background: "var(--surface-modal)",
  backdropFilter: "blur(24px)",
  WebkitBackdropFilter: "blur(24px)",
  border: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass)",
};

const backdropStyle = {
  background: "var(--backdrop-strong)",
  backdropFilter: "blur(40px)",
  WebkitBackdropFilter: "blur(40px)",
};

const glassPillStyle = {
  background: "var(--surface-modal)",
  border: "1px solid var(--border-hairline-steel)",
  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
};

/* Destructive primary — rose, never the frost gradient. `color-mix` keeps it
   correct in both themes (Light --color-rose #C48B8B, Dark #D9A0A0). */
const destructivePillStyle = {
  background: "color-mix(in srgb, var(--color-rose) 18%, transparent)",
  border: "1px solid color-mix(in srgb, var(--color-rose) 45%, transparent)",
  color: "color-mix(in srgb, var(--color-rose) 70%, #3A1515)",
};

interface DeleteSttModelDialogProps {
  open: boolean;
  /** User-facing model name (`NVIDIA Parakeet` / `Qwen3 ASR`). */
  modelName: string;
  /** True while the delete request is in flight. */
  busy?: boolean;
  onConfirm: () => void;
  /** `Keep model`, Escape, and backdrop click all land here. */
  onCancel: () => void;
}

export function DeleteSttModelDialog({
  open,
  modelName,
  busy = false,
  onConfirm,
  onCancel,
}: DeleteSttModelDialogProps) {
  const keepButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) {
        event.preventDefault();
        onCancel();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, busy, onCancel]);

  useEffect(() => {
    if (open) keepButtonRef.current?.focus();
  }, [open]);

  if (!open) return null;

  return (
    <MotionConfig reducedMotion="user">
      <div className="fixed inset-0 z-50 flex items-center justify-center">
        <motion.div
          aria-hidden="true"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4 }}
          className="absolute inset-0"
          style={backdropStyle}
          onClick={busy ? undefined : onCancel}
        />

        <motion.div
          initial={{ scale: 0.92, opacity: 0, y: 12 }}
          animate={{ scale: 1, opacity: 1, y: 0 }}
          transition={{
            type: "spring",
            stiffness: 400,
            damping: 30,
            delay: 0.08,
          }}
          className="relative w-[420px] rounded-[var(--radius-card)] p-7"
          style={dialogShellStyle}
          role="dialog"
          aria-modal="true"
          aria-labelledby="delete-stt-model-heading"
          aria-describedby="delete-stt-model-description"
        >
          <h2
            id="delete-stt-model-heading"
            className="mb-3 text-center font-display text-[18px] font-normal text-slate"
          >
            Delete {modelName}?
          </h2>

          <p
            id="delete-stt-model-description"
            className="mb-6 text-center text-[13px] leading-relaxed text-steel"
          >
            This deletes the downloaded model from this device. You can download
            it again anytime.
          </p>

          <div className="flex items-center justify-center gap-2.5">
            <button
              ref={keepButtonRef}
              type="button"
              onClick={onCancel}
              disabled={busy}
              className="cursor-pointer rounded-full px-5 py-2 text-[13px] font-medium text-slate transition-all duration-200 disabled:cursor-default disabled:opacity-60"
              style={glassPillStyle}
            >
              Keep model
            </button>
            <button
              type="button"
              onClick={onConfirm}
              disabled={busy}
              className="cursor-pointer rounded-full px-5 py-2 text-[13px] font-medium transition-all duration-200 disabled:cursor-default disabled:opacity-60"
              style={destructivePillStyle}
            >
              Delete model
            </button>
          </div>
        </motion.div>
      </div>
    </MotionConfig>
  );
}
