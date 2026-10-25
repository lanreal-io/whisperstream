import {
  type AgentActiveResponse,
  type AgentName,
  type AgentActiveQuestion,
  type AgentInteraction,
} from "../../lib/socket/agentEvents";
import { type EnhancementTimeoutProvider } from "../../lib/socket/events";
import { type DictationState, type DictationModel } from "./dictationState";
import { claudeIdentityPhrase } from "../../lib/sessionName";

export type HudMode = "hidden" | "control" | "pill" | "preview" | "panel";
export type ReplyWindowState = "available" | "engaged";

export interface AmbientReplyWindow {
  state: ReplyWindowState;
  remainingSeconds: number;
  progress: number;
}

/** The rail phase of the draft a surface shows, derived from the reply
 *  owner's draft and the HUD's local editing state. */
export type ReplyPhase = "passive" | "recording" | "processing" | "draft_ready" | "editing" | "sending";

/** Why kept text cannot be sent from here (each offers Copy), or that it is
 *  waiting for the agent's next Stop. */
export type ReplyNotice = "refused" | "not_delivered" | "no_route" | "ended" | "held";

export interface AmbientReplyDraft {
  phase: "draft_ready" | "editing" | "sending";
  generation: number;
  text: string;
}

/** The one reply-phase mapping shared by the compact pill and the expanded
 *  card, so the two surfaces can never disagree about which rail phase a
 *  response is in. `null` means "nothing to say": compact stays one row,
 *  expanded falls back to the resting rail. */
export function deriveReplyRailPhase(
  replyDraft: AmbientReplyDraft | null,
  replyPhase: ReplyPhase | undefined,
  sentForThisCompletion: boolean,
): Exclude<ReplyRailPhase, "resting"> | null {
  if (sentForThisCompletion) return "sent";
  const phase = replyDraft?.phase ?? replyPhase;
  if (phase === "recording") return "live";
  if (phase === "processing") return "processing";
  if (phase === "draft_ready") return "draft";
  if (phase === "editing") return "editing";
  if (phase === "sending") return "sending";
  return null;
}
type TransientPillState =
  | "blocked"
  | "enhance_timeout"
  | "enhance_error";
// Iteration 03: Claude merely working has NO HUD presentation. The pill appears
// only for a question, a permission request, or a final response.
type ClaudeAttentionState = "permission";

interface ClaudeHudAttention {
  state: ClaudeAttentionState;
  remainingSeconds: number | null;
  detail: string | null;
  agent: AgentName;
  /** The opaque interaction id an Allow/Deny/provider-handoff action resolves.
   *  Not content: `AgentInteraction.request_id` already crosses the wire
   *  content-free (design.md state 10). */
  requestId: string;
  /** Already resolved and sanitized at the hook seam (hud-session-name Task
   *  3.6). `null` when the chain resolved nothing; the call site falls back
   *  to the literal `Claude`. */
  sourceName: string | null;
}

export interface TimedTransient {
  state: TransientPillState;
  generation: number;
  reason?: "eula_acceptance_required";
  /** Bounded provider identity for enhancement outcomes only. No model name, no
   *  host, no message text crosses the wire (transparent-indicators Task 2.2). */
  provider?: EnhancementTimeoutProvider;
}

type HudPresentation =
  | { mode: "hidden" }
  | { mode: "control"; state: "seed" | "launcher" | "arming" }
  | {
      mode: "pill";
      state: DictationState | TransientPillState | ClaudeAttentionState | "idle";
      blockedReason?: "eula_acceptance_required";
      timeoutProvider?: EnhancementTimeoutProvider;
      errorProvider?: EnhancementTimeoutProvider;
    }
  | { mode: "preview"; phase: "recording" | "processing" }
  | { mode: "question_pill" }
  | { mode: "question" }
  | { mode: "panel" };

export function ambientReplyWindow(
  active: AgentActiveResponse | null,
): AmbientReplyWindow | null {
  if (active === null || !active.routable) return null;
  const remainingSeconds = Math.max(
    0,
    Math.ceil(
      active.engaged
        ? active.hard_remaining_seconds
        : active.passive_remaining_seconds,
    ),
  );
  if (remainingSeconds === 0) return null;
  const totalSeconds = active.engaged
    ? 120
    : Math.max(1, active.passive_total_seconds);
  return {
    state: active.engaged ? "engaged" : "available",
    remainingSeconds,
    progress: Math.min(1, remainingSeconds / totalSeconds),
  };
}

export function compactQuestionPillProps(active: AgentActiveQuestion): {
  sourceName: string;
  accessibleLabel: string;
  title: string;
} {
  const question =
    active.questions[active.current_index] ?? active.questions[0];
  const secondary =
    active.phase === "surfaced"
      ? "Waiting for Claude"
      : active.phase === "answered"
        ? "Answer sent"
      : active.phase === "speaking"
        ? "Speaking"
        : active.phase === "expired"
          ? "Answer in Claude Code"
          : active.phase === "held_failed"
            ? "Claude was not ready. Answer in Claude Code."
            : active.remaining_seconds > 0
              ? `${Math.ceil(active.remaining_seconds)}s to answer`
              : question.question;
  const sourceName = active.source_name ?? CLAUDE_FALLBACK_NAME;
  return {
    sourceName,
    accessibleLabel: `${claudeIdentityPhrase(sourceName)} asked. ${secondary}. ${question.question}`,
    title: question.question,
  };
}

