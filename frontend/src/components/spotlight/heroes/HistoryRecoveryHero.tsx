import { motion, useReducedMotion } from "framer-motion";

/* History recovery hero: the gold recovery key, the same gold that marks the
   recovery key block everywhere it appears. A slow glow under motion; static
   under reduced motion. Fills come from theme tokens. */
export function HistoryRecoveryHero() {
  const reduce = useReducedMotion() ?? false;
  return (
    <svg width="164" height="120" viewBox="0 0 164 120" fill="none" aria-hidden="true">
      <motion.circle
        cx="82"
        cy="60"
        r="34"
        fill="color-mix(in srgb, var(--color-gold-mist) 70%, transparent)"
        style={{ transformOrigin: "82px 60px" }}
        {...(reduce
          ? {}
          : {
              animate: { opacity: [0.55, 1, 0.55], scale: [0.94, 1.04, 0.94] },
              transition: { duration: 4.2, repeat: Infinity, ease: "easeInOut" as const },
            })}
      />
      <g stroke="var(--color-gold-deep)" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="68" cy="72" r="13" />
        <path d="M77.5 62.5L104 36M96 44l7 7M89 51l6 6" />
      </g>
    </svg>
  );
}
