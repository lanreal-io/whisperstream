import {
  CLAUDE_PILL_MIN_W,
  CLAUDE_PILL_MAX_W,
  type MeasuredClaudeCard,
  CLAUDE_RAIL_PILL_W,
  CLAUDE_RAIL_PILL_RADIUS,
  CLAUDE_PILL_RADIUS,
  GLANCE_SPRING,
} from "../geometry";
import { type AgentName } from "../../../lib/socket/agentEvents";
import { type ReactNode, useState, useLayoutEffect } from "react";
import { type ReplyRailPhase } from "../presentation";
import {
  type ReplyRailProps,
  REPLY_RAIL_CARD_TEXT_MAX_H,
  ReplyRail,
} from "../components/ReplyRail";
import { useReducedMotion, motion, AnimatePresence } from "framer-motion";
import { useMeasuredClaudeCard } from "../components/useMeasuredHudCard";
import { ClaudeIdentityRow } from "../components/AgentIdentity";

/** Where the identity row's session name starts (12px inset, 28px mark, 6px
 *  gap). Reply text lines up under the name, not under the mark. */
const PILL_NAME_X = 12 + 28 + 6;

// --- CompactClaudePill (Task 2.1, design.md states 1-6, scope decisions 1, 4,
// 6b) ---
// Measures the compact pill's natural content width (mark + the wider of the
// session name and the hint under it + the trailing slot), clamped to the
// painted-footprint bounds (200 to 520). Content-fit rather than CSS auto-width, so the single spring
// geometry animation (decision 30) has a concrete numeric target.
export function measureCompactPillWidth(
  sourceName: string,
  hintWidth = 0,
): number {
  if (typeof document === "undefined") return CLAUDE_PILL_MIN_W;
  const measurement = document.createElement("span");
  measurement.textContent = sourceName;
  Object.assign(measurement.style, {
    position: "fixed",
    left: "-100000px",
    top: "0",
    whiteSpace: "nowrap",
    fontFamily: "var(--font-agent)",
    fontSize: "13px",
    fontWeight: "500",
  });
  document.body.appendChild(measurement);
  const textWidth = Math.max(
    Math.ceil(measurement.getBoundingClientRect().width),
    hintWidth,
  );
  measurement.remove();
  // Reserve the trailing slot (queue count, stop button or reply countdown)
  // so speech changes do not resize the native region. Include the 28px
  // mark, padding, gaps and border. The hint sits under the name, so it can
  // only widen the text column.
  const natural = 12 + 28 + 8 + textWidth + 8 + 48 + 14 + 2;
  const clamped = Math.ceil(
    Math.min(CLAUDE_PILL_MAX_W, Math.max(CLAUDE_PILL_MIN_W, natural)),
  );
  // Snap to an even width. This is the only HUD surface whose width is derived
  // from text rather than a fixed constant, so it was the only one that could
  // land on an odd number and centre on a half pixel inside the fixed
  // PANEL_WIDTH WebView, blurring its own painted curve and forcing the native
  // region to round. The recording pill and Live Preview card have been even
  // by construction all along; this makes the Claude pill behave the same way.
  return Math.min(CLAUDE_PILL_MAX_W, clamped + (clamped % 2));
}

/** The default Claude HUD presentation (design.md states 1-6). Rests as the
 *  mark-plus-session-name pill (48px, identity only, decision 19) and grows a
 *  second row beneath it only when there is live speech, a draft, a sent
 *  confirmation, or a permission ask (owner resolution 3 / scope decision
 *  6b). Replaces LegacyResponsePanel: there is no quoted response body here
 *  any more, speech carries the content (scope decision 1). Width and radius
 *  change together in one spring (GLANCE_SPRING) so a rail opening is a single native
 *  geometry event (decision 30, wired to the real window in Task 3.1/3.2). */
