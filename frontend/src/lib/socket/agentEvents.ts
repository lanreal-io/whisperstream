import { isPlainRecord, containsForbiddenAgentField, hasOnlyKeys, isOpaqueAgentId, cloneJsonRecord, isSequence, isNonNegativeFinite, isPositiveSequence, isBoundedString, isSafeSourceLabel, isBoundedNonBlankString } from "./validation";


export type AgentName = "claude" | "codex";


export interface AgentResponseCompletion {
  completion_id: string;
  sequence: number;
  kind: "response";
  text: string;
  agent: AgentName;
  source_label: string;
  empty: boolean;
  spoken_summary?: string | null;
  source_name?: string | null;
  source_name_is_custom?: boolean;
}


export interface AgentOverflowCompletion {
  completion_id: string;
  sequence: number;
  kind: "overflow";
  text: string;
  overflow_count: number;
}


export type AgentCompletion = AgentResponseCompletion | AgentOverflowCompletion;


/** Authoritative v1 live upsert for one retained visual completion. */
export interface AgentCompletionUpsertEvent {
  type: "agent.completion";
  version: 1;
  show_text: boolean;
  completion: AgentCompletion;
  /** Ordered IDs still retained by the backend after this upsert. */
  pending_completion_ids: string[];
}


/** Authoritative v1 reconnect snapshot, ordered by backend sequence. */
export interface AgentCompletionsSnapshotEvent {
  type: "agent.completions.snapshot";
  version: 1;
  completions: AgentCompletion[];
}


/** Idempotent client command sent only after a completion receives its dwell. */
export interface AgentCompletionAckMessage {
  type: "agent.completion.ack";
  version: 1;
  completion_id: string;
}


/** Server confirmation for an idempotent acknowledgement command. */
export interface AgentCompletionAcknowledgedEvent {
  type: "agent.completion.acknowledged";
  version: 1;
  completion_id: string;
}


export type AgentResponsePhase =
  | "idle"
  | "active_speaking"
  | "active_grace"
  | "quiet_delay";


export interface AgentActiveResponse {
  completion_id: string;
  phase: "active_speaking" | "active_grace";
  request_id: string | null;
  session_handle: string | null;
  generation: number;
  engaged: boolean;
  routable: boolean;
  passive_total_seconds: number;
  passive_remaining_seconds: number;
  hard_remaining_seconds: number;
  speech_completed?: boolean;
  /** Omitted by the legacy Claude wire shape; Codex sends its identity. */
  agent?: AgentName;
}


/** Full backend-authoritative response FIFO and reconnect state. */
export interface AgentResponseQueueSnapshotEvent {
  type: "agent.response_queue.snapshot";
  version: 1;
  revision: number;
  phase: AgentResponsePhase;
  ordered_completion_ids: string[];
  completions: AgentCompletion[];
  active: AgentActiveResponse | null;
  quiet_delay_remaining_seconds: number;
  show_summary_text?: boolean;
}


export interface AgentInteraction {
  request_id: string;
  session_handle: string;
  kind: "stop" | "question" | "permission";
  sequence: number;
  status: "pending" | "resolved" | "expired";
  active: boolean;
  engaged: boolean;
  passive_seconds: number;
  remaining_seconds: number;
  hard_remaining_seconds: number;
  source_name?: string | null;
  /** Omitted by the legacy Claude wire shape; Codex sends its identity. */
  agent?: AgentName;
}


/** Content-free interaction lifecycle. Decisions remain backend-only. */
export interface AgentInteractionStateEvent {
  type: "agent.interaction.state";
  version: 1;
  interaction: AgentInteraction;
}

export interface AgentInteractionsSnapshotEvent {
  type: "agent.interactions.snapshot";
  version: 1;
  revision: number;
  interactions: AgentInteractionStateEvent[];
}


export interface AgentComposeStateEvent {
  type: "agent.compose.state";
  version: 1;
  session_handle: string;
  generation: number;
  state: "recording" | "idle" | "cancelled";
  recording_id?: number;
  reason?: string;
}


export interface AgentComposePreviewEvent {
  type: "agent.compose.preview";
  version: 1;
  session_handle: string;
  generation: number;
  recording_id: number;
  revision: number;
  text: string;
  stable_char_count: number;
  presentation_version: 1;
  mutable_start_char_count: number;
  freeze: boolean;
}


