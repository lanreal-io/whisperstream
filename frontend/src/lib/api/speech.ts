import { fetchJSON, BASE } from "./client";


// ---------------------------------------------------------------------------
// Agent-speech endpoints (Phase 1 Task 3.1 frontend surface; backend in
// Phase 2 Task 2.2). Every route is experimental-gated and 404s off-gate.
// speak-sample/stop are fire-and-forget and content-free.
// ---------------------------------------------------------------------------

export type SpeechEngine = "kokoro" | "supertonic";

export type SpeechSelection = "auto" | SpeechEngine;

export type SpeechStatusReason =
  | "kokoro"
  | "supertonic"
  | "low_memory"
  | "unknown_memory"
  | "missing_kokoro"
  | "missing_supertonic"
  | "unhealthy_kokoro"
  | "slow_kokoro"
  | "runtime_unavailable";


export interface SpeechEngineStatus {
  key: SpeechEngine;
  label: string;
  installed: boolean;
  supported?: boolean;
  available?: boolean;
  unavailable_reason?: string | null;
  downloading: boolean;
  download_bytes: number;
  installed_bytes: number;
}


export interface SpeechStatus {
  plugin_variant?: "prod" | "dev";
  requested: SpeechSelection;
  preferred_engine: SpeechEngine;
  resolved_engine: SpeechEngine | null;
  active_engine: SpeechEngine | null;
  reason: SpeechStatusReason;
  memory: {
    total_mb: number | null;
    available_mb: number | null;
    eligible: boolean | null;
  };
  kokoro_memory_tier:
    | "recommended"
    | "caution"
    | "blocked"
    | "unknown";
  engines: Record<SpeechEngine, SpeechEngineStatus>;
  current_download: SpeechEngine | null;
  recommended_downloads: SpeechEngine[];
  /** Compatibility aliases retained while older frontends remain supported. */
  installed?: boolean;
  downloading?: boolean;
  engine?: SpeechEngine;
}


/** Backend-authoritative engine selection, health, and model-delivery state. */
export async function getSpeechStatus(signal?: AbortSignal): Promise<SpeechStatus> {
  return fetchJSON(`${BASE}/api/speech/status`, signal ? { signal } : undefined);
}


export interface SpeechVoice {
  id: string | number;
  sid: string | number;
  label: string;
  group: "female" | "male";
  reference_label?: string;
}


export interface SpeechVoices {
  engine: SpeechEngine;
  engine_label: string;
  voices: SpeechVoice[];
}


/** Static voice metadata for one concrete engine. Safe before model download. */
export async function getSpeechVoices(
  engine: SpeechEngine = "supertonic",
): Promise<SpeechVoices> {
  return fetchJSON(
    `${BASE}/api/speech/voices?engine=${encodeURIComponent(engine)}`,
  );
}


/** Kick off one concrete voice-model archive download. Progress arrives via the
 *  WebSocket `download.progress` / `download.complete` / `download.error` /
 *  `download.cancelled` events tagged `kind: "tts"` and `engine`. Throws on non-200
 *  (e.g. 409 when another download is already in flight). */
export async function startSpeechModelDownload(
  engine: SpeechEngine,
): Promise<void> {
  await fetchJSON(`${BASE}/api/speech/download/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ engine }),
  });
}


/** Speak the fixed sample line with the visible concrete engine and voice. */
export async function speakSample(
  engine?: SpeechEngine,
  voice?: string | number,
): Promise<{ ok: boolean }> {
  const explicit = engine !== undefined;
  return fetchJSON(`${BASE}/api/speech/speak-sample`, {
    method: "POST",
    ...(explicit
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ engine, voice }),
        }
      : {}),
  });
}


/** Speak arbitrary text (e.g. the onboarding voice-speed test box) with the
 *  visible concrete engine and voice. `text` is transcript-class user
 *  content: it is never persisted or logged, on this client or the backend. */
export async function speakText(
  text: string,
  engine?: SpeechEngine,
  voice?: string | number,
): Promise<{ ok: boolean }> {
  const explicit = engine !== undefined;
  return fetchJSON(`${BASE}/api/speech/speak-text`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(explicit ? { text, engine, voice } : { text }),
  });
}


/** Stop any in-flight agent speech immediately. The shared stop chokepoint
 *  used by the HUD stop button and the Controls sample button. */
export async function stopSpeech(): Promise<{ ok: boolean }> {
  return fetchJSON(`${BASE}/api/speech/stop`, { method: "POST" });
}
