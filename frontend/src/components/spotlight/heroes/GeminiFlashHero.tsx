import { motion, useReducedMotion } from "framer-motion";

/* Gemini 3.5 Flash hero: a frost spark/flash that brightens and sharpens
   (speed + precision). Animated by default; a static composition under reduced
   motion. All fills come from theme tokens (no raw literals). */

const pulse = {
  animate: { opacity: [0.55, 1, 0.55], scale: [0.86, 1, 0.86] },
  transition: { duration: 3.4, repeat: Infinity, ease: "easeInOut" as const },
};

const pulseSmall = {
  animate: { opacity: [0.4, 0.95, 0.4], scale: [0.8, 1, 0.8] },
  transition: { duration: 3.4, repeat: Infinity, ease: "easeInOut" as const, delay: 1.2 },
};

export function GeminiFlashHero() {
  const reduce = useReducedMotion() ?? false;
  return (
    <svg width="164" height="120" viewBox="0 0 164 120" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="spotlight-gemini" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="var(--color-frost-light)" />
          <stop offset="1" stopColor="var(--color-frost-deep)" />
        </linearGradient>
      </defs>
      <motion.path
        d="M82 28 C88 54 94 60 120 66 C94 72 88 78 82 104 C76 78 70 72 44 66 C70 60 76 54 82 28Z"
        fill="url(#spotlight-gemini)"
        style={{ transformOrigin: "82px 66px" }}
        {...(reduce ? {} : pulse)}
      />
      <motion.path
        d="M128 34 C130 44 133 47 143 49 C133 51 130 54 128 64 C126 54 123 51 113 49 C123 47 126 44 128 34Z"
        fill="var(--color-frost)"
        style={{ transformOrigin: "128px 49px" }}
        {...(reduce ? {} : pulseSmall)}
      />
      <motion.circle
        cx="44"
        cy="94"
        r="3"
        fill="var(--color-frost)"
        style={{ transformOrigin: "44px 94px" }}
        {...(reduce ? {} : pulseSmall)}
      />
    </svg>
  );
}
