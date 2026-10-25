import { useReducedMotion, motion } from "framer-motion";
import { useState, type ReactNode } from "react";
import { VoiceBars } from "../../../components/Waveform";
import {
  RECORDING_CONTROL_ACTIVE_W,
  PILL_HEIGHT,
  RECORDING_CONTROL_LAUNCHER_W,
  RECORDING_CONTROL_SEED_W,
  RECORDING_CONTROL_SEED_H,
} from "../geometry";

export function ProcessingSpinner() {
  const reducedMotion = useReducedMotion();
  return (
    <motion.svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 14 14"
      fill="none"
      animate={
        reducedMotion
          ? { opacity: [0.45, 1, 0.45] }
          : { rotate: 360 }
      }
      transition={
        reducedMotion
          ? { duration: 1.2, repeat: Infinity, ease: "easeInOut" }
          : { duration: 0.9, repeat: Infinity, ease: "linear" }
      }
      data-testid="dictation-processing-spinner"
    >
      <path
        d="M7 1.5a5.5 5.5 0 015.5 5.5"
        stroke="var(--frost-muted)"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
      <path
        d="M12.5 7A5.5 5.5 0 117 1.5"
        stroke="var(--color-steel)"
        strokeOpacity="0.28"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </motion.svg>
  );
}

function MicrophoneGlyph() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {/* Capsule. rx = w/2 makes it a true capsule. Centered on x=9. */}
      <rect x="6.5" y="2.45" width="5" height="8" rx="2.5" />
      {/* Cradle. Elliptical (rx 4.9 / ry 3.55) so it can clear the capsule
          horizontally without becoming a deep bowl. The 0.75 stubs give the
          arms a termination above the arc's widest point. */}
      <path d="M4.1 8.6v0.75a4.9 3.55 0 0 0 9.8 0V8.6" />
      {/* Neck starts exactly at the cradle's bottom point (9, 12.90). */}
      <path d="M9 12.9v3.05" />
      {/* Base is wider than the capsule (outer half 3.55 vs 3.25) so the
          mark reads planted rather than top-heavy. */}
      <path d="M6.2 15.95h5.6" />
    </svg>
  );
}

/**
 * The vessel contents between clicking the microphone and the backend
 * confirming an authoritative recording. Deliberately non-committal: the
 * microphone means "about to listen", so there are no `VoiceBars` (no audio
 * yet) and no cancel/finish discs (no session to discard or commit). The glyph
 * begins as the clicked microphone, then exits promptly while the vessel's
 * width spring remains as pending feedback. Reduced motion makes that exit
 * near-immediate.
 */
export function ArmingIndicator() {
  const reducedMotion = useReducedMotion() ?? false;
  return (
    <div
      className="flex h-full w-full items-center justify-center"
      data-testid="hud-arming-indicator"
    >
      <motion.span
        className="flex h-8 w-8 items-center justify-center"
        data-testid="hud-arming-glyph"
        initial={{ opacity: 1, scale: 1 }}
        animate={{ opacity: 0, scale: 0.86 }}
        transition={{
          duration: reducedMotion ? 0.01 : 0.1,
          ease: "easeOut",
        }}
        style={{ color: "var(--color-steel)" }}
      >
        <MicrophoneGlyph />
      </motion.span>
    </div>
  );
}

function OutcomeGlyph({
  kind,
  size = 14,
}: {
  kind: "cancel" | "finish";
  size?: number;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {kind === "cancel" ? (
        // Symmetric about (7,7). Arm span 5.8/axis -> 9.80 units of
        // dominant-stroke extent, matched to the check's 8.89.
        <path d="M4.1 4.1 9.9 9.9M9.9 4.1 4.1 9.9" />
      ) : (
        // Short arm 45.0deg / 3.75 long; long arm 48.9deg / 7.29 long.
        // Long arm is steeper than the short arm -> reads as a check, not a V.
        // Shifted 0.40 up and 0.15 left from bbox-centered so the
        // length-weighted centroid lands at (7.01, 7.18). Do NOT re-center
        // this on its bounding box.
        <path d="M3.05 6.95 5.7 9.6 10.85 3.7" />
      )}
    </svg>
  );
}

