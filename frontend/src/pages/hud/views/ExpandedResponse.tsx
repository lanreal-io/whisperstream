import { type HudSession, type AgentName } from "../../../lib/socket/agentEvents";
import { agentProviderLabel } from "../../../lib/sessionName";
import { useReducedMotion, AnimatePresence, motion } from "framer-motion";
import {
  useRef,
  useState,
  useEffect,
  useCallback,
  useContext,
  useLayoutEffect,
  type CSSProperties,
} from "react";
import { AgentMark, ClaudeIdentityRow, Spine, queueDepthCopy } from "../components/AgentIdentity";
import { type ReplyRailPhase } from "../presentation";
import {
  type ReplyRailProps,
  REPLY_RAIL_CARD_MAX_H,
  REPLY_RAIL_CARD_TEXT_MAX_H,
  ReplyRail,
} from "../components/ReplyRail";
import {
  type MeasuredClaudeCard,
  claudeCardCeiling,
  RESPONSE_MAX_H,
  CLAUDE_PILL_MIN_W,
  CLAUDE_PILL_H,
  CLAUDE_PILL_RADIUS,
  CLAUDE_CARD_RADIUS,
  CONSOLE_PAD_X,
  CONSOLE_PAD_Y,
  GLANCE_CARD_W,
  GLANCE_SPRING,
} from "../geometry";
import { ConsoleResizeFrame } from "../components/ConsoleResizeFrame";
import { HudExpandToggle } from "../components/HudExpandToggle";
import { type ConsoleResizeHandlers } from "../useConsoleResize";
import {
  type AgentReplyStatus,
  AgentConversationContext,
  AgentTimeline,
} from "../../../components/agentSpeak/AgentTimeline";
import { useMeasuredClaudeCard } from "../components/useMeasuredHudCard";
import { MarkdownText } from "../components/MarkdownText";
import { hudMetaText } from "../metaText";
import {
  CLAUDE_CARD_GAP_RAIL_FOOTER,
  CLAUDE_CARD_PAD_TOP,
  CLAUDE_CARD_GAP_IDENTITY_BODY,
  CLAUDE_CARD_GAP_BODY_DIVIDER,
  CLAUDE_CARD_DIVIDER_H,
  CLAUDE_CARD_GAP_DIVIDER_RAIL,
  CLAUDE_CARD_GAP_BODY_RAIL,
  CLAUDE_CARD_PAD_BOTTOM,
  CLAUDE_WELL_MIN_H,
  CLAUDE_CARD_PAD_X,
  CLAUDE_CARD_CONTENT_DELAY_S,
  CLAUDE_BODY_GUTTER,
  CLAUDE_WELL_FADE_H,
  CLAUDE_WELL_TOP_FADE_H,
  CLAUDE_TIMELINE_SCROLL_GAP,
} from "../responseLayout";

/** The glance card's content width: the card less its padding and border. */
const GLANCE_CONTENT_W = GLANCE_CARD_W - CLAUDE_CARD_PAD_X * 2 - 2;

// The speaker emoji, plus the two codepage-mangled renderings of its UTF-8
// bytes. An installed plugin copy older than the UTF-8 stdin fix can still
// deliver a mangled marker, so matching stays tolerant here as it does in the
// backend extractor.
const MARKER_PREFIXES = [
  "\u{1F50A}",
  "≡ƒöè",
  "ðŸ”Š",
];

/** Drop Claude's authored spoken-summary line from a response body. */

/** How long a card waits after a window blur before treating it as the user
 *  leaving (activation blurs and refocuses the WebView within a few ms). */
export const CARD_BLUR_SETTLE_MS = 150;

export function stripSpokenSummaryLine(text: string): string {
  const lines = text.split(/\r?\n/);
  while (lines.length > 0 && lines[lines.length - 1].trim() === "") lines.pop();
  const last = lines[lines.length - 1]?.trim() ?? "";
  if (MARKER_PREFIXES.some((prefix) => last.startsWith(prefix))) lines.pop();
  return lines.join("\n").trimEnd();
}

export function formatReplyTime(seconds: number): string {
  const safeSeconds = Math.max(0, Math.ceil(seconds));
  const minutes = Math.floor(safeSeconds / 60);
  return `${minutes}:${String(safeSeconds % 60).padStart(2, "0")}`;
}

/** The one queue-depth string, used byte-identically at the expanded
 *  footer's right slot and both accessible-label builders (copy-review
 *  string 1, locked contract). "in queue" makes the queue a shared
 *  container rather than a property of the named session, which is why
 *  `<n> more` (attributing the count to whichever session is currently
 *  named) was wrong: the FIFO is cross-session. No plural branch needed:
 *  "1 in queue" is correct as-is. */

// --- The session tab strip (Task 4.3, design.md section A/C/D) ---
// Lives in the identity header, so parallel sessions consume horizontal room
// beside the provider title without adding a second chrome row.
export const SESSION_TAB_H = 22;
const SESSION_TAB_RADIUS = 11;
const SESSION_TAB_GAP = 6;
const SESSION_TAB_STATUS_SLOT = 8; // reserved, painted or not (decision 5)
const SESSION_TAB_GLYPH_GAP = 6;
const SESSION_TAB_PAD_L = 7;
const SESSION_TAB_PAD_R = 9;
const SESSION_TAB_MARK_W = 9; // provider mark, mixed-provider strips only
const SESSION_TAB_BADGE_W = 6; // one digit at 9px
const SESSION_TAB_EDGE_FADE_W = 14; // horizontal sibling of the well fade
/** About three letters of a 10.5px name plus its ellipsis. Names often share
 *  a first letter, and the number badge already identifies the tab, so there
 *  is no single-letter collapse (owner decision 2026-10-23). */
