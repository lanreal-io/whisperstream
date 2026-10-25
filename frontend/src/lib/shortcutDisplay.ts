export type ShortcutPlatform = "macos" | "windows";

const MAC_PART_ALIASES: Readonly<Record<string, string>> = {
  command: "cmd",
  win: "cmd",
  super: "cmd",
  meta: "cmd",
  "left win": "left cmd",
  "right win": "right cmd",
  alt: "option",
  "left alt": "left option",
  "right alt": "right option",
  control: "ctrl",
  "left control": "left ctrl",
  "right control": "right ctrl",
  return: "enter",
  escape: "esc",
};

const MAC_GLYPHS: Readonly<Record<string, string>> = {
  ctrl: "⌃",
  "left ctrl": "⌃",
  "right ctrl": "⌃",
  option: "⌥",
  "left option": "⌥",
  "right option": "⌥",
  shift: "⇧",
  "left shift": "⇧",
  "right shift": "⇧",
  cmd: "⌘",
  "left cmd": "⌘",
  "right cmd": "⌘",
  "caps lock": "⇪",
  enter: "↩",
  tab: "⇥",
  backspace: "⌫",
  delete: "⌦",
  esc: "⎋",
  left: "←",
  right: "→",
  up: "↑",
  down: "↓",
  "page up": "⇞",
  "page down": "⇟",
  home: "↖",
  end: "↘",
};

const MAC_ORDER: Readonly<Record<string, number>> = {
  ctrl: 0,
  "left ctrl": 0,
  "right ctrl": 0,
  option: 1,
  "left option": 1,
  "right option": 1,
  shift: 2,
  "left shift": 2,
  "right shift": 2,
  cmd: 3,
  "left cmd": 3,
  "right cmd": 3,
  fn: 4,
};

export function shortcutPlatform(
  navigatorPlatform = typeof navigator === "undefined" ? "" : navigator.platform,
): ShortcutPlatform {
  return /mac/i.test(navigatorPlatform) ? "macos" : "windows";
}

function normalizeMacPart(part: string): string {
  const normalized = part.trim().toLowerCase();
  return MAC_PART_ALIASES[normalized] ?? normalized;
}

export function shortcutDisplayParts(
  shortcut: string,
  platform: ShortcutPlatform = shortcutPlatform(),
): string[] {
  const parts = shortcut
    .split("+")
    .map((part) => part.trim())
    .filter(Boolean);
  if (platform !== "macos") return parts;

  return parts
    .map(normalizeMacPart)
    .filter((part, index, all) => all.indexOf(part) === index)
    .sort((left, right) => {
      const leftOrder = MAC_ORDER[left] ?? 100;
      const rightOrder = MAC_ORDER[right] ?? 100;
      return leftOrder === rightOrder
        ? left.localeCompare(right)
        : leftOrder - rightOrder;
    })
    .map((part) => {
      if (MAC_GLYPHS[part]) return MAC_GLYPHS[part];
      if (part.length === 1) return part.toUpperCase();
      if (part === "space") return "Space";
      if (/^f\d+$/.test(part)) return part.toUpperCase();
      return part.replace(/\b\w/g, (letter) => letter.toUpperCase());
    });
}

export function formatShortcut(
  shortcut: string,
  platform: ShortcutPlatform = shortcutPlatform(),
): string {
  const parts = shortcutDisplayParts(shortcut, platform);
  return platform === "macos" ? parts.join("") : parts.join("+");
}
