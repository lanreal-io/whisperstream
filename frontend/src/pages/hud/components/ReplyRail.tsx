import { focusFieldAtEnd } from "../fieldFocus";
import { motion, useReducedMotion } from "framer-motion";
import { type ReplyNotice, type ReplyRailPhase } from "../presentation";
import { type ReactNode, useEffect, useRef, useLayoutEffect, useState } from "react";
import { ProcessingSpinner } from "../views/RecordingControls";
import { RESPONSE_MAX_H, claudeCardCeiling } from "../geometry";
import { ReconciledPreviewText } from "./PreviewText";
import { VoiceBars } from "../../../components/Waveform";
import { CopyGlyph } from "../../../components/CopyGlyph";
import { hudMetaText } from "../metaText";
import { SecondaryButton } from "../../../components/SecondaryButton";
import {
  type ShortcutPlatform,
  shortcutPlatform,
} from "../../../lib/shortcutDisplay";

// --- ReplyRail (Task 1.5, design.md Reference Code section D, decisions
// 23, 24, 26; agent-speak-fixes Task 2.4) ---
// One reply-input component shared by the compact pill, the expanded card,
// and the question card (decision 23) — there is no second reply input
// anywhere. The live phase reuses ReconciledPreviewText with its exact
// existing reveal parameters (decision 24); no second grapheme animation is
// authored here.
//
// Since agent-speak-fixes the rail is drawn as a field only where you type
// (resting and editing). Spoken phases (live, processing, draft, sent) read as
// plain text at the same inset, and clicking a spoken draft turns it into the
// field in place, so text never shifts. An Enter key cap on the right lights
// once there is something to submit, and a hint row beneath it can carry the
// spoken-draft submit hint.

/** Return-arrow outline, drawn in the key cap on Windows. */
function ReturnGlyph() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 14 14"
      aria-hidden="true"
      className="shrink-0"
    >
      <path
        d="M11 3.5v3a2 2 0 01-2 2H3.5M3.5 8.5L6 6M3.5 8.5L6 11"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Check that draws in on send, pathLength 0 -> 1 over 180ms easeOut
 *  (design.md state 6). Appears drawn under reduced motion. */
export function ReplySentGlyph({ size = 14 }: { size?: number }) {
  const reduced = useReducedMotion();
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 14 14"
      aria-hidden="true"
      className="shrink-0"
    >
      <motion.path
        d="M3 7.2l2.6 2.6L11 4.4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        initial={reduced ? false : { pathLength: 0 }}
        animate={{ pathLength: 1 }}
        transition={reduced ? { duration: 0 } : { duration: 0.18, ease: "easeOut" }}
      />
    </svg>
  );
}

/** The rail's trailing slot, where the Enter key cap sits for a draft. Live
 *  dictation's bars and the processing spinner draw here too, so reply text
 *  starts at the agent text's left edge in every phase and nothing beside it
 *  shifts as it is spoken, processed and lands as a draft (owner,
 *  2026-10-25). The bars keep the dictation pill's presence algorithm at the
 *  rail's text-line size. */
const REPLY_RAIL_TRAILING_W = 24;
const REPLY_RAIL_TRAILING_H = 22;
const REPLY_RAIL_TEXT_H = 21;

function ReplyRailIndicator({
  phase,
  rms,
  peak,
}: {
  phase: "live" | "processing";
  rms: number;
  peak: number;
}) {
  return (
    <div
      className="grid shrink-0 place-items-center"
      data-testid="claude-reply-rail-glyph"
      data-phase={phase}
      // Centered on the text block however many lines it has (owner,
      // 2026-10-23), not on its last line like the key cap.
      style={{
        width: REPLY_RAIL_TRAILING_W,
        height: REPLY_RAIL_TRAILING_H,
        alignSelf: "center",
      }}
    >
      {phase === "live" ? (
        <VoiceBars
          state="recording"
          rms={rms}
          peak={peak}
          size="rail"
          color="var(--frost-muted)"
          testId="reply-voice-bars"
        />
      ) : (
        <ProcessingSpinner />
      )}
    </div>
  );
}

