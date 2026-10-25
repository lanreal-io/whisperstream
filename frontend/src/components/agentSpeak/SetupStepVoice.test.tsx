import { createRef } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api_speech from "../../lib/api/speech";
import type { AppConfig } from "../../lib/api/config";
import type { SpeechStatus } from "../../lib/api/speech";
import type { SetupStepHandle } from "./AgentSpeakSetupModal";
import { SetupStepVoice, type SetupStepVoiceProps } from "./SetupStepVoice";
import { VOICE_MODEL_INFO } from "./voiceModelCopy";

vi.mock("../../lib/api/speech", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/speech")>();
  return {
    ...actual,
    getSpeechVoices: vi.fn(async () => ({
      engine: "supertonic" as const,
      engine_label: "Lower memory",
      voices: [
        { id: 1, sid: 1, label: "Brianna", group: "female" as const },
        { id: 6, sid: 6, label: "George", group: "male" as const },
      ],
    })),
  };
});

const LEDE =
  "Choose whether replies appear as written summaries, spoken audio, or both.";
const SUMMARY_DESCRIPTION = "Display a written summary in the overlay.";
const SPEECH_DESCRIPTION = "Play summaries using your selected voice.";
const ACCESSIBLE_CONSTRAINT = "At least one reply mode must remain enabled.";
const VOICE_CAPTION = "Choose how spoken summaries sound.";
const SPEED_CAPTION = "Adjust how quickly replies are read.";
const DOWNLOAD_START_FAILURE =
  "Could not start the download. You can try again on the last step.";
const KOKORO_FAILURE =
  "Higher quality could not start. Try Lower memory instead.";
/** Copy `A8`: the neutral caption that replaced the gold `attention` line. */
const ENGINE_CAPTION = "Balance quality and memory use.";
/** Inherited copy 22. Retired on this surface; Configure keeps it. */
const KOKORO_CAUTION =
  "Higher quality uses more memory than this computer has spare.";

function makeConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    claude_hud_enabled: true,
    agent_speak_setup_completed: false,
    agent_speech_engine: "auto",
    agent_speech_voice: 1,
    agent_speech_kokoro_voice: "af_heart",
    agent_speech_speed: 1,
    agent_speech_volume: 1,
    ...overrides,
  } as AppConfig;
}

function makeStatus(overrides: Partial<SpeechStatus> = {}): SpeechStatus {
  return {
    requested: "auto",
    preferred_engine: "supertonic",
    resolved_engine: "supertonic",
    active_engine: null,
    reason: "supertonic",
    memory: { total_mb: 16384, available_mb: 8192, eligible: true },
    kokoro_memory_tier: "recommended",
    engines: {
      kokoro: {
        key: "kokoro",
        label: "Higher quality",
        installed: false,
        downloading: false,
        download_bytes: 340_000_000,
        installed_bytes: 360_000_000,
      },
      supertonic: {
        key: "supertonic",
        label: "Lower memory",
        installed: false,
        downloading: false,
        download_bytes: 90_000_000,
        installed_bytes: 96_000_000,
      },
    },
    current_download: null,
    recommended_downloads: ["supertonic"],
    ...overrides,
  };
}

function renderStep(overrides: Partial<SetupStepVoiceProps> = {}) {
  const props: SetupStepVoiceProps = {
    config: makeConfig(),
    onCanAdvanceChange: vi.fn(),
    onChildDialogOpenChange: vi.fn(),
    onConfigChange: vi.fn(),
    speechStatus: makeStatus(),
    speechPlaying: false,
    subscribe: vi.fn(() => () => {}),
    onRefreshSpeechStatus: vi.fn(async () => null),
    onDownload: vi.fn(async () => {}),
    onCancelDownload: vi.fn(async () => {}),
    ...overrides,
  };
  return { ...render(<SetupStepVoice {...props} />), props };
}

