import { describe, expect, it } from "vitest";
import {
  hudSurfaceForMode,
  nextSurfaceFrame,
  surfaceAwaitingWindow,
  surfaceEntered,
  type HudSurfaceFrame,
  type HudSurfaceInputs,
} from "./surfaceState";

type Frame = HudSurfaceFrame<string> | null;

function run(steps: Array<Partial<HudSurfaceInputs<string>>>): Frame[] {
  let frame: Frame = null;
  return steps.map((step) => {
    frame = nextSurfaceFrame(frame, {
      desired: null,
      appliedMode: null,
      snapshot: null,
      timedLeave: null,
      ...step,
    });
    return frame;
  });
}

describe("hud surface state", () => {
  it("maps geometry modes onto window families", () => {
    expect(hudSurfaceForMode("panel")).toBe("card");
    expect(hudSurfaceForMode("summary")).toBe("card");
    expect(hudSurfaceForMode("response")).toBe("console");
    expect(hudSurfaceForMode("question")).toBe("question");
    expect(hudSurfaceForMode("permission")).toBe("permission");
    expect(hudSurfaceForMode("preview")).toBe("dictation");
    expect(hudSurfaceForMode("seed")).toBe("dictation");
  });

  it("appears from hidden, enters once the window holds it, and stays steady through prop changes", () => {
    const [mounted, shown, pressed] = run([
      { desired: "card", snapshot: "rest" },
      { desired: "card", appliedMode: "panel", snapshot: "rest" },
      { desired: "card", appliedMode: "summary", snapshot: "pressed" },
    ]);
    expect(mounted).toMatchObject({ surface: "card", phase: "appearing", entry: "hidden" });
    // From hidden the content paints at once: the window is not shown yet.
    expect(surfaceAwaitingWindow(mounted)).toBe(false);
    expect(surfaceEntered(mounted)).toBe(false);
    expect(shown).toMatchObject({ phase: "steady", snapshot: "rest" });
    expect(surfaceEntered(shown)).toBe(true);
    // Pill to glance is one window: no new entrance.
    expect(pressed).toMatchObject({ phase: "steady", snapshot: "pressed", entry: "hidden" });
  });

  it("keeps the last steady frame while leaving until the native hide lands", () => {
    const frames = run([
      { desired: "card", appliedMode: "panel", snapshot: "sent" },
      { desired: null, appliedMode: "panel", snapshot: null },
      { desired: null, appliedMode: "panel", snapshot: null },
      { desired: null, appliedMode: null, snapshot: null },
    ]);
    expect(frames[1]).toMatchObject({ surface: "card", phase: "leaving", snapshot: "sent" });
    expect(frames[2]).toMatchObject({ phase: "leaving", snapshot: "sent" });
    expect(frames[3]).toBeNull();
  });

  it("returns to steady with live content when shown again before the hide", () => {
    const frames = run([
      { desired: "card", appliedMode: "panel", snapshot: "a" },
      { desired: null, appliedMode: "panel" },
      { desired: "card", appliedMode: "panel", snapshot: "b" },
    ]);
    expect(frames[2]).toMatchObject({ phase: "steady", snapshot: "b" });
  });

  it("drops a surface that never reached the screen", () => {
    const frames = run([{ desired: "card", snapshot: "a" }, { desired: null }]);
    expect(frames[1]).toBeNull();
  });

  it("fades the console in place, then hides the incoming card until its window is placed", () => {
    const frames = run([
      { desired: "console", appliedMode: "response", snapshot: "console-1" },
      { desired: "console", appliedMode: "response", snapshot: "console-2", timedLeave: "console" },
      { desired: "console", appliedMode: "response", snapshot: "console-3", timedLeave: "console" },
      { desired: "card", appliedMode: "response", snapshot: "card-1" },
      { desired: "card", appliedMode: "summary", snapshot: "card-1" },
    ]);
    expect(frames[1]).toMatchObject({ surface: "console", phase: "leaving", timed: true, snapshot: "console-1" });
    expect(frames[2]).toMatchObject({ phase: "leaving", snapshot: "console-1" });
    expect(frames[3]).toMatchObject({ surface: "card", phase: "appearing", entry: "family" });
    expect(surfaceAwaitingWindow(frames[3])).toBe(true);
    expect(frames[4]).toMatchObject({ phase: "steady", entry: "family" });
    expect(surfaceAwaitingWindow(frames[4])).toBe(false);
  });

  it("marks a faded console with nothing after it so the window hides at once", () => {
    const frames = run([
      { desired: "console", appliedMode: "response", snapshot: "c" },
      { desired: "console", appliedMode: "response", timedLeave: "console" },
      { desired: null, appliedMode: "response" },
      { desired: null, appliedMode: null },
    ]);
    expect(frames[2]).toMatchObject({ surface: "console", phase: "leaving", faded: true });
    expect(frames[3]).toBeNull();
  });

  it("hands a card over to a dictation surface without painting either in the other's window", () => {
    const frames = run([
      { desired: "card", appliedMode: "summary", snapshot: "glance" },
      { desired: "dictation", appliedMode: "summary", snapshot: null },
      { desired: "dictation", appliedMode: "pill", snapshot: null },
    ]);
    expect(frames[1]).toMatchObject({ surface: "dictation", phase: "appearing", entry: "family" });
    expect(surfaceAwaitingWindow(frames[1])).toBe(true);
    expect(frames[2]).toMatchObject({ phase: "steady" });
  });

  it("is pure: evaluating twice from the same committed frame gives the same frame", () => {
    const committed: Frame = {
      surface: "card",
      phase: "steady",
      entry: "hidden",
      snapshot: "a",
      timed: false,
      faded: false,
    };
    const inputs = { desired: null, appliedMode: "panel" as const, snapshot: null, timedLeave: null };
    expect(nextSurfaceFrame(committed, inputs)).toEqual(nextSurfaceFrame(committed, inputs));
    expect(committed.phase).toBe("steady");
  });
});
