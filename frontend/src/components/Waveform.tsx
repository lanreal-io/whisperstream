import { createContext, useContext, useState, useEffect, useRef, type RefObject } from "react";
import { motion, useReducedMotion } from "framer-motion";

import type { EnhancementTimeoutProvider } from "../lib/socket/events";

export type HUDState = "idle" | "recording" | "processing" | "done" | "blocked";

// One HUD-owned sample, read only by the visible bars at their existing cadence.
export const AudioLevelsContext = createContext<RefObject<{ rms: number; peak: number }> | null>(null);

/** `pill`: the recording pill's seven bars. `rail`: the same presence
 *  algorithm at text-line size, for the reply rail's live dictation. */
export type VoiceBarsSize = "pill" | "rail";

interface VoiceBarsProps {
  state: HUDState;
  rms: number;
  peak: number;
  color?: string;
  size?: VoiceBarsSize;
  testId?: string;
}

const VOICE_BARS_GEOMETRY: Record<
  VoiceBarsSize,
  { count: number; gap: number; maxHeight: number }
> = {
  pill: { count: 7, gap: 3, maxHeight: 18 },
  rail: { count: 5, gap: 2, maxHeight: 12 },
};
const BAR_WIDTH = 2;
const DOT_SIZE = 2;
const PROCESSING_AMPLITUDE_RATIO = 6 / 18;
const PROCESSING_BASE_RATIO = 10 / 18;
const DEFAULT_COLOR = "#5B7FA5";

/** The painted width of a bar group, for callers that reserve its slot. */
export function voiceBarsWidth(size: VoiceBarsSize): number {
  const { count, gap } = VOICE_BARS_GEOMETRY[size];
  return count * BAR_WIDTH + (count - 1) * gap;
}

/** The tallest a bar paints at this size. */
export function voiceBarsMaxHeight(size: VoiceBarsSize): number {
  return VOICE_BARS_GEOMETRY[size].maxHeight;
}

// Framer Motion spring configs
// Slightly longer duration so springs have time to travel to random targets
const recordingSpring = { type: "spring" as const, bounce: 0.2, duration: 0.4 };
const idleSpring = { type: "spring" as const, damping: 18, stiffness: 180 };

// Processing ripple uses per-bar phase offsets
const RIPPLE_PHASES = [0.0, 1.8, 3.6, 0.9, 4.7, 2.4, 5.2];

export function VoiceBars({
  state,
  rms,
  peak,
  color = DEFAULT_COLOR,
  size = "pill",
  testId,
}: VoiceBarsProps) {
  const audioLevels = useContext(AudioLevelsContext);
  const { count: BAR_COUNT, gap: BAR_GAP, maxHeight: MAX_BAR_HEIGHT } =
    VOICE_BARS_GEOMETRY[size];
  const PROCESSING_BASE = MAX_BAR_HEIGHT * PROCESSING_BASE_RATIO;
  const PROCESSING_AMPLITUDE = MAX_BAR_HEIGHT * PROCESSING_AMPLITUDE_RATIO;
  const [heights, setHeights] = useState<number[]>(
    Array(BAR_COUNT).fill(DOT_SIZE),
  );
  const levelsRef = useRef({ rms: 0, peak: 0 });
  const timeRef = useRef(0);

  levelsRef.current = { rms, peak };

  useEffect(() => {
    if (state === "idle" || state === "done") {
      setHeights(Array(BAR_COUNT).fill(DOT_SIZE));
      return;
    }

    // Recording updates at ~8fps — slower tick lets springs animate between
    // random targets, creating a dancing effect instead of jittery tracking
    const fps = state === "recording" ? 120 : 50;

    const interval = setInterval(() => {
      timeRef.current += fps / 1000;
      const t = timeRef.current;

      if (state === "recording") {
        const { rms: curRms, peak: curPeak } = audioLevels?.current ?? levelsRef.current;
        const level = Math.sqrt(Math.max(curRms, curPeak * 0.6));
        const range = MAX_BAR_HEIGHT - DOT_SIZE;

        setHeights(
          Array.from({ length: BAR_COUNT }, () => {
            // Random 0..1 — each bar gets an independent roll every tick
            const rand = Math.random();
            const h = DOT_SIZE + rand * range * level;
            return Math.max(DOT_SIZE, Math.min(h, MAX_BAR_HEIGHT));
          }),
        );
      } else if (state === "processing") {
        setHeights(
          RIPPLE_PHASES.slice(0, BAR_COUNT).map((phase, i) => {
            const wave = Math.sin(t * 3.5 - i * 0.9 + phase * 0.3);
            return Math.max(DOT_SIZE, PROCESSING_BASE + wave * PROCESSING_AMPLITUDE);
          }),
        );
      }
    }, fps);

    return () => clearInterval(interval);
  }, [
    state,
    audioLevels,
    BAR_COUNT,
    MAX_BAR_HEIGHT,
    PROCESSING_BASE,
    PROCESSING_AMPLITUDE,
  ]);

  return (
    <div
      data-testid={testId}
      data-voice-bars-size={size}
      aria-hidden={size === "rail" ? true : undefined}
      className="flex items-center justify-center"
      style={{ gap: BAR_GAP, height: MAX_BAR_HEIGHT }}
    >
      {heights.map((h, i) => (
        <motion.div
          key={i}
          animate={{ height: h }}
          transition={state === "recording" ? recordingSpring : idleSpring}
          style={{
            width: BAR_WIDTH,
            borderRadius: 9999,
            backgroundColor: color,
            willChange: "height",
          }}
        />
      ))}
    </div>
  );
}

