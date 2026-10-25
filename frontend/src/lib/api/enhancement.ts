import { BASE, fetchJSON } from "./client";

export interface EnhancementThinking {
  kind: "configurable" | "mandatory" | "unknown" | "fixed";
  options: string[];
  default: string;
  effective: string;
  preference_key: string;
}

/** Request policy is resolved by the backend for the saved connection. */
export function getEnhancementThinking(): Promise<EnhancementThinking> {
  return fetchJSON(`${BASE}/api/enhancement/thinking`);
}

export type WorkspaceErrorCode = "workspace_required" | "workspace_invalid" | "workspace_unavailable";


export async function checkEnhancement(): Promise<{
  connected: boolean;
  message: string;
  error_code?: string;
}> {
  const res = await fetch(`${BASE}/api/enhancement/check`, { method: "POST" });
  // 403 (consent_required) returns a structured body; surface it rather than throw.
  if (!res.ok && res.status !== 403) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}


/** Error thrown by previewEnhancement; carries the backend's structured
 *  error_code so the caller can distinguish "no model configured"
 *  (not_configured) from a generic provider failure. */
export interface PreviewError extends Error {
  errorCode?: string;
}


/** Run the configured provider on a sample. Editable-prompt models use the
 *  supplied prompt; fixed-prompt local models resolve the optional profile id.
 *  Returns the cleaned output text.
 *  Throws a PreviewError on any non-200; a 400 with error_code="not_configured"
 *  means no provider/key is set up yet. The sample text is never logged. */
export async function previewEnhancement(
  text: string,
  systemPrompt: string,
  profileId?: string,
): Promise<string> {
  const res = await fetch(`${BASE}/api/enhancement/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text,
      system_prompt: systemPrompt,
      ...(profileId ? { profile_id: profileId } : {}),
    }),
  });
  if (!res.ok) {
    let errorCode: string | undefined;
    try {
      const data = await res.json();
      errorCode = data?.error_code;
    } catch {
      /* non-JSON body: leave errorCode undefined */
    }
    const err: PreviewError = new Error(`${res.status}`);
    err.errorCode = errorCode;
    throw err;
  }
  const data = await res.json();
  return data.output as string;
}


/** Backend-resolved model behavior, independent of catalog IDs and edition membership. */
export interface EnhancementModelCapabilities {
  family: "customizable" | "trained-mode";
  editable_prompt: boolean;
  supported_modes: ("chat" | "code" | "email")[] | null;
}

export interface EnhancementModel {
  id: string;
  label: string;
  recommended?: boolean;
  /** Local GGUF catalog metadata (only present for provider="local"). */
  disk_mb?: number;
  ram_mb?: number;
  /** Backend-authoritative resident-process admission estimate. For
   * candidates this is derived from the private artifact size; no path leaves
   * the backend. */
  resident_ram_mb?: number;
  resident_ram_estimated?: boolean;
  /** Minimum total system RAM (MB) before the LowRamDialog stays silent.
   *  Decoupled from ``ram_mb`` (model runtime cost) so the dialog can fire
   *  on small machines even when the model itself fits comfortably. */
  min_system_ram_mb?: number;
  gguf_repo?: string;
  gguf_filename?: string;
  /** System-prompt family for local models: "gemma" gets the full editable
   *  cleanup prompt; the "stream" families are fixed training contracts.
   *  The profile editor uses this to lock the Cleanup prompt for fine-tunes. */
  prompt_key?: string;
  capabilities?: EnhancementModelCapabilities;
  /** Additive local-catalog metadata. Every field remains optional so rows
   *  from older backends retain the legacy production-only shape. */
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
}


export interface EnhancementModelsResponse {
  provider: string;
  bundled: EnhancementModel[];
  /** Local provider only: ids of GGUFs present on disk. */
  installed_ids?: string[];
  /** Local provider only: ids the picker may SURFACE right now — a model is
   *  available when it is installed or has a pinned Hugging Face source.
   *  Models without a download source are omitted so the picker hides them.
   *  Absent on older backends -> treat every bundled id as available. */
  available_ids?: string[];
  /** Local provider only: the currently configured local model id. */
  active_id?: string;
  /** Local provider only: whether llama-server is currently running. */
  running?: boolean;
}


export async function getEnhancementModels(
  provider: "gemini" | "openai" | "anthropic" | "local",
): Promise<EnhancementModelsResponse> {
  const params = new URLSearchParams({ provider });
  return fetchJSON(`${BASE}/api/enhancement/models?${params.toString()}`);
}


export interface LocalCandidateActivationResponse {
  status: "activated";
  model_id: string;
}


/** Activate an available development candidate without entering the managed
 *  production download or benchmark flow. */
export async function activateLocalCandidate(
  modelId: string,
): Promise<LocalCandidateActivationResponse> {
  return fetchJSON(`${BASE}/api/enhancement/local-activate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_id: modelId }),
  });
}


