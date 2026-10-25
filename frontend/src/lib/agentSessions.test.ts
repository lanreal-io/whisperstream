import { describe, expect, it } from "vitest";
import {
  agentSessionsReducer,
  createAgentSessionsState,
  hasSessionStrip,
  selectSessionOnExpand,
  stripTabFor,
  sessionShortcutIndex,
} from "./agentSessions";
import type { AgentSessionsSnapshotEvent, HudSession } from "./socket/agentEvents";

function session(handle: string, overrides: Partial<HudSession> = {}): HudSession {
  return {
    session_handle: handle,
    agent: "claude",
    source_name: handle,
    unread: false,
    needs_input: false,
    can_reply: true,
    ...overrides,
  };
}

function snapshot(sessions: HudSession[]): AgentSessionsSnapshotEvent {
  return { type: "agent.sessions", sessions };
}

function applySnapshot(
  state: ReturnType<typeof createAgentSessionsState>,
  sessions: HudSession[],
) {
  return agentSessionsReducer(state, {
    type: "agent.sessions",
    event: snapshot(sessions),
  });
}

describe("agent session list state", () => {
  it("preserves backend arrival order when applying a snapshot", () => {
    const state = applySnapshot(createAgentSessionsState(), [
      session("c"),
      session("a"),
      session("b"),
    ]);

    expect(state.sessions.map((s) => s.session_handle)).toEqual([
      "c",
      "a",
      "b",
    ]);
  });

  it("does not move the selection when a new session arrives", () => {
    let state = applySnapshot(createAgentSessionsState(), [
      session("a"),
      session("b"),
    ]);
    state = agentSessionsReducer(state, { type: "select", handle: "b" });

    state = applySnapshot(state, [session("a"), session("b"), session("c")]);

    expect(state.activeHandle).toBe("b");
  });

  it("leaves the selection alone when a non-selected session is removed", () => {
    let state = applySnapshot(createAgentSessionsState(), [
      session("a"),
      session("b"),
      session("c"),
    ]);
    state = agentSessionsReducer(state, { type: "select", handle: "b" });

    state = applySnapshot(state, [session("a"), session("b")]);

    expect(state.activeHandle).toBe("b");
  });

  it("clears the selection, never retargets, when the selected session is removed", () => {
    // An open console collapses instead of silently moving to a survivor
    // whose draft Enter or push-to-talk could then reach.
    let state = applySnapshot(createAgentSessionsState(), [
      session("a"),
      session("b"),
      session("c"),
    ]);
    state = agentSessionsReducer(state, { type: "select", handle: "b" });

    state = applySnapshot(state, [session("a"), session("c")]);

    expect(state.activeHandle).toBeNull();
  });

  it("clears the selection when the last surviving session is removed", () => {
    let state = applySnapshot(createAgentSessionsState(), [session("a")]);
    state = agentSessionsReducer(state, { type: "select", handle: "a" });

    state = applySnapshot(state, []);

    expect(state.activeHandle).toBeNull();
    expect(state.sessions).toEqual([]);
  });

  it("reselects the previously selected session on expand if it is still alive", () => {
    let state = applySnapshot(createAgentSessionsState(), [
      session("a"),
      session("b"),
    ]);
    state = agentSessionsReducer(state, { type: "select", handle: "b" });

    state = selectSessionOnExpand(state, "a");

    expect(state.activeHandle).toBe("b");
  });

  it("falls back to the session that owns the FIFO conversation on expand", () => {
    // Hook items carry the conversation handle, not the session handle.
    let state = applySnapshot(createAgentSessionsState(), [
      session("a", { conversation_handle: "conv-a" }),
      session("c", { conversation_handle: "conv-c" }),
    ]);

    state = selectSessionOnExpand(state, "conv-c");

    expect(state.activeHandle).toBe("c");
  });

  it("maps Ctrl+1-9 on Windows, Cmd+1-9 on Mac and Ctrl+Tab to sessions", () => {
    let state = applySnapshot(createAgentSessionsState(), [session("a"), session("b"), session("c")]);
    state = agentSessionsReducer(state, { type: "select", handle: "c" });
    const key = (k: string, mods: Partial<KeyboardEvent> = {}) => ({
      key: k, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, ...mods,
    });
    expect(sessionShortcutIndex(key("2", { ctrlKey: true }), state, { mac: false })).toBe(1);
    expect(sessionShortcutIndex(key("2", { metaKey: true }), state, { mac: true })).toBe(1);
    expect(sessionShortcutIndex(key("2", { metaKey: true }), state, { mac: false })).toBeNull();
    expect(sessionShortcutIndex(key("2"), state, { mac: false })).toBeNull();
    expect(sessionShortcutIndex(key("9", { ctrlKey: true }), state, { mac: false })).toBeNull();
    expect(sessionShortcutIndex(key("Tab", { ctrlKey: true }), state, { mac: false })).toBe(0);
    expect(sessionShortcutIndex(key("Tab", { ctrlKey: true, shiftKey: true }), state, { mac: true })).toBe(1);
  });

  it("falls back to the arrival head on expand when neither prior selection nor FIFO-active survives", () => {
    let state = applySnapshot(createAgentSessionsState(), [
      session("a"),
      session("b"),
    ]);

    state = selectSessionOnExpand(state, "does-not-exist");

    expect(state.activeHandle).toBe("a");
  });

  it("reports no strip below two sessions and a strip at two or more", () => {
    const empty = createAgentSessionsState();
    expect(hasSessionStrip(empty)).toBe(false);

    const one = applySnapshot(empty, [session("a")]);
    expect(hasSessionStrip(one)).toBe(false);

    const two = applySnapshot(empty, [session("a"), session("b")]);
    expect(hasSessionStrip(two)).toBe(true);
  });
});

describe("stripTabFor", () => {
  const sessions = [
    { session_handle: "s1", conversation_handle: "conv-a" },
    { session_handle: "s2", conversation_handle: "conv-b" },
  ] as never;
  const state = (activeHandle: string | null) => ({ sessions, activeHandle });

  it("marks the speaking conversation's tab, whatever the console last selected", () => {
    expect(stripTabFor(state("s1"), { kind: "speech", conversationHandle: "conv-b" })).toBe("s2");
  });

  it("marks a tab the user picked until the next response speaks", () => {
    expect(stripTabFor(state("s2"), { kind: "pick", sessionHandle: "s1" })).toBe("s1");
  });

  it("marks no tab for speech no tab owns, a vanished pick, or nothing yet", () => {
    expect(stripTabFor(state("s1"), { kind: "speech", conversationHandle: "conv-hook-only" })).toBeNull();
    expect(stripTabFor(state("s1"), { kind: "speech", conversationHandle: null })).toBeNull();
    expect(stripTabFor(state("s1"), { kind: "pick", sessionHandle: "gone" })).toBeNull();
    expect(stripTabFor(state("s1"), null)).toBeNull();
  });
});