export const SESSION_TAB_NAME_MIN_W = 30;
/** Names this short already fit the floor whole. */
const SESSION_TAB_SHORT_NAME_CHARS = 4;

/** The floor an inactive tab shrinks to when the strip is crowded: the
 *  status slot, the number badge and about three letters. */
export function sessionTabMinWidth(mixedProviders: boolean): number {
  return (
    SESSION_TAB_PAD_L +
    SESSION_TAB_STATUS_SLOT +
    SESSION_TAB_GLYPH_GAP +
    (mixedProviders ? SESSION_TAB_MARK_W + SESSION_TAB_GLYPH_GAP : 0) +
    SESSION_TAB_NAME_MIN_W +
    SESSION_TAB_GLYPH_GAP +
    SESSION_TAB_BADGE_W +
    SESSION_TAB_PAD_R
  );
}

/** Tabs share the existing identity row and therefore cost the body no
 * vertical chrome. Exported to keep the geometry contract explicit in tests. */
export const SESSION_TAB_CHROME_COST = 0;

/** The active capsule's background slide, distinct from the card's own
 *  PILL_SPRING: design decision 6 calls for the app's nav-indicator idiom
 *  (400/30), used elsewhere for sliding active-state indicators. */
const SESSION_TAB_ACTIVE_SPRING = {
  type: "spring" as const,
  stiffness: 400,
  damping: 30,
};

/** Locked copy (copy-review.md "HUD, tab accessible names"). No state word is
 *  shared across two states (needs-input vs cannot-reply both being called
 *  "blocked" internally), matching the accessibility note in copy-review.md. */
/** A session takes a reply now, delivered at once or held for its next Stop.
 *  Older backends only report `can_reply`. */
function sessionTakesReply(session: HudSession): boolean {
  return session.accepts_reply ?? session.can_reply;
}

function sessionTabStateSentence(session: HudSession): string {
  if (session.needs_input) return "Waiting for your answer.";
  if (!sessionTakesReply(session)) return "Read only. Reply in your terminal.";
  if (session.unread) return "New response you have not read.";
  return "Nothing new in this session.";
}

/** Frame: `<provider>, <source name>. <state sentence>` plus
 *  `Current session.` when active (locked by copy-review.md). */
export function sessionTabAccessibleName(
  session: HudSession,
  active: boolean,
): string {
  const state = sessionTabStateSentence(session);
  const identity = `${agentProviderLabel(session.agent)}, ${session.source_name}.`;
  return active
    ? `${identity} ${state} Current session.`
    : `${identity} ${state}`;
}

/** The same bubble silhouette as `NeedsInputIndicator` (components/Waveform.tsx),
 *  redrawn at the strip's 8px slot instead of that indicator's 12px + label.
 *  The only saturated pixel the strip is allowed (design decisions 7, 8). */
function SessionTabQuestionGlyph() {
  return (
    <svg width="8" height="8" viewBox="0 0 8 8" fill="none" aria-hidden="true">
      <rect
        x="1.2"
        y="1.2"
        width="5.6"
        height="4"
        rx="1.3"
        stroke="var(--color-frost-deep)"
        strokeWidth="0.9"
      />
      <path
        d="M2.7 5.2 L2.7 6.8 L4.1 5.2"
        stroke="var(--color-frost-deep)"
        strokeWidth="0.9"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
      <circle cx="4" cy="3.1" r="0.5" fill="var(--color-frost-deep)" />
    </svg>
  );
}

/**
 * The reserved 8px status slot (decision 5): painted or not, so a state
 * change never reflows the strip. Priority: needs-input > cannot-reply >
 * unread, one glyph per slot (design.md section D). This HUD-private leaf
 * has no session state or window lifecycle authority.
 */
export function SessionTabStatusGlyph({ session }: { session: HudSession }) {
  const state = session.needs_input
    ? "needs"
    : !sessionTakesReply(session)
      ? "blocked"
      : session.unread
        ? "unread"
        : "none";
  return (
    <span
      aria-hidden="true"
      className="relative flex shrink-0 items-center justify-center"
      style={{
        width: SESSION_TAB_STATUS_SLOT,
        height: SESSION_TAB_STATUS_SLOT,
      }}
    >
      {state === "needs" && <SessionTabQuestionGlyph />}
      {state === "blocked" && (
        <svg width="7" height="7" viewBox="0 0 7 7" aria-hidden="true">
          <circle
            cx="3.5"
            cy="3.5"
            r="2.75"
            fill="none"
            strokeWidth="1"
            stroke="var(--color-steel)"
            opacity="0.55"
          />
        </svg>
      )}
      {state === "unread" && (
        <svg width="5" height="5" viewBox="0 0 5 5" aria-hidden="true">
          <circle cx="2.5" cy="2.5" r="2.5" fill="var(--color-steel)" />
        </svg>
      )}
    </span>
  );
}

/**
 * `SessionTabs` (Task 4.3, design.md section C). This HUD-private presentation
 * owns tab scrolling and edge fades; HUD.tsx retains authoritative selection.
 *
 * Layout (agent-speak-centered-console design.md, "Session tabs"):
 * 1. With room, every tab shows its full name.
 * 2. When crowded, inactive tabs shrink evenly (equal flex share, never past
 *    their own full width) down to the badge plus about three letters; the
 *    active tab keeps its full name.
 * 3. Past that floor the strip scrolls with fade edges, the active tab is
 *    scrolled into view, and hovering a shortened tab shows its full name.
 *
 * Renders nothing below two sessions (decision 3): a strip of one teaches
 * nothing and would regress the single-session card, which stays
 * byte-identical without a strip.
 */