/** The Enter key cap. Visual only: Enter itself belongs to the textarea and,
 *  for a spoken draft, to the backend's claim. Lit once there is something to
 *  submit. */
function EnterKeyCap({
  lit,
  platform,
}: {
  lit: boolean;
  platform: ShortcutPlatform;
}) {
  return (
    <span
      aria-hidden="true"
      data-testid="claude-reply-keycap"
      data-lit={lit ? "true" : "false"}
      className="grid shrink-0 place-items-center"
      style={{
        width: REPLY_RAIL_TRAILING_W,
        height: REPLY_RAIL_TRAILING_H,
        borderRadius: 7,
        border: `1px solid ${lit ? "var(--border-accent)" : "var(--border-hairline)"}`,
        background: "var(--surface-control)",
        color: lit ? "var(--hud-frost-deep)" : "var(--color-steel)",
        opacity: lit ? 1 : 0.45,
        transition: "opacity 0.15s, color 0.15s, border-color 0.15s",
        fontFamily: "var(--font-agent)",
        fontSize: 13,
        lineHeight: 1,
      }}
    >
      {platform === "macos" ? "⏎" : <ReturnGlyph />}
    </span>
  );
}

/** Icon-only Copy for text that cannot be sent. Same footprint and radius as
 *  the key cap so the trailing controls read as one pair; the check draws in
 *  for a moment after a successful copy. */
function ReplyCopyButton({
  copied,
  label,
  onCopy,
}: {
  copied: boolean;
  label: string;
  onCopy: () => void;
}) {
  return (
    <SecondaryButton
      type="button"
      data-testid="claude-reply-copy"
      data-hud-interactive="true"
      data-copied={copied ? "true" : "false"}
      aria-label={label}
      title={label}
      onPointerDown={(event) => event.stopPropagation()}
      onClick={(event) => {
        event.stopPropagation();
        onCopy();
      }}
      className="grid shrink-0 place-items-center p-0 active:scale-[0.96]"
      style={{
        width: 24,
        height: 22,
        borderRadius: 7,
        color: copied ? "var(--hud-frost-deep)" : "var(--color-steel)",
      }}
    >
      {copied ? <ReplySentGlyph size={13} /> : <CopyGlyph size={13} />}
    </SecondaryButton>
  );
}

