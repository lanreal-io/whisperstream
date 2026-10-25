import {
  type AgentActiveQuestion,
  type AgentQuestion,
  type AgentName,
} from "../../../lib/socket/agentEvents";
import { type DictationState } from "../dictationState";
import {
  type MeasuredClaudeCard,
  claudeCardCeiling,
  QUESTION_MIN_H,
  QUESTION_MAX_H,
  CLAUDE_CARD_W,
  CLAUDE_CARD_RADIUS,
  CLAUDE_PERMISSION_PILL_RADIUS,
  CLAUDE_PERMISSION_PILL_W,
  CLAUDE_PERMISSION_MIN_H,
  CLAUDE_PERMISSION_MAX_H,
} from "../geometry";
import { CLAUDE_FALLBACK_NAME, type ReplyRailPhase } from "../presentation";
import { useEffect, type CSSProperties, useRef, useState, useLayoutEffect } from "react";
import { useMeasuredClaudeCard } from "../components/useMeasuredHudCard";
import { motion } from "framer-motion";
import {
  CLAUDE_CARD_PAD_TOP,
  CLAUDE_CARD_PAD_X,
  CLAUDE_CARD_PAD_BOTTOM,
  CLAUDE_BODY_GUTTER,
} from "../responseLayout";
import {
  claudeIdentityPhrase,
  agentIdentityPhrase,
  agentProviderLabel,
} from "../../../lib/sessionName";
import { ClaudeIdentityRow, Spine } from "../components/AgentIdentity";
import { ReplyRail } from "../components/ReplyRail";

interface QuestionPanelProps {
  active: AgentActiveQuestion;
  questionIndex: number;
  selected: Record<string, string[]>;
  freeform: Record<string, string>;
  /** Live interim speech for this exact question, or null when not dictating. */
  interimText: string | null;
  /** Paired with interimText for the rail's live ReconciledPreviewText reveal
   *  (Task 1.5's stableCharCount thread). */
  interimStableCharCount?: number;
  /** Reply capture phase for this exact question. */
  replyPhase: DictationState;
  rms?: number;
  peak?: number;
  onOption: (question: AgentQuestion, label: string) => void;
  onFreeform: (question: string, text: string) => void;
  onSubmit: () => void;
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  /** Reports the painted-card rect into the HUD's one `measuredCardSize`
   *  state through the same routine the expanded response uses
   *  (decision 17). */
  onMeasuredCardSizeChange?: (measured: MeasuredClaudeCard) => void;
}

// --- Question card (Task 2.3, design.md state 9) ---
// The same object as the expanded response (design decision 8, 23): the
// shared ClaudeIdentityRow card form, the Spine on Claude's own words (the
// question text), and the identical ReplyRail. The initial question
// presentation stays compact; this card mounts only for the exact generation
// the user expands (or a routed capture expands so its live reply is visible).
// There is no second reply input anywhere in this round.
const QUESTION_OPTIONS_MAX_H = 172; // ~5 visible rows, then internal scroll.
const QUESTION_GAP_IDENTITY_BODY = 16;
// Matched to the expanded card's rhythm (CLAUDE_CARD_GAP_BODY_DIVIDER /
// CLAUDE_CARD_GAP_DIVIDER_RAIL) so the two surfaces read as one object.
const QUESTION_GAP_BODY_OPTIONS = 18;
const QUESTION_GAP_OPTIONS_RAIL = 14;
const QUESTION_GAP_RAIL_FOOTER = 10;

