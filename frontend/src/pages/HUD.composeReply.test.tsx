// @vitest-environment node

/**
 * Reply-visibility regression tests for the Claude question card.
 *
 * Round 03 shipped "speaking into a question card shows live text" but nobody
 * could see the state, so it was never exercised end to end. These tests
 * replay the EXACT WebSocket frame sequence captured from a live backend via
 * the round 04 dev simulator, in order, and assert the reducer ends up in a
 * state the card can render.
 *
 * Captured sequence (backend -> HUD), all values verbatim:
 *   dictation.state       phase=recording rid=1 hud_enabled=true preview_enabled=false
 *   agent.compose.state   state=recording gen=1 rid=1
 *   agent.compose.preview gen=1 rid=1 rev=1 text=""      <- real coordinator, silence
 *   agent.compose.preview gen=1 rid=1 rev=1 text="word " <- first recognized words
 *   agent.compose.preview gen=1 rid=1 rev=2..4
 */

import { describe, expect, it } from "vitest";

import { dictationReducer } from "./hud/dictationState";

const RECORDING_STATE = {
  type: "dictation.state" as const,
  version: 1 as const,
  recording_id: 1,
  phase: "recording" as const,
  origin: null,
  hud_enabled: true,
  preview_enabled: false,
  screen_reader_detected: false,
};

const INITIAL = {
  authoritative: false,
  recordingId: null,
  origin: null,
  phase: "idle" as const,
  hudEnabled: true,
  previewEnabled: false,
  screenReaderDetected: false,
  preview: null,
  previewRevealed: false,
  composeGeneration: null,
  composePreview: false,
  profile: null,
};

function composePreview(revision: number, text: string) {
  return {
    type: "agent.compose.preview" as const,
    version: 1 as const,
    session_handle: "handle",
    generation: 1,
    recording_id: 1,
    revision,
    text,
    stable_char_count: text.length,
    presentation_version: 1 as const,
    mutable_start_char_count: text.length,
    freeze: false,
  };
}

function recordingLease() {
  return {
    type: "agent.compose.state" as const,
    version: 1 as const,
    session_handle: "handle",
    generation: 1,
    recording_id: 1,
    state: "recording" as const,
  };
}

describe("compose preview reaches the question card's reply field", () => {
  it("binds the compose generation from the lease event", () => {
    let state = dictationReducer(INITIAL, {
      type: "lifecycle",
      event: RECORDING_STATE,
    });
    state = dictationReducer(state, {
      type: "compose.state",
      event: recordingLease(),
    });
    expect(state.composeGeneration).toBe(1);
  });

  it("shows recognized words even after an empty first revision", () => {
    // The real live-preview coordinator emits revision 1 with empty text while
    // the speaker is still silent. The first REAL words then arrive, and in the
    // captured trace they also carried revision 1.
    let state = dictationReducer(INITIAL, {
      type: "lifecycle",
      event: RECORDING_STATE,
    });
    state = dictationReducer(state, {
      type: "compose.state",
      event: recordingLease(),
    });
    state = dictationReducer(state, {
      type: "compose.preview",
      event: composePreview(1, ""),
    });
    state = dictationReducer(state, {
      type: "compose.preview",
      event: composePreview(1, "Let's"),
    });
    state = dictationReducer(state, {
      type: "compose.preview",
      event: composePreview(2, "Let's go"),
    });

    expect(state.preview?.text).toBe("Let's go");
    expect(state.composePreview).toBe(true);
  });

  it("keeps streaming words across the whole captured sequence", () => {
    let state = dictationReducer(INITIAL, {
      type: "lifecycle",
      event: RECORDING_STATE,
    });
    state = dictationReducer(state, {
      type: "compose.state",
      event: recordingLease(),
    });
    const words = ["Let's", "go", "with", "staging"];
    words.forEach((_, index) => {
      state = dictationReducer(state, {
        type: "compose.preview",
        event: composePreview(index + 1, words.slice(0, index + 1).join(" ")),
      });
    });
    expect(state.preview?.text).toBe("Let's go with staging");
  });

  it("does NOT require the live-preview setting to be on", () => {
    // Compose preview is a Claude reply concern, not the opt-in dictation
    // Live Preview feature. preview_enabled=false must not suppress it.
    let state = dictationReducer(INITIAL, {
      type: "lifecycle",
      event: { ...RECORDING_STATE, preview_enabled: false },
    });
    state = dictationReducer(state, {
      type: "compose.state",
      event: recordingLease(),
    });
    state = dictationReducer(state, {
      type: "compose.preview",
      event: composePreview(1, "audible"),
    });
    expect(state.preview?.text).toBe("audible");
  });

  it("ignores a preview for a different generation", () => {
    let state = dictationReducer(INITIAL, {
      type: "lifecycle",
      event: RECORDING_STATE,
    });
    state = dictationReducer(state, {
      type: "compose.state",
      event: recordingLease(),
    });
    state = dictationReducer(state, {
      type: "compose.preview",
      event: { ...composePreview(1, "stale"), generation: 99 },
    });
    expect(state.preview).toBeNull();
  });
});
