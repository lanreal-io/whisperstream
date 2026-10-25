import { readyAgentResult } from "../../test/agentCompatibility";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api_agents from "../../lib/api/agents";
import type { AppConfig } from "../../lib/api/config";
import { SetupStepPlugin, type SetupStepPluginProps } from "./SetupStepPlugin";

const revealItemInDir = vi.hoisted(() => vi.fn(async () => {}));

vi.mock("@tauri-apps/plugin-opener", () => ({ revealItemInDir }));

vi.mock("../../lib/api/agents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/agents")>();
  return {
    ...actual,
    getSetupPluginDetail: vi.fn(async () => ({
      plugin_dir: null,
      hook_definition: null,
    })),
    getSetupCodexPluginDetail: vi.fn(async () => ({
      plugin_dir: null,
      hook_definition: null,
    })),
    getManagedCliStatus: vi.fn(async () => ({
      ok: true,
      status: "not_installed" as const,
      available: true,
      installed: false,
    })),
    installManagedCli: vi.fn(),
    uninstallManagedCli: vi.fn(),
  };
});

const CODEX_DIR = "C:\\Users\\jane\\AppData\\Codex\\whisperstream";
const CLAUDE_DIR = "C:\\Users\\jane\\.claude\\plugins\\whisperstream";
const CODEX_HOOK = '{"hooks":{"Stop":[{"command":"forward-hook.ps1"}]}}';
const CLAUDE_HOOK = '{"Stop":[{"hooks":[{"command":"whisperstream-agent-hook"}]}]}';
const DISABLED_REASON = "Available after a coding agent is connected.";

function makeConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    claude_hud_enabled: false,
    codex_hud_enabled: false,
    claude_hud_show_text: true,
    agent_speak_setup_completed: false,
    ...overrides,
  } as AppConfig;
}

function renderStep(overrides: Partial<SetupStepPluginProps> = {}) {
  const props: SetupStepPluginProps = {
    config: makeConfig(),
    onCanAdvanceChange: vi.fn(),
    onChildDialogOpenChange: vi.fn(),
    providerResults: { codex: null, claude: null },
    onProviderToggle: vi.fn(),
    ...overrides,
  };
  props.providerResults = {
    codex: { ...readyAgentResult(), ...props.providerResults.codex },
    claude: { ...readyAgentResult(), ...props.providerResults.claude },
  };
  return { ...render(<SetupStepPlugin {...props} />), props };
}