export interface ReplyRailProps {
  phase: ReplyRailPhase;
  /** Draft / sent text. Ignored for "live" (see stableCharCount) and
   *  "editing" (see editingValue). */
  text?: string;
  /** Paired with `text` for ReconciledPreviewText's per-grapheme reveal
   *  during the live phase (Task 2.1 supplies both). */
  stableCharCount?: number;
  /** Live phase: the draft's kept text, shown plain before the live words a
   *  capture appends, so it neither disappears nor replays the reveal. */
  keptText?: string;
  rms?: number;
  peak?: number;
  /** Resting phase only: the countdown shown in the hint slot, e.g. "0:06". */
  countdownHint?: string | null;
  /** Resting phase only: queued responses ("1 in queue"), shown just left of
   *  the countdown on the same line (owner, 2026-10-24). */
  queueHint?: string | null;
  /** Draft phase: clicking the rail enters editing in place (design.md
   *  state 5). */
  onEditStart?: () => void;
  /** Editing phase: controlled textarea value and handlers. */
  editingValue?: string;
  onEditingChange?: (value: string) => void;
  onEditingKeyDown?: (event: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  editingInputRef?: React.RefObject<HTMLTextAreaElement | null>;
  editingAutoFocus?: boolean;
  /** Resting phase only (decision 9, Task 4.7): false renders the locked
   *  blocked-session line in place of the usual "Speak or type" placeholder,
   *  present before the user ever touches push-to-talk. No reason string is
   *  added, matching the tab strip's reason-free ring. */
  canReply?: boolean;
  /** A dictated reply is waiting and auto-submit is off: show the submit
   *  hint. Never set for typed text (owner rule, design.md decision 2). */
  submitHint?: boolean;
  /** Keep the hint row's height even while it is empty, so a spoken draft
   *  arriving does not resize the native window. */
  reserveHint?: boolean;
  /** Why the kept text cannot be sent from here, or that it waits for the
   *  agent's next Stop. Every notice offers Copy. */
  notice?: ReplyNotice | null;
  /** The agent's display name, for the held notice. */
  agentName?: string;
  /** Copies the kept text so the user can recover it elsewhere. */
  onCopy?: () => void;
  /** Resting phase: what the empty field says (the reply hint). Defaults to
   *  the locked "Speak or type"; null says nothing (a retired hint). */
  placeholder?: ReactNode;
  /** The text's height cap before it scrolls. Cards pass the five-line
   *  `REPLY_RAIL_CARD_TEXT_MAX_H`; the console grows to the monitor budget. */
  textMaxH?: number;
  /** Test seam; defaults to the running platform. */
  platform?: ShortcutPlatform;
}

/** Locked placeholder copy for the rail's editable field (Task 1.5). */
const REPLY_RAIL_PLACEHOLDER = "Speak or type";

/** Locked copy (copy-review.md): the resting rail's non-input line when the
 *  active session cannot take a reply at all. A busy agent no longer blocks
 *  the rail (its reply is held), so "for now" is gone. */
export const REPLY_RAIL_BLOCKED_COPY = "Reply in your terminal";

/** Locked copy (copy-review.md, Locked Copy Decisions). */
export const REPLY_RAIL_SUBMIT_HINT = "Press Enter to submit";
export const REPLY_RAIL_SUBMIT_HINT_MAC = "Press ↩ to submit";
export const REPLY_RAIL_SUBMIT_HINT_MAC_SPOKEN = "Press Return to submit";
export const REPLY_RAIL_REFUSED_COPY =
  "Couldn't send. Try again or reply in your terminal.";
/** Copy for reply notices (ux-writing pass, 2026-10-23). */
export const replyRailNotDeliveredCopy = (agentName: string) =>
  `Didn't reach ${agentName}. Reply in your terminal.`;
export const REPLY_RAIL_NO_ROUTE_COPY = "Can't send from here. Reply in your terminal.";
export const REPLY_RAIL_ENDED_COPY = "Session ended";
export const replyRailHeldCopy = (agentName: string) => `Sends when ${agentName} finishes`;
export const REPLY_RAIL_SENDING = "Sending";
export const REPLY_RAIL_COPY_LABEL = "Copy reply";
export const REPLY_RAIL_COPIED = "Copied";

function noticeCopy(notice: ReplyNotice, agentName: string): string {
  switch (notice) {
    case "refused":
      return REPLY_RAIL_REFUSED_COPY;
    case "not_delivered":
      return replyRailNotDeliveredCopy(agentName);
    case "no_route":
      return REPLY_RAIL_NO_ROUTE_COPY;
    case "ended":
      return REPLY_RAIL_ENDED_COPY;
    case "held":
      return replyRailHeldCopy(agentName);
  }
}

/** The rail's text box grows to this many lines and then scrolls.
 *
 *  Live text used to grow without a bound, so a long dictated reply turned the
 *  HUD into a tall moving object over whatever the user was working in; draft
 *  and sent went the other way and hard-clamped with `-webkit-line-clamp`, so
 *  the tail of your own reply was simply unreachable. One scrolling box now
 *  serves both: bounded height, nothing truncated, and while words are still
 *  arriving it stays pinned to the newest line. `CLAUDE_RAIL_MAX_H` (74) is the
 *  whole rail row; this is the text inside it. */
const REPLY_RAIL_FONT_SIZE = 13;
const REPLY_RAIL_LINE_HEIGHT = 1.45;
export const REPLY_RAIL_TEXT_MAX_H = RESPONSE_MAX_H - 180;
/** On the pill and the glance card the reply text grows to five lines, then
 *  scrolls, so their window can hold the tallest field from the start and
 *  typing never resizes it (owner, 2026-10-25). The 2px is the text's own
 *  1px top and bottom padding. */
export const REPLY_RAIL_CARD_TEXT_MAX_H =
  Math.ceil(5 * REPLY_RAIL_FONT_SIZE * REPLY_RAIL_LINE_HEIGHT) + 2;
/** Field box per the accepted prototype: 34px tall at one line. */
const REPLY_RAIL_FIELD_MIN_H = 34;
/** The field's inner inset. It bleeds outside the rail (see the field). */
export const REPLY_RAIL_FIELD_PAD_L = 12;
const REPLY_RAIL_FIELD_PAD_R = 6;
/** The resting row: one faint 10.5px hint line, no field box, the same size
 *  as the pill's hint (owner, 2026-10-24: the 22px row read as a field). */
const REPLY_RAIL_RESTING_MIN_H = 16;
const REPLY_RAIL_HINT_GAP = 5;
const REPLY_RAIL_HINT_MIN_H = 14;
/** The tallest a card's rail gets: the field at its five-line cap (box inset
 *  and border included, rounded up) plus the hint row under it. */
export const REPLY_RAIL_CARD_MAX_H =
  Math.max(14, REPLY_RAIL_FIELD_MIN_H - REPLY_RAIL_TEXT_H) +
  REPLY_RAIL_CARD_TEXT_MAX_H +
  REPLY_RAIL_HINT_GAP +
  REPLY_RAIL_HINT_MIN_H;

/** The one Reply Rail (design.md states 3-6, 8, 9; decisions 23, 24, 26).
 *  Used by the compact pill, the expanded response card, and the question
 *  card. Exactly one textarea lives here — there is no second reply input. */
export function ReplyRail({
  phase,
  text = "",
  stableCharCount = 0,
  keptText = "",
  rms = 0,
  peak = 0,
  countdownHint = null,
  queueHint = null,
  onEditStart,
  editingValue = "",
  onEditingChange,
  onEditingKeyDown,
  editingInputRef,
  editingAutoFocus = false,
  canReply = true,
  submitHint = false,
  reserveHint = false,
  notice = null,
  agentName = "Claude",
  onCopy,
  placeholder,
  textMaxH = REPLY_RAIL_TEXT_MAX_H,
  platform = shortcutPlatform(),
}: ReplyRailProps) {
  // Text on its way, or kept from an ended session, is read-only.
  const readOnly = phase === "sending" || notice === "ended";
  const editing = phase === "editing" && !readOnly;
  const draft = phase === "draft" && !readOnly;
  const shownAsText = readOnly && phase !== "live" && phase !== "processing";
  const refused = notice !== null && notice !== "held";
  const textCeiling = Math.max(
    80, Math.min(textMaxH, claudeCardCeiling(0, RESPONSE_MAX_H) - 180),
  );
  const localEditingRef = useRef<HTMLTextAreaElement | null>(null);
  const inputRef = editingInputRef ?? localEditingRef;

  // An editor that opens focused (the console) gets its caret after any
  // prefilled text: React's autoFocus would leave it before the text.
  useLayoutEffect(() => {
    if (editing && editingAutoFocus) focusFieldAtEnd(inputRef.current);
  }, [editing, editingAutoFocus, inputRef]);

  // Measure prefilled/reconnected drafts too, not just keystrokes, and again
  // when the field's width settles: a draft measured while the window was
  // still narrow would otherwise keep a several-line height.
  useLayoutEffect(() => {
    if (!editing || !inputRef.current) return;
    const el = inputRef.current;
    const fit = () => {
      el.style.height = "auto";
      el.style.height = `${Math.max(REPLY_RAIL_TEXT_H, Math.min(el.scrollHeight, textCeiling))}px`;
      el.style.overflowY = el.scrollHeight > textCeiling ? "auto" : "hidden";
    };
    fit();
    // A web font arriving after the first measure rewraps the same text at
    // the same width, which the width observer below cannot see.
    let live = true;
    void document.fonts?.ready.then(() => {
      if (live) fit();
    });
    if (typeof ResizeObserver === "undefined") {
      return () => {
        live = false;
      };
    }
    let width = el.clientWidth;
    const observer = new ResizeObserver(() => {
      if (el.clientWidth === width) return;
      width = el.clientWidth;
      fit();
    });
    observer.observe(el);
    return () => {
      live = false;
      observer.disconnect();
    };
  }, [editing, editingValue, inputRef, textCeiling]);

  // Inline state slot. Sending and dismissal are direct keyboard actions, so
  // the rail does not spend permanent chrome explaining them; the sent state
  // and a live countdown still communicate actual state.
  // Sent always shows its label, because the check now lives beside it.
  const inlineState =
    phase === "sending"
      ? REPLY_RAIL_SENDING
      : phase === "sent"
        ? "Sent"
        : phase === "live" || phase === "processing" || editing || draft || shownAsText
          ? null
          : countdownHint;

  // Words arrive a grapheme at a time, so the newest line has to be brought
  // into view on every revision or the box would settle showing the first three
  // lines of a reply the user is still speaking.
  const textBoxRef = useRef<HTMLDivElement | null>(null);
  const followsNewest = phase === "live" || phase === "processing";
  useLayoutEffect(() => {
    if (!followsNewest) return;
    const box = textBoxRef.current;
    if (!box) return;
    box.scrollTop = box.scrollHeight;
  }, [followsNewest, text, stableCharCount]);

  const handleEditingInput = (
    event: React.ChangeEvent<HTMLTextAreaElement>,
  ) => {
    const el = event.currentTarget;
    // Grow within the monitor's reading budget; the receipt remains outside.
    el.style.height = "auto";
    el.style.height = `${Math.max(REPLY_RAIL_TEXT_H, Math.min(el.scrollHeight, textCeiling))}px`;
    el.style.overflowY = el.scrollHeight > textCeiling ? "auto" : "hidden";
    onEditingChange?.(el.value);
  };

  const blocked = phase === "resting" && !canReply;
  // Only typing draws the field box (owner, 2026-10-24): at rest the row is
  // just the faint reply hint, since most replies are spoken, and clicking it
  // opens the box in place.
  const framed = editing;
  const resting = phase === "resting";
  // A retired hint (null placeholder) hands its spot to the queue depth, the
  // same place the pill puts it (owner, 2026-10-24).
  const queueInHintSpot = resting && placeholder === null && queueHint !== null;
  const clickToEdit = draft || (phase === "resting" && canReply && !readOnly);
  const showKeyCap = phase !== "sent" && !resting && !blocked && !readOnly;
  const keyCapLit = editing
    ? editingValue.trim().length > 0
    : draft && text.trim().length > 0;

  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);

