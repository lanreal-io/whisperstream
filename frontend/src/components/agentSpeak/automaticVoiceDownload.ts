import type { SpeechEngine, SpeechEngineStatus, SpeechStatus } from "../../lib/api/speech";

/** Automatic downloads both voice models, Supertonic first so speech can
 *  start sooner, then Kokoro (owner decision, 2026-10-25). Returns the
 *  supported models still missing, in that order. */
export function missingAutomaticEngines(status: SpeechStatus): SpeechEngine[] {
  return (["supertonic", "kokoro"] as const).filter((engine) => {
    const model: SpeechEngineStatus | undefined = status.engines[engine];
    return Boolean(model && model.supported !== false && !model.installed);
  });
}

/** The confirm dialog's model for one or both missing Automatic models: a
 *  single model keeps its own name and size; both are named together with
 *  their combined size so the consent covers everything that downloads. */
export function automaticDownloadModel(
  status: SpeechStatus,
  engines: SpeechEngine[],
): { name: string; downloadBytes: number; installedBytes: number; plural: boolean } {
  const models = engines.map((engine) => status.engines[engine]);
  if (models.length === 1) {
    return {
      name: models[0].label,
      downloadBytes: models[0].download_bytes,
      installedBytes: models[0].installed_bytes,
      plural: false,
    };
  }
  return {
    name: "Both voice models",
    downloadBytes: models.reduce((sum, model) => sum + model.download_bytes, 0),
    installedBytes: models.reduce((sum, model) => sum + model.installed_bytes, 0),
    plural: true,
  };
}
