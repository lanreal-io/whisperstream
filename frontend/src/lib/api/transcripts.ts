import { fetchJSON, BASE, extractPath, logApiRequest, logApiError, logApiResponse } from "./client";


// ---------------------------------------------------------------------------
// [Transcripts] history + import endpoints
// Shapes mirror the backend in src/whisperstream/server.py (list/get/delete/
// clear/purge/import) and transcript_store.py. Never log transcript text.
// ---------------------------------------------------------------------------

/** Immutable, encrypted snapshot of the profile used for one enhancement
 *  attempt. `timeout` / `error` with enhanced text means a partial fallback;
 *  without enhanced text it means Whisperstream kept the original. */
export interface TranscriptEnhancementProvenance {
  profile_id: string;
  profile_name: string;
  outcome: "success" | "timeout" | "error";
}


/** A row in the transcript list view (metadata + a 2-line snippet). The full
 *  text is fetched per-record via getTranscript. */
export interface TranscriptListItem {
  id: string;
  created_at: number;
  duration_ms: number;
  has_audio: boolean;
  /** True when the displayed snippet and default full-text view are enhanced. */
  has_enhanced: boolean;
  /** Encrypted-at-rest enhancement snapshot, or null for a no-attempt,
   *  legacy, corrupt, unknown, or contradictory record. */
  enhancement_provenance: TranscriptEnhancementProvenance | null;
  snippet: string;
}


/** Cursor-paginated list response. `next_cursor` is the `created_at` of the
 *  last item when more pages remain, else null. `decryptable` is present and
 *  false only when the store's data key can't be unsealed on this profile. */
export interface TranscriptListResponse {
  items: TranscriptListItem[];
  next_cursor: number | null;
  decryptable?: boolean;
  availability?: "empty" | "ready" | "locked" | "unavailable" | "not-decryptable" | "key-error" | "key-missing";
}


/** A full transcript record (detail view). `enhanced_text` is null when no
 *  enhancement ran or an attempt completely fell back to Original; `peaks`
 *  is null when no audio was saved. */
export interface TranscriptRecord {
  id: string;
  created_at: number;
  duration_ms: number;
  model: string;
  raw_text: string;
  enhanced_text: string | null;
  enhancement_provenance: TranscriptEnhancementProvenance | null;
  peaks: number[] | null;
  has_audio: boolean;
}


export type DevTranscriptExecutionMode = "whole" | "batched";


export interface DevTranscriptEnhancementResult {
  model_id: string;
  serving_contract: "raw-v1" | "wrap-v2" | "chatml";
  mode: DevTranscriptExecutionMode;
  profile_mode: "chat" | "code" | "email";
  chunk_count: number;
  elapsed_ms: number;
  output: string;
}


export interface TranscriptReenhancementResult {
  provider: string;
  model_id: string;
  profile_id: string;
  profile_name: string;
  profile_mode: string;
  mode: DevTranscriptExecutionMode;
  chunk_count: number;
  elapsed_ms: number;
  output: string;
  unchanged: boolean;
  unchanged_reason: "short_input" | "provider_or_safeguard_fallback" | null;
}


interface TranscriptReenhancementErrorBody {
  error?: string;
}


export class TranscriptReenhancementError extends Error {
  readonly status: number | null;
  readonly code: string | null;

  constructor({ status, code }: { status: number | null; code?: string | null }) {
    super(code ?? "transcript_reenhancement_failed");
    this.name = "TranscriptReenhancementError";
    this.status = status;
    this.code = code ?? null;
  }
}


export interface TranscriptBakeoffSelection {
  model_id: string;
  profile_id: string;
}


export interface TranscriptBakeoffStartResult {
  run_id: string;
  generation: number;
  total_count: number;
}


export interface TranscriptBakeoffCancelResult {
  run_id: string;
  cancelled: boolean;
}


export interface TranscriptBakeoffRetryResult {
  run_id: string;
  generation: number;
  pair_index: number;
}


interface TranscriptBakeoffErrorBody {
  error?: string;
  required_mb?: number;
  available_mb?: number;
}


export class TranscriptBakeoffApiError extends Error {
  readonly status: number | null;
  readonly code: string | null;
  readonly requiredMb: number | null;
  readonly availableMb: number | null;

