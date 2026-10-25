import { memo, useRef } from "react";
import { motion, useReducedMotion } from "framer-motion";

interface PreviewGlyph {
  id: number;
  grapheme: string;
  animateEntrance: boolean;
  revealDelay: number;
}

interface GraphemeSegmenter {
  segment(input: string): Iterable<{ segment: string }>;
}

interface GraphemeSegmenterConstructor {
  new (
    locales?: string | string[],
    options?: { granularity: "grapheme" },
  ): GraphemeSegmenter;
}

const PreviewSegmenter = (
  Intl as unknown as { Segmenter: GraphemeSegmenterConstructor }
).Segmenter;
const previewSegmenter = new PreviewSegmenter(undefined, {
  granularity: "grapheme",
});
const PREVIEW_GLYPH_REVEAL_DURATION = 0.24;
const PREVIEW_GLYPH_STAGGER = 0.022;

function segmentPreviewGraphemes(text: string): string[] {
  return Array.from(previewSegmenter.segment(text), ({ segment }) => segment);
}

function reconcilePreviewGlyphs(
  previous: PreviewGlyph[],
  _previousText: string,
  _previousStableCharCount: number,
  text: string,
  nextIdRef: { current: number },
): PreviewGlyph[] {
  const nextGraphemes = segmentPreviewGraphemes(text);
  let prefixLength = 0;
  while (
    prefixLength < previous.length &&
    prefixLength < nextGraphemes.length &&
    previous[prefixLength].grapheme === nextGraphemes[prefixLength]
  ) {
    prefixLength += 1;
  }

  const isAppendOnly = prefixLength === previous.length;
  // Corrections preserve the complete common-prefix DOM, even when part of it
  // is still mutable. Only a true append receives entrance motion.
  const retainedLength = prefixLength;
  const reconciled = previous.slice(0, retainedLength);
  for (
    let index = retainedLength;
    index < nextGraphemes.length;
    index += 1
  ) {
    reconciled.push({
      id: nextIdRef.current,
      grapheme: nextGraphemes[index],
      animateEntrance: isAppendOnly,
      revealDelay: (index - retainedLength) * PREVIEW_GLYPH_STAGGER,
    });
    nextIdRef.current += 1;
  }
  return reconciled;
}

const PreviewGlyphSpan = memo(function PreviewGlyphSpan({
  glyph,
  reducedMotion,
}: {
  glyph: PreviewGlyph;
  reducedMotion: boolean;
}) {
  const sharedProps = {
    "data-testid": "preview-grapheme",
    "data-grapheme-id": glyph.id,
  };

  if (reducedMotion || !glyph.animateEntrance) {
    return <span {...sharedProps}>{glyph.grapheme}</span>;
  }

  return (
    <motion.span
      {...sharedProps}
      initial={{ filter: "blur(3px)", opacity: 0.45 }}
      animate={{ filter: "blur(0px)", opacity: 1 }}
      transition={{
        duration: PREVIEW_GLYPH_REVEAL_DURATION,
        delay: glyph.revealDelay,
        ease: [0.22, 1, 0.36, 1],
      }}
    >
      {glyph.grapheme}
    </motion.span>
  );
});

export const ReconciledPreviewText = memo(function ReconciledPreviewText({
  text,
  stableCharCount,
}: {
  text: string;
  stableCharCount: number;
}) {
  const reducedMotion = useReducedMotion() ?? false;
  const glyphsRef = useRef<PreviewGlyph[]>([]);
  const nextIdRef = useRef(1);
  const previousTextRef = useRef("");
  const previousStableCharCountRef = useRef(0);
  glyphsRef.current = reconcilePreviewGlyphs(
    glyphsRef.current,
    previousTextRef.current,
    previousStableCharCountRef.current,
    text,
    nextIdRef,
  );
  previousTextRef.current = text;
  previousStableCharCountRef.current = stableCharCount;

  return glyphsRef.current.map((glyph) => (
    <PreviewGlyphSpan
      key={glyph.id}
      glyph={glyph}
      reducedMotion={reducedMotion}
    />
  ));
});