export interface AgentComposeFinalEvent {
  type: "agent.compose.final";
  version: 1;
  session_handle: string;
  generation: number;
  recording_id: number;
  text: string;
}


export type AgentComposeEvent =
  | AgentComposeStateEvent
  | AgentComposePreviewEvent
  | AgentComposeFinalEvent;


export type AgentQuestionPhase =
  | "surfaced"
  | "speaking"
  | "grace"
  | "answered"
  | "deferred"
  | "expired"
  | "held_failed";


export interface AgentQuestionOption {
  label: string;
  description?: string;
}


export interface AgentQuestion {
  question: string;
  header: string;
  options: AgentQuestionOption[];
  multiSelect: boolean;
}


export interface AgentActiveQuestion {
  session_handle: string;
  generation: number;
  phase: AgentQuestionPhase;
  questions: AgentQuestion[];
  current_index: number;
  request_id: string | null;
  routable: boolean;
  capture_available: boolean;
  remaining_seconds: number;
  answers: Record<string, string>;
  source_name?: string | null;
}


export interface AgentQuestionSnapshotEvent {
  type: "agent.question.snapshot";
  version: 1;
  revision: number;
  active: AgentActiveQuestion | null;
}


export interface AgentConversation {
  session_handle: string;
  activity: "working" | "finished";
  turns: { id: string; role: "user" | "assistant"; text: string }[];
}

export interface AgentConversationSnapshot {
  type: "agent.conversation.snapshot";
  version: 1;
  sessions: AgentConversation[];
}

export function parseAgentConversationEvent(value: unknown): AgentConversationSnapshot | null {
  if (!isPlainRecord(value) || containsForbiddenAgentField(value) || !hasOnlyKeys(value, ["type", "version", "sessions"]) || value.type !== "agent.conversation.snapshot" || value.version !== 1 || !Array.isArray(value.sessions) || value.sessions.length > 12) return null;
  let characters = 0;
  const handles = new Set<string>();
  for (const session of value.sessions) {
    if (!isPlainRecord(session) || !hasOnlyKeys(session, ["session_handle", "activity", "turns"]) || !isOpaqueAgentId(session.session_handle) || handles.has(session.session_handle) || !["working", "finished"].includes(session.activity as string) || !Array.isArray(session.turns) || session.turns.length > 20) return null;
    handles.add(session.session_handle);
    const ids = new Set<string>();
    for (const turn of session.turns) {
      if (!isPlainRecord(turn) || !hasOnlyKeys(turn, ["id", "role", "text"]) || !isOpaqueAgentId(turn.id) || ids.has(turn.id) || !["user", "assistant"].includes(turn.role as string) || typeof turn.text !== "string" || turn.text.length > 64000) return null;
      // Python bounds Unicode code points; JavaScript length counts UTF-16 units.
      const turnCharacters = Array.from(turn.text).length;
      if (turnCharacters > 32000) return null;
      ids.add(turn.id); characters += turnCharacters;
    }
  }
  if (characters > 200000) return null;
  return cloneJsonRecord(value) as unknown as AgentConversationSnapshot;
}


/** One managed coding-agent session as it appears in the HUD tab strip. */
export interface HudSession {
  session_handle: string;
  agent: AgentName;
  source_name: string;
  unread: boolean;
  needs_input: boolean;
  can_reply: boolean;
  conversation_handle?: string | null;
  latest_response?: string | null;
  /** The reply target this tab's console shares with its hook cards. */
  reply_target?: string;
  /** A reply can be accepted now, delivered at once or at the next Stop. */
  accepts_reply?: boolean;
  /** A reply sent while the agent worked waits for its next Stop. */
  reply_held?: boolean;
  /** The conversation ended; its console only offers Copy for kept text. */
  ended?: boolean;
}


/** Reconnect-replayed session list (Task 4.1). Never carries a reason string,
 * `cwd`, the provider `session_id`, or a raw token. */
export interface AgentSessionsSnapshotEvent {
  type: "agent.sessions";
  sessions: HudSession[];
}


export type SocketSend = (message: Record<string, unknown>) => boolean;


// ---------------------------------------------------------------------------
// Replies: one backend owner (agent_reply_desk). Drafts are keyed by an opaque
// target: `conv:` (a conversation; its hook card and console share it) or
// `question:`. A wrapped session whose conversation is not bound yet has no
// target and takes no reply.
// ---------------------------------------------------------------------------