  constructor({
    status,
    code,
    requiredMb,
    availableMb,
  }: {
    status: number | null;
    code?: string | null;
    requiredMb?: number | null;
    availableMb?: number | null;
  }) {
    super(code ?? "bakeoff_request_failed");
    this.name = "TranscriptBakeoffApiError";
    this.status = status;
    this.code = code ?? null;
    this.requiredMb = requiredMb ?? null;
    this.availableMb = availableMb ?? null;
  }
}


interface DevTranscriptEnhancementErrorBody {
  error?: string;
  model_id?: string;
  profile_mode?: string;
}


/** Structured dev-evaluation failure. The UI uses the backend's stable error
 * code to choose recovery copy without parsing an exception string. */
export class DevTranscriptEnhancementError extends Error {
  readonly status: number | null;
  readonly code: string | null;
  readonly modelId: string | null;
  readonly profileMode: string | null;

  constructor({
    status,
    code,
    modelId,
    profileMode,
  }: {
    status: number | null;
    code?: string | null;
    modelId?: string | null;
    profileMode?: string | null;
  }) {
    super(code ?? "dev_transcript_evaluation_failed");
    this.name = "DevTranscriptEnhancementError";
    this.status = status;
    this.code = code ?? null;
    this.modelId = modelId ?? null;
    this.profileMode = profileMode ?? null;
  }
}


/** Turn on saving (Mac and Windows). `needs_recovery_setup` tells the caller to
 *  continue into recovery-key setup; a 409 `history_key_missing` means the
 *  protected key is gone and the missing-key screen should take over. */
export async function enableTranscriptHistory(
  keychainConsent = true,
): Promise<{ ok: true; needs_recovery_setup?: boolean }> {
  return fetchJSON(`${BASE}/api/transcripts/enable`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      history_consent: true,
      ...(keychainConsent ? { keychain_consent: true } : {}),
    }),
  });
}


export async function listTranscripts(params?: {
  before?: number | null;
  limit?: number;
  q?: string;
}): Promise<TranscriptListResponse> {
  const search = new URLSearchParams();
  if (params?.before != null) search.set("before", String(params.before));
  if (params?.limit != null) search.set("limit", String(params.limit));
  if (params?.q) search.set("q", params.q);
  const query = search.toString();
  return fetchJSON(`${BASE}/api/transcripts${query ? `?${query}` : ""}`);
}


export async function getTranscript(id: string): Promise<TranscriptRecord> {
  return fetchJSON(`${BASE}/api/transcripts/${id}`);
}


/** Re-run a saved record's server-held raw text through the current production
 * enhancement configuration. The result is ephemeral and the dynamic record ID
 * is templated before any frontend API diagnostic is emitted. */
export async function reenhanceTranscript(
  id: string,
): Promise<TranscriptReenhancementResult> {
  const url = `${BASE}/api/experimental/transcripts/${encodeURIComponent(id)}/reenhance`;
  const method = "POST";
  const path = extractPath(url);
  const startedAt = performance.now();
  logApiRequest(method, path);

  let response: Response;
  try {
    response = await fetch(url, { method });
  } catch {
    logApiError(method, path, "network", "request_failed");
    throw new TranscriptReenhancementError({ status: null });
  }

  const durationMs = performance.now() - startedAt;
  if (response.ok) {
    logApiResponse(method, path, response.status, durationMs);
    return response.json();
  }

  let body: TranscriptReenhancementErrorBody = {};
  try {
    body = await response.json();
  } catch {
    // A non-JSON reverse-proxy response has no stable recovery code.
  }
  const code = typeof body.error === "string" ? body.error : null;
  logApiError(method, path, response.status, "HttpError");
  throw new TranscriptReenhancementError({ status: response.status, code });
}


async function requestTranscriptBakeoff<T>(
  url: string,
  body?: Record<string, unknown>,
): Promise<T> {
  const method = "POST";
  const path = extractPath(url);
  const startedAt = performance.now();
  logApiRequest(method, path);
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    logApiError(method, path, "network", "request_failed");
    throw new TranscriptBakeoffApiError({ status: null });
  }
  const durationMs = performance.now() - startedAt;
  if (response.ok) {
    logApiResponse(method, path, response.status, durationMs);
    return response.json();
  }
  let errorBody: TranscriptBakeoffErrorBody = {};
  try {
    errorBody = await response.json();
  } catch {
    // A non-JSON reverse-proxy response has no stable recovery metadata.
  }
  logApiError(method, path, response.status, "HttpError");
  throw new TranscriptBakeoffApiError({
    status: response.status,
    code: typeof errorBody.error === "string" ? errorBody.error : null,
    requiredMb: Number.isFinite(errorBody.required_mb)
      ? errorBody.required_mb
      : null,
    availableMb: Number.isFinite(errorBody.available_mb)
      ? errorBody.available_mb
      : null,
  });
}


