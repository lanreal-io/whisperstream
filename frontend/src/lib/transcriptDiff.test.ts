import { describe, expect, it } from "vitest";
import {
  TRANSCRIPT_DIFF_MAX_CELLS,
  TRANSCRIPT_DIFF_MAX_CHARS,
  TRANSCRIPT_DIFF_MAX_TOKENS,
  diffTranscripts,
  tokenizeTranscript,
  type TranscriptDiff,
} from "./transcriptDiff";

/** Reassemble one side of a diff. `equal` text belongs to both sides. */
function rebuild(diff: TranscriptDiff, side: "original" | "revised"): string {
  const skip = side === "original" ? "added" : "removed";
  return diff.segments
    .filter((segment) => segment.op !== skip)
    .map((segment) => segment.text)
    .join("");
}

function ops(diff: TranscriptDiff): string[] {
  return diff.segments.map((segment) => segment.op);
}

describe("tokenizeTranscript", () => {
  it("covers every character so the tokens rebuild the input exactly", () => {
    const text =
      "  okay so\tthis is an update on the re-enhance feature...\n\nIt's 42% \"done\", right? eleve emoji \u{1F525} too.  ";
    const tokens = tokenizeTranscript(text);

    expect(tokens.join("")).toBe(text);
    expect(tokens.every((token) => token.length > 0)).toBe(true);
  });

  it("attaches leading whitespace to its token so a space never anchors a match", () => {
    expect(tokenizeTranscript("don't re-enhance, now")).toEqual([
      "don't",
      " re-enhance",
      ",",
      " now",
    ]);
  });

  it("groups a punctuation run, keeps trailing space, and returns nothing for empty text", () => {
    expect(tokenizeTranscript("wait...")).toEqual(["wait", "..."]);
    expect(tokenizeTranscript("hi \n")).toEqual(["hi", " \n"]);
    expect(tokenizeTranscript("   ")).toEqual(["   "]);
    expect(tokenizeTranscript("")).toEqual([]);
  });
});

