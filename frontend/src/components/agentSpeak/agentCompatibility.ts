import type { AgentSurfaceCompatibility, ClaudePluginActionResult, CodexPluginActionResult, CodingAgentProvider } from "../../lib/api/agents";
import type { AppConfig } from "../../lib/api/config";

type Result = ClaudePluginActionResult | CodexPluginActionResult | null;

/** What a provider row is doing right now. A boolean cannot tell Connecting
 *  from Disconnecting from a list-wide check, and rows show which it is. */
export type ProviderActivity = "idle" | "connecting" | "disconnecting" | "resuming" | "checking";
export type AgentProviderActivityMap = Record<CodingAgentProvider, ProviderActivity>;
export const IDLE_ACTIVITY: AgentProviderActivityMap = { codex: "idle", claude: "idle" };

export type ProviderRowState =
  | "not_connected" | "connecting" | "waiting" | "connected"
  | "paused" | "resuming" | "update_needed" | "failed";

/** Usable detected Codex installs, which decide the trust instructions shown. */
export type CodexTrustRoutes = "app" | "cli" | "both";

export type ProviderFailure = "cliUnavailable" | "listFailed" | "installFailed" | "unsupported" | "other";

export interface ProviderRowModel {
  state: ProviderRowState;
  /** Connect (or Try again) can run: compatibility has arrived and the agent is detected. */
  connectable: boolean;
  /** Compatibility arrived and no install of the agent was detected. */
  missing: boolean;
  /** No install was found because detection itself failed, so check again. */
  detectionFailed: boolean;
  /** Compatibility has not arrived yet. */
  loading: boolean;
  codexRoutes?: CodexTrustRoutes;
  blocking?: { surface: AgentSurfaceCompatibility["surface"]; version: string; minimum: string };
  failure?: ProviderFailure;
  /** The waiting line is the direct result of a Connect click that installed. */
  installedPrefix: boolean;
  /** Connected on trust: the provider was connected before and this launch's
   *  first check has not answered yet. Rendered dimmed (owner, 2026-10-24). */
  unconfirmed?: boolean;
}

/** "installed" adds the Installed. prefix; "failed" keeps Try again on a row
 *  whose provider was never remembered because its Connect did not succeed. */
export type LastConnect = "installed" | "failed" | null;
export type AgentProviderLastConnectMap = Partial<Record<CodingAgentProvider, LastConnect>>;

export interface ProviderRowInput {
  agentsEnabled: boolean;
  /** `*_hud_enabled`: the provider the user connected, kept while paused. */
  remembered: boolean;
  activity: ProviderActivity;
  result: Result;
  /** Outcome of this session's last Connect click for the provider. */
  lastConnect?: LastConnect;
}

/** Installs that really exist. A versionless failed check is not an install. */
export function detectedAgentSurfaces(result: Result): AgentSurfaceCompatibility[] {
  return result?.compatibility?.surfaces.filter((surface) =>
    surface.state !== "not_installed" &&
    (surface.state !== "check_failed" || Boolean(surface.app_version || surface.engine_version))) ?? [];
}

const USABLE_STATES = new Set<AgentSurfaceCompatibility["state"]>(["ready", "legacy", "supported", "unknown_engine"]);
const INSTALLED_STATUSES = new Set(["current", "installed", "updated"]);

function pluginInstalled(result: Result): boolean {
  return result?.ok === true && INSTALLED_STATUSES.has(result.status ?? "");
}

function versionParts(version: string): number[] {
  return version.split(/[^0-9]+/).filter(Boolean).map(Number);
}

