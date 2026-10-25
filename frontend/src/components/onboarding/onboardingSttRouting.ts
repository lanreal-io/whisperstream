import type { AppConfig } from "../../lib/api/config";
import type { SttArtifactId, SttStatusResponse } from "../../lib/api/models";
import {
  recommendedSttArtifact,
  sttArtifactRegistryId,
  sttRoutingPatch,
  type ActiveSttArtifact,
  type SttRoutingPatch,
} from "../../lib/languages";

/** Exact model work required when the language step is committed.
 *
 * Onboarding is always an Automatic path. A previous interrupted run may have
 * left any recognizer resident, so readiness is based on the backend's exact
 * loaded artifact rather than the collapsed `model_loaded` health flag.
 */
export interface OnboardingSttRoutingPlan {
  artifact: ActiveSttArtifact;
  registryId: SttArtifactId;
  patch: SttRoutingPatch;
  targetDownloaded: boolean;
  needsActivation: boolean;
}

export function planOnboardingSttRouting(
  config: AppConfig,
  status: SttStatusResponse | null,
): OnboardingSttRoutingPlan {
  const language = config.language?.trim().toLowerCase() || "en";
  const artifact = recommendedSttArtifact(language);
  const registryId = sttArtifactRegistryId(artifact);
  const target = status?.artifacts.find((entry) => entry.id === registryId);

  return {
    artifact,
    registryId,
    patch: sttRoutingPatch({
      artifact,
      language,
      mode: "automatic",
      qwenForceLanguage: false,
      currentParakeetModel: config.parakeet_model,
    }),
    // A failed inventory probe must fail toward the idempotent prepare path.
    // It is safe for complete artifacts and avoids trusting stale local state.
    targetDownloaded: target?.downloaded ?? false,
    needsActivation: status?.loaded_artifact_id !== registryId,
  };
}
