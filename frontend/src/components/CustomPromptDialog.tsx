import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { getBuiltinPrompt } from "../lib/api/enhancement";
import { errorType } from "../lib/logCapture";

/* ---------- Dialog shell tokens ----------
   Shared with EnableLocalConfirmDialog / LowRamDialog /
   DataLeavesDialog / PrivacyModal — the frost-glass dialog vocabulary.
   Per LOCKED Decision 5-B the card is 520px wide with a max-height of
   480px and the footer pairs a left-aligned "Reset to default" link
   with right-aligned Cancel / Save pills. */
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

const gradientPillStyle = {
  background: "var(--accent-fill)",
  boxShadow:
    "var(--shadow-pearl)",
};

interface CustomPromptDialogProps {
  /** When false, the component renders nothing. Mirrors LowRamDialog. */
  open: boolean;
  /** Seed value for the textarea. The draft state is re-seeded from
   *  this prop every time `open` flips to true so reopening the dialog
   *  picks up the latest stored prompt rather than a stale draft. */
  initialPrompt: string;
  /** Primary action (gradient pill, right side): user accepts the
   *  edited prompt. Parent persists to config. */
  onSave: (prompt: string) => void;
  /** Secondary action (glass pill) AND Esc / backdrop click: user
   *  discards the draft and closes the dialog. */
  onCancel: () => void;
}

export function CustomPromptDialog({
  open,
  initialPrompt,
  onSave,
  onCancel,
}: CustomPromptDialogProps) {
  // Local draft state. Seeded from `initialPrompt` each time the dialog
  // opens (not on every prop change) so the user's in-progress edits
  // are preserved across re-renders while open.
  const [draft, setDraft] = useState(initialPrompt);

  useEffect(() => {
    if (open) setDraft(initialPrompt);
  }, [open, initialPrompt]);

  // Esc dismisses. Enter is intentionally NOT bound — a textarea owns
  // Enter for newlines; the Save pill is the explicit commit path.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        onClick={onCancel}
      />

      {/* Glass card — 520px wide per LOCKED Decision 5-B. max-h-[480px]
          caps card height so the resize-y textarea grows up to 280px
          without escaping the 700x500 Tauri main window. */}
      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[520px] max-h-[480px] rounded-[var(--radius-card)] p-7"
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="custom-prompt-heading"
      >
        <h2
          id="custom-prompt-heading"
          className="font-display text-[18px] font-normal text-slate mb-1"
        >
          Custom prompt
        </h2>

        <p className="text-[12px] text-steel mb-4 leading-relaxed">
          Used to rewrite every transcription longer than three words.
        </p>

        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="e.g., Clean up this transcription. Fix grammar. Keep the original meaning."
          className="w-full min-h-[140px] max-h-[280px] resize-y rounded-2xl px-4 py-3 text-[13px] text-slate font-body placeholder:text-steel/40 outline-none transition-all duration-300"
          style={{
            background: "var(--surface-control)",
            border: "1px solid var(--border-hairline)",
          }}
          onFocus={(e) => {
            // Mirror Enhancement.tsx inline-textarea recipe (lines
            // 903-910): brighten the border to frost and lift the
            // background one step on focus.
            e.currentTarget.style.borderColor = "var(--border-accent)";
            e.currentTarget.style.background = "var(--surface-hover)";
          }}
          onBlur={(e) => {
            e.currentTarget.style.borderColor = "var(--border-hairline)";
            e.currentTarget.style.background = "var(--surface-control)";
          }}
        />

        {/* Footer: Reset link pinned left, Cancel/Save pair pinned
            right — the "one auxiliary link, one primary pair" pattern
            from DataLeavesDialog. */}
        <div className="flex items-center justify-between gap-2.5 mt-5">
          <button
            type="button"
            onClick={async () => {
              try {
                const { prompt } = await getBuiltinPrompt("chat");
                setDraft(prompt);
              } catch (err) {
                console.warn(
                  `[CustomPromptDialog] builtin prompt fetch failed error_type=${errorType(err)}`,
                );
              }
            }}
            className="text-[11px] text-steel/70 hover:text-frost-deep transition-colors cursor-pointer"
          >
            Reset to default
          </button>
          <div className="flex items-center gap-2.5">
            <button
              type="button"
              onClick={onCancel}
              className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
              style={glassPillStyle}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => onSave(draft)}
              className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
              style={gradientPillStyle}
            >
              Save
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  );
}
