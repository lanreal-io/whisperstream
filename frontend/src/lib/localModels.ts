/** Display label for a local bundled model, by catalog id. Short and
 *  chip-friendly, distinct from the catalog's backend `label` field ("Gemma 4
 *  E2B Instruct" is too long for a trigger/row). Falls back to the catalog's
 *  own label for any future/unknown id.
 *
 *  Shared between the Enhancement local model picker and the BenchmarkFlow so
 *  both surfaces agree on how a catalog id is named. Do NOT duplicate these
 *  literals in either consumer. */
export const LOCAL_MODEL_DISPLAY_LABEL: Record<string, string> = {
  "stream-1-mini": "Stream-1 Mini",
  // Naming round §1/§9: the steerable general tier is named by the ONE
  // capability that justifies it ("Customizable"), never by vendor. The
  // backing model is named only in its panel description hover, so swapping
  // it never renames this row.
  "gemma4-e2b-instruct": "Customizable",
};

/** The one model the automatic first-setup speed check measures. Mirrors the
 *  backend's AUTO_BENCHMARK_MODEL_ID and catalog default. */
export const AUTO_BENCHMARK_MODEL_ID = "stream-1-mini";

/** Whether a catalog id is one of OUR first-party models versus a bundled
 *  third-party model (Google's Gemma). The picker uses this to choose the
 *  leading identity chip: the Whisperstream mark for fixed, trained models and
 *  a pen for the model whose prompts you write. Any future/unknown id
 *  defaults to third-party. */
export const LOCAL_MODEL_FIRST_PARTY: Record<string, boolean> = {
  "stream-1-mini": true,
  "gemma4-e2b-instruct": false,
};

/** One short line per model, shown only in the open picker panel and the
 *  speed-check intro card (design.md "Open panel rows"). */
export const LOCAL_MODEL_DESCRIPTION: Record<string, string> = {
  "stream-1-mini": "Fast enhancement for Chat and Email",
  "gemma4-e2b-instruct": "Write your own prompts. Built on Gemma 4.",
};

/** Optional hover on one phrase of a model's panel description. Customizable
 *  names its backing model here (owner review, October 19, 2026); this is the
 *  only surface where Google's Gemma is named in the picker. */
export const LOCAL_MODEL_DESCRIPTION_HOVER: Record<
  string,
  { term: string; hover: string } | undefined
> = {
  "gemma4-e2b-instruct": {
    term: "Gemma 4",
    hover:
      "Powered by Google's Gemma 4 E2B Instruct (4-bit). Write your own cleanup prompts. About 3.1 GB, runs entirely on your computer.",
  },
};

/** Read a model's cached GPU benchmark verdict from the per-model map
 *  (config.enhancement_local_gpu_verdicts). A model id absent from the map has
 *  never been benchmarked and reads as "not_run". Mirrors the backend
 *  get_local_gpu_verdict() helper so both surfaces agree on the default. */
export function getLocalGpuVerdict(
  verdicts: Record<string, string> | undefined,
  modelId: string,
): string {
  return verdicts?.[modelId] ?? "not_run";
}
