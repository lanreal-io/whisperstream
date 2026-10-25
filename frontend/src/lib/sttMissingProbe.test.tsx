/**
 * Regression test for the "STT model vanished after setup" recovery probe.
 *
 * Bug: the backend's `model.missing` WS event is sent once on connect and races
 * the initial `GET /api/config`. On a cold start it lands ~1ms BEFORE config
 * resolves and is dropped while config is null (see missingModelRace). StrictMode
 * masks this in dev (the WS reconnect re-fires the event after config loads) but
 * a production build has a single connect, so a data dir whose model is gone
 * (deleted, moved, stale symlink) lands the user on the settings UI with no model
 * and no download flow.
 *
 * Fix: after config loads with `setup_completed === true`, deterministically probe
 * `getSttStatus()` (the same disk-presence source of truth the backend uses) and,
 * if the CONFIGURED exact artifact is not `downloaded`, surface the DownloadDialog
 * (which auto-starts the download). This is NOT racy and does not depend on the
 * one-shot WS event.
 *
 * The probe reads the exact-artifact inventory rather than the collapsed
 * per-backend flags: downloads are retained now, so "Parakeet is on disk" no
 * longer answers "is the artifact this config will load on disk".
 *
 * This mirrors the App.tsx probe effect + the DownloadDialog render gate in a tiny
 * harness (same approach as missingModelRace.test.tsx) so the exact decision logic
 * is driven deterministically without mounting the Tauri-coupled App.
 */
