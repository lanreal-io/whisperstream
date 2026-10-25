/** Ported benchmark-dialog visuals (tiered-benchmark Phase 2, Task 2.2).
 *
 *  These are the prototype pieces from PR #269's `HybridVariant` re-inlined for
 *  production. The dialog family deliberately has NO shared base component, so the shared bits
 *  from the prototype's `shared.tsx` / `LadderVariant.tsx` are re-inlined here
 *  rather than imported from the (dev-only) benchproto tree. They live in a
 *  module co-located with `BenchmarkFlow.tsx`; Task 2.3 wires them into the real
 *  flow phases (WS ticks, verdict cards, chip morph). Nothing here does flow
 *  wiring on its own.
 *
 *  What changed from the prototype:
 *  - `ProtoModel`/`LADDER_MODELS`/`GEMMA_MODEL` are replaced by the real catalog
 *    helper (`LOCAL_MODEL_DISPLAY_LABEL`, keyed on the
 *    backend catalog id). `ModelChip` / `WinnerHero` take an `id` and look the
 *    name up, so the dialog and the Enhancement picker always name a
 *    given model identically.
 *  - `SpeedBar` gains a real live-fill mode: instead of animating to a fixed
 *    `value` over a fixed 1.6s, the fill width TRACKS the latest generated
 *    words/sec measurement, eased ~250ms per tick, with a ~1.2s
 *    minimum-animation floor so a sub-second decode still eases up (never
 *    flashes). Single-model live measurements use the scale's rose / caution /
 *    mint classification, while the automatic ladder keeps its frost handoff
 *    until a terminal verdict lands.
 */
import type { ReactNode } from "react";
import { memo, useEffect } from "react";
import { animate, motion, useMotionValue, useTransform } from "framer-motion";
import { LOCAL_MODEL_DISPLAY_LABEL } from "../lib/localModels";

/* ---------- Shared tokens (re-inlined from the prototype's shared.tsx) ---------- */

/** The standard entrance spring (frontend backlog #11 — not yet a shared lib
 *  const; re-inlined here per the no-shared-base dialog decision). */
export const SPRING = { type: "spring", stiffness: 400, damping: 30 } as const;

const glassPillStyle = {
  background: "var(--surface-modal)",
  border: "1px solid var(--border-hairline-steel)",
  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
} as const;

const gradientPillStyle = {
  background: "var(--accent-fill)",
  boxShadow: "var(--shadow-pearl)",
} as const;

/* ---------- Pills ---------- */

/** Primary gradient pill (dialog family primary action). */
export function PrimaryPill({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick?: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
      style={gradientPillStyle}
    >
      {children}
    </button>
  );
}

/** Secondary glass pill. */
export function GlassPill({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick?: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
      style={glassPillStyle}
    >
      {children}
    </button>
  );
}

/* ---------- Life-sign status line ---------- */

/** Status line whose dot becomes a lighter breathing life sign once real WPS
 *  samples are streaming. The state is also announced without adding visible
 *  copy that would compete with the numeric readout. */
export const LifeSignStatus = memo(function LifeSignStatus({
  status,
  active = false,
}: {
  status: string;
  active?: boolean;
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-live-measurement={active ? "true" : "false"}
      className="flex items-center gap-1.5"
    >
      <motion.span
        aria-hidden="true"
        data-live-dot={active ? "active" : "idle"}
        className="w-1.5 h-1.5 rounded-full shrink-0"
        style={{
          background: active ? "var(--color-frost)" : "var(--accent-fill)",
          boxShadow: active
            ? "0 0 7px rgba(126, 174, 212, 0.42)"
            : "none",
        }}
        animate={
          active
            ? { opacity: [1, 0.35, 1], scale: [1, 0.78, 1] }
            : { opacity: 0.78, scale: 1 }
        }
        transition={
          active
            ? { duration: 1.35, repeat: Infinity, ease: "easeInOut" }
            : { duration: 0.2, ease: "easeOut" }
        }
      />
      <p className="text-[11px] text-steel/60 italic">
        {active ? <span className="sr-only">Live measurement. </span> : null}
        {status}
      </p>
    </div>
  );
});

