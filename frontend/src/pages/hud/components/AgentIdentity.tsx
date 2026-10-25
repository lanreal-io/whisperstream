import { useReducedMotion, motion, AnimatePresence } from "framer-motion";
import { type AgentName } from "../../../lib/socket/agentEvents";
import { AgentVoiceGlyph } from "../../../components/agentSpeak/AgentVoiceGlyph";
import { type ReactNode } from "react";
import { agentProviderLabel } from "../../../lib/sessionName";
import { CLAUDE_PILL_H } from "../geometry";
import { HUD_META_CAPTION_SIZE, HUD_META_OPACITY, hudMetaText } from "../metaText";

function spokenSentenceChunks(summary: string): string[] {
  return (
    summary.match(/[^.!?]+(?:[.!?]+|$)/gu)?.map((sentence) => sentence.trim())
      .filter(Boolean) ?? [summary]
  );
}

// --- Spine (Task 1.4, design.md Reference Code section B, decisions 10-12) ---
// Vertical attribution rule for Claude's own words. Reuses the existing
// spokenSentenceChunks splitter rather than a second sentence splitter.
/** Turn a spoken summary into relative segment weights for the Spine, one
 *  weight per spoken sentence. `[1]` (a single unsegmented weight) when there
 *  is no spoken summary. */
export function spineSegmentsFromSpokenSummary(spokenSummary: string): number[] {
  if (!spokenSummary.trim()) return [1];
  const chunks = spokenSentenceChunks(spokenSummary);
  return chunks.length > 0 ? chunks.map((chunk) => Math.max(1, chunk.length)) : [1];
}

/** Vertical attribution rule for Claude's own words. Segments = spoken sentences.
 *  Lives INSIDE the scrolling well and scrolls with the text. Reports nothing
 *  about scroll position (decision 11) — the 24px bottom fade (Task 2.2) does
 *  that job instead. */
export function Spine({
  segments,
  speakingIndex,
}: {
  segments: number[]; // relative weights; [1] when there is no spoken summary
  speakingIndex: number | null;
}) {
  const reduced = useReducedMotion();
  return (
    <div
      aria-hidden="true"
      data-testid="claude-spine"
      className="absolute left-0 top-0 bottom-0 flex flex-col"
      style={{ width: 2, gap: 2 }}
    >
      {segments.map((weight, i) => (
        <motion.span
          key={i}
          style={{ flex: weight, borderRadius: 9999, background: "var(--color-frost-deep)" }}
          animate={
            speakingIndex === null
              ? { opacity: 0.3 }
              : reduced
                ? { opacity: i <= speakingIndex ? 0.6 : 0.28 }
                : i === speakingIndex
                  ? { opacity: [0.28, 0.85, 0.28] }
                  : { opacity: i < speakingIndex ? 0.6 : 0.28 }
          }
          transition={
            !reduced && i === speakingIndex
              ? { duration: 1.1, repeat: Infinity, ease: "easeInOut" }
              : { duration: 0.16 }
          }
        />
      ))}
    </div>
  );
}

/** The compact pill's caption: 4px under the name, one 13px line. The line
 *  is taller than the 9px type so descenders ("p" in "reply") are not
 *  clipped by the height animation's overflow. */
const COMPACT_CAPTION_GAP = 4;
const COMPACT_CAPTION_LINE = 13;
const COMPACT_CAPTION_H = COMPACT_CAPTION_GAP + COMPACT_CAPTION_LINE;

/** Queued responses behind the current one, in words (pill caption and the
 *  card footer). */
export function queueDepthCopy(remainingCount: number): string {
  return `${remainingCount} in queue`;
}

export const AGENT_MARK_VARIANTS = [
  "prompt-cursor",
  "code-chevrons",
  "braces-cursor",
  "terminal-frame",
] as const;

export type AgentMarkVariant = (typeof AGENT_MARK_VARIANTS)[number];

/** One code-owned choice controls every production agent surface. */
export const DEFAULT_AGENT_MARK_VARIANT: AgentMarkVariant = "terminal-frame";

/** Small neutral coding-agent mark shown in the question card header and every
 *  other agent surface. Provider identity belongs to the adjacent text; Claude
 *  and Codex deliberately share this Whisperstream-owned artwork. `breathing`
 *  drives the compact-speaking pulse without changing the glyph. */
