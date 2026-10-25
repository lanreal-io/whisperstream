import { motion, useReducedMotion } from "framer-motion";

export type HudLightState = "ready" | "attention" | "off";

/** Bead tones. The overall light uses ready/attention/off only (no red in the
 *  light); provider rows add busy, error and the hollow idle ring. */
export type BeadTone = "ready" | "attention" | "busy" | "error" | "off" | "idle";

const DOT_TOKEN: Record<BeadTone, string | null> = {
  ready: "var(--color-mint)",
  attention: "var(--color-gold)",
  busy: "var(--color-frost)",
  error: "var(--color-rose)",
  off: "var(--color-steel)",
  idle: null,
};

// Emitted-light halo, the dialog-icon halo tokens (faint in light, brighter in
// dark). Off, idle and error emit no light: a failed install is static.
const HALO_TOKEN: Partial<Record<BeadTone, string>> = {
  ready: "var(--halo-mint)",
  attention: "var(--halo-gold)",
  busy: "var(--halo-frost)",
};

// Locked copy #9: visible micro-labels.
const LABEL: Record<HudLightState, string> = {
  ready: "Ready",
  attention: "Attention",
  off: "Off",
};

// Locked copy #10: per-state aria sentence map (NOT a `status: ${state}` template).
const ARIA_LABEL: Record<HudLightState, string> = {
  ready: "Agents is on and ready",
  attention: "Agents needs attention",
  off: "Agents is off",
};

/** The glass bead: a token dot with a caught-light rim and an emitted halo.
 *  Shared by the overall light and each provider row so they read as one family. */
export function StatusBead({ tone }: { tone: BeadTone }) {
  const reduce = useReducedMotion();
  const halo = HALO_TOKEN[tone];
  const breathing = (tone === "attention" || tone === "busy") && !reduce;
  const dot = DOT_TOKEN[tone];
  return (
    <span aria-hidden className="relative flex shrink-0 items-center justify-center" style={{ width: 18, height: 18 }}>
      {halo && (
        <motion.span
          className="absolute inset-0 rounded-full"
          style={{
            background: `radial-gradient(circle, ${halo} 0%, transparent 70%)`,
            transform: "scale(1.6)",
          }}
          initial={false}
          animate={breathing ? { opacity: [0.55, 1, 0.55] } : { opacity: 1 }}
          transition={
            breathing
              ? { duration: 2.4, repeat: Infinity, ease: "easeInOut" }
              : { duration: 0.2 }
          }
        />
      )}
      <span
        className="relative rounded-full transition-colors duration-200"
        style={
          dot === null
            ? { width: 8, height: 8, boxShadow: "inset 0 0 0 1.5px var(--color-steel)", opacity: 0.55 }
            : { width: 8, height: 8, background: dot, boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.6 * var(--rim-scale)))" }
        }
      />
    </span>
  );
}

const ChevronGlyph = () => (
  <svg
    width="8"
    height="8"
    viewBox="0 0 8 8"
    aria-hidden="true"
    className="-translate-x-[3px] text-steel opacity-0 transition duration-150 group-hover:translate-x-0 group-hover:opacity-100 group-focus-visible:translate-x-0 group-focus-visible:opacity-100"
  >
    <path d="M2.5 1l3 3-3 3" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

export interface StatusLightProps {
  state: HudLightState;
  /** Overrides the state's default accessible sentence. */
  ariaLabel?: string;
  /** When present the light becomes a button (e.g. Attention opens Connections). */
  onActivate?: () => void;
  activateLabel?: string;
}

/** The pill's own dot: smaller than the bead and lit by a soft glow instead
 *  of a halo, so the hairline pill stays the object and the dot its signal
 *  (owner-accepted direction, 2026-10-25). Attention breathes as the bead does. */
function PillDot({ state }: { state: HudLightState }) {
  const reduce = useReducedMotion();
  const color = DOT_TOKEN[state] as string;
  const lit = state !== "off";
  return (
    <motion.span
      aria-hidden
      className="shrink-0 rounded-full transition-colors duration-200"
      style={{
        width: 7,
        height: 7,
        background: color,
        boxShadow: lit ? `0 0 6px ${color}` : "none",
        opacity: lit ? 1 : 0.6,
      }}
      initial={false}
      animate={state === "attention" && !reduce ? { opacity: [0.55, 1, 0.55] } : { opacity: lit ? 1 : 0.6 }}
      transition={
        state === "attention" && !reduce
          ? { duration: 2.4, repeat: Infinity, ease: "easeInOut" }
          : { duration: 0.2 }
      }
    />
  );
}

const PILL =
  "flex select-none items-center gap-1.5 rounded-full border border-[var(--border-hairline-steel)] px-2.5 py-[3px] text-[11px] leading-4 text-steel transition-colors duration-200";

export function StatusLight({ state, ariaLabel, onActivate, activateLabel }: StatusLightProps) {
  const inner = (
    <>
      <PillDot state={state} />
      <span>{LABEL[state]}</span>
    </>
  );
  if (onActivate) {
    return (
      <button
        type="button"
        onClick={onActivate}
        aria-label={activateLabel ?? ariaLabel ?? ARIA_LABEL[state]}
        className={`group cursor-pointer ${PILL} hover:border-[var(--border-accent)] hover:bg-[var(--surface-hover)] focus-visible:border-[var(--border-accent)] focus-visible:bg-[var(--surface-hover)]`}
      >
        {inner}
        <ChevronGlyph />
      </button>
    );
  }
  return (
    <div
      role="status"
      aria-label={ariaLabel ?? ARIA_LABEL[state]}
      className={PILL}
    >
      {inner}
    </div>
  );
}