/** `sending`: handed to a wrapper or held for its next Stop; read-only. */
export type ReplyDraftPhase = "empty" | "recording" | "processing" | "ready" | "editing" | "sending";
export type ReplySurface = "card" | "console" | "question";
export type ReplyDelivery = "now" | "hold" | "none";

export interface AgentReplyDraft {
  draft_id: string;
  target_id: string;
  kind: "reply" | "question";
  phase: ReplyDraftPhase;
  text: string;
  text_revision: number;
  compose_generation: number | null;
  capture_mode: "push_to_talk" | "toggle" | null;
  delivery: ReplyDelivery;
  /** While `sending`: waiting for the agent's next Stop. */
  held: boolean;
  /**
   * The text came back at `revision`: refused at submit, or handed to a
   * wrapper that never collected it. The text is kept for Copy.
   */
  outcome: { revision: number; result: "refused"; reason: "refused" | "not_delivered" } | null;
}

/** A delivered draft and the exact text that was sent. */
export interface AgentReplyReceipt {
  target_id: string;
  draft_id: string;
  result: "sent";
  text: string;
}

/** The one reply state the HUD renders; replayed on connect, emptied on teardown. */
export interface AgentReplySnapshotEvent {
  type: "agent.reply.snapshot";
  version: 1;
  epoch: string | null;
  revision: number;
  foreground: {
    target_id: string;
    surface: ReplySurface;
    painted: boolean;
    focused: boolean;
  } | null;
  drafts: AgentReplyDraft[];
  receipts: AgentReplyReceipt[];
  enter_owner: "global" | "hud" | "none";
  /** The user has dictated enough HUD replies that the resting reply hints
   *  stop showing. Absent from older backends: treat as not retired. */
  reply_hint_retired?: boolean;
}

/** Correlated answer to one HUD reply command. */
export interface AgentReplyResultEvent {
  type: "agent.reply.result";
  version: 1;
  command: "agent.draft.update" | "agent.draft.submit" | "agent.draft.cancel";
  result: "applied" | "refused" | "stale" | "conflict";
  command_id?: number;
  draft_id?: string;
  target_id?: string;
  text_revision?: number;
}

/** Escape dismissed the console, or skipped the card, for this target. */
export interface AgentReplyCloseEvent {
  type: "agent.reply.close";
  version: 1;
  target_id: string;
  surface: "card" | "console";
}

const REPLY_TARGET_PREFIXES = ["conv:", "question:"];

export function isReplyTargetId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 600 &&
    REPLY_TARGET_PREFIXES.some((prefix) => value.startsWith(prefix)) &&
    !/[\u0000-\u001f]/.test(value)
  );
}

export function replyTargetForConversation(handle: string): string {
  return `conv:${handle}`;
}

export function replyTargetForQuestion(sessionHandle: string, generation: number): string {
  return `question:${sessionHandle}:${generation}`;
}

const REPLY_PHASES = new Set(["empty", "recording", "processing", "ready", "editing", "sending"]);
const REPLY_DELIVERIES = new Set(["now", "hold", "none"]);
const REPLY_SURFACES = new Set(["card", "console", "question"]);

function isAgentReplyDraft(value: unknown): value is AgentReplyDraft {
  return (
    isPlainRecord(value) &&
    hasOnlyKeys(value, [
      "draft_id",
      "target_id",
      "kind",
      "phase",
      "text",
      "text_revision",
      "compose_generation",
      "capture_mode",
      "held",
      "delivery",
      "outcome",
    ]) &&
    isOpaqueAgentId(value.draft_id) &&
    isReplyTargetId(value.target_id) &&
    (value.kind === "reply" || value.kind === "question") &&
    REPLY_PHASES.has(String(value.phase)) &&
    typeof value.text === "string" &&
    value.text.length <= 50_000 &&
    isSequence(value.text_revision) &&
    (value.compose_generation === null || isPositiveSequence(value.compose_generation)) &&
    (value.capture_mode === null ||
      value.capture_mode === "push_to_talk" ||
      value.capture_mode === "toggle") &&
    typeof value.held === "boolean" &&
    REPLY_DELIVERIES.has(String(value.delivery)) &&
    (value.outcome === null ||
      (isPlainRecord(value.outcome) &&
        hasOnlyKeys(value.outcome, ["revision", "result", "reason"]) &&
        isSequence(value.outcome.revision) &&
        value.outcome.result === "refused" &&
        (value.outcome.reason === "refused" || value.outcome.reason === "not_delivered")))
  );
}

