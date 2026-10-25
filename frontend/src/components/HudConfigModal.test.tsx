import { readyAgentResult } from "../test/agentCompatibility";
import { VOICE_MODEL_INFO } from "./agentSpeak/voiceModelCopy";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api_speech from "../lib/api/speech";
import * as api_agents from "../lib/api/agents";
import type { AppConfig } from "../lib/api/config";
import type { SpeechVoices, SpeechStatus } from "../lib/api/speech";
import { HudConfigModal, type HudConfigModalProps } from "./HudConfigModal";

vi.mock("../lib/api/speech", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/speech")>();
  return {
    ...actual,
    getSpeechVoices: vi.fn(() => new Promise<never>(() => {})),
    speakSample: vi.fn(async () => ({ ok: true })),
    stopSpeech: vi.fn(async () => ({ ok: true })),
  };
});
vi.mock("../lib/api/agents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/agents")>();
  return {
    ...actual,
    getManagedCliStatus: vi.fn(async () => ({
      ok: true,
      status: "not_installed" as const,
      available: true,
      installed: false,
    })),
    installManagedCli: vi.fn(async () => ({
      ok: true,
      status: "installed" as const,
      available: true,
      installed: true,
      restart_required: true,
    })),
    uninstallManagedCli: vi.fn(async () => ({
      ok: true,
      status: "not_installed" as const,
      available: true,
      installed: false,
    })),
  };
});

const SUPERTONIC_VOICES = [
  {
    id: 0,
    sid: 0,
    label: "Ava",
    group: "female",
    reference_label: "Voice 0",
  },
  {
    id: 1,
    sid: 1,
    label: "Brianna",
    group: "female",
    reference_label: "Voice 1",
  },
  {
    id: 2,
    sid: 2,
    label: "Claire",
    group: "female",
    reference_label: "Voice 2",
  },
  {
    id: 3,
    sid: 3,
    label: "Danielle",
    group: "female",
    reference_label: "Voice 3",
  },
  {
    id: 4,
    sid: 4,
    label: "Eleanor",
    group: "female",
    reference_label: "Voice 4",
  },
  {
    id: 5,
    sid: 5,
    label: "Frank",
    group: "male",
    reference_label: "Voice 5",
  },
  {
    id: 6,
    sid: 6,
    label: "George",
    group: "male",
    reference_label: "Voice 6",
  },
  {
    id: 7,
    sid: 7,
    label: "Henry",
    group: "male",
    reference_label: "Voice 7",
  },
  {
    id: 8,
    sid: 8,
    label: "Isaiah",
    group: "male",
    reference_label: "Voice 8",
  },
  {
    id: 9,
    sid: 9,
    label: "James",
    group: "male",
    reference_label: "Voice 9",
  },
] as const;

const KOKORO_VOICES = [
  { id: "af_heart", sid: "af_heart", label: "Heart", group: "female" },
  { id: "af_bella", sid: "af_bella", label: "Bella", group: "female" },
  { id: "af_nicole", sid: "af_nicole", label: "Nicole", group: "female" },
  { id: "af_aoede", sid: "af_aoede", label: "Aoede", group: "female" },
  { id: "af_kore", sid: "af_kore", label: "Kore", group: "female" },
  { id: "af_sarah", sid: "af_sarah", label: "Sarah", group: "female" },
  { id: "am_fenrir", sid: "am_fenrir", label: "Fenrir", group: "male" },
  { id: "am_michael", sid: "am_michael", label: "Michael", group: "male" },
  { id: "am_puck", sid: "am_puck", label: "Puck", group: "male" },
] as const;

function mockVoiceMetadata(): void {
  vi.mocked(api_speech.getSpeechVoices).mockImplementation(
    (requestedEngine?: "kokoro" | "supertonic") => {
      const engine = requestedEngine ?? "supertonic";
      return Promise.resolve(
        engine === "kokoro"
          ? {
              engine,
              engine_label: "Kokoro",
              voices: [...KOKORO_VOICES],
            }
          : {
              engine,
              engine_label: "Supertonic",
              voices: [...SUPERTONIC_VOICES],
            },
      );
    },
  );
}

/**
 * The voice picker rests collapsed (owner reversal, 2026-08-13), so every
 * assertion about the roster has to open it first. The trigger's accessible
 * name is the shared `Dropdown`'s `"<ariaLabel>: <current value>"`.
 */
async function openVoicePicker(triggerName: string) {
  fireEvent.click(await screen.findByRole("button", { name: triggerName }));
  return screen.getByRole("listbox", { name: "Voice" });
}

