import { hasVoiceSample } from "./voiceSamples";
import { Dropdown, type DropdownOption } from "../Dropdown";
import { voiceKey, type SpeechEngine } from "./useVoicePreview";

/**
 * The Agents concrete-voice picker: the shared `Dropdown`, grouped
 * Female / Male, with an inline play/pause control on every option row.
 *
 * Why this is a composition and NOT an extension of `Dropdown`: `Dropdown`
 * already ships `renderOptionAction`, an opt-in per-row slot whose node is
 * rendered in an absolutely-positioned right cell OUTSIDE the option
 * `<button>` (button-in-button is invalid HTML and the classic mis-click
 * trap). `Enhancement.tsx`'s local-model picker is the existing consumer.
 * Nothing about "a play button on each row" needs a new capability, so the
 * shared component is untouched and no other consumer can regress.
 *
 * The two states on a row stay independently coded, exactly as the deleted
 * `VoiceList` had them: selection is the option button (Dropdown's own
 * `aria-selected` + accent rail), playback is the transport disc. Clicking
 * the disc auditions without selecting or closing; clicking the name selects
 * and closes. `useVoicePreview` is the PLAYER and is passed straight through
 * unchanged, so a second voice pauses the first and `ended` returns the disc
 * to play.
 *
 * Deliberately knows nothing about install state, `config`, or `SpeechStatus`
 * so PR 2's voice-pick step can embed it verbatim.
 */

export interface VoiceItem {
  id: string | number;
  label: string;
  group: "female" | "male";
  reference_label?: string;
}

/**
 * The disc sits inside the Dropdown panel, whose fill is `--surface-overlay`
 * (white at 0.95 in Light). A `--surface-control` fill ringed with
 * `--border-hairline` is white-on-white there and disappears, which is the
 * defect the deleted well hit first. Light gets its structure from
 * steel-tinted lines: steel resolves dark-on-light in Light and light-on-dark
 * in Dark, so one value reads against the panel in both themes.
 */
const DISC_RING = "color-mix(in srgb, var(--color-steel) 40%, transparent)";
const DISC_RING_HOVER = "color-mix(in srgb, var(--color-steel) 65%, transparent)";

/**
 * The FEMALE->MALE separator's hairline. `Dropdown`'s default `--divider` is
 * white at low alpha and near-invisible on the `--surface-overlay` panel in
 * Light (the same defect the disc ring sidesteps), so this split uses the
 * steel-tint HAIRLINE, which resolves against the panel in both themes. The
 * muted group heading carries the split too; this makes the boundary crisp.
 */
const GROUP_DIVIDER = "color-mix(in srgb, var(--color-steel) 22%, transparent)";

/**
 * How tall the open panel is allowed to get.
 *
 * `Dropdown` already clamps to the room between the trigger and the window
 * edge, so an uncapped voice panel was never *outside* the window. It was
 * worse than that: with 9 (Kokoro) or 10 (Supertonic) voices it took every
 * pixel it was offered, ending 8px off the window bottom and ~60px BELOW the
 * modal that owns it. A panel running from a control inside a dialog to the
 * bottom edge of the app window does not read as "a list that scrolls", it
 * reads as a surface that escaped its dialog and got cut by the window. So the
 * panel is capped well short of the room it could take, and `Dropdown`'s own
 * clamp still applies on top (the cap only ever makes it shorter).
 *
 * The cap is COMPUTED from the roster rather than fixed, for the same reason
 * the deleted `VoiceList` computed `WELL_ROW_OFFSET`: the fold has to land in
 * the middle of a row. A cut on a clean row boundary reads as the end of the
 * list, and this panel has no scrollbar gutter to say otherwise. A single
 * fixed height cannot do that for both engines, because their group runs
 * differ (Supertonic is 5 female + 5 male, Kokoro 6 + 3), so the same pixel
 * lands mid-row for one and inside the "MALE" heading for the other. Walking
 * the actual runs picks the tallest allowed height whose fold is mid-row for
 * whatever roster is on screen, including future ones.
 *
 * `PANEL_ROW_HEIGHT`: `px-4 py-2` (16px) around a 13px `leading-tight` label.
 * `PANEL_GROUP_HEADING_HEIGHT`: `px-4 pt-2 pb-1` (12px) around an 11px label.
 * `PANEL_PAD` / `PANEL_BORDERS`: the panel's `py-1.5` and its 1px hairline.
 * All are verified against the live panel in the QA harness at 957x680.
 */