export function currentClaudeAttention(
  interactions: AgentInteraction[],
): ClaudeHudAttention | null {
  const interaction = interactions
    .filter(
      (candidate) =>
        candidate.kind === "permission" &&
        candidate.status === "pending",
    )
    .sort((left, right) => right.sequence - left.sequence)[0];
  if (interaction === undefined) {
    // Iteration 03: no pending permission means no attention state. Claude
    // activity deliberately produces nothing.
    return null;
  }
  return {
    state: "permission",
    agent: interaction.agent ?? "claude",
    remainingSeconds: Math.max(
      0,
      Math.ceil(interaction.remaining_seconds),
    ),
    // No detail text crosses the wire yet: AgentInteraction is deliberately
    // content-free (lib/socket/agentEvents.ts). ClaudePermissionPill renders without a
    // detail line until a future round adds one.
    detail: null,
    requestId: interaction.request_id,
    sourceName: interaction.source_name ?? null,
  };
}

export interface PresentationInputs {
  dictation: DictationModel;
  transient: TimedTransient | null;
  claudeAttention: ClaudeHudAttention | null;
  activeQuestion: AgentActiveQuestion | null;
  questionExpanded: boolean;
  hasCompletion: boolean;
  persistentControlVisible: boolean;
  persistentControlExpanded: boolean;
  idleScreenReaderDetected: boolean;
  startCommandPending: boolean;
  finishVisualHold: boolean;
  dedicatedStartPendingEligible?: boolean;
  startupArmingVisible?: boolean;
}

export function derivePresentation({
  dictation,
  transient,
  claudeAttention,
  activeQuestion,
  questionExpanded,
  hasCompletion,
  persistentControlVisible,
  persistentControlExpanded,
  idleScreenReaderDetected,
  startCommandPending,
  finishVisualHold,
  dedicatedStartPendingEligible = false,
  startupArmingVisible = false,
}: PresentationInputs): HudPresentation {
  // A reply capture is a reply for its whole recording, never ordinary
  // dictation. This reads only the recording's own record: deriving it from
  // a second, separately ordered message (the reply snapshot) flashed the
  // recording pill on press and release.
  const replyOwnsDictation = dictation.composeGeneration !== null;
  if (dictation.phase !== "idle" && !replyOwnsDictation) {
    if (!dictation.hudEnabled || dictation.screenReaderDetected) {
      return { mode: "hidden" };
    }
    // Live Preview owns the complete recording lifecycle. Show its full card
    // immediately, even before the first text revision, and retain it through
    // processing instead of falling back to the compact pill.
    if (dictation.previewEnabled) {
      return { mode: "preview", phase: dictation.phase };
    }
    if (
      dictation.composePreview &&
      (dictation.phase === "recording" || dictation.previewRevealed)
    ) {
      return { mode: "preview", phase: dictation.phase };
    }
    return { mode: "pill", state: dictation.phase };
  }
  if (activeQuestion !== null) {
    if (questionExpanded) return { mode: "question" };
    return { mode: "question_pill" };
  }
  if (transient !== null) {
    return {
      mode: "pill",
      state: transient.state,
      blockedReason: transient.reason,
      timeoutProvider: transient.provider,
      errorProvider: transient.provider,
    };
  }
  if (
    startupArmingVisible &&
    dictation.hudEnabled &&
    !idleScreenReaderDetected
  ) {
    return { mode: "pill", state: "processing" };
  }
  if (
    finishVisualHold &&
    dictation.hudEnabled &&
    !dictation.screenReaderDetected
  ) {
    return { mode: "pill", state: "processing" };
  }
  // Arming: the click has been accepted locally but no authoritative
  // `dictation.state` has confirmed a recording yet. It outranks the completion
  // panel because the user just acted on this surface, and it
  // sits below the blocked/error transient so a rejected start still shows its
  // own treatment. Both the shared fallback and dedicated pending event map to
  // the existing start-command state, whose bounded timeout prevents stranding.
  if (
    startCommandPending &&
    (persistentControlVisible || dedicatedStartPendingEligible) &&
    !idleScreenReaderDetected
  ) {
    return { mode: "control", state: "arming" };
  }
  if (claudeAttention !== null) {
    return { mode: "pill", state: claudeAttention.state };
  }
  if (hasCompletion) return { mode: "panel" };
  if (persistentControlVisible && !idleScreenReaderDetected) {
    return {
      mode: "control",
      state: persistentControlExpanded ? "launcher" : "seed",
    };
  }
  return { mode: "hidden" };
}

export const CLAUDE_FALLBACK_NAME = "Claude";

export type ReplyRailPhase =
  | "resting"
  | "live"
  | "processing"
  | "draft"
  | "editing"
  | "sending"
  | "sent";

// Shared paint fade and native hide tail; the coordinator owns the timer.
export const HIDE_ANIMATION_MS = 300;
