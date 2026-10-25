import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { startBenchmark } from "../lib/api/enhancement";
import type { AppEvent } from "../lib/socket/events";
import { LOCAL_MODEL_DISPLAY_LABEL } from "../lib/localModels";
import {
  SpeedBar,
  LifeSignStatus,
  BLOCK,
  PASS,
  speedToneForWps,
  tokPerSecToWordsPerSec,
} from "./BenchmarkVisuals";

/* ---------- Dialog shell tokens ----------
   Dialog family A ("no shared base by design"): the shell /
   backdrop / entrance recipe is copied verbatim from EnableLocalConfirmDialog,
   spelled in theme tokens. This is the ONLY manual "switch up to a slow
   fine-tune" consent surface (design.md round 01, Decision 5). Against-advice
   action = SECONDARY glass pill; the safe path = PRIMARY gradient pill. */
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
  boxShadow: "var(--shadow-pearl)",
};

/** A representative marginal fill for the CACHED receipt bar: only the verdict
 *  was cached (no precise number), so we position the fill midway through the
 *  caution band (between the BLOCK and PASS ticks) and show the microcopy
 *  caption instead of a fabricated words/sec readout. */
const CACHED_RECEIPT_WPS = Math.round((BLOCK + PASS) / 2);

interface SlowModelConsentDialogProps {
  /** When false, the component renders nothing (mirrors EnableLocalConfirmDialog).
   *  The parent also mounts this conditionally, so `open` is effectively always
   *  true when mounted; the gate is kept for template symmetry. */
  open: boolean;
  /** The fixed-prompt model the user is switching to (Stream-1 Mini). Its
   *  display name drives the copy. */
  modelId: string;
  /** "cached" — the model's verdict is already `marginal` in the cache: render
   *  the consent card immediately with a static caution receipt bar (no bench).
   *  "fresh" — no cached verdict: run a single-model manual bench first, then
   *  show consent (marginal), apply directly (viable), or block (unviable). */
  variant: "cached" | "fresh";
  /** Raw subscribe from useAppSocket(). Needed for the "fresh" bench's
   *  benchmark.progress / benchmark.result stream. */
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  /** The user accepts the model. `verdict` is the tri-state to cache for it
   *  ("marginal" from the consent card; "viable" when a fresh bench cleared).
   *  The parent applies the model + caches the verdict in one combined patch. */
  onUse: (modelId: string, verdict: "viable" | "marginal") => void;
  /** Keep the current model, no switch (also Esc / backdrop click). */
  onKeep: () => void;
}

type ConsentPhase = "measuring" | "consent" | "blocked";

