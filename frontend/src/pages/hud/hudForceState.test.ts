import {
  StrictMode,
  createElement,
  type PropsWithChildren,
} from "react";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AppEvent } from "../../lib/socket/events";
import {
  HUD_FORCE_EVENT,
  HUD_FORCE_KEY,
  HUD_FORCED_STATES,
  forcedStateById,
  hudForcedStateQaId,
  readForcedHudStateId,
  setForcedHudState,
  useForcedHudState,
} from "./hudForceState";

function StrictWrapper({ children }: PropsWithChildren) {
  return createElement(StrictMode, null, children);
}

describe("frozen HUD fixture contract", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
    window.localStorage.clear();
  });

  it("keeps fixture IDs unique and every catalogue row diagnosable", () => {
    const ids = HUD_FORCED_STATES.map((state) => state.id);

    expect(new Set(ids).size).toBe(ids.length);
    expect(HUD_FORCED_STATES.length).toBeGreaterThan(0);
    for (const state of HUD_FORCED_STATES) {
      expect(state.id).toMatch(/^[a-z0-9]+(?:_[a-z0-9]+)*$/);
      expect(state.group.trim()).not.toBe("");
      expect(state.label.trim()).not.toBe("");
      expect(state.note.trim()).not.toBe("");
    }
  });

  it("resolves only registered fixture IDs", () => {
    const fixture = HUD_FORCED_STATES[0];
    expect(forcedStateById(fixture.id)).toBe(fixture);
    expect(forcedStateById("not_registered")).toBeNull();
    expect(forcedStateById(null)).toBeNull();
  });

  it("keeps written summaries opt-in in frozen response queues", () => {
    for (const state of HUD_FORCED_STATES) {
      if (state.completionQueue !== undefined) {
        expect(typeof state.completionQueue.showSummaryText).toBe("boolean");
      }
    }
  });

  it("configures deterministic written summary fixtures correctly", () => {
    const textFixture = forcedStateById("agent_summary_text");
    expect(textFixture).not.toBeNull();
    expect(hudForcedStateQaId("agent_summary_text")).toBe(
      "hud.agent-summary-text",
    );
    expect(textFixture?.completionQueue?.showSummaryText).toBe(true);
    expect(textFixture?.completionQueue?.active?.passive_total_seconds).toBe(20);
    expect(textFixture?.completionQueue?.active?.passive_remaining_seconds).toBe(
      20,
    );
    expect(textFixture?.replyDraft).toBeUndefined();
    const textItem = textFixture?.completionQueue?.queue[0];
    expect(textItem?.kind === "response" && textItem.spoken_summary).toBeTruthy();

    const replyFixture = forcedStateById("agent_summary_reply");
    expect(replyFixture).not.toBeNull();
    expect(hudForcedStateQaId("agent_summary_reply")).toBe(
      "hud.agent-summary-reply",
    );
    expect(replyFixture?.completionQueue?.showSummaryText).toBe(true);
    expect(replyFixture?.replyDraft?.phase).toBe("recording");
    expect(replyFixture?.replyPreview).toBeTruthy();
    const replyItem = replyFixture?.completionQueue?.queue[0];
    expect(
      replyItem?.kind === "response" && replyItem.spoken_summary,
    ).toBeTruthy();
  });

  it("writes, reads, and clears the shared selection with same-window notification", () => {
    const onChange = vi.fn();
    window.addEventListener(HUD_FORCE_EVENT, onChange);

    setForcedHudState(HUD_FORCED_STATES[0].id);
    expect(window.localStorage.getItem(HUD_FORCE_KEY)).toBe(
      HUD_FORCED_STATES[0].id,
    );
    expect(readForcedHudStateId()).toBe(HUD_FORCED_STATES[0].id);

    setForcedHudState(null);
    expect(window.localStorage.getItem(HUD_FORCE_KEY)).toBeNull();
    expect(readForcedHudStateId()).toBeNull();
    expect(onChange).toHaveBeenCalledTimes(2);

    window.removeEventListener(HUD_FORCE_EVENT, onChange);
  });

  it("synchronizes hook consumers for same-window and cross-window changes", () => {
    const { result } = renderHook(() => useForcedHudState());
    const first = HUD_FORCED_STATES[0];
    const second = HUD_FORCED_STATES[1];

    expect(result.current).toBeNull();

    act(() => setForcedHudState(first.id));
    expect(result.current).toBe(first);

    act(() => {
      window.localStorage.setItem(HUD_FORCE_KEY, second.id);
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: HUD_FORCE_KEY,
          newValue: second.id,
          storageArea: window.localStorage,
        }),
      );
    });
    expect(result.current).toBe(second);

    act(() => setForcedHudState(null));
    expect(result.current).toBeNull();
  });

  it("survives React StrictMode effect replay without losing the selection", () => {
    window.localStorage.setItem(HUD_FORCE_KEY, HUD_FORCED_STATES[0].id);

    const { result } = renderHook(() => useForcedHudState(), {
      wrapper: StrictWrapper,
    });
    expect(result.current).toBe(HUD_FORCED_STATES[0]);

    act(() => setForcedHudState(HUD_FORCED_STATES[1].id));
    expect(result.current).toBe(HUD_FORCED_STATES[1]);
  });

  it("does not read or mutate the fixture channel outside development", () => {
    window.localStorage.setItem(HUD_FORCE_KEY, HUD_FORCED_STATES[0].id);
    vi.stubEnv("DEV", false);

    expect(readForcedHudStateId()).toBeNull();
    setForcedHudState(HUD_FORCED_STATES[1].id);
    expect(window.localStorage.getItem(HUD_FORCE_KEY)).toBe(
      HUD_FORCED_STATES[0].id,
    );

    const { result } = renderHook(() => useForcedHudState());
    expect(result.current).toBeNull();
  });
});

