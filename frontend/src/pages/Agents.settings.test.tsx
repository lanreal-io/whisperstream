import { readyAgentResult } from "../test/agentCompatibility";
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
import { useState } from "react";
import { MotionConfig } from "framer-motion";
import type { AppConfig } from "../lib/api/config";
import type { SpeechStatus } from "../lib/api/speech";
import * as api_speech from "../lib/api/speech";
import * as api_agents from "../lib/api/agents";
import * as api_config from "../lib/api/config";
import type { AppEvent } from "../lib/socket/events";
import { Agents } from "./Agents";

vi.mock("../lib/api/agents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/agents")>();
  return {
    ...actual,
    enableClaudeHud: vi.fn(),
    disableClaudeHud: vi.fn(),
    getClaudeHudStatus: vi.fn(),
    enableCodexHud: vi.fn(),
    disableCodexHud: vi.fn(),
    getCodexHudStatus: vi.fn(async () => ({
      ok: true,
      status: "not_installed" as const,
      codex_cli_available: true,
      codex_cli_supported: true,
      trust_required: false,
    })),
    getManagedCliStatus: vi.fn(async () => ({
      ok: true,
      status: "not_installed" as const,
      available: true,
      installed: false,
    })),
    installManagedCli: vi.fn(),
    uninstallManagedCli: vi.fn(),
    teardownSetup: vi.fn(async () => ({ ok: true })),
    armSetupDeliveryPreview: vi.fn(async () => ({ ok: true })),
    getSetupPluginDetail: vi.fn(async () => ({
      plugin_dir: null,
      hook_definition: null,
    })),
    getSetupCodexPluginDetail: vi.fn(async () => ({
      plugin_dir: null,
      hook_definition: null,
    })),
  };
});
vi.mock("../lib/api/speech", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/speech")>();
  return {
    ...actual,
    getSpeechStatus: vi.fn(),
    getSpeechVoices: vi.fn(),
    startSpeechModelDownload: vi.fn(),
    speakSample: vi.fn(),
    stopSpeech: vi.fn(),
  };
});
vi.mock("../lib/api/models", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/models")>();
  return {
    ...actual,
    cancelDownload: vi.fn(),
  };
});
vi.mock("../lib/api/config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/config")>();
  return {
    ...actual,
    saveConfig: vi.fn(async (config) => config),
  };
});

vi.mock("@tauri-apps/plugin-opener", () => ({
  revealItemInDir: vi.fn(async () => {}),
}));

afterEach(() => {
  // Restore real timers before unmount so Motion keeps its pending frame.
  vi.useRealTimers();
  cleanup();
  vi.clearAllMocks();
});

function makeConfig(): AppConfig {
  return {
    claude_hud_enabled: false,
    codex_hud_enabled: false,
    claude_hud_show_text: true,
    // Settings tests default to completed setup. Entry tests override this.
    agent_speak_setup_completed: true,
    agent_speech_mode: "off",
    agent_speech_enabled: false,
    agent_summary_text_enabled: false,
    agent_speech_engine: "auto",
    agent_speech_voice: 1,
    agent_speech_kokoro_voice: "af_heart",
    agent_speech_speed: 1,
    agent_speech_volume: 1,
    claude_reply_grace_seconds: 6,
    claude_reply_queue_delay_seconds: 2,
    claude_reply_auto_submit: false,
    push_to_talk_shortcut: "right shift",
    toggle_shortcut: "",
    paste_last_shortcut: "ctrl+alt+v",
    paste_last_original_shortcut: "",
  } as AppConfig;
}

