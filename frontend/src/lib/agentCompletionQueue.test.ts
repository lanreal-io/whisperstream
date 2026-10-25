// @vitest-environment node

import { describe, expect, it } from "vitest";
import {
  agentCompletionQueueReducer,
  createAgentCompletionQueueState,
  currentCompletion,
  currentCompletionIsSpeaking,
  queuedCompletionCount,
} from "./agentCompletionQueue";
import type { AgentCompletion, AgentResponseQueueSnapshotEvent } from "./socket/agentEvents";

function response(completionId: string, sequence: number): AgentCompletion {
  return {
    completion_id: completionId,
    sequence,
    kind: "response",
    text: `Reply ${completionId}`,
    agent: "claude",
    source_label: `Claude · project-${sequence}`,
    empty: false,
  };
}

function snapshot({
  revision,
  phase,
  ordered,
  completions,
  activeId = null,
  passiveRemaining = 0,
  hardRemaining = 0,
  quietRemaining = 0,
  routable = false,
  engaged = false,
  showSummaryText,
}: {
  revision: number;
  phase: AgentResponseQueueSnapshotEvent["phase"];
  ordered: string[];
  completions: AgentCompletion[];
  activeId?: string | null;
  passiveRemaining?: number;
  hardRemaining?: number;
  quietRemaining?: number;
  routable?: boolean;
  engaged?: boolean;
  showSummaryText?: boolean;
}): AgentResponseQueueSnapshotEvent {
  return {
    type: "agent.response_queue.snapshot",
    version: 1,
    revision,
    phase,
    ordered_completion_ids: ordered,
    completions,
    active:
      activeId === null
        ? null
        : {
            completion_id: activeId,
            phase:
              phase === "active_speaking"
                ? "active_speaking"
                : "active_grace",
            request_id: "request-1",
            session_handle: "session-1",
            generation: revision + 1,
            engaged,
            routable,
            passive_total_seconds: 6,
            passive_remaining_seconds: passiveRemaining,
            hard_remaining_seconds: hardRemaining,
          },
    quiet_delay_remaining_seconds: quietRemaining,
    show_summary_text: showSummaryText,
  };
}

