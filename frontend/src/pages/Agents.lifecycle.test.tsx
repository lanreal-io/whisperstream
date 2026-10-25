import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Agents } from "./Agents";
import type { AppConfig } from "../lib/api/config";
import type { AppEvent } from "../lib/socket/events";
import type { HudConfigModalProps } from "../components/HudConfigModal";
const mocks = vi.hoisted(() => ({
  codex: vi.fn(), claude: vi.fn(), enableCodex: vi.fn(), enableClaude: vi.fn(),
  modal: { current: null as HudConfigModalProps | null },
}));
vi.mock("../components/HudConfigModal", () => ({ HudConfigModal: (props: HudConfigModalProps) => { mocks.modal.current = props; return null; } }));
vi.mock("../components/agentSpeak/AgentSpeakSetupModal", () => ({ AgentSpeakSetupModal: () => null }));
vi.mock("../lib/api/agents", async (original) => ({ ...await original<typeof import("../lib/api/agents")>(), getCodexHudStatus: mocks.codex, getClaudeHudStatus: mocks.claude, enableCodexHud: mocks.enableCodex, enableClaudeHud: mocks.enableClaude }));
vi.mock("../lib/api/speech", async (original) => ({ ...await original<typeof import("../lib/api/speech")>(), getSpeechStatus: vi.fn(async () => ({ active_engine: null, current_download: null, engines: {} })) }));
afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks(); });

it("polls only the unresolved enabled provider and refreshes ready providers on focus", async () => {
  vi.useFakeTimers();
  const ready = { ok: true, status: "current", compatibility: { surfaces: [{ surface: "cli", state: "ready", reply_verified: true }] } };
  mocks.codex.mockResolvedValue({ ...ready, trust_required: true });
  mocks.claude.mockResolvedValue(ready);
  const handlers = new Set<(event: AppEvent) => void>();
  const subscribe = (handler: (event: AppEvent) => void) => { handlers.add(handler); return () => { handlers.delete(handler); }; };
  render(<Agents config={{ claude_hud_enabled: true, codex_hud_enabled: true, agent_speak_setup_completed: true } as AppConfig}
    subscribe={subscribe} connected onConfigChange={vi.fn()} send={vi.fn()} onShowStatus={vi.fn()} />);
  await act(async () => {});
  expect(mocks.codex).toHaveBeenCalledOnce();
  expect(mocks.claude).toHaveBeenCalledOnce();
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(mocks.codex).toHaveBeenCalledTimes(2);
  expect(mocks.claude).toHaveBeenCalledOnce();
  mocks.codex.mockResolvedValue(ready);
  await act(async () => { await vi.advanceTimersByTimeAsync(50000); });
  expect(mocks.codex).toHaveBeenCalledTimes(3);
  expect(mocks.claude).toHaveBeenCalledOnce();
  await act(async () => {
    for (let i = 0; i < 100; i += 1) for (const handler of handlers) handler({ type: "agent.completion" } as AppEvent);
    await vi.advanceTimersByTimeAsync(10000);
  });
  expect(mocks.codex).toHaveBeenCalledTimes(3);
  expect(mocks.claude).toHaveBeenCalledOnce();
  await act(async () => { window.dispatchEvent(new Event("focus")); });
  expect(mocks.codex).toHaveBeenCalledTimes(4);
  expect(mocks.claude).toHaveBeenCalledTimes(2);
});

it("waits for a slow first status read so a not-yet-connected provider can Connect", async () => {
  vi.useFakeTimers();
  const ready = { ok: true, status: "current", compatibility: { surfaces: [{ surface: "cli", state: "ready", reply_verified: true }] } };
  // A cold Codex read takes 11.7 s in the dev app; it used to be cancelled at 5 s.
  mocks.codex.mockImplementation(() => new Promise((resolve) => setTimeout(() => resolve(ready), 11700)));
  mocks.claude.mockResolvedValue(ready);
  render(<Agents config={{ claude_hud_enabled: false, codex_hud_enabled: false, agent_speak_setup_completed: true } as AppConfig}
    subscribe={() => () => {}} connected onConfigChange={vi.fn()} send={vi.fn()} onShowStatus={vi.fn()} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(12000); });
  expect(mocks.codex).toHaveBeenCalledOnce();
  // The settings modal is mocked here; the row it derives Connect from gets the result.
  expect(mocks.modal.current?.providerResults.codex).toEqual(ready);
});

it("retries a first status read that never answered", async () => {
  vi.useFakeTimers();
  const ready = { ok: true, status: "current", compatibility: { surfaces: [{ surface: "cli", state: "ready", reply_verified: true }] } };
  mocks.codex.mockImplementationOnce((_force: boolean, signal: AbortSignal) => new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new Error("aborted")))));
  mocks.codex.mockResolvedValue(ready);
  mocks.claude.mockResolvedValue(ready);
  render(<Agents config={{ claude_hud_enabled: false, codex_hud_enabled: false, agent_speak_setup_completed: true } as AppConfig}
    subscribe={() => () => {}} connected onConfigChange={vi.fn()} send={vi.fn()} onShowStatus={vi.fn()} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(mocks.codex).toHaveBeenCalledTimes(2);
});