export function SessionTabs({
  sessions,
  activeHandle,
  onSelect,
  locked,
  stripRef,
}: {
  sessions: HudSession[];
  activeHandle: string | null;
  onSelect: (handle: string) => void;
  /** Rail is non-resting: switching is refused (decision 11, wired in Task 4.7). */
  locked?: boolean;
  stripRef?: React.RefObject<HTMLDivElement | null>;
}) {
  const reduced = useReducedMotion();
  const internalRef = useRef<HTMLDivElement | null>(null);
  const ref = stripRef ?? internalRef;
  const tabRefs = useRef(new Map<string, HTMLButtonElement>());
  const mixedProviders = new Set(sessions.map((session) => session.agent)).size > 1;
  // Tabs slide only when the set of tabs or the active one changes. Measuring
  // on every render made them animate from where the card used to be when it
  // grew under them (a reply row opening on push-to-talk), and the strip's
  // scroll clip blanked them for a frame.
  const tabLayoutKey = `${activeHandle ?? ""}|${sessions.map((s) => s.session_handle).join(",")}`;
  const inactiveMinWidth = sessionTabMinWidth(mixedProviders);
  // Conditional edge fades (design.md "Density, overflow"): each side shows
  // only while content is hidden past it, so a strip that never scrolls
  // paints neither fade.
  const [edgeFade, setEdgeFade] = useState({ left: false, right: false });

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const recompute = () => {
      setEdgeFade({
        left: node.scrollLeft > 1,
        right: node.scrollLeft + node.clientWidth < node.scrollWidth - 1,
      });
    };
    recompute();
    node.addEventListener("scroll", recompute, { passive: true });
    if (typeof ResizeObserver === "undefined") {
      return () => node.removeEventListener("scroll", recompute);
    }
    const observer = new ResizeObserver(recompute);
    observer.observe(node);
    return () => {
      node.removeEventListener("scroll", recompute);
      observer.disconnect();
    };
    // Re-measure on session count change; scrollLeft/clientWidth reads on
    // `ref` don't need to be tracked as reactive deps.
  }, [sessions.length]);

  useEffect(() => {
    const selected = activeHandle === null ? null : tabRefs.current.get(activeHandle);
    selected?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [activeHandle, sessions.length]);

  if (sessions.length < 2) return null;
  return (
    <div className="relative min-w-0 flex-1" style={{ height: SESSION_TAB_H }}>
      <div
        ref={ref}
        role="tablist"
        aria-label="Coding agent sessions"
        data-testid="hud-session-tabs"
        className="relative flex h-full items-center overflow-x-auto"
        style={{
          gap: SESSION_TAB_GAP,
          scrollbarWidth: "none",
        }}
      >
      <AnimatePresence initial={false}>
      {sessions.map((session, index) => {
        const active = session.session_handle === activeHandle;
        return (
          <motion.button
            ref={(node) => {
              if (node) tabRefs.current.set(session.session_handle, node);
              else tabRefs.current.delete(session.session_handle);
            }}
            key={session.session_handle}
            layout={!reduced}
            layoutDependency={tabLayoutKey}
            // A capsule arriving fades in at the end while `layout` slides its
            // neighbours (400/30); one exiting fades out. Width belongs to the
            // flex layout below, so it is not animated directly. Neither moves
            // anyone else's selection.
            initial={reduced ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={
              reduced
                ? { duration: 0 }
                : SESSION_TAB_ACTIVE_SPRING
            }
            type="button"
            role="tab"
            aria-selected={active}
            aria-disabled={locked || undefined}
            aria-label={sessionTabAccessibleName(session, active)}
            tabIndex={-1}
            data-hud-interactive="true"
            onPointerDown={(e) => e.stopPropagation()}
            onClick={() => {
              if (!locked) onSelect(session.session_handle);
            }}
            // A shortened tab shows its full name on hover. Set only while
            // the name is actually cut, so a whole name is not announced
            // twice next to the tab's accessible name.
            onPointerEnter={(event) => {
              const name = event.currentTarget.querySelector<HTMLElement>(
                "[data-session-tab-name]",
              );
              event.currentTarget.title =
                name !== null && name.scrollWidth > name.clientWidth
                  ? session.source_name
                  : "";
            }}
            data-active={active ? "true" : "false"}
            className="relative flex cursor-pointer items-center overflow-hidden whitespace-nowrap"
            style={{
              height: SESSION_TAB_H,
              // The active tab keeps its whole name. Inactive tabs share the
              // remaining room equally, capped at their own full width and
              // floored at the badge plus about three letters.
              // A name already about that short keeps its own width as the
              // floor, so it is never padded past its text.
              ...(active
                ? { flex: "none" }
                : {
                    flex: "1 1 0px",
                    minWidth:
                      Array.from(session.source_name).length <=
                      SESSION_TAB_SHORT_NAME_CHARS
                        ? "max-content"
                        : inactiveMinWidth,
                    maxWidth: "max-content",
                  }),
              padding: `0 ${SESSION_TAB_PAD_R}px 0 ${SESSION_TAB_PAD_L}px`,
              gap: SESSION_TAB_GLYPH_GAP,
              borderRadius: SESSION_TAB_RADIUS,
            }}
          >
            {active && (
              // The app's nav-indicator idiom: ONE object that slides.
              <motion.span
                layoutId="hud-session-tab-active"
                aria-hidden="true"
                transition={reduced ? { duration: 0 } : SESSION_TAB_ACTIVE_SPRING}
                className="absolute inset-0"
                style={{
                  borderRadius: SESSION_TAB_RADIUS,
                  background: "var(--surface-control)",
                  border: "1px solid var(--border-hairline)",
                }}
              />
            )}
            <SessionTabStatusGlyph session={session} />
            {mixedProviders && (
              <span aria-hidden="true" className="relative flex shrink-0">
                <AgentMark provider={session.agent} size={9} />
              </span>
            )}
            <span
              data-session-tab-name="true"
              className="relative block min-w-0 truncate"
              style={{
                fontFamily: "var(--font-agent)",
                fontSize: 10.5,
                fontWeight: 500,
                color: "var(--color-slate)",
                opacity: sessionTakesReply(session)
                  ? active
                    ? 1
                    : 0.78
                  : 0.62,
              }}
            >
              {session.source_name}
            </span>
            {index < 9 && (
              <span
                aria-hidden="true"
                className="relative ml-auto shrink-0 text-right"
                style={{
                  fontFamily: "var(--font-agent)",
                  fontSize: 9,
                  fontWeight: 600,
                  color: "var(--color-steel)",
                  opacity: 0.75,
                }}
              >
                {index + 1}
              </span>
            )}
          </motion.button>
        );
      })}
      </AnimatePresence>
      </div>
      {edgeFade.left && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 left-0"
          style={{
            width: SESSION_TAB_EDGE_FADE_W,
            background:
              "linear-gradient(to right, var(--surface-overlay), transparent)",
          }}
        />
      )}
      {edgeFade.right && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 right-0"
          style={{
            width: SESSION_TAB_EDGE_FADE_W,
            background:
              "linear-gradient(to left, var(--surface-overlay), transparent)",
          }}
        />
      )}
    </div>
  );
}

