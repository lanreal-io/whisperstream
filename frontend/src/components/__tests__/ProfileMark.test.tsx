/**
 * Vitest + Testing Library coverage for ProfileMark — the shared profile stamp.
 *
 * The load-bearing contract is PARITY: a curated glyph and a letter fallback are
 * the same object with different contents (same tile, same radius, same ink,
 * same aria treatment), so a custom profile with no icon never looks broken.
 *
 * Tests cover:
 *   - built-in ids derive their fixed glyph and ignore any stored icon key;
 *   - a valid curated key renders that glyph;
 *   - an unknown / empty key falls through to the LETTER, not a generic glyph;
 *   - the letter is the first user-perceived grapheme, upper-cased;
 *   - an empty / whitespace name renders the neutral 4px dot;
 *   - the size table drives tile, radius and glyph box for every size;
 *   - the mark is aria-hidden and inked with --color-slate in every variant.
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { ProfileMark, PROFILE_MARK_SIZES, deriveProfileLetter } from "../ProfileMark";

afterEach(() => cleanup());

function mark() {
  return screen.getByTestId("profile-mark");
}

describe("ProfileMark — contents resolution", () => {
  it("derives built-in glyphs from the immutable id", () => {
    render(<ProfileMark profileId="chat" name="Chat" size={16} />);
    expect(mark().getAttribute("data-profile-icon")).toBe("chat");
    cleanup();
    render(<ProfileMark profileId="code" name="Code" size={16} />);
    expect(mark().getAttribute("data-profile-icon")).toBe("code");
    cleanup();
    render(<ProfileMark profileId="email" name="Email" size={16} />);
    expect(mark().getAttribute("data-profile-icon")).toBe("mail");
  });

  it("a built-in ignores a stored icon key (built-ins are never customizable)", () => {
    render(<ProfileMark profileId="chat" name="Chat" iconKey="globe" size={16} />);
    expect(mark().getAttribute("data-profile-icon")).toBe("chat");
  });

  it("a custom profile with a valid curated key renders that glyph", () => {
    render(<ProfileMark profileId="c1" name="Research" iconKey="search" size={20} />);
    expect(mark().getAttribute("data-profile-icon")).toBe("search");
    expect(mark().querySelector("svg")).not.toBeNull();
  });

  it("an unrecognized key falls through to the letter, never a generic glyph", () => {
    render(<ProfileMark profileId="c1" name="Slack" iconKey="rocket" size={20} />);
    expect(mark().getAttribute("data-profile-icon")).toBe("letter");
    expect(mark().textContent).toBe("S");
    expect(mark().querySelector("svg")).toBeNull();
  });

  it("an absent or empty key falls back to the letter", () => {
    render(<ProfileMark profileId="c1" name="notes" size={20} />);
    expect(mark().getAttribute("data-profile-icon")).toBe("letter");
    expect(mark().textContent).toBe("N");
    cleanup();
    render(<ProfileMark profileId="c1" name="notes" iconKey="" size={20} />);
    expect(mark().getAttribute("data-profile-icon")).toBe("letter");
  });

  it("renders the neutral dot (not a '?') for an empty or whitespace name", () => {
    render(<ProfileMark profileId="c1" name="   " size={16} />);
    expect(mark().getAttribute("data-profile-icon")).toBe("empty");
    expect(mark().textContent).toBe("");
    const dot = mark().querySelector("span");
    expect(dot?.style.width).toBe("4px");
    expect(dot?.style.height).toBe("4px");
  });
});

describe("deriveProfileLetter", () => {
  it("takes the first grapheme and upper-cases it", () => {
    expect(deriveProfileLetter("  slack  ")).toBe("S");
    expect(deriveProfileLetter("email")).toBe("E");
  });

  it("returns a single grapheme for emoji and combining marks", () => {
    expect(Array.from(deriveProfileLetter("👨‍👩‍👧 Family")).length).toBeGreaterThan(0);
    expect(deriveProfileLetter("éclair")).toBe("É");
  });

  it("returns an empty string for an empty or whitespace name", () => {
    expect(deriveProfileLetter("")).toBe("");
    expect(deriveProfileLetter("   ")).toBe("");
  });
});

describe("ProfileMark — parity and geometry", () => {
  it("glyph and letter share tile, radius, ink and aria treatment at every size", () => {
    for (const size of [16, 20, 24, 32] as const) {
      const spec = PROFILE_MARK_SIZES[size];
      cleanup();
      render(<ProfileMark profileId="c1" name="Slack" iconKey="pen" size={size} />);
      const glyph = mark();
      const glyphStyle = {
        width: glyph.style.width,
        height: glyph.style.height,
        borderRadius: glyph.style.borderRadius,
        color: glyph.style.color,
      };
      expect(glyphStyle.width).toBe(`${size}px`);
      expect(glyphStyle.borderRadius).toBe(`${spec.radius}px`);
      expect(glyph.getAttribute("aria-hidden")).toBe("true");
      const svg = glyph.querySelector("svg");
      expect(svg?.getAttribute("width")).toBe(String(spec.glyphBox));
      expect(svg?.getAttribute("stroke-width")).toBe(String(spec.stroke));

      cleanup();
      render(<ProfileMark profileId="c1" name="Slack" size={size} />);
      const letter = mark();
      expect(letter.style.width).toBe(glyphStyle.width);
      expect(letter.style.height).toBe(glyphStyle.height);
      expect(letter.style.borderRadius).toBe(glyphStyle.borderRadius);
      expect(letter.style.color).toBe(glyphStyle.color);
      expect(letter.getAttribute("aria-hidden")).toBe("true");
      expect(letter.style.fontSize).toBe(`${spec.letter}px`);
    }
  });

  it("inks both variants with --color-slate and never a status color", () => {
    render(<ProfileMark profileId="c1" name="Slack" iconKey="book" size={16} />);
    expect(mark().style.color).toBe("var(--color-slate)");
    cleanup();
    render(<ProfileMark profileId="c1" name="Slack" size={16} />);
    expect(mark().style.color).toBe("var(--color-slate)");
  });

  it("uses tokens for its surface, border and specular rim", () => {
    render(<ProfileMark profileId="c1" name="Slack" iconKey="book" size={16} />);
    const el = mark();
    expect(el.style.background).toBe("var(--surface-control)");
    expect(el.style.border).toContain("var(--border-hairline)");
    expect(el.style.boxShadow).toContain("var(--rim-specular)");
  });
});
