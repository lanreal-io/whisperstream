import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentProviderInstallList, setupPluginBlockReason, type AgentProviderInstallListProps } from "./AgentProviderInstallList";
import { IDLE_ACTIVITY, type AgentProviderRowMap, type ProviderRowModel } from "./agentCompatibility";

vi.mock("../../lib/api/agents", async (original) => ({
  ...await original<typeof import("../../lib/api/agents")>(),
  getManagedCliStatus: vi.fn(async () => ({ ok: true, status: "installed", available: true, installed: true })),
}));
afterEach(cleanup);

const row = (state: ProviderRowModel["state"], extra: Partial<ProviderRowModel> = {}): ProviderRowModel => ({
  state, connectable: true, missing: false, detectionFailed: false, loading: false, installedPrefix: false, ...extra,
});

function props(rows: Partial<AgentProviderRowMap>, overrides: Partial<AgentProviderInstallListProps> = {}): AgentProviderInstallListProps {
  return {
    rows: { codex: row("not_connected"), claude: row("not_connected"), ...rows },
    activity: IDLE_ACTIVITY, connected: { codex: false, claude: false },
    onToggle: vi.fn(), onResume: vi.fn(), onRecheck: vi.fn(), ...overrides,
  };
}

function rowOf(container: HTMLElement, provider: string) {
  return within(container.querySelector(`[data-agent-provider="${provider}"]`) as HTMLElement);
}