function compareVersions(left: string, right: string): number {
  const a = versionParts(left);
  const b = versionParts(right);
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const diff = (a[index] ?? 0) - (b[index] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

function claudeConnection(result: ClaudePluginActionResult | null): "connected" | "waiting" | "none" {
  if (result?.connection) return result.connection;
  // Older snapshots without the field: live reply evidence still counts.
  const live = result?.compatibility?.surfaces.some((surface) => surface.state === "ready" && surface.reply_verified);
  if (live) return "connected";
  return pluginInstalled(result) ? "waiting" : "none";
}

function failureFor(provider: CodingAgentProvider, result: Result): ProviderFailure {
  if (provider === "claude") {
    const error = (result as ClaudePluginActionResult | null)?.error;
    if (error === "claude_cli_unavailable") return "cliUnavailable";
    if (error === "plugin_list_failed" || error === "plugin_list_invalid_json") return "listFailed";
    if (error === "plugin_install_failed" || error === "plugin_update_failed") return "installFailed";
    return "other";
  }
  const codex = result as CodexPluginActionResult | null;
  if (codex?.codex_cli_available === false) return "cliUnavailable";
  if (codex?.codex_cli_supported === false) return "unsupported";
  return "other";
}

function codexRoutes(result: Result): CodexTrustRoutes | undefined {
  const usable = detectedAgentSurfaces(result).filter((surface) => surface.state !== "update_required");
  const app = usable.some((surface) => surface.surface === "desktop");
  const cli = usable.some((surface) => surface.surface === "cli");
  return app && cli ? "both" : app ? "app" : cli ? "cli" : undefined;
}

function blockingInstall(result: Result): ProviderRowModel["blocking"] | null {
  const detected = detectedAgentSurfaces(result);
  if (detected.some((surface) => USABLE_STATES.has(surface.state))) return null;
  const outdated = detected
    .filter((surface) => surface.state === "update_required")
    .map((surface) => ({ surface: surface.surface, version: surface.engine_version ?? surface.app_version ?? "", minimum: surface.minimum_version ?? "" }))
    .sort((left, right) => compareVersions(right.version, left.version));
  return outdated[0] ?? null;
}

/** The single row model for Settings and setup (design.md derivation order). */
export function deriveProviderRow(provider: CodingAgentProvider, input: ProviderRowInput): ProviderRowModel {
  const { result } = input;
  const loading = !result?.compatibility;
  const detected = detectedAgentSurfaces(result);
  const missing = !loading && detected.length === 0;
  const detectionFailed = missing && Boolean(result?.compatibility?.surfaces.some((surface) => surface.state === "check_failed"));
  const base = { connectable: !loading && !missing, missing, detectionFailed, loading, installedPrefix: false };

  if (input.activity === "connecting") return { ...base, state: "connecting" };
  if (input.activity === "resuming") return { ...base, state: "resuming" };
  if (!input.remembered) {
    return input.lastConnect === "failed"
      ? { ...base, state: "failed", failure: failureFor(provider, result) }
      : { ...base, state: "not_connected" };
  }
  if (!input.agentsEnabled) return { ...base, state: "paused" };
  // A remembered connection is trusted until the first check answers. The
  // backend reports Claude's plugin as "unknown" until its startup reconcile.
  const unanswered = loading || (provider === "claude" && result?.status === "unknown");
  if (unanswered) return { ...base, state: "connected", unconfirmed: true };
  if (result?.ok === false || result?.status === "failed" ||
      result?.status === "not_installed" || result?.status === "removed") {
    return { ...base, state: "failed", failure: failureFor(provider, result) };
  }
  const blocking = blockingInstall(result);
  if (blocking) return { ...base, state: "update_needed", blocking };
  if (provider === "codex") {
    const codex = result as CodexPluginActionResult | null;
    if (codex?.trust_required) {
      return { ...base, state: "waiting", codexRoutes: codexRoutes(result) ?? "both", installedPrefix: input.lastConnect === "installed" };
    }
    if (pluginInstalled(result)) return { ...base, state: "connected" };
  } else {
    const connection = claudeConnection(result as ClaudePluginActionResult | null);
    if (connection === "waiting") return { ...base, state: "waiting", installedPrefix: input.lastConnect === "installed" };
    if (connection === "connected") return { ...base, state: "connected" };
  }
  return { ...base, state: "not_connected", connectable: false };
}

export type AgentProviderRowMap = Record<CodingAgentProvider, ProviderRowModel>;
type ResultMap = { codex: CodexPluginActionResult | null; claude: ClaudePluginActionResult | null };

export function agentsEnabled(config: AppConfig): boolean {
  return config.agents_enabled ?? true;
}

export function rememberedProviders(config: AppConfig): Record<CodingAgentProvider, boolean> {
  return { codex: Boolean(config.codex_hud_enabled), claude: Boolean(config.claude_hud_enabled) };
}

export function agentRows(
  config: AppConfig,
  results: ResultMap,
  activity: AgentProviderActivityMap = IDLE_ACTIVITY,
  lastConnect: AgentProviderLastConnectMap = {},
): AgentProviderRowMap {
  const remembered = rememberedProviders(config);
  const enabled = agentsEnabled(config);
  const row = (provider: CodingAgentProvider) => deriveProviderRow(provider, {
    agentsEnabled: enabled, remembered: remembered[provider], activity: activity[provider],
    result: results[provider], lastConnect: lastConnect[provider] ?? null,
  });
  return { codex: row("codex"), claude: row("claude") };
}

export function agentConnections(config: AppConfig, results: ResultMap): Record<CodingAgentProvider, boolean> {
  const rows = agentRows(config, results);
  return { codex: rows.codex.state === "connected", claude: rows.claude.state === "connected" };
}

/** Waiting, update needed and couldn't connect are the states the user owns. */
export function rowNeedsUser(row: ProviderRowModel): boolean {
  return row.state === "waiting" || row.state === "update_needed" || row.state === "failed";
}

export interface AgentsLight {
  state: "ready" | "attention" | "off";
  /** Something needs the user, so General may offer the light as a button. */
  actionable: boolean;
  ariaLabel: string;
}

/** Attention whenever any remembered provider is unsettled; Ready only when all are connected. */
export function aggregateAgentsLight(enabled: boolean, rows: AgentProviderRowMap): AgentsLight {
  if (!enabled) return { state: "off", actionable: false, ariaLabel: "Agents is paused" };
  const tracked = Object.values(rows).filter((row) => row.state !== "not_connected");
  if (tracked.some(rowNeedsUser)) return { state: "attention", actionable: true, ariaLabel: "Agents needs attention" };
  if (tracked.some((row) => row.state === "connecting" || row.state === "resuming")) {
    return { state: "attention", actionable: false, ariaLabel: "Agents needs attention" };
  }
  if (tracked.length > 0 && tracked.every((row) => row.state === "connected")) {
    return { state: "ready", actionable: false, ariaLabel: "Agents is on and ready" };
  }
  return { state: "off", actionable: false, ariaLabel: "No agents connected" };
}
