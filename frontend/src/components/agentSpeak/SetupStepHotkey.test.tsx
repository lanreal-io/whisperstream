import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AppConfig } from "../../lib/api/config";
import { SetupStepHotkey, type SetupStepHotkeyProps } from "./SetupStepHotkey";

const LEDE =
  "The HUD shows short summaries. Use this shortcut to open the full reply.";
const NO_GESTURE =
  "No dictation shortcut is set, so you cannot reply by voice yet.";

function makeConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    claude_hud_enabled: true,
    agent_speak_setup_completed: false,
    claude_view_shortcut: "ctrl+alt+`",
    primary_shortcut: "right shift",
    push_to_talk_shortcut: "right shift",
    toggle_shortcut: "ctrl+alt+d",
    paste_last_shortcut: "",
    paste_last_original_shortcut: "",
    profile_hotkeys: {},
    ...overrides,
  } as AppConfig;
}

function renderStep(overrides: Partial<SetupStepHotkeyProps> = {}) {
  const props: SetupStepHotkeyProps = {
    config: makeConfig(),
    onCanAdvanceChange: vi.fn(),
    onChildDialogOpenChange: vi.fn(),
    onConfigChange: vi.fn(),
    connected: true,
    send: vi.fn(() => true),
    subscribe: vi.fn(() => () => {}),
    ...overrides,
  };
  return { ...render(<SetupStepHotkey {...props} />), props };
}

function statusLine(): HTMLElement | null {
  return document.querySelector<HTMLElement>("p.min-h-\\[14px\\]");
}

function gestureSentence(): HTMLElement | null {
  return document.querySelector<HTMLElement>("[data-setup-gesture-sentence]");
}

/** The capture's own button, identified the way the user sees it: its value. */
function captureButton(): HTMLElement {
  return screen.getByText("Ctrl+Alt+`");
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SetupStepHotkey", () => {
  it("renders the step heading and lede over one chrome-less capture", () => {
    renderStep();
    expect(
      screen.getByRole("heading", { name: "Set your shortcut", level: 2 }),
    ).toBeInTheDocument();
    expect(screen.getByText(LEDE)).toBeInTheDocument();
    expect(captureButton()).toBeInTheDocument();
  });

  it("renders exactly one ShortcutCapture, with no label row beside it", () => {
    renderStep();
    // `ShortcutCapture`'s own wrapper. Exactly one on the step, and the step
    // renders no `View full response` label to its left: the heading and the
    // lede do that job and the capture self-labels.
    const captures = document.querySelectorAll(
      "div.inline-flex.flex-col.items-stretch",
    );
    expect(captures).toHaveLength(1);
    expect(screen.queryByText("View full response")).toBeNull();
  });

  it("captures claude_view only, with the shared capture protocol", () => {
    const { props } = renderStep();
    fireEvent.click(captureButton());
    expect(props.send).toHaveBeenCalledWith({
      type: "shortcut.capture.start",
      target: "claude_view",
    });
  });

  it("renders the two reply gestures as ONE sentence with inline key chips", () => {
    renderStep();
    const sentence = gestureSentence();
    expect(sentence).not.toBeNull();
    expect(sentence!.textContent).toBe(
      "To reply, hold Right Shift or tap Ctrl+Alt+D. Agents adds no new shortcut.",
    );
    // A sentence, not two rows and not a card: no control of any kind inside.
    expect(sentence!.querySelector("button")).toBeNull();
    expect(sentence!.querySelector("input")).toBeNull();
    // Two chip groups, one per gesture.
    expect(sentence!.querySelectorAll("[data-setup-key]").length).toBe(4);
    // The chips read as part of the sentence: no per-token label, and the
    // separators are hidden so it does not read "Ctrl plus Alt plus".
    for (const chip of sentence!.querySelectorAll("[data-setup-key]")) {
      expect(chip.getAttribute("aria-label")).toBeNull();
    }
    for (const sep of sentence!.querySelectorAll(
      "[data-setup-key-separator]",
    )) {
      expect(sep.getAttribute("aria-hidden")).toBe("true");
    }
  });

  it("drops the unbound half rather than rendering Not set inside the sentence", () => {
    renderStep({ config: makeConfig({ toggle_shortcut: "" }) });
    expect(gestureSentence()?.textContent).toBe(
      "To reply, hold Right Shift. Agents adds no new shortcut.",
    );
    expect(gestureSentence()?.textContent).not.toContain("Not set");

    cleanup();
    renderStep({ config: makeConfig({ push_to_talk_shortcut: "" }) });
    expect(gestureSentence()?.textContent).toBe(
      "To reply, tap Ctrl+Alt+D. Agents adds no new shortcut.",
    );
  });

  it("suppresses the sentence entirely when neither gesture is bound", async () => {
    const onCanAdvanceChange = vi.fn();
    renderStep({
      config: makeConfig({
        push_to_talk_shortcut: "",
        toggle_shortcut: "",
      }),
      onCanAdvanceChange,
    });
    expect(gestureSentence()).toBeNull();
    expect(statusLine()?.textContent).toBe(NO_GESTURE);
    // Attention, not error: it does not block this step.
    expect(statusLine()?.getAttribute("role")).toBe("status");
    await waitFor(() => expect(onCanAdvanceChange).toHaveBeenCalledWith(true));
  });

  it("disables Continue while a shortcut conflict is live, and outranks the no-gesture line", async () => {
    const onCanAdvanceChange = vi.fn();
    renderStep({
      config: makeConfig({
        // Bound to the same key as Push to Talk, and to nothing above it in
        // Configure's candidate list.
        primary_shortcut: "f9",
        claude_view_shortcut: "right shift",
        toggle_shortcut: "",
      }),
      onCanAdvanceChange,
    });

    await waitFor(() => expect(onCanAdvanceChange).toHaveBeenCalledWith(false));
    expect(statusLine()?.textContent).not.toBe(NO_GESTURE);
    expect(statusLine()?.textContent).toContain("Push to Talk");
    expect(statusLine()?.getAttribute("role")).toBe("alert");
  });

  it("re-enables Continue once no conflict is live", async () => {
    const onCanAdvanceChange = vi.fn();
    renderStep({ onCanAdvanceChange });
    await waitFor(() => expect(onCanAdvanceChange).toHaveBeenCalledWith(true));
    expect(onCanAdvanceChange).not.toHaveBeenCalledWith(false);
    expect(statusLine()?.textContent).toBe("");
  });

  it("never saves a conflicting capture", () => {
    const { props } = renderStep();
    const captured = vi.mocked(props.subscribe).mock.calls[0][0];

    fireEvent.click(captureButton());
    captured({ type: "shortcut.captured", target: "claude_view", keys: "right shift" } as never);
    expect(props.onConfigChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("Press shortcut"));
    fireEvent.click(captureButton());
    captured({ type: "shortcut.captured", target: "claude_view", keys: "ctrl+alt+k" } as never);
    expect(props.onConfigChange).toHaveBeenCalledWith({
      claude_view_shortcut: "ctrl+alt+k",
    });
  });
});