  // Copy sits in the field's trailing slot, beside the text it copies, so it
  // keeps one place in every notice state: next to the key cap while the text
  // stays editable, and alone in that slot once an ended session or a send in
  // flight removes the key cap. The notice line below only says why.
  const showCopy = notice !== null && onCopy !== undefined;
  const copyLabel = copied ? REPLY_RAIL_COPIED : REPLY_RAIL_COPY_LABEL;

  // One hint row under the field. A notice outranks the submit hint. The
  // Copy button sits outside this live region, so its confirmation is
  // announced here.
  const hint: ReactNode = notice !== null ? (
    <>
      {noticeCopy(notice, agentName)}
      {copied && <span className="sr-only">{REPLY_RAIL_COPIED}</span>}
    </>
  ) : submitHint && draft ? (
    platform === "macos" ? (
      <>
        <span aria-hidden="true">{REPLY_RAIL_SUBMIT_HINT_MAC}</span>
        <span className="sr-only">{REPLY_RAIL_SUBMIT_HINT_MAC_SPOKEN}</span>
      </>
    ) : (
      REPLY_RAIL_SUBMIT_HINT
    )
  ) : null;
  // Resting reserves nothing: the submit hint's row arrives with the draft it
  // describes, not as an empty band under the resting hint.
  const hintRow = hint !== null || (reserveHint && !resting);