const BLOCKED_COLOR = "#5B7FA5";

/** Dictation is refused until the updated terms are accepted. */
export function BlockedIndicator() {
  return (
    <motion.div
      initial={{ opacity: 0, y: 2 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -2 }}
      transition={{ duration: 0.18, ease: "easeOut" }}
      className="flex items-center justify-center"
      style={{ gap: 6 }}
      role="status"
      aria-live="assertive"
      aria-label="Review and accept the updated terms before using dictation."
    >
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
        {/* Lock body — fades with the rest */}
        <rect
          x="2.5"
          y="5.5"
          width="7"
          height="5"
          rx="1"
          stroke={BLOCKED_COLOR}
          strokeWidth="1.2"
        />
        {/* Lock shackle — drops into place with its own spring */}
        <motion.path
          d="M4 5.5V4a2 2 0 014 0v1.5"
          stroke={BLOCKED_COLOR}
          strokeWidth="1.2"
          strokeLinecap="round"
          fill="none"
          initial={{ y: -2 }}
          animate={{ y: 0 }}
          transition={{ type: "spring", stiffness: 600, damping: 26, delay: 0.18 }}
        />
      </svg>
      <span
        style={{
          fontFamily: "Inter, system-ui, sans-serif",
          fontSize: 9,
          fontWeight: 600,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          color: BLOCKED_COLOR,
          lineHeight: 1,
        }}
      >
        REVIEW TERMS
      </span>
    </motion.div>
  );
}

const NEEDS_INPUT_COLOR = "#7EAED4"; // --color-frost-deep — distinct from blocked's steel #5B7FA5

export function NeedsInputIndicator() {
  return (
    <motion.div
      initial={{ opacity: 0, y: 2 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -2 }}
      transition={{ duration: 0.18, ease: "easeOut" }}
      className="flex items-center justify-center"
      style={{ gap: 6 }}
    >
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
        {/* Speech / question bubble — approachable, NOT a lock */}
        <motion.g
          initial={{ scale: 0.9 }}
          animate={{ scale: 1 }}
          transition={{ type: "spring", stiffness: 600, damping: 26, delay: 0.18 }}
          style={{ transformOrigin: "6px 5px" }}
        >
          <rect
            x="1.8"
            y="1.8"
            width="8.4"
            height="6"
            rx="2"
            stroke={NEEDS_INPUT_COLOR}
            strokeWidth="1.2"
          />
          <path
            d="M4 7.8 L4 10 L6.2 7.8"
            stroke={NEEDS_INPUT_COLOR}
            strokeWidth="1.2"
            strokeLinecap="round"
            strokeLinejoin="round"
            fill="none"
          />
          {/* the "?" — a dot reads cleaner than a glyph at 12px */}
          <circle cx="6" cy="4.6" r="0.7" fill={NEEDS_INPUT_COLOR} />
        </motion.g>
      </svg>
      <span
        style={{
          fontFamily: "Inter, system-ui, sans-serif",
          fontSize: 9,
          fontWeight: 600,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          color: NEEDS_INPUT_COLOR,
          lineHeight: 1,
        }}
      >
        Needs you
      </span>
    </motion.div>
  );
}

