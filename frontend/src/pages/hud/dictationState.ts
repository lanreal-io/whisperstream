import {
  type DictationStateEvent,
  type TranscriptionPreviewEvent,
} from "../../lib/socket/dictationEvents";
import {
  type AgentComposeStateEvent,
  type AgentComposePreviewEvent,
} from "../../lib/socket/agentEvents";
import { type AppEvent } from "../../lib/socket/events";

export type DictationState = "idle" | "recording" | "processing";
type RecordingOrigin = "hud" | "push_to_talk" | "toggle" | null;

export interface DictationModel {
  authoritative: boolean;
  recordingId: number | null;
  origin: RecordingOrigin;
  phase: DictationState;
  hudEnabled: boolean;
  previewEnabled: boolean;
  screenReaderDetected: boolean;
  preview: {
    revision: number;
    text: string;
    stableCharCount: number;
    mutableStartCharCount: number;
    freeze: boolean;
  } | null;
  previewRevealed: boolean;
  composeGeneration: number | null;
  composePreview: boolean;
  /** The effective enhancement profile for THIS recording, or null when
   *  enhancement will not run (the intentional raw path publishes nothing).
   *  Held on the dictation model rather than beside it so it is cleared by the
   *  same lifecycle transitions that clear the preview: a payload can only ever
   *  belong to the recording the model is currently tracking. */
  profile: EffectiveProfile | null;
}

/** Content-free identity for one dictation's effective profile. `icon` is a
 *  bounded curated key present only for custom profiles; built-in glyphs are
 *  derived from the immutable id and never travel. */
export interface EffectiveProfile {
  profileId: string;
  name: string;
  icon?: string;
}

type DictationAction =
  | { type: "lifecycle"; event: DictationStateEvent }
  | { type: "preview"; event: TranscriptionPreviewEvent }
  | { type: "compose.state"; event: AgentComposeStateEvent }
  | { type: "compose.preview"; event: AgentComposePreviewEvent }
  | {
      type: "enhancement.profile";
      event: Extract<AppEvent, { type: "enhancement.profile" }>;
    }
  | { type: "legacy.recording"; recording: boolean; screenReaderDetected: boolean }
  | { type: "legacy.transcribing.done" }
  // The socket dropped. A restarted backend numbers recordings from 1 again,
  // so an id kept from the old one would reject every new event as stale.
  | { type: "reset" };

export const INITIAL_DICTATION_MODEL: DictationModel = {
  authoritative: false,
  recordingId: null,
  origin: null,
  phase: "idle",
  hudEnabled: true,
  previewEnabled: false,
  screenReaderDetected: false,
  preview: null,
  previewRevealed: false,
  composeGeneration: null,
  composePreview: false,
  profile: null,
};

const PHASE_ORDER: Record<DictationState, number> = {
  recording: 1,
  processing: 2,
  idle: 3,
};