export function parseAgentReplyEvent(
  value: unknown,
): AgentReplySnapshotEvent | AgentReplyResultEvent | AgentReplyCloseEvent | null {
  if (!isPlainRecord(value) || containsForbiddenAgentField(value) || value.version !== 1) {
    return null;
  }
  if (value.type === "agent.reply.snapshot") {
    if (
      !hasOnlyKeys(value, [
        "type",
        "version",
        "epoch",
        "revision",
        "foreground",
        "drafts",
        "receipts",
        "enter_owner",
        "reply_hint_retired",
      ]) ||
      (value.reply_hint_retired !== undefined && typeof value.reply_hint_retired !== "boolean") ||
      (value.epoch !== null && !isOpaqueAgentId(value.epoch)) ||
      !isSequence(value.revision) ||
      !Array.isArray(value.drafts) ||
      value.drafts.length > 512 ||
      !value.drafts.every(isAgentReplyDraft) ||
      !Array.isArray(value.receipts) ||
      value.receipts.length > 16 ||
      !value.receipts.every(
        (receipt) =>
          isPlainRecord(receipt) &&
          hasOnlyKeys(receipt, ["target_id", "draft_id", "result", "text"]) &&
          isReplyTargetId(receipt.target_id) &&
          isOpaqueAgentId(receipt.draft_id) &&
          receipt.result === "sent" &&
          typeof receipt.text === "string" &&
          receipt.text.length <= 50_000,
      ) ||
      !["global", "hud", "none"].includes(String(value.enter_owner)) ||
      (value.foreground !== null &&
        !(
          isPlainRecord(value.foreground) &&
          hasOnlyKeys(value.foreground, ["target_id", "surface", "painted", "focused"]) &&
          isReplyTargetId(value.foreground.target_id) &&
          REPLY_SURFACES.has(String(value.foreground.surface)) &&
          typeof value.foreground.painted === "boolean" &&
          typeof value.foreground.focused === "boolean"
        ))
    ) {
      return null;
    }
    return cloneJsonRecord(value) as unknown as AgentReplySnapshotEvent;
  }
  if (value.type === "agent.reply.result") {
    if (
      !hasOnlyKeys(value, [
        "type",
        "version",
        "command",
        "result",
        "command_id",
        "draft_id",
        "target_id",
        "text_revision",
      ]) ||
      !["agent.draft.update", "agent.draft.submit", "agent.draft.cancel"].includes(
        String(value.command),
      ) ||
      !["applied", "refused", "stale", "conflict"].includes(String(value.result)) ||
      (value.command_id !== undefined && !isSequence(value.command_id)) ||
      (value.draft_id !== undefined && !isOpaqueAgentId(value.draft_id)) ||
      (value.target_id !== undefined && !isReplyTargetId(value.target_id)) ||
      (value.text_revision !== undefined && !isSequence(value.text_revision))
    ) {
      return null;
    }
    return cloneJsonRecord(value) as unknown as AgentReplyResultEvent;
  }
  if (value.type === "agent.reply.close") {
    if (
      !hasOnlyKeys(value, ["type", "version", "target_id", "surface"]) ||
      !isReplyTargetId(value.target_id) ||
      (value.surface !== "card" && value.surface !== "console")
    ) {
      return null;
    }
    return { type: "agent.reply.close", version: 1, target_id: value.target_id, surface: value.surface };
  }
  return null;
}

/** Report what the HUD actually painted and whether it has native focus. */
export function sendAgentHudPresented(
  send: SocketSend,
  presented: {
    seq: number;
    target_id: string | null;
    surface: ReplySurface | null;
    painted: boolean;
    focused: boolean;
  },
): boolean {
  if (
    !isSequence(presented.seq) ||
    (presented.target_id !== null && !isReplyTargetId(presented.target_id)) ||
    (presented.target_id === null) !== (presented.surface === null)
  ) {
    return false;
  }
  return send({ type: "agent.hud.presented", version: 1, ...presented });
}

/** One typed edit; the first update for a target creates its draft. */
export function sendAgentDraftUpdate(
  send: SocketSend,
  update: { command_id: number; target_id: string; base_revision: number; text: string },
): boolean {
  if (
    !isReplyTargetId(update.target_id) ||
    !isSequence(update.base_revision) ||
    typeof update.text !== "string" ||
    update.text.length > 50_000
  ) {
    return false;
  }
  return send({ type: "agent.draft.update", version: 1, ...update });
}