const PANEL_ROW_HEIGHT = 32.25;
const PANEL_GROUP_HEADING_HEIGHT = 28.5;
const PANEL_PAD = 6;
const PANEL_BORDERS = 2;
/**
 * Roughly seven rows. Sized so the panel opened from the Voice row of the
 * Agents modal at the app's fixed 957x680 stays inside that modal, which
 * is the containment the design wants and a much stronger guarantee than
 * merely fitting the window.
 */
const PANEL_HEIGHT_CEILING = 250;

/**
 * The largest panel height at or below the ceiling whose bottom edge falls
 * mid-row. `groupSizes` is the option count per contiguous group run, in
 * render order. A roster that fits entirely gets its natural height, so a
 * short list is never given a pointless scrollbar.
 */
export function voicePanelMaxHeight(groupSizes: number[]): number {
  const content = groupSizes.reduce(
    (total, size) => total + PANEL_GROUP_HEADING_HEIGHT + size * PANEL_ROW_HEIGHT,
    0,
  );
  const natural = Math.ceil(PANEL_PAD * 2 + content + PANEL_BORDERS);
  if (natural <= PANEL_HEIGHT_CEILING) return natural;

  // Offsets are measured from the panel's padding-box top, which is what its
  // `max-height` minus its two borders actually exposes.
  let offset = PANEL_PAD;
  let best = PANEL_HEIGHT_CEILING;
  for (const size of groupSizes) {
    offset += PANEL_GROUP_HEADING_HEIGHT;
    for (let row = 0; row < size; row += 1) {
      const height = Math.round(
        offset + PANEL_ROW_HEIGHT / 2 + PANEL_BORDERS,
      );
      if (height <= PANEL_HEIGHT_CEILING) best = height;
      offset += PANEL_ROW_HEIGHT;
    }
  }
  return best;
}

/** 12px inside the 24px frame; `WaveformPlayer` uses 14 inside 28. */
function PlayIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
      <path d="M4.5 3.2v9.6c0 .6.66.96 1.17.63l7.2-4.8a.75.75 0 000-1.26l-7.2-4.8A.75.75 0 004.5 3.2z" />
    </svg>
  );
}

function PauseIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
      <rect x="4" y="3" width="3" height="10" rx="1" />
      <rect x="9" y="3" width="3" height="10" rx="1" />
    </svg>
  );
}

/**
 * Locked copy 27. Supertonic carries `reference_label` ("Voice 3"), which is
 * what the transport announces there; Kokoro has none and announces its name.
 * Always visible (never hover-revealed): auditioning IS the point of this
 * picker, unlike `Enhancement.tsx`'s hover-revealed download action.
 */
function VoiceTransport({
  engine,
  voice,
  playing,
  onTogglePlay,
}: {
  engine: SpeechEngine;
  voice: VoiceItem;
  playing: boolean;
  onTogglePlay: (engine: SpeechEngine, id: string | number) => void;
}) {
  const transportName = voice.reference_label ?? voice.label;
  if (!hasVoiceSample(engine, voice.id)) return null;
  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        onTogglePlay(engine, voice.id);
      }}
      aria-label={`${playing ? "Pause" : "Play"} ${transportName} sample`}
      className={`flex h-6 w-6 shrink-0 cursor-pointer items-center justify-center rounded-full transition-colors duration-150 ${
        playing
          ? ""
          : "border border-[var(--voice-disc-ring)] bg-[var(--surface-control)] text-steel hover:border-[var(--voice-disc-ring-hover)] hover:bg-[var(--surface-hover)] hover:text-slate"
      }`}
      style={
        playing
          ? {
              background: "var(--accent-fill)",
              color: "#fff",
              boxShadow: "var(--shadow-frost-glow)",
            }
          : ({
              "--voice-disc-ring": DISC_RING,
              "--voice-disc-ring-hover": DISC_RING_HOVER,
            } as React.CSSProperties)
      }
    >
      {playing ? <PauseIcon /> : <PlayIcon />}
    </button>
  );
}

