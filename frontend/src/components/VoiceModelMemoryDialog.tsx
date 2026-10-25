import { useEffect, useRef } from "react";
import { motion } from "framer-motion";
import type { SpeechStatus } from "../lib/api/speech";
import { MemoryRingIcon } from "./LowRamDialog";

interface VoiceModelMemoryDialogProps {
  open: boolean;
  tier: Extract<
    SpeechStatus["kokoro_memory_tier"],
    "caution" | "blocked" | "unknown"
  >;
  memory: SpeechStatus["memory"];
  onContinue: () => void;
  onUseLowerMemory: () => void;
  onDismiss: () => void;
}

function formatGigabytes(megabytes: number): string {
  return (megabytes / 1024).toLocaleString(undefined, {
    maximumFractionDigits: 1,
  });
}

export function VoiceModelMemoryDialog({
  open,
  tier,
  memory,
  onContinue,
  onUseLowerMemory,
  onDismiss,
}: VoiceModelMemoryDialogProps) {
  const secondaryButtonRef = useRef<HTMLButtonElement>(null);
  const blocked = tier === "blocked";

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onDismiss();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onDismiss, open]);

  useEffect(() => {
    if (open) secondaryButtonRef.current?.focus();
  }, [open]);

  if (!open) return null;

  const hasMemory =
    memory.total_mb !== null && memory.available_mb !== null;
  const memorySentence = hasMemory
    ? `This PC has ${formatGigabytes(memory.total_mb!)} GB of RAM, with ${formatGigabytes(memory.available_mb!)} GB currently available.`
    : "Whisperstream couldn't check this PC's available memory.";
  const blockedGuidance =
    memory.total_mb !== null && memory.total_mb < 8 * 1024
      ? "There is not enough total memory for Kokoro to run reliably. Use the lower-memory model instead."
      : "Kokoro does not have enough memory available right now. Close other apps and try again, or use the lower-memory model.";
  const body = blocked
    ? `${memorySentence} ${blockedGuidance}`
    : `${memorySentence} Kokoro may slow other apps or make speech stutter.`;

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={{
          background: "var(--backdrop-strong)",
          backdropFilter: "blur(40px)",
          WebkitBackdropFilter: "blur(40px)",
        }}
        onClick={onDismiss}
      />

      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[420px] rounded-[var(--radius-card)] p-7"
        style={{
          background: "var(--surface-modal)",
          backdropFilter: "blur(24px)",
          WebkitBackdropFilter: "blur(24px)",
          border: "1px solid var(--border-emphasis)",
          boxShadow: "var(--shadow-glass)",
        }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="voice-model-memory-heading"
      >
        <MemoryRingIcon />

        <h2
          id="voice-model-memory-heading"
          className="font-display text-[18px] font-normal text-slate text-center mb-2"
        >
          {blocked
            ? "Not enough memory for Kokoro"
            : "Kokoro may use too much memory"}
        </h2>

        <p className="text-[13px] text-steel mb-6 leading-relaxed text-center">
          {body}
        </p>

        <div className="flex items-center justify-center gap-2.5">
          <button
            ref={secondaryButtonRef}
            onClick={blocked ? onDismiss : onContinue}
            className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
            style={{
              background: "var(--surface-modal)",
              border: "1px solid var(--border-emphasis)",
              boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
            }}
          >
            {blocked ? "Cancel" : "Continue anyway"}
          </button>
          <button
            onClick={onUseLowerMemory}
            className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
            style={{
              background: "var(--accent-fill)",
              boxShadow: "var(--shadow-pearl)",
            }}
          >
            Use lower memory
          </button>
        </div>
      </motion.div>
    </div>
  );
}