/** Submit exactly the shown text at an exact revision. */
export function sendAgentDraftSubmit(
  send: SocketSend,
  submit: { command_id: number; target_id: string; revision: number; text: string },
): boolean {
  if (
    !isReplyTargetId(submit.target_id) ||
    !isSequence(submit.revision) ||
    typeof submit.text !== "string" ||
    submit.text.length > 50_000 ||
    submit.text.trim().length === 0
  ) {
    return false;
  }
  return send({ type: "agent.draft.submit", version: 1, ...submit });
}

export const AGENT_INTERACTION_KINDS = new Set([
  "stop",
  "question",
  "permission",
]);

export const AGENT_INTERACTION_STATUSES = new Set([
  "pending",
  "resolved",
  "expired",
]);

export const AGENT_NAMES = new Set<AgentName>(["claude", "codex"]);

export const AGENT_RESPONSE_PHASES = new Set([
  "idle",
  "active_speaking",
  "active_grace",
  "quiet_delay",
]);


export function parseAgentResponseQueueEvent(
  value: unknown,
): AgentResponseQueueSnapshotEvent | null {
  if (
    !isPlainRecord(value) ||
    containsForbiddenAgentField(value) ||
    value.type !== "agent.response_queue.snapshot" ||
    value.version !== 1 ||
    !hasOnlyKeys(value, [
      "type",
      "version",
      "revision",
      "phase",
      "ordered_completion_ids",
      "completions",
      "active",
      "quiet_delay_remaining_seconds",
      "show_summary_text",
    ]) ||
    !isSequence(value.revision) ||
    !AGENT_RESPONSE_PHASES.has(String(value.phase)) ||
    !Array.isArray(value.ordered_completion_ids) ||
    !value.ordered_completion_ids.every(isOpaqueAgentId) ||
    new Set(value.ordered_completion_ids).size !==
      value.ordered_completion_ids.length ||
    !Array.isArray(value.completions) ||
    !value.completions.every(isAgentCompletion) ||
    !isNonNegativeFinite(value.quiet_delay_remaining_seconds) ||
    value.quiet_delay_remaining_seconds > 10 ||
    (value.show_summary_text !== undefined &&
      typeof value.show_summary_text !== "boolean")
  ) {
    return null;
  }
  const ordered = new Set(value.ordered_completion_ids);
  if (
    !value.completions.every((completion) =>
      ordered.has(completion.completion_id),
    )
  ) {
    return null;
  }
  if (value.active === null) {
    if (
      value.phase === "active_speaking" ||
      value.phase === "active_grace"
    ) {
      return null;
    }
  } else if (
    !isAgentActiveResponse(value.active) ||
    value.active.phase !== value.phase ||
    value.ordered_completion_ids[0] !== value.active.completion_id
  ) {
    return null;
  } else {
    const active = value.active as AgentActiveResponse;
    const activeCompletion = (value.completions as AgentCompletion[]).find(
      (completion) => completion.completion_id === active.completion_id,
    );
    if (
      activeCompletion?.kind === "response" &&
      (active.agent ?? "claude") !== activeCompletion.agent
    ) {
      return null;
    }
  }
  return structuredClone(value) as unknown as AgentResponseQueueSnapshotEvent;
}


/** Parse one privacy-minimal current-interaction lifecycle event. */
export function parseAgentInteractionEvent(
  value: unknown,
): AgentInteractionStateEvent | null {
  if (
    !isPlainRecord(value) ||
    containsForbiddenAgentField(value) ||
    value.type !== "agent.interaction.state" ||
    value.version !== 1 ||
    !hasOnlyKeys(value, ["type", "version", "interaction"]) ||
    !isAgentInteraction(value.interaction)
  ) {
    return null;
  }
  return cloneJsonRecord(value) as unknown as AgentInteractionStateEvent;
}

