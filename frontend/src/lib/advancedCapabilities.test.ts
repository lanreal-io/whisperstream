import { describe, expect, it } from "vitest";
import {
  advancedCapabilityDecision,
  parseAdvancedCapabilityReport,
  type AdvancedCapabilityReport,
} from "./advancedCapabilities";

const REPORT: AdvancedCapabilityReport = {
  schema_version: 1,
  platform: "Darwin",
  capabilities: [
    { id: "app-identity", state: "available", reason: "appkit-context", presentation: "enable" },
    { id: "app-path-browse", state: "unavailable", reason: "raw-path-boundary-unavailable", presentation: "disable" },
    { id: "browser-page-identity", state: "available", reason: "accessibility-host-only", presentation: "enable" },
  ],
};

describe("advanced capability reports", () => {
  it("parses sorted reports and returns authoritative presentation decisions", () => {
    const parsed = parseAdvancedCapabilityReport(REPORT);
    expect(advancedCapabilityDecision(parsed, "app-identity")).toMatchObject({
      enabled: true,
      presentation: "enable",
      reason: "appkit-context",
    });
    expect(advancedCapabilityDecision(parsed, "app-path-browse")).toMatchObject({
      enabled: false,
      presentation: "disable",
      reason: "raw-path-boundary-unavailable",
    });
  });

  it("fails closed when a capability is absent or the report is unavailable", () => {
    expect(advancedCapabilityDecision(REPORT, "missing")).toMatchObject({
      enabled: false,
      reason: "capability-unreported",
      presentation: "explain",
    });
    expect(advancedCapabilityDecision(null, "app-identity")).toMatchObject({
      enabled: false,
      reason: "capability-loading",
      presentation: "explain",
    });
  });

  it("rejects malformed, unsorted, duplicate, and inconsistent reports", () => {
    expect(() => parseAdvancedCapabilityReport({ ...REPORT, schema_version: 2 })).toThrow();
    expect(() =>
      parseAdvancedCapabilityReport({ ...REPORT, capabilities: [...REPORT.capabilities].reverse() }),
    ).toThrow();
    expect(() =>
      parseAdvancedCapabilityReport({
        ...REPORT,
        capabilities: [REPORT.capabilities[0], REPORT.capabilities[0]],
      }),
    ).toThrow();
    expect(() =>
      parseAdvancedCapabilityReport({
        ...REPORT,
        capabilities: [
          {
            id: "app-identity",
            state: "available",
            reason: "appkit-context",
            presentation: "disable",
          },
        ],
      }),
    ).toThrow();
  });
});
