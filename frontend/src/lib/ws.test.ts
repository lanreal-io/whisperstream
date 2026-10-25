// @vitest-environment node

/**
 * Tests for the subscribeToDownloadEvents helper in ws.ts.
 *
 * Covers Task 2.1's requirement for a kind-filtered WebSocket subscription
 * helper that allows the Enhancement tab to subscribe to "local_llm" download
 * events without crossing with Parakeet events (and vice versa).
 */
import { describe, it, expect, vi } from "vitest";
import { logBenchmarkEvent } from "./socket/events";
import { parseAgentComposeEvent, parseAgentInteractionEvent, parseAgentQuestionEvent, parseAgentReplyEvent, parseAgentResponseQueueEvent, parseAgentSessionsEvent, sendAgentDraftSubmit, sendAgentDraftUpdate, sendAgentHudPresented, sendAgentQuestionOpportunityAck } from "./socket/agentEvents";
import { parseTranscriptBakeoffEvent } from "./socket/bakeoffEvents";
import { subscribeToDownloadEvents } from "./socket/downloads";
import type { AgentCompletionAcknowledgedEvent, AgentCompletionUpsertEvent, AgentResponseCompletion, AgentResponseQueueSnapshotEvent } from "./socket/agentEvents";
import type { AppEvent, EventHandler } from "./socket/events";

describe("agent compose preview presentation contract", () => {
  const preview = {
    type: "agent.compose.preview",
    version: 1,
    session_handle: "opaque-session",
    generation: 2,
    recording_id: 3,
    revision: 4,
    text: "cumulative preview",
    stable_char_count: 5,
    presentation_version: 1,
    mutable_start_char_count: 11,
    freeze: false,
  };

  it("accepts the complete backend presentation metadata", () => {
    expect(parseAgentComposeEvent(preview)).toEqual(preview);
  });

  it("rejects missing or unsupported presentation metadata", () => {
    const { mutable_start_char_count: _missing, ...withoutBoundary } =
      preview;
    expect(parseAgentComposeEvent(withoutBoundary)).toBeNull();
    expect(
      parseAgentComposeEvent({
        ...preview,
        presentation_version: 2,
      }),
    ).toBeNull();
  });
});

