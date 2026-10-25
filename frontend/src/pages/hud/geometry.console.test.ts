import { beforeEach, describe, expect, it, vi } from "vitest";

const monitor = vi.hoisted(() => ({
  current: {
    name: "\\\\.\\DISPLAY1",
    scaleFactor: 1,
    position: { x: 0, y: 0 },
    size: { width: 1920, height: 1080 },
    workArea: {
      position: { x: 0, y: 0 },
      size: { width: 1920, height: 1040 },
    },
  } as Record<string, unknown> | null,
}));

vi.mock("@tauri-apps/api/window", () => ({
  currentMonitor: vi.fn(async () => monitor.current),
}));

import {
  CONSOLE_DEFAULT_H,
  CONSOLE_DEFAULT_W,
  CONSOLE_MIN_H,
  CONSOLE_MIN_W,
  CONSOLE_PAD_X,
  CONSOLE_PAD_Y,
  CONSOLE_WORK_AREA_MARGIN,
  clampConsoleSize,
  consoleDisplayKey,
  consoleWindowRect,
  draggedConsolePlacement,
  readConsoleOffset,
  readConsoleSize,
  resizedConsoleSize,
  resolveHudGeometry,
  saveConsoleOffset,
  saveConsoleSize,
  SNAP_THRESHOLD,
} from "./geometry";

const FULL_HD = { x: 0, y: 0, width: 1920, height: 1040 };
const WINDOW_W = CONSOLE_DEFAULT_W + CONSOLE_PAD_X * 2;
const WINDOW_H = CONSOLE_DEFAULT_H + CONSOLE_PAD_Y * 2;

function primaryMonitor() {
  return {
    name: "\\\\.\\DISPLAY1",
    scaleFactor: 1,
    position: { x: 0, y: 0 },
    size: { width: 1920, height: 1080 },
    workArea: {
      position: { x: 0, y: 0 },
      size: { width: 1920, height: 1040 },
    },
  };
}

function secondMonitor() {
  return {
    name: "\\\\.\\DISPLAY2",
    scaleFactor: 1.5,
    position: { x: -2560, y: -200 },
    size: { width: 3840, height: 2160 },
    workArea: {
      position: { x: -2560, y: -200 },
      size: { width: 3840, height: 2100 },
    },
  };
}

