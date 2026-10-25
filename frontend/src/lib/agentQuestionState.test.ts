// @vitest-environment node

import { describe, expect, it } from "vitest";

import {
  agentQuestionReducer,
  createAgentQuestionState,
  exactQuestionAnswers,
  questionCanAnswer,
  toggleQuestionOption,
} from "./agentQuestionState";
import type { AgentQuestion, AgentQuestionSnapshotEvent } from "./socket/agentEvents";

const questions: AgentQuestion[] = [
  {
    question: "Exact release question?",
    header: "Release",
    options: [{ label: "Ship", description: "Publish now" }],
    multiSelect: false,
  },
  {
    question: "Which checks?",
    header: "Checks",
    options: [{ label: "Unit" }, { label: "E2E" }],
    multiSelect: true,
  },
];

function snapshot(
  revision: number,
  phase: NonNullable<AgentQuestionSnapshotEvent["active"]>["phase"],
): AgentQuestionSnapshotEvent {
  return {
    type: "agent.question.snapshot",
    version: 1,
    revision,
    active: {
      session_handle: "session-1",
      generation: 4,
      phase,
      questions,
      current_index: 0,
      request_id: null,
      routable: false,
      capture_available: true,
      remaining_seconds: 0,
      answers: {},
    },
  };
}

describe("agent question state", () => {
  it("replays the newest snapshot and ignores stale generations", () => {
    const current = agentQuestionReducer(createAgentQuestionState(), {
      type: "snapshot",
      event: snapshot(3, "grace"),
    });
    const stale = agentQuestionReducer(current, {
      type: "snapshot",
      event: snapshot(2, "surfaced"),
    });
    expect(stale).toBe(current);
    expect(current.active?.phase).toBe("grace");
  });

  it("takes a lower revision from a new connection", () => {
    const current = agentQuestionReducer(createAgentQuestionState(), {
      type: "snapshot", connection: 1, event: snapshot(30, "grace"),
    });
    expect(agentQuestionReducer(current, { type: "snapshot", connection: 1, event: snapshot(2, "surfaced") })).toBe(current);
    expect(agentQuestionReducer(current, { type: "snapshot", connection: 2, event: snapshot(2, "surfaced") }).active?.phase).toBe("surfaced");
  });

  it("maps option and freeform answers under verbatim question keys", () => {
    expect(exactQuestionAnswers(
      questions,
      {
        "Exact release question?": ["Ship"],
        "Which checks?": ["Unit", "E2E"],
      },
      {},
    )).toEqual({
      "Exact release question?": "Ship",
      "Which checks?": "Unit, E2E",
    });
    expect(exactQuestionAnswers(
      questions,
      { "Which checks?": ["Unit"] },
      { "Exact release question?": "Use a typed answer" },
    )).toEqual({
      "Exact release question?": "Use a typed answer",
      "Which checks?": "Unit",
    });
  });

  it("toggles multi-select while single-select replaces and terminal phases close", () => {
    expect(toggleQuestionOption(questions[0], [], "Ship")).toEqual(["Ship"]);
    expect(toggleQuestionOption(questions[1], ["Unit"], "E2E"))
      .toEqual(["Unit", "E2E"]);
    expect(toggleQuestionOption(questions[1], ["Unit", "E2E"], "Unit"))
      .toEqual(["E2E"]);
    expect(questionCanAnswer(snapshot(1, "surfaced").active)).toBe(true);
    expect(questionCanAnswer(snapshot(2, "held_failed").active)).toBe(false);
  });
});
