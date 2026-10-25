import type { ReactNode } from "react";

import { CardStatusLine, type CardStatus } from "./SpeakSection";

/**
 * The Agents setup flow's step frame.
 *
 * This replaces `SpeakSection` as the shell of every step in this flow. It is
 * NOT a variant of it and it does not modify it: `SpeakSection` is the
 * Configure modal's grammar and `HudConfigModal` is its correct home.
 *
 * The concept (round 01 `design.md`): the setup flow is the first-run overlay's
 * second act, not Configure's preview. Onboarding's `StepShortcut` and
 * `StepTest` say "the app is teaching you something, once" with a
 * display-weight step title, one line of plain explanation, and then ONE
 * instrument, alone, in the middle of the card. Every step here is a sentence
 * and an instrument, in that same grammar.
 *
 * Why a step needed a heading rung at all: as a `SpeakSection` a step's title
 * was a 13px settings label, the same rung as `Voice model`. Nothing on the
 * surface could group anything, so grouping needed a card, state needed a
 * coloured 11px line, and the step's primary action needed a section's
 * right-hand action slot. One missing rung produced all three symptoms.
 *
 * **20px, not onboarding's 24px.** This card already carries a 15px semibold
 * `Set up Agents` identity line plus the step indicator two rows above; a
 * 24px display title under a 15px semibold one inverts the weight relationship.
 * 20px `font-display font-light` still reads unambiguously as a step heading
 * rather than a settings label, and keeps the modal title as the constant
 * identity band it was designed to be.
 *
 * Contrast, both themes (no new token, no new value): heading `text-slate`
 * (`#263849` Light / `#E9F1F8` Dark) on `--surface-modal`; lede and captions
 * `text-steel` (`#607C97` / `#98AFC5`). Both are AA on that ground in both
 * themes. Nothing here introduces a fill, a border, or a shadow: this flow has
 * exactly one elevation, the modal card itself.
 *
 * Three rules the frame enforces for its hosts:
 *
 *   1. **The instrument region is `flex-1`.** Residual vertical space goes
 *      AROUND the instrument, not under it. That is what kills the dead space
 *      the fixed body height used to pool below the content, and it is exactly
 *      `StepShortcut`'s mechanism.
 *   2. **The status line is reserved and never unmounted.** `CardStatusLine`
 *      holds `min-h-[14px]` with no message, so a state change recolours a line
 *      instead of reflowing the step.
 *   3. **No `overflow` anywhere on this frame or its ancestors.**
 *      `HoverTooltip` is portal-less CSS and an overflow ancestor clips it.
 *
 * The `h2` is the flow's focus target on every step change
 * (`AgentSpeakSetupModal.focusStepHeading`). That is why the heading must name
 * the step's JOB (`Connect your coding agents`, `Pick a voice`,
 * `Set your shortcut`, `Try it out`) rather than a noun: it is the first thing a screen-reader user
 * hears when the step changes.
 */
export interface SetupStepFrameProps {
  /** The step's job, as a sentence-case phrase. Renders as the focusable `h2`. */
  heading: string;
  /** One or two rendered lines of plain explanation. Optional by design: a lede
   *  that repeats its own heading is the defect this frame exists to remove. */
  lede?: ReactNode;
  /** Optional right-aligned element on the heading row, vertically centered on
   *  the 20px display heading. Mirrors `SpeakSection`'s `action` slot so a
   *  step's aggregate status (e.g. Step 1's `StatusLight`) reads as
   *  `Heading  ● READY`, the same treatment as the Configure dialog's
   *  `Agent  ● READY`, rather than as a floating orphan below the rows. */
  headingAction?: ReactNode;
  /** The step's single reserved status line. Genuine faults only. */
  status?: CardStatus | null;
  className?: string;
  /** The instrument. Centre it with `flex-1 flex items-center justify-center`. */
  children?: ReactNode;
}

export function SetupStep({
  heading,
  lede,
  headingAction,
  status,
  className,
  children,
}: SetupStepFrameProps) {
  // Stable per element without pulling `useId` into a component that renders
  // once per step: the heading text is the step's identity in this flow.
  const headingId = `setup-step-${heading.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;

  return (
    <section
      className={`flex flex-1 flex-col${className ? ` ${className}` : ""}`}
      aria-labelledby={headingId}
    >
      {/* The heading row. `headingAction` (Step 1's aggregate `StatusLight`)
          sits right, vertically centered on the 20px display heading rather
          than baseline-aligned to it, matching `SpeakSection`'s title/action
          treatment. Absent an action the row collapses to just the h2. */}
      <div className="flex items-center justify-between gap-4">
        <h2
          id={headingId}
          className="font-display text-[20px] font-light leading-tight tracking-tight text-slate"
        >
          {heading}
        </h2>
        {headingAction}
      </div>

      {lede ? (
        <p className="mt-2 text-[12px] leading-relaxed text-steel">{lede}</p>
      ) : null}

      {/* The instrument region. `flex-1` is load-bearing: it is what puts the
          step's slack around the instrument instead of under it. */}
      <div className="mt-5 flex flex-1 flex-col">{children}</div>

      <CardStatusLine status={status} />
    </section>
  );
}
