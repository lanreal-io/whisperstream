import { useMemo, type ReactNode } from "react";
import { diffTranscripts, type TranscriptDiff } from "../lib/transcriptDiff";

const ADDED_STYLE: React.CSSProperties = {
  background: "color-mix(in srgb, var(--color-mint) 16%, transparent)",
  textDecorationLine: "underline",
  textDecorationColor: "var(--color-mint)",
  textDecorationThickness: "1.5px",
  textUnderlineOffset: "2px",
  borderRadius: "3px",
};

const REMOVED_STYLE: React.CSSProperties = {
  background: "color-mix(in srgb, var(--color-rose) 14%, transparent)",
  textDecorationLine: "line-through",
  textDecorationColor: "var(--color-rose)",
  textDecorationThickness: "1.5px",
  borderRadius: "3px",
};

/** Shared lossless diff rendering for transcript Bakeoff and fine-tune audit
 * evidence. A replacement remains an adjacent removed + added pair so the old
 * text stays visible and the new text is never confused with a caution state. */
export function SemanticDiffSegments({ diff }: { diff: TranscriptDiff }) {
  return (
    <>
      {diff.segments.map((segment, index) => {
        if (segment.op === "equal") return <span key={index}>{segment.text}</span>;
        if (segment.op === "added") {
          return (
            <ins
              key={index}
              aria-label={`Added: ${segment.text}`}
              data-diff-op="added"
              style={ADDED_STYLE}
            >
              {segment.text}
            </ins>
          );
        }
        return (
          <del
            key={index}
            aria-label={`Removed: ${segment.text}`}
            data-diff-op="removed"
            style={REMOVED_STYLE}
          >
            {segment.text}
          </del>
        );
      })}
    </>
  );
}

export function SemanticDiffText({
  original,
  revised,
  fallback,
}: {
  original: string;
  revised: string;
  fallback?: ReactNode;
}) {
  const diff = useMemo(() => diffTranscripts(original, revised), [original, revised]);
  if (!diff.ok) {
    return (
      <span className="select-text">
        <span>{revised}</span>
        {fallback ?? (
          <span className="mt-1 block text-[10px] text-steel/60">
            Diff too large. Showing clean text.
          </span>
        )}
      </span>
    );
  }
  return (
    <span className="select-text">
      <SemanticDiffSegments diff={diff} />
    </span>
  );
}

export function SemanticDiffLegend() {
  return (
    <p className="flex items-center gap-4 text-[11px] text-steel">
      <span className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="px-1" style={ADDED_STYLE}>
          +
        </span>
        Added
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="px-1" style={REMOVED_STYLE}>
          {"−"}
        </span>
        Removed
      </span>
    </p>
  );
}
