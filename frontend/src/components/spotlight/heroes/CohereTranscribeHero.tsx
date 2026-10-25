import { motion, useReducedMotion } from "framer-motion";

/* Cohere transcribe hero: a transcription waveform (equalizer) resolving onto a
   crisp baseline (accuracy). Bars animate by default; static under reduced
   motion. All fills come from theme tokens (no raw literals). */

const HEIGHTS = [30, 58, 40, 72, 48, 84, 52, 64, 34];

export function CohereTranscribeHero() {
  const reduce = useReducedMotion() ?? false;
  return (
    <svg width="172" height="120" viewBox="0 0 172 120" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="spotlight-cohere" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--color-frost)" />
          <stop offset="1" stopColor="var(--color-frost-deep)" />
        </linearGradient>
      </defs>
      {HEIGHTS.map((h, i) => {
        const x = 32 + i * 12;
        const y = 96 - h;
        return (
          <motion.rect
            key={i}
            x={x}
            y={y}
            width="6"
            height={h}
            rx="3"
            fill="url(#spotlight-cohere)"
            style={{ transformOrigin: `${x + 3}px 96px` }}
            {...(reduce
              ? {}
              : {
                  animate: { scaleY: [0.4, 1, 0.4] },
                  transition: {
                    duration: 1.5,
                    repeat: Infinity,
                    ease: "easeInOut" as const,
                    delay: i * 0.11,
                  },
                })}
          />
        );
      })}
      <rect x="30" y="102" width="116" height="2.5" rx="1.25" fill="var(--color-frost)" opacity="0.55" />
    </svg>
  );
}