it("arms trust recovery after config enablement without another immediate status probe", async () => {
  vi.useFakeTimers();
  const ready = { ok: true, status: "current", compatibility: { surfaces: [{ surface: "cli", state: "ready", reply_verified: true }] } };
  mocks.codex.mockResolvedValue({ ...ready, trust_required: true });
  mocks.claude.mockResolvedValue(ready);
  const subscribe = vi.fn(() => () => {});
  const props = { subscribe, connected: true, onConfigChange: vi.fn(), send: vi.fn(), onShowStatus: vi.fn() };
  const config = { claude_hud_enabled: false, codex_hud_enabled: false, agent_speak_setup_completed: true } as AppConfig;
  const { rerender } = render(<Agents {...props} config={config} />);
  await act(async () => {});
  expect(mocks.codex).toHaveBeenCalledOnce();
  rerender(<Agents {...props} config={{ ...config, codex_hud_enabled: true }} />);
  await act(async () => {});
  expect(mocks.codex).toHaveBeenCalledOnce();
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(mocks.codex).toHaveBeenCalledTimes(2);
  expect(mocks.claude).toHaveBeenCalledOnce();
});

describe("master Agents switch and Connect", () => {
  const ready = { ok: true, status: "current", compatibility: { surfaces: [{ surface: "cli", state: "ready", reply_verified: true }] } };
  const base = { claude_hud_enabled: true, codex_hud_enabled: true, agents_enabled: false, agent_speak_setup_completed: true } as AppConfig;
  function mount(config: AppConfig = base) {
    const onCommitConfig = vi.fn(async (_patch: Partial<AppConfig>) => {});
    render(<Agents config={config} onCommitConfig={onCommitConfig} subscribe={() => () => {}} connected
      onConfigChange={vi.fn()} send={vi.fn()} onShowStatus={vi.fn()} />);
    return { onCommitConfig, modal: () => mocks.modal.current as HudConfigModalProps };
  }

  it("pauses by saving agents_enabled false without uninstalling", async () => {
    mocks.codex.mockResolvedValue(ready); mocks.claude.mockResolvedValue(ready);
    const { onCommitConfig, modal } = mount({ ...base, agents_enabled: true });
    await act(async () => {});
    let outcome: { needsAction: boolean } | undefined;
    await act(async () => { outcome = await modal().onAgentsToggle!(false); });
    expect(onCommitConfig).toHaveBeenCalledWith({ agents_enabled: false });
    expect(mocks.enableCodex).not.toHaveBeenCalled();
    expect(outcome).toEqual({ needsAction: false });
  });

  it("resumes remembered providers without force and stays put when all connect", async () => {
    mocks.codex.mockResolvedValue(ready); mocks.claude.mockResolvedValue(ready);
    mocks.enableCodex.mockResolvedValue(ready); mocks.enableClaude.mockResolvedValue(ready);
    const { onCommitConfig, modal } = mount();
    await act(async () => {});
    let outcome: { needsAction: boolean } | undefined;
    await act(async () => { outcome = await modal().onAgentsToggle!(true); });
    expect(onCommitConfig).toHaveBeenCalledWith({ agents_enabled: true });
    expect(mocks.enableCodex).toHaveBeenCalledWith({ force: false });
    expect(mocks.enableClaude).toHaveBeenCalledWith({ force: false });
    expect(outcome).toEqual({ needsAction: false });
  });

  it("reports needsAction when a resumed Codex needs hook trust", async () => {
    mocks.codex.mockResolvedValue(ready); mocks.claude.mockResolvedValue(ready);
    mocks.enableCodex.mockResolvedValue(ready); mocks.enableClaude.mockResolvedValue(ready);
    const { modal } = mount({ ...base, claude_hud_enabled: false });
    await act(async () => {});
    mocks.codex.mockResolvedValue({ ...ready, trust_required: true });
    let outcome: { needsAction: boolean } | undefined;
    await act(async () => { outcome = await modal().onAgentsToggle!(true); });
    expect(mocks.enableClaude).not.toHaveBeenCalled();
    expect(outcome).toEqual({ needsAction: true });
  });

  it("Connect installs fresh, resumes Agents first while paused and marks the Installed receipt", async () => {
    mocks.codex.mockResolvedValue({ ...ready, trust_required: true }); mocks.claude.mockResolvedValue(ready);
    mocks.enableCodex.mockResolvedValue({ ok: true, status: "installed" }); mocks.enableClaude.mockResolvedValue(ready);
    const { onCommitConfig, modal } = mount({ ...base, codex_hud_enabled: false });
    await act(async () => {});
    await act(async () => { modal().onProviderToggle("codex", true); });
    await act(async () => {});
    expect(onCommitConfig.mock.calls.map(([patch]) => patch)).toEqual([{ agents_enabled: true }, { codex_hud_enabled: true }]);
    expect(mocks.enableCodex).toHaveBeenCalledWith({ force: true });
    expect(modal().providerLastConnect).toMatchObject({ codex: "installed" });
  });
});