  return (
    <div
      data-testid="claude-reply-rail"
      // Only a click-to-edit rail takes focus itself. Otherwise a click on the
      // field's padding would focus this box and the next keys would go nowhere.
      tabIndex={clickToEdit ? 0 : undefined}
      role={clickToEdit ? "button" : undefined}
      aria-label={clickToEdit ? "Reply to agent" : undefined}
      onKeyDown={(event) => {
        if (event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) {
          event.preventDefault();
          onEditStart?.();
        }
      }}
      data-hud-interactive="true"
      data-hud-text-field={clickToEdit || editing ? "true" : undefined}
      // While editing, the whole rail is the text box: a press on the field's
      // padding, beside a short line or on the hint row keeps the caret in
      // the textarea instead of dropping focus to the page.
      onMouseDown={
        editing
          ? (event) => {
              if (event.target === inputRef.current) return;
              if (event.target instanceof Element && event.target.closest("button")) return;
              event.preventDefault();
              focusFieldAtEnd(inputRef.current);
            }
          : undefined
      }
      onPointerDown={(event) => event.stopPropagation()}
      onClick={clickToEdit ? onEditStart : undefined}
    >
      <div
        data-testid="claude-reply-field"
        data-framed={framed ? "true" : "false"}
        data-click-to-edit={clickToEdit ? "true" : "false"}
        className="hud-reply-field flex items-end"
        style={{
          gap: 8,
          minHeight: resting ? REPLY_RAIL_RESTING_MIN_H : REPLY_RAIL_FIELD_MIN_H,
          // Reply text starts where the agent's text does, in every phase.
          // The field's box bleeds outward by its own inset instead of
          // pushing the text in, so typing frames the text where it already
          // stands and the trailing slot ends on the content's right edge.
          padding: resting ? 0 : `6px ${REPLY_RAIL_FIELD_PAD_R}px 6px ${REPLY_RAIL_FIELD_PAD_L}px`,
          margin: resting ? 0 : `0 ${-(REPLY_RAIL_FIELD_PAD_R + 1)}px 0 ${-(REPLY_RAIL_FIELD_PAD_L + 1)}px`,
          borderRadius: 14,
          // Spoken text keeps the same box and inset with no fill or border,
          // so it does not read as typed input until the user clicks in.
          background: framed ? "var(--surface-control)" : "transparent",
          border: `1px solid ${framed ? "var(--border-hairline)" : "transparent"}`,
          transition: "background-color 0.15s, border-color 0.15s",
          cursor: clickToEdit ? "text" : "default",
        }}
      >
        <div
          ref={textBoxRef}
          data-testid="claude-reply-rail-text"
          className="min-w-0 flex-1"
          style={{
            fontFamily: "var(--font-agent)",
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            fontSize: REPLY_RAIL_FONT_SIZE,
            fontWeight: 400,
            lineHeight: REPLY_RAIL_LINE_HEIGHT,
            color: "var(--color-slate)",
            opacity: phase === "processing" || phase === "sent" || phase === "sending" ? 0.72 : 1,
            minHeight: resting ? REPLY_RAIL_RESTING_MIN_H : REPLY_RAIL_TEXT_H,
            padding: editing || resting ? 0 : "1px 0",
            // One bounded, scrolling box for every read-only phase. The
            // textarea owns its own growth in the editing phase, so it is left
            // alone here.
            ...(editing
              ? null
              : {
                  maxHeight: textCeiling,
                  overflowY: "auto" as const,
                  overscrollBehavior: "contain" as const,
                }),
          }}
        >
          {phase === "live" ? (
            <>
              {keptText !== "" && (
                <span data-testid="claude-reply-kept-text">{`${keptText} `}</span>
              )}
              <ReconciledPreviewText
                text={text}
                stableCharCount={stableCharCount}
              />
            </>
          ) : editing ? (
            <textarea
              ref={inputRef}
              aria-label="Edit your reply"
              placeholder={REPLY_RAIL_PLACEHOLDER}
              rows={1}
              tabIndex={0}
              data-hud-interactive="true"
              value={editingValue}
              onChange={handleEditingInput}
              onKeyDown={onEditingKeyDown}
              onPointerDown={(event) => event.stopPropagation()}
              className="block w-full resize-none bg-transparent outline-none"
              style={{
                font: "inherit",
                color: "inherit",
                padding: "1px 0",
                height: REPLY_RAIL_TEXT_H,
                minHeight: REPLY_RAIL_TEXT_H,
                maxHeight: textCeiling,
                overflowY: "hidden",
                caretColor: "var(--color-frost)",
                outline: "none",
                boxShadow: "none",
                WebkitAppearance: "none",
              }}
            />
          ) : text || (phase === "editing" && readOnly && editingValue) ? (
            text || editingValue
          ) : blocked ? (
            <span style={{ color: "var(--color-steel)" }}>
              {REPLY_RAIL_BLOCKED_COPY}
            </span>
          ) : (
            <span
              style={{
                ...hudMetaText(),
                display: "block",
                lineHeight: `${REPLY_RAIL_RESTING_MIN_H}px`,
              }}
            >
              {placeholder === undefined
                ? REPLY_RAIL_PLACEHOLDER
                : placeholder ?? (queueInHintSpot ? queueHint : null)}
            </span>
          )}
        </div>

        {resting && queueHint !== null && !queueInHintSpot && (
          <span
            className="shrink-0"
            data-testid="claude-reply-queue"
            style={{
              ...hudMetaText(),
              lineHeight: `${REPLY_RAIL_RESTING_MIN_H}px`,
            }}
          >
            {queueHint}
          </span>
        )}

        {inlineState !== null && (
          <span
            className="flex shrink-0 items-center"
            data-testid="claude-reply-inline-state"
            style={{
              // The sent check and its label read as one group.
              gap: 4,
              // On the last text line, like the glyph and the key cap.
              lineHeight: `${resting ? REPLY_RAIL_RESTING_MIN_H : REPLY_RAIL_TEXT_H}px`,
              ...hudMetaText(),
              // The countdown is quiet status; "Sending" and "Sent" report
              // an outcome and stay at full strength.
              ...(resting ? null : { opacity: 1 }),
            }}
          >
            {phase === "sent" && (
              <span
                data-testid="claude-reply-sent-glyph"
                style={{ color: "var(--color-frost-deep)", display: "flex" }}
              >
                <ReplySentGlyph size={13} />
              </span>
            )}
            {inlineState}
          </span>
        )}

        {showCopy && (
          <ReplyCopyButton
            copied={copied}
            label={copyLabel}
            onCopy={() => {
              onCopy?.();
              setCopied(true);
            }}
          />
        )}

        {phase === "live" || phase === "processing" ? (
          <ReplyRailIndicator phase={phase} rms={rms} peak={peak} />
        ) : (
          showKeyCap && <EnterKeyCap lit={keyCapLit} platform={platform} />
        )}
      </div>

      {hintRow && (
        <div
          data-testid="claude-reply-hint"
          data-state={notice !== null ? notice : hint !== null ? "submit" : "reserved"}
          aria-live="polite"
          className="text-right"
          style={{
            marginTop: REPLY_RAIL_HINT_GAP,
            minHeight: REPLY_RAIL_HINT_MIN_H,
            paddingRight: 4,
            fontFamily: "var(--font-agent)",
            fontSize: 10.5,
            fontWeight: 500,
            lineHeight: `${REPLY_RAIL_HINT_MIN_H}px`,
            color: refused ? "var(--color-rose-text)" : "var(--color-steel)",
            visibility: hint === null ? "hidden" : "visible",
          }}
        >
          {hint}
        </div>
      )}
    </div>
  );
}