function disclosureButton(): HTMLButtonElement {
  return screen.getByRole("button", {
    name: "What Whisperstream added",
  }) as HTMLButtonElement;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

beforeEach(() => {
  vi.mocked(api_agents.getManagedCliStatus).mockResolvedValue({
    ok: true,
    status: "not_installed",
    available: true,
    installed: false,
  });
  vi.mocked(api_agents.getSetupPluginDetail).mockResolvedValue({
    plugin_dir: null,
    hook_definition: null,
  });
  vi.mocked(api_agents.getSetupCodexPluginDetail).mockResolvedValue({
    plugin_dir: null,
    hook_definition: null,
  });
});

describe("SetupStepPlugin", () => {
  it("uses the locked heading and lede, Codex first, and no CLI row or footer", async () => {
    renderStep();
    expect(screen.getByRole("heading", { name: "Connect your coding agents" })).toBeInTheDocument();
    expect(screen.getByText("Connect Claude Code, Codex, or both to see and hear their replies.")).toBeInTheDocument();
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveAttribute("data-agent-provider", "codex");
    expect(rows[1]).toHaveAttribute("data-agent-provider", "claude");
    expect(screen.queryByRole("button", { name: "Check connections again" })).toBeNull();
    expect(screen.queryByText(/Connection details/)).toBeNull();
    expect(screen.getByRole("status", { name: "No agents connected" })).toBeInTheDocument();
    await waitFor(() => expect(api_agents.getSetupCodexPluginDetail).toHaveBeenCalled());
  });

  it("gates Continue until either provider is connected", async () => {
    const none = vi.fn();
    const first = renderStep({ onCanAdvanceChange: none });
    await waitFor(() => expect(none).toHaveBeenCalledWith(false));
    first.unmount();

    const codexOnly = vi.fn();
    const second = renderStep({ config: makeConfig({ codex_hud_enabled: true }), onCanAdvanceChange: codexOnly });
    await waitFor(() => expect(codexOnly).toHaveBeenCalledWith(true));
    second.unmount();

    const claudeOnly = vi.fn();
    renderStep({ config: makeConfig({ claude_hud_enabled: true }), onCanAdvanceChange: claudeOnly });
    await waitFor(() => expect(claudeOnly).toHaveBeenCalledWith(true));
  });

  it("treats installed-but-untrusted Codex as waiting with its trust route", async () => {
    const onCanAdvanceChange = vi.fn();
    renderStep({
      config: makeConfig({ codex_hud_enabled: true }),
      providerLastConnect: { codex: "installed" },
      providerResults: { codex: { ...readyAgentResult(), status: "installed", trust_required: true }, claude: null },
      onCanAdvanceChange,
    });
    const codex = within(screen.getAllByRole("listitem")[0]);
    expect(codex.getByText("Trust needed")).toBeInTheDocument();
    expect(codex.getAllByRole("status").some((node) =>
      node.textContent === "Installed. In the Codex CLI, run /hooksCopy, then trust Whisperstream.")).toBe(true);
    expect(screen.getByRole("status", { name: "Agents needs attention" })).toBeInTheDocument();
    await waitFor(() => expect(onCanAdvanceChange).toHaveBeenCalledWith(false));
  });

  it("enables Continue with one connected agent while the light reports the other", async () => {
    const onCanAdvanceChange = vi.fn();
    renderStep({
      config: makeConfig({ claude_hud_enabled: true, codex_hud_enabled: true }),
      providerResults: { codex: { ok: false, status: "failed", codex_cli_available: false }, claude: readyAgentResult() },
      onCanAdvanceChange,
    });
    const [codexRow, claudeRow] = screen.getAllByRole("listitem");
    expect(within(codexRow).getByRole("alert")).toHaveTextContent("Codex isn't installed. Install it, then try again.");
    expect(within(claudeRow).getByText("Connected")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Agents needs attention" })).toBeInTheDocument();
    await waitFor(() => expect(onCanAdvanceChange).toHaveBeenCalledWith(true));
  });

  it("connects in one click and disconnects without a surface choice", () => {
    const { props } = renderStep({
      config: makeConfig({ claude_hud_enabled: true }),
      providerResults: { codex: null, claude: readyAgentResult() },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect Codex" }));
    expect(props.onProviderToggle).toHaveBeenCalledWith("codex", true);
    expect(screen.queryByRole("group", { name: /Use .* with/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Disconnect Claude Code" }));
    expect(props.onProviderToggle).toHaveBeenCalledWith("claude", false);
  });

  it("shows the connecting state in setup density", () => {
    renderStep({ providerActivity: { codex: "connecting", claude: "idle" } });
    expect(screen.getByRole("button", { name: "Connecting Codex" })).toBeDisabled();
    expect(screen.getByRole("status", { name: "Agents needs attention" })).toBeInTheDocument();
  });

  it("reveals both receipts and shows each full plugin path", async () => {
    vi.mocked(api_agents.getSetupCodexPluginDetail).mockResolvedValue({
      plugin_dir: CODEX_DIR,
      hook_definition: CODEX_HOOK,
    });
    vi.mocked(api_agents.getSetupPluginDetail).mockResolvedValue({
      plugin_dir: CLAUDE_DIR,
      hook_definition: CLAUDE_HOOK,
    });
    const { container } = renderStep({
      config: makeConfig({ codex_hud_enabled: true, claude_hud_enabled: true }),
    });
    await waitFor(() =>
      expect(api_agents.getSetupCodexPluginDetail).toHaveBeenCalled(),
    );

    fireEvent.click(disclosureButton());
    // The compact receipt replaces the raw JSON dump: no <pre>, no hook names.
    expect(container.querySelector("pre")).toBeNull();
    expect(container.textContent).not.toContain("Stop");

    const codexDetail = container.querySelector(
      '[data-setup-plugin-detail="codex"]',
    );
    const claudeDetail = container.querySelector(
      '[data-setup-plugin-detail="claude"]',
    );
    // Codex receipt parses to one hook -> singular, count-led inline descriptor,
    // and the real full path is now visible (the old middle ellipsis is gone).
    expect(codexDetail?.textContent).toContain("Added 1 hook");
    expect(codexDetail?.textContent).toContain(CODEX_DIR);
    // Claude receipt has no top-level `hooks` object -> no descriptor (never
    // "Added 0 hooks"), just the name and the full path.
    expect(claudeDetail?.textContent).not.toContain("Added");
    expect(claudeDetail?.textContent).toContain(CLAUDE_DIR);

    fireEvent.click(
      screen.getByRole("button", {
        name: `Show in Explorer for Codex: ${CODEX_DIR}`,
      }),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: `Show in Explorer for Claude Code: ${CLAUDE_DIR}`,
      }),
    );
    expect(revealItemInDir).toHaveBeenNthCalledWith(1, CODEX_DIR);
    expect(revealItemInDir).toHaveBeenNthCalledWith(2, CLAUDE_DIR);
  });

  it("keeps unavailable receipts in place with the locked accessible reason", async () => {
    const { container } = renderStep();
    await waitFor(() => expect(api_agents.getSetupPluginDetail).toHaveBeenCalled());
    fireEvent.click(disclosureButton());

    for (const label of ["Codex", "Claude Code"]) {
      const button = screen.getByRole("button", {
        name: `Show in Explorer for ${label}`,
      });
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute("title", DISABLED_REASON);
      const describedBy = button.getAttribute("aria-describedby");
      expect(document.getElementById(describedBy!)?.textContent).toBe(
        DISABLED_REASON,
      );
    }
    expect(container.querySelector("[data-setup-hook-hairline]")).toHaveStyle({
      background:
        "color-mix(in srgb, var(--color-steel) 22%, transparent)",
    });
  });
});
