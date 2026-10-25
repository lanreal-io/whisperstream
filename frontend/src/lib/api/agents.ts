import { fetchJSON, BASE } from "./client";


// ---------------------------------------------------------------------------
// Coding-agent plugin lifecycle endpoints
// These manage only Whisperstream-owned plugin files and registrations. The
// normal patchConfig flow owns each provider's independent enabled setting.
// ---------------------------------------------------------------------------

export type CodingAgentProvider = "codex" | "claude";


/** Stored for compatibility only; it no longer gates install or status. */
export type AgentSurfaceSelection = "auto" | "cli" | "desktop" | "both";

export interface AgentSurfaceCompatibility {
  surface: "cli" | "desktop";
  state: "check_failed" | "not_installed" | "update_required" | "capability_unavailable"
    | "supported" | "unknown_engine" | "legacy" | "ready";
  app_version: string | null;
  engine_version: string | null;
  minimum_version: string | null;
  reply_verified: boolean;
  transport: "legacy" | "claude_mod" | "codex_queue";
}

export interface AgentCompatibility {
  surfaces: AgentSurfaceCompatibility[];
}


export interface ClaudePluginActionResult {
  compatibility?: AgentCompatibility;
  ok: boolean;
  status?:
    | "unknown"
    | "installed"
    | "updated"
    | "current"
    | "failed"
    | "removed"
    | "not_installed"
    | "not_owned";
  version?: string;
  installed_version?: string | null;
  source_status?: "installed" | "updated" | "current";
  claude_cli_available?: boolean;
  reload_required?: boolean;
  reload_message?: string | null;
  error?: string | null;
  /** Status reads only: connected = live or recorded handshake for the current
   *  plugin version on any surface; waiting = installed, no handshake yet. */
  connection?: "connected" | "waiting" | "none";
}


export interface CodexPluginActionResult {
  compatibility?: AgentCompatibility;
  ok: boolean;
  status?:
    | "unknown"
    | "installed"
    | "updated"
    | "current"
    | "failed"
    | "removed"
    | "not_installed"
    | "not_owned";
  version?: string;
  installed_version?: string | null;
  source_status?: "installed" | "updated" | "current";
  codex_cli_available?: boolean;
  codex_cli_supported?: boolean;
  trust_required?: boolean;
  trust_message?: string | null;
  update_available?: boolean;
}


export interface ManagedCliActionResult {
  ok: boolean;
  status: "installed" | "not_installed" | "unavailable" | "failed";
  available: boolean;
  installed: boolean;
  restart_required?: boolean;
}


export interface AgentPluginActionResultByProvider {
  codex: CodexPluginActionResult;
  claude: ClaudePluginActionResult;
}


export type AgentPluginActionResult<
  Provider extends CodingAgentProvider = CodingAgentProvider,
> = AgentPluginActionResultByProvider[Provider];


export interface AgentEnableOptions {
  /** Connect reinstalls fresh. Resume and startup reconcile without force. */
  force?: boolean;
}

function enableRequest(options: AgentEnableOptions): RequestInit {
  return options.force
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ force: true }) }
    : { method: "POST" };
}

/** Install or update Whisperstream's Claude plugin. Throws on non-200. */
export async function enableClaudeHud(options: AgentEnableOptions = {}): Promise<ClaudePluginActionResult> {
  return fetchJSON(`${BASE}/api/claude-hud/enable`, enableRequest(options));
}


/** Read the last startup or explicit Claude plugin reconciliation result. */
export async function getClaudeHudStatus(refresh = false, signal?: AbortSignal): Promise<ClaudePluginActionResult> {
  return fetchJSON(`${BASE}/api/claude-hud/status${refresh ? "?refresh=true" : ""}`, signal ? { signal } : undefined);
}


/** Remove only Whisperstream-owned Claude plugin files and registration. */
export async function disableClaudeHud(): Promise<ClaudePluginActionResult> {
  return fetchJSON(`${BASE}/api/claude-hud/disable`, { method: "POST" });
}


/** Install or update Whisperstream's owned Codex plugin. Throws on non-200. */
export async function enableCodexHud(options: AgentEnableOptions = {}): Promise<CodexPluginActionResult> {
  return fetchJSON(`${BASE}/api/codex-hud/enable`, enableRequest(options));
}


/** Refresh Codex install, version, CLI capability, and hook-trust state. */
export async function getCodexHudStatus(refresh = false, signal?: AbortSignal): Promise<CodexPluginActionResult> {
  return fetchJSON(`${BASE}/api/codex-hud/status${refresh ? "?refresh=true" : ""}`, signal ? { signal } : undefined);
}