export function AgentMark({
  provider = "claude",
  size = 12,
  breathing = false,
  variant,
}: {
  provider?: AgentName;
  size?: number;
  breathing?: boolean;
  variant?: AgentMarkVariant;
}) {
  const reduced = useReducedMotion();
  if (variant === undefined) return <AgentVoiceGlyph speaking={breathing} size={size} provider={provider} />;
  return (
    <motion.svg
      width={size}
      height={size}
      viewBox="0 0 14 14"
      aria-hidden="true"
      data-agent-mark={provider}
      data-agent-mark-variant={variant}
      className="shrink-0"
      animate={
        breathing && !reduced
          ? { opacity: [0.55, 1, 0.55] }
          : { opacity: breathing ? 0.85 : 1 }
      }
      transition={
        breathing && !reduced
          ? { duration: 1.6, repeat: Infinity, ease: "easeInOut" }
          : { duration: 0.16 }
      }
    >
      {variant === "prompt-cursor" ? (
        <g
          fill="none"
          stroke="var(--color-frost-deep)"
          strokeWidth="1.25"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M2.25 3.75 5.5 7l-3.25 3.25" />
          <line x1="7.25" y1="10.25" x2="11.75" y2="10.25" />
        </g>
      ) : variant === "code-chevrons" ? (
        <g
          fill="none"
          stroke="var(--color-frost-deep)"
          strokeWidth="1.2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M4.75 3 1.75 7l3 4" />
          <path d="m9.25 3 3 4-3 4" />
          <path d="m8.25 2.5-2.5 9" />
        </g>
      ) : variant === "braces-cursor" ? (
        <g
          fill="none"
          stroke="var(--color-frost-deep)"
          strokeWidth="1.15"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M5 2.25h-.7c-.85 0-1.3.45-1.3 1.3V5c0 1.05-.45 1.7-1.35 2 .9.3 1.35.95 1.35 2v1.45c0 .85.45 1.3 1.3 1.3H5" />
          <path d="M9 2.25h.7c.85 0 1.3.45 1.3 1.3V5c0 1.05.45 1.7 1.35 2-.9.3-1.35.95-1.35 2v1.45c0 .85-.45 1.3-1.3 1.3H9" />
          <line x1="5.75" y1="10.25" x2="8.25" y2="10.25" />
        </g>
      ) : (
        <g
          fill="none"
          stroke="var(--color-frost-deep)"
          strokeWidth="1.25"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <rect x="1.25" y="2" width="11.5" height="10" rx="2" />
          <path d="M3.5 5.25 5.25 7 3.5 8.75" />
          <line x1="7" y1="8.75" x2="10" y2="8.75" />
        </g>
      )}
    </motion.svg>
  );
}

// --- ClaudeIdentityRow (Task 1.3, design.md Reference Code section A;
// reworked in hud-session-name Task 3.1) ---
// The one identity row reused verbatim across compact, expanded, question,
// and permission surfaces (design decision 8). Two spellings of the same
// mark + label idiom: "compact" shows the resolved session name (alone) and
// a trailing queue chip / stop-speech button; "card" shows the provider label
// plus a subordinate `· <name>` suffix, ungated (hud-session-name
// design decision 5), and the right-aligned stop-speaking control.
// No `CLAUDE SESSION`, no status dot, no uppercase, no letter-spacing
// (design decisions 2, 6, 31).

