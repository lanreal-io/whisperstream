import type { DailyUseCapabilityReport } from "../lib/dailyUseCapabilities";
import type { AdvancedCapabilityReport } from "../lib/advancedCapabilities";

export const WINDOWS_CAPABILITIES: DailyUseCapabilityReport = {
  schema_version: 1, platform: "Windows",
  capabilities: ["auto-update", "cloud-enhancement", "launch-at-login", "managed-local-enhancement", "model-lifecycle", "ollama-enhancement", "qwen-stt"].map(id => ({
    id, state: "available", reason: "implemented", presentation: "enable",
  })),
};
export const WINDOWS_ADVANCED_CAPABILITIES: AdvancedCapabilityReport = {
  schema_version: 1, platform: "Windows",
  capabilities: ["app-identity", "app-path-browse", "browser-page-identity"].map(id => ({
    id, state: "available", reason: "implemented", presentation: "enable",
  })),
};
