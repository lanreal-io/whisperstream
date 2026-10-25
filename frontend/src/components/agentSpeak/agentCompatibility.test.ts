import { describe, expect, it } from "vitest";
import type { AgentSurfaceCompatibility, ClaudePluginActionResult, CodexPluginActionResult } from "../../lib/api/agents";
import type { AppConfig } from "../../lib/api/config";
import {
  agentConnections, agentRows, aggregateAgentsLight, deriveProviderRow, detectedAgentSurfaces,
  type ProviderRowInput, type ProviderRowModel,
} from "./agentCompatibility";

export function surface(surface: "cli" | "desktop", state: AgentSurfaceCompatibility["state"] = "ready", version = "0.160.0"): AgentSurfaceCompatibility {
  return { surface, state, app_version: surface === "desktop" ? "26.1" : null, engine_version: version,
    minimum_version: state === "update_required" ? "0.160.0" : null,
    reply_verified: state === "ready", transport: state === "legacy" ? "legacy" : "codex_queue" };
}

const codex = (surfaces: AgentSurfaceCompatibility[], extra: Partial<CodexPluginActionResult> = {}): CodexPluginActionResult =>
  ({ ok: true, status: "current", compatibility: { surfaces }, ...extra });
const claude = (connection: ClaudePluginActionResult["connection"], extra: Partial<ClaudePluginActionResult> = {}): ClaudePluginActionResult =>
  ({ ok: true, status: "current", connection, compatibility: { surfaces: [surface("cli", "supported", "2.1.300")] }, ...extra });

const on = (result: ProviderRowInput["result"], extra: Partial<ProviderRowInput> = {}): ProviderRowInput =>
  ({ agentsEnabled: true, remembered: true, activity: "idle", result, ...extra });

describe("deriveProviderRow", () => {
  const cases: Array<[string, "codex" | "claude", ProviderRowInput, Partial<ProviderRowModel>]> = [
    ["connecting wins over everything", "claude", on(claude("connected"), { activity: "connecting", agentsEnabled: false }), { state: "connecting" }],
    ["resuming wins over paused", "codex", on(codex([surface("cli")]), { activity: "resuming", agentsEnabled: false }), { state: "resuming" }],
    ["not remembered is not connected", "codex", on(codex([surface("cli")]), { remembered: false }), { state: "not_connected", connectable: true }],
    ["not installed agent disables Connect", "claude", on(claude("none", { compatibility: { surfaces: [surface("cli", "not_installed")] } }), { remembered: false }),
      { state: "not_connected", connectable: false, missing: true }],
    ["failed detection asks for a recheck", "codex", on(codex([{ ...surface("cli", "check_failed"), engine_version: null }]), { remembered: false }),
      { state: "not_connected", connectable: false, missing: true, detectionFailed: true }],
    ["Codex trust with no detected route shows both", "codex", on(codex([], { trust_required: true })), { state: "waiting", codexRoutes: "both" }],
    ["a failed Connect keeps Try again before the provider is remembered", "codex",
      on(codex([surface("cli")], { ok: false, status: "failed" }), { remembered: false, lastConnect: "failed" }), { state: "failed", failure: "other" }],
    ["remembered while Agents is off is paused", "claude", on(claude("connected"), { agentsEnabled: false }), { state: "paused" }],
    ["failed action is couldn't connect", "claude", on(claude("none", { ok: false, status: "failed", error: "claude_cli_unavailable" })), { state: "failed", failure: "cliUnavailable" }],
    ["Claude list failure", "claude", on(claude("none", { ok: false, status: "failed", error: "plugin_list_failed" })), { failure: "listFailed" }],
    ["Claude install failure", "claude", on(claude("none", { ok: false, status: "failed", error: "plugin_update_failed" })), { failure: "installFailed" }],
    ["Codex unsupported", "codex", on(codex([surface("cli")], { ok: false, status: "failed", codex_cli_supported: false })), { state: "failed", failure: "unsupported" }],
    ["Codex other failure", "codex", on(codex([surface("cli")], { ok: false, status: "failed" })), { failure: "other" }],
    ["remembered plugin removed is couldn't connect", "codex", on(codex([surface("cli")], { status: "removed" })), { state: "failed" }],
    ["only an outdated install is update needed", "codex", on(codex([surface("cli", "update_required", "0.151.0")])),
      { state: "update_needed", blocking: { surface: "cli", version: "0.151.0", minimum: "0.160.0" } }],
    ["an outdated CLI beside a usable app is silent", "codex", on(codex([surface("cli", "update_required", "0.151.0"), surface("desktop", "legacy")])), { state: "connected" }],
    ["Codex trust, app only", "codex", on(codex([surface("cli", "not_installed"), surface("desktop", "legacy")], { trust_required: true })), { state: "waiting", codexRoutes: "app" }],
    ["Codex trust, CLI only", "codex", on(codex([surface("cli", "legacy")], { trust_required: true })), { state: "waiting", codexRoutes: "cli" }],
    ["Codex trust, both", "codex", on(codex([surface("cli", "legacy"), surface("desktop", "legacy")], { trust_required: true })), { state: "waiting", codexRoutes: "both" }],
    ["Codex trust, app with old CLI", "codex", on(codex([surface("cli", "update_required", "0.151.0"), surface("desktop", "legacy")], { trust_required: true })),
      { state: "waiting", codexRoutes: "app" }],
    ["Installed prefix only after Connect", "codex", on(codex([surface("cli", "legacy")], { trust_required: true }), { lastConnect: "installed" }), { installedPrefix: true }],
    ["Claude waiting for a new session", "claude", on(claude("waiting")), { state: "waiting", installedPrefix: false }],
    ["Claude connected from any surface", "claude", on(claude("connected")), { state: "connected" }],
    ["Codex connected from any surface", "codex", on(codex([surface("desktop", "legacy")])), { state: "connected" }],
    ["a remembered provider stays connected, unconfirmed, while loading", "claude", on(null), { state: "connected", unconfirmed: true, connectable: false, loading: true }],
    ["Claude before its startup reconcile is connected, unconfirmed", "claude", on(claude("none", { status: "unknown" })), { state: "connected", unconfirmed: true }],
    ["never connected while loading stays not connected", "codex", on(null, { remembered: false }), { state: "not_connected", loading: true }],
    ["paused wins over an unconfirmed connection", "claude", on(null, { agentsEnabled: false }), { state: "paused" }],
  ];
  it.each(cases)("%s", (_name, provider, input, expected) => {
    expect(deriveProviderRow(provider, input)).toMatchObject(expected);
  });

  it("does not dim a confirmed connection", () => {
    expect(deriveProviderRow("claude", on(claude("connected"))).unconfirmed).toBeUndefined();
  });

  it("does not mistake a failed versionless check for an installation", () => {
    const unknownApp = { ...surface("desktop", "check_failed"), engine_version: null, app_version: null };
    expect(detectedAgentSurfaces(codex([unknownApp]))).toEqual([]);
    expect(detectedAgentSurfaces(codex([surface("cli", "check_failed")]))).toHaveLength(1);
  });
});

