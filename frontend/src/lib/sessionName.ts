/**
 * Shared session-name display normalizer and identity-phrase composer for
 * the HUD's accessible labels.
 *
 * `normalizeSessionNameForDisplay` is the TypeScript twin of
 * `normalize_session_name_for_speech`
 * (`src/whisperstream/agent_session_names.py`). Locked copy contract 1 requires the two stay in
 * agreement: hyphens, underscores, and slashes become spaces; camelCase and
 * PascalCase boundaries get a space inserted; whitespace runs collapse to
 * one space; leading/trailing whitespace is stripped. Case is never
 * changed, and no terminator is added or removed.
 *
 * `agentIdentityPhrase` composes the provider-aware identity phrase shared by
 * every Agent Speak surface: `<provider>, <name>` when a session name
 * resolved, or the bare provider label when it did not. It never emits a
 * trailing comma, since that would corrupt the `. `-joined accessible label
 * arrays it feeds. `claudeIdentityPhrase` remains the compatibility wrapper
 * for the shipped Claude call sites.
 */

import type { AgentName } from "./socket/agentEvents";

// Mirrors HUD.tsx's `CLAUDE_FALLBACK_NAME`. Kept as a literal rather than an
// import so this lib module has no dependency on the page module — HUD.tsx
// imports from here, not the reverse.
const CLAUDE_FALLBACK_NAME = "Claude";
const AGENT_LABELS: Record<AgentName, string> = {
  claude: CLAUDE_FALLBACK_NAME,
  codex: "Codex",
};

const CAMEL_BOUNDARY_RE = /(?<=[a-z0-9])(?=[A-Z])/g;
const SEPARATOR_RE = /[-_/]/g;
const WHITESPACE_RUN_RE = /\s+/g;

export function normalizeSessionNameForDisplay(name: string): string {
  const translated = name.replace(SEPARATOR_RE, " ");
  const spaced = translated.replace(CAMEL_BOUNDARY_RE, " ");
  return spaced.replace(WHITESPACE_RUN_RE, " ").trim();
}

export function claudeIdentityPhrase(sourceName: string): string {
  return agentIdentityPhrase("claude", sourceName);
}

export function agentProviderLabel(agent: AgentName): string {
  return AGENT_LABELS[agent];
}

export function agentIdentityPhrase(
  agent: AgentName,
  sourceName: string,
): string {
  const label = agentProviderLabel(agent);
  return sourceName.length > 0 && sourceName !== label
    ? `${label}, ${normalizeSessionNameForDisplay(sourceName)}`
    : label;
}