/** Start a server-owned, memory-only comparison from one saved raw record. */
export function startTranscriptBakeoff(
  transcriptId: string,
  selections: TranscriptBakeoffSelection[],
): Promise<TranscriptBakeoffStartResult> {
  return requestTranscriptBakeoff(
    `${BASE}/api/experimental/transcripts/${encodeURIComponent(transcriptId)}/bakeoff`,
    { selections },
  );
}


export function cancelTranscriptBakeoff(
  runId: string,
): Promise<TranscriptBakeoffCancelResult> {
  return requestTranscriptBakeoff(
    `${BASE}/api/experimental/bakeoffs/${encodeURIComponent(runId)}/cancel`,
  );
}


export function retryTranscriptBakeoffResult(
  runId: string,
  pairIndex: number,
): Promise<TranscriptBakeoffRetryResult> {
  return requestTranscriptBakeoff(
    `${BASE}/api/experimental/bakeoffs/${encodeURIComponent(runId)}/retry`,
    { pair_index: pairIndex },
  );
}


/** Re-run one saved transcript's server-held raw text with the active candidate.
 * The client sends only the transcript ID, optimistic active-model guard, and
 * execution shape. Candidate output is returned ephemerally and never saved. */
export async function enhanceDevTranscript(
  id: string,
  modelId: string,
  mode: DevTranscriptExecutionMode,
): Promise<DevTranscriptEnhancementResult> {
  const url = `${BASE}/api/dev/transcripts/${encodeURIComponent(id)}/enhance`;
  const method = "POST";
  const path = extractPath(url);
  const startedAt = performance.now();
  logApiRequest(method, path);

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: modelId, mode }),
    });
  } catch {
    logApiError(method, path, "network", "request_failed");
    throw new DevTranscriptEnhancementError({ status: null });
  }

  const durationMs = performance.now() - startedAt;
  if (response.ok) {
    logApiResponse(method, path, response.status, durationMs);
    return response.json();
  }

  let body: DevTranscriptEnhancementErrorBody = {};
  try {
    body = await response.json();
  } catch {
    // A non-JSON reverse-proxy or network response has no stable recovery code.
  }
  const code = typeof body.error === "string" ? body.error : null;
  logApiError(method, path, response.status, "HttpError");
  throw new DevTranscriptEnhancementError({
    status: response.status,
    code,
    modelId: typeof body.model_id === "string" ? body.model_id : null,
    profileMode:
      typeof body.profile_mode === "string" ? body.profile_mode : null,
  });
}


export async function deleteTranscript(id: string): Promise<{ deleted: boolean }> {
  return fetchJSON(`${BASE}/api/transcripts/${id}`, { method: "DELETE" });
}


export async function clearTranscripts(): Promise<{ cleared: number }> {
  return fetchJSON(`${BASE}/api/transcripts/clear`, { method: "POST" });
}


/** Run the retention age-purge with an explicit window (called after the
 *  retention-shrink confirm dialog). `retention_days` of 0 = Forever (no-op). */
export async function purgeTranscripts(
  retention_days: number,
): Promise<{ purged: number }> {
  return fetchJSON(`${BASE}/api/transcripts/purge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ retention_days }),
  });
}


/** Kick off a path-based batch import (Tauri picker / drag-drop file paths).
 *  Returns immediately; per-file progress arrives via the WebSocket
 *  `import.progress` events and the list refreshes on `transcripts.changed`. */
export async function importAudio(paths: string[]): Promise<{ started: boolean }> {
  return fetchJSON(`${BASE}/api/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths }),
  });
}


export async function importAudioSelections(
  selectionTokens: string[],
): Promise<{ started: boolean }> {
  return fetchJSON(`${BASE}/api/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ selection_tokens: selectionTokens }),
  });
}


/** URL for a transcript's decrypted WAV. The Range-aware backend route serves
 *  it directly to an HTML5 `<audio>` element (do not fetch it through fetchJSON). */
export function transcriptAudioUrl(id: string): string {
  return `${BASE}/api/transcripts/${id}/audio`;
}