// Provider timeout transient (transparent-indicators Task 2.2). Generic
// operational fallbacks use the sibling rose error indicator below.
//
// It borrows the BlockedIndicator recipe in this file (12px
// glyph, 6-8px gap, same 0.18s entrance, same delayed spring for the moving
// part) with one deliberate deviation: a two-line text stack. A single 9px
// uppercase line cannot carry "Anthropic" or "Local model" inside the 120px
// vessel without clipping, and two stacked uppercase lines read as a warning
// label. Nothing failed for the user here: their dictation was transcribed and
// pasted, so the register is a fact, not an alarm.
//
// Gold is the system's caution token and is correct: the dictation succeeded,
// the cleanup did not. Rose is reserved for failures and must not be used
// here. The color lives on the glyph only, so no gold
// text sits on the near-white light HUD surface, and meaning never rides on
// color: the two visible lines plus the announcement carry every fact.

/** 120px vessel - 20px padding - 12px glyph - 8px gap. The binding constraint
 *  is line 1 (`Local model`, the longest label); line 2 is invariant. */
export const ENHANCE_TIMEOUT_TEXT_W = 80;

const ENHANCE_TIMEOUT_COLOR = "var(--color-gold)";

/** Visible line 1, locked by copy-review.md. `custom` is deliberately generic:
 *  the endpoint host is user-entered and is not HUD content. */
export const ENHANCEMENT_TIMEOUT_LABELS: Record<
  EnhancementTimeoutProvider,
  string
> = {
  gemini: "Gemini",
  openai: "OpenAI",
  anthropic: "Anthropic",
  ollama: "Ollama",
  local: "Local model",
  custom: "AI provider",
};

/** Visible line 2, invariant across every provider. Lowercase on purpose: it
 *  wraps with line 1 into one readable fragment (*Gemini / timed out*) rather
 *  than stacking as a second heading. */
export const ENHANCE_TIMEOUT_LINE_TWO = "timed out";

/** The full announcement, locked by copy-review.md. Follows the
 *  `[Condition]. [What happened to the text].` pattern so the HUD's
 *  announced strings stay one family. `custom` takes an article here and only
 *  here: the visible column has no room for one, but the sentence needs it. */
export const ENHANCEMENT_TIMEOUT_ANNOUNCEMENTS: Record<
  EnhancementTimeoutProvider,
  string
> = {
  gemini: "Gemini timed out. Your text was pasted without enhancement.",
  openai: "OpenAI timed out. Your text was pasted without enhancement.",
  anthropic: "Anthropic timed out. Your text was pasted without enhancement.",
  ollama: "Ollama timed out. Your text was pasted without enhancement.",
  local: "Local model timed out. Your text was pasted without enhancement.",
  custom:
    "The AI provider timed out. Your text was pasted without enhancement.",
};