describe("backend-authoritative completion queue", () => {
  it("renders only the exact active response while preserving backend order", () => {
    const first = response("first", 1);
    const second = response("second", 2);
    const state = agentCompletionQueueReducer(
      createAgentCompletionQueueState(),
      {
        type: "response_queue.snapshot",
        event: snapshot({
          revision: 1,
          phase: "active_speaking",
          ordered: ["first", "second"],
          completions: [first, second],
          activeId: "first",
          hardRemaining: 119,
        }),
      },
    );

    expect(currentCompletion(state)).toEqual(first);
    expect(currentCompletionIsSpeaking(state)).toBe(true);
    expect(queuedCompletionCount(state)).toBe(1);
    expect(state.orderedCompletionIds).toEqual(["first", "second"]);
  });

  it("renders authoritative grace and hard remaining without a local dwell clock", () => {
    const item = response("active", 1);
    const state = agentCompletionQueueReducer(
      createAgentCompletionQueueState(),
      {
        type: "response_queue.snapshot",
        event: snapshot({
          revision: 2,
          phase: "active_grace",
          ordered: ["active"],
          completions: [item],
          activeId: "active",
          passiveRemaining: 4.25,
          hardRemaining: 112.5,
          routable: true,
        }),
      },
    );

    expect(currentCompletion(state)).toEqual(item);
    expect(currentCompletionIsSpeaking(state)).toBe(false);
    expect(state.active).toMatchObject({
      request_id: "request-1",
      session_handle: "session-1",
      generation: 3,
      passive_remaining_seconds: 4.25,
      hard_remaining_seconds: 112.5,
      routable: true,
    });
    expect(state).not.toHaveProperty("remainingDwellMs");
    expect(state).not.toHaveProperty("pendingAcknowledgementIds");
  });

  it("is visually silent during quiet delay and reconnects to the next promotion", () => {
    const next = response("next", 2);
    let state = agentCompletionQueueReducer(
      createAgentCompletionQueueState(),
      {
        type: "response_queue.snapshot",
        event: snapshot({
          revision: 3,
          phase: "quiet_delay",
          ordered: ["next"],
          completions: [next],
          quietRemaining: 1.4,
        }),
      },
    );

    expect(currentCompletion(state)).toBeNull();
    expect(state.quietDelayRemainingSeconds).toBe(1.4);

    state = agentCompletionQueueReducer(state, {
      type: "response_queue.snapshot",
      event: snapshot({
        revision: 4,
        phase: "active_grace",
        ordered: ["next"],
        completions: [next],
        activeId: "next",
        passiveRemaining: 6,
        hardRemaining: 95,
        routable: true,
      }),
    });
    expect(currentCompletion(state)).toEqual(next);
  });

  it("rejects an older live snapshot after a newer reconnect snapshot", () => {
    const first = response("first", 1);
    const newer = agentCompletionQueueReducer(
      createAgentCompletionQueueState(),
      {
        type: "response_queue.snapshot",
        event: snapshot({
          revision: 10,
          phase: "active_grace",
          ordered: ["first"],
          completions: [first],
          activeId: "first",
          passiveRemaining: 2,
          hardRemaining: 90,
        }),
      },
    );
    const stale = agentCompletionQueueReducer(newer, {
      type: "response_queue.snapshot",
      event: snapshot({
        revision: 9,
        phase: "idle",
        ordered: [],
        completions: [],
      }),
    });

    expect(stale).toBe(newer);
  });

  it("takes a lower revision from a new connection (a restarted backend counts from zero)", () => {
    const first = response("first", 1);
    const before = agentCompletionQueueReducer(createAgentCompletionQueueState(), {
      type: "response_queue.snapshot",
      connection: 1,
      event: snapshot({ revision: 150, phase: "idle", ordered: [], completions: [] }),
    });
    const sameConnection = agentCompletionQueueReducer(before, {
      type: "response_queue.snapshot",
      connection: 1,
      event: snapshot({ revision: 3, phase: "idle", ordered: [], completions: [] }),
    });
    expect(sameConnection).toBe(before);
    const restarted = agentCompletionQueueReducer(before, {
      type: "response_queue.snapshot",
      connection: 2,
      event: snapshot({ revision: 3, phase: "active_speaking", ordered: ["first"], completions: [first], activeId: "first", passiveRemaining: 6, hardRemaining: 90 }),
    });
    expect(restarted.revision).toBe(3);
    expect(restarted.active?.completion_id).toBe("first");
  });

  it("applies equal-revision snapshots, whose content can still change", () => {
    // The coordinator revision does not cover every snapshot field: the
    // backend rebroadcasts the same revision when the setup delivery preview
    // flips show_summary_text, completion bodies come from a separate activity
    // store, and remaining-seconds fields are time-derived. Skipping equal
    // revisions would freeze those changes.
    const item = response("first", 1);
    const base = agentCompletionQueueReducer(createAgentCompletionQueueState(), {
      type: "response_queue.snapshot",
      event: snapshot({
        revision: 4,
        phase: "active_grace",
        ordered: ["first"],
        completions: [item],
        activeId: "first",
        showSummaryText: false,
      }),
    });
    const next = agentCompletionQueueReducer(base, {
      type: "response_queue.snapshot",
      event: snapshot({
        revision: 4,
        phase: "active_grace",
        ordered: ["first"],
        completions: [item],
        activeId: "first",
        showSummaryText: true,
      }),
    });
    expect(next.showSummaryText).toBe(true);
  });

  it("does not surface a queued response while the active item is hidden", () => {
    const queued = response("queued", 2);
    const state = agentCompletionQueueReducer(
      createAgentCompletionQueueState(),
      {
        type: "response_queue.snapshot",
        event: snapshot({
          revision: 1,
          phase: "active_grace",
          ordered: ["hidden-active", "queued"],
          completions: [queued],
          activeId: "hidden-active",
          passiveRemaining: 6,
        }),
      },
    );

    expect(currentCompletion(state)).toBeNull();
    expect(queuedCompletionCount(state)).toBe(1);
  });

  it("defaults showSummaryText to false and updates from snapshot event", () => {
    const initialState = createAgentCompletionQueueState();
    expect(initialState.showSummaryText).toBe(false);

    const withTrue = agentCompletionQueueReducer(initialState, {
      type: "response_queue.snapshot",
      event: snapshot({
        revision: 1,
        phase: "idle",
        ordered: [],
        completions: [],
        showSummaryText: true,
      }),
    });
    expect(withTrue.showSummaryText).toBe(true);

    const withFalse = agentCompletionQueueReducer(withTrue, {
      type: "response_queue.snapshot",
      event: snapshot({
        revision: 2,
        phase: "idle",
        ordered: [],
        completions: [],
        showSummaryText: false,
      }),
    });
    expect(withFalse.showSummaryText).toBe(false);

    const withUndefined = agentCompletionQueueReducer(withTrue, {
      type: "response_queue.snapshot",
      event: snapshot({
        revision: 3,
        phase: "idle",
        ordered: [],
        completions: [],
      }),
    });
    expect(withUndefined.showSummaryText).toBe(false);
  });
});
