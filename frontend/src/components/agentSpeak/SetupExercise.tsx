import type { ReactNode } from "react";

import { SecondaryButton } from "../SecondaryButton";
import { CardStatusLine, type CardStatus } from "./SpeakSection";
import { SetupStatusDot, type SetupDotState } from "./SetupStatusDot";

/**
 * One rung of step 4's exercise ladder.
 *
 * Round 01 issue 5. The two exercises used to be two `SpeakSection`s wrapping
 * two inset cards, which put a settings-panel grammar on a teaching surface and
 * gave step 4 three competing headings. A rung is the replacement: an ordinal
 * state disc, the exercise's own title row with its trigger, the instruction
 * body, and one reserved status line. No card, no fill, no shadow. The two
 * rungs separate with a 16px gap and one steel-tint hairline, drawn by
 * `SetupStepTest`.
 *
 * **The trigger stays on the title row.** That placement was never the defect;
 * it was only wrong when the title row was a *step's* heading. Here the row
 * genuinely is one exercise's own row, which is what an action slot is for.
 *
 * ## There is no muted state, and there is only one rung
 *
 * Round 01 rendered exercise 2 here from first paint in a MUTED presentation
 * (`text-steel` title, `pending` disc, `opacity-45` trigger) and un-muted it
 * when exercise 1 went terminal. Round 02 retired that rule outright (design
 * decision 32): the owner rejected it as illegible and as an overload, and
 * exercise 2 is now absent from the DOM until the checklist completes and then
 * REPLACES it. The `muted` prop went with the rule; it had no caller left, and
 * a dead prop whose docstring says it is load-bearing is worse than no prop.
 *
 * The three arguments that used to justify muted-not-hidden are answered
 * elsewhere now: the live region that must pre-exist is the step-level one that
 * survives both acts plus the checklist's own, mounted from first paint; the
 * no-reflow-within-a-step rule gained the swap as its third named exception
 * (decision 33); and the user learns act two exists from the lede's count
 * clause and from the completed summary strip.
 *
 * The rung still exists as a component because act two still needs exactly this
 * shape: a disc, a title row with the trigger on it, an instruction body, and
 * one reserved status line. `SetupStepTest` is its single consumer.
 *
 * ## A11y contract
 *
 * - The status line is **pre-mounted** and never conditionally unmounted.
 *   `CardStatusLine` routes the error tone to `role="alert"` and everything
 *   else to `role="status"`.
 * - `aria-busy` is true while the rung is running.
 * - The trigger always carries `aria-describedby` pointing at its own status
 *   line, and `aria-disabled` alongside `disabled` when it is off. A disabled
 *   control must never stand alone: while a precondition is live, that line is
 *   the sentence that says why the trigger is off.
 * - The disc is entirely `aria-hidden`; the state label is the whole message.
 */

/** The sanctioned theme-invariant structural literal for this flow.
 *  `SecondaryButton`'s own `--border-hairline` is white-family and would ship
 *  these triggers as bare floating text in Light. */
const STEEL_BORDER = "1px solid rgba(107,130,153,0.28)";

/** The flow's one in-place hierarchy tween. It survives the muting rule's
 *  retirement because the trigger still animates its own disabled opacity when
 *  a precondition clears, and it changes no height. */
const STATE_TWEEN = "220ms cubic-bezier(0.2, 0, 0, 1)";

export interface SetupExerciseProps {
  /** 1-indexed ladder position. Decorative; the label carries the meaning. */
  ordinal: number;
  /** `Locked 15` / `Locked 19`. */
  title: string;
  /** The disc's three token-routed states. */
  discState: SetupDotState;
  /** `Locked 18` + `A13`, or `Locked 24` + `A14`. The entire message for a
   *  user who cannot see the disc, which is why `failed` gets its own. */
  stateLabel: string;
  /** True while the rung is mid-run. Drives `aria-busy`. */
  busy?: boolean;
  /** `Locked 16` / `Locked 20`, both `Try it`. */
  triggerLabel: string;
  triggerId: string;
  triggerDisabled: boolean;
  onTrigger: () => void;
  /** The pre-mounted reserved line. `null` renders it empty, never absent. */
  status?: CardStatus | null;
  /** The status line's DOM id, so the trigger can describe itself with it. */
  statusId: string;
  /** The instruction body. Full `text-steel`, untouched by muting. */
  children?: ReactNode;
}

export function SetupExercise({
  ordinal,
  title,
  discState,
  stateLabel,
  busy = false,
  triggerLabel,
  triggerId,
  triggerDisabled,
  onTrigger,
  status,
  statusId,
  children,
}: SetupExerciseProps) {
  return (
    <div
      data-setup-exercise={ordinal}
      aria-busy={busy}
      className="flex flex-col"
    >
      <div className="flex items-center gap-2.5">
        <SetupStatusDot ordinal={ordinal} state={discState} />
        <h3 className="flex-1 text-[13px] font-medium text-slate">{title}</h3>
        <SecondaryButton
          className="rounded-full px-3 py-1 text-[12px] font-medium text-slate disabled:opacity-45"
          style={{ border: STEEL_BORDER, transition: `opacity ${STATE_TWEEN}` }}
          id={triggerId}
          disabled={triggerDisabled}
          aria-disabled={triggerDisabled}
          aria-describedby={statusId}
          onClick={onTrigger}
        >
          {triggerLabel}
        </SecondaryButton>
      </div>

      {/* Indented to the disc's text column so the rung reads as one object.
          30px = the 20px disc plus the 10px gap. */}
      <div className="mt-2 pl-[30px]">
        {children}

        {/* The state label's own polite region. Separate from the status line
            because the two say different things: this one is "where the
            exercise is", the line below is "what is wrong". */}
        <div role="status" className="mt-1.5 text-[11px] text-steel/70">
          {stateLabel}
        </div>

        {/* Pre-mounted, reserved, never conditionally unmounted. */}
        <div id={statusId}>
          <CardStatusLine status={status} />
        </div>
      </div>
    </div>
  );
}