describe("agent completion v1 contracts", () => {
  it("uses privacy-minimal completion payloads and authoritative live pending IDs", () => {
    const completion: AgentResponseCompletion = {
      completion_id: "completion-1",
      sequence: 1,
      kind: "response",
      text: "Finished",
      agent: "claude",
      source_label: "Claude · whisperstream",
      empty: false,
    };
    const event: AgentCompletionUpsertEvent = {
      type: "agent.completion",
      version: 1,
      show_text: true,
      completion,
      pending_completion_ids: ["overflow", "completion-1"],
    };

    expect(event.pending_completion_ids).toEqual([
      "overflow",
      "completion-1",
    ]);
    expect(event.completion).not.toHaveProperty("session_id");
    expect(event.completion).not.toHaveProperty("source_key");
  });

  it("accepts a response completion carrying source_name and source_name_is_custom", () => {
    const completion: AgentResponseCompletion = {
      completion_id: "completion-1",
      sequence: 1,
      kind: "response",
      text: "Finished",
      agent: "claude",
      source_label: "Claude · whisperstream",
      empty: false,
      source_name: "speak-harness",
      source_name_is_custom: true,
    };
    const event: AgentCompletionUpsertEvent = {
      type: "agent.completion",
      version: 1,
      show_text: true,
      completion,
      pending_completion_ids: ["completion-1"],
    };

    expect(parseAgentResponseQueueEvent({
      type: "agent.response_queue.snapshot",
      version: 1,
      revision: 1,
      phase: "idle",
      ordered_completion_ids: ["completion-1"],
      completions: [event.completion],
      active: null,
      quiet_delay_remaining_seconds: 0,
    })).not.toBeNull();
  });

  it("accepts Codex completions and rejects unknown providers", () => {
    const completion = {
      completion_id: "completion-codex",
      sequence: 2,
      kind: "response",
      text: "Finished",
      agent: "codex",
      source_label: "Codex whisperstream",
      empty: false,
      source_name: "Codex",
    };
    const snapshot = {
      type: "agent.response_queue.snapshot",
      version: 1,
      revision: 2,
      phase: "idle",
      ordered_completion_ids: ["completion-codex"],
      completions: [completion],
      active: null,
      quiet_delay_remaining_seconds: 0,
    };

    expect(parseAgentResponseQueueEvent(snapshot)).toEqual(snapshot);
    expect(parseAgentResponseQueueEvent({
      ...snapshot,
      completions: [{ ...completion, agent: "unknown" }],
    })).toBeNull();
  });

  it("rejects a response completion whose source_name contains a slash", () => {
    const completion = {
      completion_id: "completion-1",
      sequence: 1,
      kind: "response",
      text: "Finished",
      agent: "claude",
      source_label: "Claude · whisperstream",
      empty: false,
      source_name: "app/pty-proxy",
    };
    expect(parseAgentResponseQueueEvent({
      type: "agent.response_queue.snapshot",
      version: 1,
      revision: 1,
      phase: "idle",
      ordered_completion_ids: ["completion-1"],
      completions: [completion],
      active: null,
      quiet_delay_remaining_seconds: 0,
    })).toBeNull();
  });

  it("rejects a response completion whose source_name contains a control character", () => {
    const completion = {
      completion_id: "completion-1",
      sequence: 1,
      kind: "response",
      text: "Finished",
      agent: "claude",
      source_label: "Claude · whisperstream",
      empty: false,
      source_name: "speak\x00harness",
    };
    expect(parseAgentResponseQueueEvent({
      type: "agent.response_queue.snapshot",
      version: 1,
      revision: 1,
      phase: "idle",
      ordered_completion_ids: ["completion-1"],
      completions: [completion],
      active: null,
      quiet_delay_remaining_seconds: 0,
    })).toBeNull();
  });

  it("still validates a response completion carrying neither new field (backwards compatibility)", () => {
    const completion: AgentResponseCompletion = {
      completion_id: "completion-1",
      sequence: 1,
      kind: "response",
      text: "Finished",
      agent: "claude",
      source_label: "Claude · whisperstream",
      empty: false,
    };
    expect(parseAgentResponseQueueEvent({
      type: "agent.response_queue.snapshot",
      version: 1,
      revision: 1,
      phase: "idle",
      ordered_completion_ids: ["completion-1"],
      completions: [completion],
      active: null,
      quiet_delay_remaining_seconds: 0,
    })).not.toBeNull();
  });

  it("defines the v1 server acknowledgement confirmation", () => {
    const event: AgentCompletionAcknowledgedEvent = {
      type: "agent.completion.acknowledged",
      version: 1,
      completion_id: "completion-1",
    };

    expect(event).toEqual({
      type: "agent.completion.acknowledged",
      version: 1,
      completion_id: "completion-1",
    });
  });
});

describe("agent question snapshots", () => {
  const event = {
    type: "agent.question.snapshot",
    version: 1,
    revision: 4,
    active: {
      session_handle: "opaque-session",
      generation: 2,
      phase: "grace",
      questions: [{
        question: "Exact question?",
        header: "Choice",
        options: [{ label: "One", description: "First option" }],
        multiSelect: false,
      }],
      current_index: 0,
      request_id: "opaque-request",
      routable: true,
      capture_available: true,
      remaining_seconds: 30,
      answers: {},
    },
  };

  it("accepts the privacy-minimal exact-generation snapshot", () => {
    expect(parseAgentQuestionEvent(event)).toEqual(event);
  });

  it("accepts an active question snapshot carrying source_name", () => {
    const named = {
      ...event,
      active: { ...event.active, source_name: "pay-kit" },
    };
    expect(parseAgentQuestionEvent(named)).toEqual(named);
  });

  it("rejects an active question snapshot whose source_name is unsafe", () => {
    expect(parseAgentQuestionEvent({
      ...event,
      active: { ...event.active, source_name: "app/pty-proxy" },
    })).toBeNull();
  });

  it("rejects route metadata and malformed option counts", () => {
    expect(parseAgentQuestionEvent({
      ...event,
      active: { ...event.active, raw_session_id: "provider-id" },
    })).toBeNull();
    expect(parseAgentQuestionEvent({
      ...event,
      active: {
        ...event.active,
        questions: [{ ...event.active.questions[0], options: [] }],
      },
    })).toBeNull();
  });

  it("rejects provider-tagged Codex structured questions", () => {
    expect(parseAgentQuestionEvent({
      ...event,
      active: { ...event.active, agent: "codex" },
    })).toBeNull();
  });
});