describe("diffTranscripts", () => {
  it("reports no change for identical text and preserves it verbatim", () => {
    const text = "Here is an update on the feature.";
    const diff = diffTranscripts(text, text);

    expect(diff.ok).toBe(true);
    expect(diff.added).toBe(0);
    expect(diff.removed).toBe(0);
    expect(diff.changed).toBe(false);
    expect(diff.segments).toEqual([{ op: "equal", text }]);
  });

  it("handles empty sides without inventing segments", () => {
    expect(diffTranscripts("", "")).toEqual({
      ok: true,
      segments: [],
      added: 0,
      removed: 0,
      changed: false,
    });

    const added = diffTranscripts("", "new words");
    expect(added.segments).toEqual([{ op: "added", text: "new words" }]);
    expect(added.added).toBe(2);

    const removed = diffTranscripts("old words", "");
    expect(removed.segments).toEqual([{ op: "removed", text: "old words" }]);
    expect(removed.removed).toBe(2);
  });

  it("marks a pure addition and counts only content tokens", () => {
    const diff = diffTranscripts("one two", "one and a half two");

    expect(diff.ok).toBe(true);
    expect(ops(diff)).toEqual(["equal", "added", "equal"]);
    expect(diff.added).toBe(3);
    expect(diff.removed).toBe(0);
    expect(diff.changed).toBe(true);
    expect(rebuild(diff, "original")).toBe("one two");
    expect(rebuild(diff, "revised")).toBe("one and a half two");
  });

  it("marks a pure removal", () => {
    const diff = diffTranscripts("one filler two", "one two");

    expect(ops(diff)).toEqual(["equal", "removed", "equal"]);
    expect(diff.added).toBe(0);
    expect(diff.removed).toBe(1);
    expect(rebuild(diff, "original")).toBe("one filler two");
    expect(rebuild(diff, "revised")).toBe("one two");
  });

  it("renders a replacement as a removal immediately followed by an addition", () => {
    const diff = diffTranscripts("the quick brown fox", "the slow brown fox");

    expect(ops(diff)).toEqual(["equal", "removed", "added", "equal"]);
    expect(diff.segments[1]).toEqual({ op: "removed", text: " quick" });
    expect(diff.segments[2]).toEqual({ op: "added", text: " slow" });
    expect(diff.added).toBe(1);
    expect(diff.removed).toBe(1);
  });

  it("preserves punctuation and whitespace on both sides of a real cleanup", () => {
    const original = "okay so  this is an update\non the feature";
    const revised = "This is an update on the feature.";
    const diff = diffTranscripts(original, revised);

    expect(diff.ok).toBe(true);
    expect(rebuild(diff, "original")).toBe(original);
    expect(rebuild(diff, "revised")).toBe(revised);
    expect(diff.added).toBeGreaterThan(0);
    expect(diff.removed).toBeGreaterThan(0);
  });

  it("treats a punctuation-only edit as one addition rather than no change", () => {
    const diff = diffTranscripts("stop go", "stop, go");

    expect(diff.changed).toBe(true);
    expect(diff.added).toBe(1);
    expect(diff.removed).toBe(0);
    expect(rebuild(diff, "original")).toBe("stop go");
    expect(rebuild(diff, "revised")).toBe("stop, go");
  });

  it("treats a case change as a replacement, not an equality", () => {
    const diff = diffTranscripts("okay then", "Okay then");

    expect(ops(diff)).toEqual(["removed", "added", "equal"]);
    expect(diff.added).toBe(1);
    expect(diff.removed).toBe(1);
  });

  it("merges neighbouring tokens of one operation into a single segment", () => {
    const diff = diffTranscripts("a b c d", "a x y d");

    expect(ops(diff)).toEqual(["equal", "removed", "added", "equal"]);
    expect(diff.segments[1].text).toBe(" b c");
    expect(diff.segments[2].text).toBe(" x y");
  });

  it("is deterministic for the same inputs", () => {
    const original = "one two three four five six seven";
    const revised = "one three four extra five seven eight";

    expect(diffTranscripts(original, revised)).toEqual(
      diffTranscripts(original, revised),
    );
  });

  it("never leaves an empty segment behind", () => {
    const diff = diffTranscripts("alpha beta", "alpha  beta gamma");

    expect(diff.segments.every((segment) => segment.text.length > 0)).toBe(true);
    expect(rebuild(diff, "original")).toBe("alpha beta");
    expect(rebuild(diff, "revised")).toBe("alpha  beta gamma");
  });

  it("falls back to clean text when either side exceeds the character bound", () => {
    const huge = "word ".repeat(TRANSCRIPT_DIFF_MAX_CHARS);
    expect(huge.length).toBeGreaterThan(TRANSCRIPT_DIFF_MAX_CHARS);

    const diff = diffTranscripts(huge, "short");
    expect(diff).toEqual({
      ok: false,
      segments: [],
      added: 0,
      removed: 0,
      changed: false,
    });
    expect(diffTranscripts("short", huge).ok).toBe(false);
  });

  it("falls back when a pathological input exceeds the token bound", () => {
    // Every mark is its own punctuation token, so the token count blows past
    // the bound long before the character bound does.
    const pathological = "!"
      .repeat(TRANSCRIPT_DIFF_MAX_TOKENS + 1)
      .split("")
      .join(" ");
    expect(pathological.length).toBeLessThan(TRANSCRIPT_DIFF_MAX_CHARS);
    expect(tokenizeTranscript(pathological).length).toBeGreaterThan(
      TRANSCRIPT_DIFF_MAX_TOKENS,
    );

    expect(diffTranscripts(pathological, "clean").ok).toBe(false);
  });

  it("falls back when two unrelated bodies exceed the comparison bound", () => {
    const side = (prefix: string) =>
      Array.from({ length: 2200 }, (_, i) => `${prefix}${i}`).join(" ");
    const tokens = tokenizeTranscript(side("a")).length;

    expect(tokens).toBeLessThan(TRANSCRIPT_DIFF_MAX_TOKENS);
    expect((tokens + 1) * (tokens + 1)).toBeGreaterThan(TRANSCRIPT_DIFF_MAX_CELLS);
    expect(diffTranscripts(side("a"), side("b")).ok).toBe(false);
  });

  it("still diffs a long transcript whose common prefix and suffix trim away", () => {
    const body = Array.from({ length: 2000 }, (_, i) => `word${i}`).join(" ");
    const original = `${body} middle ${body}`;
    const revised = `${body} centre ${body}`;

    const diff = diffTranscripts(original, revised);
    expect(diff.ok).toBe(true);
    expect(diff.added).toBe(1);
    expect(diff.removed).toBe(1);
    expect(rebuild(diff, "original")).toBe(original);
    expect(rebuild(diff, "revised")).toBe(revised);
  });
});