/** The one reserved status line this step owns. */
function statusLine(): HTMLElement | null {
  return document.querySelector<HTMLElement>("p.min-h-\\[14px\\]");
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

beforeEach(() => {
  vi.mocked(api_speech.getSpeechVoices).mockResolvedValue({
    engine: "supertonic",
    engine_label: "Lower memory",
    voices: [
      { id: 1, sid: 1, label: "Brianna", group: "female" },
      { id: 6, sid: 6, label: "George", group: "male" },
    ],
  });
});

describe("SetupStepVoice", () => {
  it("renders ONE heading, the locked lede, mode toggles, and the engine control", async () => {
    renderStep();
    const headings = screen.getAllByRole("heading");
    expect(headings).toHaveLength(1);
    expect(headings[0]).toHaveTextContent("Choose how replies appear");
    expect(headings[0].tagName).toBe("H2");
    // The retired `SpeakSection` title must not come back alongside it.
    expect(screen.queryByRole("heading", { name: "Voice" })).toBeNull();
    expect(screen.getByText(LEDE)).toBeInTheDocument();
    expect(screen.getByText("Show summaries")).toBeInTheDocument();
    expect(screen.getByText("Read replies aloud")).toBeInTheDocument();
    expect(screen.getByText("Voice model")).toBeInTheDocument();
    // Each pill carries its own "i" and tooltip (owner, 2026-10-25).
    expect(screen.queryByRole("button", { name: "Voice model information" })).toBeNull();
    for (const [name, value] of [["Automatic", "auto"], ["Higher quality", "kokoro"], ["Lower memory", "supertonic"]] as const) {
      const pill = screen.getByRole("button", { name });
      expect(pill.querySelector("svg")).not.toBeNull();
      expect(within(pill.parentElement as HTMLElement).getByRole("tooltip")).toHaveTextContent(VOICE_MODEL_INFO[value]);
    }
    await waitFor(() => expect(api_speech.getSpeechVoices).toHaveBeenCalled());
  });

  it("renders both delivery mode toggles enabled by default and prevents disabling both", () => {
    const onShowSummariesChange = vi.fn();
    const onReadAloudChange = vi.fn();
    renderStep({ onShowSummariesChange, onReadAloudChange });

    const summariesToggle = screen.getByRole("switch", {
      name: "Show written summaries for coding agent replies",
    });
    const speechToggle = screen.getByRole("switch", {
      name: "Read coding agent replies aloud",
    });

    expect(summariesToggle).toHaveAttribute("aria-checked", "true");
    expect(speechToggle).toHaveAttribute("aria-checked", "true");
    expect(summariesToggle).not.toBeDisabled();
    expect(speechToggle).not.toBeDisabled();
    expect(screen.getByText(SUMMARY_DESCRIPTION)).toBeVisible();
    expect(screen.getByText(SPEECH_DESCRIPTION)).toBeVisible();
    expect(screen.queryByText("At least one reply mode must stay on.")).toBeNull();
    expect(screen.getByText(ACCESSIBLE_CONSTRAINT)).toHaveClass("sr-only");
    expect(summariesToggle).toHaveAttribute(
      "aria-describedby",
      "show-summaries-description reply-mode-constraint",
    );
    expect(speechToggle).toHaveAttribute(
      "aria-describedby",
      "read-aloud-description reply-mode-constraint",
    );

    // Turn off speech: summaries becomes disabled (cannot disable last mode)
    fireEvent.click(speechToggle);
    expect(onReadAloudChange).toHaveBeenCalledWith(false);
  });

  it("uses full-width, left-aligned rows with toggles on the right", () => {
    const { container } = renderStep();
    const group = container.querySelector("[data-setup-reply-modes]");
    const rows = Array.from(
      container.querySelectorAll("[data-setup-reply-mode-row]"),
    );

    expect(group).toHaveClass("w-full");
    expect(rows).toHaveLength(2);
    for (const row of rows) {
      expect(row).toHaveClass("w-full", "justify-between");
      expect(row.className).not.toMatch(/\bborder-(?:b|t)/);
      expect(row.querySelector(".text-left")).not.toBeNull();
      expect(row.lastElementChild).toHaveAttribute("role", "switch");
    }
  });

  it("enforces cannot-disable-last constraint without silently toggling the other mode", () => {
    const { rerender, props } = renderStep({
      showSummaries: true,
      readAloud: false,
    });

    const summariesToggle = screen.getByRole("switch", {
      name: "Show written summaries for coding agent replies",
    });
    const speechToggle = screen.getByRole("switch", {
      name: "Read coding agent replies aloud",
    });

    expect(summariesToggle).toBeDisabled();
    expect(speechToggle).not.toBeDisabled();

    // Clicking the disabled summary toggle does nothing
    fireEvent.click(summariesToggle);
    expect(summariesToggle).toHaveAttribute("aria-checked", "true");

    // Turning speech back on enables both
    rerender(<SetupStepVoice {...props} showSummaries={true} readAloud={true} />);
    expect(
      screen.getByRole("switch", {
        name: "Show written summaries for coding agent replies",
      }),
    ).not.toBeDisabled();
  });

  it("hides voice controls and does not download on commit when speech is disabled", async () => {
    const ref = createRef<SetupStepHandle>();
    const onDownload = vi.fn(async () => {});
    renderStep({
      ref,
      showSummaries: true,
      readAloud: false,
      onDownload,
    });

    expect(screen.queryByText("Voice model")).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Text to speak" })).toBeNull();
    expect(screen.queryByRole("slider", { name: "Speech speed" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Play test text" })).toBeNull();

    await act(async () => {
      await ref.current?.commit?.();
    });
    expect(onDownload).not.toHaveBeenCalled();
  });

  it("draws no card and gives the VoicePicker trigger a steel-tint border but no added fill", async () => {
    // Design decision 19, REVISED 2026-08-21 by owner feedback. #404 removed
    // the trigger's chrome entirely, but on this centred step the dropdown is a
    // lone instrument with no filled sibling to anchor it, so white
    // `--border-hairline` over white `--surface-control` over white
    // `--surface-modal` composited to no edge and the control stopped reading
    // as a dropdown. The fix is a border ONLY, via the shared `Dropdown`'s
    // opt-in `triggerBorderColor` set to the same both-theme steel-tint idiom
    // as this picker's `DISC_RING`/`GROUP_DIVIDER`. Still no added fill and no
    // inset card: the background stays `Dropdown`'s own `--surface-control`
    // default, not a step-specific fill. Configure's copy keeps the
    // `--border-hairline` default (owner decision 2026-08-13); that trigger is
    // asserted elsewhere. The voice-model control is a segmented `ModeSelector`,
    // not a `Dropdown`, so it is not governed by this decision.
    const { container } = renderStep();
    await waitFor(() => expect(api_speech.getSpeechVoices).toHaveBeenCalled());

    const trigger = screen.getByRole("button", { name: /^Voice[:,]/ });
    expect(trigger.style.border).toBe(
      "1px solid color-mix(in srgb, var(--color-steel) 32%, transparent)",
    );
    expect(trigger.style.background).toBe("var(--surface-control)");

    // No trace of the deleted inset card.
    expect(container.innerHTML).not.toContain("--surface-inset-card");
  });

  it("renders the voice-model choice with Automatic as its own pill", async () => {
    // Owner, 2026-10-24: Automatic is a visible choice and the default, no
    // longer shown as whichever model it resolved to.
    renderStep();
    const group = screen.getByRole("group", { name: "Voice model" });
    const pills = within(group).getAllByRole("button");
    expect(pills.map((pill) => pill.textContent)).toEqual([
      "Automatic",
      "Higher quality",
      "Lower memory",
    ]);
    expect(
      within(group).getByRole("button", { name: "Automatic" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      within(group).getByRole("button", { name: "Lower memory" }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it("renders no preview hint row: the lede carries it (locked copy 9)", () => {
    const { container } = renderStep();
    // In order: the lede, delivery-mode subtitles, three voice-control
    // subtitles, and the one reserved status line, empty at rest.
    const paragraphs = Array.from(container.querySelectorAll("p"));
    expect(paragraphs.map((p) => p.textContent)).toEqual([
      LEDE,
      SUMMARY_DESCRIPTION,
      SPEECH_DESCRIPTION,
      ENGINE_CAPTION,
      VOICE_CAPTION,
      SPEED_CAPTION,
      "",
    ]);
  });

  it("aligns every voice control as a full-width label-left control-right row", () => {
    const { container } = renderStep();
    const group = container.querySelector("[data-setup-voice-controls]");
    const rows = Array.from(
      container.querySelectorAll("[data-setup-voice-control-row]"),
    );

    expect(group).toHaveClass("w-full");
    expect(rows.map((row) => row.getAttribute("data-setup-voice-control-row"))).toEqual([
      "model",
      "voice",
      "speed",
    ]);
    for (const row of rows) {
      expect(row).toHaveClass("w-full", "justify-between");
      expect(row.className).not.toMatch(/\bborder-(?:b|t)/);
      expect(row.querySelector(".text-left")).not.toBeNull();
    }

    expect(screen.getByText(VOICE_CAPTION)).toBeVisible();
    expect(screen.getByText(SPEED_CAPTION)).toBeVisible();
  });

  it("carries no StatusLight and no gold attention branch, even on an ineligible machine", async () => {
    // Design decision 20: the engine is auto-selected FROM the user's RAM, so
    // there is no error, no user decision, and no requested action. Gold is
    // the app's caution hue; flagging a correct automatic decision as caution
    // reads as the interface apologising for itself.
    renderStep({
      config: makeConfig({ agent_speech_engine: "kokoro" }),
      speechStatus: makeStatus({
        memory: { total_mb: 8192, available_mb: 1024, eligible: false },
      }),
    });

    expect(screen.queryByText(KOKORO_CAUTION)).toBeNull();
    expect(statusLine()?.textContent).toBe("");
    // `StatusLight` announces one of these three sentences and renders one of
    // three visible micro-labels. None of them may exist on this step.
    expect(
      screen.queryByRole("status", { name: /agent speak is|needs attention/i }),
    ).toBeNull();
    for (const label of ["Ready", "Attention", "Off"]) {
      expect(screen.queryByText(label)).toBeNull();
    }

    // The fact survives as the neutral caption instead.
    expect(screen.getByText(ENGINE_CAPTION)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Automatic" }).querySelector("svg")).not.toBeNull();
  });

  it("plays previews with no voice model installed", async () => {
    // `useVoicePreview` plays bundled clips with no backend call, so every row
    // auditions BEFORE anything is on disk. Previews are never gated on
    // install state.
    const bare = makeStatus();
    expect(bare.engines.supertonic.installed).toBe(false);
    expect(bare.engines.kokoro.installed).toBe(false);
    renderStep({ speechStatus: bare });

    const picker = await screen.findByRole("button", { name: /^Voice[:,]/ });
    await waitFor(() => expect(picker).not.toBeDisabled());
    fireEvent.click(picker);
    // The per-row disc inside the open panel remains available too.
    const list = await screen.findByRole("listbox", { name: "Voice" });
    const transport = await within(list).findByRole("button", {
      name: "Play Brianna sample",
    });
    expect(transport).not.toBeDisabled();
    fireEvent.click(transport);
    // Nothing threw and no backend call was made for the audition.
    expect(api_speech.getSpeechVoices).toHaveBeenCalledTimes(1);
  });

  it("always lets Continue advance", async () => {
    const onCanAdvanceChange = vi.fn();
    renderStep({ onCanAdvanceChange });
    await waitFor(() => expect(onCanAdvanceChange).toHaveBeenCalledWith(true));
    expect(onCanAdvanceChange).not.toHaveBeenCalledWith(false);
  });

  it("orders the status line engine failure > download-start failure > sample failure, with no caution rung", async () => {
    // 1. Kokoro failure wins outright. The memory state is ineligible here
    //    too, and it contributes nothing: the caution rung no longer exists.
    const failing = renderStep({
      config: makeConfig({ agent_speech_engine: "kokoro" }),
      speechStatus: makeStatus({
        reason: "unhealthy_kokoro",
        memory: { total_mb: 8192, available_mb: 1024, eligible: false },
      }),
    });
    expect(statusLine()?.textContent).toBe(KOKORO_FAILURE);
    failing.unmount();

    // 2. Download-start failure, with no engine failure above it.
    const ref = createRef<SetupStepHandle>();
    renderStep({
      ref,
      onDownload: vi.fn(async () => {
        throw new Error("409 Conflict");
      }),
    });
    await act(async () => {
      await ref.current?.commit?.();
    });
    await waitFor(() =>
      expect(statusLine()?.textContent).toBe(DOWNLOAD_START_FAILURE),
    );
  });

  it("does not report Automatic's slow fallback as an explicit Kokoro startup failure", () => {
    renderStep({
      config: makeConfig({ agent_speech_engine: "kokoro" }),
      speechStatus: makeStatus({ reason: "slow_kokoro" }),
    });

    expect(screen.queryByText(KOKORO_FAILURE)).toBeNull();
    expect(statusLine()?.textContent).toBe("");
  });

  it("surfaces the locked download-start failure as an alert on a 409 and still resolves so the shell advances", async () => {
    const ref = createRef<SetupStepHandle>();
    const onDownload = vi.fn(async () => {
      throw new Error("409 Conflict");
    });
    renderStep({ ref, onDownload });

    // `commit` must never reject: a rejected commit would be an unhandled
    // rejection in the shell, which advances without awaiting it.
    await act(async () => {
      await expect(ref.current!.commit!()).resolves.toBeUndefined();
    });

    expect(onDownload).toHaveBeenCalledWith(["supertonic", "kokoro"]);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(DOWNLOAD_START_FAILURE);
  });

  it("downloads both models for Automatic on commit, Supertonic first, skipping what is installed", async () => {
    const ref = createRef<SetupStepHandle>();
    const onDownload = vi.fn(async () => {});
    const { unmount } = renderStep({ ref, onDownload });
    await act(async () => {
      await ref.current?.commit?.();
    });
    expect(onDownload).toHaveBeenCalledWith(["supertonic", "kokoro"]);
    unmount();

    const partialRef = createRef<SetupStepHandle>();
    const onDownloadPartial = vi.fn(async () => {});
    const partial = makeStatus();
    partial.engines.supertonic.installed = true;
    const partialView = renderStep({
      ref: partialRef,
      onDownload: onDownloadPartial,
      speechStatus: partial,
    });
    await act(async () => {
      await partialRef.current?.commit?.();
    });
    expect(onDownloadPartial).toHaveBeenCalledWith(["kokoro"]);
    partialView.unmount();

    const installedRef = createRef<SetupStepHandle>();
    const onDownload2 = vi.fn(async () => {});
    const installed = makeStatus();
    installed.engines.supertonic.installed = true;
    installed.engines.kokoro.installed = true;
    renderStep({
      ref: installedRef,
      onDownload: onDownload2,
      speechStatus: installed,
    });
    await act(async () => {
      await installedRef.current?.commit?.();
    });
    expect(onDownload2).not.toHaveBeenCalled();
  });

  it("reuses Configure's download-confirm dialog for an uninstalled engine and reports the child dialog to the shell", async () => {
    const onChildDialogOpenChange = vi.fn();
    renderStep({
      config: makeConfig({ agent_speech_engine: "kokoro" }),
      onChildDialogOpenChange,
    });

    fireEvent.click(screen.getByRole("button", { name: "Lower memory" }));

    await waitFor(() =>
      expect(onChildDialogOpenChange).toHaveBeenCalledWith(true),
    );
    expect(screen.getByRole("button", { name: /download/i })).toBeInTheDocument();
  });

  it("raises the blocking memory dialog when the Kokoro tier is not recommended", async () => {
    const onChildDialogOpenChange = vi.fn();
    const cautionStatus = makeStatus({
      kokoro_memory_tier: "caution",
      memory: { total_mb: 8192, available_mb: 2048, eligible: false },
    });
    renderStep({
      onChildDialogOpenChange,
      onRefreshSpeechStatus: vi.fn(async () => cautionStatus),
    });

    fireEvent.click(screen.getByRole("button", { name: "Higher quality" }));

    await waitFor(() =>
      expect(onChildDialogOpenChange).toHaveBeenCalledWith(true),
    );
    expect(
      screen.getByRole("dialog", { name: /memory/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Use lower memory" }),
    ).toBeInTheDocument();
  });

  it("renders speed and an icon-only bundled preview beside the voice selector", async () => {
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechVoices).toHaveBeenCalled());

    // The speed slider keeps the same accessible name as Configure.
    expect(
      screen.getByRole("slider", { name: "Speech speed" }),
    ).toBeInTheDocument();

    // Preview is the compact transport immediately beside the collapsed
    // selector. Its accessible name carries the action while visible copy does
    // not add another label or test box.
    const preview = await screen.findByRole("button", {
      name: "Play Brianna sample",
    });
    const picker = screen
      .getByRole("button", { name: /^Voice[:,]/ })
      .closest('[data-ui-qa-voice-picker="true"]');
    expect(picker).toContainElement(preview);
    expect(preview).toHaveTextContent("");
    expect(screen.queryByRole("textbox", { name: "Text to speak" })).toBeNull();
    expect(
      document.querySelector('[data-setup-voice-control-row="test"]'),
    ).toBeNull();
    fireEvent.click(preview);
    expect(api_speech.getSpeechVoices).toHaveBeenCalledTimes(1);
  });

  it("writes agent_speech_speed when the speed slider moves", async () => {
    const onConfigChange = vi.fn();
    renderStep({ onConfigChange });
    const slider = screen.getByRole("slider", { name: "Speech speed" });
    fireEvent.change(slider, { target: { value: "1.5" } });
    expect(onConfigChange).toHaveBeenCalledWith({ agent_speech_speed: 1.5 });
    // Settle the pending voices-load state update inside act().
    await waitFor(() => expect(api_speech.getSpeechVoices).toHaveBeenCalled());
  });

});

it("does not offer an unsupported Mac voice runtime or download on Continue", async () => {
  const status = makeStatus({ requested: "supertonic", preferred_engine: "supertonic", resolved_engine: null });
  status.engines.supertonic.supported = false;
  const ref = createRef<SetupStepHandle>();
  const onDownload = vi.fn();
  renderStep({ config: makeConfig({ agent_speech_engine: "supertonic" }), speechStatus: status, ref, onDownload });
  expect(screen.getByRole("button", { name: "Lower memory" })).toBeDisabled();
  await act(async () => { await ref.current?.commit?.(); });
  expect(onDownload).not.toHaveBeenCalled();
});

// Synthetic available clips exercise playback independently of distribution assets.
vi.mock("./voiceSamples", () => ({ hasVoiceSample: () => true }));
