/**
 * Vitest coverage for the Home model row's Manual picker (Task 3.2).
 *
 * Locks the behavior the locked design and copy require of the picker itself,
 * independently of Home's routing:
 *   - only the two user-facing models are ever listed;
 *   - the selected option carries a non-action check announced as
 *     `Current model: {model}`;
 *   - a downloaded inactive option carries a `Delete {model}` button that does
 *     NOT also select the row;
 *   - an absent option carries a `Download {model}` glyph and selecting the row
 *     is what starts the download;
 *   - a protected (configured / loaded) artifact is never deletable;
 *   - the trailing actions stay reachable by keyboard while the panel is open.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup, fireEvent, within } from "@testing-library/react";
import { SttModelPicker } from "../SttModelPicker";
import type { SttModelPickerOption } from "../SttModelPicker";
import type { SttBackend } from "../../lib/languages";

afterEach(() => {
  cleanup();
});

const PARAKEET_SELECTED: SttModelPickerOption = {
  backend: "parakeet",
  state: "selected",
};

function renderPicker(
  options: SttModelPickerOption[],
  overrides: {
    value?: SttBackend;
    onChange?: (backend: SttBackend) => void;
    onRequestDelete?: (backend: SttBackend) => void;
    disabled?: boolean;
  } = {},
) {
  const onChange = overrides.onChange ?? vi.fn();
  const onRequestDelete = overrides.onRequestDelete ?? vi.fn();
  const result = render(
    <SttModelPicker
      value={overrides.value ?? "parakeet"}
      options={options}
      onChange={onChange}
      onRequestDelete={onRequestDelete}
      disabled={overrides.disabled}
    />,
  );
  return { ...result, onChange, onRequestDelete };
}

function openPanel() {
  fireEvent.click(screen.getByRole("button", { name: /Voice recognition model/ }));
  return screen.getByRole("listbox");
}

describe("SttModelPicker options", () => {
  it("lists the compatible user-facing models in routing order", () => {
    renderPicker([
      PARAKEET_SELECTED,
      { backend: "qwen", state: "absent" },
      { backend: "cohere", state: "absent" },
    ]);
    const panel = openPanel();

    const optionNames = within(panel)
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(optionNames).toHaveLength(3);
    expect(optionNames[0]).toContain("NVIDIA Parakeet");
    expect(optionNames[1]).toContain("Qwen3 ASR");
    expect(optionNames[2]).toContain("Cohere Transcribe");
    // Parakeet's v2/v3 split never surfaces here.
    expect(panel.textContent).not.toMatch(/v2|v3|Multilingual|English-only/);
  });

  it("announces the selected option and gives it no action", () => {
    renderPicker([PARAKEET_SELECTED, { backend: "qwen", state: "absent" }]);
    const panel = openPanel();

    expect(
      within(panel).getByText("Current model: NVIDIA Parakeet"),
    ).toBeInTheDocument();
    expect(
      within(panel).queryByRole("button", { name: "Delete NVIDIA Parakeet" }),
    ).toBeNull();
  });

  // A visually separate sibling of the row is announced only while browsing
  // the panel, and never when the option takes focus, which is how a keyboard
  // user meets it. The sentence has to be BOUND to the option.
  it("binds the current-model sentence to the selected option itself", () => {
    renderPicker([PARAKEET_SELECTED, { backend: "qwen", state: "absent" }]);
    const panel = openPanel();

    const selected = within(panel).getByRole("option", {
      name: /NVIDIA Parakeet/,
    });
    expect(selected).toHaveAttribute("aria-selected", "true");
    expect(selected).toHaveAccessibleDescription(
      "Current model: NVIDIA Parakeet",
    );
  });

  it("keeps the sentence out of the option's name and off unselected rows", () => {
    renderPicker([PARAKEET_SELECTED, { backend: "qwen", state: "absent" }]);
    const panel = openPanel();

    // Describing, not renaming: the row is still called by the model name.
    expect(
      within(panel).getByRole("option", { name: /NVIDIA Parakeet.*Fastest/ }),
    ).toBeInTheDocument();
    expect(
      within(panel).getByRole("option", { name: /Qwen3 ASR/ }),
    ).not.toHaveAccessibleDescription("Current model: Qwen3 ASR");
  });

  it("selects an absent option to start its download", () => {
    const { onChange } = renderPicker([
      PARAKEET_SELECTED,
      { backend: "qwen", state: "absent" },
    ]);
    const panel = openPanel();

    const option = within(panel).getByRole("option", { name: "Qwen3 ASR" });
    expect(option).toHaveAccessibleDescription("Download Qwen3 ASR");
    // The visible tooltip is attached to the icon, but the row keeps one clean
    // accessible name and remains the pointer/keyboard action.
    const tooltip = panel.querySelector<HTMLElement>('[role="tooltip"]');
    expect(tooltip).not.toBeNull();
    expect(tooltip).toHaveTextContent("Download Qwen3 ASR");
    fireEvent.mouseEnter(tooltip!.parentElement!);
    expect(tooltip).toBeInTheDocument();
    expect(
      within(panel).queryByRole("option", {
        name: "Qwen3 ASR Download Qwen3 ASR",
      }),
    ).toBeNull();

    fireEvent.click(option);
    expect(onChange).toHaveBeenCalledWith("qwen");
  });

  it("deletes a downloaded inactive option without selecting it", () => {
    const { onChange, onRequestDelete } = renderPicker([
      PARAKEET_SELECTED,
      { backend: "qwen", state: "downloaded", deletable: true },
    ]);
    const panel = openPanel();

    const trash = within(panel).getByRole("button", { name: "Delete Qwen3 ASR" });
    fireEvent.click(trash);

    expect(onRequestDelete).toHaveBeenCalledWith("qwen");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("never offers delete for a protected artifact", () => {
    renderPicker([
      PARAKEET_SELECTED,
      { backend: "qwen", state: "downloaded", deletable: false },
    ]);
    const panel = openPanel();

    expect(
      within(panel).queryByRole("button", { name: "Delete Qwen3 ASR" }),
    ).toBeNull();
    expect(within(panel).queryByText("Download Qwen3 ASR")).toBeNull();
  });

  it("keeps the trailing action keyboard reachable while the panel is open", () => {
    renderPicker([
      PARAKEET_SELECTED,
      { backend: "qwen", state: "downloaded", deletable: true },
    ]);
    const panel = openPanel();

    // Tabbing off an option must not tear the panel down, or the action that
    // follows it in DOM order could never receive focus.
    fireEvent.keyDown(within(panel).getByRole("option", { name: /Qwen3 ASR/ }), {
      key: "Tab",
    });
    expect(screen.queryByRole("listbox")).not.toBeNull();

    const trash = screen.getByRole("button", { name: "Delete Qwen3 ASR" });
    trash.focus();
    expect(trash).toHaveFocus();
  });

  it("does not open while a model operation is running", () => {
    renderPicker([PARAKEET_SELECTED, { backend: "qwen", state: "absent" }], {
      disabled: true,
    });

    fireEvent.click(
      screen.getByRole("button", { name: /Voice recognition model/ }),
    );
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});