export function parseAgentInteractionsSnapshot(value: unknown): AgentInteractionsSnapshotEvent | null {
  if (!isPlainRecord(value) || containsForbiddenAgentField(value) ||
      value.type !== "agent.interactions.snapshot" || value.version !== 1 ||
      !hasOnlyKeys(value, ["type", "version", "revision", "interactions"]) ||
      !isSequence(value.revision) || !Array.isArray(value.interactions) ||
      value.interactions.length > 1024) return null;
  const interactions: AgentInteractionStateEvent[] = [];
  const seen = new Set<string>();
  for (const row of value.interactions) {
    const parsed = parseAgentInteractionEvent(row);
    if (!parsed || seen.has(parsed.interaction.request_id)) return null;
    seen.add(parsed.interaction.request_id);
    interactions.push(parsed);
  }
  return { type: "agent.interactions.snapshot", version: 1, revision: value.revision, interactions };
}


/** Parse generation-correlated compose facts without accepting route metadata. */
export function parseAgentComposeEvent(value: unknown): AgentComposeEvent | null {
  if (!isPlainRecord(value) || containsForbiddenAgentField(value)) return null;
  if (
    value.version !== 1 ||
    !isOpaqueAgentId(value.session_handle) ||
    !isPositiveSequence(value.generation)
  ) {
    return null;
  }

  const valid =
    (value.type === "agent.compose.state" &&
      hasOnlyKeys(value, [
        "type",
        "version",
        "session_handle",
        "generation",
        "state",
        "recording_id",
        "reason",
      ]) &&
      (value.state === "recording" ||
        value.state === "idle" ||
        value.state === "cancelled") &&
      (value.recording_id === undefined ||
        isPositiveSequence(value.recording_id)) &&
      (value.reason === undefined ||
        (typeof value.reason === "string" &&
          /^[a-z][a-z0-9_]{0,31}$/.test(value.reason)))) ||
    (value.type === "agent.compose.preview" &&
      hasOnlyKeys(value, [
        "type",
        "version",
        "session_handle",
        "generation",
        "recording_id",
        "revision",
        "text",
        "stable_char_count",
        "presentation_version",
        "mutable_start_char_count",
        "freeze",
      ]) &&
      isPositiveSequence(value.recording_id) &&
      isPositiveSequence(value.revision) &&
      isBoundedString(value.text) &&
      isSequence(value.stable_char_count) &&
      value.presentation_version === 1 &&
      isSequence(value.mutable_start_char_count) &&
      typeof value.freeze === "boolean") ||
    (value.type === "agent.compose.final" &&
      hasOnlyKeys(value, [
        "type",
        "version",
        "session_handle",
        "generation",
        "recording_id",
        "text",
      ]) &&
      isPositiveSequence(value.recording_id) &&
      isBoundedString(value.text));
  return valid
    ? (cloneJsonRecord(value) as unknown as AgentComposeEvent)
    : null;
}


export function parseAgentQuestionEvent(
  value: unknown,
): AgentQuestionSnapshotEvent | null {
  if (
    !isPlainRecord(value) ||
    containsForbiddenAgentField(value) ||
    value.type !== "agent.question.snapshot" ||
    value.version !== 1 ||
    !hasOnlyKeys(value, ["type", "version", "revision", "active"]) ||
    !isSequence(value.revision) ||
    (value.active !== null && !isAgentActiveQuestion(value.active))
  ) {
    return null;
  }
  return cloneJsonRecord(value) as unknown as AgentQuestionSnapshotEvent;
}


export function isHudSession(value: unknown): value is HudSession {
  return (
    isPlainRecord(value) &&
    hasOnlyKeys(value, [
      "session_handle",
      "agent",
      "source_name",
      "unread",
      "needs_input",
      "can_reply",
      "conversation_handle",
      "latest_response",
      "reply_target",
      "accepts_reply",
      "reply_held",
      "ended",
    ]) &&
    isOpaqueAgentId(value.session_handle) &&
    (value.agent === "claude" || value.agent === "codex") &&
    isSafeSourceLabel(value.source_name) &&
    typeof value.unread === "boolean" &&
    typeof value.needs_input === "boolean" &&
    typeof value.can_reply === "boolean" &&
    (value.conversation_handle === undefined || value.conversation_handle === null || isOpaqueAgentId(value.conversation_handle)) &&
    (value.latest_response === null || isBoundedString(value.latest_response)) &&
    (value.reply_target === undefined || isReplyTargetId(value.reply_target)) &&
    (value.accepts_reply === undefined || typeof value.accepts_reply === "boolean") &&
    (value.reply_held === undefined || typeof value.reply_held === "boolean") &&
    (value.ended === undefined || typeof value.ended === "boolean")
  );
}


