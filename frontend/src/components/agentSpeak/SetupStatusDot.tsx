import type { CSSProperties } from "react";
import { motion, useReducedMotion } from "framer-motion";

/**
 * The Agents setup flow's **ordinal state disc**.
 *
 * Round 01 issue 5 re-skinned this component. It used to be a 6px dot paired
 * with its own label; it is now the 20px disc that leads each `SetupExercise`
 * rung and carries the exercise's ORDINAL and its STATE in one object. The
 * label moved out to `SetupExercise`, which owns the `role="status"` region it
 * lives in.
 *
 * The three-state vocabulary survives (design.md decision 16), renamed `idle`
 * to `pending` because the disc now means "this rung has not had its turn yet"
 * rather than "this dot is dark". None of onboarding `StepTest`'s `RecStatus`
 * literals came with it: `rgba(148,163,184,0.5)` and `#7EC489` are raw colour
 * literals, which the theme system makes dark-mode bugs rather than mere
 * duplication. `StepTest` itself is deliberately NOT retro-fixed here.
 *
 * | State | Treatment (both themes resolve through tokens) |
 * |---|---|
 * | `pending` | `STEEL_BORDER` ring, **no fill**, numeral in `text-steel/60` |
 * | `active` | `--accent-fill` + `--shadow-frost-glow` + a 1.2s opacity breath |
 * | `done` | `--color-mint` + a mint glow, **numeral replaced by a check glyph** |
 *
 * The numeral-to-check swap is why pending versus done is not carried by
 * colour alone. `STEEL_BORDER` is the sanctioned theme-invariant structural
 * literal already used by `SetupKeyChip`, `AgentProviderInstallList`'s buttons, and
 * `VoicePicker`'s disc ring: steel resolves dark-on-light in Light and
 * light-on-dark in Dark, so one value draws the circle on both grounds.
 *
 * **The disc, its numeral, and its check glyph are all `aria-hidden`**
 * (copy-review.md, Accessibility notes). The disc sits beside a live region;
 * if the numeral were exposed, every state change would re-announce the
 * ordinal ("1, Speaking...", then "1, Listening..."). The ordinal is already
 * conveyed by document order and by each exercise's own label, and the label
 * is the entire message for a user who cannot see the disc.
 */

export type SetupDotState = "pending" | "active" | "done";

/** The sanctioned theme-invariant structural literal for this flow. */
const STEEL_BORDER = "1px solid rgba(107,130,153,0.28)";

const DISC: Record<SetupDotState, CSSProperties> = {
  pending: {
    background: "transparent",
    border: STEEL_BORDER,
    boxShadow: "none",
  },
  active: {
    background: "var(--accent-fill)",
    border: "1px solid transparent",
    boxShadow: "var(--shadow-frost-glow)",
  },
  done: {
    background: "var(--color-mint)",
    border: "1px solid transparent",
    boxShadow: "0 0 8px color-mix(in srgb, var(--color-mint) 55%, transparent)",
  },
};

/** The glyph's own colour rung. `text-white` is the app-wide treatment for a
 *  glyph on `--accent-fill` (AA in both themes per `system.md`) and on mint;
 *  pending drops to the tertiary text rung on the open modal surface. */
const GLYPH: Record<SetupDotState, string> = {
  pending: "text-steel/60",
  active: "text-white",
  done: "text-white",
};

/** The flow's one in-place state tween (design.md, step 4's state machine): the
 *  disc's ring, fill, and numeral colour cross it whenever a checklist line or
 *  an exercise changes state. A colour change, never a height. */
const STATE_TWEEN = "220ms cubic-bezier(0.2, 0, 0, 1)";

interface SetupStatusDotProps {
  /** 1-indexed position in the ladder. Decorative: the label carries meaning. */
  ordinal: number;
  state: SetupDotState;
}

export function SetupStatusDot({ ordinal, state }: SetupStatusDotProps) {
  const reduced = useReducedMotion();
  const breathing = state === "active" && !reduced;

  return (
    <motion.span
      aria-hidden="true"
      data-setup-dot={state}
      data-breathing={breathing ? "true" : "false"}
      className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full font-display text-[11px] font-medium leading-none ${GLYPH[state]}`}
      style={{
        ...DISC[state],
        transition: `border-color ${STATE_TWEEN}, background ${STATE_TWEEN}, color ${STATE_TWEEN}`,
      }}
      animate={breathing ? { opacity: [1, 0.4, 1] } : { opacity: 1 }}
      transition={{
        duration: 1.2,
        repeat: breathing ? Infinity : 0,
        ease: "easeInOut",
      }}
    >
      {state === "done" ? (
        <svg
          viewBox="0 0 12 12"
          className="h-3 w-3"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M2.5 6.4 4.8 8.8 9.5 3.4" />
        </svg>
      ) : (
        ordinal
      )}
    </motion.span>
  );
}
