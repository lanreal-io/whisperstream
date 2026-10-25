import type { EnhancementModelCapabilities } from "../../lib/api/enhancement";
import { LOCAL_MODEL_DISPLAY_LABEL } from "../../lib/localModels";


/** Shape of a bundled local GGUF model row returned by /api/enhancement/models?provider=local. */
export type LocalModel = {
  id: string;
  label: string;
  disk_mb: number;
  ram_mb: number;
  /** Threshold the LowRamDialog compares against (total system RAM).
   *  Optional: if absent we fall back to ``ram_mb`` so older catalog rows
   *  keep their previous behavior. */
  min_system_ram_mb?: number;
  recommended?: boolean;
  /** Registry prompt family. "stream" and "stream-mini" own fixed model contracts. */
  prompt_key?: string;
  capabilities?: EnhancementModelCapabilities;
  /** Additive development-catalog metadata. Optional for compatibility with
   *  older production-only backends. */
  source?: "production" | "candidate";
  experiment_id?: string | null;
  parameter_size?: "350M" | "0.6B" | "0.8B" | "1.2B" | null;
  role?: string | null;
  status?: string | null;
  serving_contract?: "raw-v1" | "wrap-v2" | "chatml" | null;
  trained_modes?: ("chat" | "code" | "email")[] | null;
  artifact_available?: boolean;
  selectable?: boolean;
  downloadable?: boolean;
  active?: boolean;
};

/** "about 2.9 GB" / "about 697 MB" size disclosure for a model that still
 *  needs to be downloaded (copy-review.md Locked Copy Decisions #4/#5).
 *  Switches to GB (one decimal) at the 1024 MB boundary; below that, whole
 *  MB, matching Customizable (3195 -> "about 3.1 GB") and Stream-1 Mini
 *  (462 -> "about 462 MB"). */
export function formatModelSize(diskMb: number): string {
  if (diskMb >= 1024) {
    return `about ${(diskMb / 1024).toFixed(1)} GB`;
  }
  return `about ${Math.round(diskMb)} MB`;
}

/** Compact size for the picker ("462 MB", "3.1 GB"): the same binary-MB
 *  rounding as `formatModelSize`, without the "about" prefix, for trailing
 *  meta, helper lines and accessible names (design.md "Open panel rows"). */
export function formatModelSizeShort(diskMb: number): string {
  return formatModelSize(diskMb).replace(/^about /, "");
}

/** Display name for a picker row's accessible labels ("Download Stream-1 Mini,
 *  462 MB", "Delete Customizable"). Every production row has a distinct
 *  display label, so no badge suffix is needed. */
export function localModelDownloadLabel(id: string): string {
  return LOCAL_MODEL_DISPLAY_LABEL[id] ?? id;
}

/** Prefer server policy. The fallback supports older backends at one boundary. */
export function localModelCapabilities(model: LocalModel): EnhancementModelCapabilities {
  if (model.capabilities) return model.capabilities;
  const fixed = model.prompt_key === "stream" || model.prompt_key === "stream-mini";
  return {
    family: fixed ? "trained-mode" : "customizable",
    editable_prompt: !fixed,
    supported_modes: model.trained_modes ?? (fixed ? ["chat", "code"] : null),
  };
}
