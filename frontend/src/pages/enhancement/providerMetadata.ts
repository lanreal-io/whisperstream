import { type AppConfig } from "../../lib/api/config";


/* Cloud providers that store an API key in a dedicated config slot. Used to
 * route the key input + Remove key action to the slot for the active
 * provider so credentials persist across provider switches. */
export const PROVIDER_KEY_FIELD: Record<string, keyof AppConfig | undefined> = {
  gemini: "enhancement_gemini_api_key",
  openai: "enhancement_openai_api_key",
  anthropic: "enhancement_anthropic_api_key",
  custom: "enhancement_custom_api_key",
};

export function getStoredKeyForProvider(config: AppConfig, provider: string): string {
  const field = PROVIDER_KEY_FIELD[provider];
  if (!field) return "";
  return (config[field] as string | undefined) ?? "";
}

/* Server-computed masked fingerprint (first 4 + bullets + last 4) of a provider's
 * stored key. The full plaintext never reaches the client; this is the only
 * key-derived value shown in the UI. Empty when no key is set. */
export function getKeyFingerprintForProvider(config: AppConfig, provider: string): string {
  const field = PROVIDER_KEY_FIELD[provider];
  if (!field) return "";
  return (
    ((config as unknown as Record<string, unknown>)[field + "_fingerprint"] as
      | string
      | undefined) ?? ""
  );
}

/* Help links per provider. Short labels — the link text stays scannable. */
export const PROVIDER_HELP: Record<string, { url?: string; label?: string }> = {
  openai:    { url: "https://platform.openai.com/api-keys",      label: "Get an API key" },
  anthropic: { url: "https://console.anthropic.com/settings/keys", label: "Get an API key" },
  ollama:    { url: "https://ollama.com/download",                label: "Download Ollama" },
};

/* Provider options grouped by data-handling: the managed Local provider first,
 * then Ollama and the named cloud providers (Gemini, OpenAI, Anthropic), then Custom
 * set apart as the bring-your-own OpenAI-compatible endpoint. The dropdown inserts a
 * hairline at each group boundary; Local stays a first-class peer per scope.md. */
export const PROVIDERS: { value: string; label: string; kind: "local" | "cloud" }[] = [
  { value: "local",     label: "Local",     kind: "local" },
  { value: "ollama",    label: "Ollama",    kind: "cloud" },
  { value: "gemini",    label: "Gemini",    kind: "cloud" },
  { value: "openai",    label: "OpenAI",    kind: "cloud" },
  { value: "anthropic", label: "Anthropic", kind: "cloud" },
  { value: "custom",    label: "Custom",    kind: "cloud" },
];

/* Provider display names for the cloud-data indicator.
   Intentionally duplicated from DataLeavesDialog.tsx — that map is not
   exported. If a fifth provider lands, update both files. */
export const PROVIDER_DISPLAY: Record<string, string> = {
  gemini: "Google Gemini",
  openai: "OpenAI",
  anthropic: "Anthropic",
  ollama: "Ollama",
  custom: "your custom endpoint",
};

/** The address describes Ollama's first hop, not its model's execution location. */
export function isLocalOllamaEndpoint(endpoint?: string): boolean {
  if (!endpoint) return true;
  try {
    const hostname = new URL(endpoint).hostname;
    return (
      hostname === "localhost" ||
      hostname === "127.0.0.1" ||
      hostname === "::1" ||
      hostname === "[::1]"
    );
  } catch {
    return false;
  }
}

function endpointHost(endpoint?: string): string {
  if (!endpoint) return "";
  try {
    return new URL(endpoint).hostname;
  } catch {
    return "";
  }
}

export function providerDataTooltip(
  value: string,
  customHost?: string,
  ollamaEndpoint?: string,
): string {
  if (value === "local") return "Cleanup runs on this computer.";
  if (value === "ollama") {
    if (isLocalOllamaEndpoint(ollamaEndpoint)) {
      return "Server on this device; may use cloud models";
    }
    const host = endpointHost(ollamaEndpoint);
    return host
      ? `Sent to ${host} for cleanup.`
      : "Sent to the configured endpoint for cleanup.";
  }
  if (value === "custom")
    return customHost
      ? `Your dictation is sent to ${customHost} for cleanup, then comes back.`
      : "Your dictation is sent to the endpoint you set for cleanup, then comes back.";
  const name = PROVIDER_DISPLAY[value] ?? value;
  return `Your dictation is sent to ${name} for cleanup, then comes back.`;
}

/** True when the provider keeps data on the user's device (Shield icon); false
 *  for cloud providers (Cloud icon). */
export function providerStaysLocal(value: string, _ollamaEndpoint?: string): boolean {
  return value === "local";
}

/* ---------- Provider dropdown (extracted so both cloud + Ollama paths
 *            share the exact same trigger + menu) ----------
 *
 * The dropdown no longer talks to PrivacyModal directly. The Enhancement
 * page owns the two-step consent chain (DataLeavesDialog -> PrivacyModal)
 * and recipient review, so the dropdown's only
 * job is to surface "user picked a new provider" via onProviderPicked.
 */
export type ProviderValue = "local" | "gemini" | "openai" | "anthropic" | "ollama" | "custom";