export function EnhancementTimeoutIndicator({
  provider,
}: {
  provider: EnhancementTimeoutProvider;
}) {
  const reducedMotion = useReducedMotion() ?? false;

  return (
    <motion.div
      initial={{ opacity: 0, y: 2 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -2 }}
      transition={{ duration: 0.18, ease: "easeOut" }}
      className="flex items-center justify-center"
      style={{ gap: 8 }}
      // Polite, not assertive: the text already landed, so nothing should
      // interrupt what the user is doing. `assertive` stays reserved for
      // BlockedIndicator, where dictation is actually refused.
      role="status"
      aria-live="polite"
      aria-label={ENHANCEMENT_TIMEOUT_ANNOUNCEMENTS[provider]}
      data-testid="hud-enhance-timeout"
    >
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
        {/* Clock face — fades with the rest */}
        <circle
          cx="6"
          cy="6"
          r="4.4"
          stroke={ENHANCE_TIMEOUT_COLOR}
          strokeWidth="1.2"
        />
        {/* Hands at 12 and 4 o'clock. They take the same delayed beat the lock
            shackle uses; a reduced-motion run mounts them in place. */}
        <motion.path
          d="M6 3.2V6l1.9 1.1"
          stroke={ENHANCE_TIMEOUT_COLOR}
          strokeWidth="1.2"
          strokeLinecap="round"
          strokeLinejoin="round"
          fill="none"
          initial={reducedMotion ? false : { scale: 0.7, opacity: 0 }}
          animate={reducedMotion ? undefined : { scale: 1, opacity: 1 }}
          transition={{ type: "spring", stiffness: 600, damping: 26, delay: 0.18 }}
          style={{ transformOrigin: "6px 6px" }}
          data-testid="hud-enhance-timeout-hands"
        />
      </svg>
      <div
        className="flex flex-col"
        style={{ width: ENHANCE_TIMEOUT_TEXT_W }}
        data-testid="hud-enhance-timeout-text"
      >
        <span
          style={{
            fontFamily: "Inter, system-ui, sans-serif",
            fontSize: 10,
            fontWeight: 500,
            color: "var(--color-slate)",
            lineHeight: "11px",
            whiteSpace: "nowrap",
          }}
        >
          {ENHANCEMENT_TIMEOUT_LABELS[provider]}
        </span>
        <span
          style={{
            fontFamily: "Inter, system-ui, sans-serif",
            fontSize: 9,
            fontWeight: 500,
            color: "var(--color-steel)",
            lineHeight: "11px",
            whiteSpace: "nowrap",
          }}
        >
          {ENHANCE_TIMEOUT_LINE_TWO}
        </span>
      </div>
    </motion.div>
  );
}

export const ENHANCE_ERROR_LINE_TWO = "returned an error";

export const ENHANCEMENT_ERROR_ANNOUNCEMENTS: Record<
  EnhancementTimeoutProvider,
  string
> = {
  gemini: "Gemini returned an error. Your text was pasted without enhancement.",
  openai: "OpenAI returned an error. Your text was pasted without enhancement.",
  anthropic: "Anthropic returned an error. Your text was pasted without enhancement.",
  ollama: "Ollama returned an error. Your text was pasted without enhancement.",
  local: "Local model returned an error. Your text was pasted without enhancement.",
  custom:
    "The AI provider returned an error. Your text was pasted without enhancement.",
};

export function EnhancementErrorIndicator({
  provider,
}: {
  provider: EnhancementTimeoutProvider;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 2 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -2 }}
      transition={{ duration: 0.18, ease: "easeOut" }}
      className="flex items-center justify-center"
      style={{ gap: 8 }}
      role="status"
      aria-live="polite"
      aria-label={ENHANCEMENT_ERROR_ANNOUNCEMENTS[provider]}
      data-testid="hud-enhance-error"
    >
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
        <circle
          cx="6"
          cy="6"
          r="4.4"
          stroke="var(--color-rose)"
          strokeWidth="1.2"
        />
        <path
          d="M6 3.2V6.5M6 8.4h.01"
          stroke="var(--color-rose)"
          strokeWidth="1.25"
          strokeLinecap="round"
        />
      </svg>
      <div
        className="flex flex-col"
        style={{ width: ENHANCE_TIMEOUT_TEXT_W }}
        data-testid="hud-enhance-error-text"
      >
        <span
          style={{
            fontFamily: "Inter, system-ui, sans-serif",
            fontSize: 10,
            fontWeight: 500,
            color: "var(--color-slate)",
            lineHeight: "11px",
            whiteSpace: "nowrap",
          }}
        >
          {ENHANCEMENT_TIMEOUT_LABELS[provider]}
        </span>
        <span
          style={{
            fontFamily: "Inter, system-ui, sans-serif",
            fontSize: 9,
            fontWeight: 500,
            color: "var(--color-steel)",
            lineHeight: "11px",
            whiteSpace: "nowrap",
          }}
        >
          {ENHANCE_ERROR_LINE_TWO}
        </span>
      </div>
    </motion.div>
  );
}
