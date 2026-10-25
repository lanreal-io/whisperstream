import { useEffect, useRef, type CSSProperties } from "react";
import { motion } from "framer-motion";

const spring = { type: "spring" as const, stiffness: 400, damping: 30 };

const glassCard: CSSProperties = {
  background: "var(--surface-raised)",
  border: "1px solid var(--border-hairline)",
  borderTop: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass-sm)",
};

interface LogsAttachStepProps {
  logsDownloaded: boolean;
  onOpenEmail: () => void;
  onBack: () => void;
}

function CheckIcon({
  className,
  style,
}: {
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <svg
      className={className}
      style={style}
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 8l3.5 3.5L13 5" />
    </svg>
  );
}

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

function MailIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
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
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="M3.5 7l8.5 5.5L20.5 7" />
    </svg>
  );
}

export function LogsAttachStep({
  logsDownloaded,
  onOpenEmail,
  onBack,
}: LogsAttachStepProps) {
  const primaryRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    primaryRef.current?.focus();
  }, []);

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
        {logsDownloaded ? (
          <>
            {/* Success check icon */}
            <div
              className="w-12 h-12 rounded-full flex items-center justify-center mb-4"
              style={{
                background: "rgba(91, 175, 124, 0.12)",
                border: "1px solid rgba(91, 175, 124, 0.3)",
              }}
            >
              <CheckIcon className="w-5 h-5" style={{ color: "#5BAF7C" }} />
            </div>

            <h3 className="text-[14px] font-medium text-slate mb-4">
              Ready to send
            </h3>

            <div
              className="rounded-2xl px-4 py-3.5 w-full mb-5"
              style={{
                background: "rgba(255, 255, 255, 0.25)",
                border: "1px solid rgba(255, 255, 255, 0.4)",
              }}
            >
              <p className="text-[13px] text-slate">
                Attach the <span className="font-medium">log file</span> you exported to the email before sending.
              </p>
            </div>
          </>
        ) : (
          <>
            {/* Mail icon for no-logs path */}
            <div
              className="w-12 h-12 rounded-full flex items-center justify-center mb-4"
              style={{
                background: "color-mix(in srgb, var(--color-frost) 12%, transparent)",
                border: "1px solid color-mix(in srgb, var(--color-frost) 25%, transparent)",
                color: "var(--color-frost-deep)",
              }}
            >
              <MailIcon />
            </div>

            <h3 className="text-[14px] font-medium text-slate mb-1.5">
              Ready to send
            </h3>

            <p className="text-[12px] text-steel mb-5 max-w-[280px]">
              Your message is ready to go.
            </p>
          </>
        )}

        {/* Open email button */}
        <button
          ref={primaryRef}
          type="button"
          onClick={onOpenEmail}
          className="h-11 px-7 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
          style={{
            background:
              "linear-gradient(135deg, var(--color-frost), var(--color-frost-deep))",
            boxShadow: "var(--shadow-pearl)",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.transform = "translateY(-1px)";
            e.currentTarget.style.boxShadow =
              "var(--shadow-frost-glow), inset 0 1px 0 rgb(255 255 255 / calc(0.6 * var(--rim-scale)))";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.transform = "translateY(0)";
            e.currentTarget.style.boxShadow = "var(--shadow-pearl)";
          }}
        >
          Open email
        </button>
      </div>
    </motion.div>
  );
}

export default LogsAttachStep;
