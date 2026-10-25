/**
 * Race-condition regression test for the legacy DownloadDialog flash bug.
 *
 * Bug: After fresh install, `model.missing` lands on the WebSocket ~1ms
 * before the initial `GET /api/config` returns. The original guard was
 * `configRef.current?.setup_completed !== false`, which evaluates to
 * `undefined !== false === true` while config is null, so it eagerly set
 * `downloadModel`. The secondary mount gate (`setup_completed !== false`)
 * suppressed the dialog during onboarding, but the moment the user clicked
 * Finish and `setup_completed` flipped to true, the stale `downloadModel`
 * caused `DownloadDialog` to mount and flash for ~2.3s.
 *
 * Fix: gate the FIRST setter with a strict positive match — a loaded config
 * (configRef.current !== null) AND setup_completed === true. Both null
 * (not-yet-loaded) and false (mid-onboarding) collapse to "do not show".
 *
 * This test mirrors the App.tsx state machine (configRef + downloadModel +
 * the same two gates) in a tiny harness so we can drive the exact event
 * ordering deterministically without mounting the full Tauri-coupled App.
 */
import { describe, it, expect } from "vitest";
import { useEffect, useRef, useState, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { AppConfig } from "./api/config";

// Minimal AppEvent subset we care about for this race.
type TestEvent = { type: "model.missing"; model_name: string };
type EventHandler = (event: TestEvent) => void;

// Tiny event-bus stand-in for `useAppSocket().subscribe`.
function makeBus() {
  const handlers = new Set<EventHandler>();
  return {
    subscribe(h: EventHandler) {
      handlers.add(h);
      return () => {
        handlers.delete(h);
      };
    },
    emit(event: TestEvent) {
      // Snapshot to be safe against handler-set mutation during dispatch.
      [...handlers].forEach((h) => h(event));
    },
  };
}
type Bus = ReturnType<typeof makeBus>;

interface HarnessProps {
  bus: Bus;
  config: AppConfig | null;
}

const DOWNLOAD_DIALOG_TESTID = "download-dialog";

/**
 * Mirrors the state-setting logic from App.tsx:
 * - configRef tracks the latest config inside the WS subscribe closure
 * - the model.missing handler uses the strict positive match to gate setDownloadModel
 * - the render gate also requires setup_completed !== false
 */
function Harness({ bus, config }: HarnessProps) {
  const [downloadModel, setDownloadModel] = useState<string | null>(null);
  const configRef = useRef<AppConfig | null>(config);

  useEffect(() => {
    configRef.current = config;
  }, [config]);

  useEffect(() => {
    return bus.subscribe((event) => {
      if (event.type === "model.missing") {
        if (
          configRef.current !== null &&
          configRef.current.setup_completed === true
        ) {
          setDownloadModel(event.model_name);
        }
      }
    });
  }, [bus]);

  // Mirror the secondary gate at App.tsx:932:
  //   downloadModel && (config.setup_completed as boolean) !== false
  const showDialog =
    downloadModel !== null && config?.setup_completed !== false;
  return showDialog ? <div data-testid={DOWNLOAD_DIALOG_TESTID} /> : null;
}

// Build a minimally-typed AppConfig with the given setup_completed.
function configWith(setup_completed: boolean): AppConfig {
  return {
    primary_shortcut: "right shift",
    push_to_talk_shortcut: "right shift",
    toggle_shortcut: "",
    paste_last_shortcut: "",
    paste_last_original_shortcut: "",
    stt_model_selection_mode: "automatic",
    stt_backend: "parakeet",
    parakeet_model: "nemo-parakeet-tdt-0.6b-v3",
    parakeet_quantization: null,
    qwen_force_language: false,
    model_storage_dir: "",
    onnx_providers: "cpu",
    threads: null,
    language: null,
    word_boosts: [],
    word_filters: [],
    word_replacements: {},
    post_processing: "",
    clipboard_behavior: true,
    clipboard_clear_delay: 0,
    input_style: "clipboard",
    input_style_user_set: false,
    keystroke_delay_ms: 0,
    model_timeout: 60,
    audio_feedback: true,
    audio_feedback_volume: 1,
    start_sound_path: null,
    stop_sound_path: null,
    error_sound_path: null,
    max_recording_duration: 0,
    system_volume_mode: "off",
    recording_mode: "push_to_talk",
    run_on_startup: false,
    audio_input_device: "",
    show_recording_indicator: true,
    show_recording_control: false,
    show_live_preview: false,
    setup_completed,
    enhancement_enabled: false,
    enhancement_provider: "gemini",
    enhancement_api_key: "",
    enhancement_gemini_api_key: "",
    enhancement_openai_api_key: "",
    enhancement_anthropic_api_key: "",
    enhancement_ollama_endpoint: "http://localhost:11434",
    enhancement_ollama_model: "llama3.2",
    enhancement_gemini_model: "gemini-2.5-flash-lite",
    enhancement_openai_model: "gpt-4o-mini",
    enhancement_anthropic_model: "claude-haiku-4-5-20251001",
    enhancement_mode: "default",
    enhancement_custom_prompt: "",
    enhancement_custom_base_url: "",
    enhancement_custom_model: "",
    enhancement_custom_api_key: "",
    profiles: [
      { id: "chat", name: "Chat", prompt: "Chat prompt" },
      { id: "code", name: "Code", prompt: "Code prompt" },
    ],
    default_profile_id: "chat",
    power_mode_map: {},
    site_map: {},
    profile_hotkeys: {},
    enhancement_local_model: "gemma4-e2b-instruct",
    enhancement_local_port: 0,
    enhancement_local_endpoint: "",
    enhancement_safeguards_enabled: true,
    enhancement_local_idle_timeout: 600,
    enhancement_local_gpu_verdicts: {},
    enhancement_local_gpu_device: "",
    enhancement_local_nudge_seen: false,
    history_enabled: true,
    save_audio: true,
    transcript_retention_days: 7,
    claude_hud_enabled: false,
    claude_hud_show_text: true,
    agent_speak_setup_completed: false,
    agent_speech_mode: "off",
    agent_speech_voice: 0,
    agent_speech_speed: 1.0,
    agent_speech_volume: 1.0,
    lockdown_mode: false,
    lockdown_idle_lock_seconds: 300,
    theme: "light",
    dismissed_offer_ids: [],
    shown_offer_ids: [],
  };
}

// Tiny render helper. Avoids @testing-library/react's findBy* timing
// surface; we drive every state change synchronously via act() so the
// assertions remain deterministic.
function mount(initial: HarnessProps): {
  rerender: (next: HarnessProps) => void;
  unmount: () => void;
  container: HTMLDivElement;
} {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root: Root;
  act(() => {
    root = createRoot(container);
    root.render(<Harness {...initial} />);
  });
  return {
    rerender(next) {
      act(() => {
        root.render(<Harness {...next} />);
      });
    },
    unmount() {
      act(() => {
        root.unmount();
      });
      container.remove();
    },
    container,
  };
}

describe("model.missing race during onboarding", () => {
  it("does NOT mount DownloadDialog when model.missing fires before config loads, then onboarding completes", () => {
    const bus = makeBus();

    // 1. Initial mount with config=null (config not yet loaded).
    const { rerender, unmount, container } = mount({ bus, config: null });

    // 2. WS delivers model.missing BEFORE GET /api/config resolves. With the
    //    bug, this would call setDownloadModel and stash the model name.
    act(() => {
      bus.emit({
        type: "model.missing",
        model_name: "nemo-parakeet-tdt-0.6b-v3",
      });
    });
    expect(
      container.querySelector(`[data-testid="${DOWNLOAD_DIALOG_TESTID}"]`),
    ).toBeNull();

    // 3. Config loads with setup_completed=false (user is mid-onboarding).
    rerender({ bus, config: configWith(false) });
    expect(
      container.querySelector(`[data-testid="${DOWNLOAD_DIALOG_TESTID}"]`),
    ).toBeNull();

    // 4. User clicks Finish on onboarding step 3; setup_completed flips true.
    //    Pre-fix, the stale downloadModel from step 2 would now satisfy the
    //    secondary gate and DownloadDialog would mount and flash.
    rerender({ bus, config: configWith(true) });
    expect(
      container.querySelector(`[data-testid="${DOWNLOAD_DIALOG_TESTID}"]`),
    ).toBeNull();

    unmount();
  });

  it("DOES mount DownloadDialog when model.missing fires after onboarding (the legitimate post-onboarding case)", () => {
    const bus = makeBus();

    // App is in steady-state: config loaded, setup completed.
    const { rerender, unmount, container } = mount({
      bus,
      config: configWith(true),
    });

    // Model file disappears (deleted, AV quarantined, etc) -> backend
    // emits model.missing. The legacy DownloadDialog SHOULD pop here.
    act(() => {
      bus.emit({
        type: "model.missing",
        model_name: "nemo-parakeet-tdt-0.6b-v3",
      });
    });
    expect(
      container.querySelector(`[data-testid="${DOWNLOAD_DIALOG_TESTID}"]`),
    ).not.toBeNull();

    rerender({ bus, config: configWith(true) });
    unmount();
  });
});