describe("HudConfigModal adaptive voice controls", () => {
  it("keeps idle unloading advanced and defaults legacy snapshots to 30 minutes", () => {
    const props = renderModal(makeConfig({ agent_speech_enabled: true }));
    const advanced = screen.getByRole("button", { name: "Advanced" });
    expect(advanced).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /Unload voice model when idle:/ })).not.toBeInTheDocument();

    fireEvent.click(advanced);
    expect(advanced).toHaveAttribute("aria-expanded", "true");
    const helpLabel = screen.getByText("Unload voice model when idle", { exact: true });
    expect(helpLabel).toHaveAttribute("tabindex", "0");
    expect(helpLabel).toHaveAccessibleDescription("Free memory after speech is inactive. The next reply may take a moment to start.");
    fireEvent.click(screen.getByRole("button", { name: "Unload voice model when idle: 30 min" }));
    const listbox = screen.getByRole("listbox", { name: "Unload voice model when idle" });
    expect(within(listbox).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Never", "5 min", "10 min", "15 min", "30 min", "1 hour",
    ]);
    fireEvent.click(within(listbox).getByRole("option", { name: "Never" }));
    expect(props.onConfigChange).toHaveBeenCalledWith({ agent_speech_idle_timeout_seconds: 0 });
  });

  it("preserves the saved idle timeout while speech is disabled and re-enabled", () => {
    const config = makeConfig({ agent_speech_enabled: true, agent_speech_idle_timeout_seconds: 900 });
    const props = renderModal(config);
    cleanup();
    const { rerender } = render(<HudConfigModal {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    expect(screen.getByRole("button", { name: "Unload voice model when idle: 15 min" })).toBeInTheDocument();

    rerender(<HudConfigModal {...props} config={{ ...config, agent_speech_enabled: false }} />);
    expect(screen.queryByRole("button", { name: "Advanced" })).not.toBeInTheDocument();
    expect(props.onConfigChange).not.toHaveBeenCalled();
    rerender(<HudConfigModal {...props} config={config} />);
    expect(screen.getByRole("button", { name: "Unload voice model when idle: 15 min" })).toBeInTheDocument();
    expect(props.onConfigChange).not.toHaveBeenCalled();
  });

  it("offers Automatic beside both models, with an info tooltip for the current choice", () => {
    const props = renderModal(
      makeConfig({ agent_speech_enabled: true, agent_speech_engine: "auto" }),
    );

    const trigger = screen.getByRole("button", {
      name: "Voice model: Automatic",
    });
    expect(trigger).toHaveAttribute("aria-haspopup", "listbox");
    expect(
      screen.getByRole("button", { name: "Voice model information" }),
    ).toHaveAccessibleDescription(VOICE_MODEL_INFO.auto);
    fireEvent.click(trigger);

    expect(screen.getByRole("option", { name: "Automatic" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Higher quality" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: "Lower memory" }));

    expect(props.onConfigChange).toHaveBeenCalledWith({
      agent_speech_engine: "supertonic",
    });
  });

  it("explains the chosen model and switches back to Automatic", () => {
    const props = renderModal(
      makeConfig({ agent_speech_enabled: true, agent_speech_engine: "kokoro" }),
    );
    expect(
      screen.getByRole("button", { name: "Voice model information" }),
    ).toHaveAccessibleDescription(VOICE_MODEL_INFO.kokoro);
    fireEvent.click(screen.getByRole("button", { name: "Voice model: Higher quality" }));
    fireEvent.click(screen.getByRole("option", { name: "Automatic" }));
    expect(props.onConfigChange).toHaveBeenCalledWith({ agent_speech_engine: "auto" });
  });

  it("renders grouped names for both engines and saves only stable typed IDs", async () => {
    mockVoiceMetadata();
    const kokoro = renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "kokoro",
        agent_speech_kokoro_voice: "af_heart",
        agent_speech_voice: 2,
      }),
      {
        speechStatus: makeSpeechStatus({
          requested: "kokoro",
          preferred_engine: "kokoro",
          resolved_engine: "kokoro",
          active_engine: "kokoro",
        }),
      },
    );

    const kokoroList = await openVoicePicker("Voice: Heart");
    expect(
      within(kokoroList).getByRole("option", { name: "Heart" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(within(kokoroList).getByText("FEMALE")).toBeInTheDocument();
    expect(within(kokoroList).getByText("MALE")).toBeInTheDocument();
    expect(
      within(kokoroList)
        .getAllByRole("option")
        .map((row) => row.textContent),
    ).toEqual([
      "Heart",
      "Bella",
      "Nicole",
      "Aoede",
      "Kore",
      "Sarah",
      "Fenrir",
      "Michael",
      "Puck",
    ]);
    expect(screen.queryByText("af_heart")).not.toBeInTheDocument();
    fireEvent.click(within(kokoroList).getByRole("option", { name: "Michael" }));
    expect(kokoro.onConfigChange).toHaveBeenCalledWith({
      agent_speech_kokoro_voice: "am_michael",
    });
    const kokoroConfigCalls = vi.mocked(kokoro.onConfigChange).mock.calls;
    expect(kokoroConfigCalls[kokoroConfigCalls.length - 1]?.[0]).toEqual({
      agent_speech_kokoro_voice: "am_michael",
    });
    expect(kokoro.onConfigChange).not.toHaveBeenCalledWith({
      agent_speech_voice: expect.anything(),
    });

    cleanup();
    const supertonic = renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "supertonic",
        agent_speech_kokoro_voice: "af_bella",
        agent_speech_voice: 2,
      }),
      {
        speechStatus: makeSpeechStatus({
          requested: "supertonic",
          preferred_engine: "supertonic",
          resolved_engine: "supertonic",
          active_engine: "supertonic",
          reason: "supertonic",
        }),
      },
    );
    const supertonicList = await openVoicePicker("Voice: Claire");
    const supertonicVoice = within(supertonicList).getByRole("option", {
      name: "Claire",
    });
    expect(supertonicVoice).toHaveAttribute("aria-selected", "true");
    expect(supertonicVoice).toHaveTextContent("Claire");
    // `reference_label` no longer prints on the row (it was redundant noise
    // beside a distinct name), but it still has to keep the rows apart for a
    // screen reader, so it lives on the transport's accessible name.
    expect(supertonicVoice).not.toHaveTextContent("Voice 2");
    expect(
      within(supertonicList).getByRole("button", { name: "Play Voice 2 sample" }),
    ).toBeInTheDocument();
    expect(
      within(supertonicList)
        .getAllByRole("option")
        .map((row) => row.textContent),
    ).toEqual([
      "Ava",
      "Brianna",
      "Claire",
      "Danielle",
      "Eleanor",
      "Frank",
      "George",
      "Henry",
      "Isaiah",
      "James",
    ]);
    fireEvent.click(
      within(supertonicList).getByRole("option", { name: "George" }),
    );
    const supertonicConfigCalls = vi.mocked(
      supertonic.onConfigChange,
    ).mock.calls;
    const supertonicPatch =
      supertonicConfigCalls[supertonicConfigCalls.length - 1]?.[0];
    expect(supertonicPatch).toEqual({
      agent_speech_voice: 6,
    });
    expect(typeof supertonicPatch?.agent_speech_voice).toBe("number");
  });

  it("shows Brianna and Heart for the per-engine default voice slots", async () => {
    mockVoiceMetadata();
    renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "supertonic",
      }),
      {
        speechStatus: makeSpeechStatus({
          requested: "supertonic",
          preferred_engine: "supertonic",
          resolved_engine: "supertonic",
          active_engine: "supertonic",
          reason: "supertonic",
        }),
      },
    );
    // The collapsed trigger IS the selection readout now.
    expect(
      await screen.findByRole("button", { name: "Voice: Brianna" }),
    ).toBeInTheDocument();

    cleanup();
    renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "kokoro",
      }),
      {
        speechStatus: makeSpeechStatus({
          requested: "kokoro",
          preferred_engine: "kokoro",
          resolved_engine: "kokoro",
          active_engine: "kokoro",
        }),
      },
    );
    expect(
      await screen.findByRole("button", { name: "Voice: Heart" }),
    ).toBeInTheDocument();
  });

  it("marks the voice picker busy while metadata is loading", () => {
    renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "supertonic",
      }),
      {
        speechStatus: makeSpeechStatus({
          requested: "supertonic",
          preferred_engine: "supertonic",
          resolved_engine: "supertonic",
          active_engine: "supertonic",
          reason: "supertonic",
        }),
      },
    );

    // A collapsed trigger has a fixed box, so an engine switch never reflows
    // the modal; the loading state is announced, not drawn.
    expect(
      document.querySelector("[data-ui-qa-voice-picker]"),
    ).toHaveAttribute("aria-busy", "true");
    expect(
      screen.getByRole("button", { name: /^Voice, loading/ }),
    ).toBeDisabled();
    expect(screen.queryAllByRole("option")).toHaveLength(0);
  });

  it("ignores stale voice metadata after rapidly switching engines", async () => {
    const pending = new Map<
      "kokoro" | "supertonic",
      (voices: SpeechVoices) => void
    >();
    vi.mocked(api_speech.getSpeechVoices).mockImplementation(
      (engine = "supertonic") =>
        new Promise<SpeechVoices>((resolve) => {
          pending.set(engine, resolve);
        }),
    );
    const baseProps: HudConfigModalProps = {
      open: true,
      onClose: vi.fn(),
      config: makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "kokoro",
        agent_speech_kokoro_voice: "af_heart",
        agent_speech_voice: 2,
      }),
      onConfigChange: vi.fn(),
      connected: true,
      send: vi.fn(() => true),
      onProviderToggle: vi.fn(),
      providerResults: { codex: null, claude: null },
      speechStatus: makeSpeechStatus({
        requested: "kokoro",
        preferred_engine: "kokoro",
        resolved_engine: "kokoro",
        active_engine: "kokoro",
      }),
      speechPlaying: false,
      subscribe: vi.fn(() => () => {}),
      onSpeechEnabledChange: vi.fn(),
      onRefreshSpeechStatus: vi.fn().mockResolvedValue(
        makeSpeechStatus({
          requested: "kokoro",
          preferred_engine: "kokoro",
          resolved_engine: "kokoro",
        }),
      ),
      onDownload: vi.fn().mockResolvedValue(undefined),
      onCancelDownload: vi.fn().mockResolvedValue(undefined),
    };
    const view = render(<HudConfigModal {...baseProps} />);

    await waitFor(() =>
      expect(api_speech.getSpeechVoices).toHaveBeenCalledWith("kokoro"),
    );
    view.rerender(
      <HudConfigModal
        {...baseProps}
        config={{
          ...baseProps.config,
          agent_speech_engine: "supertonic",
        }}
        speechStatus={makeSpeechStatus({
          requested: "supertonic",
          preferred_engine: "supertonic",
          resolved_engine: "supertonic",
          active_engine: "supertonic",
          reason: "supertonic",
        })}
      />,
    );
    await waitFor(() =>
      expect(api_speech.getSpeechVoices).toHaveBeenCalledWith("supertonic"),
    );

    await act(async () => {
      pending.get("supertonic")?.({
        engine: "supertonic",
        engine_label: "Supertonic",
        voices: [
          SUPERTONIC_VOICES[1],
          SUPERTONIC_VOICES[2],
        ],
      });
    });
    expect(
      await screen.findByRole("button", { name: "Voice: Claire" }),
    ).toBeInTheDocument();

    await act(async () => {
      pending.get("kokoro")?.({
        engine: "kokoro",
        engine_label: "Kokoro",
        voices: [KOKORO_VOICES[0]],
      });
    });
    const list = await openVoicePicker("Voice: Claire");
    expect(
      within(list).getByRole("option", { name: "Claire" }),
    ).toBeInTheDocument();
    expect(
      within(list).queryByRole("option", { name: "Heart" }),
    ).not.toBeInTheDocument();
  });

  it("shows Automatic as its own choice while the voice list follows the model it picked", async () => {
    mockVoiceMetadata();
    renderModal(
      makeConfig({ agent_speech_enabled: true, agent_speech_engine: "auto" }),
      {
        speechStatus: makeSpeechStatus({
          preferred_engine: "supertonic",
          resolved_engine: "supertonic",
          active_engine: "supertonic",
          reason: "low_memory",
          memory: {
            total_mb: 8192,
            available_mb: 1200,
            eligible: false,
          },
          kokoro_memory_tier: "caution",
        }),
      },
    );

    expect(
      screen.getByRole("button", { name: "Voice model: Automatic" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Voice: Brianna" }),
    ).toBeInTheDocument();
  });

  it("does not repeat normal per-reply model details below the selector", async () => {
    mockVoiceMetadata();
    renderModal(
      makeConfig({ agent_speech_enabled: true, agent_speech_engine: "auto" }),
      {
        speechPlaying: true,
        speechStatus: makeSpeechStatus({
          preferred_engine: "kokoro",
          resolved_engine: "kokoro",
          active_engine: "supertonic",
          reason: "kokoro",
        }),
      },
    );

    expect(screen.queryByText(/Using Supertonic for this reply/)).not.toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Voice: Brianna" }),
    ).toBeInTheDocument();
  });

  it("keeps a missing explicit model pending and opens the shared confirmation dialog", async () => {
    const props = renderModal(
      makeConfig({ agent_speech_enabled: true, agent_speech_engine: "auto" }),
      {
        speechStatus: makeSpeechStatus({
          engines: {
            ...makeSpeechStatus().engines,
            kokoro: {
              ...makeSpeechStatus().engines.kokoro,
              installed: false,
            },
          },
        }),
      },
    );

    fireEvent.click(screen.getByRole("button", { name: "Voice model: Automatic" }));
    fireEvent.click(screen.getByRole("option", { name: "Higher quality" }));

    await screen.findByRole("heading", { name: "Download this model?" });
    const dialogs = screen.getAllByRole("dialog");
    expect(dialogs).toHaveLength(2);
    expect(
      screen.getByRole("heading", { name: "Download this model?" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Kokoro is about 345 MB/)).toBeInTheDocument();
    expect(screen.queryByText("Download voice")).not.toBeInTheDocument();
    expect(props.onConfigChange).not.toHaveBeenCalledWith({
      agent_speech_engine: "kokoro",
    });

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    // Cancelling the download leaves the saved choice, Automatic, in place.
    expect(
      screen.getByRole("button", { name: "Voice model: Automatic" }),
    ).toBeInTheDocument();
    expect(props.onConfigChange).not.toHaveBeenCalledWith({
      agent_speech_engine: "kokoro",
    });
  });

  it("does not select or redownload an installed unavailable runtime", async () => {
    mockVoiceMetadata();
    const props = renderModal(
      makeConfig({ agent_speech_enabled: true, agent_speech_engine: "kokoro" }),
      {
        speechStatus: makeSpeechStatus({
          engines: {
            ...makeSpeechStatus().engines,
            supertonic: {
              ...makeSpeechStatus().engines.supertonic,
              installed: true,
              available: false,
              unavailable_reason: "runtime_unavailable",
            },
          },
        }),
      },
    );

    fireEvent.click(screen.getByRole("button", { name: "Voice model: Higher quality" }));
    fireEvent.click(screen.getByRole("option", { name: "Lower memory" }));

    expect(
      await screen.findByText("Lower memory isn't available on this platform."),
    ).toBeInTheDocument();
    expect(props.onConfigChange).not.toHaveBeenCalledWith({
      agent_speech_engine: "supertonic",
    });
    expect(screen.queryByRole("heading", { name: "Download this model?" })).not.toBeInTheDocument();
  });

  it("allows an explicit Kokoro retry after Automatic quarantines a supported engine", async () => {
    mockVoiceMetadata();
    const status = makeSpeechStatus({
      requested: "auto",
      preferred_engine: "kokoro",
      resolved_engine: "supertonic",
      reason: "unhealthy_kokoro",
      engines: {
        ...makeSpeechStatus().engines,
        kokoro: {
          ...makeSpeechStatus().engines.kokoro,
          supported: true,
          available: false,
          unavailable_reason: "load_failed",
        },
      },
    });
    const props = renderModal(
      makeConfig({ agent_speech_enabled: true, agent_speech_engine: "auto" }),
      { speechStatus: status, onRefreshSpeechStatus: vi.fn(async () => status) },
    );
    fireEvent.click(screen.getByRole("button", { name: "Voice model: Automatic" }));
    fireEvent.click(screen.getByRole("option", { name: "Higher quality" }));
    await waitFor(() => expect(props.onConfigChange).toHaveBeenCalledWith({
      agent_speech_engine: "kokoro",
    }));
    expect(screen.queryByText(/isn't available on this platform/)).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Download this model?" })).not.toBeInTheDocument();
  });

  it("keeps bundled voice previews available when the selected runtime is unavailable", async () => {
    mockVoiceMetadata();
    renderModal(
      makeConfig({ agent_speech_enabled: true, agent_speech_engine: "supertonic" }),
      {
        speechStatus: makeSpeechStatus({
          requested: "supertonic",
          preferred_engine: "supertonic",
          resolved_engine: null,
          reason: "runtime_unavailable",
          engines: {
            ...makeSpeechStatus().engines,
            supertonic: {
              ...makeSpeechStatus().engines.supertonic,
              installed: true,
              available: false,
              unavailable_reason: "runtime_unavailable",
            },
          },
        }),
      },
    );

    expect(
      await screen.findByText("Lower memory isn't available on this platform."),
    ).toBeInTheDocument();
    await openVoicePicker("Voice: Brianna");
    expect(screen.getByRole("button", { name: "Play Voice 3 sample" })).toBeEnabled();
  });

  it("starts the concrete download and applies the pending selection only after its matching completion", async () => {
    vi.useFakeTimers();
    const handlers: Array<(event: import("../lib/socket/events").AppEvent) => void> = [];
    const subscribe = vi.fn(
      (handler: (event: import("../lib/socket/events").AppEvent) => void) => {
        handlers.push(handler);
        return () => {};
      },
    );
    const props = renderModal(
      makeConfig({ agent_speech_enabled: true, agent_speech_engine: "auto" }),
      {
        speechStatus: makeSpeechStatus({
          engines: {
            ...makeSpeechStatus().engines,
            kokoro: {
              ...makeSpeechStatus().engines.kokoro,
              installed: false,
            },
          },
        }),
        subscribe,
      },
    );

    fireEvent.click(screen.getByRole("button", { name: "Voice model: Automatic" }));
    fireEvent.click(screen.getByRole("option", { name: "Higher quality" }));
    await act(async () => {
      await Promise.resolve();
    });
    screen.getByRole("heading", { name: "Download this model?" });
    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    expect(props.onDownload).toHaveBeenCalledWith(["kokoro"]);
    expect(props.onConfigChange).not.toHaveBeenCalledWith({
      agent_speech_engine: "kokoro",
    });

    act(() => {
      for (const handler of handlers) {
        handler({
          type: "download.complete",
          kind: "tts",
          engine: "supertonic",
        });
      }
      vi.advanceTimersByTime(300);
    });
    expect(props.onConfigChange).not.toHaveBeenCalledWith({
      agent_speech_engine: "kokoro",
    });

    act(() => {
      for (const handler of handlers) {
        handler({
          type: "download.complete",
          kind: "tts",
          engine: "kokoro",
        });
      }
      vi.advanceTimersByTime(300);
    });
    expect(props.onConfigChange).toHaveBeenCalledWith({
      agent_speech_engine: "kokoro",
    });
    expect(
      screen.queryByRole("heading", { name: "Download this model?" }),
    ).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("rechecks memory and asks before using Kokoro in the caution band", async () => {
    const cautionStatus = makeSpeechStatus({
      requested: "supertonic",
      preferred_engine: "supertonic",
      resolved_engine: "supertonic",
      reason: "supertonic",
      memory: {
        total_mb: 12_288,
        available_mb: 1_536,
        eligible: false,
      },
      kokoro_memory_tier: "caution",
    });
    const props = renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "supertonic",
      }),
      {
        speechStatus: cautionStatus,
        onRefreshSpeechStatus: vi.fn().mockResolvedValue(cautionStatus),
      },
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Voice model: Lower memory" }),
    );
    fireEvent.click(screen.getByRole("option", { name: "Higher quality" }));

    expect(
      await screen.findByRole("heading", {
        name: "Kokoro may use too much memory",
      }),
    ).toBeInTheDocument();
    const dialogs = screen.getAllByRole("dialog");
    expect(dialogs).toHaveLength(2);
    expect(
      screen
        .getByRole("heading", { name: "Kokoro may use too much memory" })
        .closest('[role="dialog"]')?.parentElement,
    ).toHaveClass("z-[60]");
    expect(props.onRefreshSpeechStatus).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", { name: "Continue anyway" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Continue anyway" }));
    expect(props.onConfigChange).toHaveBeenCalledWith({
      agent_speech_engine: "kokoro",
    });
  });

  it("blocks Kokoro below the override floor without a continue action", async () => {
    const blockedStatus = makeSpeechStatus({
      requested: "supertonic",
      preferred_engine: "supertonic",
      resolved_engine: "supertonic",
      reason: "supertonic",
      memory: {
        total_mb: 4_096,
        available_mb: 512,
        eligible: false,
      },
      kokoro_memory_tier: "blocked",
    });
    const props = renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "supertonic",
      }),
      {
        speechStatus: blockedStatus,
        onRefreshSpeechStatus: vi.fn().mockResolvedValue(blockedStatus),
      },
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Voice model: Lower memory" }),
    );
    fireEvent.click(screen.getByRole("option", { name: "Higher quality" }));

    expect(
      await screen.findByRole("heading", {
        name: "Not enough memory for Kokoro",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Continue anyway" }),
    ).not.toBeInTheDocument();
    expect(props.onConfigChange).not.toHaveBeenCalledWith({
      agent_speech_engine: "kokoro",
    });
  });

  it("keeps showing Automatic when it falls back from an unhealthy Kokoro", () => {
    renderModal(
      makeConfig({ agent_speech_enabled: true, agent_speech_engine: "auto" }),
      {
        speechStatus: makeSpeechStatus({
          resolved_engine: "supertonic",
          active_engine: "supertonic",
          reason: "unhealthy_kokoro",
          recommended_downloads: [],
        }),
      },
    );

    expect(
      screen.getByRole("button", { name: "Voice model: Automatic" }),
    ).toBeInTheDocument();
  });

  it("keeps explicit Kokoro selected and gives a text caution on low memory", () => {
    renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "kokoro",
      }),
      {
        speechStatus: makeSpeechStatus({
          requested: "kokoro",
          memory: {
            total_mb: 8192,
            available_mb: 1200,
            eligible: false,
          },
        }),
      },
    );

    expect(
      screen.getByText(
        "Higher quality uses more memory than this computer has spare.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Voice model: Higher quality/ }),
    ).toBeInTheDocument();
  });

  it("gives explicit Kokoro users a working retry step", () => {
    renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "kokoro",
      }),
      {
        speechStatus: makeSpeechStatus({
          requested: "kokoro",
          resolved_engine: null,
          active_engine: null,
          reason: "unhealthy_kokoro",
          engines: {
            ...makeSpeechStatus().engines,
            kokoro: {
              ...makeSpeechStatus().engines.kokoro,
              supported: true,
              available: false,
              unavailable_reason: "load_failed",
            },
          },
        }),
      },
    );

    // Previews are bundled clips now, so "try the sample again" was dead
    // advice; the one recovery that works is the other voice model.
    expect(
      screen.getByRole("alert"),
    ).toHaveTextContent(
      "Higher quality could not start. Try Lower memory instead.",
    );
  });

  it("does not label Automatic's slow fallback as an explicit Kokoro startup failure", () => {
    renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "kokoro",
      }),
      {
        speechStatus: makeSpeechStatus({
          requested: "kokoro",
          resolved_engine: "kokoro",
          active_engine: null,
          reason: "slow_kokoro",
        }),
      },
    );

    expect(
      screen.queryByText(
        "Higher quality could not start. Try Lower memory instead.",
      ),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Voice model: Higher quality/ }),
    ).toBeInTheDocument();
  });

  it("previews a voice inline from a bundled clip with no backend call", async () => {
    mockVoiceMetadata();
    const props = renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "kokoro",
        agent_speech_kokoro_voice: "af_bella",
      }),
      {
        speechStatus: makeSpeechStatus({ requested: "kokoro" }),
      },
    );

    const bellaList = await openVoicePicker("Voice: Bella");
    // Row-scoped: the collapsed-trigger disc carries the same label for the
    // selected voice, so target the row transport specifically.
    fireEvent.click(
      within(bellaList).getByRole("button", { name: "Play Bella sample" }),
    );
    // Playing never selects (design decision 15), and the clip is bundled, so
    // no sample request reaches the backend.
    expect(props.onConfigChange).not.toHaveBeenCalled();
    expect(api_speech.speakSample).not.toHaveBeenCalled();
    expect(api_speech.stopSpeech).not.toHaveBeenCalled();

    cleanup();
    renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_engine: "supertonic",
        agent_speech_voice: 6,
      }),
      {
        speechStatus: makeSpeechStatus({
          requested: "supertonic",
          preferred_engine: "supertonic",
          resolved_engine: "supertonic",
          active_engine: "supertonic",
          reason: "supertonic",
        }),
      },
    );
    // Supertonic rows carry a reference_label, and the transport announces it
    // so two rows never read identically to a screen reader.
    const georgeList = await openVoicePicker("Voice: George");
    expect(
      within(georgeList).getByRole("button", { name: "Play Voice 6 sample" }),
    ).toBeInTheDocument();
    expect(api_speech.speakSample).not.toHaveBeenCalled();
  });
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  vi.clearAllMocks();
  vi.mocked(api_speech.getSpeechVoices).mockImplementation(
    () => new Promise<never>(() => {}),
  );
});

function makeConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    claude_hud_enabled: false,
    codex_hud_enabled: false,
    claude_hud_show_text: true,
    agent_speak_setup_completed: false,
    agent_speech_mode: "off",
    agent_speech_engine: "auto",
    agent_speech_voice: 1,
    agent_speech_kokoro_voice: "af_heart",
    agent_speech_speed: 1,
    agent_speech_volume: 1,
    claude_reply_grace_seconds: 6,
    claude_reply_queue_delay_seconds: 2,
    claude_reply_auto_submit: false,
    claude_view_shortcut: "ctrl+alt+`",
    agent_summary_text_enabled: false,
    ...overrides,
  } as AppConfig;
}

function makeSpeechStatus(
  overrides: Partial<SpeechStatus> = {},
): SpeechStatus {
  return {
    requested: "auto",
    preferred_engine: "kokoro",
    resolved_engine: "kokoro",
    active_engine: "kokoro",
    reason: "kokoro",
    memory: { total_mb: 32768, available_mb: 8192, eligible: true },
    kokoro_memory_tier: "recommended",
    engines: {
      supertonic: {
        key: "supertonic",
        label: "Supertonic",
        installed: true,
        downloading: false,
        download_bytes: 128_774_318,
        installed_bytes: 145_316_356,
      },
      kokoro: {
        key: "kokoro",
        label: "Kokoro",
        installed: true,
        downloading: false,
        download_bytes: 318_413_913,
        installed_bytes: 345_395_578,
      },
    },
    current_download: null,
    recommended_downloads: [],
    ...overrides,
  };
}