/* ---------- Model chips (re-inlined from LadderVariant.tsx) ---------- */

export type ChipStatus = "queued" | "active" | "dimmed" | "landed";

/** A model pill chip. `id` is a backend catalog id; the name + tag are looked
 *  up from the shared catalog maps so the chip and the Enhancement picker name a
 *  model identically. The `layoutId` lets the winning chip morph into
 *  `WinnerHero` (framer layout animation) when both mount under one
 *  `AnimatePresence` with a matching `layoutPrefix`; `null` opts out (the
 *  onboarding run keeps the chip in place instead of morphing).
 *
 *  `landed` is WinnerHero's mint dot and `--halo-mint` bloom kept inline-left,
 *  for the onboarding fast verdict. `sizeLabel` adds the download size tag. */
export function ModelChip({
  id,
  status,
  layoutPrefix = "benchmark",
  animateActive = true,
  sizeLabel,
}: {
  id: string;
  status: ChipStatus;
  layoutPrefix?: string | null;
  /** Repeating motion communicates which rung is active in the automatic
   *  ladder. Single-model checks disable it because there is no handoff. */
  animateActive?: boolean;
  /** Compact download size ("462 MB"), shown as a steel tag after the name. */
  sizeLabel?: string;
}) {
  const name = LOCAL_MODEL_DISPLAY_LABEL[id] ?? id;
  const hasActiveMotion = status === "active" && animateActive;
  const chip = (
    <motion.div
      layoutId={layoutPrefix ? `${layoutPrefix}-chip-${id}` : undefined}
      data-model-chip-status={status}
      data-model-chip-motion={hasActiveMotion ? "animated" : "static"}
      className="relative overflow-hidden rounded-full px-4 py-2 flex items-center gap-2"
      style={{
        background: "var(--surface-modal)",
        border:
          status === "active"
            ? "1px solid var(--color-frost)"
            : "1px solid var(--border-emphasis)",
        boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
      }}
      animate={{
        scale: status === "dimmed" ? 0.94 : 1,
        opacity: status === "dimmed" ? 0.45 : 1,
      }}
      transition={SPRING}
    >
      {/* Shimmer sweep inside the active chip only. */}
      {hasActiveMotion && (
        <motion.div
          data-model-chip-shimmer
          className="absolute inset-0 pointer-events-none"
          style={{
            background:
              "linear-gradient(90deg, transparent, var(--frost-mesh-1), transparent)",
          }}
          animate={{ x: ["-100%", "100%"] }}
          transition={{ duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
        />
      )}
      {hasActiveMotion && (
        <motion.span
          data-model-chip-pulse
          className="w-1.5 h-1.5 rounded-full shrink-0 relative"
          style={{
            background: "var(--accent-fill)",
            boxShadow: "0 0 8px rgba(126, 174, 212, 0.55)",
          }}
          animate={{ opacity: [1, 0.4, 1] }}
          transition={{ duration: 1.6, repeat: Infinity, ease: "easeInOut" }}
        />
      )}
      {status === "landed" && (
        <motion.span
          data-model-chip-landed
          aria-hidden="true"
          className="w-2 h-2 rounded-full bg-mint shrink-0 relative"
          style={{ boxShadow: "0 0 10px rgba(91, 175, 124, 0.5)" }}
          initial={{ scale: 0 }}
          animate={{ scale: 1 }}
          transition={{ ...SPRING, delay: 0.15 }}
        />
      )}
      <span className="text-[13px] font-medium text-slate relative">{name}</span>
      {sizeLabel && (
        <span className="text-[11px] text-steel/70 tabular-nums relative">
          {sizeLabel}
        </span>
      )}
    </motion.div>
  );
  if (status !== "landed") return chip;
  // The halo sits outside the chip (which clips its own shimmer) so the bloom
  // reads as light on the card behind it, anchored at the chip's leading edge.
  return (
    <div className="relative inline-flex">
      <motion.div
        aria-hidden="true"
        className="absolute left-2 top-1/2 -mt-20 w-40 h-40 rounded-full pointer-events-none"
        style={{
          background: "radial-gradient(circle, var(--halo-mint) 0%, transparent 70%)",
        }}
        initial={{ opacity: 0, scale: 0.6 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.7, ease: [0.2, 0, 0, 1], delay: 0.1 }}
      />
      {chip}
    </div>
  );
}

/** The winning chip, morphed into the success hero via the shared `layoutId`.
 *  A `--halo-mint` radial blooms behind it and a mint dot pops in as it lands. */
export function WinnerHero({
  id,
  layoutPrefix = "benchmark",
}: {
  id: string;
  layoutPrefix?: string;
}) {
  const name = LOCAL_MODEL_DISPLAY_LABEL[id] ?? id;
  return (
    <div className="relative flex justify-center mb-4">
      {/* Mint halo blooms behind the landed chip. */}
      <motion.div
        className="absolute inset-0 flex items-center justify-center pointer-events-none"
        initial={{ opacity: 0, scale: 0.6 }}
        animate={{ opacity: 1, scale: 1.6 }}
        transition={{ duration: 0.7, ease: [0.2, 0, 0, 1], delay: 0.25 }}
      >
        <div
          className="w-28 h-28 rounded-full"
          style={{
            background:
              "radial-gradient(circle, var(--halo-mint) 0%, transparent 70%)",
          }}
        />
      </motion.div>
      <motion.div
        layoutId={`${layoutPrefix}-chip-${id}`}
        className="relative rounded-full px-5 py-2.5 flex items-center gap-2.5"
        style={{
          background: "var(--surface-modal)",
          border: "1px solid var(--border-emphasis)",
          boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
        }}
        transition={SPRING}
      >
        <motion.span
          className="w-2 h-2 rounded-full bg-mint shrink-0"
          style={{ boxShadow: "0 0 10px rgba(91, 175, 124, 0.5)" }}
          initial={{ scale: 0 }}
          animate={{ scale: 1 }}
          transition={{ ...SPRING, delay: 0.3 }}
        />
        <span className="text-[14px] font-medium text-slate">{name}</span>
      </motion.div>
    </div>
  );
}

/* ---------- SpeedBar ---------- */

/** Bar scale and zone boundaries, in displayed words/sec (tok/s divided by
 *  ~1.3 tokens/word). Locked in review + design.md Decision 2: green >= 50 tok/s
 *  (~38 wps), yellow 35-50 tok/s (~27-38 wps), red < 35 tok/s (~27 wps). These
 *  are the ONLY copy of the dialog tick constants (the authoritative verdict
 *  thresholds live in gpu_benchmark.py). */
export const MAX = 60;
export const BLOCK = 27; // 35 tok/s
export const PASS = 38; // 50 tok/s

/** ~1.2s floor so a sub-second decode still eases up before the verdict lands
 *  (never flashes); subsequent live ticks track in ~250ms. */
const MIN_FILL_ANIMATION_S = 1.2;
const PER_TICK_FILL_S = 0.25;

export type SpeedBarMode = "marquee" | "fill";
export type SpeedTone = "frost" | "mint" | "caution" | "rose";

/** Fill color. `caution` is the signal-yellow marginal hue
 *  (tiered-benchmark round 01, Issue 4), deliberately distinct from
 *  `--color-gold` (the Pro/upgrade hue). */
const TONE_FILL: Record<SpeedTone, string> = {
  frost: "var(--color-frost)",
  mint: "var(--color-mint)",
  caution: "var(--color-caution)",
  rose: "var(--color-rose)",
};

/** Classify a displayed live measurement against the scale painted behind it.
 *  Terminal verdicts remain authoritative and are supplied separately by the
 *  flow's result phases. */
export function speedToneForWps(wps: number): Exclude<SpeedTone, "frost"> {
  if (wps < BLOCK) return "rose";
  if (wps < PASS) return "caution";
  return "mint";
}

/** Convert decode tok/s to displayed words/sec (~1.3 tokens per word), rounded.
 *  The prototype only had this as a comment; here it is real so the readout and
 *  the fill width both reflect true measured throughput. */
export function tokPerSecToWordsPerSec(tps: number): number {
  return Math.round(tps / 1.3);
}

/** The one bar that carries the whole speed story: an indeterminate marquee
 *  while pre-decode work runs, then a determinate fill that tracks the live
 *  decode speed, with faint zone bands and two cutoff ticks.
 *
 *  - `mode="marquee"` — indeterminate sweep (model load / prefilter).
 *  - `mode="fill"` — determinate fill. The target words/sec is taken from the
 *    live `wps` prop (measured generated words/s) when present, otherwise from the
 *    static `value` prop (a landed receipt on a verdict card). The width eases
 *    to `min(wps, MAX) / MAX` and the readout counts up via a rounded transform
 *    of the same motion value.
 *  - `tone` — the fill color. Single-model checks classify live WPS; automatic
 *    ladder handoffs stay frost; terminal cards pass the authoritative verdict
 *    tone. */
export function SpeedBar({
  mode,
  wps,
  value,
  tone = "frost",
  caption,
  settled = false,
}: {
  mode: SpeedBarMode;
  /** Live measured generated words/s. Takes precedence over `value`. */
  wps?: number;
  /** Static final words/sec for a landed receipt bar (verdict cards). */
  value?: number;
  /** Fill color; frost while measuring, verdict color when landed. */
  tone?: SpeedTone;
  /** Optional readout-row label (steel, small) shown IN PLACE OF the counting
   *  `<number> words / sec` readout. Used by the cached SlowModelConsentDialog
   *  receipt, where only the verdict was cached and there is no precise number
   *  to show (`From your last speed check`). The fill still positions from
   *  `value` so the bar sits in the caution band; only the numeric readout is
   *  suppressed. Ignored while `mode="marquee"` (no readout row then). */
  caption?: string;
  /** Show an authoritative landed value immediately instead of easing toward
   *  it. Used by the per-device completion dwell, where the full one-second
   *  beat must display the terminal reading rather than an animation toward it. */
  settled?: boolean;
}) {
  const mv = useMotionValue(0);
  const shown = useTransform(mv, (v) => Math.round(v));
  const width = useTransform(mv, (v) => `${(Math.min(v, MAX) / MAX) * 100}%`);

  // Target words/sec: live measured WPS wins, else the static receipt value.
  const targetWps = wps != null ? wps : Math.round(value ?? 0);

  useEffect(() => {
    // Marquee doesn't read the fill value; reset it so the next fill starts
    // from zero and gets the full min-animation floor.
    if (mode !== "fill") {
      mv.set(0);
      return;
    }
    if (settled) {
      mv.set(targetWps);
      return;
    }
    // Duration is decided from the live motion value (robust to StrictMode
    // double-mount and re-mounts): a fill starting from ~0 gets the floor so it
    // never flashes; a live tick that only nudges an already-filled bar tracks
    // quickly.
    const fromZero = mv.get() < 0.5;
    const duration = fromZero ? MIN_FILL_ANIMATION_S : PER_TICK_FILL_S;
    const anim = animate(mv, targetWps, { duration, ease: [0.2, 0, 0, 1] });
    return () => anim.stop();
    // mv is a stable ref from useMotionValue; re-run only when the mode or the
    // target changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, targetWps, settled]);

  return (
    <div
      data-speed-mode={mode}
      data-speed-tone={tone}
      data-speed-target-wps={mode === "fill" ? targetWps : undefined}
    >
      {/* Counting readout above the bar's right edge (fill mode only). A
          `caption` (e.g. the cached consent receipt) replaces the number. */}
      <div className="h-4 mb-1 flex justify-end items-baseline gap-1">
        {mode === "fill" &&
          (caption != null ? (
            <span className="text-[11px] text-steel leading-none">{caption}</span>
          ) : (
            <>
              {settled ? (
                <span className="font-display text-[13px] text-slate leading-none">
                  {Math.round(targetWps)}
                </span>
              ) : (
                <motion.span className="font-display text-[13px] text-slate leading-none">
                  {shown}
                </motion.span>
              )}
              <span className="text-[9px] uppercase tracking-wider text-steel">
                words / sec
              </span>
            </>
          ))}
      </div>

      <div className="relative">
        <div
          className="w-full h-2 rounded-full overflow-hidden relative"
          style={{ background: "var(--frost-mesh-1)" }}
        >
          {/* Zone bands, faint so they read as context, not verdicts. */}
          <div
            className="absolute inset-y-0 left-0"
            style={{
              width: `${(BLOCK / MAX) * 100}%`,
              background:
                "color-mix(in srgb, var(--color-rose) 14%, transparent)",
            }}
          />
          <div
            className="absolute inset-y-0"
            style={{
              left: `${(BLOCK / MAX) * 100}%`,
              width: `${((PASS - BLOCK) / MAX) * 100}%`,
              background: "var(--color-caution-mist)",
            }}
          />

          {/* Keys are lifecycle-critical: Framer Motion's marquee owns an
              animated x transform that must never survive onto the left-
              anchored solid fill when React reconciles this branch. */}
          {mode === "marquee" ? (
            <motion.div
              key="marquee"
              data-speed-fill="marquee"
              className="absolute inset-y-0 w-1/3 rounded-full"
              style={{
                background:
                  "linear-gradient(90deg, transparent, var(--color-frost), transparent)",
              }}
              animate={{ x: ["-100%", "400%"] }}
              transition={{ duration: 1.5, repeat: Infinity, ease: "easeInOut" }}
            />
          ) : (
            <motion.div
              key="solid"
              data-speed-fill="solid"
              className="absolute inset-y-0 left-0 rounded-full overflow-hidden"
              style={{
                width: settled
                  ? `${(Math.min(targetWps, MAX) / MAX) * 100}%`
                  : width,
                background: TONE_FILL[tone],
                transition: "background 0.5s ease",
              }}
            >
              {/* The automatic ladder's frost handoff retains its shimmer.
                  Semantic live and terminal fills are solid. */}
              {tone === "frost" && (
                <motion.div
                  data-speed-fill-shimmer
                  className="absolute inset-y-0 w-1/2"
                  style={{
                    background:
                      "linear-gradient(90deg, transparent, rgba(255,255,255,0.55), transparent)",
                  }}
                  animate={{ x: ["-100%", "300%"] }}
                  transition={{
                    duration: 1.3,
                    repeat: Infinity,
                    ease: "easeInOut",
                  }}
                />
              )}
            </motion.div>
          )}
        </div>

        {/* Cutoff ticks poking above/below the track. */}
        {[BLOCK, PASS].map((t) => (
          <div
            key={t}
            className="absolute w-px -top-1 -bottom-1"
            style={{
              left: `${(t / MAX) * 100}%`,
              background: "var(--color-frost-deep)",
              opacity: 0.55,
            }}
          />
        ))}
      </div>
    </div>
  );
}

/* ---------- Per-device completion chip ---------- */

export type BenchmarkDeviceVerdict = "viable" | "marginal" | "unviable";

export function completionTone(
  verdict: BenchmarkDeviceVerdict,
): "mint" | "caution" | "rose" {
  if (verdict === "viable") return "mint";
  if (verdict === "marginal") return "caution";
  return "rose";
}

export function DeviceCompletionStatus({
  verdict,
  receipt = false,
}: {
  verdict: BenchmarkDeviceVerdict;
  /** Settled verdict-card receipt (onboarding): the marginal label uses the
   *  deeper caution hue, which stays legible as text on light glass. */
  receipt?: boolean;
}) {
  const label =
    verdict === "viable"
      ? "Target met"
      : verdict === "marginal"
        ? "Near target"
        : "Below target";
  const color =
    verdict === "viable"
      ? "var(--color-mint)"
      : verdict === "marginal"
        ? receipt
          ? "var(--color-caution-deep)"
          : "var(--color-caution)"
        : "var(--color-rose)";

  return (
    <div
      role="status"
      aria-live="polite"
      data-device-completion={verdict}
      className="h-[18px] flex items-center gap-1.5 text-[12px] font-medium"
      style={{ color }}
    >
      {verdict === "viable" ? (
        <svg
          width="13"
          height="13"
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
          data-device-completion-check
        >
          <path d="m3.5 8.2 2.7 2.7 6.3-6.3" />
        </svg>
      ) : null}
      <span>{label}</span>
    </div>
  );
}
