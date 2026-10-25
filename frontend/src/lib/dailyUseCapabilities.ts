export type DailyUseCapabilityState =
  | "available"
  | "not-implemented"
  | "unavailable"
  | "external-dependency"
  | "requires-user-action";

export type DailyUseCapabilityPresentation = "enable" | "disable" | "explain";

export interface DailyUseCapability {
  id: string;
  state: DailyUseCapabilityState;
  reason: string;
  presentation: DailyUseCapabilityPresentation;
}

export interface DailyUseCapabilityReport {
  schema_version: 1;
  platform: string;
  capabilities: DailyUseCapability[];
}

export interface CapabilityDecision {
  enabled: boolean;
  explained: boolean;
  reason: string;
  state: DailyUseCapabilityState;
}

const STATES = new Set<DailyUseCapabilityState>([
  "available",
  "not-implemented",
  "unavailable",
  "external-dependency",
  "requires-user-action",
]);
const PRESENTATIONS = new Set<DailyUseCapabilityPresentation>([
  "enable",
  "disable",
  "explain",
]);

export function parseDailyUseCapabilityReport(value: unknown): DailyUseCapabilityReport {
  if (!value || typeof value !== "object") throw new Error("invalid capability report");
  const candidate = value as Partial<DailyUseCapabilityReport>;
  if (
    candidate.schema_version !== 1 ||
    typeof candidate.platform !== "string" ||
    !candidate.platform.trim() ||
    !Array.isArray(candidate.capabilities)
  ) {
    throw new Error("unsupported capability report");
  }
  const ids: string[] = [];
  for (const raw of candidate.capabilities) {
    if (
      !raw ||
      typeof raw.id !== "string" ||
      !raw.id ||
      typeof raw.reason !== "string" ||
      !raw.reason ||
      !STATES.has(raw.state) ||
      !PRESENTATIONS.has(raw.presentation)
    ) {
      throw new Error("invalid capability entry");
    }
    if ((raw.state === "available") !== (raw.presentation === "enable")) {
      throw new Error("inconsistent capability presentation");
    }
    ids.push(raw.id);
  }
  if (ids.join("\0") !== [...ids].sort().join("\0") || new Set(ids).size !== ids.length) {
    throw new Error("capabilities must be sorted and unique");
  }
  return candidate as DailyUseCapabilityReport;
}

export function capabilityDecision(
  report: DailyUseCapabilityReport | null,
  id: string,
): CapabilityDecision {
  if (!report) {
    return {
      enabled: false,
      explained: true,
      reason: "capability-loading",
      state: "unavailable",
    };
  }
  const capability = report.capabilities.find((item) => item.id === id);
  if (!capability) {
    return {
      enabled: false,
      explained: true,
      reason: "capability-unreported",
      state: "unavailable",
    };
  }
  return {
    enabled:
      capability.presentation === "enable" ||
      capability.state === "external-dependency" ||
      capability.state === "requires-user-action",
    explained: capability.presentation === "explain",
    reason: capability.reason,
    state: capability.state,
  };
}

export function capabilityExplanation(decision: CapabilityDecision): string {
  switch (decision.reason) {
    case "endpoint-required":
      return "Requires an endpoint you install and manage separately.";
    case "phase5-pending":
      return "Managed on-device cleanup is not available on this platform yet.";
    case "phase-excluded":
      return "Updates are not available in this internal Mac build.";
    case "adapter-unavailable":
      return "This language needs a speech model that is not available on this platform.";
    case "software-output-control-unsupported":
      return "Lowering and muting other audio is not supported on this platform.";
    case "output-control-unconfirmed":
      return "Lowering and muting other audio stays off until output support is confirmed.";
    case "capability-loading":
      return "Checking platform support...";
    case "capability-unreported":
      return "This action is unavailable because support could not be confirmed.";
    default:
      return decision.enabled ? "Available." : "Not available on this platform.";
  }
}