function renderModal(
  config: AppConfig = makeConfig(),
  overrides: Partial<HudConfigModalProps> = {},
) {
  const props: HudConfigModalProps = {
    open: true,
    onClose: vi.fn(),
    config,
    onConfigChange: vi.fn(),
    connected: true,
    send: vi.fn(() => true),
    onProviderToggle: vi.fn(),
    providerResults: { codex: null, claude: null },
    speechStatus: makeSpeechStatus(),
    speechPlaying: false,
    subscribe: vi.fn(() => () => {}),
    onSpeechEnabledChange: vi.fn(),
    onRefreshSpeechStatus: vi
      .fn()
      .mockResolvedValue(overrides.speechStatus ?? makeSpeechStatus()),
    onDownload: vi.fn().mockResolvedValue(undefined),
    onCancelDownload: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };

  props.providerResults = {
    codex: { ...readyAgentResult(), ...props.providerResults.codex },
    claude: { ...readyAgentResult(), ...props.providerResults.claude },
  };
  render(<HudConfigModal {...props} />);
  return props;
}

describe("HudConfigModal locked Agent HUD settings", () => {
  it.each(["light", "dark"])("uses backend-selected dev commands in %s", (theme) => {
    document.documentElement.dataset.theme = theme;
    renderModal(makeConfig({ agent_speech_enabled: true }), {
      speechStatus: makeSpeechStatus({ plugin_variant: "dev" }),
    });
    // The selected option's hint is both the row description and its pill
    // tooltip.
    expect(screen.getAllByText(
      "Use /whisperstream-dev:speak in Claude Code or $speak-dev in Codex for that session.",
    )).toHaveLength(2);
    expect(screen.queryByText(
      "Use /whisperstream:speak in Claude Code or $speak in Codex for that session.",
    )).not.toBeInTheDocument();
    delete document.documentElement.dataset.theme;
  });

  it("puts one tooltip on each activation option and marks the QA hover target", () => {
    renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_activation: "per_session",
      }),
      { uiQaStateId: "settings.agent-speak-tooltip" },
    );

    // Locked copy 18 was one row-level hint carrying both definitions. The
    // owner split it (2026-08-13): each half now hangs on the pill it defines,
    // and the `Speak in` label carries no tooltip at all.
    // The selected option's hint also shows as the row description (owner,
    // 2026-10-25), so pick the tooltip copy out by role.
    const perSession = screen
      .getAllByText(
        "Use /whisperstream:speak in Claude Code or $speak in Codex for that session.",
      )
      .find((node) => node.getAttribute("role") === "tooltip");
    const alwaysOn = screen.getByText(/Speaks in every session/i);
    expect(perSession).toHaveAttribute("role", "tooltip");
    expect(alwaysOn).toHaveAttribute("role", "tooltip");
    // The old concatenated one-hint-for-both string is gone entirely.
    expect(
      screen.queryByText(/you want spoken\. Every session:/),
    ).not.toBeInTheDocument();

    const hoverTarget = document.querySelector("[data-ui-qa-hover-target]");
    expect(hoverTarget).not.toBeNull();
    expect(hoverTarget).toHaveTextContent("When I ask");
  });

  it("renders the two locked sections without a console shortcut or legacy Off and None choices", () => {
    // Speech on, because the Voice section body now collapses to one line when
    // the header master switch is off (the activation pills only exist in the
    // expanded body).
    renderModal(makeConfig({ agent_speech_enabled: true }));

    // Sentence case, matching ProfileEditorModal's section labels. Owner
    // restructure (2026-08-13): `Claude Code` became `Agent` and absorbed the
    // reply rows, so the bare `Replies` heading is gone.
    // Owner direction (2026-10-25): the master panels carry the only
    // headings; the bare Overlay and Replies titles are gone.
    expect(
      screen.getByRole("heading", { name: "Agents", level: 3 }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Voice", level: 3 }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Overlay", level: 3 }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Replies", level: 3 }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Claude Code", level: 3 }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("CLAUDE CODE")).not.toBeInTheDocument();
    expect(screen.queryByText("REPLIES")).not.toBeInTheDocument();
    expect(screen.queryByText("Open Agent Console")).not.toBeInTheDocument();
    expect(screen.getByText("When I ask")).toBeInTheDocument();
    expect(screen.getByText("Every session")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Off" })).not.toBeInTheDocument();
    expect(screen.queryByText("None")).not.toBeInTheDocument();
    expect(screen.queryByText("Full reply")).not.toBeInTheDocument();
    expect(screen.queryByText("Updates")).not.toBeInTheDocument();
  });

  it("normalizes a legacy non-off mode into the speech master without exposing that mode", () => {
    renderModal(makeConfig({ agent_speech_mode: "summary" }));

    expect(
      screen.getByRole("switch", { name: "Read coding agent replies aloud" }),
    ).toBeChecked();
    expect(screen.getByRole("button", { name: "When I ask" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      screen.getByRole("button", { name: /^Voice, loading/ }),
    ).toBeInTheDocument();
  });

  it("uses the speech master for silence and keeps Automatic when both models are installed", async () => {
    const props = renderModal(makeConfig({ agent_speech_enabled: false }));

    fireEvent.click(
      screen.getByRole("switch", { name: "Read coding agent replies aloud" }),
    );

    await waitFor(() =>
      expect(props.onSpeechEnabledChange).toHaveBeenCalledWith(true),
    );
    expect(props.onConfigChange).toHaveBeenCalledWith({
      agent_speech_engine: "auto",
    });
    expect(
      document.querySelector("[data-ui-qa-voice-picker]"),
    ).toBeNull();
  });

  it("confirms the one detected model download before enabling speech", async () => {
    vi.useFakeTimers();
    const handlers: Array<(event: import("../lib/socket/events").AppEvent) => void> = [];
    const props = renderModal(
      makeConfig({ agent_speech_enabled: false }),
      {
        speechStatus: makeSpeechStatus({
          preferred_engine: "supertonic",
          resolved_engine: null,
          active_engine: null,
          reason: "missing_supertonic",
          engines: {
            ...makeSpeechStatus().engines,
            supertonic: {
              ...makeSpeechStatus().engines.supertonic,
              installed: false,
            },
          },
          recommended_downloads: ["supertonic"],
        }),
        onRefreshSpeechStatus: vi.fn().mockResolvedValue(
          makeSpeechStatus({
            preferred_engine: "supertonic",
            resolved_engine: null,
            reason: "missing_supertonic",
            engines: {
              ...makeSpeechStatus().engines,
              supertonic: {
                ...makeSpeechStatus().engines.supertonic,
                installed: false,
              },
            },
            recommended_downloads: ["supertonic"],
          }),
        ),
        subscribe: (handler) => {
          handlers.push(handler);
          return () => {};
        },
      },
    );

    fireEvent.click(
      screen.getByRole("switch", { name: "Read coding agent replies aloud" }),
    );

    await act(async () => {
      await Promise.resolve();
    });
    screen.getByRole("heading", { name: "Download this model?" });
    expect(props.onSpeechEnabledChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(props.onDownload).toHaveBeenCalledWith(["supertonic"]);

    act(() => {
      for (const handler of handlers) {
        handler({
          type: "download.complete",
          kind: "tts",
          engine: "supertonic",
        });
      }
      vi.advanceTimersByTime(300);
    });

    expect(props.onSpeechEnabledChange).toHaveBeenCalledWith(true);
    expect(props.onConfigChange).toHaveBeenCalledWith({
      agent_speech_engine: "auto",
    });
  });

  it("confirms both Automatic models at once, Supertonic first, and enables speech when Supertonic lands", async () => {
    vi.useFakeTimers();
    const handlers: Array<(event: import("../lib/socket/events").AppEvent) => void> = [];
    const bothMissing = makeSpeechStatus({
      resolved_engine: null,
      reason: "missing_kokoro",
      engines: {
        kokoro: { ...makeSpeechStatus().engines.kokoro, installed: false },
        supertonic: { ...makeSpeechStatus().engines.supertonic, installed: false },
      },
      recommended_downloads: ["supertonic", "kokoro"],
    });
    const props = renderModal(makeConfig({ agent_speech_enabled: false }), {
      speechStatus: bothMissing,
      onRefreshSpeechStatus: vi.fn().mockResolvedValue(bothMissing),
      subscribe: (handler) => {
        handlers.push(handler);
        return () => {};
      },
    });

    fireEvent.click(
      screen.getByRole("switch", { name: "Read coding agent replies aloud" }),
    );
    await act(async () => {
      await Promise.resolve();
    });
    screen.getByRole("heading", { name: "Download these models?" });
    screen.getByText(/^Both voice models are about \d+ MB\. They download once/);

    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(props.onDownload).toHaveBeenCalledWith(["supertonic", "kokoro"]);

    act(() => {
      for (const handler of handlers) {
        handler({ type: "download.complete", kind: "tts", engine: "supertonic" });
      }
      vi.advanceTimersByTime(300);
    });
    expect(props.onSpeechEnabledChange).toHaveBeenCalledWith(true);
    expect(props.onConfigChange).toHaveBeenCalledWith({
      agent_speech_engine: "auto",
    });
  });

  it("choosing Automatic with speech on confirms the missing Kokoro before saving", async () => {
    const kokoroMissing = makeSpeechStatus({
      requested: "supertonic",
      engines: {
        ...makeSpeechStatus().engines,
        kokoro: { ...makeSpeechStatus().engines.kokoro, installed: false },
      },
    });
    const props = renderModal(
      makeConfig({ agent_speech_enabled: true, agent_speech_engine: "supertonic" }),
      { speechStatus: kokoroMissing },
    );

    fireEvent.click(screen.getByRole("button", { name: "Voice model: Lower memory" }));
    fireEvent.click(screen.getByRole("option", { name: "Automatic" }));
    screen.getByRole("heading", { name: "Download this model?" });
    expect(props.onConfigChange).not.toHaveBeenCalledWith({
      agent_speech_engine: "auto",
    });
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(props.onDownload).toHaveBeenCalledWith(["kokoro"]);
  });

  it("an explicit voice model still downloads and saves only itself", async () => {
    const props = renderModal(
      makeConfig({ agent_speech_enabled: false, agent_speech_engine: "supertonic" }),
      {
        onRefreshSpeechStatus: vi.fn().mockResolvedValue(
          makeSpeechStatus({ requested: "supertonic", preferred_engine: "supertonic" }),
        ),
      },
    );

    fireEvent.click(
      screen.getByRole("switch", { name: "Read coding agent replies aloud" }),
    );

    await waitFor(() =>
      expect(props.onSpeechEnabledChange).toHaveBeenCalledWith(true),
    );
    expect(props.onConfigChange).toHaveBeenCalledWith({
      agent_speech_engine: "supertonic",
    });
  });

  it("persists activation and exposes the exact contextual help", () => {
    const props = renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_activation: "per_session",
      }),
    );

    // The selected option's help is the row description and its pill tooltip.
    expect(
      screen.getAllByText(
        "Use /whisperstream:speak in Claude Code or $speak in Codex for that session.",
      ),
    ).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Every session" }));

    expect(props.onConfigChange).toHaveBeenCalledWith({
      agent_speech_activation: "always_on",
    });
    cleanup();
    renderModal(
      makeConfig({
        agent_speech_enabled: true,
        agent_speech_activation: "always_on",
      }),
    );
    // Selected, the always-on hint is the row description and its tooltip.
    expect(
      screen.getAllByText(/Speaks in every session\. No command needed\./),
    ).toHaveLength(2);
  });

  it("no longer shows the Summary source row at all", () => {
    // Its only selectable value was "Agent summary" (the V1 contract and the
    // sole supported source); the other was a permanently-disabled
    // "Coming later" placeholder. A control with one selectable option is an
    // advertisement, not a setting, so the whole row was cut in round 04.
    renderModal(makeConfig({ agent_speech_enabled: true }));

    expect(screen.queryByText("Summary source")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Whisperstream summary/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Coming later")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Agent summary" }),
    ).not.toBeInTheDocument();
  });

  it("retains voice sample, speed, and volume controls under the speech master", async () => {
    mockVoiceMetadata();
    const props = renderModal(makeConfig({ agent_speech_enabled: true }));

    const heartList = await openVoicePicker("Voice: Heart");
    expect(
      within(heartList).getByRole("button", { name: "Play Heart sample" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByRole("slider", { name: "Speech speed" }), {
      target: { value: "1.4" },
    });
    fireEvent.change(screen.getByRole("slider", { name: "Speech volume" }), {
      target: { value: "0.65" },
    });

    expect(props.onConfigChange).toHaveBeenCalledWith({
      agent_speech_speed: 1.4,
    });
    expect(props.onConfigChange).toHaveBeenCalledWith({
      agent_speech_volume: 0.65,
    });
  });

  it("no longer offers the controls cut in iteration 03", () => {
    renderModal(makeConfig());

    expect(
      screen.queryByRole("slider", { name: "Delay between replies" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("switch", { name: "Allow permission decisions" }),
    ).not.toBeInTheDocument();
  });

  it("shows and persists the locked reply controls across 0 to 30 seconds", () => {
    const props = renderModal(makeConfig());
    const grace = screen.getByRole("slider", {
      name: "Reply window",
    });
    const autoSubmit = screen.getByRole("switch", {
      name: "Send replies automatically",
    });

    expect(grace).toHaveAttribute("min", "0");
    expect(grace).toHaveAttribute("max", "6");
    expect(grace).toHaveAttribute("step", "any");
    expect(grace).toHaveValue("2");
    expect(screen.getByRole("textbox", { name: "Reply window seconds" })).toHaveValue("6");
    expect(autoSubmit).not.toBeChecked();
    // Row descriptions became label tooltips; the standing "Replies continue
    // Claude through its active reply window." lead-in was cut outright.
    expect(
      screen.getByText(
        "Time to reply after the voice stops. Escape skips to the next response.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Sends your reply the moment dictation stops, with no Enter press.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Turn it off to review/)).toBeNull();
    expect(
      screen.getByText(/sessions started with the Whisperstream CLI/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        "Replies continue Claude through its active reply window.",
      ),
    ).not.toBeInTheDocument();

    fireEvent.change(grace, { target: { value: "0" } });
    expect(props.onConfigChange).toHaveBeenCalledWith({
      claude_reply_grace_seconds: 0,
    });

    fireEvent.change(screen.getByRole("textbox", { name: "Reply window seconds" }), { target: { value: "1" } });
    expect(props.onConfigChange).toHaveBeenCalledWith({
      claude_reply_grace_seconds: 1,
    });

    fireEvent.change(screen.getByRole("textbox", { name: "Reply window seconds" }), { target: { value: "20" } });
    expect(props.onConfigChange).toHaveBeenCalledWith({
      claude_reply_grace_seconds: 20,
    });

    fireEvent.change(screen.getByRole("textbox", { name: "Reply window seconds" }), { target: { value: "30" } });
    expect(props.onConfigChange).toHaveBeenCalledWith({
      claude_reply_grace_seconds: 30,
    });

    fireEvent.click(autoSubmit);
    expect(props.onConfigChange).toHaveBeenCalledWith({
      claude_reply_auto_submit: true,
    });

    cleanup();
    renderModal(
      makeConfig({
        claude_reply_grace_seconds: 0,
        claude_hud_enabled: true,
      }),
    );
    // The tick labels are gone (the box shows the value); zero still
    // announces as Off.
    expect(screen.getByRole("slider", { name: "Reply window" })).toHaveAttribute("aria-valuetext", "Off");

    cleanup();
    renderModal(
      makeConfig({
        claude_reply_grace_seconds: 1,
        claude_hud_enabled: true,
      }),
    );
    expect(screen.getByRole("textbox", { name: "Reply window seconds" })).toHaveValue("1");

    cleanup();
    renderModal(
      makeConfig({
        claude_reply_grace_seconds: 20,
        claude_hud_enabled: true,
      }),
    );
    expect(screen.getByRole("textbox", { name: "Reply window seconds" })).toHaveValue("20");

    cleanup();
    renderModal(
      makeConfig({
        claude_reply_grace_seconds: 30,
        claude_hud_enabled: true,
      }),
    );
    expect(screen.getByRole("textbox", { name: "Reply window seconds" })).toHaveValue("30");
  });

  it("orders the General rows view shortcut, summaries, reply window, auto-send", () => {
    renderModal(makeConfig({ agent_speech_enabled: true }));

    const agent = screen.getByRole("region", { name: "Agents" });
    // View full response leads, then Show summaries, then Reply window, then Send replies automatically
    expect(
      within(agent)
        .getAllByText(
          /^(View full response|Show summaries|Reply window|Send replies automatically)$/,
        )
        .map((node) => node.textContent),
    ).toEqual([
      "View full response",
      "Show summaries",
      "Reply window",
      "Send replies automatically",
    ]);
    expect(
      within(agent).getByRole("switch", {
        name: "Show written summaries for coding agent replies",
      }),
    ).toBeInTheDocument();
    expect(
      within(agent).getByRole("slider", { name: "Reply window" }),
    ).toBeInTheDocument();
    expect(
      within(agent).getByRole("switch", { name: "Send replies automatically" }),
    ).toBeInTheDocument();
  });

  it("shows the locked copy as the Show summaries description", () => {
    renderModal(makeConfig({ agent_summary_text_enabled: false }));

    const toggle = screen.getByRole("switch", {
      name: "Show written summaries for coding agent replies",
    });
    expect(toggle).not.toBeChecked();

    const hint = screen.getByText(
      "Shows your coding agent’s short summary in the overlay when it replies.",
    );
    expect(hint.tagName).toBe("P");
    expect(hint).not.toHaveAttribute("role", "tooltip");
  });

  it("operates Show summaries independently from Voice", () => {
    // Both off
    const props = renderModal(
      makeConfig({
        agent_summary_text_enabled: false,
        agent_speech_enabled: false,
      }),
    );

    const summariesToggle = screen.getByRole("switch", {
      name: "Show written summaries for coding agent replies",
    });
    const voiceToggle = screen.getByRole("switch", {
      name: "Read coding agent replies aloud",
    });

    expect(summariesToggle).not.toBeChecked();
    expect(voiceToggle).not.toBeChecked();

    // Toggle summaries on: voice master is not touched
    fireEvent.click(summariesToggle);
    expect(props.onConfigChange).toHaveBeenCalledWith(
      expect.objectContaining({ agent_summary_text_enabled: true }),
    );
    expect(props.onSpeechEnabledChange).not.toHaveBeenCalled();

    cleanup();
    // Summaries on, Voice on
    const props2 = renderModal(
      makeConfig({
        agent_summary_text_enabled: true,
        agent_speech_enabled: true,
      }),
    );
    const summariesToggle2 = screen.getByRole("switch", {
      name: "Show written summaries for coding agent replies",
    });
    const voiceToggle2 = screen.getByRole("switch", {
      name: "Read coding agent replies aloud",
    });
    expect(summariesToggle2).toBeChecked();
    expect(voiceToggle2).toBeChecked();

    // Toggle summaries off: voice remains enabled
    fireEvent.click(summariesToggle2);
    expect(props2.onConfigChange).toHaveBeenCalledWith(
      expect.objectContaining({ agent_summary_text_enabled: false }),
    );
    expect(props2.onSpeechEnabledChange).not.toHaveBeenCalled();
  });

  it("applies smart default transitions between 6s and 20s when enabling and disabling summaries", () => {
    // Enabling when grace is exactly 6s updates grace to 20s in the same change
    const props1 = renderModal(
      makeConfig({
        agent_summary_text_enabled: false,
        claude_reply_grace_seconds: 6,
      }),
    );
    fireEvent.click(
      screen.getByRole("switch", {
        name: "Show written summaries for coding agent replies",
      }),
    );
    expect(props1.onConfigChange).toHaveBeenCalledWith({
      agent_summary_text_enabled: true,
      claude_reply_grace_seconds: 20,
    });

    cleanup();
    // Enabling when grace is default undefined (treated as 6s) updates grace to 20s
    const configWithoutGrace = makeConfig({ agent_summary_text_enabled: false });
    delete configWithoutGrace.claude_reply_grace_seconds;
    const props2 = renderModal(configWithoutGrace);
    fireEvent.click(
      screen.getByRole("switch", {
        name: "Show written summaries for coding agent replies",
      }),
    );
    expect(props2.onConfigChange).toHaveBeenCalledWith({
      agent_summary_text_enabled: true,
      claude_reply_grace_seconds: 20,
    });

    cleanup();
    // Disabling when grace is exactly 20s reverts grace to 6s in the same change
    const props3 = renderModal(
      makeConfig({
        agent_summary_text_enabled: true,
        claude_reply_grace_seconds: 20,
      }),
    );
    fireEvent.click(
      screen.getByRole("switch", {
        name: "Show written summaries for coding agent replies",
      }),
    );
    expect(props3.onConfigChange).toHaveBeenCalledWith({
      agent_summary_text_enabled: false,
      claude_reply_grace_seconds: 6,
    });
  });

  it("strictly preserves custom grace values when enabling and disabling summaries", () => {
    // Enabling with custom grace (10s) keeps 10s
    const props1 = renderModal(
      makeConfig({
        agent_summary_text_enabled: false,
        claude_reply_grace_seconds: 10,
      }),
    );
    fireEvent.click(
      screen.getByRole("switch", {
        name: "Show written summaries for coding agent replies",
      }),
    );
    expect(props1.onConfigChange).toHaveBeenCalledWith({
      agent_summary_text_enabled: true,
    });
    expect(props1.onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ claude_reply_grace_seconds: expect.anything() }),
    );

    cleanup();
    // Enabling with grace set to Off (0s) keeps 0s
    const props2 = renderModal(
      makeConfig({
        agent_summary_text_enabled: false,
        claude_reply_grace_seconds: 0,
      }),
    );
    fireEvent.click(
      screen.getByRole("switch", {
        name: "Show written summaries for coding agent replies",
      }),
    );
    expect(props2.onConfigChange).toHaveBeenCalledWith({
      agent_summary_text_enabled: true,
    });

    cleanup();
    // Disabling with custom grace (25s) keeps 25s
    const props3 = renderModal(
      makeConfig({
        agent_summary_text_enabled: true,
        claude_reply_grace_seconds: 25,
      }),
    );
    fireEvent.click(
      screen.getByRole("switch", {
        name: "Show written summaries for coding agent replies",
      }),
    );
    expect(props3.onConfigChange).toHaveBeenCalledWith({
      agent_summary_text_enabled: false,
    });

    cleanup();
    // Disabling with custom grace (30s) keeps 30s
    const props4 = renderModal(
      makeConfig({
        agent_summary_text_enabled: true,
        claude_reply_grace_seconds: 30,
      }),
    );
    fireEvent.click(
      screen.getByRole("switch", {
        name: "Show written summaries for coding agent replies",
      }),
    );
    expect(props4.onConfigChange).toHaveBeenCalledWith({
      agent_summary_text_enabled: false,
    });

    cleanup();
    // Disabling with grace already at 6s keeps 6s (only 20s changes to 6s)
    const props5 = renderModal(
      makeConfig({
        agent_summary_text_enabled: true,
        claude_reply_grace_seconds: 6,
      }),
    );
    fireEvent.click(
      screen.getByRole("switch", {
        name: "Show written summaries for coding agent replies",
      }),
    );
    expect(props5.onConfigChange).toHaveBeenCalledWith({
      agent_summary_text_enabled: false,
    });
  });

  it("keeps the selection light in the Voice panel, between the title and the master switch", () => {
    renderModal(makeConfig({ agent_speech_enabled: true }));

    const voice = screen.getByRole("region", { name: "Voice" });
    const header = within(voice).getByRole("heading", {
      name: "Voice",
      level: 3,
    }).parentElement?.parentElement as HTMLElement;
    // `name` disambiguates the light from `CardStatusLine`, the section's other
    // `role="status"` element (the reserved, permanently mounted status line).
    const light = within(header).getByRole("status", { name: /Agents/ });
    const master = within(header).getByRole("switch", {
      name: "Read coding agent replies aloud",
    });
    // Title, then light, then switch.
    expect(
      light.compareDocumentPosition(master) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // Exactly one light in the section proves it did not stay beside the
    // `Voice model` dropdown where it used to live.
    expect(
      within(voice).getAllByRole("status", { name: /Agents/ }),
    ).toHaveLength(1);
    expect(
      within(voice).getByRole("button", { name: /^Voice model: / }),
    ).toBeInTheDocument();
  });

  it("renders Codex then Claude Code as transparent rows under one aggregate light", () => {
    renderModal(makeConfig(), { providerResults: { codex: { ...readyAgentResult(), status: "not_installed" }, claude: { ...readyAgentResult(), status: "not_installed" } } });
    const connections = screen.getByRole("region", { name: "Connections" });
    const rows = within(connections).getAllByRole("listitem");
    expect(rows[0]).toHaveAttribute("data-agent-provider", "codex");
    expect(rows[1]).toHaveAttribute("data-agent-provider", "claude");
    expect(within(connections).getByRole("status", { name: "No agents connected" })).toBeInTheDocument();
    expect(within(connections).getByText("One connection covers the desktop app and the CLI.")).toBeInTheDocument();
    expect(document.querySelector("[style*='--surface-inset-card']")).toBeNull();
  });

  it("keeps provider failures local and the shortcut conflict in the Overlay slot", () => {
    const conflictText = "This shortcut is already used by Enter.";
    renderModal(
      makeConfig({ claude_hud_enabled: true, codex_hud_enabled: true, claude_view_shortcut: "enter" }),
      {
        providerResults: {
          codex: { ok: false, status: "failed", codex_cli_available: false, codex_cli_supported: false },
          claude: readyAgentResult(),
        },
      },
    );
    const connections = screen.getByRole("region", { name: "Connections" });
    const rows = within(connections).getAllByRole("listitem");
    expect(within(rows[0]).getByRole("alert")).toHaveTextContent("Codex isn't installed. Install it, then try again.");
    expect(within(rows[1]).queryByRole("alert")).toBeNull();
    expect(within(screen.getByRole("region", { name: "Agents" })).getByText(conflictText)).toBeInTheDocument();
    // A failed second provider is no longer hidden behind the first one's success.
    expect(within(connections).getByRole("status", { name: "Agents needs attention" })).toBeInTheDocument();
  });

  it("routes provider actions with one click and no surface argument", () => {
    const disconnected = renderModal();
    fireEvent.click(screen.getByRole("button", { name: "Connect Codex" }));
    expect(disconnected.onProviderToggle).toHaveBeenCalledWith("codex", true);
    fireEvent.click(screen.getByRole("button", { name: "Connect Claude Code" }));
    expect(disconnected.onProviderToggle).toHaveBeenCalledWith("claude", true);
    expect(screen.queryByText(/Connection details/)).toBeNull();
    cleanup();

    renderModal(makeConfig({ claude_hud_enabled: true }), {
      providerActivity: { codex: "connecting", claude: "idle" },
      providerLastConnect: { claude: "installed" },
      providerResults: {
        codex: null,
        claude: { ok: true, status: "installed", connection: "waiting",
          compatibility: { surfaces: [{ ...readyAgentResult().compatibility.surfaces[0], state: "supported", reply_verified: false }] } },
      },
    });
    expect(screen.getByText("Installing the Whisperstream plugin...")).toBeInTheDocument();
    expect(screen.getByText("Installed. Start a new Claude Code session, or run this in an open one:")).toBeInTheDocument();
    cleanup();

    const both = renderModal(makeConfig({ codex_hud_enabled: true, claude_hud_enabled: true }), {
      providerResults: { codex: { ...readyAgentResult(), trust_required: false }, claude: readyAgentResult() },
    });
    fireEvent.click(screen.getByRole("button", { name: "Disconnect Codex" }));
    expect(both.onProviderToggle).toHaveBeenCalledWith("codex", false);
    expect(screen.queryByRole("button", { name: /Update Whisperstream plugin/ })).toBeNull();
  });

  it("installs the managed-session CLI from the Connections list", async () => {
    renderModal(makeConfig({ claude_hud_enabled: true, codex_hud_enabled: true }));
    const install = await screen.findByRole("button", { name: "Install Whisperstream CLI" });
    await waitFor(() => expect(install).toBeEnabled());
    fireEvent.click(install);
    await waitFor(() => expect(api_agents.installManagedCli).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("button", { name: "Uninstall Whisperstream CLI" })).toBeInTheDocument();
    expect(screen.getByText(
      "Open a new terminal, then run whisperstream codex or whisperstream claude to start a managed session.",
    )).toBeInTheDocument();
  });

  it("uses either provider for voice availability and the generic copy", () => {
    renderModal(makeConfig({ codex_hud_enabled: true, agent_speech_enabled: true }), {
      providerResults: { codex: { ...readyAgentResult(), trust_required: false }, claude: null },
    });
    expect(screen.queryByText("Connect Codex or Claude Code in Connections so Agent Speak can listen.")).toBeNull();
    expect(screen.getByRole("switch", { name: "Read coding agent replies aloud" })).toBeInTheDocument();
  });
});

describe("HudConfigModal master Agents switch", () => {
  const connectedResults = { codex: { ...readyAgentResult(), trust_required: false }, claude: readyAgentResult() };

  it("leads General with the Agents switch and pauses through onAgentsToggle", async () => {
    const onAgentsToggle = vi.fn(async () => ({ needsAction: false }));
    renderModal(makeConfig({ codex_hud_enabled: true, claude_hud_enabled: true }), {
      embedded: true, onAgentsToggle, providerResults: connectedResults,
    });
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["General", "Voice", "Connections"]);
    const headings = screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent);
    expect(headings).toEqual(["Agents"]);
    const agents = screen.getByRole("region", { name: "Agents" });
    expect(within(agents).getByRole("status", { name: "Agents is on and ready" })).toBeInTheDocument();
    await act(async () => { fireEvent.click(within(agents).getByRole("switch", { name: "Agents" })); });
    expect(onAgentsToggle).toHaveBeenCalledWith(false);
    expect(screen.getByRole("tab", { name: "General" })).toHaveAttribute("aria-selected", "true");
  });

  it("shows the paused line, hides the light and keeps Overlay editable while off", () => {
    renderModal(makeConfig({ agents_enabled: false, codex_hud_enabled: true, claude_hud_enabled: true }), {
      embedded: true, onAgentsToggle: vi.fn(async () => ({ needsAction: false })), providerResults: connectedResults,
    });
    const agents = screen.getByRole("region", { name: "Agents" });
    expect(within(agents).getByText("Agents is paused. Turn it on to resume Codex and Claude Code.")).toBeInTheDocument();
    expect(within(agents).queryByRole("status", { name: /Agents is/ })).toBeNull();
    const toggle = within(agents).getByRole("switch", { name: "Agents" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("switch", { name: "Send replies automatically" })).toBeEnabled();
    fireEvent.click(screen.getByRole("tab", { name: "Connections" }));
    expect(screen.getByText("Agents is paused. Turn it on in General, or resume here.")).toBeInTheDocument();
    expect(screen.getAllByText("Paused")).toHaveLength(2);
  });

  it("stays on General when every remembered provider resumes connected", async () => {
    const onAgentsToggle = vi.fn(async () => ({ needsAction: false }));
    renderModal(makeConfig({ agents_enabled: false, codex_hud_enabled: true }), {
      embedded: true, onAgentsToggle, providerResults: connectedResults,
    });
    await act(async () => { fireEvent.click(screen.getByRole("switch", { name: "Agents" })); });
    expect(onAgentsToggle).toHaveBeenCalledWith(true);
    expect(screen.getByRole("tab", { name: "General" })).toHaveAttribute("aria-selected", "true");
  });

  it("switches to Connections and focuses its tab when a resumed provider needs trust", async () => {
    const onAgentsToggle = vi.fn(async () => ({ needsAction: true }));
    renderModal(makeConfig({ agents_enabled: false, codex_hud_enabled: true }), {
      embedded: true, onAgentsToggle, providerResults: connectedResults,
    });
    await act(async () => { fireEvent.click(screen.getByRole("switch", { name: "Agents" })); });
    await waitFor(() => expect(screen.getByRole("tab", { name: "Connections" })).toHaveAttribute("aria-selected", "true"));
    await waitFor(() => expect(screen.getByRole("tab", { name: "Connections" })).toHaveFocus());
  });

  it("makes the Attention light on General a button to Connections only when the user owns the step", () => {
    renderModal(makeConfig({ codex_hud_enabled: true }), {
      embedded: true, onAgentsToggle: vi.fn(async () => ({ needsAction: false })),
      providerResults: { codex: { ...readyAgentResult(), trust_required: true }, claude: null },
    });
    fireEvent.click(screen.getByRole("button", { name: "Agents needs attention, open Connections" }));
    expect(screen.getByRole("tab", { name: "Connections" })).toHaveAttribute("aria-selected", "true");
    // On Connections the light is a plain status again.
    expect(screen.queryByRole("button", { name: "Agents needs attention, open Connections" })).toBeNull();
    expect(screen.getByRole("status", { name: "Agents needs attention" })).toBeInTheDocument();
    cleanup();
    renderModal(makeConfig({ codex_hud_enabled: false }), {
      embedded: true, providerActivity: { codex: "connecting", claude: "idle" },
    });
    expect(screen.queryByRole("button", { name: "Agents needs attention, open Connections" })).toBeNull();
  });
});

// Test the optional playback UI with synthetic sample availability.
vi.mock("./agentSpeak/voiceSamples", () => ({ hasVoiceSample: () => true }));