describe("dictation fixture contract (transparent-indicators Task 2.3)", () => {
  const byId = (id: string) => {
    const fixture = HUD_FORCED_STATES.find((state) => state.id === id);
    expect(fixture, `missing fixture ${id}`).toBeDefined();
    return fixture!;
  };

  const lifecycle = (fixture: (typeof HUD_FORCED_STATES)[number]) =>
    (fixture.dictationEvents ?? []).filter(
      (event) => event.type === "dictation.state",
    );

  it("registers the stamped recording, processing, and letter-fallback states", () => {
    const recording = byId("dictation_recording_builtin");
    const processing = byId("dictation_processing_builtin");
    const letter = byId("dictation_recording_letter");

    for (const fixture of [recording, processing, letter]) {
      expect(fixture.group).toBe("Dictation");
      expect(fixture.dictationEvents?.length).toBeGreaterThan(0);
      // A stamp is presentation for an ACTIVE dictation only: without a
      // lifecycle event the production reducer never marks the model
      // authoritative and the stamp cannot paint.
      expect(lifecycle(fixture).length).toBeGreaterThan(0);
    }

    const phaseOf = (fixture: (typeof HUD_FORCED_STATES)[number]) => {
      const events = lifecycle(fixture);
      const last = events[events.length - 1];
      return last.type === "dictation.state" ? last.phase : null;
    };
    expect(phaseOf(recording)).toBe("recording");
    expect(phaseOf(processing)).toBe("processing");
    expect(phaseOf(letter)).toBe("recording");

    // Recording and processing are the SAME dictation, so a reviewer comparing
    // the two screenshots is looking at one vessel that did not resize.
    expect(lifecycle(recording)[0]).toEqual(lifecycle(processing)[0]);
  });

  it("stamps a built-in glyph and a letter fallback from real profile payloads", () => {
    const profileEvent = (id: string) => {
      const event = (byId(id).dictationEvents ?? []).find(
        (candidate) => candidate.type === "enhancement.profile",
      );
      expect(event, `fixture ${id} publishes no effective profile`).toBeDefined();
      return event as Extract<AppEvent, { type: "enhancement.profile" }>;
    };

    // Built-in: the glyph derives from the immutable id, so no icon key
    // travels with it.
    const builtin = profileEvent("dictation_recording_builtin");
    expect(builtin.profile_id).toBe("email");
    expect(builtin.icon).toBeUndefined();
    expect(profileEvent("dictation_processing_builtin").profile_id).toBe("email");

    // Letter fallback: a custom profile with no curated key at all.
    const fallback = profileEvent("dictation_recording_letter");
    expect(["chat", "code", "email"]).not.toContain(fallback.profile_id);
    expect(fallback.icon).toBeUndefined();
    expect(fallback.name.trim()).not.toBe("");
  });

  it("correlates every profile payload with its own dictation", () => {
    for (const fixture of HUD_FORCED_STATES) {
      const recordingIds = new Set(
        (fixture.dictationEvents ?? [])
          .filter((event) => event.type === "dictation.state")
          .map((event) =>
            event.type === "dictation.state" ? event.recording_id : -1,
          ),
      );
      for (const event of fixture.dictationEvents ?? []) {
        if (event.type !== "enhancement.profile") continue;
        // A payload that does not correlate would be a stale-event bypass,
        // which is exactly what the production reducer rejects.
        expect(recordingIds).toContain(event.recording_id);
      }
    }
  });

  it("registers the two widest timeout labels without a dictation", () => {
    const local = byId("enhance_timeout_local");
    const anthropic = byId("enhance_timeout_anthropic");

    expect(local.timeoutProvider).toBe("local");
    expect(anthropic.timeoutProvider).toBe("anthropic");
    for (const fixture of [local, anthropic]) {
      expect(fixture.group).toBe("Enhancement");
      // The notice shows after the raw paste, so the dictation is over.
      expect(fixture.dictationEvents).toBeUndefined();
    }

    const providers = HUD_FORCED_STATES.map(
      (fixture) => fixture.timeoutProvider,
    ).filter((provider) => provider !== undefined);
    expect(new Set(providers).size).toBe(providers.length);
    for (const provider of providers) {
      expect([
        "gemini",
        "openai",
        "anthropic",
        "ollama",
        "local",
        "custom",
      ]).toContain(provider);
    }
  });

  it("keeps every new fixture payload serializable", () => {
    const payloads = HUD_FORCED_STATES.map((fixture) => ({
      dictationEvents: fixture.dictationEvents ?? null,
      timeoutProvider: fixture.timeoutProvider ?? null,
    }));

    expect(JSON.parse(JSON.stringify(payloads))).toEqual(payloads);
  });
});
