let downloadStartGeneration = 0;
import { fetchJSON, BASE } from "./client";


export async function startDownload(): Promise<void> {
  const generation = ++downloadStartGeneration;
  // A cold startup can still own the model lock when onboarding chooses
  // another artifact. Retry only that transient conflict, never dictation or
  // an existing download. The server revalidates current routing on admission.
  for (let attempt = 0; ; attempt += 1) {
    if (generation !== downloadStartGeneration) throw new DOMException("Download start cancelled", "AbortError");
    try {
      await fetchJSON(`${BASE}/api/download/start`, { method: "POST" });
      return;
    } catch (error) {
      let operationBusy = false;
      if (error instanceof Error && error.message.startsWith("409: ")) {
        try {
          operationBusy = JSON.parse(error.message.slice(5)).reason === "operation_busy";
        } catch { /* Unstructured conflicts are not safe to retry. */ }
      }
      if (!operationBusy || attempt >= 119) throw error;
      await new Promise<void>((resolve) => window.setTimeout(resolve, 500));
    }
  }
}


/** Ask the model-download/activation worker to stop.
 *
 *  The backend joins the worker thread with a 10 second ceiling, so `status` is
 *  authoritative about whether the worker is DONE: `cancelled` means it exited
 *  and has already broadcast its terminal event, `cancelling` means it is still
 *  running (still deleting `.tmp` files and still restoring the prior routing
 *  state), and `not_running` means there was nothing to stop. Callers must not
 *  treat `cancelling` as a settled cancel. */
export async function cancelDownload(): Promise<{ status: string }> {
  downloadStartGeneration += 1;
  return fetchJSON(`${BASE}/api/download/cancel`, { method: "POST" });
}


export type SttArtifactId =
  | "nemo-parakeet-tdt-0.6b-v3"
  | "nemo-parakeet-tdt-0.6b-v2"
  | "qwen3-asr-0.6b-int8"
  | "cohere-transcribe-03-2026-int8";


export interface SttArtifactStatus {
  id: SttArtifactId;
  backend: "parakeet" | "qwen" | "cohere";
  downloaded: boolean;
  /** True only for the exact artifact owned by the live recognizer. */
  active: boolean;
  /** Logical bytes currently on disk, including incomplete downloads. */
  size_bytes: number;
}


export interface ExactSttStatus {
  configured_artifact_id: SttArtifactId;
  loaded_artifact_id: SttArtifactId | null;
  artifacts: SttArtifactStatus[];
  /** Bytes held by artifacts that are neither configured nor loaded. */
  unused_size_bytes: number;
}


/** Transitional collapsed status used by the existing recovery probe. Exact
 *  artifact consumers should use the fields on `ExactSttStatus`; Phase 3 moves
 *  the remaining call sites off these backend conveniences. */
export interface SttStatus {
  parakeet: { downloaded: boolean; active: boolean };
  qwen: { downloaded: boolean; active: boolean };
  cohere: { downloaded: boolean; active: boolean };
}


export type SttStatusResponse = ExactSttStatus & SttStatus;


export async function getSttStatus(): Promise<SttStatusResponse> {
  return fetchJSON(`${BASE}/api/stt/status`);
}


/** Delete one exact, inactive STT artifact from disk.
 *
 *  Registry model ids are the only identifiers that cross this boundary, so
 *  Parakeet's English-only and multilingual artifacts stay independently
 *  addressable behind the single user-facing Parakeet choice. The backend
 *  refuses a configured, loaded, or busy target with a 409, which `fetchJSON`
 *  raises as an Error carrying the reason — callers must surface it rather
 *  than assume the delete happened. Missing targets are idempotent. */
export async function deleteSttModel(
  modelId: SttArtifactId,
): Promise<{ status: string; stt_status?: ExactSttStatus }> {
  return fetchJSON(`${BASE}/api/stt/delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_id: modelId }),
  });
}


/** Delete every downloaded STT artifact that is neither configured nor loaded.
 *
 *  The bulk counterpart to `deleteSttModel`, and the only way to reclaim the
 *  Parakeet variant the picker hides behind its single user-facing choice. The
 *  backend derives the eligible set itself from the same protected identities
 *  the status endpoint reports, so this takes no arguments: a frontend-supplied
 *  list would be a stale snapshot of what is safe to remove. `removed_model_ids`
 *  reports what actually went, which can be shorter than the caller expected
 *  (or empty), so callers MUST re-read the status rather than assume the disk
 *  now matches their pre-click view. Dictation-active and operation-busy
 *  refusals arrive as a 409 that `fetchJSON` raises. */
export async function deleteUnusedSttModels(): Promise<{
  status: string;
  removed_model_ids: SttArtifactId[];
  stt_status?: ExactSttStatus;
}> {
  return fetchJSON(`${BASE}/api/stt/delete-unused`, { method: "POST" });
}


/** Fetch free disk space at any target path (or the default models root when
 *  `path` is omitted). Used by the Parakeet onboarding pre-check and the
 *  low-disk re-target re-check. `free_mb` may be -1 when the OS denies the
 *  query; treat as "unknown" and fail open (proceed with the download). */
export async function getModelsDiskFree(
  path?: string,
  requiredMb?: number,
): Promise<{ free_mb: number; required_mb: number; sufficient: boolean }> {
  const params = new URLSearchParams();
  if (path != null) params.set("path", path);
  if (requiredMb != null) params.set("required_mb", String(requiredMb));
  const query = params.toString() ? `?${params.toString()}` : "";
  return fetchJSON(`${BASE}/api/models/disk-free${query}`);
}


/** Classify a relocation destination before moving model files.
 *
 *  Returns one of three outcomes:
 *  - `"ok"` — empty/usable folder with room; includes `total_bytes` and `free_bytes`.
 *  - `"reuse"` — destination already holds a complete valid model set.
 *  - `"blocked"` — cannot use; `reason` is `"no_room"`, `"unwritable"`, or `"foreign"`.
 */
export async function checkRelocate(destination: string): Promise<{
  outcome: "ok" | "reuse" | "blocked";
  reason?: "no_room" | "unwritable" | "foreign";
  total_bytes?: number;
  free_bytes?: number;
}> {
  return fetchJSON(`${BASE}/api/models/relocate/check`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ destination }),
  });
}


/** Spawn the background relocation worker. Progress is broadcast over the
 *  WebSocket as `download.*` events with `kind === "relocate"`. Cancel via
 *  the existing `cancelDownload()` (shared thread slot). */
export async function startRelocate(destination: string): Promise<void> {
  await fetchJSON(`${BASE}/api/models/relocate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ destination }),
  });
}


export async function startRegisteredRelocate(destinationToken: string): Promise<void> {
  await fetchJSON(`${BASE}/api/models/relocate/registered`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ destination_token: destinationToken }),
  });
}
