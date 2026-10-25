import type {
  AgentReplyDraft,
  AgentReplyReceipt,
  AgentReplyResultEvent,
  AgentReplySnapshotEvent,
} from "./socket/agentEvents";

/**
 * A rendered copy of the backend reply owner (`agent_reply_desk`). The HUD
 * keeps no draft state of its own beyond the keystroke buffers below.
 */
export interface AgentReplyState {
  epoch: string | null;
  revision: number;
  foreground: AgentReplySnapshotEvent["foreground"];
  drafts: Record<string, AgentReplyDraft>;
  receipts: AgentReplyReceipt[];
}

export function createAgentReplyState(): AgentReplyState {
  return { epoch: null, revision: -1, foreground: null, drafts: {}, receipts: [] };
}

export function agentReplyReducer(
  state: AgentReplyState,
  action: { type: "snapshot"; event: AgentReplySnapshotEvent } | { type: "reset" },
): AgentReplyState {
  if (action.type === "reset") return createAgentReplyState();
  const event = action.event;
  // A reordered older snapshot from the same backend epoch never wins.
  if (event.epoch === state.epoch && event.revision < state.revision) return state;
  const drafts: Record<string, AgentReplyDraft> = {};
  for (const draft of event.drafts) drafts[draft.target_id] = draft;
  return {
    epoch: event.epoch,
    revision: event.revision,
    foreground: event.foreground,
    drafts,
    receipts: event.receipts,
  };
}

/** A refusal at the draft's current revision: keep the text, offer Copy. */
export function draftRefused(draft: AgentReplyDraft | null | undefined): boolean {
  return (
    draft !== null &&
    draft !== undefined &&
    draft.outcome?.result === "refused" &&
    draft.outcome.revision === draft.text_revision
  );
}

// ---------------------------------------------------------------------------
// Keystroke buffers
//
// One buffer per target holds what the user typed. Updates are pipelined:
// each carries the revision it expects to replace, so fast typing never
// waits for an acknowledgement and keys typed before the draft exists are
// kept (the first update creates it). While any update is in flight, the
// backend's echo never overwrites the buffer. Once the backend moves past the
// buffer (a dictation appended), or refuses an update, its text wins.
// ---------------------------------------------------------------------------

export interface ReplyBuffer {
  text: string;
  /** The text revision the backend will hold after every sent update. */
  expectedRevision: number;
  inflight: number;
}

export type ReplyBuffers = Record<string, ReplyBuffer>;

export function bufferedText(
  buffers: ReplyBuffers,
  target: string,
  draft: AgentReplyDraft | undefined,
): string {
  const buffer = buffers[target];
  if (buffer !== undefined && (buffer.inflight > 0 || buffer.expectedRevision >= (draft?.text_revision ?? 0))) {
    return buffer.text;
  }
  return draft?.text ?? "";
}

export function bufferedRevision(
  buffers: ReplyBuffers,
  target: string,
  draft: AgentReplyDraft | undefined,
): number {
  const buffer = buffers[target];
  if (buffer !== undefined && (buffer.inflight > 0 || buffer.expectedRevision >= (draft?.text_revision ?? 0))) {
    return buffer.expectedRevision;
  }
  return draft?.text_revision ?? 0;
}

/** Apply one keystroke; returns the new buffers and the update to send. */
export function editBuffer(
  buffers: ReplyBuffers,
  target: string,
  draft: AgentReplyDraft | undefined,
  text: string,
): { buffers: ReplyBuffers; baseRevision: number } {
  const baseRevision = bufferedRevision(buffers, target, draft);
  const inflight = (buffers[target]?.inflight ?? 0) + 1;
  return {
    buffers: { ...buffers, [target]: { text, expectedRevision: baseRevision + 1, inflight } },
    baseRevision,
  };
}

/**
 * Fold one update result. Applied settles one in-flight update. Anything else
 * (a conflict, a stale revision, a refusal while a capture or a send owns the
 * draft) means the backend holds other text: its text wins and the buffer is
 * dropped, never resent over it.
 */
export function settleBuffer(
  buffers: ReplyBuffers,
  target: string,
  result: AgentReplyResultEvent["result"],
): ReplyBuffers {
  const buffer = buffers[target];
  if (buffer === undefined) return buffers;
  if (result === "applied") {
    return { ...buffers, [target]: { ...buffer, inflight: Math.max(0, buffer.inflight - 1) } };
  }
  const next = { ...buffers };
  delete next[target];
  return next;
}

/** Forget buffers whose draft is gone (sent, discarded) and nothing is in flight. */
export function pruneBuffers(
  buffers: ReplyBuffers,
  drafts: Record<string, AgentReplyDraft>,
  keep: string | null,
): ReplyBuffers {
  let changed = false;
  const next: ReplyBuffers = {};
  for (const [target, buffer] of Object.entries(buffers)) {
    const draft = drafts[target];
    const stale =
      buffer.inflight === 0 &&
      target !== keep &&
      (draft === undefined || draft.text_revision > buffer.expectedRevision);
    if (stale) {
      changed = true;
      continue;
    }
    next[target] = buffer;
  }
  return changed ? next : buffers;
}
