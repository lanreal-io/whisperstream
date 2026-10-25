import { hasVoiceSample } from "./voiceSamples";
vi.mock("./voiceSamples", () => ({ hasVoiceSample: vi.fn(() => true) }));
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  VoicePicker,
  voicePanelMaxHeight,
  type VoiceItem,
  type VoicePickerProps,
} from "./VoicePicker";

/**
 * These assertions are the exact regressions the design warns about, carried
 * over from the deleted `VoiceList` so the reversal to a `Dropdown` cannot
 * quietly drop them:
 *
 * - the transport button and the selection target must be SIBLINGS. Nesting
 *   either inside the other is invalid HTML and the classic mis-click trap.
 * - selecting never plays and playing never selects.
 * - playing must not close the picker; selecting must.
 * - Supertonic rows must announce their `reference_label`, or every row reads
 *   identically to a screen reader.
 */

const KOKORO_VOICES: VoiceItem[] = [
  { id: "af_heart", label: "Heart", group: "female" },
  { id: "af_bella", label: "Bella", group: "female" },
  { id: "am_puck", label: "Puck", group: "male" },
];

const SUPERTONIC_VOICES: VoiceItem[] = [
  { id: 1, label: "Brianna", group: "female", reference_label: "Voice 1" },
  { id: 3, label: "Danielle", group: "female", reference_label: "Voice 3" },
  { id: 6, label: "George", group: "male", reference_label: "Voice 6" },
];

function renderPicker(overrides: Partial<VoicePickerProps> = {}) {
  const props: VoicePickerProps = {
    engine: "kokoro",
    voices: KOKORO_VOICES,
    loading: false,
    error: false,
    selectedId: "af_heart",
    onSelect: vi.fn(),
    playingKey: null,
    onTogglePlay: vi.fn(),
    ...overrides,
  };
  render(<VoicePicker {...props} />);
  return props;
}

function open(name = "Voice: Heart") {
  fireEvent.click(screen.getByRole("button", { name }));
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.mocked(hasVoiceSample).mockReturnValue(true);
});

