import type { AppConfig } from "../../lib/api/config";
import type { BenchmarkVerdict } from "../../components/BenchmarkFlow";

/** Where a terminal benchmark verdict is applied from. Snapshotted when the
 *  flow opens:
 *  - failureFallback "gemini": ordinary Local in Enhancement; an unviable
 *    verdict routes to Gemini through the consent chain.
 *  - "current": an existing cloud provider (or onboarding); stay put and only
 *    cache real verdicts.
 *  - "local": Lockdown; stay on-device and only cache real verdicts. */
export interface BenchmarkVerdictContext {
  failureFallback: "gemini" | "current" | "local";
  /** The model this flow measured (deleted on give-up when freshly downloaded). */
  benchmarkModelId: string;
  /** config.enhancement_local_model at flow-open; cached as viable when an
   *  older backend reports no verdicts. */
  currentLocalModelId: string;
  /** Local models on disk, for the marginal download gate. */
  installedLocalModelIds: readonly string[];
}

/** One combined config patch plus the side effects the caller performs. The
 *  patch is null when nothing should be written. App.tsx debounces config
 *  writes, so callers must issue the patch as ONE write. */
export interface BenchmarkVerdictPlan {
  patch: Partial<AppConfig> | null;
  /** Refresh the local model picker (the applied model is now on disk). */
  refreshLocalModels: boolean;
  /** Applied model that is not on disk; route through the download gate. */
  downloadModelId: string | null;
  /** Models this flow freshly downloaded and the user gave up on. Delete after
   *  the benchmark's llama-server releases the GGUF handle. */
  deleteModelIds: string[];
  /** Provider to pick through the consent chain after the patch. */
  providerPick: "gemini" | null;
}

/** Pure mapping from a BenchmarkFlow terminal verdict to config changes.
 *  Enhancement and onboarding share it; only the context differs. */
export function buildBenchmarkVerdictPatch(
  v: BenchmarkVerdict,
  config: Pick<AppConfig, "enhancement_local_gpu_verdicts">,
  ctx: BenchmarkVerdictContext,
): BenchmarkVerdictPlan {
  const plan: BenchmarkVerdictPlan = {
    patch: null,
    refreshLocalModels: false,
    downloadModelId: null,
    deleteModelIds: [],
    providerPick: null,
  };
  const cached = config.enhancement_local_gpu_verdicts;

  if (v.kind === "viable") {
    // provider=local covers the cloud-switch case; a no-op when already local.
    // Fallback (older backend / no verdicts) caches just the active model.
    const patch: Partial<AppConfig> = {
      enhancement_enabled: true,
      enhancement_provider: "local",
      enhancement_local_gpu_verdicts: {
        ...cached,
        ...(v.verdicts ?? { [v.appliedModelId ?? ctx.currentLocalModelId]: "viable" }),
      },
    };
    if (v.appliedModelId) patch.enhancement_local_model = v.appliedModelId;
    plan.patch = patch;
    // The applied model was just benched, so it is always on disk.
    plan.refreshLocalModels = true;
    return plan;
  }

  if (v.kind === "marginal") {
    // appliedModelId null ("Keep my current model" / "Leave enhancement off")
    // leaves provider/enabled/model untouched and only caches the verdict.
    const patch: Partial<AppConfig> = {
      enhancement_local_gpu_verdicts: { ...cached, ...v.verdicts },
    };
    if (v.appliedModelId) {
      patch.enhancement_enabled = true;
      patch.enhancement_provider = "local";
      patch.enhancement_local_model = v.appliedModelId;
      if (!ctx.installedLocalModelIds.includes(v.appliedModelId)) {
        plan.downloadModelId = v.appliedModelId;
      }
    }
    plan.patch = patch;
    return plan;
  }

  // unviable. Auto-delete only the model THIS flow freshly downloaded; a
  // pre-existing model is left alone.
  if (v.freshlyDownloaded) plan.deleteModelIds = [ctx.benchmarkModelId];
  // Merge only REAL measured verdicts; never cache an unmeasured model.
  if (ctx.failureFallback === "gemini") {
    plan.patch = {
      enhancement_enabled: true,
      enhancement_local_gpu_verdicts: { ...cached, ...(v.verdicts ?? {}) },
    };
    plan.providerPick = "gemini";
  } else if (v.verdicts) {
    plan.patch = { enhancement_local_gpu_verdicts: { ...cached, ...v.verdicts } };
  }
  return plan;
}

/** Onboarding's context: the measured model is the one just downloaded and
 *  benched, and an unviable verdict never routes to Gemini (enhancement stays
 *  off; only real verdicts are cached). */
export function onboardingVerdictContext(modelId: string): BenchmarkVerdictContext {
  return {
    failureFallback: "current",
    benchmarkModelId: modelId,
    currentLocalModelId: modelId,
    installedLocalModelIds: [modelId],
  };
}