export function ClaudeIdentityRow({
  variant,
  provider = "claude",
  sourceName,
  speaking = false,
  remainingCount = 0,
  cardTrailing,
  cardControl,
  compactHint,
  compactTime = null,
  markVariant,
}: {
  variant: "compact" | "card";
  provider?: AgentName;
  /** Compact + permission: the whole text slot. Card: the subordinate
   *  suffix after the provider label. Already resolved and capped backend-side; this
   *  row does no resolution and does not know where the name came from
   *  (hud-session-name design decision 4 — no visual consumer of
   *  `source_name_is_custom`). */
  sourceName: string;
  speaking?: boolean;
  /** Compact only: queue depth for the trailing queue chip. */
  remainingCount?: number;
  /** Card only: horizontal controls that sit beside the provider title. */
  cardTrailing?: ReactNode;
  /** Card only: the row's last control (the expand/collapse pill). */
  cardControl?: ReactNode;
  /** Compact only: the faint resting reply hint, a caption under the session
   *  name (owner, 2026-10-24, prototype 1b). Hidden while speaking or queued. */
  compactHint?: ReactNode;
  /** Compact only: the reply countdown in the trailing slot. It yields to the
   *  stop button and queue chip without changing the pill's width. */
  compactTime?: string | null;
  /** Development comparison surface only. Production callers omit this and
   *  use `DEFAULT_AGENT_MARK_VARIANT`. */
  markVariant?: AgentMarkVariant;
}) {
  const reducedMotion = useReducedMotion();
  const providerLabel = agentProviderLabel(provider);
  // hud-session-name design decision 7: the chain's terminal value never
  // becomes a card suffix, so `Claude · Claude` is unreachable.
  const named = sourceName.length > 0 && sourceName !== providerLabel;

  if (variant === "compact") {
    // One caption under the name (owner, 2026-10-24): the queue depth while
    // responses wait, otherwise the resting reply hint.
    const caption =
      remainingCount > 0
        ? { key: "queue", testId: "claude-compact-queue", content: queueDepthCopy(remainingCount) as ReactNode }
        : compactHint !== undefined && !speaking
          ? { key: "hint", testId: "claude-compact-reply-hint", content: compactHint }
          : null;
    const showTime = compactTime !== null && !speaking;
    return (
      <div
        className="flex items-center"
        style={{ height: CLAUDE_PILL_H, padding: "0 14px 0 12px", gap: 6 }}
      >
        {/* Constant. Deliberately NOT keyed: the provider did not change, only
            which session did (hud-session-name design decision 9). */}
        <AgentMark
          provider={provider}
          size={28}
          breathing={speaking}
          variant={markVariant}
        />
        <div className="relative flex min-w-0 flex-1 flex-col justify-center">
          <AnimatePresence initial={false}>
            <motion.span
              key={sourceName}
              initial={reducedMotion ? false : { opacity: 0, y: 3 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reducedMotion ? { opacity: 0 } : { opacity: 0, y: -3 }}
              transition={{ duration: reducedMotion ? 0 : 0.15 }}
              className="block truncate text-[13px] font-medium text-slate"
              style={{ fontFamily: "var(--font-agent)", lineHeight: "16px" }}
            >
              {sourceName}
            </motion.span>
          </AnimatePresence>
          {/* Height animates with the fade so the name glides up instead of
              jumping when the caption arrives after speech. */}
          <AnimatePresence initial={false} mode="wait">
            {caption !== null && (
              <motion.span
                key={caption.key}
                data-testid={caption.testId}
                className="block overflow-hidden whitespace-nowrap"
                initial={reducedMotion ? false : { height: 0, opacity: 0 }}
                animate={{ height: COMPACT_CAPTION_H, opacity: HUD_META_OPACITY }}
                exit={reducedMotion ? { opacity: 0 } : { height: 0, opacity: 0 }}
                transition={{ duration: reducedMotion ? 0 : 0.18, ease: "easeOut" }}
                style={{
                  // The queue depth keeps the shared size it has on the glance
                  // card and console; only the reply hint is the small caption.
                  ...hudMetaText(caption.key === "queue" ? undefined : HUD_META_CAPTION_SIZE),
                  // Framer owns the caption's opacity while it animates.
                  opacity: undefined,
                  lineHeight: `${COMPACT_CAPTION_LINE}px`,
                  paddingTop: COMPACT_CAPTION_GAP,
                }}
              >
                {caption.content}
              </motion.span>
            )}
          </AnimatePresence>
        </div>
        {showTime && (
          <span
            data-testid="claude-compact-reply-time"
            className="shrink-0"
            style={hudMetaText()}
          >
            {compactTime}
          </span>
        )}
      </div>
    );
  }

  return (
    <div
      className="flex items-center"
      style={{ minHeight: cardControl ? 28 : 22, gap: 6 }}
    >
      <AgentMark provider={provider} size={28} breathing={speaking} variant={markVariant} />
      <span
        className="text-[13px] font-medium text-slate"
        style={{ fontFamily: "var(--font-agent)" }}
      >
        {providerLabel}
      </span>
      {/* Ungated (hud-session-name design decision 5): shown whenever a name
          resolved. `0 1 auto` + minWidth 0 so a long name truncates instead
          of pushing the stop control. A tab strip appearing empties this prop
          (Task 4.4, decision 2); the cross-fade out is 150ms (design.md
          transitions table, "Second session arrives"). */}
      <AnimatePresence initial={false}>
        {named && (
          <motion.span
            key="suffix"
            className="min-w-0 truncate"
            initial={reducedMotion ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: reducedMotion ? 0 : 0.15 }}
            style={{
              flex: "0 1 auto",
              minWidth: 0,
              fontFamily: "var(--font-agent)",
              fontSize: 10.5,
              fontWeight: 500,
              color: "var(--color-steel)",
            }}
          >
            {"·"}
            <span style={{ marginLeft: 3 }}>{sourceName}</span>
          </motion.span>
        )}
      </AnimatePresence>
      {cardTrailing ? (
        <div
          data-testid="claude-card-header-trailing"
          className="ml-1 min-w-0 flex-1"
        >
          {cardTrailing}
        </div>
      ) : (
        <span className="ml-auto" />
      )}
      {cardControl}
    </div>
  );
}


