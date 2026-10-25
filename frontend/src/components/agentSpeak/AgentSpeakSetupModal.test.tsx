import { readyAgentResult } from "../../test/agentCompatibility";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api_config from "../../lib/api/config";
import * as api_agents from "../../lib/api/agents";
import * as api_speech from "../../lib/api/speech";
import type { AppConfig } from "../../lib/api/config";
import type { SpeechStatus } from "../../lib/api/speech";
import {
  AgentSpeakSetupModal,
  STEP_BODY_FLOOR_H,
  type AgentSpeakSetupModalProps,
} from "./AgentSpeakSetupModal";

vi.mock("@tauri-apps/plugin-opener", () => ({
  revealItemInDir: vi.fn(async () => {}),
}));

vi.mock("../../lib/api/config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/config")>();
  return {
    ...actual,
    saveConfig: vi.fn(async (config: Partial<AppConfig>) => config as AppConfig),
  };
});
vi.mock("../../lib/api/agents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/agents")>();
  return {
    ...actual,
    teardownSetup: vi.fn(async () => ({ ok: true })),
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
vi.mock("../../lib/api/speech", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/speech")>();
  return {
    ...actual,
    getSpeechVoices: vi.fn(async () => ({
      engine: "supertonic" as const,
      engine_label: "Supertonic",
      voices: [
        { id: 1, sid: 1, label: "Brianna", group: "female" as const },
      ],
    })),
    getSpeechStatus: vi.fn(),
  };
});

function makeConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    // Step 1 gates Continue on the plugin being connected (design decision 10),
    // so the shell's step-machine tests start from a connected config.
    claude_hud_enabled: true,
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
    push_to_talk_shortcut: "right shift",
    toggle_shortcut: "",
    ...overrides,
  } as AppConfig;
}

/** Neither engine installed, so step 2 can raise the download-confirm dialog. */
function makeSpeechStatus(): SpeechStatus {
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
        label: "Kokoro",
        installed: false,
        downloading: false,
        download_bytes: 340_000_000,
        installed_bytes: 360_000_000,
      },
      supertonic: {
        key: "supertonic",
        label: "Supertonic",
        installed: false,
        downloading: false,
        download_bytes: 90_000_000,
        installed_bytes: 96_000_000,
      },
    },
    current_download: null,
    recommended_downloads: ["supertonic"],
  };
}

function makeInstalledSpeechStatus(): SpeechStatus {
  const status = makeSpeechStatus();
  status.engines.supertonic.installed = true;
  status.recommended_downloads = [];
  return status;
}

function renderModal(overrides: Partial<AgentSpeakSetupModalProps> = {}) {
  const props: AgentSpeakSetupModalProps = {
    open: true,
    onClose: vi.fn(),
    onFinish: vi.fn(),
    config: makeConfig(),
    providerResults: { codex: null, claude: null },
    onProviderToggle: vi.fn(),
    onConfigChange: vi.fn(),
    connected: true,
    send: vi.fn(() => true),
    subscribe: vi.fn(() => () => {}),
    speechStatus: makeSpeechStatus(),
    speechPlaying: false,
    onRefreshSpeechStatus: vi.fn(async () => null),
    onDownload: vi.fn(async () => {}),
    onCancelDownload: vi.fn(async () => {}),
    ...overrides,
  };
  props.providerResults = {
    codex: { ...readyAgentResult(), ...props.providerResults.codex },
    claude: { ...readyAgentResult(), ...props.providerResults.claude },
  };
  return { ...render(<AgentSpeakSetupModal {...props} />), props };
}

function card(): HTMLElement {
  return screen.getByRole("dialog");
}