/** Remove only Whisperstream's owned Codex plugin and marketplace entry. */
export async function disableCodexHud(): Promise<CodexPluginActionResult> {
  return fetchJSON(`${BASE}/api/codex-hud/disable`, { method: "POST" });
}


/** Read whether the bundled managed-session command is exposed on user PATH. */
export async function getManagedCliStatus(): Promise<ManagedCliActionResult> {
  return fetchJSON(`${BASE}/api/managed-cli/status`);
}


/** Expose the bundled command on the current user's PATH. */
export async function installManagedCli(): Promise<ManagedCliActionResult> {
  return fetchJSON(`${BASE}/api/managed-cli/install`, { method: "POST" });
}


/** Remove only Whisperstream's exact CLI directory from user PATH. */
export async function uninstallManagedCli(): Promise<ManagedCliActionResult> {
  return fetchJSON(`${BASE}/api/managed-cli/uninstall`, { method: "POST" });
}


export interface AgentProviderLifecycleApi<
  Provider extends CodingAgentProvider,
> {
  enable: (options?: AgentEnableOptions) => Promise<AgentPluginActionResult<Provider>>;
  status: () => Promise<AgentPluginActionResult<Provider>>;
  disable: () => Promise<AgentPluginActionResult<Provider>>;
}


/** Provider-keyed adapters used by setup and Configure without branching on
 *  endpoint paths or widening one provider's result into the other's shape. */
export const AGENT_PROVIDER_LIFECYCLE_API: {
  [Provider in CodingAgentProvider]: AgentProviderLifecycleApi<Provider>;
} = {
  codex: {
    enable: enableCodexHud,
    status: getCodexHudStatus,
    disable: disableCodexHud,
  },
  claude: {
    enable: enableClaudeHud,
    status: getClaudeHudStatus,
    disable: disableClaudeHud,
  },
};


// ---------------------------------------------------------------------------
// Agent Speak setup flow (Task 1.5 frontend surface; backend in Tasks 1.3 and
// 1.4). Always-registered, non-dev-gated routes. The fire routes return
// immediately (fire-and-forget); teardown is idempotent.
// ---------------------------------------------------------------------------

/** Fire the setup flow's synthetic AskUserQuestion sequence through the real
 *  hook path. Returns `{ok: false, detail}` when `claude_hud_enabled` is off
 *  or acceptance is stale, rather than throwing. */
export async function fireSetupQuestion(): Promise<{
  ok: boolean;
  detail?: string;
}> {
  return fetchJSON(`${BASE}/api/setup/agent-speak/question`, {
    method: "POST",
  });
}


/** Ephemerally arm delivery mode overrides for step 4 testing. */
export interface SetupDeliveryPreviewPayload {
  show_summary_text?: boolean;
  agent_speech_enabled?: boolean;
  show_summaries?: boolean;
  read_aloud?: boolean;
}


export async function armSetupDeliveryPreview(
  payload: SetupDeliveryPreviewPayload,
): Promise<{
  ok: boolean;
  detail?: string;
  agent_summary_text_enabled?: boolean;
  agent_speech_enabled?: boolean;
}> {
  return fetchJSON(`${BASE}/api/setup/agent-speak/delivery-preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}


/** Release whatever a setup fire route may have left live (routed compose
 *  lease, question, response, blocked broker requests). Idempotent; always
 *  `{ok: true}`. Call on setup modal close and on step 4 unmount. */
export async function teardownSetup(): Promise<{
  ok: boolean;
  closed_interactions?: number;
}> {
  return fetchJSON(`${BASE}/api/setup/agent-speak/teardown`, {
    method: "POST",
  });
}


export interface SetupPluginDetail {
  plugin_dir: string | null;
  hook_definition: string | null;
}


/** Deliberate, setup-only reversal of `_public_claude_plugin_status`'s path
 *  stripping: the plugin folder path and the verbatim installed hook
 *  definition, for step 1's Show in Explorer and hook-definition disclosure.
 *  Never log either value. */
export async function getSetupPluginDetail(): Promise<SetupPluginDetail> {
  return fetchJSON(`${BASE}/api/setup/agent-speak/plugin-detail`);
}


/** Codex twin of the privacy-sensitive setup receipt route. Never log either
 *  returned value or render the path as text. */
export async function getSetupCodexPluginDetail(): Promise<SetupPluginDetail> {
  return fetchJSON(`${BASE}/api/setup/agent-speak/codex-plugin-detail`);
}