function makeSpeechStatus(
  overrides: Partial<SpeechStatus> = {},
): SpeechStatus {
  return {
    requested: "auto",
    preferred_engine: "kokoro",
    resolved_engine: "kokoro",
    active_engine: null,
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

describe("Agents coding-agent connection lifecycle", () => {
  it("does not revoke a saved Codex connection when trust status is pending", async () => {
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(makeSpeechStatus());
    vi.mocked(api_speech.getSpeechVoices).mockImplementation(() => new Promise<never>(() => {}));
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue({ ...readyAgentResult(), ok: true, status: "unknown", reload_required: false });
    vi.mocked(api_agents.getCodexHudStatus).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "installed",
      trust_required: true,
    });
    const onConfigChange = vi.fn();
    render(<Agents
      config={{ ...makeConfig(), codex_hud_enabled: true, codex_verified: true }}
      onConfigChange={onConfigChange}
      send={vi.fn(() => true)}
      subscribe={vi.fn(() => () => {})}
      connected={true}
      onShowStatus={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("tab", { name: "Connections" }));
    expect(await screen.findByText("Trust needed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disconnect Codex" })).toBeInTheDocument();
    expect(onConfigChange).not.toHaveBeenCalledWith({ codex_hud_enabled: false });
  });

  it("connects directly and keeps the structured result after reopening settings", async () => {
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(makeSpeechStatus());
    vi.mocked(api_speech.getSpeechVoices).mockImplementation(
      () => new Promise<never>(() => {}),
    );
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValueOnce({ ...readyAgentResult(),
      ok: true,
      status: "unknown",
      reload_required: false,
    }).mockResolvedValue({ ...readyAgentResult(), status: "installed", reload_required: true, compatibility: { surfaces: [{ ...readyAgentResult().compatibility.surfaces[0], state: "supported", reply_verified: false }] } });
    vi.mocked(api_agents.enableClaudeHud).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "installed",
      version: "1.1.0",
      reload_required: true,
    });
    const onConfigChange = vi.fn();
    function Harness() {
      const [config, setConfig] = useState(makeConfig());
      return <Agents
        config={config}
        onConfigChange={(patch) => { onConfigChange(patch); setConfig((previous) => ({ ...previous, ...patch })); }}
        send={vi.fn(() => true)}
        subscribe={() => () => {}}
        connected={true}
        onShowStatus={vi.fn()}
      />;
    }
    render(<Harness />);

    fireEvent.click(
      screen.getByRole("tab", { name: "Connections" }),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect Claude Code" })).toBeEnabled());
    fireEvent.click(
      screen.getByRole("button", { name: "Connect Claude Code" }),
    );

    await waitFor(() => {
      expect(api_agents.enableClaudeHud).toHaveBeenCalledTimes(1);
      expect(onConfigChange).toHaveBeenCalledWith({
        claude_hud_enabled: true,
      });
    });
    expect(
      await screen.findByText(
        "Installed. Start a new Claude Code session, or run this in an open one:",
      ),
    ).toBeInTheDocument();
    expect(api_agents.enableClaudeHud).toHaveBeenCalledWith({ force: true });
    expect(screen.queryByText("Enable Claude HUD")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "General" }));
    fireEvent.click(
      screen.getByRole("tab", { name: "Connections" }),
    );

    expect(
      screen.getByText(
        "Installed. Start a new Claude Code session, or run this in an open one:",
      ),
    ).toBeInTheDocument();
  });

  it("keeps Codex on while trust is pending and detects trust without another click", async () => {
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(makeSpeechStatus());
    vi.mocked(api_speech.getSpeechVoices).mockImplementation(
      () => new Promise<never>(() => {}),
    );
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "unknown",
      reload_required: false,
    });
    vi.mocked(api_agents.getCodexHudStatus)
      .mockResolvedValueOnce({ ...readyAgentResult(), ok: true, status: "not_installed", trust_required: false })
      .mockResolvedValueOnce({ ...readyAgentResult(), status: "installed", trust_required: true })
      .mockRejectedValueOnce(new Error("temporary status failure"))
      .mockResolvedValue({ ...readyAgentResult(), trust_required: false });
    vi.mocked(api_agents.enableCodexHud).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "installed",
      trust_required: true,
      codex_cli_available: true,
      codex_cli_supported: true,
    });
    const onConfigChange = vi.fn();

    const props = {
      config: makeConfig(),
      onConfigChange,
      send: vi.fn(() => true),
      subscribe: vi.fn(() => () => {}),
      connected: true,
      onShowStatus: vi.fn(),
    };
    const { rerender } = render(
      <Agents
        {...props}
      />,
    );

    fireEvent.click(
      screen.getByRole("tab", { name: "Connections" }),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect Codex" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Connect Codex" }));
    await waitFor(() =>
      expect(onConfigChange).toHaveBeenCalledWith({ codex_hud_enabled: true }),
    );
    // Only provider polling uses fake time. Faking requestAnimationFrame can
    // strand Motion's shared loop and hide later download-error transitions.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    rerender(<Agents {...props} config={{ ...props.config, codex_hud_enabled: true }} />);
    expect(screen.getByText("Trust needed")).toBeInTheDocument();
    expect(screen.getByText("This row updates on its own.")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(api_agents.getCodexHudStatus).toHaveBeenCalledTimes(3);
    expect(screen.getByText("Trust needed")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(api_agents.getCodexHudStatus).toHaveBeenCalledTimes(4);
    expect(within(document.querySelector('[data-agent-provider="codex"]') as HTMLElement).getByText("Connected")).toBeInTheDocument();
    expect(onConfigChange).not.toHaveBeenCalledWith({ codex_hud_enabled: false });
  });

  it("aborts a background status read before installation and rejects its late trust receipt", async () => {
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(makeSpeechStatus());
    vi.mocked(api_speech.getSpeechVoices).mockImplementation(() => new Promise<never>(() => {}));
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue(readyAgentResult());
    let finishOldRead!: (value: Awaited<ReturnType<typeof api_agents.getCodexHudStatus>>) => void;
    let oldSignal: AbortSignal | undefined;
    vi.mocked(api_agents.getCodexHudStatus)
      .mockResolvedValueOnce({ ...readyAgentResult(), status: "not_installed" })
      .mockImplementationOnce((_refresh, signal) => {
        oldSignal = signal;
        return new Promise((resolve) => { finishOldRead = resolve; });
      })
      .mockResolvedValue(readyAgentResult());
    vi.mocked(api_agents.enableCodexHud).mockResolvedValue(readyAgentResult());
    const props = {
      config: makeConfig(), onConfigChange: vi.fn(), send: vi.fn(() => true),
      subscribe: vi.fn(() => () => {}), connected: true, onShowStatus: vi.fn(),
    };
    const { rerender } = render(<Agents {...props} />);
    fireEvent.click(screen.getByRole("tab", { name: "Connections" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect Codex" })).toBeEnabled());
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    expect(api_agents.getCodexHudStatus).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Connect Codex" }));
    expect(oldSignal?.aborted).toBe(true);
    await waitFor(() => expect(api_agents.getCodexHudStatus).toHaveBeenCalledTimes(3));
    rerender(<Agents {...props} config={{ ...props.config, codex_hud_enabled: true }} />);
    await act(async () => { finishOldRead({ ...readyAgentResult(), status: "installed", trust_required: true }); });
    expect(within(document.querySelector('[data-agent-provider="codex"]') as HTMLElement).getByText("Connected")).toBeInTheDocument();
    expect(screen.queryByText("Trust needed")).not.toBeInTheDocument();
  });

  it("installs in one click with force and keeps recheck read-only", async () => {
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(makeSpeechStatus());
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue(readyAgentResult());
    vi.mocked(api_agents.getCodexHudStatus).mockResolvedValue(readyAgentResult());
    vi.mocked(api_agents.enableCodexHud).mockResolvedValue(readyAgentResult());
    const onCommitConfig = vi.fn<(_patch: Partial<AppConfig>) => Promise<void>>().mockResolvedValue(undefined);
    render(<Agents config={makeConfig()} onConfigChange={vi.fn()} onCommitConfig={onCommitConfig}
      send={() => true} subscribe={() => () => {}} connected onShowStatus={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Connections" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect Codex" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Connect Codex" }));
    await waitFor(() => expect(onCommitConfig).toHaveBeenCalledWith({ codex_hud_enabled: true }));
    expect(onCommitConfig.mock.calls[0][0]).toEqual({ codex_hud_enabled: true });
    expect(api_agents.enableCodexHud).toHaveBeenCalledWith({ force: true });
    await waitFor(() => expect(screen.getByRole("button", { name: "Check connections again" })).toBeEnabled());
    vi.mocked(api_agents.enableCodexHud).mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Check connections again" }));
    await waitFor(() => expect(api_agents.getCodexHudStatus).toHaveBeenCalledWith(true));
    expect(api_agents.getClaudeHudStatus).toHaveBeenCalledWith(true);
    expect(api_agents.enableCodexHud).not.toHaveBeenCalled();
    expect(onCommitConfig).toHaveBeenCalledTimes(1);
  });

  it("reports a connection that could not be saved", async () => {
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(makeSpeechStatus());
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue(readyAgentResult());
    vi.mocked(api_agents.getCodexHudStatus).mockResolvedValue(readyAgentResult());
    vi.mocked(api_agents.enableCodexHud).mockResolvedValue(readyAgentResult());
    const onShowStatus = vi.fn();
    render(<Agents config={makeConfig()} onConfigChange={vi.fn()}
      onCommitConfig={vi.fn(async () => { throw new Error("save failed"); })}
      send={() => true} subscribe={() => () => {}} connected onShowStatus={onShowStatus} />);
    fireEvent.click(screen.getByRole("tab", { name: "Connections" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect Codex" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Connect Codex" }));
    await waitFor(() => expect(onShowStatus).toHaveBeenCalledWith("Couldn't connect Codex. Try again.", true));
  });

  it("disconnects one provider without clearing the other", async () => {
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(makeSpeechStatus());
    vi.mocked(api_speech.getSpeechVoices).mockImplementation(
      () => new Promise<never>(() => {}),
    );
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "current",
      reload_required: false,
    });
    vi.mocked(api_agents.getCodexHudStatus).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "current",
      trust_required: false,
      codex_cli_available: true,
      codex_cli_supported: true,
    });
    vi.mocked(api_agents.disableCodexHud).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "removed",
      trust_required: false,
    });
    const onConfigChange = vi.fn();

    render(
      <Agents
        config={{
          ...makeConfig(),
          codex_hud_enabled: true,
          claude_hud_enabled: true,
        }}
        onConfigChange={onConfigChange}
        send={vi.fn(() => true)}
        subscribe={vi.fn(() => () => {})}
        connected={true}
        onShowStatus={vi.fn()}
      />,
    );

    fireEvent.click(
      screen.getByRole("tab", { name: "Connections" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Disconnect Codex" }),
    );
    await waitFor(() =>
      expect(onConfigChange).toHaveBeenCalledWith({ codex_hud_enabled: false }),
    );
    expect(onConfigChange).not.toHaveBeenCalledWith({
      claude_hud_enabled: false,
    });
  });
});