/** Kick off a local LLM GGUF download. Progress arrives via the WebSocket
 *  ``download.progress`` / ``download.complete`` / ``download.error`` /
 *  ``download.cancelled`` events tagged with ``kind: "local_llm"``. */
export async function startLocalLLMDownload(modelId: string, recovery = false): Promise<void> {
  await fetchJSON(`${BASE}/api/enhancement/local-download/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_id: modelId, ...(recovery ? { recovery: true } : {}) }),
  });
}


/** Cancel an in-progress local LLM GGUF download. */
export async function cancelLocalLLMDownload(): Promise<void> {
  await fetchJSON(`${BASE}/api/enhancement/local-download/cancel`, {
    method: "POST",
  });
}


/** Delete an installed local GGUF from disk; stops the llama-server
 *  supervisor first if the deleted id is the active model. */
export async function deleteLocalModel(modelId: string): Promise<void> {
  await fetchJSON(`${BASE}/api/enhancement/local-download/delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_id: modelId }),
  });
}


/** Kick off a GPU viability benchmark. Two modes:
 *  - `mode:"auto"` (the default, and the meaning of calling with no args) runs
 *    the fully-automatic check of Stream-1 Mini (Gemma excluded), zero decisions.
 *  - `mode:"manual"` benches EXACTLY `modelId` (Gemma included) and can return
 *    a `marginal` verdict.
 *  The backend runs a prefilter (device check), downloads the model(s) if
 *  needed, then runs the real decode test. Progress and result arrive via the
 *  WebSocket `benchmark.progress` / `benchmark.result` events. Returns 409 if a
 *  benchmark is already running. */
export async function startBenchmark(
  opts?: { mode?: "auto" | "manual"; modelId?: string },
): Promise<{ status: string }> {
  const body: Record<string, unknown> = {};
  if (opts?.mode) body.mode = opts.mode;
  if (opts?.modelId) body.model_id = opts.modelId;
  const init: RequestInit = { method: "POST" };
  if (Object.keys(body).length > 0) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  return fetchJSON(`${BASE}/api/enhancement/benchmark/start`, init);
}


/** Fetch the cached GPU benchmark verdict from config.
 *  Returns ``{verdict: "not_run", device: ""}`` before the benchmark runs. */
export async function getBenchmarkVerdict(): Promise<{
  verdict: string;
  device: string;
  /** True once after a launch-time GPU-set change invalidated the verdict
   *  (read-and-clear server-side); the page auto-runs the benchmark when set. */
  recheck_pending?: boolean;
}> {
  return fetchJSON(`${BASE}/api/enhancement/benchmark/verdict`);
}


/** Fetch the bundled CHAT, CODE, or EMAIL prompt body. Used by the Enhancement
 *  page to pre-seed the Custom-mode textarea with a working template. */
export async function getBuiltinPrompt(
  mode: "chat" | "code" | "email" = "chat",
): Promise<{ mode: string; prompt: string }> {
  return fetchJSON(`${BASE}/api/enhancement/builtin-prompt?mode=${mode}`);
}


/** Fetch the current production llama-server supervisor state.
 *  Used by Enhancement on mount (local provider) to seed the status badge
 *  without waiting for the next WS transition event.
 *  Returns ``{state: "absent"}`` when no supervisor exists (pre-download,
 *  unknown-model-id, missing-binary, or endpoint-override scenarios). */
export async function getLlamaStatus(): Promise<{
  state:
    | "absent"
    | "dormant"
    | "spawning"
    | "health_ok"
    | "warming"
    | "ready"
    | "error"
    | "stopping";
  model?: string;
  port?: number | null;
  message?: string;
}> {
  return fetchJSON(`${BASE}/api/llama/status`);
}


export interface MissingEnhancementModel {
  id: string;
  label: string;
  disk_mb: number;
}


export async function getLocalRecovery(): Promise<{
  model: MissingEnhancementModel | null;
  busy: boolean;
}> {
  return fetchJSON(`${BASE}/api/enhancement/local-recovery`);
}


/** Fetch free disk space at the local-LLM cache dir, optionally with the
 *  required-bytes-for-this-model precomputed. Used by Enhancement to
 *  decide whether to mount LowDiskSpaceDialog before opening the local
 *  download flow. ``free_mb`` may be -1 if the OS denied the query, in
 *  which case the frontend treats it as "unknown" and fails open. */
export async function getEnhancementDiskFree(
  model_id?: string,
): Promise<{
  free_mb: number;
  required_mb?: number;
  model_id?: string;
  sufficient?: boolean;
}> {
  const query = model_id ? `?model_id=${encodeURIComponent(model_id)}` : "";
  return fetchJSON(`${BASE}/api/enhancement/disk-free${query}`);
}
