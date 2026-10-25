import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AppConfig } from "../lib/api/config";
import { Controls } from "./Controls";

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
});

function makeConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    show_recording_indicator: true,
    show_recording_control: false,
    show_live_preview: false,
    push_to_talk_shortcut: "right shift",
    toggle_shortcut: "",
    paste_last_shortcut: "",
    paste_last_original_shortcut: "",
    input_style: "clipboard",
    input_style_user_set: true,
    screen_reader_detected: false,
    keystroke_delay_ms: 0,
    ...overrides,
  } as AppConfig;
}

function renderControls(
  config: AppConfig,
  onConfigChange = vi.fn<(patch: Partial<AppConfig>) => void>(),
) {
  render(
    <Controls
      config={config}
      onConfigChange={onConfigChange}
      send={() => true}
      subscribe={() => () => {}}
      connected
      onShowStatus={() => {}}
    />,
  );
  return onConfigChange;
}

describe("settings grouping", () => {
  it("groups the indicator and hotkey rows under named sections", () => {
    renderControls(makeConfig());

    const groups = screen.getAllByRole("region");
    expect(groups).toHaveLength(2);
    expect(groups[0]).toHaveAccessibleName("Recording Indicator");
    expect(groups[1]).toHaveAccessibleName("Hotkeys");

    // The heading names the feature, the row names its switch: neither group
    // repeats a row title (design.md D4).
    expect(
      screen.getByRole("heading", { name: "Recording Indicator", level: 3 }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "Recording Indicator" })).toBeNull();
  });

  it("puts the indicator rows inside group one and the shortcut rows inside group two", () => {
    renderControls(makeConfig());

    const [indicatorGroup, hotkeyGroup] = screen.getAllByRole("region");
    expect(indicatorGroup).toContainElement(screen.getByText("Show indicator"));
    expect(indicatorGroup).toContainElement(screen.getByText("Live Preview"));
    expect(hotkeyGroup).toContainElement(screen.getByText("Push to Talk"));
    expect(hotkeyGroup).toContainElement(
      screen.getByText("Paste Last Transcript (Original)"),
    );

    // Input Style stays ungrouped below the hairline (D3: a heading is earned
    // by two or more visible rows).
    expect(indicatorGroup).not.toContainElement(screen.getByText("Input Style"));
    expect(hotkeyGroup).not.toContainElement(screen.getByText("Input Style"));
  });

  it("draws exactly one hairline, at the unheaded boundary", () => {
    renderControls(makeConfig());

    // Rows inside a group are joined by whitespace; the only --divider left is
    // the Hotkeys-group to Input-Style boundary, which has no heading.
    expect(
      document.body.querySelectorAll('[style*="--divider"]'),
    ).toHaveLength(1);
  });
});

describe("persistent recording control", () => {
  it("renders the production setting and preserves its stored value", () => {
    const onConfigChange = renderControls(
      makeConfig({ show_recording_control: true }),
    );

    expect(screen.getByText("Keep control visible")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Show a compact control on your desktop between recordings",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: "Keep control visible" }),
    ).toBeChecked();
    expect(onConfigChange).not.toHaveBeenCalled();
  });

  it("persists changes without a development flag", () => {
    const onConfigChange = renderControls(makeConfig());

    fireEvent.click(
      screen.getByRole("switch", { name: "Keep control visible" }),
    );

    expect(onConfigChange).toHaveBeenCalledWith({
      show_recording_control: true,
    });
  });

  it("keeps the preference visible but disabled behind the indicator master", () => {
    renderControls(
      makeConfig({
        show_recording_indicator: false,
        show_recording_control: true,
      }),
    );

    expect(
      screen.getByRole("switch", { name: "Keep control visible" }),
    ).toBeChecked();
    expect(
      screen.getByRole("switch", { name: "Keep control visible" }),
    ).toBeDisabled();
  });
});

describe("Live Preview control", () => {
  it("renders below the persistent control with its locked copy and accessible name", () => {
    renderControls(makeConfig());

    const indicatorTitle = screen.getByText("Show indicator");
    const controlTitle = screen.getByText("Keep control visible");
    const previewTitle = screen.getByText("Live Preview");

    const indicatorSection =
      indicatorTitle.parentElement?.parentElement?.parentElement;
    const controlSection =
      controlTitle.parentElement?.parentElement?.parentElement;
    const previewSection = previewTitle.parentElement?.parentElement?.parentElement;
    expect(indicatorSection?.nextElementSibling).toBe(controlSection);
    expect(controlSection?.nextElementSibling).toBe(previewSection);
    expect(
      screen.getByText(
        "Preview raw dictation in the overlay as you speak",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: "Live Preview" }),
    ).not.toBeChecked();
  });

  it("persists changes while Recording Indicator is enabled", () => {
    const onConfigChange = renderControls(makeConfig());

    fireEvent.click(screen.getByRole("switch", { name: "Live Preview" }));

    expect(onConfigChange).toHaveBeenCalledWith({ show_live_preview: true });
  });

  it("preserves the child value but blocks mouse and keyboard activation while the master is off", () => {
    const onConfigChange = renderControls(
      makeConfig({ show_recording_indicator: false, show_live_preview: true }),
    );
    const preview = screen.getByRole("switch", { name: "Live Preview" });

    expect(preview).toBeChecked();
    expect(preview).toBeDisabled();
    expect(preview).toHaveAttribute("aria-disabled", "true");

    fireEvent.click(preview);
    fireEvent.keyDown(preview, { key: " ", code: "Space" });
    fireEvent.keyDown(preview, { key: "Enter", code: "Enter" });

    expect(onConfigChange).not.toHaveBeenCalled();
  });
});

describe("Input Style compatibility copy", () => {
  it("uses the approved conditional keystroke hint", () => {
    renderControls(makeConfig());

    expect(
      screen.getByRole("tooltip", {
        name: "Can help when pasting is blocked.",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/RDP|Citrix/)).toBeNull();
  });
});