export function dictationReducer(
  state: DictationModel,
  action: DictationAction,
): DictationModel {
  if (action.type === "reset") return INITIAL_DICTATION_MODEL;
  if (action.type === "lifecycle") {
    const event = action.event;
    const currentId = state.recordingId;
    if (currentId !== null && event.recording_id < currentId) return state;
    if (
      currentId === event.recording_id &&
      state.authoritative &&
      PHASE_ORDER[event.phase] < PHASE_ORDER[state.phase]
    ) {
      return state;
    }

    const newRecording = currentId !== event.recording_id;
    const origin: RecordingOrigin =
      event.origin === "hud" ||
      event.origin === "push_to_talk" ||
      event.origin === "toggle"
        ? event.origin
        : null;
    return {
      authoritative: true,
      recordingId: event.recording_id,
      origin,
      phase: event.phase,
      hudEnabled: event.hud_enabled,
      previewEnabled: event.preview_enabled,
      screenReaderDetected: event.screen_reader_detected,
      preview: event.phase === "idle" || newRecording ? null : state.preview,
      previewRevealed:
        event.phase === "idle" || newRecording ? false : state.previewRevealed,
      // A reply capture is named on the recording's own lifecycle events, so
      // the HUD never sees it as ordinary dictation for even one frame.
      composeGeneration:
        event.phase === "idle"
          ? null
          : event.compose_generation ??
            (newRecording ? null : state.composeGeneration),
      composePreview:
        event.phase === "idle" || newRecording
          ? false
          : state.composePreview,
      // Same clearing rule as the preview: idle ends the dictation the profile
      // described, and a new recording id starts one this payload never
      // belonged to. Nothing survives into a later dictation.
      profile:
        event.phase === "idle" || newRecording ? null : state.profile,
    };
  }

  if (action.type === "enhancement.profile") {
    const event = action.event;
    // Stale-recording rejection is load-bearing, not defensive: a payload that
    // outlived its dictation would stamp the NEXT one with the wrong identity.
    if (
      !state.authoritative ||
      state.recordingId === null ||
      event.recording_id !== state.recordingId ||
      state.phase === "idle"
    ) {
      return state;
    }
    return {
      ...state,
      profile: {
        profileId: event.profile_id,
        name: event.name,
        icon: event.icon,
      },
    };
  }

  if (action.type === "compose.state") {
    const event = action.event;
    if (event.state === "recording") {
      if (
        state.phase !== "recording" ||
        event.recording_id !== state.recordingId ||
        (state.composeGeneration !== null &&
          event.generation < state.composeGeneration)
      ) {
        return state;
      }
      return { ...state, composeGeneration: event.generation };
    }
    // The lease can end before the recording does (final, then idle). The
    // recording stays a reply until its own lifecycle reaches idle; a lease
    // never hands its recording to ordinary dictation.
    return state;
  }

  if (action.type === "preview" || action.type === "compose.preview") {
    const event = action.event;
    const composePreview = action.type === "compose.preview";
    if (
      !state.authoritative ||
      state.phase !== "recording" ||
      event.presentation_version !== 1 ||
      event.recording_id !== state.recordingId ||
      !state.hudEnabled ||
      (!composePreview && !state.previewEnabled) ||
      (action.type === "compose.preview" &&
        action.event.generation !== state.composeGeneration) ||
      state.screenReaderDetected ||
      (state.preview !== null && event.revision <= state.preview.revision)
    ) {
      return state;
    }
    const text = normalizePreviewText(event.text);
    const textLength = previewCodePointLength(text);
    const nextStableCharCount = Math.max(
      0,
      Math.min(
        Math.trunc(event.stable_char_count),
        textLength,
      ),
    );
    const nextMutableStartCharCount = Math.max(
      nextStableCharCount,
      Math.min(
        Math.trunc(event.mutable_start_char_count),
        textLength,
      ),
    );
    return {
      ...state,
      preview: {
        revision: event.revision,
        text,
        stableCharCount: nextStableCharCount,
        mutableStartCharCount: nextMutableStartCharCount,
        freeze: event.freeze,
      },
      previewRevealed: state.previewRevealed || text.length > 0,
      composePreview: composePreview || state.composePreview,
    };
  }

  // Once a versioned lifecycle has arrived, legacy stop/transcribing copies can
  // never roll it back or clear a newer correlated recording.
  if (state.authoritative) return state;
  if (action.type === "legacy.recording") {
    return {
      ...state,
      phase: action.recording ? "recording" : "processing",
      screenReaderDetected: action.recording
        ? action.screenReaderDetected
        : state.screenReaderDetected,
    };
  }
  if (action.type === "legacy.transcribing.done" && state.phase === "processing") {
    return { ...state, phase: "idle", screenReaderDetected: false };
  }
  return state;
}

export function normalizePreviewText(text: string): string {
  return text.trim().replace(/\s+/g, " ");
}

function previewCodePointLength(text: string): number {
  return Array.from(text).length;
}
