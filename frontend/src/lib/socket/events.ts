import { type AgentConversationSnapshot, type AgentCompletionUpsertEvent, type AgentCompletionsSnapshotEvent, type AgentCompletionAcknowledgedEvent, type AgentResponseQueueSnapshotEvent, type AgentInteractionStateEvent, type AgentComposeEvent, type AgentQuestionSnapshotEvent, type AgentSessionsSnapshotEvent, type AgentReplySnapshotEvent, type AgentReplyResultEvent, type AgentReplyCloseEvent, parseAgentReplyEvent, parseAgentResponseQueueEvent, parseAgentInteractionEvent, parseAgentInteractionsSnapshot, parseAgentComposeEvent, parseAgentQuestionEvent, parseAgentConversationEvent, parseAgentSessionsEvent } from "./agentEvents";
import { type DictationStateEvent, type TranscriptionPreviewEvent, type SttFailureEvent, type SpeechStateEvent, type SpeechEngineHealthEvent } from "./dictationEvents";
import { type TranscriptBakeoffEvent, parseTranscriptBakeoffEvent } from "./bakeoffEvents";
import { isPlainRecord } from "./validation";


/** The closed provider identity the backend's timeout observer publishes.
 *  Mirrors `EnhancementProvider` in `src/whisperstream/llm_enhancer.py`; both
 *  sides are bounded so a user-entered custom endpoint can never reach the HUD.
 */
export type EnhancementTimeoutProvider =
  | "gemini"
  | "openai"
  | "anthropic"
  | "ollama"
  | "local"
  | "custom";