export function QuestionPanel({
  active,
  questionIndex,
  selected,
  freeform,
  interimText,
  interimStableCharCount = 0,
  replyPhase,
  rms = 0,
  peak = 0,
  onOption,
  onFreeform,
  onSubmit,
  inputRef,
  onMeasuredCardSizeChange,
}: QuestionPanelProps) {
  // The opaque compose lease id (`active.session_handle`) must never paint
  // (design decision 8); degrade to the literal fallback, never to an id.
  const sourceName = active.source_name ?? CLAUDE_FALLBACK_NAME;
  const question = active.questions[questionIndex] ?? active.questions[0];
  const draft = freeform[question.question] ?? "";
  const selectedLabels = selected[question.question] ?? [];
  const listening = replyPhase === "recording";
  const processing = replyPhase === "processing";

  // One footer status line. Keyboard instructions are deliberately omitted;
  // the fallback is empty when no lifecycle state needs communicating.
  const action = listening
    ? "Listening"
    : processing
      ? "Processing"
      : active.phase === "surfaced"
        ? "Waiting for Claude"
        : active.phase === "expired"
          ? "Answer in Claude Code"
          : active.phase === "held_failed"
            ? "Claude was not ready"
            : null;

  // The identical ReplyRail (decision 23): live while listening, processing
  // while transcribing, editing the rest of the time so the user can always
  // type directly. There is no separate draft/click-to-edit step for
  // questions (unlike the response's auto-submit flow), so "editing" doubles
  // as the question rail's resting state.
  const railPhase: ReplyRailPhase = listening
    ? "live"
    : processing
      ? "processing"
      : "editing";
  const railText = listening || processing ? (interimText ?? "") : draft;

  // The shared ReplyRail only mounts the editable textarea in the "editing"
  // phase; keep the load-bearing data-question-freeform marker on it so the
  // existing arrow-key cycling (querySelectorAll below) still finds it.
  useEffect(() => {
    inputRef.current?.setAttribute("data-question-freeform", "true");
  }, [inputRef, railPhase]);

  const ceiling = claudeCardCeiling(QUESTION_MIN_H, QUESTION_MAX_H);

  // The SAME measurement routine as the expanded response (decision 17): one
  // painted-card measure feeding one measuredCardSize, which feeds both the
  // CSS box and the native bounds.
  const { cardRef } = useMeasuredClaudeCard("question", onMeasuredCardSizeChange);

  const microStyle: CSSProperties = {
    fontFamily: "var(--font-agent)",
    fontSize: 10.5,
    fontWeight: 500,
    letterSpacing: "0.01em",
    color: "var(--color-steel)",
  };

  return (
    <motion.section
      ref={cardRef}
      data-testid="agent-question-panel"
      data-claude-card-measure="question"
      data-question-generation={active.generation}
      className="relative flex flex-col overflow-hidden"
      style={{
        width: CLAUDE_CARD_W,
        minHeight: QUESTION_MIN_H,
        maxHeight: ceiling,
        borderRadius: CLAUDE_CARD_RADIUS,
        padding: `${CLAUDE_CARD_PAD_TOP}px ${CLAUDE_CARD_PAD_X}px ${CLAUDE_CARD_PAD_BOTTOM}px`,
        background: "var(--surface-overlay)",
        border: "1px solid var(--hud-desktop-border)",
        boxShadow: "var(--hud-card-elevation)",
      }}
      onPointerDown={(event) => event.stopPropagation()}
      data-hud-interactive="true"
      aria-label={`${claudeIdentityPhrase(sourceName)} asked a question`}
    >
      <ClaudeIdentityRow
        variant="card"
        sourceName={sourceName}
      />

      {/* Question text carries the spine: it is Claude's words (decision 17,
          "spine = Claude spoke"). */}
      <div
        className="relative min-h-0 shrink-0"
        style={{
          marginTop: QUESTION_GAP_IDENTITY_BODY,
          paddingLeft: CLAUDE_BODY_GUTTER,
        }}
      >
        <Spine segments={[1]} speakingIndex={null} />
        <p
          className="m-0 whitespace-pre-wrap"
          data-testid="agent-question-copy"
          style={{
            fontFamily: "var(--font-quote)",
            fontSize: 14,
            fontWeight: 400,
            lineHeight: 1.5,
            letterSpacing: "-0.006em",
            color: "var(--color-slate)",
          }}
        >
          {question.question}
        </p>
      </div>

      {/* The one place internal scrolling is expected rather than
          exceptional: a list of controls, capped at ~5 visible rows. The
          negative margin keeps layout identical while giving the app-wide
          2px focus outline room to render inside the scroller. */}
      <div
        className="-mx-1 min-h-0 shrink-0 space-y-1 overflow-y-auto px-1 py-1"
        style={{
          marginTop: QUESTION_GAP_BODY_OPTIONS,
          maxHeight: QUESTION_OPTIONS_MAX_H,
        }}
      >
        {question.options.map((option, index) => {
          const checked = selectedLabels.includes(option.label);
          return (
            <button
              key={option.label}
              type="button"
              className="flex w-full items-start gap-2.5 text-left"
              style={{
                borderRadius: 12,
                padding: "8px 10px",
                border: "1px solid var(--border-hairline)",
                background: checked ? "var(--surface-control)" : "transparent",
                boxShadow: checked
                  ? "inset 2px 0 0 0 var(--color-frost-deep)"
                  : "none",
              }}
              aria-pressed={checked}
              onClick={() => onOption(question, option.label)}
              data-hud-interactive="true"
              data-question-option="true"
            >
              <span
                className="mt-[3px] shrink-0"
                style={{
                  fontFamily: "var(--font-agent)",
                  fontSize: 10.5,
                  fontWeight: 500,
                  color: "var(--color-steel)",
                  fontVariantNumeric: "tabular-nums",
                }}
              >
                {index + 1}
              </span>
              <span className="min-w-0">
                <span
                  className="block"
                  style={{
                    fontFamily: "var(--font-agent)",
                    fontSize: 13,
                    lineHeight: "17px",
                    color: "var(--color-slate)",
                  }}
                >
                  {option.label}
                </span>
                {option.description && (
                  <span
                    className="mt-0.5 block"
                    style={{
                      fontFamily: "var(--font-agent)",
                      fontSize: 11,
                      lineHeight: "14px",
                      color: "var(--color-steel)",
                    }}
                  >
                    {option.description}
                  </span>
                )}
              </span>
            </button>
          );
        })}
      </div>

      <div
        className="shrink-0"
        style={{ marginTop: QUESTION_GAP_OPTIONS_RAIL }}
      >
        <ReplyRail
          phase={railPhase}
          text={railText}
          stableCharCount={interimStableCharCount}
          rms={rms}
          peak={peak}
          editingValue={draft}
          onEditingChange={(text) => onFreeform(question.question, text)}
          onEditingKeyDown={(event) => {
            if (event.nativeEvent.isComposing) {
              event.stopPropagation();
              return;
            }
            const digit = Number(event.key);
            if (
              !event.altKey &&
              !event.ctrlKey &&
              !event.metaKey &&
              Number.isInteger(digit) &&
              digit >= 1 &&
              digit <= question.options.length
            ) {
              event.preventDefault();
              event.stopPropagation();
              onOption(question, question.options[digit - 1].label);
              return;
            }
            // Plain Enter only: modified Enter and IME composition belong to
            // the text box.
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.ctrlKey &&
              !event.altKey &&
              !event.metaKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              event.stopPropagation();
              onSubmit();
            }
          }}
          editingInputRef={inputRef}
          editingAutoFocus
        />
      </div>

      <footer
        className="flex shrink-0 items-center justify-between gap-3"
        style={{ ...microStyle, marginTop: QUESTION_GAP_RAIL_FOOTER }}
      >
        <span data-testid="agent-question-action">{action}</span>
        <span>Esc to cancel</span>
      </footer>
    </motion.section>
  );
}

