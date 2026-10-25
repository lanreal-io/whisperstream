// @vitest-environment node

import { describe, expect, it } from "vitest";
import {
  capabilityDecision,
  parseDailyUseCapabilityReport,
} from "./dailyUseCapabilities";

const report = {
  schema_version: 1,
  platform: "Darwin",
  capabilities: [
    { id: "auto-update", state: "unavailable", reason: "phase-excluded", presentation: "disable" },
    { id: "cloud-enhancement", state: "available", reason: "shared-transport", presentation: "enable" },
    { id: "launch-at-login", state: "available", reason: "smappservice-main-app", presentation: "enable" },
    { id: "managed-local-enhancement", state: "not-implemented", reason: "phase5-pending", presentation: "disable" },
    { id: "model-lifecycle", state: "available", reason: "fluidaudio-verified", presentation: "enable" },
    { id: "ollama-enhancement", state: "external-dependency", reason: "endpoint-required", presentation: "explain" },
    { id: "window-lifecycle", state: "available", reason: "appkit-window-lifecycle", presentation: "enable" },
  ],
};

describe("daily-use capability hydration", () => {
  it("accepts a sorted schema-v1 report and derives presentation only from it", () => {
    const parsed = parseDailyUseCapabilityReport(report);
    expect(capabilityDecision(parsed, "model-lifecycle")).toEqual({
      enabled: true,
      explained: false,
      reason: "fluidaudio-verified",
      state: "available",
    });
    expect(capabilityDecision(parsed, "ollama-enhancement")).toEqual({
      enabled: true,
      explained: true,
      reason: "endpoint-required",
      state: "external-dependency",
    });
    expect(capabilityDecision(parsed, "managed-local-enhancement").enabled).toBe(false);
    expect(capabilityDecision(parsed, "auto-update").enabled).toBe(false);
    expect(capabilityDecision(parsed, "launch-at-login").enabled).toBe(true);
    expect(capabilityDecision(parsed, "window-lifecycle").enabled).toBe(true);
  });

  it("fails closed for missing, malformed, or unsorted reports", () => {
    expect(capabilityDecision(null, "model-lifecycle")).toEqual({
      enabled: false,
      explained: true,
      reason: "capability-loading",
      state: "unavailable",
    });
    expect(() => parseDailyUseCapabilityReport({ ...report, schema_version: 2 })).toThrow();
    expect(() =>
      parseDailyUseCapabilityReport({ ...report, capabilities: [...report.capabilities].reverse() }),
    ).toThrow();
  });
});