export type AppEvent =
  | AgentConversationSnapshot
  | { type: "state.recording"; recording: boolean; screen_reader_detected?: boolean }
  | {
      type: "state.arming";
      active: boolean;
      hud_enabled?: boolean;
      screen_reader_detected?: boolean;
      /** A reply capture into the agent surface: no generic arming pill. */
      agent_compose?: boolean;
    }
  | { type: "agent-hud-view" }
  | { type: "agent.hud.activity"; version: 1; active: boolean }
  | { type: "state.transcribing"; active: boolean }
  // Backend-owned screen-reader detection, pushed on change. Windows fetch
  // the cached value once and after reconnect instead of polling.
  | { type: "accessibility.screen_reader"; active: boolean }
  | DictationStateEvent
  | { type: "audio.levels"; rms: number; peak: number }
  | TranscriptionPreviewEvent
  // raw_text/enhancement are present only while onboarding suppresses
  // injection (its Enhance step compares what was said with the result).
  // Label by `enhancement`, never by diffing the texts. Never log either.
  | {
      type: "transcription.result";
      text: string;
      duration_ms: number;
      enhance_ms?: number;
      raw_text?: string;
      enhancement?: "applied" | "skipped" | "failed" | "off";
    }
  | { type: "shortcut.capturing"; keys: string; target?: string }
  | { type: "shortcut.captured"; keys: string; target?: string }
  | { type: "shortcut.capture.failed"; target?: string }
  // Transient manual-override signal (issue 14). profile_id null = cleared
  // (auto-routing resumed); non-null = a hotkey override is active. NOT persisted.
  | {
      type: "active_profile_override";
      profile_id: string | null;
      name?: string;
      source?: string;
    }
  // Recording-correlated effective enhancement profile for ONE dictation
  // (transparent-indicators Task 1.2). Published once per dictation for manual,
  // site, app, and default routing alike, and only when enhancement will
  // actually run - the intentional raw/no-default path emits nothing. `icon` is
  // a bounded curated key and appears only for custom profiles; built-in glyphs
  // derive from the immutable id. `recording_id` is load-bearing: a payload that
  // outlives its dictation must never stamp the next one.
  | {
      type: "enhancement.profile";
      recording_id: number;
      profile_id: string;
      name: string;
      icon?: string;
    }
  // Provider fallback outcomes for ONE dictation. Timeout and generic error
  // events share a recording-level dedupe budget and carry only this bounded
  // provider enum, never a model id, endpoint host, exception, or transcript.
  | {
      type: "enhancement.timeout";
      recording_id: number;
      provider: EnhancementTimeoutProvider;
    }
  | {
      type: "enhancement.error";
      recording_id: number;
      provider: EnhancementTimeoutProvider;
    }
  | { type: "download.progress"; downloaded: number; total: number; speed_bps: number; kind?: string; model_id?: string; engine?: "kokoro" | "supertonic" }
  | { type: "download.complete"; kind?: string; model_id?: string; engine?: "kokoro" | "supertonic" }
  | { type: "download.error"; message: string; reason?: string; restored?: boolean; kind?: string; model_id?: string; engine?: "kokoro" | "supertonic" }
  | { type: "download.cancelled"; kind?: string; model_id?: string; engine?: "kokoro" | "supertonic" }
  | { type: "model.missing"; model_name: string }
  | { type: "model.loading" }
  | { type: "model.loaded" }
  | { type: "model.load_failed"; reason: string }
  | SttFailureEvent
  | { type: "error"; message: string }
  | { type: "error.dialog"; code: string; title: string; message: string; retry_action?: string }
  | { type: "recording.blocked"; reason: "eula_acceptance_required" }
  | { type: "review.prompt" }
  | { type: "transcripts.changed" }
  /** Emitted by the backend when the transcript surface locks (idle timeout or
   *  explicit lock). The frontend `useLockdown` hook listens for this to
   *  update the lock state without polling. */
  | { type: "transcripts.locked"; reason?: string }
  | { type: "lockdown.changed"; enabled: boolean }
  | { type: "import.progress"; file: string; status: "transcribing" | "done" | "failed"; reason?: string }
  | {
      type: "dev.llama.status";
      kind: "stopped" | "loading" | "running" | "error";
      port?: number;
      model?: string;
      message?: string;
    }
  | {
      type: "llama.status";
      state:
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
    }
  // Speed-check progress. `model_id` names the model being measured during
  // the "running" phase so the dialog's chip names it. `wps` is the live rate measured directly
  // from generated content. `tps` remains optional for compatibility with
  // older backends. `device_complete` carries the authoritative terminal
  // llama.cpp timing/verdict for one serial device, allowing the UI to hold the
  // completed reading without delaying backend computation. Progress fields
  // remain optional so "prefilter" still matches.
  | {
      type: "benchmark.progress";
      phase: "prefilter" | "running" | "device_complete";
      model_id?: string;
      /** Identity of the Vulkan device currently being measured. Present on
       *  device-start and live-rate events from current backends. */
      device_name?: string;
      device_index?: number;
      wps?: number;
      tps?: number;
      /** Authoritative terminal decode speed for `device_complete`. `null`
       *  means the device attempt failed before llama.cpp returned timings. */
      measured_tok_per_s?: number | null;
      predicted_n?: number | null;
      verdict?: "viable" | "marginal" | "unviable";
    }
  | {
      type: "benchmark.result";
      // Tri-state (Task 1.5): "marginal" sits between viable and unviable —
      // the measured model runs, just slowly. Both the automatic first-setup
      // check (Stream-1 Mini only) and a manual single-model check may emit it.
      verdict: "viable" | "marginal" | "unviable";
      /** Present when no benchmark measurement was produced. The verdict is
       *  retained for wire compatibility but must not be cached as hardware
       *  evidence. */
      outcome?: "infrastructure_error" | "measured";
      model_downloaded: boolean;
      // Which benchmark mode produced this result. "auto" = the first-setup
      // check of Stream-1 Mini; "manual" = an explicit single-model bench.
      // Optional so the type still matches older backends that omit it.
      mode?: "auto" | "manual";
      // REAL verdict keyed by the measured model id ({<model_id>: <verdict>}).
      // Optional (older backends omit it).
      verdicts?: Record<string, "viable" | "marginal" | "unviable">;
      // True only when THIS bench actually downloaded the GGUF (a
      // pre-existing model reports false). Drives auto-delete-on-give-up so a
      // model the user already had is never removed.
      freshly_downloaded?: boolean;
      device?: string;
      // The measured model id + its decode tok/s — the deciding number on the
      // receipt bar.
      measured_model_id?: string;
      measured_tok_per_s?: number;
      // Tiered-recommender fields retained for back-compat (retired in round 01;
      // the auto path measures rungs directly instead of estimating). Optional
      // so the type still matches older backends that emit them.
      recommended_model_id?: string | null;
      // The measured rung's tri-state verdict, duplicated from the
      // top-level `verdict` field for callers that read the tier fields as
      // a group. Optional so the type still matches older backends.
      measured_verdict?: "viable" | "marginal" | "unviable";
      tiers?: Array<{
        id: string;
        label: string;
        badge?: { label: string; tone?: string };
        tok_per_s: number;
        measured: boolean;
        clears: boolean;
        reason?: string;
        // Per-rung tri-state verdict (Task 1.2). Optional so the type still
        // matches older backends that omit it.
        verdict?: "viable" | "marginal" | "unviable";
      }>;
    }
  // The v1 completion stream is authoritative for queue-aware HUD clients.
  | AgentCompletionUpsertEvent
  | AgentCompletionsSnapshotEvent
  | AgentCompletionAcknowledgedEvent
  | AgentResponseQueueSnapshotEvent
  | AgentInteractionStateEvent
  | import("./agentEvents").AgentInteractionsSnapshotEvent
  | AgentComposeEvent
  | AgentQuestionSnapshotEvent
  | AgentSessionsSnapshotEvent
  | AgentReplySnapshotEvent
  | AgentReplyResultEvent
  | AgentReplyCloseEvent
  // Agent-speech playback state (Phase 1 Task 3.1). Content-free contract:
  // state is an enum only, NEVER response text. Treat any value other than
  // "speaking" as idle so the backend can add states later without breaking
  // older frontends.
  | SpeechStateEvent
  | SpeechEngineHealthEvent
  | TranscriptBakeoffEvent;


