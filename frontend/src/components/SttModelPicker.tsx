import { Dropdown, type DropdownOption } from "./Dropdown";
import { HoverTooltip } from "./HoverTooltip";
import type { SttBackend } from "../lib/languages";

/**
 * The Home model row's speech-model picker.
 *
 * A composition over the shared `Dropdown`, not a fork of it. The bespoke
 * `ModelPicker` from the pre-Automatic/Manual rounds re-rolled the whole
 * portal/positioning/keyboard stack to get one per-row slot; `Dropdown` now
 * ships two (`adornment` inside the row button, `renderOptionAction` outside
 * it), which is exactly what the locked design needs.
 *
 * Parakeet, Cohere, and advanced Qwen choices reuse this picker. Parakeet's English-only and
 * multilingual artifacts stay behind the single `NVIDIA Parakeet` choice
 * (scope.md); the host resolves which exact artifact a pick lands on.
 *
 * Trailing state is icon-only, and the meaning rides on the accessible name
 * rather than the glyph shape:
 *   selected   -> check, announced as `Current model: {model}`, never an action
 *   downloaded -> `Delete {model}` button, which must not also select the row
 *   absent     -> `Download {model}` glyph with visible tooltip; selecting the
 *                 ROW is the action, so the glyph stays inside the option
 *                 button where pointer and keyboard activation still select
 *
 * The selected announcement is `Dropdown`'s `accessibleDescription`, so it is
 * bound to that option through `aria-describedby` and is read when the row
 * takes focus. It used to be a bare `sr-only` span inside the check's action
 * slot: a visually separate sibling of the option, which a reader announces
 * only while browsing the panel and never on focus, so the one user who needs
 * it was the one user who did not get it.
 *
 * Copy is locked.
 */

export type SttModelOptionState = "selected" | "downloaded" | "absent";

export interface SttModelPickerOption {
  backend: SttBackend;
  state: SttModelOptionState;
  /** Downloaded artifacts that are configured or loaded are protected: the
   *  backend refuses to delete them, so the UI must not offer it either. */
  deletable?: boolean;
}

/** Locked user-facing names. Never a version, never a variant. */
export const STT_MODEL_LABELS: Record<SttBackend, string> = {
  parakeet: "NVIDIA Parakeet",
  qwen: "Qwen3 ASR",
  cohere: "Cohere Transcribe",
};

interface SttModelPickerProps {
  value: SttBackend;
  /** Rendered in the given order; Parakeet leads for dual-support languages. */
  options: SttModelPickerOption[];
  onChange: (backend: SttBackend) => void;
  /** The row's trash was activated. The host opens the deletion confirmation;
   *  nothing is removed from here. */
  onRequestDelete: (backend: SttBackend) => void;
  /** True while a model operation owns the recognizer. */
  disabled?: boolean;
}

export function SttModelPicker({
  value,
  options,
  onChange,
  onRequestDelete,
  disabled = false,
}: SttModelPickerProps) {
  const stateByBackend = new Map(
    options.map((option) => [option.backend, option]),
  );

  const dropdownOptions: DropdownOption<SttBackend>[] = options.map(
    ({ backend, state }) => ({
      value: backend,
      label: STT_MODEL_LABELS[backend],
      badge:
        backend === "parakeet"
          ? { label: "Fastest", tone: "frost" }
          : backend === "cohere"
            ? { label: "Best quality", tone: "frost" }
            : undefined,
      badgeInOptionsOnly: true,
      // A description, not a name: it states which model is running without
      // relabelling the row and without reading as something to activate.
      accessibleDescription:
        state === "selected"
          ? `Current model: ${STT_MODEL_LABELS[backend]}`
          : state === "absent"
            ? `Download ${STT_MODEL_LABELS[backend]}`
            : undefined,
      // Only the absent state uses the in-button slot, so the collapsed trigger
      // (which always shows the SELECTED option) never carries a glyph.
      //
      adornment:
        state === "absent" ? (
          // The visual bubble is hidden from the accessibility tree so its
          // text cannot join and duplicate the option name. The same locked
          // sentence is bound to the row through `aria-describedby` above.
          <span aria-hidden="true" className="inline-flex">
            <HoverTooltip
              side="auto"
              width="w-44"
              content={`Download ${STT_MODEL_LABELS[backend]}`}
            >
              <DownloadGlyph />
            </HoverTooltip>
          </span>
        ) : undefined,
    }),
  );

  return (
    <Dropdown<SttBackend>
      value={value}
      options={dropdownOptions}
      onChange={onChange}
      disabled={disabled}
      ariaLabel="Voice recognition model"
      triggerClassName="w-[168px]"
      // Wider than the trigger on purpose: the option rows give up 40px of
      // right padding to the action slot, and at the trigger's own width
      // `NVIDIA Parakeet` wrapped onto two lines behind the check.
      panelWidth={312}
      // These rows do not need to scroll, and both download and delete
      // affordances carry hover bubbles that have to escape the panel box.
      panelOverflow="visible"
      // The trash is icon-only and lives outside the option button, so Tab has
      // to be able to reach it while the panel is open.
      optionActionsFocusable
      renderOptionAction={(option) => {
        const entry = stateByBackend.get(option.value);
        if (!entry) return null;
        if (entry.state === "selected") {
          // Purely visual: the sentence lives on the option's
          // `aria-describedby`, so nothing here is announced or actionable.
          return (
            <span className="flex h-6 w-6 items-center justify-center">
              <SelectedGlyph />
            </span>
          );
        }
        if (entry.state !== "downloaded" || entry.deletable === false) {
          return null;
        }
        const label = `Delete ${STT_MODEL_LABELS[option.value]}`;
        return (
          <HoverTooltip side="auto" width="w-44" content={label}>
            <button
              type="button"
              aria-label={label}
              onClick={(event) => {
                // The action node is a sibling of the option button, but a
                // pointer event here must not reach the row wrapper either.
                event.stopPropagation();
                onRequestDelete(option.value);
              }}
              className="flex h-6 w-6 cursor-pointer items-center justify-center rounded-full text-steel/70 transition-colors hover:text-rose"
            >
              <TrashGlyph />
            </button>
          </HoverTooltip>
        );
      }}
    />
  );
}

function SelectedGlyph() {
  return (
    <svg
      viewBox="0 0 12 12"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className="text-steel"
    >
      <path d="M2.5 6.5l2.5 2.5 4.5-5" />
    </svg>
  );
}

function DownloadGlyph() {
  return (
    <svg
      viewBox="0 0 12 12"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className="text-steel/70"
    >
      <path d="M6 1.5v6" />
      <path d="M3.5 5.5L6 8l2.5-2.5" />
      <path d="M2 10.5h8" />
    </svg>
  );
}

function TrashGlyph() {
  return (
    <svg
      viewBox="0 0 12 12"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M2 3.5h8" />
      <path d="M3 3.5v6.5a1 1 0 001 1h4a1 1 0 001-1V3.5" />
      <path d="M5 3.5v-1a1 1 0 011-1h0a1 1 0 011 1v1" />
      <path d="M5 5.5v3M7 5.5v3" />
    </svg>
  );
}