// --- ClaudePermissionPill (Task 2.4, design.md state 10, Reference Code
// section F) ---
// The permission ask is the compact pill plus a rail, NOT a card: width 520,
// radius 20, natural height, min 74, max 128 (CLAUDE_PERMISSION_PILL_W /
// _RADIUS / _MIN_H / _MAX_H, Task 1.2). Row 1 is the compact identity row and
// shows the resolved session name (scope decision 3, hud-session-name):
// against a *session* name, which session is asking for Bash access is the
// single most decision-relevant fact on this surface. No countdown bar
// (decision 34): 120 seconds of crawling progress is anxiety furniture and
// the decision is not time-pressured in any way the user can act on.
/** The detail line carries the spine only at 2 lines or more (design.md
 *  state 10): a 2px rule beside one short line is noise, not structure.
 *  Measures the rendered line-clamp height against one line's height rather
 *  than counting characters, so it stays correct across font metrics. */
function ClaudePermissionDetail({ detail }: { detail: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [multiline, setMultiline] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const lineHeight = parseFloat(getComputedStyle(el).lineHeight || "0");
    setMultiline(lineHeight > 0 && el.scrollHeight > lineHeight * 1.5);
  }, [detail]);

  return (
    <div
      className="relative min-h-0"
      style={{ paddingLeft: multiline ? CLAUDE_BODY_GUTTER : 0 }}
    >
      {multiline && <Spine segments={[1]} speakingIndex={null} />}
      <p
        ref={ref}
        className="m-0"
        data-testid="claude-permission-detail"
        style={{
          fontFamily: "var(--font-quote)",
          fontSize: 13,
          lineHeight: 1.4,
          color: "var(--color-slate)",
          display: "-webkit-box",
          WebkitLineClamp: 2,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
        }}
      >
        {detail}
      </p>
    </div>
  );
}