describe("provider rows", () => {
  it("connects in one click with no surface chooser or details", () => {
    const p = props({});
    render(<AgentProviderInstallList {...p} />);
    fireEvent.click(screen.getByRole("button", { name: "Connect Codex" }));
    expect(p.onToggle).toHaveBeenCalledWith("codex", true);
    fireEvent.click(screen.getByRole("button", { name: "Connect Claude Code" }));
    expect(p.onToggle).toHaveBeenCalledWith("claude", true);
    expect(screen.queryByText(/Connection details/)).toBeNull();
    expect(screen.queryByRole("group", { name: /Use .* with/ })).toBeNull();
    expect(screen.queryByText(/Update Whisperstream plugin/)).toBeNull();
    expect(screen.getByText("See and hear Codex replies in the overlay.")).toBeInTheDocument();
    expect(screen.getByText("See and hear Claude Code replies in the overlay.")).toBeInTheDocument();
  });

  it("disables Connect for a missing agent and asks for a recheck when detection failed", () => {
    const { container } = render(<AgentProviderInstallList {...props({
      codex: row("not_connected", { connectable: false, missing: true, detectionFailed: true }),
      claude: row("not_connected", { connectable: false, missing: true }),
    })} />);
    expect(rowOf(container, "codex").getByText("Couldn't check Codex installations. Check again.")).toBeInTheDocument();
    expect(rowOf(container, "claude").getByText("Claude Code isn't installed. Install it, then check again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect Codex" })).toBeDisabled();
  });

  it("shows the connecting state with a busy button", () => {
    const { container } = render(<AgentProviderInstallList {...props({ claude: row("connecting") })} />);
    const claude = rowOf(container, "claude");
    expect(claude.getByText("Connecting")).toBeInTheDocument();
    expect(claude.getByText("Installing the Whisperstream plugin...")).toBeInTheDocument();
    expect(claude.getByRole("button", { name: "Connecting Claude Code" })).toBeDisabled();
  });

  it("gives Claude's waiting row the reload command, with the Installed prefix only after Connect", async () => {
    const writeText = vi.fn(async () => {});
    Object.assign(navigator, { clipboard: { writeText } });
    const p = props({ claude: row("waiting", { installedPrefix: true }) });
    const { container, rerender } = render(<AgentProviderInstallList {...p} />);
    const claude = rowOf(container, "claude");
    expect(claude.getByText("Almost ready")).toBeInTheDocument();
    expect(claude.getByText("Installed. Start a new Claude Code session, or run this in an open one:")).toBeInTheDocument();
    expect(claude.getByText("This row updates on its own.")).toBeInTheDocument();
    await act(async () => { fireEvent.click(claude.getByRole("button", { name: "Copy /reload-plugins" })); });
    expect(writeText).toHaveBeenCalledWith("/reload-plugins");
    expect(claude.getAllByText("Copied").length).toBeGreaterThan(0);
    fireEvent.click(claude.getByRole("button", { name: "Disconnect Claude Code" }));
    expect(p.onToggle).toHaveBeenCalledWith("claude", false);
    rerender(<AgentProviderInstallList {...props({ claude: row("waiting") })} />);
    expect(rowOf(container, "claude").getByText("Start a new Claude Code session, or run this in an open one:")).toBeInTheDocument();
  });

  it.each([
    ["app", "In the Codex app, open Settings > Hooks > Whisperstream, then Trust all."],
    ["cli", "In the Codex CLI, run /hooksCopy, then trust Whisperstream."],
  ] as const)("shows the Codex %s trust route", (routes, text) => {
    const { container } = render(<AgentProviderInstallList {...props({ codex: row("waiting", { codexRoutes: routes }) })} />);
    const codex = rowOf(container, "codex");
    expect(codex.getByText("Trust needed")).toBeInTheDocument();
    expect(codex.getAllByRole("status").some((node) => node.textContent === text)).toBe(true);
    expect(codex.queryByText("Codex app")).toBeNull();
  });

  it("shows both Codex trust routes once when the app and CLI are usable", () => {
    const { container } = render(<AgentProviderInstallList {...props({ codex: row("waiting", { codexRoutes: "both", installedPrefix: true }) })} />);
    const codex = rowOf(container, "codex");
    expect(codex.getByText("Installed. Trust Whisperstream once, in either place:")).toBeInTheDocument();
    expect(codex.getByText("Codex app")).toBeInTheDocument();
    expect(codex.getByText("Codex CLI")).toBeInTheDocument();
    expect(codex.getByLabelText("Settings, Hooks, Whisperstream")).toBeInTheDocument();
    expect(codex.getByRole("button", { name: "Copy /hooks" })).toBeInTheDocument();
  });

  it("uses the locked connected, paused, resuming and update lines", () => {
    const p = props({ codex: row("connected"), claude: row("paused") });
    const { container, rerender } = render(<AgentProviderInstallList {...p} />);
    expect(rowOf(container, "codex").getByText("Codex replies show in the overlay, from the app or terminal.")).toBeInTheDocument();
    expect(rowOf(container, "claude").getByText("Paused with Agents. Your connection is kept.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resume Agents" }));
    expect(p.onResume).toHaveBeenCalledOnce();
    rerender(<AgentProviderInstallList {...props({
      codex: row("paused"),
      claude: row("update_needed", { blocking: { surface: "cli", version: "2.1.204", minimum: "2.1.287" } }),
    })} />);
    expect(rowOf(container, "codex").getByText("Paused with Agents. Your connection and hook trust are kept.")).toBeInTheDocument();
    const claude = rowOf(container, "claude");
    expect(claude.getAllByRole("status")[0].textContent)
      .toBe("Claude Code 2.1.204 is too old. Update to 2.1.287 or later, then start a new session.");
    expect(claude.getByRole("button", { name: "Check Claude Code again" })).toBeEnabled();
    rerender(<AgentProviderInstallList {...props({ codex: row("resuming") })} />);
    expect(rowOf(container, "codex").getByText("Resuming Codex...")).toBeInTheDocument();
    expect(rowOf(container, "codex").getByRole("button", { name: "Resume Agents" })).toBeDisabled();
  });

  it("announces a failure with Try again", () => {
    const p = props({ codex: row("failed", { failure: "unsupported" }) });
    const { container } = render(<AgentProviderInstallList {...p} />);
    const codex = rowOf(container, "codex");
    expect(codex.getByRole("alert")).toHaveTextContent("This Codex version can't add plugins. Update Codex, then try again.");
    expect(codex.getByText("Couldn't connect")).toBeInTheDocument();
    fireEvent.click(codex.getByRole("button", { name: "Try connecting Codex again" }));
    expect(p.onToggle).toHaveBeenCalledWith("codex", true);
  });

  it("keeps the CLI row and footer in Settings only", async () => {
    const p = props({});
    const { container, rerender } = render(<AgentProviderInstallList {...p} />);
    expect(container.querySelector('[data-agent-provider="managed-cli"]')).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Check connections again" }));
    expect(p.onRecheck).toHaveBeenCalledOnce();
    rerender(<AgentProviderInstallList {...p} density="setup" />);
    expect(container.querySelector('[data-agent-provider="managed-cli"]')).toBeNull();
    expect(screen.queryByRole("button", { name: "Check connections again" })).toBeNull();
  });
});

describe("setupPluginBlockReason", () => {
  it("allows Continue once one provider is connected", () => {
    expect(setupPluginBlockReason({ codex: row("connected"), claude: row("failed") })).toBeNull();
    expect(setupPluginBlockReason({ codex: row("waiting"), claude: row("not_connected") })).toBe("Finish connecting one agent to continue.");
    expect(setupPluginBlockReason({ codex: row("not_connected"), claude: row("not_connected") })).toBe("Connect one agent to continue.");
  });
});
