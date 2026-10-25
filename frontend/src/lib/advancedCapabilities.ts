export type AdvancedCapabilityState =
  | "available"
  | "development-only"
  | "not-implemented"
  | "unavailable"
  | "external-dependency";

export type AdvancedCapabilityPresentation = "enable" | "disable" | "explain";

export interface AdvancedCapability {
  id: string;
  state: AdvancedCapabilityState;
  reason: string;
  presentation: AdvancedCapabilityPresentation;
}

export interface AdvancedCapabilityReport {
  schema_version: 1;
  platform: string;
  capabilities: AdvancedCapability[];
}

export interface AdvancedCapabilityDecision {
  enabled: boolean;
  explained: boolean;
  reason: string;
  state: AdvancedCapabilityState;
  presentation: AdvancedCapabilityPresentation;
}

const STATES = new Set<AdvancedCapabilityState>([
  "available",
  "development-only",
  "not-implemented",
  "unavailable",
  "external-dependency",
]);
const PRESENTATIONS = new Set<AdvancedCapabilityPresentation>([
  "enable",
  "disable",
  "explain",
]);

export function parseAdvancedCapabilityReport(value: unknown): AdvancedCapabilityReport {
  if (!value || typeof value !== "object") throw new Error("invalid advanced capability report");
  const candidate = value as Partial<AdvancedCapabilityReport>;
  if (
    candidate.schema_version !== 1 ||
    typeof candidate.platform !== "string" ||
    !candidate.platform.trim() ||
    !Array.isArray(candidate.capabilities)
  ) {
    throw new Error("unsupported advanced capability report");
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
      throw new Error("invalid advanced capability entry");
    }
    if ((raw.state === "available") !== (raw.presentation === "enable")) {
      throw new Error("inconsistent advanced capability presentation");
    }
    ids.push(raw.id);
  }

  if (ids.join("\0") !== [...ids].sort().join("\0") || new Set(ids).size !== ids.length) {
    throw new Error("advanced capabilities must be sorted and unique");
  }
  return candidate as AdvancedCapabilityReport;
}

export function advancedCapabilityDecision(
  report: AdvancedCapabilityReport | null,
  id: string,
): AdvancedCapabilityDecision {
  const capability = report?.capabilities.find((item) => item.id === id);
  if (!capability) {
    return {
      enabled: false,
      explained: true,
      reason: report ? "capability-unreported" : "capability-loading",
      state: "unavailable",
      presentation: "explain",
    };
  }
  return {
    enabled: capability.presentation === "enable",
    explained: capability.presentation === "explain",
    reason: capability.reason,
    state: capability.state,
    presentation: capability.presentation,
  };
}
