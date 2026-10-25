import { useState, type CSSProperties } from "react";
import { motion } from "framer-motion";
import { exportLogs } from "../lib/api/diagnostics";

const spring = { type: "spring" as const, stiffness: 400, damping: 30 };

const glassCard: CSSProperties = {
  background: "var(--surface-raised)",
  border: "1px solid var(--border-hairline)",
  borderTop: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass-sm)",
};

type DownloadState = "idle" | "preparing" | "saved" | "denied" | "error";

function BackArrowIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M10 3L5 8l5 5" />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 4v12" />
      <path d="M7.5 11.5L12 16l4.5-4.5" />
      <path d="M4 18.5v1a1.5 1.5 0 001.5 1.5h13a1.5 1.5 0 001.5-1.5v-1" />
    </svg>
  );
}

interface LogsDownloadStepProps {
  onDownloaded: () => void;
  onSkip: () => void;
  onBack: () => void;
}

export function LogsDownloadStep({
  onDownloaded,
  onSkip,
  onBack,
}: LogsDownloadStepProps) {
  const [state, setState] = useState<DownloadState>("idle");

  const handleDownload = async () => {
    if (state === "preparing") return;
    setState("preparing");
    try {
      const result = await exportLogs();
      if (result === "cancelled") {
        setState("idle");
        return;
      }
      if (result === "denied") {
        setState("denied");
        return;
      }
      setState("saved");
      setTimeout(() => onDownloaded(), 800);
    } catch {
      setState("error");
      setTimeout(() => setState("idle"), 3000);
    }
  };

  const buttonLabel =
    state === "idle"
      ? "Export logs"
      : state === "preparing"
        ? "Preparing..."
        : state === "saved"
          ? "Exported"
          : state === "denied"
            ? "Unavailable in Lockdown"
            : "Couldn't export";

  const buttonColor =
    state === "saved"
      ? "var(--color-mint)"
      : state === "error" || state === "denied"
        ? "var(--color-rose)"
        : undefined;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ ...spring, delay: 0.03 }}
      className="rounded-3xl p-7"
      style={glassCard}
    >
      {/* Back link */}
      <button
        type="button"
        onClick={onBack}
        className="flex items-center gap-1.5 text-[12px] text-steel hover:text-slate transition-colors cursor-pointer mb-6"
      >
        <BackArrowIcon className="text-steel/70" />
        Back
      </button>

      {/* Centered content */}
      <div className="flex flex-col items-center text-center">
        {/* Icon */}
        <div
          className="w-12 h-12 rounded-full flex items-center justify-center mb-4"
          style={{
            background: "color-mix(in srgb, var(--color-frost) 12%, transparent)",
            border: "1px solid color-mix(in srgb, var(--color-frost) 25%, transparent)",
            color: "var(--color-frost-deep)",
          }}
        >
          <DownloadIcon />
        </div>

        {/* Heading */}
        <h3 className="text-[14px] font-medium text-slate mb-1.5">
          Include your logs
        </h3>

        {/* Description */}
        <p className="text-[12px] text-steel mb-6 max-w-[280px]">
          Export a local bundle to attach to your email. In Lockdown, it contains
          technical status only, never transcript or audio content. Whisperstream
          does not upload it. Other apps may sync the folder you choose.
        </p>

        {/* Download button */}
        <button
          type="button"
          onClick={handleDownload}
          disabled={state === "preparing"}
          className="h-11 px-7 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200 disabled:cursor-not-allowed mb-4"
          style={{
            background: buttonColor
              ? undefined
              : "linear-gradient(135deg, var(--color-frost), var(--color-frost-deep))",
            backgroundColor: buttonColor || undefined,
            boxShadow: buttonColor
              ? "none"
              : "var(--shadow-pearl)",
          }}
          onMouseEnter={(e) => {
            if (state === "idle") {
              e.currentTarget.style.transform = "translateY(-1px)";
              e.currentTarget.style.boxShadow =
                "var(--shadow-frost-glow), inset 0 1px 0 rgb(255 255 255 / calc(0.6 * var(--rim-scale)))";
            }
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.transform = "translateY(0)";
            if (state === "idle") {
              e.currentTarget.style.boxShadow = "var(--shadow-pearl)";
            }
          }}
        >
          {buttonLabel}
        </button>

        {/* Skip link */}
        <button
          type="button"
          onClick={onSkip}
          className="text-[11px] text-steel hover:text-slate transition-colors cursor-pointer"
        >
          Send without logs
        </button>
      </div>
    </motion.div>
  );
}

export default LogsDownloadStep;