export interface VoicePickerProps {
  engine: SpeechEngine;
  voices: VoiceItem[];
  loading: boolean;
  error: boolean;
  selectedId: string | number | null | undefined;
  onSelect: (id: string | number) => void;
  /** `useVoicePreview`'s `playingKey`, i.e. `` `${engine}/${id}` `` or null. */
  playingKey: string | null;
  /** `useVoicePreview`'s `toggle`, passed straight through. */
  onTogglePlay: (engine: SpeechEngine, id: string | number) => void;
  triggerClassName?: string;
  panelWidth?: number;
  /** Opt-in resting border color for the collapsed trigger, passed straight to
   *  the shared `Dropdown`. Undefined by default, so Configure's picker keeps
   *  the `--border-hairline` default byte-for-byte; the setup flow passes a
   *  steel-tint value so the trigger reads as a control on `--surface-modal`. */
  triggerBorderColor?: string;
  /** Opt-out for the collapsed-trigger "selected voice" disc below. Default
   *  `true`, so shared surfaces keep the selected-voice preview unless they
   *  explicitly provide another audition control. */
  showSelectedTransport?: boolean;
}

export function VoicePicker({
  engine,
  voices,
  loading,
  error,
  selectedId,
  onSelect,
  playingKey,
  onTogglePlay,
  triggerClassName = "w-[190px]",
  panelWidth = 240,
  triggerBorderColor,
  showSelectedTransport = true,
}: VoicePickerProps) {
  // Female first, then male, matching the deleted well's render order. The
  // group heading is the shared `Dropdown`'s own labelled `role="group"`
  // section (locked copy 26; the heading class already uppercases).
  const ordered = [
    ...voices.filter((voice) => voice.group === "female"),
    ...voices.filter((voice) => voice.group !== "female"),
  ];
  const options: DropdownOption<string | number>[] = ordered.map((voice, index) => ({
    value: voice.id,
    label: voice.label,
    groupLabel: voice.group === "female" ? "FEMALE" : "MALE",
    // Draw a hairline before the first Male row (the female->male boundary in
    // `ordered`). The muted group heading below is the primary separator; this
    // is the secondary cue. See the render note on the shared `--divider`.
    dividerBefore:
      voice.group !== "female" && ordered[index - 1]?.group === "female",
  }));
  const byId = new Map(ordered.map((voice) => [voice.id, voice]));
  const selectedVoice = byId.get(selectedId ?? "");
  // Female then male is exactly two runs, and an engine with only one gender
  // collapses to one, so the group sizes come straight off the split above.
  const groupSizes = [
    ordered.filter((voice) => voice.group === "female").length,
    ordered.filter((voice) => voice.group !== "female").length,
  ].filter((size) => size > 0);

  return (
    <div
      className="flex items-center gap-2"
      data-ui-qa-voice-picker="true"
      aria-busy={loading || undefined}
    >
      <Dropdown<string | number>
        value={selectedId ?? ""}
        options={options}
        onChange={onSelect}
        // Mute FEMALE/MALE to the tertiary rung so the heading reads as a
        // caption/separator, not a selectable row. Opt-in: Enhancement's
        // grouped picker keeps the default `text-steel`.
        groupHeadingClassName="text-steel/60"
        // Steel-tint the female->male hairline so it is perceptible on the
        // panel in Light. Opt-in: Enhancement's divider keeps `--divider`.
        dividerColor={GROUP_DIVIDER}
        // Locked copy 29 kept "Voice, loading" as the busy accessible name.
        // The radiogroup it used to sit on is gone with the well, so the
        // string moves to the control that replaced it.
        ariaLabel={loading ? "Voice, loading" : "Voice"}
        // Locked copy 30 for the failure, and deliberately NO text while
        // loading (locked copy 29 is "no text").
        placeholder={error ? "Could not load voices." : ""}
        disabled={loading || options.length === 0}
        triggerClassName={triggerClassName}
        triggerBorderColor={triggerBorderColor}
        panelWidth={panelWidth}
        panelMaxHeight={voicePanelMaxHeight(groupSizes)}
        renderOptionAction={(option) => {
          const voice = byId.get(option.value);
          if (!voice) return null;
          return (
            <VoiceTransport
              engine={engine}
              voice={voice}
              playing={playingKey === voiceKey(engine, voice.id)}
              onTogglePlay={onTogglePlay}
            />
          );
        }}
      />
      {/* Audition the SELECTED voice without opening the panel. Same disc, same
          `useVoicePreview` player as every row, so a preview started here is
          paused by a row press and vice versa. Absent until a roster loads and
          a voice is selected (so it never shows while loading or on error).
          Opt-out via `showSelectedTransport={false}` (default true) for
          surfaces that provide their own preview affordance. */}
      {showSelectedTransport && selectedVoice && (
        <VoiceTransport
          engine={engine}
          voice={selectedVoice}
          playing={playingKey === voiceKey(engine, selectedVoice.id)}
          onTogglePlay={onTogglePlay}
        />
      )}
    </div>
  );
}