describe("centered console geometry", () => {
  beforeEach(() => {
    localStorage.clear();
    monitor.current = primaryMonitor();
  });

  it("opens at 860x630 by default, centered in the work area", async () => {
    const geometry = await resolveHudGeometry("response", false, 0);

    expect(geometry.size.width).toBe(WINDOW_W);
    expect(geometry.size.height).toBe(WINDOW_H);
    expect(geometry.position?.x).toBe((1920 - WINDOW_W) / 2);
    expect(geometry.position?.y).toBe((1040 - WINDOW_H) / 2);
  });

  it("never sizes the console below 640x420", () => {
    expect(clampConsoleSize({ width: 200, height: 100 }, FULL_HD)).toEqual({
      width: CONSOLE_MIN_W,
      height: CONSOLE_MIN_H,
    });
    expect(
      clampConsoleSize({ width: Number.NaN, height: -5 }, FULL_HD),
    ).toEqual({ width: CONSOLE_MIN_W, height: CONSOLE_MIN_H });
  });

  it("keeps the console inside the work area, which wins over the minimum", () => {
    expect(clampConsoleSize({ width: 5000, height: 5000 }, FULL_HD)).toEqual({
      width: 1920 - CONSOLE_WORK_AREA_MARGIN * 2 - CONSOLE_PAD_X * 2,
      height: 1040 - CONSOLE_WORK_AREA_MARGIN * 2 - CONSOLE_PAD_Y * 2,
    });
    const tiny = { x: 0, y: 0, width: 600, height: 400 };
    const clamped = clampConsoleSize({ width: 860, height: 630 }, tiny);
    expect(clamped.width).toBe(600 - CONSOLE_WORK_AREA_MARGIN * 2 - CONSOLE_PAD_X * 2);
    expect(clamped.height).toBe(400 - CONSOLE_WORK_AREA_MARGIN * 2 - CONSOLE_PAD_Y * 2);
  });

  it("centers on an offset, scaled display", async () => {
    monitor.current = secondMonitor();
    const geometry = await resolveHudGeometry("response", false, 0);
    // 3840x2100 physical at 1.5x is a 2560x1400 logical work area at
    // (-1706.67, -133.33).
    const workX = -2560 / 1.5;
    const workY = -200 / 1.5;
    expect(geometry.position?.x).toBe(Math.round(workX + (2560 - WINDOW_W) / 2));
    expect(geometry.position?.y).toBe(Math.round(workY + (1400 - WINDOW_H) / 2));
  });

  it("is centered for any size, so the window rect is a pure function of size", () => {
    const rect = consoleWindowRect({ width: 1000, height: 700 }, FULL_HD);
    expect(rect.card).toEqual({ width: 1000, height: 700 });
    expect(rect.x + rect.width / 2).toBe(960);
    expect(rect.y + rect.height / 2).toBe(520);
  });

  it("remembers the size per display", async () => {
    const primaryKey = consoleDisplayKey(primaryMonitor());
    saveConsoleSize(primaryKey, { width: 1100, height: 720 });

    const onPrimary = await resolveHudGeometry("response", false, 0);
    expect(onPrimary.size.width).toBe(1100 + CONSOLE_PAD_X * 2);
    expect(onPrimary.size.height).toBe(720 + CONSOLE_PAD_Y * 2);

    // The other display has nothing saved: it opens at the default.
    monitor.current = secondMonitor();
    const onSecond = await resolveHudGeometry("response", false, 0);
    expect(onSecond.size.width).toBe(WINDOW_W);
    expect(onSecond.size.height).toBe(WINDOW_H);

    const secondKey = consoleDisplayKey(secondMonitor());
    saveConsoleSize(secondKey, { width: 900, height: 500 });
    expect(readConsoleSize(primaryKey)).toEqual({ width: 1100, height: 720 });
    expect(readConsoleSize(secondKey)).toEqual({ width: 900, height: 500 });
  });

  it("applies a live resize request only on the display it was made on", async () => {
    const key = consoleDisplayKey(primaryMonitor());
    const request = { displayKey: key, width: 1200, height: 800 };
    const sized = await resolveHudGeometry("response", false, 0, null, request);
    expect(sized.size.width).toBe(1200 + CONSOLE_PAD_X * 2);
    expect(sized.position?.x).toBe((1920 - (1200 + CONSOLE_PAD_X * 2)) / 2);

    monitor.current = secondMonitor();
    const elsewhere = await resolveHudGeometry("response", false, 0, null, request);
    expect(elsewhere.size.width).toBe(WINDOW_W);
  });

  it("opens where it was dragged on this display, and resizes around that center", async () => {
    const key = consoleDisplayKey(primaryMonitor());
    saveConsoleOffset(key, { dx: -300, dy: 100 });
    const moved = await resolveHudGeometry("response", false, 0);
    expect(moved.position?.x).toBe((1920 - WINDOW_W) / 2 - 300);
    expect(moved.position?.y).toBe(Math.round((1040 - WINDOW_H) / 2 + 100));
    // A resize keeps the dragged center.
    const resized = consoleWindowRect({ width: 1000, height: 700 }, FULL_HD, { dx: -300, dy: 100 });
    expect(resized.x + resized.width / 2).toBe(960 - 300);
    expect(resized.y + resized.height / 2).toBe(520 + 100);
    // Another display keeps its own placement: centered.
    monitor.current = secondMonitor();
    expect(readConsoleOffset(consoleDisplayKey(secondMonitor()))).toBeNull();
  });

  it("snaps a dragged console to center on each axis and keeps it on screen", () => {
    const size = { width: WINDOW_W, height: WINDOW_H };
    const centeredX = (1920 - WINDOW_W) / 2;
    const centeredY = (1040 - WINDOW_H) / 2;
    // Near center on x only: x snaps, y stays where it was dragged.
    const nearX = draggedConsolePlacement(
      { x: centeredX + SNAP_THRESHOLD - 1, y: centeredY - 120 },
      size,
      FULL_HD,
    );
    expect(nearX.x).toBe(centeredX);
    expect(nearX.offset).toEqual({ dx: 0, dy: -120 });
    // Near center on both axes: back to exactly centered.
    expect(
      draggedConsolePlacement({ x: centeredX + 10, y: centeredY - 10 }, size, FULL_HD).offset,
    ).toEqual({ dx: 0, dy: 0 });
    // Dragged past the edge: held inside the work area.
    const past = draggedConsolePlacement({ x: -400, y: 900 }, size, FULL_HD);
    expect(past.x).toBe(0);
    expect(past.y).toBe(1040 - WINDOW_H);
    // A stored offset that no longer fits (a smaller display) is clamped too.
    const clamped = consoleWindowRect(
      { width: CONSOLE_DEFAULT_W, height: CONSOLE_DEFAULT_H },
      FULL_HD,
      { dx: 5000, dy: 0 },
    );
    expect(clamped.x + clamped.width).toBe(1920);
  });

  it("tells same-named displays apart by their rectangle", () => {
    const left = consoleDisplayKey({
      name: "Generic Monitor",
      position: { x: 0, y: 0 },
      size: { width: 1920, height: 1080 },
    });
    const right = consoleDisplayKey({
      name: "Generic Monitor",
      position: { x: 1920, y: 0 },
      size: { width: 1920, height: 1080 },
    });
    expect(left).not.toBe(right);
  });

  it("ignores unreadable stored sizes", () => {
    localStorage.setItem("hud-console-size", "{not json");
    expect(readConsoleSize("any")).toBeNull();
    localStorage.setItem(
      "hud-console-size",
      JSON.stringify({ bad: { width: "wide" }, good: { width: 700, height: 500 } }),
    );
    expect(readConsoleSize("bad")).toBeNull();
    expect(readConsoleSize("good")).toEqual({ width: 700, height: 500 });
  });

  it("falls back to the default window without a monitor", async () => {
    monitor.current = null;
    const geometry = await resolveHudGeometry("response", false, 0);
    expect(geometry.size.width).toBe(WINDOW_W);
    expect(geometry.size.height).toBe(WINDOW_H);
    expect(geometry.position).toBeNull();
  });

  it("moves both edges of an axis together so the console stays centered", () => {
    const start = { width: 860, height: 630 };
    expect(resizedConsoleSize(start, "e", 20, 99)).toEqual({ width: 900, height: 630 });
    expect(resizedConsoleSize(start, "w", -20, 99)).toEqual({ width: 900, height: 630 });
    expect(resizedConsoleSize(start, "s", 99, 15)).toEqual({ width: 860, height: 660 });
    expect(resizedConsoleSize(start, "n", 99, -15)).toEqual({ width: 860, height: 660 });
    expect(resizedConsoleSize(start, "se", 10, 10)).toEqual({ width: 880, height: 650 });
    expect(resizedConsoleSize(start, "nw", 10, 10)).toEqual({ width: 840, height: 610 });
    expect(resizedConsoleSize(start, "ne", 10, 10)).toEqual({ width: 880, height: 610 });
    expect(resizedConsoleSize(start, "sw", 10, 10)).toEqual({ width: 840, height: 650 });
  });
});
