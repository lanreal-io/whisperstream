import { describe, expect, it } from "vitest";
import {
  agentReplyReducer,
  bufferedRevision,
  bufferedText,
  createAgentReplyState,
  draftRefused,
  editBuffer,
  pruneBuffers,
  settleBuffer,
  type ReplyBuffers,
} from "./agentReplyState";
import type { AgentReplyDraft, AgentReplySnapshotEvent } from "./socket/agentEvents";

const T = "conv:c1";

function draft(overrides: Partial<AgentReplyDraft> = {}): AgentReplyDraft {
  return {
    draft_id: "d1",
    target_id: T,
    kind: "reply",
    phase: "editing",
    text: "",
    text_revision: 0,
    compose_generation: null,
    capture_mode: null,
    held: false,
    delivery: "now",
    outcome: null,
    ...overrides,
  };
}

function snapshot(revision: number, drafts: AgentReplyDraft[], epoch = "e1"): AgentReplySnapshotEvent {
  return {
    type: "agent.reply.snapshot",
    version: 1,
    epoch,
    revision,
    foreground: null,
    drafts,
    receipts: [],
    enter_owner: "none",
  };
}

describe("agentReplyReducer", () => {
  it("ignores a reordered older snapshot from the same epoch", () => {
    const newer = agentReplyReducer(createAgentReplyState(), { type: "snapshot", event: snapshot(5, [draft({ text: "new" })]) });
    const older = agentReplyReducer(newer, { type: "snapshot", event: snapshot(3, [draft({ text: "old" })]) });
    expect(older.drafts[T].text).toBe("new");
  });

  it("accepts any revision from a new backend epoch", () => {
    const first = agentReplyReducer(createAgentReplyState(), { type: "snapshot", event: snapshot(9, [draft()]) });
    const restarted = agentReplyReducer(first, { type: "snapshot", event: snapshot(1, [], "e2") });
    expect(restarted.drafts).toEqual({});
  });
});

describe("keystroke buffers", () => {
  it("keeps keys typed before any acknowledgement and pipelines revisions", () => {
    let buffers: ReplyBuffers = {};
    let step = editBuffer(buffers, T, undefined, "h");
    expect(step.baseRevision).toBe(0);
    buffers = step.buffers;
    step = editBuffer(buffers, T, undefined, "hi");
    expect(step.baseRevision).toBe(1);
    buffers = step.buffers;
    expect(bufferedText(buffers, T, undefined)).toBe("hi");
    expect(bufferedRevision(buffers, T, undefined)).toBe(2);
  });

  it("never lets an echo overwrite newer local keystrokes", () => {
    let buffers = editBuffer({}, T, undefined, "h").buffers;
    buffers = editBuffer(buffers, T, undefined, "he").buffers;
    // The echo of the first update arrives while the second is in flight.
    const echo = draft({ text: "h", text_revision: 1 });
    buffers = settleBuffer(buffers, T, "applied");
    expect(bufferedText(buffers, T, echo)).toBe("he");
  });

  it("lets the backend win once it moves past the buffer (dictation appended)", () => {
    let buffers = editBuffer({}, T, undefined, "typed").buffers;
    buffers = settleBuffer(buffers, T, "applied");
    const appended = draft({ text: "typed and spoken", text_revision: 2, phase: "ready" });
    expect(bufferedText(buffers, T, appended)).toBe("typed and spoken");
    expect(bufferedRevision(buffers, T, appended)).toBe(2);
  });

  it.each(["conflict", "stale", "refused"] as const)(
    "lets the backend text win after %s, with no resend",
    (result) => {
      const buffers = editBuffer({}, T, draft({ text: "x", text_revision: 1 }), "xy").buffers;
      const moved = draft({ text: "x spoken", text_revision: 2 });
      const settled = settleBuffer(buffers, T, result);
      expect(settled[T]).toBeUndefined();
      expect(bufferedText(settled, T, moved)).toBe("x spoken");
    },
  );

  it("forgets a buffer once its draft is gone and nothing is in flight", () => {
    let buffers = editBuffer({}, T, undefined, "sent text").buffers;
    buffers = settleBuffer(buffers, T, "applied");
    expect(pruneBuffers(buffers, {}, null)).toEqual({});
    expect(pruneBuffers(buffers, {}, T)).toBe(buffers);
  });
});

describe("draftRefused", () => {
  it("is true only for a refusal at the current revision", () => {
    const outcome = { revision: 0, result: "refused" as const, reason: "refused" as const };
    expect(draftRefused(draft({ outcome }))).toBe(true);
    expect(draftRefused(draft({ text_revision: 1, outcome }))).toBe(false);
    expect(draftRefused(undefined)).toBe(false);
  });
});
