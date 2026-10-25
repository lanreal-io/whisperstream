/**
 * `ProfileMark` — the profile stamp.
 *
 * One softly-rounded tile carrying either a curated glyph or a single
 * letterform in EXACTLY the same footprint and ink. It is the same object at
 * every size across the product (recording indicator 16, Enhancement row 20,
 * editor header 24, picker cell 32), and its distinguishing property is
 * PARITY: a letter and a glyph differ only in contents, so a custom profile
 * with no icon never looks like a broken state.
 *
 * Ink is `--color-slate` for both variants. Identity is not status, so the
 * stamp never borrows gold, rose, or mint.
 *
 * It is `aria-hidden` in every context: the HUD renders nothing at all under a
 * detected screen reader, and every in-app placement sits beside an
 * already-labeled profile name.
 */
import {
  BUILTIN_PROFILE_ICONS,
  isProfileIconKey,
  renderProfileIcon,
  type ProfileIconKey,
} from "./profileIcons";

export type ProfileMarkSize = 16 | 20 | 24 | 32;

/**
 * Every number the stamp draws with, keyed by tile size. Radius is
 * `round(size * 0.3)` up to 24 and deliberately below `--radius-input` (12):
 * the system's radius scale is written for inputs and cards, and a 12px radius
 * on a 16px tile would read as a status dot. Do not hardcode per-size magic
 * numbers anywhere else.
 */
export const PROFILE_MARK_SIZES: Record<
  ProfileMarkSize,
  { radius: number; glyphBox: number; stroke: number; letter: number }
> = {
  16: { radius: 5, glyphBox: 10, stroke: 1.25, letter: 9 },
  20: { radius: 6, glyphBox: 13, stroke: 1.4, letter: 11 },
  24: { radius: 7, glyphBox: 15, stroke: 1.5, letter: 13 },
  32: { radius: 9, glyphBox: 18, stroke: 1.6, letter: 15 },
};

/** The neutral empty-name marker. Not a "?", not a generic glyph. */
const EMPTY_DOT_PX = 4;

/**
 * The first user-perceived grapheme of a profile name, upper-cased. Returns ""
 * for an empty or whitespace-only name (the caller then renders the neutral
 * dot). Emoji and non-cased scripts pass through unchanged: no transliteration,
 * no forced color.
 */
export function deriveProfileLetter(name: string): string {
  const trimmed = (name ?? "").trim();
  if (!trimmed) return "";
  let first: string | undefined;
  // Intl.Segmenter is present in WebView2/Chromium but is not in this project's
  // TS lib target, so it is reached through a minimal local shape. The
  // Array.from fallback keeps this correct-enough (code points, not graphemes)
  // anywhere the runtime lacks it.
  const Segmenter = (
    Intl as unknown as {
      Segmenter?: new (
        locales?: string | string[],
        options?: { granularity?: string },
      ) => { segment(input: string): Iterable<{ segment: string }> };
    }
  ).Segmenter;
  if (typeof Segmenter === "function") {
    const iterator = new Segmenter(undefined, { granularity: "grapheme" })
      .segment(trimmed)[Symbol.iterator]();
    first = iterator.next().value?.segment;
  }
  if (!first) first = Array.from(trimmed)[0];
  return (first ?? "").toLocaleUpperCase();
}

export type ProfileMarkContent =
  | { kind: "glyph"; key: ProfileIconKey }
  | { kind: "letter"; letter: string }
  | { kind: "empty" };

/**
 * Contents resolution, in order:
 *   1. a built-in id -> its fixed derived glyph (a stored key is ignored);
 *   2. a valid curated key -> that glyph;
 *   3. absent / empty / UNRECOGNIZED key -> the letter. An unknown key falls
 *      through to the letter rather than to a generic glyph so a config written
 *      by a newer build degrades to something truthful instead of mislabeling;
 *   4. letter with an empty name -> the neutral dot.
 */
export function resolveProfileMark(
  profileId: string | undefined,
  name: string,
  iconKey: string | undefined,
): ProfileMarkContent {
  if (profileId && profileId in BUILTIN_PROFILE_ICONS) {
    return {
      kind: "glyph",
      key: BUILTIN_PROFILE_ICONS[profileId as keyof typeof BUILTIN_PROFILE_ICONS],
    };
  }
  if (isProfileIconKey(iconKey)) return { kind: "glyph", key: iconKey };
  const letter = deriveProfileLetter(name);
  return letter ? { kind: "letter", letter } : { kind: "empty" };
}

export function ProfileMark({
  profileId,
  name,
  iconKey,
  size,
  testId = "profile-mark",
  appearance,
}: {
  /** The profile's id. A built-in id derives its own fixed glyph. */
  profileId?: string;
  /** The profile's live name; the letter fallback is derived from it. */
  name: string;
  /** The stored curated key for a custom profile. */
  iconKey?: string;
  size: ProfileMarkSize;
  /** QA hook override; the default is the shared `profile-mark` id. */
  testId?: string;
  /**
   * Surface overrides for the picker cell, whose rest/hover/selected states
   * ARE the tile. Everywhere else the stamp keeps its own rest tokens.
   */
  appearance?: {
    background?: string;
    border?: string;
    boxShadow?: string;
    ink?: string;
  };
}) {
  const spec = PROFILE_MARK_SIZES[size];
  const content = resolveProfileMark(profileId, name, iconKey);
  const dataIcon =
    content.kind === "glyph"
      ? content.key
      : content.kind === "letter"
        ? "letter"
        : "empty";

  return (
    <span
      aria-hidden="true"
      data-testid={testId}
      data-profile-icon={dataIcon}
      className="inline-flex shrink-0 items-center justify-center transition-colors duration-200"
      style={{
        width: `${size}px`,
        height: `${size}px`,
        borderRadius: `${spec.radius}px`,
        background: appearance?.background ?? "var(--surface-control)",
        border: appearance?.border ?? "1px solid var(--border-hairline)",
        boxShadow: appearance?.boxShadow ?? "inset 0 1px 0 var(--rim-specular)",
        color: appearance?.ink ?? "var(--color-slate)",
        fontFamily: "var(--font-body)",
        fontSize: `${spec.letter}px`,
        fontWeight: 600,
        lineHeight: 1,
        textAlign: "center",
        letterSpacing: 0,
      }}
    >
      {content.kind === "glyph" ? (
        <svg
          width={spec.glyphBox}
          height={spec.glyphBox}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={spec.stroke}
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          {renderProfileIcon(content.key)}
        </svg>
      ) : content.kind === "letter" ? (
        content.letter
      ) : (
        <span
          style={{
            width: `${EMPTY_DOT_PX}px`,
            height: `${EMPTY_DOT_PX}px`,
            borderRadius: "9999px",
            background: "currentColor",
          }}
        />
      )}
    </span>
  );
}
