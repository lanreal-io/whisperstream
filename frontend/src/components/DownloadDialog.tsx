import { useEffect, useState, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { DownloadCard } from "./onboarding/DownloadCard";
import { startDownload, cancelDownload } from "../lib/api/models";
import { errorType, flushLogSync } from "../lib/logCapture";
import type { AppEvent } from "../lib/socket/events";

type Phase = "downloading" | "error" | "cancelling";

interface DownloadDialogProps {
  title: string;
  subtitle: string;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  onComplete: () => void;
  onCancel: () => Promise<void>;
  /**
   * Override the function called to kick off (or retry) the download.
   * Defaults to the Parakeet ``startDownload`` helper.
   * Pass ``startLocalLLMDownload`` from Enhancement.tsx for local GGUF downloads.
   */
  startFn?: () => Promise<void>;
  /**
   * Override the function called to cancel an in-flight download.
   * Defaults to the Parakeet ``cancelDownload`` helper.
   * Pass ``cancelLocalLLMDownload`` from Enhancement.tsx for local GGUF downloads.
   */
  cancelFn?: () => Promise<unknown>;
}

export function DownloadDialog({
  title,
  subtitle,
  subscribe,
  onComplete,
  onCancel,
  startFn = startDownload,
  cancelFn = cancelDownload,
}: DownloadDialogProps) {
  const [phase, setPhase] = useState<Phase>("downloading");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [needsSpeedReview, setNeedsSpeedReview] = useState(false);
  const startTimeRef = useRef<number | null>(null);

  // Subscribe to WS lifecycle events.
  useEffect(() => {
    return subscribe((event) => {
      if (event.type === "download.complete") {
        const durationS =
          startTimeRef.current !== null
            ? ((performance.now() - startTimeRef.current) / 1000).toFixed(1)
            : "unknown";
        console.log(`[Download] complete duration_s=${durationS}`);
        flushLogSync().catch(() => undefined).then(() => onComplete());
      } else if (event.type === "download.error") {
        console.error("[Download] failed error_type=RemoteError");
        flushLogSync().catch(() => undefined);
        setNeedsSpeedReview(event.reason === "model_benchmark_required");
        setErrorMsg(event.message);
        setPhase("error");
      } else if (event.type === "download.cancelled") {
        console.log("[Download] cancelled");
        flushLogSync().catch(() => undefined).then(() => onCancel());
      }
    });
  }, [subscribe, onComplete, onCancel]);

  // Kick the download on mount.
  useEffect(() => {
    startTimeRef.current = performance.now();
    console.log(`[Download] start title=${title}`);
    startFn().catch((e) => {
      const msg = String(e);
      if (msg.includes("409")) {
        console.log("[Download] start already in progress, joining existing download");
        return;
      }
      setErrorMsg(msg);
      setPhase("error");
    });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleCancel = async () => {
    if (phase === "cancelling") return;
    if (phase === "error") {
      // Nothing to abort backend-side; just unmount.
      await onCancel();
      return;
    }
    setPhase("cancelling");
    console.log("[Download] cancel requested");
    try {
      await cancelFn();
      // The download.cancelled WS event will land and call onCancel().
      // If the backend completes before the cancel request lands, the WS
      // download.complete handler will win and we'll unmount via onComplete.
    } catch (err) {
      console.error(`[Download] cancel error error_type=${errorType(err)}`);
      // Fall back: unmount anyway. Better to lose the cancel UX than lock the user in.
      await onCancel();
    }
  };

  const handleRetry = () => {
    setErrorMsg(null);
    setNeedsSpeedReview(false);
    setPhase("downloading");
    startTimeRef.current = performance.now();
    startFn().catch((e) => {
      const msg = String(e);
      if (msg.includes("409")) {
        console.log("[Download] retry already in progress, joining existing download");
        return;
      }
      setErrorMsg(msg);
      setPhase("error");
    });
  };

  // Esc handler.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && phase !== "cancelling") {
        e.preventDefault();
        handleCancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <AnimatePresence>
      <div className="fixed inset-0 z-50 flex items-center justify-center">
        {/* Backdrop — family C: --backdrop-soft (8px blur) */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
          className="absolute inset-0"
          style={{
            background: "var(--backdrop-soft)",
            backdropFilter: "blur(8px)",
          }}
          onClick={phase === "error" ? handleCancel : undefined}
        />

        {/* Glass card */}
        <motion.div
          initial={{ scale: 0.92, opacity: 0, y: 12 }}
          animate={{ scale: 1, opacity: 1, y: 0 }}
          exit={{ scale: 0.96, opacity: 0, y: 6 }}
          transition={{ type: "spring", stiffness: 400, damping: 30 }}
          className="relative w-[420px] rounded-[var(--radius-card)] p-7"
          style={{
            background: "var(--surface-modal)",
            backdropFilter: "blur(24px)",
            border: "1px solid var(--border-emphasis)",
            boxShadow: "var(--shadow-glass)",
          }}
        >
          <AnimatePresence mode="wait">
            {(phase === "downloading" || phase === "cancelling") && (
              <motion.div
                key="downloading"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
              >
                <DownloadCard
                  subscribe={subscribe}
                  hideFooter
                  compact
                  title={title}
                  subtitle={subtitle}
                />
                <div className="mt-5 flex items-center justify-end">
                  <button
                    onClick={handleCancel}
                    disabled={phase === "cancelling"}
                    className="px-4 py-1.5 rounded-full text-[13px] font-medium text-steel hover:text-slate hover:bg-white/40 cursor-pointer transition-all duration-200 disabled:cursor-default disabled:opacity-60"
                  >
                    {phase === "cancelling" ? "Cancelling..." : "Cancel"}
                  </button>
                </div>
              </motion.div>
            )}

            {phase === "error" && (
              <motion.div
                key="error"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
              >
                <h2 className="font-display text-[18px] font-normal text-slate mb-2">
                  {needsSpeedReview ? "Review the speed check" : "Download failed"}
                </h2>
                <p className="text-[13px] text-rose leading-relaxed mb-6">
                  {errorMsg ?? "Couldn't finish the download. Check your connection and try again."}
                </p>
                <div className="flex items-center justify-end gap-2.5">
                  <button
                    onClick={handleCancel}
                    className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                    style={{
                      background: "var(--surface-modal)",
                      border: "1px solid rgba(255, 255, 255, 0.85)",
                      boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
                    }}
                  >
                    {needsSpeedReview ? "Done" : "Cancel"}
                  </button>
                  {!needsSpeedReview && <button
                    onClick={handleRetry}
                    className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
                    style={{
                      background: "var(--accent-fill)",
                      boxShadow:
                        "var(--shadow-pearl)",
                    }}
                  >
                    Retry
                  </button>}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </motion.div>
      </div>
    </AnimatePresence>
  );
}
