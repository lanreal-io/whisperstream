// ---------------------------------------------------------------------------
// transcriptDiff — the bounded word diff behind the Bakeoff comparison rail.
//
// Every Bakeoff answer is diffed against the ONE saved raw transcript, never
// against another model's answer, so this module only ever compares two
// strings. It is deliberately:
//
//   - deterministic: the same pair of inputs always produces the same segments
//     (a fixed LCS tie-break, no heuristics keyed on timing or randomness);
//   - lossless: concatenating the `equal` + `removed` segments rebuilds the
//     original byte for byte, and `equal` + `added` rebuilds the revised text,
//     so whitespace, punctuation, and line breaks all survive the round trip;
//   - bounded: pathological or very large inputs return `ok: false` instead of
//     running a quadratic diff on the render path and freezing the WebView.
//     The caller shows clean text plus the approved fallback sentence.
//
// It never logs, and diff output must never reach diagnostics.
// ---------------------------------------------------------------------------

/** One text operation. A replacement is a `removed` run immediately followed
 *  by an `added` run; there is deliberately no third "changed" operation. */
export type TranscriptDiffOp = "equal" | "added" | "removed";

/** A maximal run of neighbouring tokens that share one operation. */
export interface TranscriptDiffSegment {
  op: TranscriptDiffOp;
  text: string;
}

export interface TranscriptDiff {
  /** false when the input was too large to highlight safely. `segments` is
   *  then empty and the caller must render clean text with the fallback note. */
  ok: boolean;
  segments: TranscriptDiffSegment[];
  /** Content tokens (words or punctuation runs) added by the model. */
  added: number;
  /** Content tokens present in the raw transcript that the model dropped. */
  removed: number;
  changed: boolean;
}

/** Per-side character ceiling. A dictation this long is far past the point
 *  where an inline diff helps, and the WS contract already caps output at
 *  800,000 characters. */
export const TRANSCRIPT_DIFF_MAX_CHARS = 60_000;
/** Per-side token ceiling. Guards inputs that are short in characters but
 *  tokenize into an enormous stream (long runs of isolated punctuation). */
export const TRANSCRIPT_DIFF_MAX_TOKENS = 12_000;
/** Ceiling on the dynamic-programming table AFTER the common prefix and
 *  suffix are trimmed. 2.5M cells is a 10 MB `Int32Array` and a few tens of
 *  milliseconds; two unrelated bodies larger than that fall back to clean. */
export const TRANSCRIPT_DIFF_MAX_CELLS = 2_500_000;

const EMPTY_DIFF: TranscriptDiff = {
  ok: false,
  segments: [],
  added: 0,
  removed: 0,
  changed: false,
};

// One token is: any leading whitespace plus exactly one content unit (a word,
// with internal apostrophes/hyphens kept whole, or a run of punctuation). A
// trailing whitespace run with no content after it is its own token.
//
// Whitespace is attached to the FOLLOWING content rather than tokenized on its
// own because a bare " " token matches almost everywhere and shreds a real
// rewrite into alternating one-word spans. Attaching it forward (rather than
// backward) also keeps the highlight from starting after the space it belongs
// to at a line break.
const TOKEN_RE =
  /\s*(?:[\p{L}\p{N}]+(?:['’_-][\p{L}\p{N}]+)*|[^\s\p{L}\p{N}]+)|\s+/gu;

const CONTENT_RE = /\S/;

/** Split text into diff tokens. `tokenizeTranscript(t).join("") === t`. */
export function tokenizeTranscript(text: string): string[] {
  if (!text) return [];
  const tokens: string[] = [];
  TOKEN_RE.lastIndex = 0;
  let match = TOKEN_RE.exec(text);
  while (match !== null) {
    if (match[0].length === 0) break; // defensive: never loop on a zero-width match
    tokens.push(match[0]);
    match = TOKEN_RE.exec(text);
  }
  return tokens;
}

/** Diff one model answer against the saved raw transcript. */
export function diffTranscripts(
  original: string,
  revised: string,
): TranscriptDiff {
  if (
    original.length > TRANSCRIPT_DIFF_MAX_CHARS ||
    revised.length > TRANSCRIPT_DIFF_MAX_CHARS
  ) {
    return { ...EMPTY_DIFF };
  }
  if (original === revised) {
    return {
      ok: true,
      segments: original ? [{ op: "equal", text: original }] : [],
      added: 0,
      removed: 0,
      changed: false,
    };
  }

  const source = tokenizeTranscript(original);
  const target = tokenizeTranscript(revised);
  if (
    source.length > TRANSCRIPT_DIFF_MAX_TOKENS ||
    target.length > TRANSCRIPT_DIFF_MAX_TOKENS
  ) {
    return { ...EMPTY_DIFF };
  }

  // Enhancement usually rewrites a small part of a long transcript, so trim
  // the shared head and tail before paying for the table at all.
  const shortest = Math.min(source.length, target.length);
  let head = 0;
  while (head < shortest && source[head] === target[head]) head += 1;
  let tail = 0;
  while (
    tail < shortest - head &&
    source[source.length - 1 - tail] === target[target.length - 1 - tail]
  ) {
    tail += 1;
  }

  const a = source.slice(head, source.length - tail);
  const b = target.slice(head, target.length - tail);
  if ((a.length + 1) * (b.length + 1) > TRANSCRIPT_DIFF_MAX_CELLS) {
    return { ...EMPTY_DIFF };
  }

  const segments: TranscriptDiffSegment[] = [];
  let added = 0;
  let removed = 0;
  const push = (op: TranscriptDiffOp, text: string) => {
    if (!text) return;
    const last = segments[segments.length - 1];
    if (last !== undefined && last.op === op) last.text += text;
    else segments.push({ op, text });
  };
  const take = (op: TranscriptDiffOp, token: string) => {
    if (op === "added" && CONTENT_RE.test(token)) added += 1;
    if (op === "removed" && CONTENT_RE.test(token)) removed += 1;
    push(op, token);
  };

  push("equal", source.slice(0, head).join(""));

  // Suffix LCS lengths: table[i][j] is the LCS of a[i..] and b[j..]. Walking
  // it FORWARD (rather than backtracking from the end) is what lets a
  // replacement come out as removed-then-added in reading order.
  const width = b.length + 1;
  const table = new Int32Array((a.length + 1) * width);
  for (let i = a.length - 1; i >= 0; i -= 1) {
    const row = i * width;
    const next = row + width;
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[row + j] =
        a[i] === b[j]
          ? table[next + j + 1] + 1
          : Math.max(table[next + j], table[row + j + 1]);
    }
  }

  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      push("equal", a[i]);
      i += 1;
      j += 1;
      continue;
    }
    // Ties prefer the removal, so the original words are shown first and the
    // walk stays deterministic.
    if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
      take("removed", a[i]);
      i += 1;
    } else {
      take("added", b[j]);
      j += 1;
    }
  }
  while (i < a.length) {
    take("removed", a[i]);
    i += 1;
  }
  while (j < b.length) {
    take("added", b[j]);
    j += 1;
  }

  push("equal", tail > 0 ? source.slice(source.length - tail).join("") : "");

  return { ok: true, segments, added, removed, changed: added > 0 || removed > 0 };
}
