import { describe, expect, it } from "vitest";
import {
  buildBenchmarkVerdictPatch,
  onboardingVerdictContext,
  type BenchmarkVerdictContext,
} from "./benchmarkVerdictPatch";

const cached = { "gemma4-e2b-instruct": "viable" };
const config = { enhancement_local_gpu_verdicts: cached };

function ctx(overrides: Partial<BenchmarkVerdictContext> = {}): BenchmarkVerdictContext {
  return {
    failureFallback: "gemini",
    benchmarkModelId: "stream-1-mini",
    currentLocalModelId: "gemma4-e2b-instruct",
    installedLocalModelIds: ["stream-1-mini", "gemma4-e2b-instruct"],
    ...overrides,
  };
}

describe("buildBenchmarkVerdictPatch", () => {
  it("viable enables local, applies the measured model and merges verdicts", () => {
    const plan = buildBenchmarkVerdictPatch(
      { kind: "viable", appliedModelId: "stream-1-mini", verdicts: { "stream-1-mini": "viable" } },
      config,
      ctx(),
    );
    expect(plan.patch).toEqual({
      enhancement_enabled: true,
      enhancement_provider: "local",
      enhancement_local_model: "stream-1-mini",
      enhancement_local_gpu_verdicts: { ...cached, "stream-1-mini": "viable" },
    });
    expect(plan.refreshLocalModels).toBe(true);
    expect(plan.downloadModelId).toBeNull();
    expect(plan.deleteModelIds).toEqual([]);
    expect(plan.providerPick).toBeNull();
  });

  it("viable without an applied model keeps the model and caches the current one", () => {
    const plan = buildBenchmarkVerdictPatch({ kind: "viable" }, config, ctx());
    expect(plan.patch).toEqual({
      enhancement_enabled: true,
      enhancement_provider: "local",
      enhancement_local_gpu_verdicts: { ...cached, "gemma4-e2b-instruct": "viable" },
    });
    expect(plan.patch).not.toHaveProperty("enhancement_local_model");
  });

  it("marginal applied enables the chosen model", () => {
    const plan = buildBenchmarkVerdictPatch(
      { kind: "marginal", appliedModelId: "stream-1-mini", verdicts: { "stream-1-mini": "marginal" } },
      config,
      ctx(),
    );
    expect(plan.patch).toEqual({
      enhancement_enabled: true,
      enhancement_provider: "local",
      enhancement_local_model: "stream-1-mini",
      enhancement_local_gpu_verdicts: { ...cached, "stream-1-mini": "marginal" },
    });
    expect(plan.downloadModelId).toBeNull();
    expect(plan.refreshLocalModels).toBe(false);
  });

  it("marginal applied for a model not on disk requests the download gate", () => {
    const plan = buildBenchmarkVerdictPatch(
      { kind: "marginal", appliedModelId: "stream-1-mini", verdicts: { "stream-1-mini": "marginal" } },
      config,
      ctx({ installedLocalModelIds: ["gemma4-e2b-instruct"] }),
    );
    expect(plan.downloadModelId).toBe("stream-1-mini");
  });

  it("marginal kept only caches the verdict", () => {
    const plan = buildBenchmarkVerdictPatch(
      { kind: "marginal", appliedModelId: null, verdicts: { "stream-1-mini": "marginal" } },
      config,
      ctx(),
    );
    expect(plan.patch).toEqual({
      enhancement_local_gpu_verdicts: { ...cached, "stream-1-mini": "marginal" },
    });
    expect(plan.downloadModelId).toBeNull();
  });

  it("unviable on ordinary local enables and routes to Gemini through the consent chain", () => {
    const plan = buildBenchmarkVerdictPatch(
      { kind: "unviable", freshlyDownloaded: true, verdicts: { "stream-1-mini": "unviable" } },
      config,
      ctx({ failureFallback: "gemini" }),
    );
    expect(plan.patch).toEqual({
      enhancement_enabled: true,
      enhancement_local_gpu_verdicts: { ...cached, "stream-1-mini": "unviable" },
    });
    expect(plan.providerPick).toBe("gemini");
    expect(plan.deleteModelIds).toEqual(["stream-1-mini"]);
  });

  it("unviable on Gemini fallback without verdicts still enables and routes", () => {
    const plan = buildBenchmarkVerdictPatch(
      { kind: "unviable", freshlyDownloaded: false },
      config,
      ctx({ failureFallback: "gemini" }),
    );
    expect(plan.patch).toEqual({
      enhancement_enabled: true,
      enhancement_local_gpu_verdicts: cached,
    });
    expect(plan.deleteModelIds).toEqual([]);
  });

  it.each(["current", "local"] as const)(
    "unviable with %s fallback only caches real verdicts",
    (failureFallback) => {
      const plan = buildBenchmarkVerdictPatch(
        { kind: "unviable", freshlyDownloaded: false, verdicts: { "stream-1-mini": "unviable" } },
        config,
        ctx({ failureFallback }),
      );
      expect(plan.patch).toEqual({
        enhancement_local_gpu_verdicts: { ...cached, "stream-1-mini": "unviable" },
      });
      expect(plan.providerPick).toBeNull();
      expect(plan.deleteModelIds).toEqual([]);
    },
  );

  it.each(["current", "local"] as const)(
    "unviable with %s fallback and no verdicts writes nothing",
    (failureFallback) => {
      const plan = buildBenchmarkVerdictPatch(
        { kind: "unviable", freshlyDownloaded: false },
        config,
        ctx({ failureFallback }),
      );
      expect(plan.patch).toBeNull();
    },
  );
});

describe("onboardingVerdictContext", () => {
  it("never routes an unviable verdict to Gemini and leaves enhancement off", () => {
    const plan = buildBenchmarkVerdictPatch(
      { kind: "unviable", freshlyDownloaded: true, verdicts: { "stream-1-mini": "unviable" } },
      { enhancement_local_gpu_verdicts: {} },
      onboardingVerdictContext("stream-1-mini"),
    );
    expect(plan.providerPick).toBeNull();
    expect(plan.patch).toEqual({
      enhancement_local_gpu_verdicts: { "stream-1-mini": "unviable" },
    });
    expect(plan.deleteModelIds).toEqual(["stream-1-mini"]);
  });

  it("viable enables local enhancement with Stream-1 Mini", () => {
    const plan = buildBenchmarkVerdictPatch(
      { kind: "viable", appliedModelId: "stream-1-mini", verdicts: { "stream-1-mini": "viable" } },
      { enhancement_local_gpu_verdicts: {} },
      onboardingVerdictContext("stream-1-mini"),
    );
    expect(plan.patch).toMatchObject({
      enhancement_enabled: true,
      enhancement_provider: "local",
      enhancement_local_model: "stream-1-mini",
    });
  });

  it("marginal applied never requests a download for the model it just measured", () => {
    const plan = buildBenchmarkVerdictPatch(
      { kind: "marginal", appliedModelId: "stream-1-mini", verdicts: { "stream-1-mini": "marginal" } },
      { enhancement_local_gpu_verdicts: {} },
      onboardingVerdictContext("stream-1-mini"),
    );
    expect(plan.downloadModelId).toBeNull();
  });
});
