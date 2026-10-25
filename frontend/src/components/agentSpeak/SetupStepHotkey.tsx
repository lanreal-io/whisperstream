import { useEffect, useState } from "react";

import { ShortcutCapture } from "../ShortcutCapture";
import type { CardStatus } from "./SpeakSection";
import { SetupStep } from "./SetupStep";
import { SetupKeyChip } from "./SetupKeyChip";
import type { AppConfig } from "../../lib/api/config";
import type { AppEvent } from "../../lib/socket/events";
import {
  namedShortcutConflict,
  shortcutConflict,
} from "../../lib/shortcutConflict";
import type { SetupStepProps } from "./AgentSpeakSetupModal";

/**
 * Step 3 of the Agents setup flow: bind the shortcut that opens the full
 * reply, and state how you answer it.
 *
 * Round 01 issue 4 rebuilt this step on `SetupStep`. It no longer uses
 * `SpeakSection`, it draws no card, and it renders no label beside its control.
 *
 * **The capture is the step's instrument, centred and chrome-less**, exactly
 * `StepShortcut`'s arrangement in first-run onboarding: one `ShortcutCapture`
 * inside `flex-1 flex items-center justify-center`, no label row, no status
 * text of its own. `ShortcutCapture` self-labels (`Not set` / the bound value /
 * `Press shortcut`, plus `Esc to cancel` at 11px `text-steel/60`), and the 20px
 * heading (`A3`) plus the lede (`Locked 11`) already say what is being bound.
 * The copied `SettingRow` and its `View full response` label are DELETED: they
 * existed only because settings grammar requires every control to carry a label
 * to its left, and that grammar is what made this step read as a control panel.
 * `Inherited 38` is still the shortcut's name everywhere else in the app (the
 * conflict candidate list references it); this
 * surface simply does not render it.
 *
 * **The two reply gestures are ONE sentence, not two rows** (copy `A7`, with
 * `A7a` when exactly one is bound and `A7b` when neither is). They are facts,
 * not controls: rendered as label/chip rows they looked editable, and they put
 * three same-species rows into two treatments, which is what the owner
 * objected to. Facts are sentences, and `SetupKeyChip` was built for a key
 * inside prose (no `aria-label`, `aria-hidden` `+` separators, so the sentence
 * reads continuously). **`Not set` never appears inside the sentence**:
 * `hold [Not set]` is an instruction to press nothing, so the unbound half is
 * dropped from the sentence instead, and with neither bound the sentence is
 * suppressed entirely and `Locked 14` carries it on the reserved status line.
 *
 * Agents binds no dictation gesture of its own (scope decision 5): the
 * push-to-talk and toggle shortcuts belong to Controls, and giving the setup
 * flow a second place to edit them would imply Agents owns them.
 *
 * `ShortcutCapture` already wins Esc through a capture-phase listener with
 * `stopPropagation`, so this step adds no Esc guard of its own.
 */

/** Copy `A3`. The step's job, and the focus target on every step change. */
const HEADING = "Set your shortcut";

/** Copy `Locked 11`. */
const LEDE =
  "The HUD shows short summaries. Use this shortcut to open the full reply.";

/** Copy `Locked 14`. */
const NO_GESTURE =
  "No dictation shortcut is set, so you cannot reply by voice yet.";

const DEFAULT_VIEW_SHORTCUT = "ctrl+alt+`";

export interface SetupStepHotkeyProps extends SetupStepProps {
  onConfigChange: (patch: Partial<AppConfig>) => void;
  connected: boolean;
  send: (msg: Record<string, unknown>) => boolean;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
}

export function SetupStepHotkey({
  config,
  onCanAdvanceChange,
  onChildDialogOpenChange,
  onConfigChange,
  connected,
  send,
  subscribe,
}: SetupStepHotkeyProps) {
  const [captureConflict, setCaptureConflict] = useState<string | null>(null);

  // This step owns no blocking child dialog; say so once so the shell's Esc
  // handler is never left standing down after a step change.
  useEffect(() => {
    onChildDialogOpenChange(false);
  }, [onChildDialogOpenChange]);

  const viewShortcut = config.claude_view_shortcut ?? DEFAULT_VIEW_SHORTCUT;

  // The same candidate list Configure builds, unchanged.
  const viewConflictCandidates = [
    { shortcut: config.primary_shortcut, action: "the primary shortcut" },
    { shortcut: config.push_to_talk_shortcut, action: "Push to Talk" },
    { shortcut: config.toggle_shortcut, action: "Toggle" },
    { shortcut: config.paste_last_shortcut, action: "Paste Last Transcript" },
    {
      shortcut: config.paste_last_original_shortcut,
      action: "Paste Last Transcript (Original)",
    },
    ...Object.values(config.profile_hotkeys ?? {}).map((shortcut) => ({
      shortcut,
      action: "a profile shortcut",
    })),
    { shortcut: "enter", action: "Enter" },
    { shortcut: "esc", action: "Escape" },
  ];
  const viewConflict =
    shortcutConflict(viewShortcut) ||
    namedShortcutConflict(viewShortcut, viewConflictCandidates);
  const conflict = captureConflict ?? viewConflict;

  const holdKeys = config.push_to_talk_shortcut ?? "";
  const tapKeys = config.toggle_shortcut ?? "";
  const noReplyGesture = !holdKeys && !tapKeys;

  /**
   * Continue is disabled while a conflict is live (design decision 25): a
   * conflicting capture is never saved, so step 4 would fire the test against
   * a stale binding and the user would press a key that does nothing.
   */
  useEffect(() => {
    onCanAdvanceChange(conflict === null);
  }, [conflict, onCanAdvanceChange]);

  // Conflict (error) > both reply gestures unbound (attention) > empty. The
  // unbound rung is attention rather than error on purpose: it does not block
  // this step, it tells the user why the voice half of step 4 will not work.
  const status: CardStatus | null = conflict
    ? { message: conflict, tone: "error" }
    : noReplyGesture
      ? { message: NO_GESTURE, tone: "attention" }
      : null;

  return (
    <SetupStep heading={HEADING} lede={LEDE} status={status}>
      <div className="flex flex-1 flex-col">
        {/* The instrument, alone in the middle of the remaining space and
            chrome-less, byte-for-byte onboarding's `StepShortcut`. */}
        <div className="flex flex-1 items-center justify-center">
          <ShortcutCapture
            value={viewShortcut}
            onChange={(keys) => {
              const next =
                shortcutConflict(keys) ||
                namedShortcutConflict(keys, viewConflictCandidates);
              setCaptureConflict(next);
              if (!next) onConfigChange({ claude_view_shortcut: keys });
            }}
            send={send}
            subscribe={subscribe}
            connected={connected}
            captureTarget="claude_view"
            clearable={false}
          />
        </div>

        {/* Copy `A7` / `A7a`. One sentence, centred, at the quiet rung: this is
            a fact about gestures the user already has, not a control. `A7b`
            suppresses it and `Locked 14` speaks on the status line instead. */}
        {noReplyGesture ? null : (
          <p
            data-setup-gesture-sentence
            className="mt-4 text-center text-[11px] leading-relaxed text-steel/70"
          >
            To reply,{" "}
            {holdKeys ? (
              <>
                hold <SetupKeyChip keys={holdKeys} />
              </>
            ) : null}
            {holdKeys && tapKeys ? " or " : null}
            {tapKeys ? (
              <>
                tap <SetupKeyChip keys={tapKeys} />
              </>
            ) : null}
            . Agents adds no new shortcut.
          </p>
        )}
      </div>
    </SetupStep>
  );
}