export type EventHandler = (event: AppEvent) => void;


export function parseAppSocketEvent(value: unknown): AppEvent | null {
  if (!isPlainRecord(value) || typeof value.type !== "string") return null;
  if (value.type.startsWith("enhancement.bakeoff.")) {
    return parseTranscriptBakeoffEvent(value);
  }
  if (value.type === "agent.response_queue.snapshot") {
    return parseAgentResponseQueueEvent(value);
  }
  if (value.type === "agent.interaction.state") {
    return parseAgentInteractionEvent(value);
  }
  if (value.type === "agent.interactions.snapshot") {
    return parseAgentInteractionsSnapshot(value);
  }
  if (value.type.startsWith("agent.compose.")) {
    return parseAgentComposeEvent(value);
  }
  if (value.type === "agent.question.snapshot") {
    return parseAgentQuestionEvent(value);
  }
  if (value.type === "agent.conversation.snapshot") return parseAgentConversationEvent(value);
  if (value.type === "agent.sessions") {
    return parseAgentSessionsEvent(value);
  }
  if (value.type.startsWith("agent.reply.")) {
    return parseAgentReplyEvent(value);
  }
  if (value.type === "accessibility.screen_reader") {
    return typeof value.active === "boolean"
      ? { type: "accessibility.screen_reader", active: value.active }
      : null;
  }
  return value as AppEvent;
}


/** Log exact content-free benchmark payloads for exported frontend diagnostics. */
export function logBenchmarkEvent(event: AppEvent): void {
  if (!event.type.startsWith("benchmark.")) return;
  console.log(
    `[Benchmark] event receive payload=${JSON.stringify(event)}`,
  );
}