describe("terminal Agent Speak acknowledgements", () => {
  it("sends exact question generations", () => {
    const send = vi.fn(() => true);

    expect(
      sendAgentQuestionOpportunityAck(send, "opaque-session", 3),
    ).toBe(true);
    expect(send).toHaveBeenCalledWith({
      type: "agent.question.ack",
      version: 1,
      session_handle: "opaque-session",
      generation: 3,
    });
  });

  it("rejects malformed acknowledgement identities", () => {
    const send = vi.fn(() => true);
    expect(sendAgentQuestionOpportunityAck(send, "", 1)).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });
});

describe("reply owner wire", () => {
  const snapshot = {
    type: "agent.reply.snapshot",
    version: 1,
    epoch: "a1b2c3",
    revision: 7,
    foreground: { target_id: "conv:opaque", surface: "console", painted: true, focused: false },
    drafts: [{
      draft_id: "d3",
      target_id: "conv:opaque",
      kind: "reply",
      phase: "ready",
      text: "Ship it",
      text_revision: 2,
      compose_generation: null,
      capture_mode: null,
      held: false,
      delivery: "hold",
      outcome: { revision: 2, result: "refused", reason: "not_delivered" },
    }],
    receipts: [{ target_id: "question:opaque:4", draft_id: "d1", result: "sent", text: "Beta" }],
    enter_owner: "global",
  };

  it("parses the reply snapshot, results and close", () => {
    expect(parseAgentReplyEvent(snapshot)).toEqual(snapshot);
    const result = {
      type: "agent.reply.result", version: 1, command: "agent.draft.update",
      result: "conflict", command_id: 4, draft_id: "d3", target_id: "conv:opaque", text_revision: 2,
    };
    expect(parseAgentReplyEvent(result)).toEqual(result);
    const close = { type: "agent.reply.close", version: 1, target_id: "conv:opaque", surface: "card" };
    expect(parseAgentReplyEvent(close)).toEqual(close);
    expect(parseAgentReplyEvent({ ...close, surface: "pill" })).toBeNull();
    expect(parseAgentReplyEvent({ ...snapshot, drafts: [{ ...snapshot.drafts[0], target_id: "managed:h" }] })).toBeNull();
  });

  it("rejects unknown targets, phases and extra fields", () => {
    expect(parseAgentReplyEvent({ ...snapshot, drafts: [{ ...snapshot.drafts[0], target_id: "raw-session" }] })).toBeNull();
    expect(parseAgentReplyEvent({ ...snapshot, drafts: [{ ...snapshot.drafts[0], phase: "draft_ready" }] })).toBeNull();
    expect(parseAgentReplyEvent({ ...snapshot, raw_session_id: "x" })).toBeNull();
    expect(parseAgentReplyEvent({ ...snapshot, reply_hint_retired: true })).toEqual({ ...snapshot, reply_hint_retired: true });
    expect(parseAgentReplyEvent({ ...snapshot, reply_hint_retired: "yes" })).toBeNull();
    expect(parseAgentReplyEvent({ ...snapshot, foreground: { ...snapshot.foreground, surface: "pill" } })).toBeNull();
  });

  it("sends presented, update and submit commands with exact targets", () => {
    const send = vi.fn(() => true);
    expect(sendAgentHudPresented(send, { seq: 1, target_id: "conv:a", surface: "card", painted: true, focused: false })).toBe(true);
    expect(sendAgentHudPresented(send, { seq: 2, target_id: null, surface: "card", painted: false, focused: false })).toBe(false);
    expect(sendAgentDraftUpdate(send, { command_id: 1, target_id: "conv:a", base_revision: 0, text: "h" })).toBe(true);
    expect(sendAgentDraftSubmit(send, { command_id: 2, target_id: "conv:a", revision: 1, text: "   " })).toBe(false);
    expect(sendAgentDraftSubmit(send, { command_id: 3, target_id: "nope", revision: 1, text: "hi" })).toBe(false);
    expect(send).toHaveBeenNthCalledWith(2, {
      type: "agent.draft.update", version: 1, command_id: 1, target_id: "conv:a", base_revision: 0, text: "h",
    });
  });
});

