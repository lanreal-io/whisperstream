import type { ReactNode } from "react";

import type { CodingAgentProvider } from "../../lib/api/agents";
import { SetupKeyChip } from "./SetupKeyChip";
import { SetupStatusDot, type SetupDotState } from "./SetupStatusDot";

/**
 * Step 4's first act: the live checklist over a REAL selected-provider turn.
 *
 * Exercise 1 used to fire a synthetic reply and watch
 * itself; it now fires nothing. The user leaves this window, sends a message in
 * their own coding-agent session, and Whisperstream lights three or four lines
 * as each one is proved by a real event.
 *
 * **This component is presentational.** It owns no timer, no subscription, no
 * API call, and no business logic. It takes the line states and renders them;
 * `SetupStepTest` owns the observers, the auto-submit guard that drops line 4,
 * and Claude's provider-specific swap to exercise 2.
 *
 * ## Three redundant signals, none of them colour (WCAG 1.4.1, decision 16)
 *
 *   1. **Shape**: `SetupStatusDot` swaps the numeral for a check glyph on
 *      `done`. Reused VERBATIM at its existing 20px. No smaller sibling: the
 *      disc's whole job was already "carry an ordinal and a state in one
 *      object", which is exactly a checklist line's job, and a second disc
 *      would be the per-screen invention PR #404 removed.
 *   2. **Text**: every line carries a trailing status word (`B3a` `Not yet` /
 *      `B3b` `Now` / `B3c` `Done`), GOV.UK's convention of a text status label
 *      with no checkbox and no tick of its own. This is the carrier for
 *      pending-versus-active, which the shape swap does not cover, and it is
 *      the ENTIRE state message for a user who cannot see the disc.
 *   3. **Type rung**: the title is `text-steel` before its turn and `text-slate`
 *      from active onward. A hierarchy shift, not an opacity reduction: a
 *      literal gray-out would land near 2.6:1 in Light.
 *
 * ## It must not look like a checkbox (decision 16)
 *
 * No square, no hollow box, no hit target, no cursor change, no hover state on
 * a line, no `role="checkbox"`, no `<input>`, no per-line `<button>`. Nothing
 * here is focusable. The research is unanimous: a checkbox-shaped mark on a
 * system-verified item is the anti-pattern where the user tries to operate the
 * mark and stalls waiting for it to respond.
 *
 * ## Sub-text never collapses, in any state (decision 31)
 *
 * The user reads these lines while alt-tabbed into another application, and
 * NN/g's finding is that people cannot read an instruction and apply it at the
 * same time, with short-term memory for it fading in about 20 seconds. So each
 * sub-text is a reference card, not a progress readout: it renders at full
 * `text-steel` before, during, and AFTER its line completes, and the locked copy
 * is written so it still reads correctly on return (no past tense, no result
 * report, no pointer at anything only visible in this window).
 *
 * ## A11y contract (design.md, `## Accessibility contract`)
 *
 * - **One polite live region wraps the whole list** (`role="status"`,
 *   `aria-live="polite"`, `aria-atomic="false"`), mounted from first paint and
 *   never conditionally unmounted. One region for the list rather than one per
 *   line is an explicit owner decision, 2026-08-14. A live region that mounts
 *   only when it has something to say is never announced.
 * - Each line's title row is `aria-atomic="true"`, so a status change announces
 *   the whole line as `<title>, <status word>` rather than a bare `Done`. The
 *   sr-only comma is what makes that read as a sentence. The sub-text sits
 *   outside that row and never changes, so it is never re-announced.
 * - `aria-busy="true"` while any line is active.
 * - **The disc, its numeral, and its check glyph stay `aria-hidden`**, as
 *   `SetupStatusDot` already is. Exposing the numeral re-announces the ordinal
 *   on every state change; document order already conveys it.
 * - Focus never moves in here, on a line completing or on anything else.
 *
 * ## Tokens
 *
 * Every value routes through an existing token or a Tailwind utility that
 * resolves to one: `text-slate`, `text-steel`, and (inside `SetupStatusDot`)
 * `--accent-fill`, `--color-mint`, `--shadow-frost-glow`, `STEEL_BORDER`. No
 * raw hex, no fill, no border, no shadow of this component's own: the flow has
 * exactly one elevation, the modal card, and lines separate by whitespace.
 * Deliberately NOT `--divider` anywhere here: it is white at 0.25 in Light and
 * invisible on this near-white modal.
 */

/** The three line states. Same vocabulary as the disc, deliberately: the disc
 *  is the state's shape and the status word is the state's text. */
export type SetupChecklistLineState = SetupDotState;

/** `B3a` / `B3b` / `B3c`. Short by design: they are appended to a title on
 *  every announcement, and the reserved slot is 48px wide. */
export const CHECKLIST_STATUS_WORD: Record<SetupChecklistLineState, string> = {
  pending: "Not yet",
  active: "Now",
  done: "Done",
};

/** `B1a` to `B1d`. Line 4 is dropped WHOLE (title and sub-text together) when
 *  `claude_reply_auto_submit` is true; never render a line whose event cannot
 *  arrive (decision 30). */
const PROVIDER_LABEL: Record<CodingAgentProvider, string> = {
  codex: "Codex",
  claude: "Claude Code",
};