export function ClaudePermissionPill({
  provider = "claude",
  sourceName,
  detail,
  onAllow,
  onDeny,
  onDefer,
  onMeasuredCardSizeChange,
}: {
  provider?: AgentName;
  sourceName: string;
  detail: string | null;
  onAllow: () => void;
  onDeny: () => void;
  onDefer: () => void;
  /** Reports the painted-pill rect into the HUD's one `measuredCardSize`
   *  state, which sizes this mode's window and shapes its native region. */
  onMeasuredCardSizeChange?: (measured: MeasuredClaudeCard) => void;
}) {
  const { cardRef } = useMeasuredClaudeCard(
    "permission",
    onMeasuredCardSizeChange,
    CLAUDE_PERMISSION_PILL_RADIUS,
  );
  const phrase = agentIdentityPhrase(provider, sourceName);
  return (
    <div
      ref={cardRef as React.RefObject<HTMLDivElement>}
      data-claude-card-measure="permission"
      className="relative overflow-hidden flex flex-col"
      role="status"
      aria-atomic="true"
      aria-label={
        detail === null
          ? `${phrase} needs approval`
          : `${phrase} needs approval. ${detail}`
      }
      data-testid="claude-permission-pill"
      style={{
        width: CLAUDE_PERMISSION_PILL_W,
        minHeight: CLAUDE_PERMISSION_MIN_H,
        maxHeight: CLAUDE_PERMISSION_MAX_H,
        borderRadius: CLAUDE_PERMISSION_PILL_RADIUS,
        background: "var(--surface-overlay)",
        border: "1px solid var(--hud-desktop-border)",
        boxShadow: "var(--hud-card-elevation)",
      }}
    >
      <ClaudeIdentityRow
        variant="compact"
        provider={provider}
        sourceName={sourceName}
      />
      <div
        aria-hidden="true"
        style={{ height: 1, margin: "0 14px", background: "var(--divider)" }}
      />
      <div
        className="flex flex-col"
        style={{ padding: "6px 14px 10px", gap: 8 }}
      >
        {detail !== null && <ClaudePermissionDetail detail={detail} />}
        <div className="flex items-center" style={{ gap: 8 }}>
          <button
            type="button"
            tabIndex={-1}
            data-hud-interactive="true"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={onDefer}
            style={{
              fontFamily: "var(--font-agent)",
              fontSize: 10.5,
              fontWeight: 500,
              color: "var(--color-steel)",
              background: "none",
              border: "none",
              padding: 0,
              cursor: "pointer",
            }}
          >
            Decide in {agentProviderLabel(provider)}
          </button>
          <span className="ml-auto" />
          <button
            type="button"
            tabIndex={-1}
            data-hud-interactive="true"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={onDeny}
            style={{
              height: 26,
              borderRadius: 13,
              padding: "0 12px",
              fontFamily: "var(--font-agent)",
              fontSize: 12,
              fontWeight: 600,
              color: "var(--color-steel)",
              background: "var(--surface-control)",
              border: "1px solid var(--border-hairline)",
              cursor: "pointer",
            }}
          >
            Deny
          </button>
          <button
            type="button"
            tabIndex={-1}
            data-hud-interactive="true"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={onAllow}
            style={{
              height: 26,
              borderRadius: 13,
              padding: "0 14px",
              fontFamily: "var(--font-agent)",
              fontSize: 12,
              fontWeight: 600,
              color: "white",
              background: "var(--accent-fill)",
              border: "1px solid var(--border-accent)",
              cursor: "pointer",
            }}
          >
            Allow
          </button>
        </div>
      </div>
    </div>
  );
}