export function ClaudeExpandedCard({
  provider,
  body,
  sourceName,
  speaking,
  spineSegments,
  remainingCount,
  accessibleLabel,
  railPhase,
  countdownHint,
  railProps,
  sessions = [],
  activeSessionHandle = null,
  onSelectSession = () => {},
  onCardFocusChange = () => {},
  compactConsole = false,
  summaryMode = false,
  growFromPill = true,
  consoleResize,
  onToggleExpanded,
  onMeasuredCardSizeChange,
}: {
  provider: AgentName;
  body: string;
  sourceName: string;
  speaking: boolean;
  spineSegments: number[];
  remainingCount: number;
  accessibleLabel: string;
  railPhase: ReplyRailPhase;
  countdownHint: string | null;
  railProps: Omit<ReplyRailProps, "phase" | "countdownHint">;
  /** The managed-session list behind the tab strip. Fewer than two sessions
   *  means no strip at all (design decision 3), and then this card is
   *  byte-identical to the single-session card that shipped before it. */
  sessions?: HudSession[];
  activeSessionHandle?: string | null;
  onSelectSession?: (handle: string) => void;
  onCardFocusChange?: (focused: boolean) => void;
  compactConsole?: boolean;
  /** The glance card at the pill. Otherwise this is the centered console. */
  summaryMode?: boolean;
  /** Glance only, read on mount: grow out of the pill. False when the card
   *  returns from the console. Growing while hidden re-measured the card on
   *  every frame, which held back the window move until the spring settled,
   *  leaving the HUD empty for about half a second (owner report 2026-10-24). */
  growFromPill?: boolean;
  /** Console only: the resize handles' pointer handlers. */
  consoleResize?: ConsoleResizeHandlers;
  /** Opens the console from the glance card, or collapses it to the pill. */
  onToggleExpanded?: () => void;
  /** Reports the painted-card rect into the HUD's one `measuredCardSize`
   *  state (decision 17), which feeds the native bounds and region. */
  onMeasuredCardSizeChange?: (measured: MeasuredClaudeCard) => void;
}) {
  const conversation = useContext(AgentConversationContext);
  const reducedMotion = useReducedMotion();
  const identityRef = useRef<HTMLDivElement | null>(null);
  const railRef = useRef<HTMLDivElement | null>(null);
  const footerRef = useRef<HTMLDivElement | null>(null);
  const wellRef = useRef<HTMLDivElement | null>(null);
  // Decision 3. Kept as one value so the rendered rows and the measured chrome
  // can never disagree about whether a strip exists.
  const stripPresent = sessions.length >= 2;
  // Temporary hook cards still lock the route while a reply is in flight.
  // Managed consoles keep one independent draft per session, so editing one
  // session must not disable navigation to the others.
  const railBusy = railPhase !== "resting";
  const switchingLocked = railBusy && !compactConsole;
  // Decision 9 (Task 4.7): the resting rail's blocked-line copy reflects the
  // ACTIVE session's `can_reply`, read straight off the strip's own data so
  // it can never disagree with the capsule's status glyph.
  const activeSession = sessions.find(
    (session) => session.session_handle === activeSessionHandle,
  );
  // A working agent no longer blocks the field: its reply is held.
  const activeCanReply = activeSession ? sessionTakesReply(activeSession) : true;

  // Temporary cards require a plain click before keyboard navigation. A
  // managed console already activates the native window and autofocuses its
  // composer, so Ctrl+1-9 / Ctrl+Tab must work immediately from that caret.
  const [cardFocused, setCardFocused] = useState(false);
  const focusActive = stripPresent && (cardFocused || compactConsole);

  useEffect(() => {
    if (!stripPresent) setCardFocused(false);
  }, [stripPresent]);

  useEffect(() => {
    onCardFocusChange(focusActive);
    return () => onCardFocusChange(false);
  }, [focusActive, onCardFocusChange]);

  // Focus stays strictly user-initiated (never set by an arriving
  // completion); once granted, losing the OS window's focus (e.g. the user
  // alt-tabs to their editor) releases it symmetrically.
  // Native activation itself moves keyboard focus to the top-level window and
  // straight back to the WebView, which fires a blur. Only a blur the window
  // has not recovered from shortly after is the user leaving.
  useEffect(() => {
    if (!cardFocused) return;
    let timer: number | null = null;
    const onBlur = () => {
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        timer = null;
        if (!document.hasFocus()) setCardFocused(false);
      }, CARD_BLUR_SETTLE_MS);
    };
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("blur", onBlur);
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [cardFocused]);

  // Session shortcuts (Ctrl/Cmd+1-9, Ctrl+Tab) are owned by the HUD
  // coordinator, which handles them from the card and the console alike.

  // The measured painted-card rect (decision 13), through the one shared
  // routine this surface and the question card both use.
  // The glance card's window holds the card with its reply field at the
  // five-line cap from the start, so typing never resizes it.
  const railCanReply = railProps.canReply ?? activeCanReply;
  const reservesTyping =
    summaryMode && railCanReply && railProps.notice !== "ended";
  const growthReserve = useCallback(
    () =>
      reservesTyping
        ? Math.max(0, REPLY_RAIL_CARD_MAX_H - (railRef.current?.offsetHeight ?? 0))
        : 0,
    [reservesTyping],
  );
  const { cardRef, measuredCardSize } = useMeasuredClaudeCard(
    summaryMode ? "summary" : "response",
    onMeasuredCardSizeChange,
    undefined,
    growthReserve,
  );
  // Everything on the card that is not the body well. Measured, not assumed,
  // because the rail grows as the reply field grows.
  const [chromeHeight, setChromeHeight] = useState(0);
  // The body text's own height, read even while the card is shorter than it.
  const [wellNaturalHeight, setWellNaturalHeight] = useState(0);
  const [scrollable, setScrollable] = useState(false);
  const [atBottom, setAtBottom] = useState(false);

  // The console fills its window, which the native reconciler sizes from the
  // remembered per-display size; the glance card takes its natural height up
  // to the monitor-aware ceiling.
  const consoleMode = !summaryMode;
  const ceiling = claudeCardCeiling(0, RESPONSE_MAX_H);

  useLayoutEffect(() => {
    const measureChrome = () => {
      const identityHeight = identityRef.current?.offsetHeight ?? 0;
      const railHeight = railRef.current?.offsetHeight ?? 0;
      const footerHeight = footerRef.current?.offsetHeight ?? 0;
      const footerChrome =
        footerHeight > 0 ? CLAUDE_CARD_GAP_RAIL_FOOTER + footerHeight : 0;
      setChromeHeight(
        CLAUDE_CARD_PAD_TOP +
          identityHeight +
          CLAUDE_CARD_GAP_IDENTITY_BODY +
          (consoleMode
            ? CLAUDE_CARD_GAP_BODY_DIVIDER +
              CLAUDE_CARD_DIVIDER_H +
              CLAUDE_CARD_GAP_DIVIDER_RAIL
            : CLAUDE_CARD_GAP_BODY_RAIL) +
          railHeight +
          footerChrome +
          CLAUDE_CARD_PAD_BOTTOM,
      );
    };
    measureChrome();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measureChrome);
    for (const node of [
      identityRef.current,
      railRef.current,
      footerRef.current,
    ]) {
      if (node) observer.observe(node);
    }
    return () => observer.disconnect();
    // `stripPresent` is a chrome row appearing or disappearing, exactly like a
    // rail phase change: re-measure rather than wait for an observation.
  }, [consoleMode, railPhase, remainingCount, stripPresent]);

  // The conversation timeline reads oldest first and opens on the newest
  // turn: on open, on a tab switch and when a new turn arrives (scope decision
  // 7). A single response body (summary, or a hook card without history)
  // still opens at its start. "Newest turn" means its top when it is taller
  // than the well, otherwise the bottom of the timeline.
  const timelineMode = !summaryMode && conversation !== null;
  const newestTurnId = timelineMode
    ? (conversation.turns[conversation.turns.length - 1]?.id ?? null)
    : null;
  const tabKey = activeSessionHandle ?? "single";
  // While true, content growth (fonts, a working row, the chrome settling)
  // re-pins the well to the newest turn. Scrolling away releases it.
  const followNewestRef = useRef(true);
  const smoothScrollUntilRef = useRef(0);
  // Where this card last put the well. The scroll event for that move can
  // arrive after the layout has shifted again; it is ours, not the user's.
  const programmaticTopRef = useRef<number | null>(null);
  const newestScrollTarget = useCallback((): number | null => {
    const well = wellRef.current;
    if (!well) return null;
    const maxScroll = Math.max(0, well.scrollHeight - well.clientHeight);
    const tabBody = Array.from(
      well.querySelectorAll<HTMLElement>("[data-claude-tab-body]"),
    ).find((node) => node.dataset.claudeTabBody === tabKey);
    const newest = tabBody?.querySelector<HTMLElement>(
      '[data-newest-turn="true"]',
    );
    if (!newest) return maxScroll;
    const top =
      newest.getBoundingClientRect().top -
      well.getBoundingClientRect().top +
      well.scrollTop -
      CLAUDE_TIMELINE_SCROLL_GAP;
    return Math.min(maxScroll, Math.max(0, top));
  }, [tabKey]);
  const scrollToNewest = useCallback(
    (smooth: boolean) => {
      const well = wellRef.current;
      const target = newestScrollTarget();
      if (!well || target === null) return;
      followNewestRef.current = true;
      if (smooth && typeof well.scrollTo === "function") {
        smoothScrollUntilRef.current = Date.now() + 700;
        well.scrollTo({ top: target, behavior: "smooth" });
      } else {
        well.scrollTop = target;
        programmaticTopRef.current = well.scrollTop;
      }
    },
    [newestScrollTarget],
  );
  const scrollMarkRef = useRef<{ tab: string; newest: string | null } | null>(
    null,
  );
  useLayoutEffect(() => {
    const well = wellRef.current;
    if (!well) return;
    const previous = scrollMarkRef.current;
    scrollMarkRef.current = { tab: tabKey, newest: newestTurnId };
    if (!timelineMode) {
      // A background session's position never leaks into the next read.
      if (previous === null || previous.tab !== tabKey) well.scrollTop = 0;
      return;
    }
    if (
      previous !== null &&
      previous.tab === tabKey &&
      previous.newest === newestTurnId
    ) {
      return;
    }
    // A new turn on the tab being read glides there (a sent reply lands);
    // opening and switching tabs land immediately.
    const newTurnOnSameTab = previous !== null && previous.tab === tabKey;
    scrollToNewest(newTurnOnSameTab && !reducedMotion);
  }, [tabKey, newestTurnId, timelineMode, reducedMotion, scrollToNewest]);

  // Webfonts can change line wrapping after the first paint.
  useEffect(() => {
    if (!timelineMode) return;
    let active = true;
    void document.fonts?.ready.then(
      () => {
        if (active && followNewestRef.current) scrollToNewest(false);
      },
      () => undefined,
    );
    return () => {
      active = false;
    };
  }, [timelineMode, scrollToNewest]);

  // Top fade once scrolled away from the start; bottom fade while there is
  // more below. Both stay mounted so their opacity can ease (0.15s).
  const [scrolledFromTop, setScrolledFromTop] = useState(false);
  useEffect(() => {
    const well = wellRef.current;
    if (!well) return;
    const recompute = () => {
      setWellNaturalHeight(well.scrollHeight);
      setScrollable(well.scrollHeight > well.clientHeight + 1);
      setAtBottom(
        well.scrollTop + well.clientHeight >= well.scrollHeight - 12,
      );
      setScrolledFromTop(well.scrollTop > 2);
    };
    const onScroll = () => {
      recompute();
      if (!timelineMode || Date.now() < smoothScrollUntilRef.current) return;
      const programmaticTop = programmaticTopRef.current;
      if (
        programmaticTop !== null &&
        Math.abs(well.scrollTop - programmaticTop) <= 1
      ) {
        return;
      }
      programmaticTopRef.current = null;
      const target = newestScrollTarget();
      followNewestRef.current =
        target !== null && Math.abs(well.scrollTop - target) <= 12;
    };
    const onResize = () => {
      if (timelineMode && followNewestRef.current) scrollToNewest(false);
      recompute();
    };
    onResize();
    well.addEventListener("scroll", onScroll, { passive: true });
    if (typeof ResizeObserver === "undefined") {
      return () => well.removeEventListener("scroll", onScroll);
    }
    const observer = new ResizeObserver(onResize);
    observer.observe(well);
    const content = well.firstElementChild;
    if (content) observer.observe(content);
    return () => {
      well.removeEventListener("scroll", onScroll);
      observer.disconnect();
    };
  }, [
    body,
    chromeHeight,
    conversation,
    timelineMode,
    newestScrollTarget,
    scrollToNewest,
  ]);

  // Under the newest user turn: held for the agent's next Stop, or sent
  // while the agent works.
  const newestTurn = conversation?.turns[conversation.turns.length - 1];
  const replyStatus: AgentReplyStatus | null =
    newestTurn?.role !== "user"
      ? null
      : activeSession?.reply_held === true
        ? "held"
        : conversation?.activity === "working"
          ? "sent"
          : null;

  // The glance card's resting line carries the queue depth beside the
  // countdown instead of a footer row of its own.
  const queueInRail = !consoleMode && railPhase === "resting" && remainingCount > 0;

  // The glance card's body opens typing wherever the rail itself would.
  const bodyOpensTyping =
    summaryMode &&
    railProps.onEditStart !== undefined &&
    railProps.notice !== "ended" &&
    (railPhase === "draft" || (railPhase === "resting" && railCanReply));

  // The one elastic measurement on the card.
  const wellMaxHeight = Math.max(CLAUDE_WELL_MIN_H, ceiling - chromeHeight);
  const microStyle: CSSProperties = hudMetaText();
  // The glance card animates to a number, not "auto": Framer animates only
  // when its target changes, so with "auto" a reply row opening on
  // push-to-talk (or a draft landing) snapped the card taller in one frame.
  // The number is the card's natural height: the measured chrome, the body's
  // own height up to its budget, and the 1px border on each side.
  const glanceHeight: number | "auto" =
    chromeHeight > 0 && wellNaturalHeight > 0
      ? Math.min(ceiling, chromeHeight + Math.min(wellNaturalHeight, wellMaxHeight) + 2)
      : "auto";

  return (
    <motion.section
      ref={cardRef}
      data-testid="claude-expanded-card"
      data-claude-card-measure={summaryMode ? "summary" : "response"}
      data-measured-card-height={measuredCardSize?.height ?? undefined}
      // The glance card grows out of the pill. The console opens in place at
      // the center of the display, so it has no pill to grow from.
      initial={
        consoleMode || !growFromPill
          ? false
          : {
              width: CLAUDE_PILL_MIN_W,
              height: CLAUDE_PILL_H,
              borderRadius: CLAUDE_PILL_RADIUS,
            }
      }
      animate={
        consoleMode
          ? { borderRadius: CLAUDE_CARD_RADIUS }
          : {
              width: GLANCE_CARD_W,
              height: glanceHeight,
              borderRadius: CLAUDE_CARD_RADIUS,
            }
      }
      transition={
        consoleMode || reducedMotion ? { duration: 0 } : GLANCE_SPRING
      }
      className="relative flex min-h-0 flex-col overflow-hidden"
      aria-label={accessibleLabel}
      data-console={consoleMode ? "true" : "false"}
      onClick={stripPresent ? () => setCardFocused(true) : undefined}
      style={{
        transformOrigin: "left center",
        ...(consoleMode
          ? {
              width: `calc(100vw - ${CONSOLE_PAD_X * 2}px)`,
              height: `calc(100vh - ${CONSOLE_PAD_Y * 2}px)`,
            }
          : { maxHeight: ceiling }),
        background: "var(--surface-overlay)",
        // The HUD desktop material shared with the dictation pill: a
        // contrasting perimeter plus the theme-aware two-stop elevation. Agent
        // cards use the card variant, which has no top specular line.
        border: "1px solid var(--hud-desktop-border)",
        boxShadow: "var(--hud-card-elevation)",
        padding: `${CLAUDE_CARD_PAD_TOP}px ${CLAUDE_CARD_PAD_X}px ${CLAUDE_CARD_PAD_BOTTOM}px`,
        // No ring for keyboard-armed session switching: the composer's own
        // focus outline already marks the active window, and a second frost
        // ring around the whole card read as a stray browser focus outline.
        outline: "none",
      }}
    >
      <motion.div
        className="flex min-h-0 flex-1 flex-col"
        // The glance content keeps its final width while the card grows out
        // of the 200px pill. Framer measures the "auto" height target when
        // the grow starts; text wrapped at the pill's width measured far too
        // tall, so the card opened tall and dropped to its real height when
        // the animation ended (owner report 2026-10-24). The card's overflow
        // clips the content until the width catches up.
        style={consoleMode ? undefined : { width: GLANCE_CONTENT_W, flexShrink: 0 }}
        initial={
          compactConsole || reducedMotion ? false : { opacity: 0, y: 6 }
        }
        animate={reducedMotion ? { opacity: 1 } : { opacity: 1, y: 0 }}
        transition={
          compactConsole || reducedMotion
            ? { duration: 0.18 }
            : {
                duration: 0.18,
                ease: "easeOut",
                delay: CLAUDE_CARD_CONTENT_DELAY_S,
              }
        }
      >
        <div ref={identityRef}>
          <ClaudeIdentityRow
            variant="card"
            // Decision 2: the strip already names every session, so the
            // subordinate `· <name>` suffix would say it twice. Emptying the
            // prop is the row's existing bare-`Claude` path; its contract is
            // untouched, and the accessible label still carries the name.
            provider={provider}
            sourceName={stripPresent ? "" : sourceName}
            speaking={speaking}
            cardTrailing={
              stripPresent ? (
                <SessionTabs
                  sessions={sessions}
                  activeHandle={activeSessionHandle}
                  onSelect={onSelectSession}
                  locked={switchingLocked}
                />
              ) : undefined
            }
            cardControl={
              onToggleExpanded ? (
                <HudExpandToggle
                  expanded={consoleMode}
                  label={consoleMode ? "Collapse response" : "Expand response"}
                  onToggle={onToggleExpanded}
                  testId="claude-expand-toggle"
                />
              ) : undefined
            }
          />
        </div>

        {/* Everything under the identity row. On the glance card it is one
            click-to-type area, out to the card's edges and gaps included; only
            the identity row drags the card (owner, 2026-10-25). */}
        <div
          data-testid="claude-card-body"
          className={`flex min-h-0 flex-col${consoleMode ? " flex-1" : ""}`}
          // Never a drag handle: the glance card and the console both drag by
          // their identity row only.
          data-hud-interactive="true"
          onClick={
            bodyOpensTyping
              ? (event) => {
                  // The reply line handles its own click.
                  const target = event.target as Element | null;
                  if (target?.closest?.("[data-hud-text-field]")) return;
                  railProps.onEditStart?.();
                }
              : undefined
          }
          style={
            summaryMode
              ? {
                  margin: `0 ${-CLAUDE_CARD_PAD_X}px ${-CLAUDE_CARD_PAD_BOTTOM}px`,
                  padding: `${CLAUDE_CARD_GAP_IDENTITY_BODY}px ${CLAUDE_CARD_PAD_X}px ${CLAUDE_CARD_PAD_BOTTOM}px`,
                  // It never drags, so never shows the root's grab cursor.
                  cursor: bodyOpensTyping ? "text" : "default",
                }
              : { paddingTop: CLAUDE_CARD_GAP_IDENTITY_BODY, cursor: "auto" }
          }
        >
        <div
          className={`relative min-h-0${consoleMode ? " flex flex-1 flex-col" : ""}`}
        >
          <div
            ref={wellRef}
            data-testid="claude-well"
            className={consoleMode ? "min-h-0 flex-1" : undefined}
            style={{
              maxHeight: consoleMode ? undefined : wellMaxHeight,
              overflowY: "auto",
              position: "relative",
              // The body's left inset lives on the tab body below, the spine's
              // containing block, so the spine keeps its gap to the text. The
              // right inset keeps the timeline's panes clear of the scrollbar.
              paddingRight: timelineMode ? CLAUDE_TIMELINE_SCROLL_GAP : 0,
            }}
          >
            {/* Tab switch: the well cross-fades at 150ms (design.md
                transitions table). `scrollTop` reset lives in the effect
                below, not here, so it also fires when reduced motion skips
                the fade. */}
            <AnimatePresence initial={false} mode="popLayout">
              <motion.div
                key={tabKey}
                // The spine's containing block: it spans the text, not the
                // console's taller well.
                className="relative"
                style={{
                  // A single response keeps the spine's 2px rule and 12px
                  // gap. The glance summary has no spine and starts at the
                  // mark's left edge (owner override of the prototype's
                  // label indent); timeline panes carry their own edge.
                  paddingLeft: timelineMode || summaryMode ? 0 : CLAUDE_BODY_GUTTER,
                }}
                data-testid="claude-tab-body"
                data-claude-tab-body={tabKey}
                initial={reducedMotion ? false : { opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: reducedMotion ? 0 : 0.15 }}
              >
                {/* The spine lives inside the scrolled content box and
                    scrolls with the text; it reports nothing about scroll
                    position (decision 11). */}
                {!summaryMode && conversation ? <AgentTimeline conversation={conversation} provider={provider} needsInput={activeSession?.needs_input === true} replyStatus={replyStatus} /> : <>
                {/* The glance card's summary carries no spine. */}
                {!summaryMode && <Spine segments={spineSegments} speakingIndex={null} />}
                {/* Agent Markdown as elements, at the reply field's size. */}
                <MarkdownText
                  text={body}
                  fontSize={13}
                  lineHeight={summaryMode ? 1.5 : 1.55}
                />
                </>}
              </motion.div>
            </AnimatePresence>
          </div>
          <div
            aria-hidden="true"
            data-testid="claude-well-top-fade"
            data-visible={scrolledFromTop ? "true" : "false"}
            style={{
              position: "absolute",
              left: 0,
              right: 0,
              top: 0,
              height: CLAUDE_WELL_TOP_FADE_H,
              pointerEvents: "none",
              opacity: scrolledFromTop ? 1 : 0,
              transition: reducedMotion ? undefined : "opacity 0.15s",
              background:
                "linear-gradient(to top, transparent, var(--surface-overlay))",
            }}
          />
          <div
            aria-hidden="true"
            data-testid="claude-well-fade"
            data-visible={scrollable && !atBottom ? "true" : "false"}
            style={{
              position: "absolute",
              left: 0,
              right: 0,
              bottom: 0,
              height: CLAUDE_WELL_FADE_H,
              pointerEvents: "none",
              opacity: scrollable && !atBottom ? 1 : 0,
              transition: reducedMotion ? undefined : "opacity 0.15s",
              background:
                "linear-gradient(to bottom, transparent, var(--surface-overlay))",
            }}
          />
        </div>

        {/* Only the console keeps the divider between its timeline and the
            composer. The glance card's hint line sits straight under the
            text (owner, 2026-10-24). */}
        {consoleMode && (
          <div
            aria-hidden="true"
            data-testid="claude-card-divider"
            className="shrink-0"
            style={{
              height: CLAUDE_CARD_DIVIDER_H,
              marginTop: CLAUDE_CARD_GAP_BODY_DIVIDER,
              background: "var(--divider)",
            }}
          />
        )}

        <div
          ref={railRef}
          className="shrink-0"
          style={{
            marginTop: consoleMode
              ? CLAUDE_CARD_GAP_DIVIDER_RAIL
              : CLAUDE_CARD_GAP_BODY_RAIL,
          }}
        >
          <motion.div
            key={activeSessionHandle ?? "single"}
            initial={reducedMotion ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: reducedMotion ? 0 : 0.15 }}
          >
            <ReplyRail
              phase={railPhase}
              textMaxH={summaryMode ? REPLY_RAIL_CARD_TEXT_MAX_H : undefined}
              countdownHint={countdownHint}
              queueHint={queueInRail ? queueDepthCopy(remainingCount) : null}
              canReply={activeCanReply}
              {...railProps}
            />
          </motion.div>
        </div>

        {remainingCount > 0 && !queueInRail && (
          <div
            ref={footerRef}
            className="flex shrink-0 items-center justify-end"
            style={{ ...microStyle, marginTop: CLAUDE_CARD_GAP_RAIL_FOOTER }}
          >
            <span className="text-right">{queueDepthCopy(remainingCount)}</span>
          </div>
        )}
        </div>
      </motion.div>
      {consoleMode && consoleResize !== undefined && (
        <ConsoleResizeFrame handlers={consoleResize} />
      )}
    </motion.section>
  );
}

export function detectTruncation(text: string): boolean {
  if (!text || text.trim().length === 0) return false;
  const lines = text.split("\n");
  if (lines.length > 4) return true;
  const charsPerLine = 45;
  let totalLines = 0;
  for (const line of lines) {
    totalLines += Math.max(1, Math.ceil(line.length / charsPerLine));
    if (totalLines > 4) return true;
  }
  return false;
}