describe("Agents adaptive voice orchestration", () => {
  it("selects a grouped Supertonic alias through the production modal using its numeric ID", async () => {
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(
      makeSpeechStatus({
        requested: "supertonic",
        preferred_engine: "supertonic",
        resolved_engine: "supertonic",
        active_engine: "supertonic",
        reason: "supertonic",
      }),
    );
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "current",
      reload_required: false,
    });
    vi.mocked(api_speech.getSpeechVoices).mockResolvedValue({
      engine: "supertonic",
      engine_label: "Supertonic",
      voices: [
        {
          id: 2,
          sid: 2,
          label: "Claire",
          group: "female",
          reference_label: "Voice 2",
        },
        {
          id: 7,
          sid: 7,
          label: "Henry",
          group: "male",
          reference_label: "Voice 7",
        },
      ],
    });
    const onConfigChange = vi.fn();

    render(
      <Agents
        config={{
          ...makeConfig(),
          agent_speech_enabled: true,
          agent_speech_engine: "supertonic",
          agent_speech_voice: 2,
        }}
        onConfigChange={onConfigChange}
        send={vi.fn(() => true)}
        subscribe={vi.fn(() => () => {})}
        connected={true}
        onShowStatus={vi.fn()}
      />,
    );

    fireEvent.click(
      screen.getByRole("tab", { name: "Voice" }),
    );
    // The picker rests collapsed; the trigger is the selection readout.
    fireEvent.click(await screen.findByRole("button", { name: "Voice: Claire" }));
    fireEvent.click(screen.getByRole("option", { name: "Henry" }));

    const configCalls = onConfigChange.mock.calls;
    const selectedVoicePatch = configCalls[configCalls.length - 1]?.[0];
    expect(selectedVoicePatch).toEqual({
      agent_speech_voice: 7,
    });
    expect(typeof selectedVoicePatch.agent_speech_voice).toBe("number");
  });

  it("initializes the speaking state from an already-active backend engine", async () => {
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(
      makeSpeechStatus({
        preferred_engine: "kokoro",
        resolved_engine: "kokoro",
        active_engine: "supertonic",
        reason: "kokoro",
      }),
    );
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "current",
      reload_required: false,
    });
    vi.mocked(api_speech.getSpeechVoices).mockResolvedValue({
      engine: "supertonic",
      engine_label: "Supertonic",
      voices: [
        {
          id: 1,
          sid: 1,
          label: "Brianna",
          group: "female",
          reference_label: "Voice 1",
        },
      ],
    });

    render(
      <Agents
        config={{
          ...makeConfig(),
          agent_speech_enabled: true,
          agent_speech_engine: "auto",
        }}
        onConfigChange={vi.fn()}
        send={vi.fn(() => true)}
        subscribe={vi.fn(() => () => {})}
        connected={true}
        onShowStatus={vi.fn()}
      />,
    );

    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    fireEvent.click(
      screen.getByRole("tab", { name: "Voice" }),
    );
    expect(screen.queryByText(/Using Supertonic for this reply/)).not.toBeInTheDocument();
    // The visible engine still follows the actively speaking one, which the
    // Supertonic-only roster is what proves.
    expect(
      await screen.findByRole("button", { name: "Voice: Brianna" }),
    ).toBeInTheDocument();
  });

  it("ignores an older status response after a newer speech event refresh", async () => {
    let resolveInitial!: (status: SpeechStatus) => void;
    let resolveLive!: (status: SpeechStatus) => void;
    vi.mocked(api_speech.getSpeechStatus)
      .mockImplementationOnce(
        () =>
          new Promise<SpeechStatus>((resolve) => {
            resolveInitial = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise<SpeechStatus>((resolve) => {
            resolveLive = resolve;
          }),
      );
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "current",
      reload_required: false,
    });
    vi.mocked(api_speech.getSpeechVoices).mockResolvedValue({
      engine: "supertonic",
      engine_label: "Supertonic",
      voices: [
        {
          id: 1,
          sid: 1,
          label: "Brianna",
          group: "female",
          reference_label: "Voice 1",
        },
      ],
    });
    const handlers: Array<(event: AppEvent) => void> = [];
    const subscribe = vi.fn((handler: (event: AppEvent) => void) => {
      handlers.push(handler);
      return () => {};
    });

    render(
      <Agents
        config={{
          ...makeConfig(),
          agent_speech_enabled: true,
          agent_speech_engine: "auto",
        }}
        onConfigChange={vi.fn()}
        send={vi.fn(() => true)}
        subscribe={subscribe}
        connected={true}
        onShowStatus={vi.fn()}
      />,
    );
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(1));

    await act(async () => {
      const event = {
        type: "speech.state",
        state: "speaking",
        engine: "supertonic",
      } as AppEvent;
      for (const handler of handlers) handler(event);
    });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(2));

    fireEvent.click(
      screen.getByRole("tab", { name: "Voice" }),
    );
    expect(screen.queryByText(/Using Supertonic for this reply/)).not.toBeInTheDocument();
    await screen.findByRole("button", { name: "Voice: Brianna" });

    await act(async () => {
      resolveLive(
        makeSpeechStatus({
          resolved_engine: "kokoro",
          active_engine: "supertonic",
        }),
      );
    });
    await act(async () => {
      resolveInitial(makeSpeechStatus({ active_engine: null }));
    });

    expect(screen.queryByText(/Using Supertonic for this reply/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Voice: Brianna" }),
    ).toBeInTheDocument();
  });

  it("refreshes authoritative selection on engine-bearing and idle speech events", async () => {
    const fallback = makeSpeechStatus({
      preferred_engine: "kokoro",
      resolved_engine: "supertonic",
      active_engine: "supertonic",
      reason: "unhealthy_kokoro",
    });
    let resolveFallback!: (status: SpeechStatus) => void;
    vi.mocked(api_speech.getSpeechStatus)
      .mockResolvedValueOnce(makeSpeechStatus())
      .mockImplementationOnce(
        () =>
          new Promise<SpeechStatus>((resolve) => {
            resolveFallback = resolve;
          }),
      )
      .mockResolvedValue(fallback);
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "current",
      reload_required: false,
    });
    vi.mocked(api_speech.getSpeechVoices).mockResolvedValue({
      engine: "kokoro",
      engine_label: "Kokoro",
      voices: [
        {
          id: "af_heart",
          sid: "af_heart",
          label: "Heart",
          group: "female",
        },
      ],
    });
    const handlers: Array<(event: AppEvent) => void> = [];
    const subscribe = vi.fn((handler: (event: AppEvent) => void) => {
      handlers.push(handler);
      return () => {};
    });

    render(
      <Agents
        config={{
          ...makeConfig(),
          agent_speech_enabled: true,
          agent_speech_engine: "auto",
        }}
        onConfigChange={vi.fn()}
        send={vi.fn(() => true)}
        subscribe={subscribe}
        connected={true}
        onShowStatus={vi.fn()}
      />,
    );

    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(1));
    fireEvent.click(
      screen.getByRole("tab", { name: "Voice" }),
    );
    expect(screen.queryByText(/Using Kokoro/)).not.toBeInTheDocument();

    const beforeEngineEvent = vi.mocked(api_speech.getSpeechStatus).mock.calls.length;
    await act(async () => {
      const event = {
        type: "speech.state",
        state: "speaking",
        engine: "supertonic",
      } as unknown as AppEvent;
      for (const handler of handlers) handler(event);
    });
    await waitFor(() =>
      expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(beforeEngineEvent + 1),
    );
    expect(screen.queryByText(/Using Supertonic for this reply/)).not.toBeInTheDocument();

    await act(async () => {
      resolveFallback(fallback);
    });
    // Automatic stays the visible choice; the fallback only changes which
    // model it speaks with.
    expect(
      screen.getByRole("button", { name: "Voice model: Automatic" }),
    ).toBeInTheDocument();

    const beforeHealthEvent = vi.mocked(api_speech.getSpeechStatus).mock.calls.length;
    await act(async () => {
      const event = {
        type: "speech.engine_health",
        engine: "kokoro",
        healthy: false,
        reason: "load_failed",
      } as AppEvent;
      for (const handler of handlers) handler(event);
    });
    await waitFor(() =>
      expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(beforeHealthEvent + 1),
    );

    const beforeIdleEvent = vi.mocked(api_speech.getSpeechStatus).mock.calls.length;
    await act(async () => {
      const event = {
        type: "speech.state",
        state: "idle",
      } as AppEvent;
      for (const handler of handlers) handler(event);
    });
    await waitFor(() =>
      expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(beforeIdleEvent + 1),
    );
  });

  it("downloads only the detected high-memory model on first enable", async () => {
    const missing = makeSpeechStatus({
      resolved_engine: null,
      active_engine: null,
      reason: "missing_kokoro",
      engines: {
        ...makeSpeechStatus().engines,
        kokoro: {
          ...makeSpeechStatus().engines.kokoro,
          installed: false,
        },
      },
      recommended_downloads: ["kokoro"],
    });
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(missing);
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "current",
      reload_required: false,
    });
    vi.mocked(api_speech.startSpeechModelDownload).mockResolvedValue();
    vi.mocked(api_speech.getSpeechVoices).mockResolvedValue({
      engine: "kokoro",
      engine_label: "Kokoro",
      voices: [
        {
          id: "af_heart",
          sid: "af_heart",
          label: "Heart",
          group: "female",
        },
      ],
    });
    const handlers: Array<(event: AppEvent) => void> = [];
    const subscribe = vi.fn((handler: (event: AppEvent) => void) => {
      handlers.push(handler);
      return () => {};
    });

    render(
      <Agents
        config={makeConfig()}
        onConfigChange={vi.fn()}
        send={vi.fn(() => true)}
        subscribe={subscribe}
        connected={true}
        onShowStatus={vi.fn()}
      />,
    );

    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    fireEvent.click(
      screen.getByRole("tab", { name: "Voice" }),
    );
    fireEvent.click(
      screen.getByRole("switch", { name: "Read coding agent replies aloud" }),
    );
    expect(
      await screen.findByRole("heading", { name: "Download this model?" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Kokoro is about 345 MB/)).toBeInTheDocument();
    expect(api_speech.startSpeechModelDownload).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    await waitFor(() =>
      expect(api_speech.startSpeechModelDownload).toHaveBeenNthCalledWith(
        1,
        "kokoro",
      ),
    );
    expect(api_speech.startSpeechModelDownload).toHaveBeenCalledTimes(1);
  });

  it("previews the visible voice inline without any backend sample call", async () => {
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(
      makeSpeechStatus({
        requested: "kokoro",
        resolved_engine: "kokoro",
        active_engine: null,
      }),
    );
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "current",
      reload_required: false,
    });
    vi.mocked(api_speech.getSpeechVoices).mockResolvedValue({
      engine: "kokoro",
      engine_label: "Kokoro",
      voices: [
        {
          id: "af_heart",
          sid: "af_heart",
          label: "Heart",
          group: "female",
        },
      ],
    });

    render(
      <Agents
        config={{
          ...makeConfig(),
          agent_speech_enabled: true,
          agent_speech_engine: "kokoro",
          agent_speech_kokoro_voice: "af_heart",
        }}
        onConfigChange={vi.fn()}
        send={vi.fn(() => true)}
        subscribe={vi.fn(() => () => {})}
        connected={true}
        onShowStatus={vi.fn()}
      />,
    );

    fireEvent.click(
      screen.getByRole("tab", { name: "Voice" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Voice: Heart" }));
    // Row-scoped: the collapsed-trigger disc also previews the selected voice
    // (Heart), so click the row transport inside the open panel.
    const voiceList = screen.getByRole("listbox", { name: "Voice" });
    fireEvent.click(
      within(voiceList).getByRole("button", { name: "Play Heart sample" }),
    );
    // Bundled clip, played in the frontend: the modal no longer calls the
    // backend sample endpoints at all.
    expect(api_speech.speakSample).not.toHaveBeenCalled();
    expect(api_speech.stopSpeech).not.toHaveBeenCalled();
  });

  it("surfaces an explicit-model start failure in the shared dialog", async () => {
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(
      makeSpeechStatus({
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
    );
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue({ ...readyAgentResult(),
      ok: true,
      status: "current",
      reload_required: false,
    });
    vi.mocked(api_speech.getSpeechVoices).mockResolvedValue({
      engine: "supertonic",
      engine_label: "Supertonic",
      voices: [
        {
          id: 1,
          sid: 1,
          label: "Brianna",
          group: "female",
          reference_label: "Voice 1",
        },
      ],
    });
    let rejectDownload!: (reason: Error) => void;
    vi.mocked(api_speech.startSpeechModelDownload).mockImplementation(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectDownload = reject;
        }),
    );

    const onConfigChange = vi.fn();
    render(
      // Exercise download state without Motion's asynchronous exit lifecycle.
      <MotionConfig isStatic>
        <Agents
          config={{
            ...makeConfig(),
            agent_speech_enabled: true,
            agent_speech_engine: "kokoro",
          }}
          onConfigChange={onConfigChange}
          send={vi.fn(() => true)}
          subscribe={vi.fn(() => () => {})}
          connected={true}
          onShowStatus={vi.fn()}
        />
      </MotionConfig>,
    );

    fireEvent.click(
      screen.getByRole("tab", { name: "Voice" }),
    );
    await screen.findByRole("button", { name: "Voice model: Higher quality" });
    fireEvent.click(
      screen.getByRole("button", { name: "Voice model: Higher quality" }),
    );
    fireEvent.click(screen.getByRole("option", { name: "Lower memory" }));
    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    expect(
      await screen.findByRole(
        "heading",
        { name: "Downloading Supertonic" },
      ),
    ).toBeInTheDocument();
    await act(async () => {
      rejectDownload(new Error("500: unavailable"));
    });

    expect(
      await screen.findByRole(
        "heading",
        { name: "Download failed" },
      ),
    ).toBeInTheDocument();
    expect(api_speech.startSpeechModelDownload).toHaveBeenCalledWith("supertonic");
    expect(screen.getByText("500: unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Downloading Supertonic" }),
    ).not.toBeInTheDocument();
    expect(onConfigChange).not.toHaveBeenCalledWith({
      agent_speech_engine: "supertonic",
    });
  });
});

