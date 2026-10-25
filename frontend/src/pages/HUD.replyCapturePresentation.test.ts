// @vitest-environment node

/**
 * A reply capture keeps the reply surface on screen for its whole recording.
 * The backend publishes the recording's lifecycle and its compose lease as
 * separate messages; this replays them in the backend's real order and
 * checks the presentation after every message, so no intermediate frame can
 * fall back to the ordinary dictation pill (owner report 2026-10-24: the
 * pill flashed and resized on push-to-talk press and on release).
 */

import { describe, expect, it } from "vitest";

import {
  dictationReducer,
  INITIAL_DICTATION_MODEL,
  type DictationModel,
} from "./hud/dictationState";
import { derivePresentation } from "./hud/presentation";
import type { DictationStateEvent } from "../lib/socket/dictationEvents";

function lifecycle(
  phase: DictationStateEvent["phase"],
  extra: Partial<DictationStateEvent> = {},
) {
  return {
    type: "lifecycle" as const,
    event: {
      type: "dictation.state" as const,
      version: 1 as const,
      recording_id: 1,
      phase,
      origin: "push_to_talk" as const,
      hud_enabled: true,
      preview_enabled: false,
      screen_reader_detected: false,
      ...extra,
    },
  };
}

function compose(state: "recording" | "idle" | "cancelled") {
  return {
    type: "compose.state" as const,
    event: {
      type: "agent.compose.state" as const,
      version: 1 as const,
      session_handle: "handle",
      generation: 7,
      recording_id: 1,
      state,
    },
  };
}

function presentationFor(dictation: DictationModel, previewEnabled = false) {
  return derivePresentation({
    dictation: { ...dictation, previewEnabled },
    transient: null,
    claudeAttention: null,
    activeQuestion: null,
    questionExpanded: false,
    hasCompletion: true,
    persistentControlVisible: false,
    persistentControlExpanded: false,
    idleScreenReaderDetected: false,
    startCommandPending: false,
    finishVisualHold: false,
  });
}

describe("reply capture presentation", () => {
  it.each([false, true])(
    "keeps the reply panel through every message of one capture (Live Preview %s)",
    (previewEnabled) => {
      // Backend order: recording state (naming the lease), lease recording,
      // processing state, lease idle after the final, then idle state.
      const sequence = [
        lifecycle("recording", { compose_generation: 7, preview_enabled: previewEnabled }),
        compose("recording"),
        lifecycle("processing", { compose_generation: 7, preview_enabled: previewEnabled }),
        compose("idle"),
        lifecycle("idle", { compose_generation: 7, preview_enabled: previewEnabled }),
      ];
      let state = INITIAL_DICTATION_MODEL;
      for (const action of sequence) {
        state = dictationReducer(state, action);
        expect(presentationFor(state, previewEnabled)).toEqual({ mode: "panel" });
      }
      expect(state.composeGeneration).toBeNull();
    },
  );

  it("still shows ordinary dictation as the recording pill", () => {
    const state = dictationReducer(INITIAL_DICTATION_MODEL, lifecycle("recording"));
    expect(presentationFor(state)).toEqual({ mode: "pill", state: "recording" });
  });

  it("does not carry a reply capture into the next ordinary recording", () => {
    let state = dictationReducer(
      INITIAL_DICTATION_MODEL,
      lifecycle("recording", { compose_generation: 7 }),
    );
    state = dictationReducer(state, lifecycle("recording", { recording_id: 2 }));
    expect(state.composeGeneration).toBeNull();
    expect(presentationFor(state)).toEqual({ mode: "pill", state: "recording" });
  });
});

describe("backend restart", () => {
  it("accepts a restarted backend's recording ids once the socket drops", () => {
    let state = dictationReducer(
      INITIAL_DICTATION_MODEL,
      lifecycle("idle", { recording_id: 9 }),
    );
    // Without the reset, the new backend's recording 1 reads as stale.
    expect(dictationReducer(state, lifecycle("recording")).phase).toBe("idle");

    state = dictationReducer(state, { type: "reset" });
    state = dictationReducer(state, lifecycle("recording", { compose_generation: 7 }));
    expect(state.phase).toBe("recording");
    expect(state.recordingId).toBe(1);
    expect(state.composeGeneration).toBe(7);
  });
});
