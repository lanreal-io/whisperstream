// @vitest-environment node

import { describe, it, expect } from "vitest";
import {
  namedShortcutConflict,
  shortcutConflict,
  WIN_CONFLICTS,
} from "./shortcutConflict";

describe("WIN_CONFLICTS map", () => {
  it("has exactly four entries", () => {
    expect(Object.keys(WIN_CONFLICTS).length).toBe(4);
  });

  it("keys are the four normalized combos", () => {
    expect("win" in WIN_CONFLICTS).toBe(true);
    expect("win+space" in WIN_CONFLICTS).toBe(true);
    expect("win+l" in WIN_CONFLICTS).toBe(true);
    expect("win+d" in WIN_CONFLICTS).toBe(true);
  });
});

describe("namedShortcutConflict", () => {
  it("names the exact action using the captured shortcut", () => {
    expect(
      namedShortcutConflict("CTRL+SHIFT+V", [
        { shortcut: "ctrl+shift+v", action: "Push to Talk" },
      ]),
    ).toBe("This shortcut is already used by Push to Talk.");
  });

  it("allows an empty or distinct shortcut", () => {
    const others = [{ shortcut: "ctrl+shift+v", action: "Push to Talk" }];
    expect(namedShortcutConflict("", others)).toBeNull();
    expect(namedShortcutConflict("ctrl+alt+`", others)).toBeNull();
  });
});

describe("shortcutConflict — exact locked strings", () => {
  it("win alone returns Start-menu advisory", () => {
    expect(shortcutConflict("win")).toBe(
      "Windows opens the Start menu when you press Win, so this shortcut may steal focus.",
    );
  });

  it("win+space returns input-language advisory", () => {
    expect(shortcutConflict("win+space")).toBe(
      "Windows uses Win+Space to switch input language. Both actions will fire on every press.",
    );
  });

  it("win+l returns lock-screen advisory", () => {
    expect(shortcutConflict("win+l")).toBe(
      "Windows locks the screen on Win+L and the system blocks apps from intercepting it.",
    );
  });

  it("win+d returns show-desktop advisory", () => {
    expect(shortcutConflict("win+d")).toBe(
      "Windows shows the desktop on Win+D. Both actions will fire on every press.",
    );
  });
});

describe("shortcutConflict — case-insensitive matching", () => {
  it("WIN+SPACE matches win+space", () => {
    expect(shortcutConflict("WIN+SPACE")).toBe(WIN_CONFLICTS["win+space"]);
  });

  it("Win + Space (with spaces around +) matches win+space", () => {
    expect(shortcutConflict("Win + Space")).toBe(WIN_CONFLICTS["win+space"]);
  });

  it("  win+space  (padded outer) matches win+space", () => {
    expect(shortcutConflict("  win+space  ")).toBe(WIN_CONFLICTS["win+space"]);
  });

  it("WIN alone matches win", () => {
    expect(shortcutConflict("WIN")).toBe(WIN_CONFLICTS["win"]);
  });

  it("Win+L matches win+l", () => {
    expect(shortcutConflict("Win+L")).toBe(WIN_CONFLICTS["win+l"]);
  });

  it("WIN+D matches win+d", () => {
    expect(shortcutConflict("WIN+D")).toBe(WIN_CONFLICTS["win+d"]);
  });
});

describe("shortcutConflict — alias matching", () => {
  it("cmd+space returns the same as win+space", () => {
    expect(shortcutConflict("cmd+space")).toBe(WIN_CONFLICTS["win+space"]);
  });

  it("super+l returns the same as win+l", () => {
    expect(shortcutConflict("super+l")).toBe(WIN_CONFLICTS["win+l"]);
  });

  it("meta+d returns the same as win+d", () => {
    expect(shortcutConflict("meta+d")).toBe(WIN_CONFLICTS["win+d"]);
  });

  it("left win+l returns the same as win+l", () => {
    expect(shortcutConflict("left win+l")).toBe(WIN_CONFLICTS["win+l"]);
  });

  it("right win alone returns the same as win", () => {
    expect(shortcutConflict("right win")).toBe(WIN_CONFLICTS["win"]);
  });

  it("cmd alone returns the same as win", () => {
    expect(shortcutConflict("cmd")).toBe(WIN_CONFLICTS["win"]);
  });

  it("super alone returns the same as win", () => {
    expect(shortcutConflict("super")).toBe(WIN_CONFLICTS["win"]);
  });

  it("meta alone returns the same as win", () => {
    expect(shortcutConflict("meta")).toBe(WIN_CONFLICTS["win"]);
  });
});

describe("shortcutConflict — non-conflict combos return null", () => {
  it("ctrl+space returns null", () => {
    expect(shortcutConflict("ctrl+space")).toBeNull();
  });

  it("right shift returns null", () => {
    expect(shortcutConflict("right shift")).toBeNull();
  });

  it("f4 returns null", () => {
    expect(shortcutConflict("f4")).toBeNull();
  });

  it("ctrl+mouse4 returns null", () => {
    expect(shortcutConflict("ctrl+mouse4")).toBeNull();
  });

  it("empty string returns null", () => {
    expect(shortcutConflict("")).toBeNull();
  });

  it("undefined returns null", () => {
    expect(shortcutConflict(undefined)).toBeNull();
  });

  it("null returns null", () => {
    expect(shortcutConflict(null)).toBeNull();
  });
});

describe("shortcutConflict - macOS advisories", () => {
  it("uses Mac names for Command system shortcuts", () => {
    expect(shortcutConflict("cmd+space", "macos")).toBe(
      "macOS uses Command-Space for Spotlight. Both actions may fire on every press.",
    );
    expect(shortcutConflict("command+tab", "macos")).toBe(
      "macOS uses Command-Tab to switch apps. Both actions may fire on every press.",
    );
  });

  it("does not show Windows-only advisories on macOS", () => {
    expect(shortcutConflict("cmd", "macos")).toBeNull();
    expect(shortcutConflict("cmd+d", "macos")).toBeNull();
  });
});