describe("aggregateAgentsLight", () => {
  const rows = (codexResult: CodexPluginActionResult | null, claudeResult: ClaudePluginActionResult | null, config: Partial<AppConfig> = {}) =>
    agentRows({ codex_hud_enabled: true, claude_hud_enabled: true, ...config } as AppConfig, { codex: codexResult, claude: claudeResult });

  it("is Ready only when every remembered provider is connected", () => {
    expect(aggregateAgentsLight(true, rows(codex([surface("cli")]), claude("connected"))))
      .toEqual({ state: "ready", actionable: false, ariaLabel: "Agents is on and ready" });
  });
  it("reports a second provider's pending trust instead of hiding it behind success", () => {
    expect(aggregateAgentsLight(true, rows(codex([surface("cli")], { trust_required: true }), claude("connected"))))
      .toMatchObject({ state: "attention", actionable: true });
  });
  it("is attention but not actionable while connecting", () => {
    const value = agentRows({ claude_hud_enabled: false } as AppConfig, { codex: null, claude: null }, { codex: "idle", claude: "connecting" });
    expect(aggregateAgentsLight(true, value)).toMatchObject({ state: "attention", actionable: false });
  });
  it("is off with a precise label when paused or nothing is connected", () => {
    expect(aggregateAgentsLight(false, rows(null, null)).ariaLabel).toBe("Agents is paused");
    expect(aggregateAgentsLight(true, rows(null, null, { codex_hud_enabled: false, claude_hud_enabled: false })))
      .toEqual({ state: "off", actionable: false, ariaLabel: "No agents connected" });
  });
});

describe("agentConnections", () => {
  it("ignores stored surface choices and the master pause", () => {
    const config = { codex_hud_enabled: true, claude_hud_enabled: true, codex_agent_surface: "cli" } as AppConfig;
    const results = { codex: codex([surface("desktop", "legacy")]), claude: claude("connected") };
    expect(agentConnections(config, results)).toEqual({ codex: true, claude: true });
    expect(agentConnections({ ...config, agents_enabled: false }, results)).toEqual({ codex: false, claude: false });
  });
});
