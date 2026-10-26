import { describe, expect, it } from "vitest";
import {
  formatShortcut,
  shortcutDisplayParts,
  shortcutPlatform,
} from "./shortcutDisplay";

describe("shortcutPlatform", () => {
  it("detects Mac WebKit platform values without changing the Windows default", () => {
    expect(shortcutPlatform("MacIntel")).toBe("macos");
    expect(shortcutPlatform("Win32")).toBe("windows");
    expect(shortcutPlatform("Linux x86_64")).toBe("windows");
  });
});

describe("Mac shortcut notation", () => {
  it.each([
    ["shift", "⇧"],
    ["right shift", "Right ⇧"],
    ["ctrl+left option+k", "⌃ Left ⌥ K"],
    ["cmd+option+shift+k", "⌥⇧⌘K"],
    ["ctrl+cmd+space", "⌃⌘Space"],
    ["win+alt+left", "⌥⌘←"],
    ["caps lock", "⇪"],
  ])("formats %s as canonical glyphs", (shortcut, expected) => {
    expect(formatShortcut(shortcut, "macos")).toBe(expected);
  });

  it("returns individual glyph pills while capture is active", () => {
    expect(shortcutDisplayParts("shift+cmd+k", "macos")).toEqual([
      "⇧",
      "⌘",
      "K",
    ]);
  });
});

describe("Windows shortcut notation", () => {
  it("title-cases the configured names and keeps the separators", () => {
    expect(formatShortcut("right shift", "windows")).toBe("Right Shift");
    expect(formatShortcut("ctrl+alt+k", "windows")).toBe("Ctrl+Alt+K");
  });
});
