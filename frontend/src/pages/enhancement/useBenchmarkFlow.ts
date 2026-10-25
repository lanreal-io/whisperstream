import { useState } from "react";
import type { LocalModel } from "./localModels";
import { AUTO_BENCHMARK_MODEL_ID } from "../../lib/localModels";

/** Snapshot benchmark target and failure route when the flow opens. */
export function useBenchmarkFlow({ localBundledModels, currentLocalModelId, availableLocalModelIds, verdicts, isLockdown, provider }: {
  localBundledModels: LocalModel[];
  currentLocalModelId: string;
  availableLocalModelIds: string[];
  /** config.enhancement_local_gpu_verdicts. Empty = no local check has ever
   *  produced a verdict, which makes this a first setup. */
  verdicts: Record<string, string> | undefined;
  isLockdown: boolean;
  provider: string;
}) {
  // ---- Benchmark flow state (local-gpu-benchmark Task 3.3) ----
  // BenchmarkFlow is the single evolving modal that replaces the old
  // EnableLocalConfirmDialog seat. It mounts when the user enables AI
  // Enhancement while provider=local AND (the GGUF is not on disk OR the GPU
  // verdict has not been run yet), opening at the "intro" phase. The slow-result
  // "Check again" link reopens it at "prefilter" (skip the intro). The toggle
  // stays visually off the entire time — enhancement_enabled is only flipped
  // true inside the SINGLE combined patch the onVerdict handler issues.
  // `failureFallback` is snapshotted from the provider AT FLOW-OPEN so the
  // failure card + verdict routing know whether to push Gemini (ordinary local),
  // stay on the user's existing cloud provider, or stay local in Lockdown.
  const [benchmarkFlow, setBenchmarkFlow] = useState<
    null | {
      start: "intro" | "prefilter";
      mode: "setup" | "recheck";
      failureFallback: "gemini" | "current" | "local";
      // "auto" = the first-setup check, which measures only Stream-1 Mini.
      // Rechecks, explicit picks and users with earlier verdicts measure one
      // available managed model manually.
      benchMode?: "auto" | "manual";
      benchModelId?: string;
      applyModelOnSuccess?: boolean;
    }
  >(null);
  const openBenchmarkFlow = (
    start: "intro" | "prefilter",
    mode: "setup" | "recheck" = "setup",
  ) => {
    const selectedModel = localBundledModels.find(
      (model) => model.id === currentLocalModelId,
    );
    // Development candidates are activated as developer-owned artifacts and
    // intentionally cannot enter the managed production benchmark/download
    // path.
    if (mode === "recheck" && selectedModel?.source === "candidate") return false;

    const availableManagedModels = localBundledModels.filter(
      (model) =>
        model.source !== "candidate"
        && model.selectable !== false
        && model.downloadable !== false
        && availableLocalModelIds.includes(model.id),
    );
    const failureFallback = isLockdown
      ? "local"
      : (provider || "local") === "local"
        ? "gemini"
        : "current";

    // First setup (no verdict recorded yet) always checks Stream-1 Mini, even
    // when the stored model is the old Customizable default. Users with earlier
    // verdicts keep their own model.
    const firstSetup =
      mode === "setup"
      && Object.keys(verdicts ?? {}).length === 0
      && availableManagedModels.some((model) => model.id === AUTO_BENCHMARK_MODEL_ID);
    if (firstSetup) {
      setBenchmarkFlow({
        start,
        mode,
        failureFallback,
        benchMode: "auto",
        benchModelId: AUTO_BENCHMARK_MODEL_ID,
        applyModelOnSuccess: true,
      });
      return true;
    }

    const benchmarkModel =
      availableManagedModels.find((model) => model.id === currentLocalModelId)
      ?? availableManagedModels[0];
    if (!benchmarkModel) return false;

    const benchModelId = benchmarkModel.id;
    setBenchmarkFlow({
      start,
      mode,
      failureFallback,
      benchMode: "manual",
      benchModelId,
      applyModelOnSuccess:
        mode === "setup"
        || (!!benchModelId && benchModelId !== currentLocalModelId),
    });
    return true;
  };

  return { benchmarkFlow, setBenchmarkFlow, openBenchmarkFlow };
}
