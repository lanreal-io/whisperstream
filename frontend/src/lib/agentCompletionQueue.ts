import type { AgentActiveResponse, AgentCompletion, AgentResponsePhase, AgentResponseQueueSnapshotEvent } from "./socket/agentEvents";

/** Legacy test/geometry duration. Lifecycle advancement no longer uses it. */
export const AGENT_COMPLETION_DWELL_MS = 6_000;

/**
 * A rendered copy of the backend coordinator. There is deliberately no local
 * dwell clock, acknowledgement queue, or promotion action.
 */
export interface AgentCompletionQueueState {
  queue: AgentCompletion[];
  orderedCompletionIds: string[];
  phase: AgentResponsePhase;
  active: AgentActiveResponse | null;
  quietDelayRemainingSeconds: number;
  revision: number;
  showSummaryText: boolean;
  /** The socket connection the revision belongs to. */
  connection?: number;
}

export type AgentCompletionQueueAction = {
  type: "response_queue.snapshot";
  event: AgentResponseQueueSnapshotEvent;
  /** Socket connection generation. A restarted backend counts revisions from
   *  zero again, so the first snapshot on a new connection is always taken. */
  connection?: number;
};

export function createAgentCompletionQueueState(): AgentCompletionQueueState {
  return {
    queue: [],
    orderedCompletionIds: [],
    phase: "idle",
    active: null,
    quietDelayRemainingSeconds: 0,
    revision: 0,
    showSummaryText: false,
  };
}

export function currentCompletion(
  state: AgentCompletionQueueState,
): AgentCompletion | null {
  if (
    state.active === null ||
    (state.phase !== "active_speaking" &&
      state.phase !== "active_grace")
  ) {
    return null;
  }
  return (
    state.queue.find(
      (completion) =>
        completion.completion_id === state.active?.completion_id,
    ) ?? null
  );
}

export function currentCompletionIsSpeaking(
  state: AgentCompletionQueueState,
): boolean {
  return (
    state.phase === "active_speaking" &&
    currentCompletion(state) !== null
  );
}

export function queuedCompletionCount(
  state: AgentCompletionQueueState,
): number {
  if (state.active === null) return state.orderedCompletionIds.length;
  return Math.max(0, state.orderedCompletionIds.length - 1);
}

export function agentCompletionQueueReducer(
  state: AgentCompletionQueueState,
  action: AgentCompletionQueueAction,
): AgentCompletionQueueState {
  if (action.type !== "response_queue.snapshot") return state;
  const event = action.event;
  const sameConnection = action.connection === undefined || action.connection === state.connection;
  if (sameConnection && event.revision < state.revision) return state;

  const byId = new Map(
    event.completions.map((completion) => [
      completion.completion_id,
      completion,
    ]),
  );
  const queue = event.ordered_completion_ids.flatMap((completionId) => {
    const completion = byId.get(completionId);
    return completion === undefined ? [] : [completion];
  });

  return {
    queue,
    orderedCompletionIds: [...event.ordered_completion_ids],
    phase: event.phase,
    active: event.active === null ? null : { ...event.active },
    quietDelayRemainingSeconds: event.quiet_delay_remaining_seconds,
    revision: event.revision,
    showSummaryText: Boolean(event.show_summary_text),
    connection: action.connection ?? state.connection,
  };
}