function HudOutcomeButton({
  kind,
  compact = false,
  disabled,
  onClick,
}: {
  kind: "cancel" | "finish";
  compact?: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  const reducedMotion = useReducedMotion() ?? false;
  const [hovered, setHovered] = useState(false);
  const [pressed, setPressed] = useState(false);
  const label = kind === "cancel" ? "Cancel dictation" : "Finish and type";
  // The pill and Live Preview footer both use a 24px target. The footer has
  // room for the full target now, so compact no longer means a harder-to-hit
  // 20px outcome button; it only documents the denser host surface.
  const size = 24;
  const glyphSize = 12;
  const active = !disabled && (hovered || pressed);

  // Framer cannot interpolate var() or color-mix(), so every color/filter
  // state is a CSS transition on inline styles. transform is deliberately
  // excluded from the transition list so it does not fight whileTap.
  const transition = reducedMotion
    ? "background-color 0.01s linear, border-color 0.01s linear, color 0.01s linear, filter 0.01s linear"
    : "background-color 140ms ease, border-color 140ms ease, color 140ms ease, filter 140ms ease";

  const cancelStyle = {
    color: active ? "var(--color-rose)" : "var(--color-steel)",
    background:
      pressed && !disabled
        ? "color-mix(in srgb, var(--color-rose) 22%, var(--surface-control))"
        : hovered && !disabled
          ? "color-mix(in srgb, var(--color-rose) 15%, var(--surface-control))"
          : "var(--surface-control)",
    border: `1px solid ${
      pressed && !disabled
        ? "color-mix(in srgb, var(--color-rose) 48%, transparent)"
        : hovered && !disabled
          ? "color-mix(in srgb, var(--color-rose) 38%, transparent)"
          : "color-mix(in srgb, var(--color-steel) 22%, transparent)"
    }`,
    boxShadow: "inset 0 1px 0 rgba(255,255,255,0.35)",
  } as const;

  const finishStyle = {
    color: "white",
    background: "var(--accent-fill)",
    border: `1px solid ${
      active
        ? "color-mix(in srgb, var(--color-frost) 55%, transparent)"
        : "var(--border-accent)"
    }`,
    // Top specular + bottom shade = domed disc. Inner light, not a drop
    // shadow: the system does not use drop shadows for elevation.
    boxShadow:
      "inset 0 1px 0 rgba(255,255,255,0.30), inset 0 -1px 0 rgba(0,0,0,0.10)",
    filter: disabled
      ? "none"
      : pressed
        ? "brightness(0.94)"
        : hovered
          ? "brightness(1.08)"
          : "none",
  } as const;

  return (
    <motion.button
      type="button"
      aria-label={label}
      tabIndex={-1}
      disabled={disabled}
      data-hud-interactive="true"
      data-compact={compact ? "true" : undefined}
      onPointerEnter={() => setHovered(true)}
      onPointerLeave={() => {
        setHovered(false);
        setPressed(false);
      }}
      onPointerDown={(event) => {
        event.stopPropagation();
        if (!disabled) setPressed(true);
      }}
      onPointerUp={() => setPressed(false)}
      onPointerCancel={() => setPressed(false)}
      onClick={onClick}
      whileTap={reducedMotion || disabled ? undefined : { scale: 0.96 }}
      className="flex shrink-0 cursor-pointer items-center justify-center rounded-full disabled:cursor-default disabled:opacity-45"
      style={{
        width: size,
        height: size,
        transition,
        ...(kind === "finish" ? finishStyle : cancelStyle),
      }}
    >
      <OutcomeGlyph kind={kind} size={glyphSize} />
    </motion.button>
  );
}

export function HudRecordingControls({
  rms,
  peak,
  barColor,
  disabled,
  compact = false,
  onCancel,
  onFinish,
  testId = "hud-recording-controls",
}: {
  rms: number;
  peak: number;
  barColor: string;
  disabled: boolean;
  compact?: boolean;
  onCancel: () => void;
  onFinish: () => void;
  testId?: string;
}) {
  return (
    <div
      className="flex w-full items-center justify-between px-1"
      data-testid={testId}
    >
      <HudOutcomeButton
        kind="cancel"
        compact={compact}
        disabled={disabled}
        onClick={onCancel}
      />
      <VoiceBars
        state="recording"
        rms={rms}
        peak={peak}
        color={barColor}
      />
      <HudOutcomeButton
        kind="finish"
        compact={compact}
        disabled={disabled}
        onClick={onFinish}
      />
    </div>
  );
}

export function PersistentRecordingControl({
  expanded,
  active = false,
  activeContent = null,
  parked = false,
  returningFromActive,
  desktopBorder,
  desktopHighlight,
  connected,
  pending,
  onStart,
}: {
  expanded: boolean;
  active?: boolean;
  activeContent?: ReactNode;
  parked?: boolean;
  returningFromActive: boolean;
  desktopBorder: string;
  desktopHighlight: string;
  connected: boolean;
  pending: boolean;
  onStart: () => void;
}) {
  const reducedMotion = useReducedMotion() ?? false;
  const transition = reducedMotion
    ? { duration: 0.01 }
    : returningFromActive
      ? { type: "spring" as const, damping: 24, stiffness: 260 }
      : active
        ? { type: "spring" as const, damping: 20, stiffness: 300 }
        : { type: "spring" as const, damping: 26, stiffness: 420 };
  const [micHovered, setMicHovered] = useState(false);
  const micEnabled = connected && !pending;
  const micActive = micEnabled && micHovered;

  return (
    <motion.div
      className="relative rounded-full flex items-center justify-center"
      initial={
        returningFromActive
          ? {
              width: RECORDING_CONTROL_ACTIVE_W,
              height: PILL_HEIGHT,
              opacity: 1,
            }
          : active
            ? {
                width: RECORDING_CONTROL_LAUNCHER_W,
                height: PILL_HEIGHT,
                opacity: 1,
              }
          : false
      }
      animate={{
        width: active
          ? RECORDING_CONTROL_ACTIVE_W
          : expanded
            ? RECORDING_CONTROL_LAUNCHER_W
            : RECORDING_CONTROL_SEED_W,
        height:
          active || expanded ? PILL_HEIGHT : RECORDING_CONTROL_SEED_H,
        opacity: 1,
      }}
      transition={parked ? { duration: 0 } : transition}
      data-testid="persistent-recording-vessel"
      data-parked={parked ? "true" : "false"}
      data-returning-from-active={
        returningFromActive ? "true" : "false"
      }
      style={{
        background: "var(--surface-overlay)",
        border: `1px solid ${desktopBorder}`,
        boxShadow: desktopHighlight,
      }}
    >
      {active ? (
        activeContent
      ) : expanded ? (
        <div
          className="flex h-full w-full items-center justify-center"
          data-testid="recording-control-launcher"
        >
          <motion.button
            type="button"
            aria-label="Start dictation"
            tabIndex={-1}
            disabled={!connected || pending}
            data-hud-interactive="true"
            onPointerEnter={() => {
              setMicHovered(true);
            }}
            onPointerLeave={() => {
              setMicHovered(false);
            }}
            onPointerDown={(event) => {
              event.stopPropagation();
            }}
            onClick={onStart}
            whileTap={
              reducedMotion || !connected || pending
                ? undefined
                : { scale: 0.96 }
            }
            // 32px hit target (locked). The microphone is glyph-only so the
            // persistent vessel remains the launcher's sole painted surface.
            className="relative flex h-8 w-8 cursor-pointer items-center justify-center rounded-full disabled:cursor-default disabled:opacity-40"
            style={{
              background: "transparent",
              color: micActive ? "var(--color-slate)" : "var(--color-steel)",
              transition: reducedMotion
                ? "color 0.01s linear"
                : "color 140ms ease",
            }}
          >
            <MicrophoneGlyph />
          </motion.button>
        </div>
      ) : (
        <span
          className="block h-full w-full rounded-full"
          data-testid="recording-control-seed"
          aria-hidden="true"
        />
      )}
    </motion.div>
  );
}