/** Advance the shell one step by pressing the footer primary. */
function pressPrimary() {
  fireEvent.click(
    screen.getByRole("button", { name: /^(Continue|Done)$/ }),
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

beforeEach(() => {
  vi.mocked(api_config.saveConfig).mockImplementation(
    async (config) => config as AppConfig,
  );
  vi.mocked(api_agents.teardownSetup).mockResolvedValue({ ok: true });
  vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(makeInstalledSpeechStatus());
});

describe("AgentSpeakSetupModal shell", () => {
  it.each([
    ["neither", false, false, null, null, false],
    ["trusted Codex only", true, false, { ok: true, status: "current", trust_required: false }, null, true],
    ["Claude only", false, true, null, { ok: true, status: "installed" }, true],
    ["both", true, true, null, null, true],
    ["untrusted Codex only", true, false, { ok: true, status: "installed", trust_required: true }, null, false],
    ["Claude with untrusted Codex", false, true, { ok: true, status: "installed", trust_required: true }, null, true],
    ["Codex with Claude unavailable", true, false, null, { ok: false, status: "failed" }, true],
    ["Claude with Codex unavailable", false, true, { ok: false, status: "failed" }, null, true],
  ] as const)("requires only one usable provider: %s", async (_label, codexEnabled, claudeEnabled, codex, claude, allowed) => {
    renderModal({
      config: makeConfig({ codex_hud_enabled: codexEnabled, claude_hud_enabled: claudeEnabled }),
      providerResults: { codex, claude },
    });
    const button = screen.getByRole("button", { name: "Continue" });
    await waitFor(() => expect(button).toHaveProperty("disabled", !allowed));
    fireEvent.click(button);
    expect(screen.getByText(allowed ? "Step 2 of 4" : "Step 1 of 4")).toBeInTheDocument();
  });

  it("connects in one click from setup and explains a blocked Continue", () => {
    const { props } = renderModal({ config: makeConfig({ claude_hud_enabled: false }) });
    expect(screen.getByRole("tooltip")).toHaveTextContent("Connect one agent to continue.");
    fireEvent.click(screen.getByRole("button", { name: "Connect Claude Code" }));
    expect(props.onProviderToggle).toHaveBeenCalledWith("claude", true);
  });

  it("explains pending trust on Continue and unlocks when trust appears", async () => {
    const { props, rerender } = renderModal({
      config: makeConfig({ codex_hud_enabled: true, claude_hud_enabled: false }),
      providerResults: { codex: { ok: true, status: "installed", trust_required: true }, claude: null },
    });
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent("Finish connecting one agent to continue.");
    expect(screen.getByLabelText("Continue unavailable")).toHaveAttribute("aria-describedby", tooltip.id);
    expect(screen.queryByRole("button", { name: "Check connection for Codex" })).toBeNull();
    rerender(<AgentSpeakSetupModal {...props}
      config={makeConfig({ codex_hud_enabled: true, claude_hud_enabled: false })}
      providerResults={{ codex: { ...readyAgentResult(), trust_required: false }, claude: null }} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    expect(screen.queryByRole("tooltip")).toBeNull();
    pressPrimary();
    expect(screen.getByText("Step 2 of 4")).toBeInTheDocument();
  });

  it("renders nothing while closed", () => {
    renderModal({ open: false });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps the title constant across all four steps", () => {
    renderModal();
    for (const expected of ["Step 1 of 4", "Step 2 of 4", "Step 3 of 4"]) {
      expect(screen.getByText(expected)).toBeInTheDocument();
      expect(screen.getByText("Set up Agents")).toBeInTheDocument();
      pressPrimary();
    }
    expect(screen.getByText("Step 4 of 4")).toBeInTheDocument();
    expect(screen.getByText("Set up Agents")).toBeInTheDocument();
  });

  it("labels the footer primary Continue, Continue, Continue, Done", () => {
    renderModal();
    for (let i = 0; i < 3; i += 1) {
      expect(
        screen.getByRole("button", { name: "Continue" }),
      ).toBeInTheDocument();
      pressPrimary();
    }
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
  });

  it.each([
    {
      label: "Codex only",
      config: makeConfig({ codex_hud_enabled: true, claude_hud_enabled: false }),
      heading: "Try it with Codex",
    },
    {
      // Both connected no longer pre-commits to Codex: the run stays unresolved
      // and shows the agent-agnostic heading until a real completion binds it to
      // whoever actually answers (scope.md #7).
      label: "both providers",
      config: makeConfig({ codex_hud_enabled: true, claude_hud_enabled: true }),
      heading: "Try it with your coding agent",
    },
    {
      label: "Claude Code only",
      config: makeConfig({ codex_hud_enabled: false, claude_hud_enabled: true }),
      heading: "Try it with Claude Code",
    },
  ])(
    "resolves the step-4 provider for $label",
    async ({ config, heading }) => {
      const installed = makeInstalledSpeechStatus();
      renderModal({ config, speechStatus: installed });
      for (let i = 0; i < 3; i += 1) pressPrimary();

      expect(
        await screen.findByRole("heading", { name: heading, level: 2 }),
      ).toBeInTheDocument();
    },
  );

  it("has no Back control on any step", () => {
    renderModal();
    for (let step = 1; step <= 4; step += 1) {
      expect(screen.queryByRole("button", { name: /back/i })).toBeNull();
      if (step < 4) pressPrimary();
    }
  });

  it("always renders the reserved left footer slot, empty on steps 1 to 3", () => {
    renderModal();
    for (let step = 1; step <= 3; step += 1) {
      const slot = card().querySelector("[data-setup-footer-slot]");
      expect(slot).not.toBeNull();
      expect(slot?.textContent).toBe("");
      pressPrimary();
    }
    expect(card().querySelector("[data-setup-footer-slot]")).not.toBeNull();
  });

  it("floors the step body at 240px and never clips the card", () => {
    renderModal();
    const body = card().querySelector<HTMLElement>("div.relative.mt-4");
    expect(body?.style.minHeight).toBe(`${STEP_BODY_FLOOR_H}px`);
    // The card may never gain an overflow container: `HoverTooltip` is
    // portal-less CSS and an overflow ancestor clips it. The 16px slide is
    // what makes that affordable.
    expect(card().className).not.toContain("overflow-hidden");
    expect(card().className).not.toContain("overflow-y-auto");
  });

  it("gives the step body a floor and no maximum", () => {
    renderModal();
    const body = card().querySelector<HTMLElement>("div.relative.mt-4");
    expect(body).not.toBeNull();
    // Floor plus grow: 240 is near the MEDIAN step, not the tallest, and a
    // taller step is meant to grow the card rather than be reserved for.
    expect(body?.style.minHeight).toBe("240px");
    // No ceiling of any kind. A step that outgrows the card is a
    // step-splitting problem, never a height or a scroller problem.
    expect(body?.style.maxHeight).toBe("");
    expect(body?.style.height).not.toBe("240px");
    expect(body?.className).not.toContain("overflow");
  });

  it("moves focus to the step's h2 heading, on mount and on every step change", async () => {
    renderModal();
    await waitFor(() => expect(document.activeElement?.tagName).toBe("H2"));
    // The step's job, not the modal's constant identity line.
    expect(document.activeElement?.textContent).toBe(
      "Connect your coding agents",
    );
    expect(document.activeElement?.id).not.toBe("agent-speak-setup-title");

    pressPrimary();
    await waitFor(() =>
      expect(document.activeElement?.textContent).toBe(
        "Choose how replies appear",
      ),
    );
    expect(document.activeElement?.tagName).toBe("H2");
  });

  it("closes on Esc and tears down exactly once", async () => {
    const onClose = vi.fn();
    renderModal({ onClose });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(api_agents.teardownSetup).toHaveBeenCalledTimes(1),
    );
  });

  it("does not close on Esc while step 2's download-confirm dialog is open", async () => {
    const onClose = vi.fn();
    // Kokoro is the selected engine, so picking Supertonic raises the
    // download-confirm dialog for an uninstalled engine.
    renderModal({
      onClose,
      config: makeConfig({ agent_speech_engine: "kokoro" }),
    });
    pressPrimary(); // step 2 owns the download-confirm / memory dialogs

    // `AnimatePresence mode="wait"` holds the incoming step until the outgoing
    // one has exited, so step 2 arrives on a later tick. The voice-model
    // choice is a segmented `ModeSelector`: each pill is its own button, no
    // open-then-select step.
    fireEvent.click(await screen.findByRole("button", { name: "Lower memory" }));

    const cancel = await screen.findByRole("button", { name: "Cancel" });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(cancel);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls the mounted step's commit hook before advancing", async () => {
    const onDownload = vi.fn(async () => {});
    renderModal({ onDownload });
    pressPrimary(); // to step 2
    await screen.findByRole("group", { name: "Voice model" });
    pressPrimary(); // Continue from step 2 commits Automatic's two downloads
    await waitFor(() =>
      expect(onDownload).toHaveBeenCalledWith(["supertonic", "kokoro"]),
    );
    expect(screen.getByText("Step 3 of 4")).toBeInTheDocument();
  });

  it("tears down exactly once when the close X is used", async () => {
    const { rerender, props } = renderModal();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(props.onClose).toHaveBeenCalledTimes(1);
    rerender(<AgentSpeakSetupModal {...props} open={false} />);
    await waitFor(() =>
      expect(api_agents.teardownSetup).toHaveBeenCalledTimes(1),
    );
  });

  it("writes all delivery mode fields with 20s reply window and finishes on Done", async () => {
    const onFinish = vi.fn();
    renderModal({ onFinish });
    for (let i = 0; i < 3; i += 1) pressPrimary();
    pressPrimary();
    await waitFor(() => expect(onFinish).toHaveBeenCalledTimes(1));
    expect(api_config.saveConfig).toHaveBeenCalledWith({
      agent_speak_setup_completed: true,
      agent_summary_text_enabled: true,
      agent_speech_enabled: true,
      claude_reply_grace_seconds: 20,
    });
  });

  it("initializes both delivery modes to true and does not save config on cancel", async () => {
    const onClose = vi.fn();
    renderModal({ onClose });
    pressPrimary(); // advance to step 2
    const summaries = await screen.findByRole("switch", {
      name: "Show written summaries for coding agent replies",
    });
    const speech = screen.getByRole("switch", {
      name: "Read coding agent replies aloud",
    });
    expect(summaries).toHaveAttribute("aria-checked", "true");
    expect(speech).toHaveAttribute("aria-checked", "true");

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(api_config.saveConfig).not.toHaveBeenCalled();
  });

  it("saves text-only delivery mode with 20s reply window and skips voice download", async () => {
    const onFinish = vi.fn();
    const onDownload = vi.fn(async () => {});
    renderModal({ onFinish, onDownload });
    pressPrimary(); // to step 2

    // Turn off speech
    const speech = await screen.findByRole("switch", {
      name: "Read coding agent replies aloud",
    });
    fireEvent.click(speech);

    pressPrimary(); // to step 3 (Continue from step 2 skips download)
    expect(onDownload).not.toHaveBeenCalled();

    pressPrimary(); // to step 4
    pressPrimary(); // Done
    await waitFor(() => expect(onFinish).toHaveBeenCalledTimes(1));
    expect(api_config.saveConfig).toHaveBeenCalledWith({
      agent_speak_setup_completed: true,
      agent_summary_text_enabled: true,
      agent_speech_enabled: false,
      claude_reply_grace_seconds: 20,
    });
  });

  it("saves speech-only delivery mode with 6s reply window", async () => {
    const onFinish = vi.fn();
    renderModal({ onFinish });
    pressPrimary(); // to step 2

    // Turn off summaries
    const summaries = await screen.findByRole("switch", {
      name: "Show written summaries for coding agent replies",
    });
    fireEvent.click(summaries);

    pressPrimary(); // to step 3
    pressPrimary(); // to step 4
    pressPrimary(); // Done
    await waitFor(() => expect(onFinish).toHaveBeenCalledTimes(1));
    expect(api_config.saveConfig).toHaveBeenCalledWith({
      agent_speak_setup_completed: true,
      agent_summary_text_enabled: false,
      agent_speech_enabled: true,
      claude_reply_grace_seconds: 6,
    });
  });

  it("keeps the modal open and alerts the locked failure string when the finish write fails", async () => {
    const onFinish = vi.fn();
    vi.mocked(api_config.saveConfig).mockRejectedValueOnce(new Error("nope"));
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    renderModal({ onFinish });
    for (let i = 0; i < 3; i += 1) pressPrimary();
    pressPrimary();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not save your setup. Try Done again.",
    );
    expect(onFinish).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Retrying the same button succeeds.
    pressPrimary();
    await waitFor(() => expect(onFinish).toHaveBeenCalledTimes(1));
    errorSpy.mockRestore();
  });
});
