import { type AgentSurfaceSelection } from "./agents";
import { type SpeechSelection } from "./speech";
import { fetchJSON, BASE } from "./client";


export interface AppConfig {
  primary_shortcut: string;
  push_to_talk_shortcut: string;
  toggle_shortcut: string;
  paste_last_shortcut: string;
  paste_last_original_shortcut: string;
  claude_view_shortcut?: string;
  /** `"automatic"` follows Whisperstream's recommended speech model for
   *  `language`; `"manual"` pins the exact artifact encoded by `stt_backend` +
   *  `parakeet_model` for as long as it supports the selected language. The
   *  backend normalizes anything else to `"automatic"`. */
  stt_model_selection_mode: string;
  stt_backend: string;
  parakeet_model: string;
  parakeet_quantization: string | null;
  // Qwen-only advanced toggle: force the selected language (English-name hint)
  // instead of auto-detecting. Effective ONLY in manual mode with Qwen
  // selected; every other combination clears it. Default false.
  qwen_force_language: boolean;
  /** Absolute path to the folder that contains the `models/` and `local-llm/`
   *  subdirectories. Empty string = default data dir. */
  model_storage_dir: string;
  onnx_providers: string;
  threads: number | null;
  language: string | null;
  word_boosts: string[];
  /** Response-only prompt-contract capability; does not imply enabled or consented. */
  enhancement_vocabulary_supported?: boolean;
  word_filters: string[];
  word_replacements: Record<string, string>;
  post_processing: string;
  clipboard_behavior: boolean;
  clipboard_clear_delay: number;
  /** How transcribed text is delivered to the focused window: `"clipboard"` (default) or `"keystroke"` (typed char-by-char via pynput). */
  input_style: string;
  /** Dirty flag: true once the user has explicitly picked an input style. While
   *  false, the backend may auto-resolve the global to `"keystroke"` when a
   *  screen reader is detected (`screen_reader_detected`). Set to true on any
   *  explicit pick so the user's choice is honored from then on. */
  input_style_user_set: boolean;
  /** Read-only, server-computed: whether a screen reader is active right now
   *  (Windows `SPI_GETSCREENREADER`). Not persisted; present on `GET /api/config`
   *  only. Drives the resolved input-style display + the auto-default hint. */
  screen_reader_detected?: boolean;
  /** Read-only, server-computed: resolved absolute path of the default app data
   *  directory (%LOCALAPPDATA%\Whisperstream). Not persisted; present on
   *  `GET /api/config` only. Used by the model-storage row to display the
   *  default path and as the destination for "Reset to default". */
  data_dir?: string;
  /** Inter-keystroke delay in milliseconds for keystroke mode; 0 = no delay (fast path). */
  keystroke_delay_ms: number;
  model_timeout: number;
  audio_feedback: boolean;
  audio_feedback_volume: number;
  start_sound_path: string | null;
  stop_sound_path: string | null;
  error_sound_path: string | null;
  max_recording_duration: number;
  system_volume_mode: string;
  recording_mode: string;
  run_on_startup: boolean;
  audio_input_device: string;
  /** Presentation-only label for the opaque saved Mac input identity. */
  audio_input_device_label?: string;
  /** Darwin-only authoritative persistence mode from the config API. */
  audio_input_device_selection_mode?: "opaque-id";
  /** Read-only Mac inventory result from GET /api/config. */
  audio_input_device_available?: boolean;
  show_recording_indicator: boolean;
  /** Keep the compact recording control available between recordings.
   *  Recording Indicator remains the runtime master gate. Defaults to false. */
  show_recording_control: boolean;
  /** Show raw, revisable dictation text in the recording HUD. The recording
   *  indicator remains the runtime master gate. Defaults to false. */
  show_live_preview: boolean;
  /** When true, the Claude HUD feature is active: the HUD expands to show Claude
   *  Code responses and lets the user dictate replies hands-free. */
  claude_hud_enabled: boolean;
  /** When true, the owned Codex plugin may deliver Agent Speak events and
   *  continue a parked Stop with a dictated reply. Independent from Claude. */
  codex_hud_enabled?: boolean;
  /** Compatibility only: no longer read or sent by the UI. */
  claude_agent_surface?: AgentSurfaceSelection;
  codex_agent_surface?: AgentSurfaceSelection;
  /** Whether the HUD overlay renders reply text. Seed-only default. */
  claude_hud_show_text: boolean;
  /** Whether the Agent Speak four-step setup flow has been completed. Gates the
   *  Experimental row between the toggle-first entry point and Configure.
   *  Seed-only default; no migration flips it for existing installs. */
  agent_speak_setup_completed: boolean;
  /** Per-agent "this agent has really produced a message" signal. Flipped true
   *  the first time a real completion arrives from that agent, or when its
   *  onboarding exercise completes. Once true, its stale setup hint
   *  (run /hooks / run /reload-plugins) retires and the row reads Connected.
   *  Optional for legacy snapshots. */
  codex_verified?: boolean;
  claude_verified?: boolean;
  /** Master Agents switch; false pauses every remembered provider. */
  agents_enabled?: boolean;
  /** Master toggle for marked agent summaries. Optional for legacy snapshots. */
  agent_speech_enabled?: boolean;
  /** Whether to show the short written summary in the HUD when an agent replies. */
  agent_summary_text_enabled?: boolean;
  /** Sessions asked to emit a marked spoken summary. Optional for legacy snapshots. */
  agent_speech_content?: "summary" | "short" | "full";
  agent_speech_short_seconds?: number;
  agent_speech_activation?: "per_session" | "always_on";
  /** Summary author. Whisperstream is a disabled, deferred source. */
  agent_speech_summary_source?: "agent" | "whisperstream";
  /** @deprecated Compatibility value retained for older config files. */
  agent_speech_mode: "off" | "verbatim" | "summary" | "conversational";
  /** Saved voice-engine preference. Missing legacy values resolve to Automatic. */
  agent_speech_engine?: SpeechSelection;
  /** Supertonic preset speaker id, 0..9. Default 1. */
  agent_speech_voice: number;
  /** Kokoro voice id. Kept separately when the engine preference changes. */
  agent_speech_kokoro_voice?: string;
  /** Playback speed, 0.5..2.0. Default 1.0. */
  agent_speech_speed: number;
  /** Playback volume, 0..1. Default 1.0. */
  agent_speech_volume: number;
  /** Seconds of speech inactivity before unloading voice synthesis. 0 = Never. Default 1800. */
  agent_speech_idle_timeout_seconds?: number;
  /** Whole-second passive Stop reply window. 0 disables reply routing. */
  claude_reply_grace_seconds?: number;
  /** Whole-second visually silent interval before the next queued reply. */
  claude_reply_queue_delay_seconds?: number;
  /** Send an authoritative routed final immediately instead of reviewing it. */
  claude_reply_auto_submit?: boolean;
  /** Permit explicit allow-once and deny decisions in the expanded HUD. */
  claude_permission_decisions_enabled?: boolean;
  setup_completed: boolean;
  /** Feature-spotlight / upgrade-offer overlay: ids of offers the user has
   *  dismissed or been shown, so the launch overlay never re-nags. */
  dismissed_offer_ids: string[];
  shown_offer_ids: string[];
  enhancement_enabled: boolean;
  enhancement_provider: string;
  /** Legacy active-provider mirror. Backend keeps it in sync with the
   *  per-provider slot for the active provider. New code should write
   *  the per-provider slot below directly. */
  enhancement_api_key: string;
  /** Stored API key for Gemini. Preserved across provider switches. */
  enhancement_gemini_api_key: string;
  /** Stored API key for OpenAI. Preserved across provider switches. */
  enhancement_openai_api_key: string;
  /** Stored API key for Anthropic. Preserved across provider switches. */
  enhancement_anthropic_api_key: string;
  enhancement_ollama_endpoint: string;
  enhancement_ollama_model: string;
  enhancement_gemini_model: string;
  enhancement_openai_model: string;
  enhancement_anthropic_model: string;
  /** Backend-keyed thinking preferences for exact provider/endpoint/model identities. */
  enhancement_thinking_levels?: Record<string, string>;
  /** Optional routing for direct Anthropic. Replacing its key clears this value. */
  enhancement_anthropic_workspace_id?: string;
  enhancement_mode: string;
  enhancement_custom_prompt: string;
  /** Base URL of the custom OpenAI-compatible endpoint (e.g. `https://openrouter.ai/api/v1`). */
  enhancement_custom_base_url: string;
  /** Free-text model name for the custom provider (e.g. `openai/gpt-4o-mini`). */
  enhancement_custom_model: string;
  enhancement_saved_models?: Record<string, string[]>;
  /** API key sent as `Authorization: Bearer <key>` to the custom endpoint. */
  enhancement_custom_api_key: string;
  /** Response-only masked fingerprints (first 4 + bullets + last 4) of the stored
   *  keys, computed server-side from the decrypted key. The full plaintext never
   *  reaches the client; these let the UI show which key is set. Present on GET/PUT
   *  config responses, dropped by the backend on the client's echo PUT. */
  enhancement_gemini_api_key_fingerprint?: string;
  enhancement_openai_api_key_fingerprint?: string;
  enhancement_anthropic_api_key_fingerprint?: string;
  enhancement_custom_api_key_fingerprint?: string;
  /** Enhancement profiles: each owns a cleanup prompt. `builtin` marks the
   *  seeded "chat"/"code" rows (non-deletable, editable in place).
   *  `input_style` is an optional per-profile override; absent = inherit the global `input_style`.
   *  `signature` is an optional per-profile default email sign-off.
   *  `icon` is an optional curated glyph for custom profiles. */
  profiles: { id: string; name: string; prompt: string; builtin?: boolean; icon?: string; language?: string; input_style?: string; signature?: string }[];
  /** Id of the profile that runs by default (the global default pointer). */
  default_profile_id: string;
  /** Per-app profile routing: process name (lowercased) -> profile id. */
  power_mode_map: Record<string, string>;
  /** Recorded exe path per power_mode_map entry: process name (lowercased) ->
   *  full exe path, captured when a rule is added. Lets install detection check
   *  path existence for apps no registry source lists. */
  power_mode_paths?: Record<string, string>;
  /** Per-site profile routing: registrable domain (lowercased) -> profile id. */
  site_map: Record<string, string>;
  /** Per-profile manual-switch hotkey: profile id -> shortcut string (e.g.
   *  "ctrl+alt+1"). Pressing it sticky-switches the active profile until focus
   *  moves to another app (issue 14). Empty by default; never seeded. */
  profile_hotkeys: Record<string, string>;
  /** Local LLM provider: catalog id of the active GGUF model. */
  enhancement_local_model: string;
  /** Local LLM provider: last-used llama-server port (diagnostic only; 0 = auto). */
  enhancement_local_port: number;
  /** Local LLM provider: optional external llama-server base URL override. Empty = use bundled. */
  enhancement_local_endpoint: string;
  /** Whether to apply output safeguards for every provider, including local
   * digit repair and integrity checks. Defaults to true. */
  enhancement_safeguards_enabled: boolean;
  /** Seconds to wait for each enhancement provider request before preserving the raw text. */
  enhancement_timeout_seconds?: number;
  /** Backend-only rollout mode for discretionary local output-guard checks.
   * Only applies while enhancement_safeguards_enabled is true.
   * Optional for compatibility with older backends and partial config fixtures. */
  enhancement_output_guard_mode?: "enforce" | "log" | "off";
  /** Seconds of inactivity before the local LLM subprocess is auto-stopped (frees ~1.6 GiB).
   *  0 = keep LLM in memory forever. Defaults to 600 (10 min). Mirrors model_timeout for Parakeet
   *  but defaults to ON-by-default (offload) since the LLM is larger and invoked less. */
  enhancement_local_idle_timeout: number;
  /** Per-model GPU benchmark verdict cache. Maps a local bundled model id ->
   *  tri-state verdict ("not_run" | "viable" | "marginal" | "unviable"). A
   *  model id absent from the map has never been benchmarked (reads "not_run").
   *  See getLocalGpuVerdict() in lib/localModels.ts (the canonical read site). */
  enhancement_local_gpu_verdicts: Record<string, string>;
  /** Device identity string from the benchmark run (e.g. "NVIDIA GeForce RTX 4070 Ti SUPER").
   *  Empty string when verdict is "not_run". Used for hardware-change re-check. */
  enhancement_local_gpu_device: string;
  /** Whether the one-time first-visit "try on-device cleanup" nudge has been
   *  shown/dismissed. Gates the Feature-#2 modal for pre-existing cloud users. */
  enhancement_local_nudge_seen: boolean;
  /** When true (default), long transcripts are cleaned in chunks so text
   *  appears progressively instead of after one blocking call. Advanced toggle. */
  enhancement_chunked_cleanup?: boolean;
  /** Word target and chunking activation threshold. Defaults to 120. */
  enhancement_words_per_chunk?: number;
  /** When true, the first cleanup chunk targets at most 60 words. Defaults to true. */
  enhancement_limit_first_chunk?: boolean;
  /** Master save switch for the Transcripts history. When false, completed
   *  dictations and imports are not persisted. Defaults to false on Mac, true on Windows. */
  history_enabled: boolean;
  /** Explicit Mac history consent, set only by a successful opt-in endpoint. */
  mac_history_opt_in?: boolean;
  /** When true (default), the recording WAV is stored alongside the transcript
   *  text. When false, history is text-only (no audio written). Only applies
   *  when history_enabled is true. */
  save_audio: boolean;
  /** How long transcripts are kept before the retention sweep purges them.
   *  0 = Forever (never purge); allowed UI values are 0 / 1 / 7 / 30. Defaults to 7. */
  transcript_retention_days: number;
  /** Lockdown Mode master switch. When true, the transcript history is gated
   *  behind a password (KDF-wrapped data key), cloud AI is force-disabled
   *  (effective provider is "local"), and diagnostic logging is hardened. The
   *  toggle is NEVER written directly from the UI: turning it on runs the setup
   *  modal (POST /api/lockdown/enable), turning it off runs the disable dialog
   *  (POST /api/lockdown/disable). Defaults to false. */
  lockdown_mode: boolean;
  /** Idle timeout (seconds) before the transcript surface auto-locks under
   *  Lockdown Mode. 0 = Never. Defaults to 300 (5 min). Only meaningful when
   *  lockdown_mode is true. */
  lockdown_idle_lock_seconds: number;
  /** Active UI theme. Valid values: "light" | "dark". Defaults to "light". */
  theme: string;
}


export async function getConfig(): Promise<AppConfig> {
  return fetchJSON(`${BASE}/api/config`);
}


export async function getDailyUseCapabilities(): Promise<import("../dailyUseCapabilities").DailyUseCapabilityReport> {
  return fetchJSON(`${BASE}/api/capabilities/daily-use`);
}


export async function getAdvancedCapabilities(): Promise<import("../advancedCapabilities").AdvancedCapabilityReport> {
  return fetchJSON(`${BASE}/api/capabilities/advanced`);
}


export async function saveConfig(config: Partial<AppConfig>): Promise<AppConfig> {
  return fetchJSON(`${BASE}/api/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}
