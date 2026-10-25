// @vitest-environment node

import { describe, expect, it } from "vitest";
import type { EnhancementModel } from "../lib/api/enhancement";
import { bakeoffModelLabel } from "./BakeoffWorkspace";

describe("Bakeoff candidate labels", () => {
  it("uses the registry display label before the internal experiment id", () => {
    const model: EnhancementModel = {
      id: "research-cand-07",
      label: "Research candidate 7",
      source: "candidate",
      experiment_id: "research-cand-07",
    };

    expect(bakeoffModelLabel(model)).toBe("Research candidate 7");
  });
});
