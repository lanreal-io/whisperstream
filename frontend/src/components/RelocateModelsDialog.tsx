import { useEffect, useState, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { HoverTooltip } from "./HoverTooltip";
import { checkRelocate, startRelocate, cancelDownload } from "../lib/api/models";
import { subscribeToDownloadEvents } from "../lib/socket/downloads";
import type { AppEvent } from "../lib/socket/events";
import { errorType } from "../lib/logCapture";

// ── Dialog shell tokens ──────────────────────────────────────────────────────
// Copied verbatim from design.md §"Shared dialog tokens" per the locked
// "dialog family has no shared base" convention. These constants are the
// canonical frost-glass treatment for RelocateModelsDialog; do not move
// them to a shared file.
// Uses --backdrop-strong (40px blur) to match its existing blur value.
const cardStyle = {
  background: "var(--surface-modal)",
  backdropFilter: "blur(24px)",
  WebkitBackdropFilter: "blur(24px)",
  border: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass)",
} as const;

const backdropStyle = {
  background: "var(--backdrop-strong)",
  backdropFilter: "blur(40px)",
  WebkitBackdropFilter: "blur(40px)",
} as const;

const gradientPillStyle = {
  background: "var(--accent-fill)",
  boxShadow: "var(--shadow-pearl)",
} as const;

const glassPillStyle = {
  background: "var(--surface-modal)",
  border: "1px solid var(--border-hairline-steel)",
  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
} as const;

// Gold caution surface — used for the reuse-confirm note (design.md decision
// #8: gold for genuine "are you sure", not for alarming the user).
const cautionCardStyle = {
  background: "var(--surface-caution)",
  border: "1px solid rgba(214, 178, 94, 0.45)",
  boxShadow:
    "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale))), 0 8px 20px -8px rgba(214,178,94,0.3)",
} as const;

// Control surface background for the From/To summary block.
const controlSurfaceStyle = {
  background: "var(--surface-raised)",
  border: "1px solid var(--border-hairline)",
} as const;

// ── Phase crossfade transition ───────────────────────────────────────────────
// Mirrors DownloadDialog's per-phase fade exactly (AnimatePresence mode="wait").
const PHASE_FADE = {
  duration: 0.22,
  ease: [0.2, 0, 0, 1] as [number, number, number, number],
};

// ── Helpers ──────────────────────────────────────────────────────────────────
/** Format a byte count as a 1-decimal-place GB string (tabular-nums ready). */
const fmtGb = (bytes: number) => `${(bytes / 1024 ** 3).toFixed(1)} GB`;

// ── Types ────────────────────────────────────────────────────────────────────
type Phase =
  | "checking"
  | "confirm"
  | "reuse"
  | "blocked"
  | "moving"
  | "success"
  | "error";

export interface RelocateModelsDialogProps {
  /**
   * Absolute path the user just picked as the new model storage location.
   * Passed to the probe and, on confirm, to the backend worker.
   */
  destination: string;
  /**
   * Current model storage path shown in the "From" field of the confirm
   * summary block. Typically `config.model_storage_dir || defaultModelsRoot`.
   */
  currentPath: string;
  /** WebSocket subscribe handle from `useAppSocket()`. */
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  /**
   * Called when the dialog should close — on dismiss in any safe state
   * (confirm / reuse / blocked / success / error) and on `download.cancelled`.
   * Parent unmounts the dialog on call.
   */
  onClose: () => void;
  /**
   * Called when the blocked state's "Choose another folder" button is pressed.
   * Parent should re-open the OS folder picker and, on a new pick, remount
   * this dialog with the updated `destination` prop.
   */
  onChooseAnother: () => void;
  /**
   * Called once the relocation has succeeded and the backend has persisted the
   * new model_storage_dir (on download.complete, including the no-copy reuse
   * path). The parent MUST re-fetch /api/config so its in-memory config
   * reflects the new path; otherwise the next config save PUTs a stale
   * model_storage_dir and silently reverts the move.
   */
  onRelocated?: () => void;
}