export function parseAgentSessionsEvent(
  value: unknown,
): AgentSessionsSnapshotEvent | null {
  if (
    !isPlainRecord(value) ||
    containsForbiddenAgentField(value) ||
    value.type !== "agent.sessions" ||
    !hasOnlyKeys(value, ["type", "sessions"]) ||
    !Array.isArray(value.sessions) ||
    !value.sessions.every(isHudSession)
  ) {
    return null;
  }
  return cloneJsonRecord(value) as unknown as AgentSessionsSnapshotEvent;
}


export function sendAgentQuestionOpportunityAnswers(
  send: SocketSend,
  sessionHandle: string,
  generation: number,
  answers: Record<string, string>,
): boolean {
  if (
    !isOpaqueAgentId(sessionHandle) ||
    !isPositiveSequence(generation) ||
    !isQuestionAnswers(answers)
  ) {
    return false;
  }
  return send({
    type: "agent.question.answer",
    version: 1,
    session_handle: sessionHandle,
    generation,
    answers,
  });
}


export function sendAgentQuestionOpportunityDefer(
  send: SocketSend,
  sessionHandle: string,
  generation: number,
): boolean {
  if (!isOpaqueAgentId(sessionHandle) || !isPositiveSequence(generation)) {
    return false;
  }
  return send({
    type: "agent.interaction.defer",
    version: 1,
    session_handle: sessionHandle,
    generation,
  });
}


export function sendAgentQuestionOpportunityAck(
  send: SocketSend,
  sessionHandle: string,
  generation: number,
): boolean {
  if (!isOpaqueAgentId(sessionHandle) || !isPositiveSequence(generation)) {
    return false;
  }
  return send({
    type: "agent.question.ack",
    version: 1,
    session_handle: sessionHandle,
    generation,
  });
}


export function isQuestionAnswers(value: unknown): value is Record<string, string> {
  if (!isPlainRecord(value) || Object.keys(value).length === 0) return false;
  let totalChars = 0;
  return Object.entries(value).every(([question, answer]) => {
    totalChars += question.length + (typeof answer === "string" ? answer.length : 0);
    return (
      question.length > 0 &&
      isBoundedNonBlankString(answer) &&
      totalChars <= 500_000
    );
  });
}


export function isAgentQuestionOption(value: unknown): value is AgentQuestionOption {
  return (
    isPlainRecord(value) &&
    hasOnlyKeys(value, ["label", "description"]) &&
    isBoundedNonBlankString(value.label) &&
    (value.description === undefined ||
      (typeof value.description === "string" &&
        value.description.length <= 500_000))
  );
}


export function isAgentQuestion(value: unknown): value is AgentQuestion {
  return (
    isPlainRecord(value) &&
    hasOnlyKeys(value, ["question", "header", "options", "multiSelect"]) &&
    isBoundedNonBlankString(value.question) &&
    typeof value.header === "string" &&
    value.header.length <= 500_000 &&
    Array.isArray(value.options) &&
    value.options.length >= 1 &&
    value.options.length <= 9 &&
    value.options.every(isAgentQuestionOption) &&
    typeof value.multiSelect === "boolean"
  );
}


export function isStringRecord(value: unknown): value is Record<string, string> {
  return (
    isPlainRecord(value) &&
    Object.entries(value).every(
      ([key, item]) => key.length > 0 && isBoundedString(item),
    )
  );
}


export function isAgentActiveQuestion(value: unknown): value is AgentActiveQuestion {
  if (
    !isPlainRecord(value) ||
    !hasOnlyKeys(value, [
      "session_handle",
      "generation",
      "phase",
      "questions",
      "current_index",
      "request_id",
      "routable",
      "capture_available",
      "remaining_seconds",
      "answers",
      "source_name",
    ]) ||
    !isOpaqueAgentId(value.session_handle) ||
    !isPositiveSequence(value.generation) ||
    !Array.isArray(value.questions) ||
    value.questions.length < 1 ||
    !value.questions.every(isAgentQuestion) ||
    !isSequence(value.current_index) ||
    value.current_index >= value.questions.length ||
    (value.request_id !== null && !isOpaqueAgentId(value.request_id)) ||
    typeof value.routable !== "boolean" ||
    typeof value.capture_available !== "boolean" ||
    !isNonNegativeFinite(value.remaining_seconds) ||
    !isStringRecord(value.answers) ||
    (value.source_name !== undefined &&
      value.source_name !== null &&
      !isSafeSourceLabel(value.source_name))
  ) {
    return false;
  }
  return [
    "surfaced",
    "speaking",
    "grace",
    "answered",
    "deferred",
    "expired",
    "held_failed",
  ].includes(String(value.phase));
}


