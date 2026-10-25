import { useRef, useState, type ReactNode } from "react";

/**
 * HoverTooltip — the app's on-brand white hover bubble.
 *
 * Extracted verbatim from the Home tab's model-info "i" tooltip so every hover
 * affordance shares one frost-glass treatment (the overlay-rung surface
 * `--surface-overlay` + `--border-emphasis`, blur, 11px slate text) instead of
 * the OS-native black `title=` tooltip. Pure CSS
 * group-hover (no JS state) for the explicit-side modes, so it composes inside
 * animated/AnimatePresence subtrees without interfering with them: the trigger
 * lives in `children`, the bubble is a sibling positioned off the shared
 * `group` wrapper.
 *
 * `side="auto"` adds viewport-edge awareness (opt-in): on hover it measures the
 * trigger position + the bubble size and flips BOTH anchors to whichever side
 * keeps the bubble on-screen — horizontal (expand left vs right) and vertical
 * (open down vs up) — the same edge-clamp idea as `Dropdown`. Use it for a
 * trigger near a window edge (a top-right badge, or a low row whose bubble
 * would otherwise clip off the bottom) so the bubble never clips. The explicit
 * `bottom-right`/`bottom-left` modes are unchanged (pure CSS, open down).
 *
 * Renders just the trigger (no bubble) when `content` is null/empty, so a
 * stateful caller can pass `tooltipFor(state)` and get nothing for states that
 * have no tooltip.
 */
interface HoverTooltipProps {
  /** Bubble contents. When null/undefined/"" only the trigger renders. */
  content: ReactNode;
  /** The hover target. */
  children: ReactNode;
  /** Which edge the bubble anchors to under the trigger. Default bottom-right
   *  (bubble expands left + down) so a top-right trigger never clips. `auto`
   *  measures on hover and flips to keep the bubble inside the viewport.
   *  `top` centers the bubble above the trigger (a centered button inside a
   *  scrolling dialog, where opening down would clip). */
  side?: "bottom-right" | "bottom-left" | "top-right" | "top" | "auto";
  /** Tailwind width class for the bubble. Default `w-56`. */
  width?: string;
  /** Extra classes on the wrapper (e.g. layout tweaks at the call site). */
  className?: string;
  /** Stable ID for callers that connect the bubble with aria-describedby. */
  bubbleId?: string;
  /** Opt-in visibility class for a caller-owned focus group. Existing hover
   *  behavior remains active alongside it. */
  focusVisibleClassName?: string;
}

const EDGE_MARGIN = 8; // px breathing room from the viewport edge (matches Dropdown)

export function HoverTooltip({
  content,
  children,
  side = "bottom-right",
  width = "w-56",
  className = "",
  bubbleId,
  focusVisibleClassName = "",
}: HoverTooltipProps) {
  const hasContent = content !== null && content !== undefined && content !== "";

  const wrapperRef = useRef<HTMLSpanElement>(null);
  const bubbleRef = useRef<HTMLSpanElement>(null);
  // For side="auto", the anchor (horizontal + vertical) resolved on hover.
  // Default to expand-left / open-down so a top-right trigger is safe before
  // the first measure.
  const [autoAnchor, setAutoAnchor] = useState<{
    h: "left" | "right";
    v: "down" | "up";
  }>({ h: "right", v: "down" });

  // side="auto": measure on pointer-enter and pick the anchor with room on BOTH
  // axes. Horizontal: a left-0 anchor expands rightward; if that runs past the
  // right viewport edge, anchor right-0 (expand left). Vertical: open below the
  // trigger by default; if the bubble would run past the bottom edge, open
  // upward instead. This is the up/down/left/right edge-awareness.
  const handleEnter = () => {
    if (side !== "auto") return;
    const wrap = wrapperRef.current;
    const bubble = bubbleRef.current;
    if (!wrap || !bubble) return;
    const rect = wrap.getBoundingClientRect();
    const bubbleW = bubble.offsetWidth;
    const bubbleH = bubble.offsetHeight;
    const overflowsRight = rect.left + bubbleW + EDGE_MARGIN > window.innerWidth;
    // 8px = the mt-2/mb-2 gap between the trigger and the bubble.
    const overflowsBottom =
      rect.bottom + 8 + bubbleH + EDGE_MARGIN > window.innerHeight;
    setAutoAnchor({
      h: overflowsRight ? "right" : "left",
      v: overflowsBottom ? "up" : "down",
    });
  };

  // Explicit bottom-left/bottom-right keep their pure-CSS behavior (open down);
  // auto uses the measured anchor.
  const anchor =
    side === "auto"
      ? autoAnchor
      : { h: side === "bottom-left" ? "left" : "right", v: side === "top-right" ? "up" : "down" };
  const hClass =
    side === "top"
      ? "left-1/2 -translate-x-1/2"
      : anchor.h === "right"
        ? "right-0"
        : "left-0";
  const vClass =
    side === "top" || anchor.v === "up" ? "bottom-full mb-2" : "top-full mt-2";

  return (
    <span
      ref={wrapperRef}
      onMouseEnter={handleEnter}
      onFocus={handleEnter}
      className={`relative group inline-flex items-center ${className}`}
    >
      {children}
      {hasContent && (
        <span
          ref={bubbleRef}
          id={bubbleId}
          role="tooltip"
          className={`pointer-events-none absolute ${vClass} ${hClass} ${width} rounded-xl px-3 py-2 text-[11px] leading-relaxed text-slate opacity-0 group-hover:opacity-100 transition-opacity duration-150 z-30 ${focusVisibleClassName}`}
          style={{
            // Overlay-rung surface (near-solid per theme) so the theme-resolved
            // slate text stays readable in dark; a literal white/95 here was
            // near-white-on-near-white in dark mode.
            background: "var(--surface-overlay)",
            border: "1px solid var(--border-emphasis)",
            backdropFilter: "blur(12px)",
            boxShadow: "var(--shadow-frost-glow)",
          }}
        >
          {content}
        </span>
      )}
    </span>
  );
}