export function SlowModelConsentDialog({
  open,
  modelId,
  variant,
  subscribe,
  onUse,
  onKeep,
}: SlowModelConsentDialogProps) {
  // Cached opens straight on the consent card; fresh measures first.
  const [phase, setPhase] = useState<ConsentPhase>(
    variant === "cached" ? "consent" : "measuring",
  );
  // Fresh-bench live generated throughput (words/s) -> the measuring SpeedBar
  // switches from marquee to a semantic live fill once the first tick lands.
  const [liveWps, setLiveWps] = useState<number | undefined>(undefined);
  // Fresh-bench final measured speed (words/sec) for the live consent receipt.
  const [measuredWps, setMeasuredWps] = useState<number>(0);

  // Kick the single-model manual bench once (StrictMode double-mount guard).
  const benchStartedRef = useRef(false);
  // Consume benchmark.result exactly once (defensive against a duplicate emit).
  const resultHandledRef = useRef(false);
  // The safe (read-before-act) button, mount-focused per the family-A a11y
  // contract when a consent / blocked card is showing.
  const safeBtnRef = useRef<HTMLButtonElement>(null);

  const name = LOCAL_MODEL_DISPLAY_LABEL[modelId] ?? modelId;

  // Fresh variant: start the manual bench for exactly this model on open.
  useEffect(() => {
    if (!open || variant !== "fresh" || benchStartedRef.current) return;
    benchStartedRef.current = true;
    startBenchmark({ mode: "manual", modelId }).catch((e) => {
      const msg = String(e);
      // 409 = a benchmark is already running (StrictMode double-invoke). Join it.
      if (msg.includes("409")) return;
      console.error("[SlowConsent] bench start failed error_type=RemoteError");
    });
  }, [open, variant, modelId]);

  // Fresh variant: single WS subscription for this model's bench stream.
  useEffect(() => {
    if (!open || variant !== "fresh") return;
    return subscribe((event) => {
      if (event.type === "benchmark.progress") {
        if (event.phase === "running" && event.model_id === modelId) {
          const wps =
            event.wps ??
            (event.tps != null
              ? tokPerSecToWordsPerSec(event.tps)
              : undefined);
          if (wps != null) {
            // Live generated-word rate -> live-fill the measuring bar. The
            // verdict still lands from authoritative final token timings.
            setLiveWps(wps);
          }
        }
        return;
      }
      if (event.type === "benchmark.result") {
        // Only the manual bench for THIS model drives this dialog (older
        // backends omit `mode`/`measured_model_id` -> don't over-filter).
        if (event.mode && event.mode !== "manual") return;
        if (event.measured_model_id && event.measured_model_id !== modelId)
          return;
        if (resultHandledRef.current) return;
        resultHandledRef.current = true;
        const wps =
          event.measured_tok_per_s != null
            ? tokPerSecToWordsPerSec(event.measured_tok_per_s)
            : 0;
        setMeasuredWps(wps);
        if (event.verdict === "viable") {
          // The model is fine here -> apply it directly (parent caches viable).
          onUse(modelId, "viable");
        } else if (event.verdict === "marginal") {
          setPhase("consent");
        } else {
          // unviable -> blocked; no switch, no Gemini routing (this is a model
          // switch, not an enable).
          setPhase("blocked");
        }
      }
    });
    // subscribe is stable (useCallback in useAppSocket); register once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, variant, modelId, subscribe]);

  // Esc dismisses to the safe path (keep current model), but NOT while a bench
  // is in flight (measuring) — an accidental key must not abandon it silently.
  useEffect(() => {
    if (!open || phase === "measuring") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onKeep();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, phase, onKeep]);

  // Focus the safe button when a consent / blocked card appears (read-first).
  useEffect(() => {
    if (phase === "consent" || phase === "blocked") {
      safeBtnRef.current?.focus();
    }
  }, [phase]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        // Backdrop dismiss (keep) only once measuring is done; an accidental
        // click must not abort an in-flight bench.
        onClick={phase === "measuring" ? undefined : onKeep}
      />

      {/* Glass card — 420px, family A shell. */}
      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[420px] rounded-[var(--radius-card)] p-7"
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="slow-consent-heading"
      >
        {phase === "measuring" && (
          <>
            <h2
              id="slow-consent-heading"
              className="font-display text-[18px] font-normal text-slate mb-2"
            >
              Checking how fast it runs
            </h2>

            <div className="mt-4 mb-3">
              <SpeedBar
                mode={liveWps != null ? "fill" : "marquee"}
                wps={liveWps}
                tone={liveWps != null ? speedToneForWps(liveWps) : "frost"}
              />
            </div>

            <LifeSignStatus status="Testing it on your computer..." />
          </>
        )}

        {phase === "consent" && (
          <>
            <h2
              id="slow-consent-heading"
              className="font-display text-[18px] font-normal text-slate text-center mb-3"
            >
              {name} runs slowly here
            </h2>

            <p className="text-[13px] text-steel mb-5 leading-relaxed text-center">
              Your speed check found it slow. Cleanup will take a moment longer
              after each dictation.
            </p>

            {/* Caution receipt bar. Cached: a representative caution-band fill
                with the "From your last speed check" caption (no fabricated
                number). Fresh: the real measured words/sec. `caution` is the
                signal-yellow marginal hue (--color-caution, Issue 4). */}
            <div className="mb-6">
              <SpeedBar
                mode="fill"
                tone="caution"
                value={variant === "cached" ? CACHED_RECEIPT_WPS : measuredWps}
                caption={
                  variant === "cached" ? "From your last speed check" : undefined
                }
              />
            </div>

            <div className="flex items-center justify-center gap-2.5">
              <button
                onClick={() => onUse(modelId, "marginal")}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
                style={glassPillStyle}
              >
                Use {name} anyway
              </button>
              <button
                ref={safeBtnRef}
                onClick={onKeep}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
                style={gradientPillStyle}
              >
                Keep my current model
              </button>
            </div>
          </>
        )}

        {phase === "blocked" && (
          <>
            <h2
              id="slow-consent-heading"
              className="font-display text-[18px] font-normal text-slate text-center mb-3"
            >
              {name} is too slow here
            </h2>

            <p className="text-[13px] text-steel mb-6 leading-relaxed text-center">
              This model is too slow to run cleanup on this computer.
            </p>

            <div className="flex justify-center">
              <button
                ref={safeBtnRef}
                onClick={onKeep}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
                style={gradientPillStyle}
              >
                Got it
              </button>
            </div>
          </>
        )}
      </motion.div>
    </div>
  );
}