describe("agent sessions snapshots (Task 4.1)", () => {
  const event = {
    type: "agent.sessions",
    sessions: [
      {
        session_handle: "opaque-session-1",
        agent: "claude",
        source_name: "whisperstream",
        unread: false,
        needs_input: false,
        can_reply: true,
        latest_response: "Finished the implementation.",
        reply_target: "conv:opaque-conversation-1",
      },
      {
        session_handle: "opaque-session-2",
        agent: "codex",
        source_name: "pay-kit",
        unread: true,
        needs_input: false,
        can_reply: false,
        latest_response: null,
        ended: true,
      },
    ],
  };

  it("accepts the privacy-minimal session-list snapshot", () => {
    expect(parseAgentSessionsEvent(event)).toEqual(event);
  });

  it("accepts an empty session list", () => {
    const empty = { type: "agent.sessions", sessions: [] };
    expect(parseAgentSessionsEvent(empty)).toEqual(empty);
  });

  it("rejects a session carrying route metadata (cwd, session_id, a raw token)", () => {
    expect(
      parseAgentSessionsEvent({
        ...event,
        sessions: [{ ...event.sessions[0], cwd: "/repo/one" }],
      }),
    ).toBeNull();
    expect(
      parseAgentSessionsEvent({
        ...event,
        sessions: [{ ...event.sessions[0], session_id: "provider-id" }],
      }),
    ).toBeNull();
    expect(
      parseAgentSessionsEvent({
        ...event,
        sessions: [{ ...event.sessions[0], token: "wrapper-token" }],
      }),
    ).toBeNull();
  });

  it("rejects a session carrying a reason string or an unsafe source_name", () => {
    expect(
      parseAgentSessionsEvent({
        ...event,
        sessions: [{ ...event.sessions[0], reason: "blocked" }],
      }),
    ).toBeNull();
    expect(
      parseAgentSessionsEvent({
        ...event,
        sessions: [{ ...event.sessions[0], source_name: "app/pty-proxy" }],
      }),
    ).toBeNull();
  });

  it("rejects a non-boolean state flag", () => {
    expect(
      parseAgentSessionsEvent({
        ...event,
        sessions: [{ ...event.sessions[0], can_reply: "yes" }],
      }),
    ).toBeNull();
  });
});

