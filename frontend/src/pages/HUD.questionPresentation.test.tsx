// @vitest-environment node

/**
 * Questions arrive as the compact Claude identity pill and expand only for the
 * exact generation selected by the contextual view command. Routed capture no
 * longer expands anything: the live reply text rides the compact pill's reply
 * rail, exactly as a response's does (see HUD.test.tsx, "answers a question in
 * the compact pill instead of expanding the card").
 */

import { describe, expect, it } from "vitest";

import { derivePresentation } from "./hud/presentation";

const IDLE_DICTATION = {
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

function question(phase: string, generation = 1) {
  return {
    session_handle: "handle",
    generation,
    phase,
    questions: [
      {
        question: "Which environment should I deploy to?",
        header: "Deploy",
        multiSelect: false,
        options: [{ label: "Staging" }, { label: "Production" }],
      },
    ],
    current_index: 0,
    remaining_seconds: 6,
    compose_generation: null,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any;
}

function presentationFor(phase: string, expanded = false) {
  return derivePresentation({
    dictation: IDLE_DICTATION,
    transient: null,
    claudeAttention: null,
    activeQuestion: question(phase),
    questionExpanded: expanded,
    hasCompletion: false,
    persistentControlVisible: false,
    persistentControlExpanded: false,
    idleScreenReaderDetected: false,
    startCommandPending: false,
    finishVisualHold: false,
  });
}

describe("question presentation", () => {
  it("surfaces a new question as the compact Claude identity pill", () => {
    expect(presentationFor("surfaced", false)).toEqual({
      mode: "question_pill",
    });
  });

  it.each(["grace", "speaking", "recording", "draft_ready"])(
    "keeps an unexpanded answerable question compact in phase %s",
    (phase) => {
      expect(presentationFor(phase, false)).toEqual({
        mode: "question_pill",
      });
    },
  );

  it("renders the full card only for the expanded generation", () => {
    expect(presentationFor("grace", true)).toEqual({ mode: "question" });
  });

  it("keeps terminal question outcomes compact until their dwell ends", () => {
    expect(presentationFor("expired")).toEqual({ mode: "question_pill" });
    expect(presentationFor("held_failed")).toEqual({ mode: "question_pill" });
  });
});
