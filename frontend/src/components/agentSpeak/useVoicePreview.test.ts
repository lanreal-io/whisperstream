/**
 * Tests for the single-player voice preview hook (design.md's "Play/pause
 * state machine" table). One module-scoped HTMLAudioElement backs every
 * hook instance, so each test resets modules and stubs a fresh global
 * `Audio` constructor to get an isolated element and to prove only one is
 * ever built per module lifetime.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { useVoicePreview as UseVoicePreviewType } from "./useVoicePreview";

type Listener = () => void;

class MockAudioElement {
  src = "";
  playbackRate = 1;
  volume = 1;
  currentTime = 0;
  paused = true;
  play = vi.fn(() => {
    this.paused = false;
    return Promise.resolve();
  });
  pause = vi.fn(() => {
    this.paused = true;
  });
  private listeners: Record<string, Listener[]> = {};
  addEventListener(type: string, cb: Listener) {
    (this.listeners[type] ??= []).push(cb);
  }
  removeEventListener(type: string, cb: Listener) {
    this.listeners[type] = (this.listeners[type] ?? []).filter((l) => l !== cb);
  }
  dispatchEvent(type: string) {
    for (const cb of [...(this.listeners[type] ?? [])]) cb();
    return true;
  }
}

let constructedCount = 0;
let instances: MockAudioElement[] = [];

async function loadHook(): Promise<typeof UseVoicePreviewType> {
  vi.resetModules();
  constructedCount = 0;
  instances = [];
  vi.stubGlobal(
    "Audio",
    vi.fn(() => {
      constructedCount += 1;
      const el = new MockAudioElement();
      instances.push(el);
      return el;
    }),
  );
  const mod = await import("./useVoicePreview");
  return mod.useVoicePreview;
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useVoicePreview", () => {
  it("plays a row from idle: sets src, applies speed/volume, and sets playingKey", async () => {
    const useVoicePreview = await loadHook();
    const { result } = renderHook(() => useVoicePreview(1.25, 0.6));

    await act(async () => {
      result.current.toggle("kokoro", "af_heart");
    });

    expect(instances).toHaveLength(1);
    const audio = instances[0];
    expect(audio.src).toBe("/voice-samples/kokoro-af_heart.ogg");
    expect(audio.play).toHaveBeenCalledTimes(1);
    expect(audio.playbackRate).toBe(1.25);
    expect(audio.volume).toBe(0.6);
    expect(result.current.playingKey).toBe("kokoro/af_heart");
    expect(result.current.error).toBe(false);
  });

  it("only ever constructs one HTMLAudioElement across repeated toggles", async () => {
    const useVoicePreview = await loadHook();
    const { result } = renderHook(() => useVoicePreview(1, 1));

    await act(async () => {
      result.current.toggle("kokoro", "af_heart");
    });
    await act(async () => {
      result.current.toggle("supertonic", 0);
    });
    await act(async () => {
      result.current.toggle("supertonic", 0);
    });

    expect(constructedCount).toBe(1);
  });

  it("playing a second row pauses and resets the first, then plays only the new one", async () => {
    const useVoicePreview = await loadHook();
    const { result } = renderHook(() => useVoicePreview(1, 1));

    await act(async () => {
      result.current.toggle("kokoro", "af_heart");
    });
    const audio = instances[0];
    audio.currentTime = 42;

    await act(async () => {
      result.current.toggle("supertonic", 3);
    });

    expect(audio.pause).toHaveBeenCalled();
    expect(audio.src).toBe("/voice-samples/supertonic-3.ogg");
    expect(audio.play).toHaveBeenCalledTimes(2);
    expect(result.current.playingKey).toBe("supertonic/3");
  });

  it("pausing the currently playing row keeps its position and clears playingKey", async () => {
    const useVoicePreview = await loadHook();
    const { result } = renderHook(() => useVoicePreview(1, 1));

    await act(async () => {
      result.current.toggle("kokoro", "af_heart");
    });
    const audio = instances[0];
    audio.currentTime = 7.5;

    await act(async () => {
      result.current.toggle("kokoro", "af_heart");
    });

    expect(audio.pause).toHaveBeenCalledTimes(1);
    expect(audio.currentTime).toBe(7.5);
    expect(result.current.playingKey).toBeNull();
  });

  it("resuming a paused row plays from the kept position", async () => {
    const useVoicePreview = await loadHook();
    const { result } = renderHook(() => useVoicePreview(1, 1));

    await act(async () => {
      result.current.toggle("kokoro", "af_heart");
    });
    const audio = instances[0];
    audio.currentTime = 7.5;
    await act(async () => {
      result.current.toggle("kokoro", "af_heart"); // pause
    });

    await act(async () => {
      result.current.toggle("kokoro", "af_heart"); // resume
    });

    expect(audio.currentTime).toBe(7.5);
    expect(result.current.playingKey).toBe("kokoro/af_heart");
  });

  it("`ended` clears playingKey and resets currentTime, returning control to play", async () => {
    const useVoicePreview = await loadHook();
    const { result } = renderHook(() => useVoicePreview(1, 1));

    await act(async () => {
      result.current.toggle("kokoro", "af_heart");
    });
    const audio = instances[0];
    audio.currentTime = 9;

    await act(async () => {
      audio.dispatchEvent("ended");
    });

    expect(result.current.playingKey).toBeNull();
    expect(audio.currentTime).toBe(0);
  });

  it("an `error` event clears playingKey, sets error, and never throws", async () => {
    const useVoicePreview = await loadHook();
    const { result } = renderHook(() => useVoicePreview(1, 1));

    await act(async () => {
      result.current.toggle("kokoro", "af_heart");
    });
    const audio = instances[0];

    expect(() => {
      act(() => {
        audio.dispatchEvent("error");
      });
    }).not.toThrow();

    expect(result.current.playingKey).toBeNull();
    expect(result.current.error).toBe(true);
  });

  it("a rejected play() promise clears playingKey and sets error without throwing", async () => {
    const useVoicePreview = await loadHook();
    const { result } = renderHook(() => useVoicePreview(1, 1));

    await act(async () => {
      result.current.toggle("kokoro", "af_heart");
    });
    const audio = instances[0];
    audio.play = vi.fn(() => Promise.reject(new Error("NotAllowedError")));

    await act(async () => {
      result.current.toggle("supertonic", 1);
    });

    expect(result.current.playingKey).toBeNull();
    expect(result.current.error).toBe(true);
  });

  it("live-writes playbackRate and volume on every speed/volume change, clamping rate to 0.5-2.0", async () => {
    const useVoicePreview = await loadHook();
    const { result, rerender } = renderHook(
      ({ speed, volume }) => useVoicePreview(speed, volume),
      { initialProps: { speed: 1, volume: 1 } },
    );

    await act(async () => {
      result.current.toggle("kokoro", "af_heart");
    });
    const audio = instances[0];

    rerender({ speed: 3, volume: 0.2 });
    expect(audio.playbackRate).toBe(2.0);
    expect(audio.volume).toBe(0.2);

    rerender({ speed: 0.1, volume: 0.9 });
    expect(audio.playbackRate).toBe(0.5);
    expect(audio.volume).toBe(0.9);
  });

  it("unmount tears everything down: pause + reset", async () => {
    const useVoicePreview = await loadHook();
    const { result, unmount } = renderHook(() => useVoicePreview(1, 1));

    await act(async () => {
      result.current.toggle("kokoro", "af_heart");
    });
    const audio = instances[0];
    audio.currentTime = 12;

    unmount();

    expect(audio.pause).toHaveBeenCalled();
    expect(audio.currentTime).toBe(0);
  });

  it("stopAll pauses, resets position, and clears playingKey", async () => {
    const useVoicePreview = await loadHook();
    const { result } = renderHook(() => useVoicePreview(1, 1));

    await act(async () => {
      result.current.toggle("kokoro", "af_heart");
    });
    const audio = instances[0];
    audio.currentTime = 5;

    await act(async () => {
      result.current.stopAll();
    });

    expect(audio.pause).toHaveBeenCalled();
    expect(audio.currentTime).toBe(0);
    expect(result.current.playingKey).toBeNull();
  });
});

// Synthetic available clips exercise playback independently of distribution assets.
vi.mock("./voiceSamples", () => ({ hasVoiceSample: () => true }));
