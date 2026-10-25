import {
  type ShortcutPlatform,
  shortcutDisplayParts,
  shortcutPlatform,
} from "../../../lib/shortcutDisplay";
import { HUD_META_CAPTION_SIZE } from "../metaText";

/** The recording shortcuts a reply hint can name. `lastOrigin` is the
 *  shortcut that started the most recent recording this session (memory
 *  only): it breaks the tie when both are bound. */
export interface ReplyShortcuts {
  pushToTalk: string;
  toggle: string;
  lastOrigin: "push_to_talk" | "toggle" | null;
}

/** Which shortcut the hint names, and the verb that matches how it records.
 *  Null when neither is bound: the hint then offers click alone. */
export function replyHintShortcut(
  shortcuts: ReplyShortcuts | null | undefined,
): { verb: "Hold" | "Press"; keys: string } | null {
  if (!shortcuts) return null;
  const ptt = shortcuts.pushToTalk.trim();
  const toggle = shortcuts.toggle.trim();
  if (ptt && (!toggle || shortcuts.lastOrigin !== "toggle")) {
    return { verb: "Hold", keys: ptt };
  }
  if (toggle) return { verb: "Press", keys: toggle };
  return null;
}

/** Copy (ux-writing pass, owner-approved 2026-10-24). The pill drops
 *  "or click" because the whole pill opens typing; the field names both. */
const PILL_SUFFIX = "to reply";
const PILL_HINT_FONT_SIZE = HUD_META_CAPTION_SIZE;
const FIELD_SUFFIX = "or click to reply";
const CLICK_ONLY = "Click to reply";

function titleCase(token: string): string {
  return token.replace(/\b\w/g, (letter) => letter.toUpperCase());
}

/** The resting reply hint. "pill" is the faint line in the compact pill's
 *  identity row; "field" is the glance card and console field placeholder.
 *  Key names render as chips; the sentence stays plain text for screen
 *  readers, with no per-chip label. */
export function ReplyHint({
  shortcuts,
  form,
  platform = shortcutPlatform(),
}: {
  shortcuts: ReplyShortcuts | null | undefined;
  form: "pill" | "field";
  platform?: ShortcutPlatform;
}) {
  const shortcut = replyHintShortcut(shortcuts);
  const pill = form === "pill";
  // The rail styles the field form's resting line. The pill's is a 9px
  // caption under the session name (owner, 2026-10-24, prototype 1b).
  const fontSize = pill ? PILL_HINT_FONT_SIZE : undefined;
  if (shortcut === null) {
    return (
      <span data-testid={`reply-hint-${form}`} style={{ fontSize }}>
        {CLICK_ONLY}
      </span>
    );
  }
  const parts = shortcutDisplayParts(shortcut.keys, platform);
  // Plain text, no key chips: the hint stays a quiet line (owner,
  // 2026-10-24: the chipped version was too prominent).
  const keys =
    platform === "macos" ? parts.join("") : parts.map(titleCase).join("+");
  return (
    <span
      data-testid={`reply-hint-${form}`}
      className="whitespace-nowrap"
      style={{ fontSize }}
    >
      {`${shortcut.verb} ${keys} ${pill ? PILL_SUFFIX : FIELD_SUFFIX}`}
    </span>
  );
}

/** Plain-text width estimate of the pill hint, for the pill's content-fit
 *  width. Measured with the same font so the identity row never truncates. */
export function measurePillHintWidth(
  shortcuts: ReplyShortcuts | null | undefined,
  platform: ShortcutPlatform = shortcutPlatform(),
): number {
  if (typeof document === "undefined") return 0;
  const shortcut = replyHintShortcut(shortcuts);
  const parts = shortcut ? shortcutDisplayParts(shortcut.keys, platform) : [];
  const keys = platform === "macos" ? parts.join("") : parts.join("+");
  const text = shortcut ? `${shortcut.verb} ${keys} ${PILL_SUFFIX}` : CLICK_ONLY;
  const measurement = document.createElement("span");
  measurement.textContent = text;
  Object.assign(measurement.style, {
    position: "fixed",
    left: "-100000px",
    top: "0",
    whiteSpace: "nowrap",
    fontFamily: "var(--font-agent)",
    fontSize: `${PILL_HINT_FONT_SIZE}px`,
    fontWeight: "500",
    letterSpacing: "0.01em",
  });
  document.body.appendChild(measurement);
  const width = Math.ceil(measurement.getBoundingClientRect().width);
  measurement.remove();
  return width + 2;
}
