/**
 * The curated profile-glyph table shared by the profile stamp (`ProfileMark`),
 * the editor's icon picker, and the HUD.
 *
 * Scope is deliberately narrow: this is NOT the general icon module the
 * frontend backlog (#5) still wants. It owns exactly the fourteen profile
 * glyphs plus the three built-in derivations, so a later extraction of the
 * duplicated `XIcon` / `MailIcon` / `PlusIcon` families can happen on its own
 * terms without unpicking this one.
 *
 * Drawing constraints (every glyph): 24x24 viewBox, `fill="none"`,
 * `stroke="currentColor"`, round caps and joins, stroke width supplied by the
 * caller's size table, at most four sub-paths, and no interior detail smaller
 * than 2 viewBox units. Everything here has to survive being painted into a
 * 10px optical box inside the recording indicator.
 */
import type { ReactNode } from "react";

/**
 * The ordered curated set. ORDER IS THE PICKER'S GRID ORDER (after the letter
 * cell): the three built-in-equivalent shapes first, then work artifacts, then
 * activities. Do not sort alphabetically at render time, and keep this exactly
 * in sync with the backend's `_PROFILE_ICON_KEYS` enum in `config_manager.py`.
 */
export const PROFILE_ICON_KEYS = [
  "chat",
  "code",
  "mail",
  "document",
  "note",
  "terminal",
  "checklist",
  "calendar",
  "people",
  "sparkle",
  "search",
  "pen",
  "book",
  "globe",
] as const;

export type ProfileIconKey = (typeof PROFILE_ICON_KEYS)[number];

/**
 * The picker cell's accessible name. These name the THING, not the drawing and
 * not the use: the user assigns the meaning, so "Sparkle" beats both "Four
 * point star" and "Brainstorming" (copy-review section 8, locked).
 */
export const PROFILE_ICON_LABELS: Record<ProfileIconKey, string> = {
  chat: "Chat",
  code: "Code",
  mail: "Mail",
  document: "Document",
  note: "Note",
  terminal: "Terminal",
  checklist: "Checklist",
  calendar: "Calendar",
  people: "People",
  sparkle: "Sparkle",
  search: "Search",
  pen: "Pen",
  book: "Book",
  globe: "Globe",
};

/**
 * Built-in glyphs are DERIVED from the immutable profile id, never stored and
 * never user-editable. Keys 1-3 of the curated set intentionally duplicate
 * them so a custom "Slack" profile can look like chat; the reservation is on
 * the built-ins' own customization, not on the shapes.
 */
export const BUILTIN_PROFILE_ICONS: Record<"chat" | "code" | "email", ProfileIconKey> = {
  chat: "chat",
  code: "code",
  email: "mail",
};

/** Narrow an arbitrary stored value to a curated key. */
export function isProfileIconKey(value: unknown): value is ProfileIconKey {
  return (
    typeof value === "string"
    && (PROFILE_ICON_KEYS as readonly string[]).includes(value)
  );
}

/** The path set for one glyph, on a 24x24 viewBox. */
const PROFILE_ICON_PATHS: Record<ProfileIconKey, ReactNode> = {
  // Speech bubble with a short tail dropping from the lower left. Shares the
  // NeedsInputIndicator bubble family MINUS its question dot, which is that
  // indicator's meaning and must not leak into identity.
  chat: (
    <>
      <rect x="3" y="5" width="18" height="12" rx="4" />
      <path d="M8 17v3.5L11.5 17" />
    </>
  ),
  // A matched pair of angle brackets. No slash between them: a slash reads as
  // "path" and loads one side with extra mass.
  code: (
    <>
      <path d="M9 8l-4 4 4 4" />
      <path d="M15 8l4 4-4 4" />
    </>
  ),
  mail: (
    <>
      <rect x="3" y="6" width="18" height="12" rx="3" />
      <path d="M4.5 8.5L12 13.5 19.5 8.5" />
    </>
  ),
  document: (
    <>
      <path d="M13.5 3H7a1.5 1.5 0 00-1.5 1.5v15A1.5 1.5 0 007 21h10a1.5 1.5 0 001.5-1.5V8z" />
      <path d="M13.5 3v5h5" />
    </>
  ),
  note: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="3" />
      <path d="M8 10h8M8 14h5" />
    </>
  ),
  terminal: (
    <>
      <path d="M5 7l4.5 4.5L5 16" />
      <path d="M12.5 16.5h6.5" />
    </>
  ),
  checklist: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="3" />
      <path d="M8.5 12.2l2.4 2.4 4.6-5" />
    </>
  ),
  calendar: (
    <>
      <rect x="3.5" y="5.5" width="17" height="15" rx="3" />
      <path d="M3.5 10.5h17" />
      <path d="M8 3.5v4M16 3.5v4" />
    </>
  ),
  people: (
    <>
      <circle cx="10" cy="8" r="3.2" />
      <path d="M4 19.5a6 6 0 0112 0" />
      <path d="M16.4 6.4a3.2 3.2 0 010 5.4M17.4 14.6a6 6 0 012.6 4.9" />
    </>
  ),
  sparkle: <path d="M12 3l2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2z" />,
  search: (
    <>
      <circle cx="11" cy="11" r="6" />
      <path d="M15.5 15.5L20.5 20.5" />
    </>
  ),
  pen: (
    <>
      <path d="M4 20l1.2-4.2L16 5a2.2 2.2 0 013 3L8.2 18.8z" />
      <path d="M14.2 6.8l3 3" />
    </>
  ),
  book: (
    <>
      <path d="M12 6.5v13" />
      <path d="M12 6.5C10.5 5 8 4.5 4 4.8v12.6c4-.3 6.5.2 8 1.6 1.5-1.4 4-1.9 8-1.6V4.8c-4-.3-6.5.2-8 1.7z" />
    </>
  ),
  globe: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M3.5 12h17" />
      <path d="M12 3.5c2.6 2.3 4 5.3 4 8.5s-1.4 6.2-4 8.5c-2.6-2.3-4-5.3-4-8.5s1.4-6.2 4-8.5z" />
    </>
  ),
};

/** The 24x24 path set for one curated key. The caller owns the `<svg>`. */
export function renderProfileIcon(key: ProfileIconKey): ReactNode {
  return PROFILE_ICON_PATHS[key];
}
