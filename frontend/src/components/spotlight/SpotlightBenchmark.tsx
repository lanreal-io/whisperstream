import { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";

/* ---------------------------------------------------------------------------
   SpotlightBenchmark — the Cohere offer's "download + check my hardware"
   hand-off, rendered INSIDE the spotlight card (as FeatureSpotlight's
   benchmarkSlot) so there is exactly ONE backdrop.

   STUB: the download + benchmark are simulated entirely client-side. There is
   no real Cohere STT backend yet (deferred to its own PR), and the real
   enhancement benchmark is a singleton-guarded, LLM-decode-tok/s path that does
   not fit a Parakeet-class STT model — so this never calls the benchmark REST
   and never mutates enhancement config. Design decision: reuse the BenchmarkFlow
   *visual language*, not its modal shell (which brings its own backdrop).
--------------------------------------------------------------------------- */

type Phase = "download" | "checking" | "verdict";

export interface SpotlightBenchmarkProps {
  modelLabel: string;
  /** Return to the offer card. */
  onBack: () => void;
  /** "Use" the model. STUB: shows nothing real yet; wired when the backend ships. */
  onUse: () => void;
  /** Deterministic QA: start frozen at the verdict, no timers. */
  frozen?: boolean;
}

export function SpotlightBenchmark({ modelLabel, onBack, onUse, frozen = false }: SpotlightBenchmarkProps) {
  const reduce = useReducedMotion() ?? false;
  const [phase, setPhase] = useState<Phase>(frozen ? "verdict" : "download");
  const [pct, setPct] = useState(frozen ? 100 : 0);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);

  useEffect(() => {
    if (frozen) return;
    const schedule = (fn: () => void, ms: number) => {
      timers.current.push(setTimeout(fn, reduce ? Math.min(ms, 120) : ms));
    };
    let p = 0;
    const tick = () => {
      p += reduce ? 40 : 6 + Math.random() * 9;
      if (p >= 100) {
        setPct(100);
        setPhase("checking");
        schedule(() => setPhase("verdict"), reduce ? 200 : 900);
      } else {
        setPct(p);
        schedule(tick, reduce ? 100 : 240);
      }
    };
    schedule(tick, 300);
    return () => {
      timers.current.forEach(clearTimeout);
      timers.current = [];
    };
  }, [reduce]);

  const title =
    phase === "download"
      ? `Downloading ${modelLabel}`
      : phase === "checking"
        ? "Checking your hardware"
        : `${modelLabel} is ready`;
  const sub =
    phase === "download"
      ? "This runs once. You can keep working."
      : phase === "checking"
        ? "Measuring how fast it runs here."
        : "Your machine has the headroom to run it.";

  return (
    <div className="px-7 pb-7 pt-6" role="group" aria-label="Hardware check">
      <h2 className="mb-1 font-display text-[19px] font-light text-slate">{title}</h2>
      <p className="mb-5 text-[12.5px] text-steel">{sub}</p>

      <div
        className="mb-2.5 h-2 overflow-hidden rounded-full"
        style={{ background: "color-mix(in srgb, var(--color-slate) 8%, transparent)" }}
      >
        <motion.div
          className="h-full rounded-full"
          style={{ background: "var(--accent-fill)", boxShadow: "var(--shadow-pearl)" }}
          animate={{ width: `${phase === "download" ? pct : 100}%` }}
          transition={{ duration: reduce ? 0 : 0.3, ease: "easeOut" }}
        />
      </div>
      <div
        className="mb-5 flex justify-between text-[11.5px] text-steel"
        style={{ fontVariantNumeric: "tabular-nums" }}
      >
        <span>{phase === "download" ? `${Math.round(pct)}%` : "100%"}</span>
        <span>{phase === "checking" ? "benchmarking..." : "~180 MB"}</span>
      </div>

      {phase === "verdict" && (
        <div
          className="mb-5 flex items-center gap-3 rounded-[var(--radius-input)] p-3.5"
          style={{
            background: "color-mix(in srgb, var(--color-mint) 12%, transparent)",
            border: "1px solid color-mix(in srgb, var(--color-mint) 32%, transparent)",
          }}
        >
          <span
            className="grid h-[34px] w-[34px] flex-none place-items-center rounded-full"
            style={{ background: "color-mix(in srgb, var(--color-mint) 20%, transparent)", color: "var(--color-mint)" }}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M3 8.5L6.5 12L13 4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
          <span>
            <b className="text-[13.5px] font-semibold text-slate">Your PC can run it</b>
            <span className="mt-0.5 block text-[12px] text-steel">Ready to transcribe at about 3.1x real-time.</span>
          </span>
        </div>
      )}

      {/* primary pill LAST -> right side (app convention) */}
      <div className="flex items-center justify-end gap-2.5">
        <button
          onClick={onBack}
          className="cursor-pointer rounded-full px-5 py-2 text-[13px] font-medium text-slate transition-all duration-200"
          style={{
            background: "var(--surface-modal)",
            border: "1px solid var(--border-emphasis)",
            boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
          }}
        >
          Not now
        </button>
        <button
          onClick={onUse}
          disabled={phase !== "verdict"}
          className="cursor-pointer rounded-full px-5 py-2 text-[13px] font-medium text-white transition-all duration-200 disabled:cursor-default disabled:opacity-40"
          style={{ background: "var(--accent-fill)", boxShadow: "var(--shadow-pearl)" }}
        >
          Use {modelLabel}
        </button>
      </div>
    </div>
  );
}