export function checklistTitles(
  provider: CodingAgentProvider | null,
  speechEnabled: boolean = true,
): readonly string[] {
  return [
    provider === null
      ? "Send a message in your coding agent"
      : `Send a message in ${PROVIDER_LABEL[provider]}`,
    speechEnabled
      ? "Hear the reply in Whisperstream."
      : "See the reply in Whisperstream.",
    "Hold your dictation key and answer",
    "Press Enter to send your reply",
  ];
}

/** Retained as the Claude default for existing inventory/documentation imports. */
export const CHECKLIST_TITLES = checklistTitles("claude");

/** The title's colour transition. Same 220ms `[0.2, 0, 0, 1]` the flow uses for
 *  every in-place hierarchy change; this is a colour change, never a height. */
const RUNG_TWEEN = "220ms cubic-bezier(0.2, 0, 0, 1)";

export interface SetupChecklistProps {
  /**
   * One state per line, in order. **Length 3 or 4**: three when
   * `claude_reply_auto_submit` is on and line 4 is guarded off, four otherwise.
   * The caller owns that condition and owns monotonic back-filling.
   */
  states: readonly SetupChecklistLineState[];
  /** The resolved setup provider, or `null` while both agents are connected and
   *  neither has answered yet. `null` renders the agent-agnostic copy ("your
   *  coding agent"); it resolves to the named copy on the first real completion
   *  (Task 3.1 / 3.2). */
  provider: CodingAgentProvider | null;
  /** `config.push_to_talk_shortcut`, rendered as line 3's inline key chip. */
  pushToTalkShortcut: string;
  /** When false, line 2 shows visual/summary copy instead of audio playback copy. */
  speechEnabled?: boolean;
  className?: string;
}

/** `B2a` to `B2d`. Static, and static is the point (decision 31).
 *
 *  `B2c` is the only one with a chip. It inherits `A7`'s rule: `SetupKeyChip`
 *  takes no `aria-label` and its separators are `aria-hidden`, so the sentence
 *  reads continuously as "Hold Right Shift while speaking, then release." */
function subTextFor(
  index: number,
  pushToTalkShortcut: string,
  provider: CodingAgentProvider | null,
  speechEnabled: boolean = true,
): ReactNode {
  switch (index) {
    case 0:
      // Agnostic (both connected, none answered): drop the /hooks vs
      // /reload-plugins split until the responder is known.
      return provider === null
        ? "Ask your coding agent anything. If you just connected it, reload it first."
        : provider === "codex"
          ? "Ask your own Codex session anything. If you just connected, run /hooks there first."
          : "Ask your own Claude Code session anything. If you just connected, run /reload-plugins there first.";
    case 1:
      return speechEnabled
        ? "Listen for the reply through your speakers or headphones."
        : "Read the summary in the overlay.";
    case 2:
      return (
        <>
          Hold <SetupKeyChip keys={pushToTalkShortcut} /> while speaking, then
          release.
        </>
      );
    default:
      return provider === null
        ? "Your words land in the HUD reply box. Press Enter to send them to your coding agent."
        : `Your words land in the HUD reply box. Press Enter to send them to ${PROVIDER_LABEL[provider]}.`;
  }
}

export function SetupChecklist({
  states,
  pushToTalkShortcut,
  provider,
  speechEnabled = true,
  className,
}: SetupChecklistProps) {
  const anyActive = states.some((state) => state === "active");
  const titles = checklistTitles(provider, speechEnabled);

  return (
    <div
      data-setup-checklist={states.length}
      role="status"
      aria-live="polite"
      aria-atomic="false"
      aria-busy={anyActive}
      className={`flex flex-col gap-4${className ? ` ${className}` : ""}`}
    >
      {states.map((state, index) => (
        <div
          key={titles[index]}
          data-setup-checklist-line={index + 1}
          data-state={state}
          className="flex flex-col"
        >
          {/* The title row. `aria-atomic` makes a status change announce the
              whole line, not a bare status word. Gaps are 12px on both sides
              of the title; the status slot is reserved at 48px so a word
              change can never shift the row. */}
          <div className="flex items-center gap-3" aria-atomic="true">
            <SetupStatusDot ordinal={index + 1} state={state} />

            <h3
              className={`flex-1 text-[13px] font-medium ${
                state === "pending" ? "text-steel" : "text-slate"
              }`}
              style={{ transition: `color ${RUNG_TWEEN}` }}
            >
              {titles[index]}
            </h3>

            {/* Deliberately `text-steel`, not `text-steel/70`: this is a
                meaning carrier, not a decorative micro-label. */}
            <span
              data-setup-checklist-status
              className="w-12 shrink-0 text-right text-[11px] text-steel"
            >
              <span className="sr-only">, </span>
              {CHECKLIST_STATUS_WORD[state]}
            </span>
          </div>

          {/* Indented to the disc's text column (20px disc + 12px gap) so the
              line reads as one object. Never collapsed, in any state. */}
          <p
            data-setup-checklist-subtext
            className="mt-1 pl-8 text-[12px] leading-relaxed text-steel"
          >
            {subTextFor(index, pushToTalkShortcut, provider, speechEnabled)}
          </p>
        </div>
      ))}
    </div>
  );
}