describe("backend response queue snapshots", () => {
  const event: AgentResponseQueueSnapshotEvent = {
    type: "agent.response_queue.snapshot",
    version: 1,
    revision: 7,
    phase: "active_grace",
    ordered_completion_ids: ["completion-1", "completion-2"],
    completions: [
      {
        completion_id: "completion-1",
        sequence: 1,
        kind: "response",
        text: "Finished",
        agent: "claude",
        source_label: "Claude Â· whisperstream",
        empty: false,
      },
    ],
    active: {
      completion_id: "completion-1",
      phase: "active_grace",
      request_id: "opaque-request",
      session_handle: "opaque-session",
      generation: 4,
      engaged: false,
      routable: true,
      passive_total_seconds: 6,
      passive_remaining_seconds: 4.5,
      hard_remaining_seconds: 117,
    },
    quiet_delay_remaining_seconds: 0,
  };

  it("parses ordered identity and authoritative timer state", () => {
    expect(parseAgentResponseQueueEvent(event)).toEqual(event);
  });

  it("accepts provider identity on Codex response state", () => {
    const codex = {
      ...event,
      completions: [{
        ...event.completions[0],
        agent: "codex",
        source_label: "Codex whisperstream",
      }],
      active: { ...event.active!, agent: "codex" },
    };

    expect(parseAgentResponseQueueEvent(codex)).toEqual(codex);
    expect(parseAgentResponseQueueEvent({
      ...codex,
      active: { ...codex.active, agent: "other" },
    })).toBeNull();
    expect(parseAgentResponseQueueEvent({
      ...codex,
      active: { ...codex.active, agent: "claude" },
    })).toBeNull();
  });

  it("rejects invalid phases and negative remaining time", () => {
    expect(
      parseAgentResponseQueueEvent({ ...event, phase: "frontend_dwell" }),
    ).toBeNull();
    expect(
      parseAgentResponseQueueEvent({
        ...event,
        active: {
          ...event.active!,
          passive_remaining_seconds: -1,
        },
      }),
    ).toBeNull();
  });

  it("requires the active phase to agree with the queue phase", () => {
    expect(
      parseAgentResponseQueueEvent({
        ...event,
        phase: "quiet_delay",
      }),
    ).toBeNull();
  });

  it("rejects reply draft fields on the response queue", () => {
    // Drafts belong to the reply owner (agent.reply.snapshot), never here.
    expect(
      parseAgentResponseQueueEvent({
        ...event,
        active: { ...event.active!, draft_text: "stale" },
      }),
    ).toBeNull();
    expect(parseAgentResponseQueueEvent({ ...event, retained_drafts: [] })).toBeNull();
  });

  it("accepts show_summary_text boolean or omitted, and rejects invalid types", () => {
    const withTrue = { ...event, show_summary_text: true };
    expect(parseAgentResponseQueueEvent(withTrue)).toEqual(withTrue);

    const withFalse = { ...event, show_summary_text: false };
    expect(parseAgentResponseQueueEvent(withFalse)).toEqual(withFalse);

    const omitted = { ...event };
    delete (omitted as any).show_summary_text;
    expect(parseAgentResponseQueueEvent(omitted)).toEqual(omitted);

    expect(
      parseAgentResponseQueueEvent({
        ...event,
        show_summary_text: "true",
      }),
    ).toBeNull();
    expect(
      parseAgentResponseQueueEvent({
        ...event,
        show_summary_text: 1,
      }),
    ).toBeNull();
  });

  it("accepts passive_total_seconds up to 30 and rejects values exceeding 30", () => {
    const twenty = {
      ...event,
      active: { ...event.active!, passive_total_seconds: 20 },
    };
    expect(parseAgentResponseQueueEvent(twenty)).toEqual(twenty);

    const thirty = {
      ...event,
      active: { ...event.active!, passive_total_seconds: 30 },
    };
    expect(parseAgentResponseQueueEvent(thirty)).toEqual(thirty);

    expect(
      parseAgentResponseQueueEvent({
        ...event,
        active: { ...event.active!, passive_total_seconds: 31 },
      }),
    ).toBeNull();
  });
});

describe("agent interaction state", () => {
  const event = {
    type: "agent.interaction.state",
    version: 1,
    interaction: {
      request_id: "opaque-request",
      session_handle: "opaque-session",
      kind: "permission",
      sequence: 1,
      status: "pending",
      active: true,
      engaged: false,
      passive_seconds: 6,
      remaining_seconds: 6,
      hard_remaining_seconds: 120,
    },
  };

  it("accepts a permission interaction carrying source_name", () => {
    const named = {
      ...event,
      interaction: { ...event.interaction, source_name: "pay kit" },
    };
    expect(parseAgentInteractionEvent(named)).toEqual(named);
  });

  it("accepts Codex permission identity and rejects unknown providers", () => {
    const codex = {
      ...event,
      interaction: { ...event.interaction, agent: "codex" },
    };
    expect(parseAgentInteractionEvent(codex)).toEqual(codex);
    expect(parseAgentInteractionEvent({
      ...event,
      interaction: { ...event.interaction, agent: "unknown" },
    })).toBeNull();
  });

  it("accepts an interaction with source_name explicitly null", () => {
    const unnamed = {
      ...event,
      interaction: { ...event.interaction, source_name: null },
    };
    expect(parseAgentInteractionEvent(unnamed)).toEqual(unnamed);
  });

  it("still validates an interaction carrying no source_name field (backwards compatibility)", () => {
    expect(parseAgentInteractionEvent(event)).toEqual(event);
  });

  it("rejects an interaction whose source_name contains a slash", () => {
    expect(
      parseAgentInteractionEvent({
        ...event,
        interaction: { ...event.interaction, source_name: "app/pty-proxy" },
      }),
    ).toBeNull();
  });

  it("rejects an interaction whose source_name contains a control character", () => {
    expect(
      parseAgentInteractionEvent({
        ...event,
        interaction: { ...event.interaction, source_name: "pay\x00kit" },
      }),
    ).toBeNull();
  });

  it("accepts passive_seconds up to 30 and rejects values exceeding 30", () => {
    const twenty = {
      ...event,
      interaction: { ...event.interaction, passive_seconds: 20 },
    };
    expect(parseAgentInteractionEvent(twenty)).toEqual(twenty);

    const thirty = {
      ...event,
      interaction: { ...event.interaction, passive_seconds: 30 },
    };
    expect(parseAgentInteractionEvent(thirty)).toEqual(thirty);

    expect(
      parseAgentInteractionEvent({
        ...event,
        interaction: { ...event.interaction, passive_seconds: 31 },
      }),
    ).toBeNull();
  });
});