describe("VoicePicker", () => {
  it("rests collapsed on the selected voice and reveals the grouped options", () => {
    renderPicker();

    // Closed: one trigger, no option rows at all.
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    open();

    const list = screen.getByRole("listbox", { name: "Voice" });
    expect(within(list).getByText("FEMALE")).toBeInTheDocument();
    expect(within(list).getByText("MALE")).toBeInTheDocument();
    expect(within(list).getAllByRole("group")).toHaveLength(2);
    expect(
      within(list)
        .getAllByRole("option")
        .map((row) => row.textContent),
    ).toEqual(["Heart", "Bella", "Puck"]);
    expect(
      within(list).getByRole("option", { name: "Heart" }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("keeps the transport and selection buttons as siblings, never nested", () => {
    renderPicker();
    open();

    const play = screen.getByRole("button", { name: "Play Bella sample" });
    const select = screen.getByRole("option", { name: "Bella" });

    expect(play.contains(select)).toBe(false);
    expect(select.contains(play)).toBe(false);
    expect(play.querySelector("button")).toBeNull();
    expect(select.querySelector("button")).toBeNull();
    // Same row wrapper, different children.
    expect(play.closest("div")?.parentElement).toBe(select.parentElement);
  });

  it("plays without selecting or closing when the transport is clicked", () => {
    const props = renderPicker();
    open();

    fireEvent.click(screen.getByRole("button", { name: "Play Bella sample" }));

    expect(props.onTogglePlay).toHaveBeenCalledWith("kokoro", "af_bella");
    expect(props.onSelect).not.toHaveBeenCalled();
    // Auditioning must not collapse the picker, or comparing voices is
    // one-open-per-voice.
    expect(screen.getByRole("listbox", { name: "Voice" })).toBeInTheDocument();
  });

  it("selects and closes without playing when the voice name is clicked", async () => {
    const props = renderPicker();
    open();

    fireEvent.click(screen.getByRole("option", { name: "Bella" }));

    expect(props.onSelect).toHaveBeenCalledWith("af_bella");
    expect(props.onTogglePlay).not.toHaveBeenCalled();
    // The panel leaves through AnimatePresence, so the close is asserted
    // after its exit rather than synchronously.
    await waitFor(() =>
      expect(screen.queryAllByRole("option")).toHaveLength(0),
    );
  });

  it("reads selection and playback as independent states on one row", () => {
    renderPicker({ selectedId: "af_bella", playingKey: "kokoro/af_bella" });
    open("Voice: Bella");
    // Scope row assertions to the open panel: the collapsed-trigger disc (a
    // sibling outside the listbox) also carries the selected voice's transport
    // label, so an unscoped query would now match two buttons.
    const list = screen.getByRole("listbox", { name: "Voice" });

    expect(
      within(list).getByRole("option", { name: "Bella" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      within(list).getByRole("option", { name: "Heart" }),
    ).toHaveAttribute("aria-selected", "false");
    // The playing row offers Pause; every other row still offers Play.
    expect(
      within(list).getByRole("button", { name: "Pause Bella sample" }),
    ).toBeInTheDocument();
    expect(
      within(list).getByRole("button", { name: "Play Heart sample" }),
    ).toBeInTheDocument();
  });

  it("previews the selected voice from a disc beside the collapsed trigger", () => {
    const props = renderPicker({ selectedId: "af_bella", playingKey: null });

    // No panel is open, so this is the collapsed-trigger disc, not a row.
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Play Bella sample" }));

    // Same player as the rows; auditioning here never selects or opens.
    expect(props.onTogglePlay).toHaveBeenCalledWith("kokoro", "af_bella");
    expect(props.onSelect).not.toHaveBeenCalled();
    expect(screen.queryAllByRole("option")).toHaveLength(0);
  });

  it("hides the collapsed-trigger disc when showSelectedTransport is false", () => {
    renderPicker({
      selectedId: "af_bella",
      playingKey: null,
      showSelectedTransport: false,
    });

    // No collapsed disc while the panel is closed.
    expect(
      screen.queryByRole("button", { name: "Play Bella sample" }),
    ).not.toBeInTheDocument();

    // The per-row disc inside the open panel is unaffected.
    open("Voice: Bella");
    const list = screen.getByRole("listbox", { name: "Voice" });
    expect(
      within(list).getByRole("button", { name: "Play Bella sample" }),
    ).toBeInTheDocument();
  });

  it("names Supertonic transports by reference_label so rows differ", () => {
    renderPicker({
      engine: "supertonic",
      voices: SUPERTONIC_VOICES,
      selectedId: 1,
    });
    open("Voice: Brianna");
    // Row-scoped: the collapsed disc also announces the selected voice's
    // reference_label, so an unscoped query would double-match Voice 1.
    const list = screen.getByRole("listbox", { name: "Voice" });

    expect(
      within(list).getByRole("button", { name: "Play Voice 1 sample" }),
    ).toBeInTheDocument();
    expect(
      within(list).getByRole("button", { name: "Play Voice 3 sample" }),
    ).toBeInTheDocument();
    expect(
      within(list).getByRole("button", { name: "Play Voice 6 sample" }),
    ).toBeInTheDocument();
    // The selection half still announces the visible name only (locked copy 28).
    expect(
      within(list).getByRole("option", { name: "Brianna" }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("announces the picker as busy and refuses to open while loading", () => {
    renderPicker({ voices: [], loading: true, selectedId: null });

    expect(
      document.querySelector('[data-ui-qa-voice-picker]'),
    ).toHaveAttribute("aria-busy", "true");
    const trigger = screen.getByRole("button", { name: /^Voice, loading/ });
    expect(trigger).toBeDisabled();
    fireEvent.click(trigger);
    expect(screen.queryAllByRole("option")).toHaveLength(0);
  });

  it("shows the locked failure line on the trigger instead of an empty list", () => {
    renderPicker({ voices: [], error: true, selectedId: null });

    expect(screen.getByText("Could not load voices.")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Voice: Could not load voices." }),
    ).toBeDisabled();
  });

  /**
   * jsdom has no layout, so the panel's containment is asserted on the
   * arithmetic that produces it. The measured metrics it uses are verified
   * against the live panel by the `settings.agent-speak-voice-picker-open` QA
   * state; this pins the two properties that arithmetic exists for.
   */
  it("caps the panel below the ceiling and folds mid-row for both rosters", () => {
    const ROW = 32.25;
    const HEADING = 28.5;
    const PAD = 6;
    const BORDERS = 2;
    const CEILING = 250;

    const foldFraction = (groupSizes: number[]) => {
      const fold = voicePanelMaxHeight(groupSizes) - BORDERS - PAD;
      let offset = 0;
      for (const size of groupSizes) {
        offset += HEADING;
        for (let row = 0; row < size; row += 1) {
          if (fold > offset && fold < offset + ROW) {
            return (fold - offset) / ROW;
          }
          offset += ROW;
        }
      }
      return null;
    };

    for (const groupSizes of [[5, 5], [6, 3], [10], [1, 9]]) {
      const height = voicePanelMaxHeight(groupSizes);
      expect(height).toBeLessThanOrEqual(CEILING);
      // Not on a row boundary and not inside a group heading: the partially
      // visible row IS the scroll affordance.
      const fraction = foldFraction(groupSizes);
      expect(fraction).not.toBeNull();
      expect(fraction).toBeGreaterThan(0.2);
      expect(fraction).toBeLessThan(0.8);
    }

    // A roster short enough to fit gets its natural height, never a cap that
    // would invent a scrollbar for a complete list.
    expect(voicePanelMaxHeight([2, 1])).toBeLessThan(CEILING);
    expect(voicePanelMaxHeight([2, 1])).toBe(
      Math.ceil(PAD * 2 + BORDERS + HEADING * 2 + ROW * 3),
    );
  });
});


describe("VoicePicker sample availability", () => {
  it("keeps selection available with no bundled samples", () => {
    vi.mocked(hasVoiceSample).mockReturnValue(false);
    const props = renderPicker();
    open();
    expect(screen.queryByRole("button", { name: /sample$/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: "Bella" }));
    expect(props.onSelect).toHaveBeenCalledWith("af_bella");
    expect(props.onTogglePlay).not.toHaveBeenCalled();
  });
  it("offers transport only for available samples in a partial set", () => {
    vi.mocked(hasVoiceSample).mockImplementation((_engine, id) => id === "af_bella");
    const props = renderPicker();
    expect(screen.queryByRole("button", { name: "Play Heart sample" })).not.toBeInTheDocument();
    open();
    expect(screen.getByRole("button", { name: "Play Bella sample" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Play Puck sample" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Play Bella sample" }));
    expect(props.onTogglePlay).toHaveBeenCalledWith("kokoro", "af_bella");
    expect(props.onSelect).not.toHaveBeenCalled();
  });
});