// ── Component ────────────────────────────────────────────────────────────────
export function RelocateModelsDialog({
  destination,
  currentPath,
  subscribe,
  onClose,
  onChooseAnother,
  onRelocated,
}: RelocateModelsDialogProps) {
  const [phase, setPhase] = useState<Phase>("checking");
  const [blockedReason, setBlockedReason] = useState<
    "no_room" | "unwritable" | "foreign" | null
  >(null);

  // Data from the check probe (ok outcome).
  const [totalBytes, setTotalBytes] = useState(0);
  const [freeBytes, setFreeBytes] = useState(0);

  // Move progress (driven by download.progress WS events).
  const [movedBytes, setMovedBytes] = useState(0);
  const [moveTotalBytes, setMoveTotalBytes] = useState(0);

  // True once the copy phase is done and the worker is in verify/swap/cleanup.
  // At this point Cancel is not safe; the button disables to "Finishing up..."
  // (design.md decision #4, locked copy "Finishing up...").
  const [isFinalizing, setIsFinalizing] = useState(false);

  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Guard against double-cancel calls (e.g. from rapid taps).
  const cancelCalledRef = useRef(false);

  // ── Check probe (extracted so mount + retry can both call it) ─────────────
  const runCheck = () => {
    setPhase("checking");
    checkRelocate(destination)
      .then((result) => {
        if (result.outcome === "ok") {
          setTotalBytes(result.total_bytes ?? 0);
          setFreeBytes(result.free_bytes ?? 0);
          setPhase("confirm");
        } else if (result.outcome === "reuse") {
          setPhase("reuse");
        } else {
          // blocked: reason is one of "no_room" | "unwritable" | "foreign"
          setBlockedReason(
            (result.reason as "no_room" | "unwritable" | "foreign") ?? null,
          );
          setPhase("blocked");
        }
      })
      .catch((err) => {
        // Surface a probe failure as an error state rather than leaving the
        // dialog stuck on "Checking...". Diagnostics retain only the exception
        // class; the dialog shows a friendly sentence.
        console.error(
          `[Relocate] check error error_type=${errorType(err)}`,
        );
        setErrorMsg("Something went wrong while checking the folder.");
        setPhase("error");
      });
  };

  // Run probe on mount.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(runCheck, []);

  // ── WebSocket subscription (relocate kind only) ───────────────────────────
  // `subscribeToDownloadEvents` from ws.ts filters the raw stream to events
  // where `kind === "relocate"`, so Parakeet / local-llm download events are
  // ignored entirely.
  useEffect(() => {
    return subscribeToDownloadEvents(
      subscribe,
      (event) => {
        if (event.type === "download.progress") {
          const { downloaded, total } = event;
          setMovedBytes(downloaded);
          setMoveTotalBytes(total);
          // Once bytes reach 100%, the worker is in the non-cancelable
          // verify/rename/cleanup phase — disable Cancel.
          if (total > 0 && downloaded >= total) {
            setIsFinalizing(true);
          }
        } else if (event.type === "download.complete") {
          // Backend has persisted the new model_storage_dir. Tell the parent to
          // re-fetch config so the UI reflects the new path and a later config
          // save doesn't PUT a stale (empty) model_storage_dir over it.
          onRelocated?.();
          setPhase("success");
        } else if (event.type === "download.error") {
          setErrorMsg(event.message ?? null);
          setPhase("error");
        } else if (event.type === "download.cancelled") {
          // Backend confirmed the cancel and rolled back; dismiss.
          onClose();
        }
      },
      "relocate",
    );
  }, [subscribe, onClose, onRelocated]);

  // ── Esc handler ───────────────────────────────────────────────────────────
  // Blocked during `moving` (design.md decision #4). Dismisses in all other
  // states by calling onClose(), matching DownloadDialog's convention.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (phase === "moving") return;
      e.preventDefault();
      onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [phase, onClose]);

  // ── Actions ───────────────────────────────────────────────────────────────
  /**
   * Called by both "Move files" (confirm) and "Use this folder" (reuse).
   * Transitions to moving, then fires the backend worker. The WS subscription
   * above drives the rest of the state machine.
   */
  const startMove = async () => {
    cancelCalledRef.current = false;
    setMovedBytes(0);
    // Seed moveTotalBytes from the probe so the bar has a denominator before
    // the first WS progress event arrives. For the reuse path this is 0 (no
    // copy), which shows the indeterminate marquee instead.
    setMoveTotalBytes(totalBytes);
    setIsFinalizing(false);
    setPhase("moving");
    try {
      await startRelocate(destination);
    } catch (err) {
      const msg = String(err);
      if (msg.includes("409")) {
        // Another relocate is already in progress — join it.
        return;
      }
      console.error("[Relocate] start error error_type=RemoteError");
      setErrorMsg("Something went wrong while moving the files.");
      setPhase("error");
    }
  };

  /**
   * Called by the Cancel button in the moving phase.
   * No-op once isFinalizing (verify/swap cannot be aborted).
   */
  const handleCancel = async () => {
    if (isFinalizing || cancelCalledRef.current) return;
    cancelCalledRef.current = true;
    try {
      await cancelDownload();
      // The download.cancelled WS event fires once the backend has rolled back;
      // that event handler calls onClose().
    } catch (err) {
      console.error(
        `[Relocate] cancel error error_type=${errorType(err)}`,
      );
      // Fallback: dismiss the dialog anyway. The backend will roll back on its
      // own because it checks the cancel event even without a response.
      onClose();
    }
  };

  /**
   * "Try again" in the error state: re-probe the destination so we handle
   * cases where a reuse-path error (rather than copy-path error) occurred
   * and re-entering "confirm" with stale 0-byte totals would be confusing.
   */
  const handleRetry = () => {
    setErrorMsg(null);
    setIsFinalizing(false);
    setMovedBytes(0);
    setMoveTotalBytes(0);
    cancelCalledRef.current = false;
    runCheck();
  };

  // ── Progress helpers ──────────────────────────────────────────────────────
  // Use moveTotalBytes once WS events arrive; fall back to the probe's
  // totalBytes until the first progress event.
  const effectiveTotal = moveTotalBytes > 0 ? moveTotalBytes : totalBytes;
  const pct =
    effectiveTotal > 0 ? Math.min(100, (movedBytes / effectiveTotal) * 100) : 0;

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop — no-op click during moving (design.md decision #4). */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        onClick={phase === "moving" ? undefined : onClose}
      />

      {/* Glass card */}
      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[420px] rounded-[var(--radius-card)] p-7"
        style={cardStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="relocate-heading"
      >
        <AnimatePresence mode="wait">
          {/* ── checking ────────────────────────────────────────────────── */}
          {phase === "checking" && (
            <motion.div
              key="checking"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={PHASE_FADE}
              className="flex flex-col items-center py-4"
            >
              {/* aria-live so a screen reader announces the async state. */}
              <h2
                id="relocate-heading"
                className="font-display text-[18px] font-normal text-slate text-center"
                aria-live="polite"
              >
                Checking the folder...
              </h2>
            </motion.div>
          )}

          {/* ── confirm ─────────────────────────────────────────────────── */}
          {phase === "confirm" && (
            <motion.div
              key="confirm"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={PHASE_FADE}
            >
              <h2
                id="relocate-heading"
                className="font-display text-[18px] font-normal text-slate text-center mb-4"
              >
                Move your models
              </h2>

              {/* From / To / Size / Free summary block */}
              <div
                className="rounded-2xl p-3.5 mb-4 space-y-3"
                style={controlSurfaceStyle}
              >
                {(
                  [
                    ["From", currentPath],
                    ["To", destination],
                  ] as const
                ).map(([label, value]) => (
                  <div key={label}>
                    <div className="text-[11px] font-medium uppercase tracking-wider text-steel/70">
                      {label}
                    </div>
                    {/* Truncate the path; full path exposed to assistive tech
                        via title + aria-label (not hover-only per a11y note). */}
                    <HoverTooltip content={value} width="w-72" side="bottom-left">
                      <div
                        className="text-[13px] text-slate truncate w-full"
                        title={value}
                        aria-label={`${label}: ${value}`}
                      >
                        {value}
                      </div>
                    </HoverTooltip>
                  </div>
                ))}

                <div className="h-px bg-white/30" />

                <div className="flex items-center justify-between text-[12px]">
                  <span className="text-steel">Size to move</span>
                  <span className="text-slate tabular-nums">{fmtGb(totalBytes)}</span>
                </div>
                <div className="flex items-center justify-between text-[12px]">
                  <span className="text-steel">Free on the new drive</span>
                  <span className="text-slate tabular-nums">{fmtGb(freeBytes)}</span>
                </div>
              </div>

              {/* Quiet pause note — steel caption, not a red/gold banner
                  (design.md decision #8: calm, factual, never alarmist). */}
              <p className="text-[12px] text-steel leading-relaxed mb-5 text-center">
                Dictation and cleanup pause while the files move, then turn back on
                by themselves.
              </p>

              <div className="flex items-center justify-center gap-2.5">
                <button
                  onClick={onClose}
                  className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                  style={glassPillStyle}
                >
                  Cancel
                </button>
                <button
                  onClick={startMove}
                  className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
                  style={gradientPillStyle}
                >
                  Move files
                </button>
              </div>
            </motion.div>
          )}

          {/* ── reuse ───────────────────────────────────────────────────── */}
          {phase === "reuse" && (
            <motion.div
              key="reuse"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={PHASE_FADE}
            >
              <h2
                id="relocate-heading"
                className="font-display text-[18px] font-normal text-slate text-center mb-4"
              >
                Use the models already there
              </h2>

              {/* Gold caution card — "are you sure" framing, not alarm
                  (same gold-tinted idiom as AddAppModal's move banner). */}
              <div className="rounded-2xl p-3.5 mb-5" style={cautionCardStyle}>
                <p className="text-[13px] text-slate leading-relaxed">
                  This folder already has Whisperstream models, so we'll use them
                  instead of copying 3.5 GB again.
                </p>
              </div>

              <div className="flex items-center justify-center gap-2.5">
                <button
                  onClick={onClose}
                  className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                  style={glassPillStyle}
                >
                  Cancel
                </button>
                <button
                  onClick={startMove}
                  className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
                  style={gradientPillStyle}
                >
                  Use this folder
                </button>
              </div>
            </motion.div>
          )}

          {/* ── blocked ─────────────────────────────────────────────────── */}
          {phase === "blocked" && (
            <motion.div
              key="blocked"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={PHASE_FADE}
            >
              <h2
                id="relocate-heading"
                className="font-display text-[18px] font-normal text-slate text-center mb-3"
              >
                Pick a different folder
              </h2>

              {/* Reason body — cause first, fix second. One short pair of
                  sentences per design.md decision #5 ("block" safe default). */}
              <p className="text-[13px] text-steel leading-relaxed mb-6 text-center">
                {blockedReason === "no_room"
                  ? "That drive doesn't have enough free space. Pick a folder with more room."
                  : blockedReason === "unwritable"
                    ? "Whisperstream can't save to that folder. Pick another one."
                    : "That folder already holds other model files. Choose an empty folder instead."}
              </p>

              <div className="flex items-center justify-center gap-2.5">
                <button
                  onClick={onClose}
                  className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                  style={glassPillStyle}
                >
                  Cancel
                </button>
                <button
                  onClick={onChooseAnother}
                  className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
                  style={gradientPillStyle}
                >
                  Choose another folder
                </button>
              </div>
            </motion.div>
          )}

          {/* ── moving ──────────────────────────────────────────────────── */}
          {phase === "moving" && (
            <motion.div
              key="moving"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={PHASE_FADE}
            >
              <h2
                id="relocate-heading"
                className="font-display text-[18px] font-normal text-slate mb-1"
              >
                Moving your models
              </h2>
              {/* aria-live so screen readers hear the mode change. */}
              <p className="text-[12px] text-steel mb-6" aria-live="polite">
                Dictation is off until this finishes.
              </p>

              {effectiveTotal > 0 ? (
                /*
                 * Determinate bar with byte readout — the justified divergence
                 * from DownloadCard (design.md decision #3). A local disk copy
                 * is steady-paced and the user approved "~3.5 GB" in the confirm
                 * step, so a live byte counter is cheap reassurance not anxiety.
                 * Tokens: track rgba(163,198,232,0.15), fill #A3C6E8→#7EAED4,
                 * width animation duration 0.4 easeOut — exact spec from design.md.
                 * Do NOT refactor DownloadCard to expose a bytes mode; this
                 * replication is intentional.
                 */
                <>
                  <div
                    className="w-full h-2 rounded-full overflow-hidden"
                    style={{ background: "var(--frost-mesh-1)" }}
                  >
                    <motion.div
                      className="h-full rounded-full"
                      style={{
                        background: "var(--accent-fill)",
                        boxShadow: "0 1px 4px color-mix(in srgb, var(--color-frost) 40%, transparent)",
                      }}
                      initial={{ width: "0%" }}
                      animate={{ width: `${pct}%` }}
                      transition={{ duration: 0.4, ease: "easeOut" }}
                    />
                  </div>
                  {/* Byte + percent metadata row — tabular-nums for alignment
                      stability as values update. aria-live="polite" so screen
                      readers get periodic progress updates without chatter. */}
                  <div
                    className="mt-3 w-full flex items-center justify-between"
                    aria-live="polite"
                    aria-label={`${fmtGb(movedBytes)} of ${fmtGb(effectiveTotal)} moved, ${pct.toFixed(0)} percent`}
                  >
                    <span className="text-[11px] text-steel/60 tabular-nums">
                      {fmtGb(movedBytes)} of {fmtGb(effectiveTotal)}
                    </span>
                    <span className="text-[13px] font-medium text-slate font-display tabular-nums">
                      {pct.toFixed(0)}%
                    </span>
                  </div>
                </>
              ) : (
                /*
                 * Indeterminate marquee — for the reuse path (no bytes are
                 * copied so no download.progress events fire). Same 1/3-width
                 * frost-gradient sweep as DownloadCard's marquee.
                 */
                <div
                  className="w-full h-2 rounded-full overflow-hidden"
                  style={{ background: "var(--frost-mesh-1)" }}
                >
                  <motion.div
                    className="h-full rounded-full"
                    style={{
                      width: "33%",
                      background: "var(--accent-fill)",
                      boxShadow: "0 1px 4px color-mix(in srgb, var(--color-frost) 40%, transparent)",
                    }}
                    animate={{ x: ["-100%", "400%"] }}
                    transition={{
                      duration: 1.4,
                      ease: "easeInOut",
                      repeat: Infinity,
                    }}
                  />
                </div>
              )}

              {/* Cancel — copy phase only. Once isFinalizing (verify/swap
                  has started), disable to "Finishing up..." (design.md #4,
                  locked copy). */}
              <div className="mt-5 flex items-center justify-end">
                <button
                  onClick={handleCancel}
                  disabled={isFinalizing}
                  aria-disabled={isFinalizing}
                  className="px-4 py-1.5 rounded-full text-[13px] font-medium text-steel hover:text-slate hover:bg-white/40 cursor-pointer transition-all duration-200 disabled:cursor-default disabled:opacity-60"
                >
                  {isFinalizing ? "Finishing up..." : "Cancel"}
                </button>
              </div>
            </motion.div>
          )}

          {/* ── success ─────────────────────────────────────────────────── */}
          {phase === "success" && (
            <motion.div
              key="success"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={PHASE_FADE}
            >
              <h2
                id="relocate-heading"
                className="font-display text-[18px] font-normal text-slate text-center mb-3"
              >
                Models moved
              </h2>
              {/* Echo the new path in-body. Long paths get word-break so the
                  paragraph never overflows the 420px card. */}
              <p className="text-[13px] text-steel leading-relaxed mb-6 text-center">
                Your models are now in{" "}
                <span
                  className="text-slate break-all"
                  title={destination}
                  aria-label={destination}
                >
                  {destination}
                </span>
                . Dictation is ready again.
              </p>

              <div className="flex items-center justify-center">
                <button
                  onClick={onClose}
                  className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
                  style={gradientPillStyle}
                >
                  Done
                </button>
              </div>
            </motion.div>
          )}

          {/* ── error ───────────────────────────────────────────────────── */}
          {phase === "error" && (
            <motion.div
              key="error"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={PHASE_FADE}
            >
              <h2
                id="relocate-heading"
                className="font-display text-[18px] font-normal text-slate text-center mb-3"
              >
                Couldn't move the models
              </h2>
              {/* The load-bearing copy is the reassurance that nothing was lost
                  (design.md decision #8). The {reason} comes first if present,
                  then the reassurance so users read it even if they skim. */}
              <p className="text-[13px] text-steel leading-relaxed mb-6 text-center">
                {errorMsg && (
                  <span className="text-rose">{errorMsg} </span>
                )}
                Nothing was lost. Your models are still in their original folder
                and ready to use.
              </p>

              <div className="flex items-center justify-center gap-2.5">
                <button
                  onClick={onClose}
                  className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                  style={glassPillStyle}
                >
                  Close
                </button>
                <button
                  onClick={handleRetry}
                  className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
                  style={gradientPillStyle}
                >
                  Try again
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  );
}