export function isAgentInteraction(value: unknown): value is AgentInteraction {
  return (
    isPlainRecord(value) &&
    hasOnlyKeys(value, [
      "request_id",
      "session_handle",
      "kind",
      "sequence",
      "status",
      "active",
      "engaged",
      "passive_seconds",
      "remaining_seconds",
      "hard_remaining_seconds",
      "source_name",
      "agent",
    ]) &&
    isOpaqueAgentId(value.request_id) &&
    isOpaqueAgentId(value.session_handle) &&
    AGENT_INTERACTION_KINDS.has(String(value.kind)) &&
    isSequence(value.sequence) &&
    AGENT_INTERACTION_STATUSES.has(String(value.status)) &&
    typeof value.active === "boolean" &&
    typeof value.engaged === "boolean" &&
    isNonNegativeFinite(value.passive_seconds) &&
    value.passive_seconds > 0 &&
    value.passive_seconds <= 30 &&
    isNonNegativeFinite(value.remaining_seconds) &&
    isNonNegativeFinite(value.hard_remaining_seconds) &&
    (value.source_name === undefined ||
      value.source_name === null ||
      isSafeSourceLabel(value.source_name)) &&
    (value.agent === undefined || AGENT_NAMES.has(value.agent as AgentName))
  );
}


export function isAgentActiveResponse(value: unknown): value is AgentActiveResponse {
  return (
    isPlainRecord(value) &&
    hasOnlyKeys(value, [
      "completion_id",
      "phase",
      "request_id",
      "session_handle",
      "generation",
      "engaged",
      "routable",
      "passive_total_seconds",
      "passive_remaining_seconds",
      "hard_remaining_seconds",
      "speech_completed",
      "agent",
    ]) &&
    isOpaqueAgentId(value.completion_id) &&
    (value.phase === "active_speaking" ||
      value.phase === "active_grace") &&
    (value.request_id === null || isOpaqueAgentId(value.request_id)) &&
    (value.session_handle === null ||
      isOpaqueAgentId(value.session_handle)) &&
    isPositiveSequence(value.generation) &&
    typeof value.engaged === "boolean" &&
    typeof value.routable === "boolean" &&
    isNonNegativeFinite(value.passive_total_seconds) &&
    value.passive_total_seconds <= 30 &&
    isNonNegativeFinite(value.passive_remaining_seconds) &&
    value.passive_remaining_seconds <= 120 &&
    isNonNegativeFinite(value.hard_remaining_seconds) &&
    value.hard_remaining_seconds <= 120 &&
    (value.speech_completed === undefined || typeof value.speech_completed === "boolean") &&
    (value.agent === undefined || AGENT_NAMES.has(value.agent as AgentName))
  );
}


export function isAgentCompletion(value: unknown): value is AgentCompletion {
  if (
    !isPlainRecord(value) ||
    !isOpaqueAgentId(value.completion_id) ||
    !isSequence(value.sequence) ||
    typeof value.text !== "string"
  ) {
    return false;
  }
  if (value.kind === "response") {
    return (
      hasOnlyKeys(value, [
        "completion_id",
        "sequence",
        "kind",
        "text",
        "agent",
        "source_label",
        "empty",
        "spoken_summary",
        "source_name",
        "source_name_is_custom",
      ]) &&
      AGENT_NAMES.has(value.agent as AgentName) &&
      isSafeSourceLabel(value.source_label) &&
      typeof value.empty === "boolean" &&
      (value.spoken_summary === undefined ||
        value.spoken_summary === null ||
        (typeof value.spoken_summary === "string" &&
          value.spoken_summary.length <= 50_000)) &&
      (value.source_name === undefined ||
        value.source_name === null ||
        isSafeSourceLabel(value.source_name)) &&
      (value.source_name_is_custom === undefined ||
        typeof value.source_name_is_custom === "boolean")
    );
  }
  return (
    value.kind === "overflow" &&
    hasOnlyKeys(value, [
      "completion_id",
      "sequence",
      "kind",
      "text",
      "overflow_count",
    ]) &&
    isPositiveSequence(value.overflow_count)
  );
}