import { describe, it, expect, vi } from "vitest";
import { StrictMode, useEffect, useRef, useState, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { AppConfig } from "./api/config";
import type { SttArtifactId, SttStatusResponse } from "./api/models";

const DOWNLOAD_DIALOG_TESTID = "download-dialog";

interface HarnessProps {
  config: AppConfig | null;
  getSttStatus: () => Promise<SttStatusResponse>;
}

/**
 * Mirrors the recovery-probe effect + the DownloadDialog render gate from App.tsx.
 * Key design points this pins:
 * - the effect depends on the derived `setupComplete` boolean, NOT on `config`,
 *   so it fires once on the false->true transition and does not re-run (and thus
 *   cannot cancel its own in-flight getSttStatus) when config is later refetched;
 * - the configured exact artifact decides, so config is not a dependency;
 * - no once-only ref guard, so StrictMode's second pass still completes the probe.
 */
function Harness({ config, getSttStatus }: HarnessProps) {
  const [downloadModel, setDownloadModel] = useState<string | null>(null);
  const configRef = useRef<AppConfig | null>(config);
  useEffect(() => {
    configRef.current = config;
  }, [config]);

  const setupComplete = config?.setup_completed === true;
  useEffect(() => {
    if (!setupComplete) return;
    let cancelled = false;
    getSttStatus()
      .then((status) => {
        if (cancelled) return;
        const configured = status.artifacts.find(
          (artifact) => artifact.id === status.configured_artifact_id,
        );
        if (configured !== undefined && !configured.downloaded) {
          setDownloadModel((prev) => prev ?? configured.id);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional: fire once per setup-complete transition
  }, [setupComplete]);

  // Mirror the secondary render gate at App.tsx (downloadModel && setup_completed !== false).
  const showDialog = downloadModel !== null && config?.setup_completed !== false;
  return showDialog ? <div data-testid={DOWNLOAD_DIALOG_TESTID} /> : null;
}

const V3: SttArtifactId = "nemo-parakeet-tdt-0.6b-v3";
const QWEN: SttArtifactId = "qwen3-asr-0.6b-int8";

/** Exact inventory with one configured artifact and one retained sibling, so
 *  the probe can only pass by reading `configured_artifact_id`. */
function sttStatus(
  configured: SttArtifactId,
  downloaded: boolean,
  { siblingDownloaded = true }: { siblingDownloaded?: boolean } = {},
): SttStatusResponse {
  const sibling: SttArtifactId = configured === QWEN ? V3 : QWEN;
  return {
    configured_artifact_id: configured,
    loaded_artifact_id: downloaded ? configured : null,
    artifacts: [
      {
        id: configured,
        backend: configured === QWEN ? "qwen" : "parakeet",
        downloaded,
        active: downloaded,
        size_bytes: downloaded ? 600_000_000 : 0,
      },
      {
        id: sibling,
        backend: sibling === QWEN ? "qwen" : "parakeet",
        downloaded: siblingDownloaded,
        active: false,
        size_bytes: siblingDownloaded ? 900_000_000 : 0,
      },
    ],
    unused_size_bytes: siblingDownloaded ? 900_000_000 : 0,
    parakeet: {
      downloaded: configured === V3 ? downloaded : siblingDownloaded,
      active: configured === V3 && downloaded,
    },
    qwen: {
      downloaded: configured === QWEN ? downloaded : siblingDownloaded,
      active: configured === QWEN && downloaded,
    },
    cohere: { downloaded: false, active: false },
  };
}

// Minimally-typed AppConfig with the given setup_completed.
function configWith(setup_completed: boolean): AppConfig {
  return {
    primary_shortcut: "right shift",
    push_to_talk_shortcut: "right shift",
    toggle_shortcut: "",
    paste_last_shortcut: "",
    paste_last_original_shortcut: "",
    stt_model_selection_mode: "automatic",
    stt_backend: "parakeet",
    parakeet_model: "nemo-parakeet-tdt-0.6b-v2",
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

function mount(
  initial: HarnessProps,
  opts: { strict?: boolean } = {},
): {
  rerender: (next: HarnessProps) => void;
  unmount: () => void;
  container: HTMLDivElement;
} {
  const wrap = (props: HarnessProps) =>
    opts.strict ? (
      <StrictMode>
        <Harness {...props} />
      </StrictMode>
    ) : (
      <Harness {...props} />
    );
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root: Root;
  act(() => {
    root = createRoot(container);
    root.render(wrap(initial));
  });
  return {
    rerender(next) {
      act(() => {
        root.render(wrap(next));
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

// Flush the async getSttStatus().then() microtask + the resulting React commit.
async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

const isVisible = (c: HTMLElement) =>
  c.querySelector(`[data-testid="${DOWNLOAD_DIALOG_TESTID}"]`) !== null;

describe("STT missing-model recovery probe", () => {
  it("surfaces DownloadDialog when setup is complete but the configured artifact is not downloaded", async () => {
    const getSttStatus = vi.fn().mockResolvedValue(sttStatus(V3, false));
    const { container, unmount } = mount({ config: configWith(true), getSttStatus });
    await flush();
    expect(isVisible(container)).toBe(true);
    expect(getSttStatus).toHaveBeenCalledTimes(1);
    unmount();
  });

  it("does NOT surface DownloadDialog when the configured artifact IS downloaded (no false positive)", async () => {
    const getSttStatus = vi.fn().mockResolvedValue(sttStatus(V3, true));
    const { container, unmount } = mount({ config: configWith(true), getSttStatus });
    await flush();
    expect(isVisible(container)).toBe(false);
    unmount();
  });

  it("does NOT probe while setup is incomplete (onboarding owns the fresh-install download)", async () => {
    const getSttStatus = vi.fn().mockResolvedValue(sttStatus(V3, false));
    const { container, unmount } = mount({ config: configWith(false), getSttStatus });
    await flush();
    expect(isVisible(container)).toBe(false);
    expect(getSttStatus).not.toHaveBeenCalled();
    unmount();
  });

  it("probes once — a later config change does not re-run getSttStatus", async () => {
    const getSttStatus = vi.fn().mockResolvedValue(sttStatus(V3, false));
    const { container, rerender, unmount } = mount({
      config: configWith(true),
      getSttStatus,
    });
    await flush();
    expect(isVisible(container)).toBe(true);
    // A config refetch (e.g. WS reconnect) must not re-probe.
    rerender({ config: configWith(true), getSttStatus });
    await flush();
    expect(getSttStatus).toHaveBeenCalledTimes(1);
    unmount();
  });

  it("surfaces DownloadDialog for a configured Qwen artifact that is not downloaded", async () => {
    const getSttStatus = vi.fn().mockResolvedValue(sttStatus(QWEN, false));
    const { container, unmount } = mount({ config: configWith(true), getSttStatus });
    await flush();
    expect(isVisible(container)).toBe(true);
    unmount();
  });

  // Retention regression: a downloaded sibling artifact is normal now and must
  // never be mistaken for the configured one being present.
  it("ignores a retained inactive artifact when the configured one is missing", async () => {
    const getSttStatus = vi
      .fn()
      .mockResolvedValue(sttStatus(QWEN, false, { siblingDownloaded: true }));
    const { container, unmount } = mount({ config: configWith(true), getSttStatus });
    await flush();
    expect(isVisible(container)).toBe(true);
    unmount();
  });

  // Regression for the design bug found by the end-to-end drive: the earlier
  // version depended on `config` and used a once-only ref guard, so StrictMode's
  // effect double-invoke (run1 registers cleanup -> cleanup sets cancelled=true;
  // run2 early-returns on the ref) swallowed the only probe and the dialog never
  // appeared in a production-style single-connect launch. The corrected effect
  // depends on `setupComplete` and drops the ref guard, so StrictMode's second
  // pass completes the probe.
  it("still surfaces DownloadDialog under StrictMode double-invoke", async () => {
    const getSttStatus = vi.fn().mockResolvedValue(sttStatus(V3, false));
    const { container, unmount } = mount(
      { config: configWith(true), getSttStatus },
      { strict: true },
    );
    await flush();
    expect(isVisible(container)).toBe(true);
    unmount();
  });
});
