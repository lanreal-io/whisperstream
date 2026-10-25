import { useCallback, useEffect, useId, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { DownloadCard } from "./onboarding/DownloadCard";
import type { AppEvent } from "../lib/socket/events";
import type { DownloadEvent } from "../lib/socket/downloads";
import type { SpeechEngine } from "../lib/api/speech";
import type { SttBackend } from "../lib/languages";

// Confirm-then-download glass dialog used when the user selects a language (or
// model) whose resolved STT backend's files are not yet on disk. Names the
// model + approximate size, then runs the existing download progress UI on
// confirm. Covers the Parakeet<->Qwen backend switch, the Parakeet v2<->v3
// family switch, and any first download of either backend. Copy is locked.
//
// Hand-rolled overlay + card (the dialog family deliberately shares no base
// component). Mounted at the App root so its
// position: fixed resolves against the viewport, not the <main> backdrop-blur
// containing block.
//
//   1. confirm     -> heading, body copy, Cancel + Download buttons.
//   2. downloading -> cross-fades to an embedded DownloadCard with cancel.
//      Once transfer reaches 100%, accurate preparation copy replaces the
//      download copy while backend verification and installation finish.
//   3. error       -> heading, error message, Cancel + Retry buttons.

type Phase = "confirm" | "downloading" | "error";

/** A failure the dialog has to explain. `start` is the POST that begins the
 *  operation being refused; `lifecycle` is a terminal backend failure. */
export interface DownloadDialogFailure {
  source: "start" | "lifecycle";
  /** Raw backend text. Rendered only when the host supplies no mapping. */
  message?: string;
  reason?: string;
  restored?: boolean;
}

export interface DownloadDialogFailureCopy {
  title: string;
  message: string;
}

interface Props {
  /** The resolved STT backend whose model the dialog is about to download. */
  targetBackend?: SttBackend;
  /** Registry-backed model details for downloads outside the STT model picker. */
  targetModel?: {
    name: string;
    downloadBytes: number;
    installedBytes: number;
    /** Several models confirmed together ("Both voice models"). */
    plural?: boolean;
  };
  /** Lifecycle family. Omit only for STT: the untagged STT stream is then
   *  selected explicitly, while tagged local-LLM, TTS, and relocation events
   *  are rejected. Named flows must provide their exact backend `kind`. */
  eventKind?: string;
  targetEngine?: SpeechEngine;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  onConfirm: () => Promise<void>;
  onComplete: () => void;
  onCancel: () => void;
  onCancelDownload: () => Promise<void>;
  /** Turn a failure into user-facing recovery copy.
   *
   *  Without it the dialog renders the backend's own message, which is the
   *  pre-existing behavior for the local-LLM and voice-model download flows.
   *  The STT flow supplies one, because those raw strings are HTTP status lines
   *  and JSON payloads (`409: {"reason":"dictation_active"}`), which name no
   *  recovery step and leak transport detail into the UI. */
  mapFailure?: (failure: DownloadDialogFailure) => DownloadDialogFailureCopy;
}

function formatMegabytes(bytes: number): string {
  return Math.max(1, Math.round(bytes / 1_000_000)).toLocaleString();
}

export function ModelDownloadConfirmDialog({
  targetBackend,
  targetModel,
  eventKind,
  targetEngine,
  subscribe,
  onConfirm,
  onComplete,
  onCancel,
  onCancelDownload,
  mapFailure,
}: Props) {
  const [phase, setPhase] = useState<Phase>("confirm");
  const [errorCopy, setErrorCopy] =
    useState<DownloadDialogFailureCopy | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const titleId = useId();
  const onCompleteRef = useRef(onComplete);
  const onCancelRef = useRef(onCancel);
  const completionTimerRef = useRef<number | null>(null);

  useEffect(() => {
    onCompleteRef.current = onComplete;
    onCancelRef.current = onCancel;
  }, [onCancel, onComplete]);

  // One place turns a failure into what the error phase says, so a mapped flow
  // can never fall back to a raw payload through the other entry point.
  const mapFailureRef = useRef(mapFailure);
  useEffect(() => {
    mapFailureRef.current = mapFailure;
  }, [mapFailure]);
  const resolveFailure = useCallback(
    (failure: DownloadDialogFailure): DownloadDialogFailureCopy =>
      mapFailureRef.current?.(failure) ?? {
        title: "Download failed",
        message:
          failure.message ??
          "Couldn't finish the download. Check your connection and try again.",
      },
    [],
  );

  const filteredSubscribe = useCallback(
    (handler: (event: AppEvent) => void) =>
      subscribe((event) => {
        if (
          event.type !== "download.progress" &&
          event.type !== "download.complete" &&
          event.type !== "download.error" &&
          event.type !== "download.cancelled"
        ) {
          return;
        }
        const downloadEvent = event as DownloadEvent;
        if (eventKind === undefined) {
          // STT is the sole untagged download family. Undefined means
          // "untagged only", never "accept every family".
          if (downloadEvent.kind !== undefined) return;
        } else if (downloadEvent.kind !== eventKind) {
          return;
        }
        if (targetEngine !== undefined && downloadEvent.engine !== targetEngine) {
          return;
        }
        handler(event);
      }),
    [eventKind, subscribe, targetEngine],
  );

  // Subscribe once; route WS events to phase state. On download.complete we
  // schedule a 300ms settle so the user sees the bar finish before the dialog
  // flies away. We do NOT change phase here — the host's onComplete callback
  // is about to unmount the whole dialog.
  useEffect(() => {
    const unsubscribe = filteredSubscribe((event) => {
      if (event.type === "download.complete") {
        if (completionTimerRef.current !== null) {
          window.clearTimeout(completionTimerRef.current);
        }
        completionTimerRef.current = window.setTimeout(
          () => onCompleteRef.current(),
          300,
        );
      } else if (
        event.type === "download.progress" &&
        event.total > 0 &&
        event.downloaded >= event.total
      ) {
        setFinalizing(true);
      } else if (event.type === "download.error") {
        setFinalizing(false);
        setErrorCopy(
          resolveFailure({
            source: "lifecycle",
            message: event.message,
            reason: event.reason,
            restored: event.restored,
          }),
        );
        setPhase("error");
      } else if (event.type === "download.cancelled") {
        onCancelRef.current();
      }
    });
    return () => {
      unsubscribe();
      if (completionTimerRef.current !== null) {
        window.clearTimeout(completionTimerRef.current);
        completionTimerRef.current = null;
      }
    };
  }, [filteredSubscribe, resolveFailure]);

  const handleConfirm = async () => {
    setFinalizing(false);
    setCancelling(false);
    setPhase("downloading");
    try {
      await onConfirm();
    } catch (err) {
      setErrorCopy(
        resolveFailure({
          source: "start",
          message: err instanceof Error ? err.message : String(err),
        }),
      );
      setPhase("error");
    }
  };

  // Enter = primary (confirm phase only); Esc = cancel (confirm + error
  // phases) or onCancelDownload (downloading phase). Esc is suppressed while a
  // cancel is already in-flight.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // A picker can mount this dialog while its selection key is still bubbling.
      if (e.defaultPrevented) return;
      if (phase === "confirm") {
        if (e.key === "Enter") {
          e.preventDefault();
          handleConfirm();
        }
        if (e.key === "Escape") {
          e.preventDefault();
          onCancel();
        }
      } else if (phase === "error") {
        if (e.key === "Escape") {
          e.preventDefault();
          onCancel();
        }
      } else if (phase === "downloading") {
        if (e.key === "Escape") {
          e.preventDefault();
          if (cancelling || finalizing) return;
          setCancelling(true);
          onCancelDownload().catch(() => {
            setCancelling(false);
          });
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // Locked copy (copy-review.md → Confirm-then-download dialog).
  const modelName =
    targetModel?.name ??
    (targetBackend === "cohere"
      ? "Cohere Transcribe"
      : targetBackend === "qwen"
        ? "Qwen3 ASR"
        : "NVIDIA Parakeet");
  const targetSizeMb = targetModel
    ? formatMegabytes(
        Math.max(targetModel.downloadBytes, targetModel.installedBytes),
      )
    : null;
  const plural = targetModel?.plural === true;
  // Mid-sentence, a plural name reads "Downloading both voice models".
  const inlineName = plural
    ? modelName.charAt(0).toLowerCase() + modelName.slice(1)
    : modelName;
  const bodyCopy = targetModel
    ? plural
      ? `${modelName} are about ${targetSizeMb} MB. They download once, then run on your CPU. Continue?`
      : `${modelName} is about ${targetSizeMb} MB. It downloads once, then runs on your CPU. Continue?`
    : targetBackend === "cohere"
      ? "Cohere Transcribe is about 2.9 GB. It downloads once, then runs on your CPU. Continue?"
      : targetBackend === "qwen"
        ? "Qwen3 ASR is about 937 MB. It downloads once, then runs on your CPU. Continue?"
        : "NVIDIA Parakeet is about 600 MB. It downloads once, then runs on your CPU. Continue?";
  const downloadTitle = `Downloading ${inlineName}`;
  const downloadSubtitle = targetModel
    ? plural
      ? `About ${targetSizeMb} MB. They download once, then run on your CPU.`
      : `About ${targetSizeMb} MB. It downloads once, then runs on your CPU.`
    : "It downloads once, then runs on your CPU.";
  const progressTitle = finalizing ? `Preparing ${inlineName}` : downloadTitle;
  const progressSubtitle = finalizing
    ? "Finishing setup. This may take a moment."
    : downloadSubtitle;

  return (
    <AnimatePresence>
      <div className="fixed inset-0 z-[60] flex items-center justify-center">
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
          onClick={phase === "confirm" ? onCancel : undefined}
        />

        {/* Glass card */}
        <motion.div
          initial={{ scale: 0.92, opacity: 0, y: 12 }}
          animate={{ scale: 1, opacity: 1, y: 0 }}
          exit={{ scale: 0.96, opacity: 0, y: 6 }}
          transition={{ type: "spring", stiffness: 400, damping: 30 }}
          className="relative w-[400px] rounded-[var(--radius-card)] p-7"
          style={{
            background: "var(--surface-modal)",
            backdropFilter: "blur(24px)",
            border: "1px solid var(--border-emphasis)",
            boxShadow: "var(--shadow-glass)",
          }}
          role="dialog"
          aria-modal="true"
          aria-labelledby={phase === "downloading" ? undefined : titleId}
          aria-label={phase === "downloading" ? progressTitle : undefined}
        >
          <AnimatePresence mode="wait">
            {phase === "confirm" && (
              <motion.div
                key="confirm"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
              >
                <h2
                  id={titleId}
                  className="font-display text-[18px] font-normal text-slate mb-2"
                >
                  {plural ? "Download these models?" : "Download this model?"}
                </h2>
                <p className="text-[13px] text-steel leading-relaxed mb-6">
                  {bodyCopy}
                </p>
                <div className="flex items-center justify-end gap-2.5">
                  <button
                    onClick={onCancel}
                    className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                    style={{
                      background: "var(--surface-modal)",
                      border: "1px solid rgba(255, 255, 255, 0.85)",
                      boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
                    }}
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleConfirm}
                    className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
                    style={{
                      background: "var(--accent-fill)",
                      boxShadow:
                        "var(--shadow-pearl)",
                    }}
                  >
                    Download
                  </button>
                </div>
              </motion.div>
            )}

            {phase === "downloading" && (
              <motion.div
                key="downloading"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
              >
                <DownloadCard
                  subscribe={filteredSubscribe}
                  hideFooter
                  compact
                  title={progressTitle}
                  subtitle={progressSubtitle}
                  finalizing={finalizing}
                />
                <div className="mt-5 flex items-center justify-end">
                  <button
                    onClick={async () => {
                      if (cancelling || finalizing) return;
                      setCancelling(true);
                      try {
                        await onCancelDownload();
                      } catch {
                        setCancelling(false);
                      }
                    }}
                    disabled={cancelling || finalizing}
                    className="px-4 py-1.5 rounded-full text-[13px] font-medium text-steel hover:text-slate hover:bg-white/40 cursor-pointer transition-all duration-200 disabled:cursor-default disabled:opacity-60"
                  >
                    {finalizing
                      ? "Finishing up..."
                      : cancelling
                        ? "Cancelling..."
                        : "Cancel"}
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
                <h2
                  id={titleId}
                  className="font-display text-[18px] font-normal text-slate mb-2"
                >
                  {errorCopy?.title ?? "Download failed"}
                </h2>
                <p className="text-[13px] text-rose leading-relaxed mb-6">
                  {errorCopy?.message ??
                    "Couldn't finish the download. Check your connection and try again."}
                </p>
                <div className="flex items-center justify-end gap-2.5">
                  <button
                    onClick={onCancel}
                    className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                    style={{
                      background: "var(--surface-modal)",
                      border: "1px solid rgba(255, 255, 255, 0.85)",
                      boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
                    }}
                  >
                    Cancel
                  </button>
                  <button
                    onClick={() => {
                      setErrorCopy(null);
                      handleConfirm();
                    }}
                    className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
                    style={{
                      background: "var(--accent-fill)",
                      boxShadow:
                        "var(--shadow-pearl)",
                    }}
                  >
                    Retry
                  </button>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </motion.div>
      </div>
    </AnimatePresence>
  );
}
