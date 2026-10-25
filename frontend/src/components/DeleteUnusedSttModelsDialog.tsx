import { useEffect, useRef } from "react";
import { MotionConfig, motion } from "framer-motion";

/**
 * Bulk unused-speech-model deletion confirm.
 *
 * Hand-rolled overlay + card: the dialog family deliberately shares no base
 * component, so the family-A tokens below are copied
 * on purpose. This is `DeleteSttModelDialog`'s sibling and follows the same
 * destructive recipe: rose primary, `Keep models` as the safe action, mount
 * focus on it, Enter deliberately unbound.
 *
 * Copy is locked.
 *
 * The list is the reason this dialog exists rather than a one-line confirm.
 * Deleting "unused models" is otherwise invisible: the picker groups Parakeet's
 * English-only and multilingual artifacts under one name, so a user who has
 * only ever seen `NVIDIA Parakeet` and `Qwen3 ASR` cannot otherwise tell what
 * a bulk delete is about to remove. This is the ONE surface allowed to
 * distinguish them (`NVIDIA Parakeet (English)` / `NVIDIA Parakeet
 * (Multilingual)`); the host builds those names from the authoritative
 * inventory and this component only renders them.
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

interface DeleteUnusedSttModelsDialogProps {
  open: boolean;
  /** Friendly names of the exact artifacts about to be removed, in the order
   *  they should be read. */
  modelNames: string[];
  /** Preformatted total the deletion frees, e.g. `1.5 GB`. */
  totalSize: string;
  /** True while the bulk delete is in flight. */
  busy?: boolean;
  onConfirm: () => void;
  /** `Keep models`, Escape, and backdrop click all land here. */
  onCancel: () => void;
}

export function DeleteUnusedSttModelsDialog({
  open,
  modelNames,
  totalSize,
  busy = false,
  onConfirm,
  onCancel,
}: DeleteUnusedSttModelsDialogProps) {
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
          aria-labelledby="delete-unused-stt-models-heading"
          aria-describedby="delete-unused-stt-models-description"
        >
          <h2
            id="delete-unused-stt-models-heading"
            className="mb-3 text-center font-display text-[18px] font-normal text-slate"
          >
            Delete unused models?
          </h2>

          <p
            id="delete-unused-stt-models-description"
            className="mb-3 text-center text-[13px] leading-relaxed text-steel"
          >
            This deletes these unused models and frees {totalSize}:
          </p>

          {/* Primary ink against the steel paragraph is what separates the
              names from the prose. A control-surface well was tried and is a
              Light-mode bug: --surface-control is white/0.45 over the modal's
              white/0.96, so the box is invisible in Light and only reads in
              Dark. Centered because every action card in this family is. */}
          <ul className="mb-6 space-y-1 text-center">
            {modelNames.map((name) => (
              <li key={name} className="text-[13px] text-slate">
                {name}
              </li>
            ))}
          </ul>

          <div className="flex items-center justify-center gap-2.5">
            <button
              ref={keepButtonRef}
              type="button"
              onClick={onCancel}
              disabled={busy}
              className="cursor-pointer rounded-full px-5 py-2 text-[13px] font-medium text-slate transition-all duration-200 disabled:cursor-default disabled:opacity-60"
              style={glassPillStyle}
            >
              Keep models
            </button>
            <button
              type="button"
              onClick={onConfirm}
              disabled={busy}
              className="cursor-pointer rounded-full px-5 py-2 text-[13px] font-medium transition-all duration-200 disabled:cursor-default disabled:opacity-60"
              style={destructivePillStyle}
            >
              Delete unused models
            </button>
          </div>
        </motion.div>
      </div>
    </MotionConfig>
  );
}