describe("Agents page entry", () => {
  beforeEach(() => {
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(makeSpeechStatus());
    vi.mocked(api_speech.getSpeechVoices).mockResolvedValue({ engine: "supertonic", voices: [] } as never);
    vi.mocked(api_agents.getClaudeHudStatus).mockResolvedValue({ ...readyAgentResult(), ok: true, status: "unknown", reload_required: false });
    vi.mocked(api_agents.getCodexHudStatus).mockResolvedValue({ ...readyAgentResult(), ok: true, status: "not_installed", trust_required: false });
    vi.mocked(api_config.saveConfig).mockImplementation(async (config) => config as AppConfig);
  });

  function renderEntry(overrides: Partial<AppConfig> = {}, connected = true) {
    const onConfigChange = vi.fn();
    const props = {
      config: { ...makeConfig(), agent_speak_setup_completed: false, ...overrides },
      onConfigChange,
      send: vi.fn(() => true),
      subscribe: () => () => {},
      connected,
      onShowStatus: vi.fn(),
    };
    return { ...render(<MotionConfig skipAnimations><Agents {...props} /></MotionConfig>), props };
  }

  it("shows only the introduction and opens setup without enabling anything", async () => {
    const { props } = renderEntry();
    expect(screen.getByRole("heading", { name: "Read, listen, and reply" })).toBeInTheDocument();
    expect(screen.getByText("Hear or read responses from Claude Code and Codex while you work in other apps. Speak or type to reply.")).toBeInTheDocument();
    expect(screen.queryAllByRole("tab", { hidden: true })).toHaveLength(0);
    expect(screen.queryByText("View full response")).not.toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Enable Agents" }));
    expect(await screen.findByRole("dialog", { name: "Set up Agents" })).toBeInTheDocument();
    expect(screen.getByText("Step 1 of 4")).toBeInTheDocument();
    expect(api_agents.enableClaudeHud).not.toHaveBeenCalled();
    expect(api_agents.enableCodexHud).not.toHaveBeenCalled();
    expect(api_speech.startSpeechModelDownload).not.toHaveBeenCalled();
    expect(props.onConfigChange).not.toHaveBeenCalled();
  });

  it("keeps settings absent after an unfinished close and reopens at step one", async () => {
    const { props } = renderEntry({ claude_hud_enabled: true });
    fireEvent.click(screen.getByRole("button", { name: "Continue setup" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByRole("heading", { name: "Choose how replies appear" });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryAllByRole("tab", { hidden: true })).toHaveLength(0);
    expect(api_config.saveConfig).not.toHaveBeenCalled();
    expect(props.onConfigChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Continue setup" }));
    expect(await screen.findByRole("heading", { name: "Connect your coding agents" })).toBeInTheDocument();
    expect(screen.getByText("Step 1 of 4")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("button", { name: "Continue setup" })).toBeInTheDocument();
  });

  it.each([
    ["Claude", true, false],
    ["Codex", false, true],
    ["both", true, true],
  ] as const)("shows only the continuation card when %s is configured", (_label, claude, codex) => {
    renderEntry({ claude_hud_enabled: claude, codex_hud_enabled: codex });
    expect(screen.getByRole("button", { name: "Continue setup" })).toBeInTheDocument();
    expect(screen.getByText("Review your connections and finish setting up Agents.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Read, listen, and reply" })).toBeNull();
    expect(screen.queryAllByRole("tab", { hidden: true })).toHaveLength(0);
    expect(screen.queryByText("View full response")).not.toBeInTheDocument();
  });

  it("keeps configured Codex on the continuation card while trust is pending", async () => {
    vi.mocked(api_agents.getCodexHudStatus).mockResolvedValue({ ...readyAgentResult(), ok: true, status: "installed", trust_required: true });
    renderEntry({ codex_hud_enabled: true });
    fireEvent.click(screen.getByRole("button", { name: "Continue setup" }));
    expect(await screen.findByText("Trust needed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    expect(screen.queryAllByRole("tab", { hidden: true })).toHaveLength(0);
  });

  it.each([
    ["neither", false, false],
    ["Claude", true, false],
    ["Codex", false, true],
    ["both", true, true],
  ] as const)("shows settings after completion with %s provider enabled", (_label, claude, codex) => {
    renderEntry({ agent_speak_setup_completed: true, claude_hud_enabled: claude, codex_hud_enabled: codex });
    expect(screen.getByRole("tab", { name: "General" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Voice" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Connections" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^(Enable Agents|Continue setup)$/ })).toBeNull();
  });

  it.each([false, true])("disables incomplete setup offline and recovers on reconnect (configured=%s)", (configured) => {
    const { props, rerender } = renderEntry({ claude_hud_enabled: configured }, false);
    const label = configured ? "Continue setup" : "Enable Agents";
    const button = screen.getByRole("button", { name: label });
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription("Whisperstream is reconnecting. Setup will be available shortly.");
    fireEvent.click(button);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryAllByRole("tab", { hidden: true })).toHaveLength(0);
    rerender(<MotionConfig skipAnimations><Agents {...props} connected /></MotionConfig>);
    expect(screen.getByRole("button", { name: label })).toBeEnabled();
    expect(screen.queryByText("Whisperstream is reconnecting. Setup will be available shortly.")).toBeNull();
  });

  it("reveals settings only after the existing setup saves successfully", async () => {
    let resolveSave!: (value: AppConfig) => void;
    vi.mocked(api_config.saveConfig).mockImplementation(() => new Promise<AppConfig>((resolve) => { resolveSave = resolve; }));
    const initial = { ...makeConfig(), claude_hud_enabled: true, agent_speak_setup_completed: false };
    const subscribe = () => () => {};
    function Page() {
      const [config, setConfig] = useState(initial);
      return <Agents config={config} onConfigChange={(patch) => setConfig((current) => ({ ...current, ...patch }))} send={() => true} subscribe={subscribe} connected onShowStatus={() => {}} />;
    }
    render(<MotionConfig skipAnimations><Page /></MotionConfig>);
    fireEvent.click(screen.getByRole("button", { name: "Continue setup" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    for (let i = 0; i < 3; i += 1) {
      fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    }
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(api_config.saveConfig).toHaveBeenCalledWith({
      agent_speak_setup_completed: true,
      agent_summary_text_enabled: true,
      agent_speech_enabled: true,
      claude_reply_grace_seconds: 20,
    });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryAllByRole("tab", { hidden: true })).toHaveLength(0);
    await act(async () => resolveSave({ ...initial, ...vi.mocked(api_config.saveConfig).mock.calls[0][0] }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("tab", { name: "General" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue setup" })).toBeNull();
  });
});

// Test the optional playback UI with synthetic sample availability.
vi.mock("../components/agentSpeak/voiceSamples", () => ({ hasVoiceSample: () => true }));
