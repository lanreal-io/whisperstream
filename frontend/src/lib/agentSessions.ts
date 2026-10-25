import type { AgentSessionsSnapshotEvent, HudSession } from "./socket/agentEvents";

/**
 * A rendered copy of the backend session list plus the tab selection. There
 * is deliberately no local session ordering or naming logic: arrival order
 * and `source_name` both come from the backend snapshot verbatim.
 */
export interface AgentSessionsState {
  sessions: HudSession[];
  activeHandle: string | null;
}

export type AgentSessionsAction =
  | { type: "agent.sessions"; event: AgentSessionsSnapshotEvent }
  | { type: "select"; handle: string };

export function createAgentSessionsState(): AgentSessionsState {
  return { sessions: [], activeHandle: null };
}

function indexOfHandle(sessions: HudSession[], handle: string | null): number {
  if (handle === null) return -1;
  return sessions.findIndex((session) => session.session_handle === handle);
}

/**
 * Applies a backend session snapshot. A session arriving or a different
 * session leaving never moves an existing, still-alive selection (design
 * decision: never re-target what the user is reading). When the selected
 * session itself is gone the selection clears: an open console collapses
 * rather than silently moving to another conversation.
 */
export function agentSessionsReducer(
  state: AgentSessionsState,
  action: AgentSessionsAction,
): AgentSessionsState {
  if (action.type === "select") {
    if (indexOfHandle(state.sessions, action.handle) === -1) return state;
    return { ...state, activeHandle: action.handle };
  }

  const sessions = action.event.sessions;
  const alive = indexOfHandle(sessions, state.activeHandle) !== -1;
  return { sessions, activeHandle: alive ? state.activeHandle : null };
}

/** What last moved a card's strip: a response that started speaking (by its
 *  conversation handle) or a tab the user picked. */
export type StripSwitch =
  | { kind: "speech"; conversationHandle: string | null }
  | { kind: "pick"; sessionHandle: string };

/**
 * The tab a card's strip marks (owner, 2026-10-25). Only two things switch
 * it, and neither pins it: a response starting to speak moves it to that
 * conversation's tab (none when no tab owns it), and the user picking a tab
 * moves it there. Typing never switches it; it holds the voice queue, so the
 * draft's session stays marked until a send lets the next response speak.
 * A remembered console selection used to outrank the speaking conversation,
 * and an unowned conversation fell back to the first tab, so the strip named
 * another agent as the speaker.
 */
export function stripTabFor(
  state: AgentSessionsState,
  last: StripSwitch | null,
): string | null {
  if (last === null) return null;
  if (last.kind === "pick") {
    return indexOfHandle(state.sessions, last.sessionHandle) !== -1 ? last.sessionHandle : null;
  }
  if (last.conversationHandle === null) return null;
  return (
    state.sessions.find((session) => session.conversation_handle === last.conversationHandle)
      ?.session_handle ?? null
  );
}

/**
 * Selection on (re-)expansion (design.md decision 13): reselect the
 * previously selected session if it is still alive, otherwise the session
 * whose conversation the completion FIFO is on (hook items carry the
 * conversation handle, not the session handle), otherwise the first.
 */
export function selectSessionOnExpand(
  state: AgentSessionsState,
  fifoConversationHandle: string | null,
): AgentSessionsState {
  if (indexOfHandle(state.sessions, state.activeHandle) !== -1) return state;
  const owner =
    fifoConversationHandle === null
      ? undefined
      : state.sessions.find(
          (session) => session.conversation_handle === fifoConversationHandle,
        );
  if (owner !== undefined) return { ...state, activeHandle: owner.session_handle };
  return { ...state, activeHandle: state.sessions[0]?.session_handle ?? null };
}

/**
 * Session shortcut while the HUD has keyboard focus: Ctrl+1-9 (Windows) or
 * Cmd+1-9 (Mac) picks session N; Ctrl+Tab and Ctrl+Shift+Tab step through
 * them. Returns the index to open, or null when the key is not a shortcut.
 */
export function sessionShortcutIndex(
  event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey">,
  state: AgentSessionsState,
  { mac }: { mac: boolean },
): number | null {
  const count = state.sessions.length;
  if (count === 0 || event.altKey) return null;
  const primary = mac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
  if (primary && /^[1-9]$/.test(event.key)) {
    const index = Number(event.key) - 1;
    return index < count ? index : null;
  }
  if (event.ctrlKey && !event.metaKey && event.key === "Tab") {
    const current = indexOfHandle(state.sessions, state.activeHandle);
    const step = event.shiftKey ? -1 : 1;
    return current === -1 ? 0 : (current + step + count) % count;
  }
  return null;
}

/** Fewer than two sessions means no strip at all (design decision 3). */
export function hasSessionStrip(state: AgentSessionsState): boolean {
  return state.sessions.length >= 2;
}
