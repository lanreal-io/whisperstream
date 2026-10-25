import { shortcutPlatform, type ShortcutPlatform } from "./shortcutDisplay";

export const WIN_CONFLICTS: Readonly<Record<string, string>> = {
  win: "Windows opens the Start menu when you press Win, so this shortcut may steal focus.",
  "win+space":
    "Windows uses Win+Space to switch input language. Both actions will fire on every press.",
  "win+l":
    "Windows locks the screen on Win+L and the system blocks apps from intercepting it.",
  "win+d":
    "Windows shows the desktop on Win+D. Both actions will fire on every press.",
};

export const MAC_CONFLICTS: Readonly<Record<string, string>> = {
  "win+space":
    "macOS uses Command-Space for Spotlight. Both actions may fire on every press.",
  "win+tab":
    "macOS uses Command-Tab to switch apps. Both actions may fire on every press.",
  "win+q":
    "macOS uses Command-Q to quit the active app. Both actions may fire on every press.",
  "win+h":
    "macOS uses Command-H to hide the active app. Both actions may fire on every press.",
};

// Alias map: normalize these part names to their canonical form before lookup.
// cmd/super/meta and left-win/right-win all resolve to "win" for matching purposes.
const PART_ALIASES: Readonly<Record<string, string>> = {
  cmd: "win",
  super: "win",
  meta: "win",
  command: "win",
  option: "alt",
  "left option": "alt",
  "right option": "alt",
  control: "ctrl",
  "left control": "ctrl",
  "right control": "ctrl",
  "left cmd": "win",
  "right cmd": "win",
  "left win": "win",
  "right win": "win",
};

function normalize(shortcut: string): string {
  return shortcut
    .split("+")
    .map((part) => part.trim().toLowerCase())
    .map((part) => PART_ALIASES[part] ?? part)
    .join("+");
}

export function shortcutConflict(
  shortcut: string | undefined | null,
  platform: ShortcutPlatform = "windows",
): string | null {
  if (!shortcut) return null;
  const conflicts = platform === "macos" ? MAC_CONFLICTS : WIN_CONFLICTS;
  return conflicts[normalize(shortcut)] ?? null;
}

export function currentShortcutConflict(
  shortcut: string | undefined | null,
): string | null {
  return shortcutConflict(shortcut, shortcutPlatform());
}

/**
 * Returns a short hint if `shortcut` is non-empty and exactly matches one of
 * the other bound shortcuts in `others`. Used by Controls to flag cross-field
 * duplicates (e.g. PTT and Toggle bound to the same key).
 *
 * Empty strings in `others` are ignored (unbound slots never conflict).
 */
export function crossFieldConflict(
  shortcut: string | undefined | null,
  others: (string | undefined | null)[],
): string | null {
  if (!shortcut) return null;
  const norm = normalize(shortcut);
  for (const other of others) {
    if (other && normalize(other) === norm) {
      return "This key is already used by another shortcut.";
    }
  }
  return null;
}

export function namedShortcutConflict(
  shortcut: string | undefined | null,
  others: ReadonlyArray<{
    shortcut: string | undefined | null;
    action: string;
  }>,
): string | null {
  if (!shortcut) return null;
  const norm = normalize(shortcut);
  const conflict = others.find(
    (candidate) =>
      Boolean(candidate.shortcut) && normalize(candidate.shortcut ?? "") === norm,
  );
  return conflict
    ? `This shortcut is already used by ${conflict.action}.`
    : null;
}
