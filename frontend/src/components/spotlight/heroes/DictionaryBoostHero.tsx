import { motion, useReducedMotion } from "framer-motion";

/* Dictionary Boost hero: a misheard spelling ("shivon", steel) is struck
   through, drifts down and fades while a short spoken wave links it to a
   Dictionary-style BOOST chip holding the boosted spelling. One 4.8s loop.
   Theme tokens only (no raw literals). Static composition under reduced
   motion: struck ghost at 0.6 opacity, chip and glow fully lit. Decorative. */

const LOOP = { duration: 4.8, repeat: Infinity, ease: [0.2, 0, 0, 1] as const };

export function DictionaryBoostHero() {
  const reduce = useReducedMotion() ?? false;
  const anim = <T extends object>(a: T) => (reduce ? {} : a);
  return (
    <svg width="172" height="120" viewBox="0 0 172 120" fill="none" aria-hidden="true" overflow="visible">
      <defs>
        <linearGradient id="spotlight-boost-underline" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stopColor="var(--color-frost)" />
          <stop offset="1" stopColor="var(--color-frost-deep)" />
        </linearGradient>
        <radialGradient id="spotlight-boost-glow" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0" stopColor="var(--color-frost)" stopOpacity="0.45" />
          <stop offset="1" stopColor="var(--color-frost)" stopOpacity="0" />
        </radialGradient>
      </defs>

      {/* Misheard spelling, struck through, drifting away. */}
      <motion.g
        style={{ opacity: reduce ? 0.6 : undefined }}
        {...anim({
          animate: { opacity: [0.85, 0.85, 0, 0, 0.85], y: [0, 0, 14, 14, 0] },
          transition: { ...LOOP, times: [0, 0.22, 0.46, 0.9, 1] },
        })}
      >
        <text
          x="86"
          y="26"
          textAnchor="middle"
          className="font-body"
          fontSize="15"
          fontWeight="500"
          letterSpacing="0.02em"
          fill="var(--color-steel)"
        >
          shivon
        </text>
        <motion.rect
          x="62"
          y="20.5"
          width="48"
          height="1.5"
          rx="0.75"
          fill="var(--color-steel)"
          opacity="0.7"
          style={{ originX: 0 }}
          {...anim({
            animate: { scaleX: [0, 0, 1, 1, 0] },
            transition: { ...LOOP, times: [0, 0.08, 0.22, 0.9, 1] },
          })}
        />
      </motion.g>

      {/* Sound-alike link: a short spoken wave. */}
      <motion.path
        d="M76 46 q2.5 -5 5 0 t5 0 t5 0 t5 0"
        stroke="var(--color-frost)"
        strokeWidth="1.6"
        strokeLinecap="round"
        {...anim({
          animate: { opacity: [0.25, 0.25, 1, 1, 0.25, 0.25] },
          transition: { ...LOOP, ease: "easeInOut" as const, times: [0, 0.2, 0.34, 0.5, 0.7, 1] },
        })}
      />

      {/* Resolved Boost word chip. */}
      <motion.ellipse
        cx="86"
        cy="80"
        rx="76"
        ry="26"
        fill="url(#spotlight-boost-glow)"
        style={{ opacity: reduce ? 1 : undefined }}
        {...anim({
          animate: { opacity: [0, 0, 1, 1, 0] },
          transition: { ...LOOP, ease: "easeInOut" as const, times: [0, 0.36, 0.56, 0.84, 1] },
        })}
      />
      <motion.g
        {...anim({
          animate: { opacity: [0.55, 0.55, 1, 1, 0.55], scale: [0.96, 0.96, 1, 1, 0.96] },
          transition: { ...LOOP, times: [0, 0.34, 0.52, 0.88, 1] },
        })}
      >
        <rect x="22" y="62" width="128" height="36" rx="18" fill="var(--surface-modal)" stroke="var(--border-accent)" />
        <rect x="31" y="71" width="40" height="18" rx="9" fill="var(--halo-frost)" />
        <text
          x="51"
          y="83.5"
          textAnchor="middle"
          className="font-display"
          fontSize="9"
          fontWeight="600"
          letterSpacing="0.08em"
          fill="var(--color-frost-deep)"
        >
          BOOST
        </text>
        <text x="80" y="85.5" className="font-display" fontSize="17" fontWeight="400" fill="var(--color-slate)">
          Siobhan
        </text>
        <rect x="80" y="91" width="60" height="2" rx="1" fill="url(#spotlight-boost-underline)" />
      </motion.g>
    </svg>
  );
}