describe("benchmark event diagnostics", () => {
  it("logs the exact content-free payload with model and device identity", () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
    const event: AppEvent = {
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "Test GPU",
      device_index: 1,
      wps: 42,
    };

    logBenchmarkEvent(event);

    expect(log).toHaveBeenCalledWith(
      `[Benchmark] event receive payload=${JSON.stringify(event)}`,
    );
  });
});

describe("enhancement Bakeoff WebSocket contracts", () => {
  const metadata = {
    serving_contract: "wrap-v2" as const,
    device_mode: "cpu" as const,
    resident_warm: false,
    load_ms: 12,
    inference_ms: 34,
    execution_mode: "whole" as const,
    chunk_count: 1,
  };

  it("accepts started, progress, result, and revoked family members", () => {
    expect(parseTranscriptBakeoffEvent({
      type: "enhancement.bakeoff.started",
      version: 1,
      run_id: "run-1",
      generation: 1,
      total_count: 2,
    })).not.toBeNull();
    expect(parseTranscriptBakeoffEvent({
      type: "enhancement.bakeoff.progress",
      version: 1,
      run_id: "run-1",
      generation: 1,
      pair_index: 0,
      state: "running",
      finished_count: 0,
      total_count: 2,
      error_code: null,
      metadata,
    })).not.toBeNull();
    expect(parseTranscriptBakeoffEvent({
      type: "enhancement.bakeoff.result",
      version: 1,
      run_id: "run-1",
      generation: 1,
      pair_index: 0,
      state: "complete",
      finished_count: 1,
      total_count: 2,
      output: "Private result stays on the event only.",
      metadata,
    })).not.toBeNull();
    expect(parseTranscriptBakeoffEvent({
      type: "enhancement.bakeoff.revoked",
      version: 1,
      run_id: "run-1",
      generation: 1,
      reason: "lockdown_locked",
    })).not.toBeNull();
  });

  it("rejects malformed generations, indexes, metadata, and result output", () => {
    const base = {
      type: "enhancement.bakeoff.result",
      version: 1,
      run_id: "run-1",
      generation: 1,
      pair_index: 0,
      state: "complete",
      finished_count: 1,
      total_count: 2,
      output: "Result",
      metadata,
    };
    expect(parseTranscriptBakeoffEvent({ ...base, generation: 0 })).toBeNull();
    expect(parseTranscriptBakeoffEvent({ ...base, pair_index: 2 })).toBeNull();
    expect(parseTranscriptBakeoffEvent({
      ...base,
      metadata: { ...metadata, device_mode: "cuda" },
    })).toBeNull();
    expect(parseTranscriptBakeoffEvent({ ...base, output: null })).toBeNull();
    expect(parseTranscriptBakeoffEvent({
      ...base,
      raw_text: "forbidden",
    })).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Minimal subscribe factory — records calls, returns the registered handler. */
function makeSubscribe() {
  let registered: EventHandler | null = null;
  const unsubscribeMock = vi.fn();
  const subscribe = vi.fn((handler: EventHandler) => {
    registered = handler;
    return unsubscribeMock;
  });
  const emit = (event: AppEvent) => {
    if (registered) registered(event);
  };
  return { subscribe, emit, unsubscribeMock };
}

// ---------------------------------------------------------------------------
// kind="local_llm" subscription
// ---------------------------------------------------------------------------

describe("subscribeToDownloadEvents with kind='local_llm'", () => {
  it("forwards download.progress events tagged kind=local_llm", () => {
    const { subscribe, emit } = makeSubscribe();
    const handler = vi.fn();
    subscribeToDownloadEvents(subscribe, handler, "local_llm");

    emit({
      type: "download.progress",
      downloaded: 512,
      total: 1024,
      speed_bps: 100,
      kind: "local_llm",
    });

    expect(handler).toHaveBeenCalledOnce();
    expect(handler.mock.calls[0][0]).toMatchObject({ type: "download.progress", kind: "local_llm" });
  });

  it("forwards download.complete events tagged kind=local_llm", () => {
    const { subscribe, emit } = makeSubscribe();
    const handler = vi.fn();
    subscribeToDownloadEvents(subscribe, handler, "local_llm");

    emit({ type: "download.complete", kind: "local_llm" });

    expect(handler).toHaveBeenCalledOnce();
  });

  it("ignores Parakeet download.progress events (no kind tag)", () => {
    const { subscribe, emit } = makeSubscribe();
    const handler = vi.fn();
    subscribeToDownloadEvents(subscribe, handler, "local_llm");

    // Parakeet events have no kind field
    emit({ type: "download.progress", downloaded: 100, total: 600, speed_bps: 50 });

    expect(handler).not.toHaveBeenCalled();
  });

  it("ignores events with a different kind value", () => {
    const { subscribe, emit } = makeSubscribe();
    const handler = vi.fn();
    subscribeToDownloadEvents(subscribe, handler, "local_llm");

    emit({ type: "download.complete", kind: "parakeet" } as AppEvent);

    expect(handler).not.toHaveBeenCalled();
  });

  it("ignores non-download event types entirely", () => {
    const { subscribe, emit } = makeSubscribe();
    const handler = vi.fn();
    subscribeToDownloadEvents(subscribe, handler, "local_llm");

    emit({ type: "state.recording", recording: true });
    emit({ type: "model.loaded" });

    expect(handler).not.toHaveBeenCalled();
  });

  it("returns the unsubscribe function from the raw subscribe call", () => {
    const { subscribe, unsubscribeMock } = makeSubscribe();
    const handler = vi.fn();
    const unsub = subscribeToDownloadEvents(subscribe, handler, "local_llm");

    unsub();

    expect(unsubscribeMock).toHaveBeenCalledOnce();
  });
});

// ---------------------------------------------------------------------------
// kind=undefined subscription (legacy Parakeet path)
// ---------------------------------------------------------------------------

describe("subscribeToDownloadEvents with kind=undefined (Parakeet legacy)", () => {
  it("forwards download.progress events without a kind tag", () => {
    const { subscribe, emit } = makeSubscribe();
    const handler = vi.fn();
    subscribeToDownloadEvents(subscribe, handler, undefined);

    emit({ type: "download.progress", downloaded: 200, total: 600, speed_bps: 80 });

    expect(handler).toHaveBeenCalledOnce();
  });

  it("ignores local_llm tagged events", () => {
    const { subscribe, emit } = makeSubscribe();
    const handler = vi.fn();
    subscribeToDownloadEvents(subscribe, handler, undefined);

    emit({ type: "download.progress", downloaded: 200, total: 600, speed_bps: 80, kind: "local_llm" });

    expect(handler).not.toHaveBeenCalled();
  });

  it("forwards download.error events without a kind tag", () => {
    const { subscribe, emit } = makeSubscribe();
    const handler = vi.fn();
    subscribeToDownloadEvents(subscribe, handler, undefined);

    emit({ type: "download.error", message: "network error" });

    expect(handler).toHaveBeenCalledOnce();
  });
});
