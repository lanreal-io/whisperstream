


/** Content-free speech state. Routine response speech carries completion_id. */
export interface SpeechStateEvent {
  type: "speech.state";
  state: "speaking" | "idle";
  engine?: "kokoro" | "supertonic";
  completion_id?: string;
}


/** Content-free process health transition for one concrete speech engine. */
export interface SpeechEngineHealthEvent {
  type: "speech.engine_health";
  engine: "kokoro" | "supertonic";
  healthy: boolean;
  reason?: string;
}


/** Content-free authoritative lifecycle for one correlated dictation. */
export interface DictationStateEvent {
  type: "dictation.state";
  version: 1;
  recording_id: number;
  phase: "recording" | "processing" | "idle";
  origin: "push_to_talk" | "toggle" | "hud" | null;
  hud_enabled: boolean;
  preview_enabled: boolean;
  screen_reader_detected: boolean;
  /** Present when this recording is an agent reply (or question answer)
   *  capture: the compose lease generation that owns it. */
  compose_generation?: number;
}


/** Ephemeral raw recognition hypothesis. Never persisted or treated as final. */
export interface TranscriptionPreviewEvent {
  type: "transcription.preview";
  recording_id: number;
  revision: number;
  text: string;
  stable_char_count: number;
  presentation_version: 1;
  mutable_start_char_count: number;
  freeze: boolean;
}


export type SttFailureCode =
  | "worker_start_failed"
  | "worker_protocol_failed"
  | "worker_timeout"
  | "worker_python_exception"
  | "worker_native_exit";


export interface SttFailureEvent {
  type: "stt.failure";
  engine: "speech_to_text";
  backend: string;
  model_id: string;
  operation: string;
  code: SttFailureCode;
  recovery_attempted: boolean;
  suppressed: boolean;
}