export function CompactClaudePill({
  provider = "claude",
  sourceName,
  railPhase,
  railProps,
  accessibleLabel,
  title,
  speaking,
  remainingCount,
  restingHint,
  restingHintWidth = 0,
  restingTime = null,
  onRestingClick,
  onMeasuredCardSizeChange,
}: {
  provider?: AgentName;
  sourceName: string;
  speaking: boolean;
  remainingCount: number;
  /** The faint "Hold X to reply" line shown in the identity row while the
   *  pill can take a reply and nothing is in its reply row yet. */
  restingHint?: ReactNode;
  /** The hint's measured width, so the content-fit pill never truncates it. */
  restingHintWidth?: number;
  /** The reply window's countdown ("0:06"), shown in the trailing slot at
   *  rest. Stays after the hint retires. */
  restingTime?: string | null;
  /** Clicking the resting pill opens typing (the hint's "to reply"). */
  onRestingClick?: () => void;
  railPhase: Exclude<ReplyRailPhase, "resting"> | null;
  railProps: Omit<ReplyRailProps, "phase">;
  accessibleLabel: string;
  title?: string;
  /** Reports the painted-pill rect into the HUD's one `measuredCardSize`
   *  state, which shapes this mode's native region (Task 3.2). */
  onMeasuredCardSizeChange?: (measured: MeasuredClaudeCard) => void;
}) {
  const reducedMotion = useReducedMotion();
  const [compactWidth, setCompactWidth] = useState(CLAUDE_PILL_MIN_W);

  // Keyed on whether a hint shows, not on the hint element: that is a new
  // node every render, which re-measured the DOM on every render.
  const hasRestingHint = restingHint !== undefined;
  useLayoutEffect(() => {
    let active = true;
    const measure = () => {
      if (active) {
        setCompactWidth(
          measureCompactPillWidth(
            sourceName,
            hasRestingHint ? restingHintWidth : 0,
          ),
        );
      }
    };

    measure();
    void document.fonts?.ready.then(measure, () => undefined);
    return () => {
      active = false;
    };
  }, [sourceName, hasRestingHint, restingHintWidth]);

  const railOpen = railPhase !== null;
  const width = railOpen ? CLAUDE_RAIL_PILL_W : compactWidth;
  const radius = railOpen ? CLAUDE_RAIL_PILL_RADIUS : CLAUDE_PILL_RADIUS;
  const { cardRef } = useMeasuredClaudeCard(
    "panel",
    onMeasuredCardSizeChange,
    radius,
  );

  return (
    <motion.div
      ref={cardRef as React.RefObject<HTMLDivElement>}
      data-claude-card-measure="panel"
      data-testid="compact-claude-pill"
      initial={false}
      animate={{ width, borderRadius: radius }}
      // Critically damped like the reply row's height: PILL_SPRING overshot
      // the open pill's width by about 11%.
      transition={reducedMotion ? { duration: 0 } : GLANCE_SPRING}
      // No rounded `overflow-hidden` on the pill itself. A rounded clip on a
      // motion-composited layer is rasterized without antialiasing, so the
      // capsule's two curved ends came back stair-stepped while the recording
      // pill (plain `rounded-full`, no clip) stayed smooth. The rail below owns
      // its own square-edged height clip and is inset 14px, well clear of the
      // 18-20px corner curve, so nothing needs the outer mask.
      className="relative flex flex-col"
      role="status"
      aria-atomic="true"
      aria-label={accessibleLabel}
      title={title}
      onClick={railPhase === null ? onRestingClick : undefined}
      // Not `data-hud-interactive`: the resting pill stays draggable. The HUD
      // root clicks it on a release that did not move (pointer capture keeps
      // the browser's own click on the root).
      data-hud-resting-click={railPhase === null && onRestingClick ? "true" : undefined}
      style={{
        cursor: railPhase === null && onRestingClick ? "text" : undefined,
        background: "var(--surface-overlay)",
        border: "1px solid var(--hud-desktop-border)",
        boxShadow: "var(--hud-card-elevation)",
      }}
    >
      <ClaudeIdentityRow
        variant="compact"
        provider={provider}
        sourceName={sourceName}
        speaking={speaking}
        remainingCount={remainingCount}
        compactHint={railPhase === null ? restingHint : undefined}
        compactTime={railPhase === null ? restingTime : null}
      />
      <AnimatePresence>
        {railOpen && (
          <motion.div
            // The row opens and closes with its height, critically damped, so
            // a press grows the pill instead of snapping it taller. Its content
            // keeps the open pill's width (the pill's own width springs at the
            // same time), so the "auto" target is measured at the final wrap;
            // this box's square clip hides the overflow while it catches up.
            initial={reducedMotion ? false : { opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={reducedMotion ? { opacity: 0 } : { opacity: 0, height: 0 }}
            transition={reducedMotion ? { duration: 0 } : GLANCE_SPRING}
            style={{ flexShrink: 0, overflow: "hidden" }}
          >
            <div style={{ width: CLAUDE_RAIL_PILL_W - 2 }}>
              <div
                aria-hidden="true"
                style={{ height: 1, margin: "0 14px", background: "var(--divider)" }}
              />
              <div style={{ padding: `6px 14px 10px ${PILL_NAME_X}px` }}>
                <ReplyRail
                  phase={railPhase as ReplyRailPhase}
                  // Five lines, then it scrolls: the pill at its tallest
                  // still fits its fixed window, so typing never resizes it.
                  textMaxH={REPLY_RAIL_CARD_TEXT_MAX_H}
                  {...railProps}
                />
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
