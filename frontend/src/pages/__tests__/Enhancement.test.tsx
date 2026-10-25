/**
 * Vitest + Testing Library coverage for the Enhancement page.
 *
 * Tests:
 *   - The Connection block (issue 1): unconfigured cloud provider renders the
 *     guided setup card EXPANDED (provider dropdown + API key field visible);
 *     a connected cloud provider COLLAPSES to a one-line summary (key field
 *     hidden) and expands inline on click; enabling AI Enhancement while
 *     unconfigured opens the setup card (no silent "Add API key first" error).
 *   - Picking a cloud provider in the dropdown fires DataLeavesDialog BEFORE
 *     PrivacyModal — Cancel leaves the dropdown on the previous selection.
 *
 * NOTE: tests that previously exercised the standalone "Local model" section
 * (the model status row, toggle-guard "Download model first" error, LowRamDialog
 * triggered from the row's Download button) were removed when the section
 * itself was removed in Task 2.4 of ai-enhance-tweaks. Task 2.5 (toggle-on
 * confirm + auto-enable flow) replaces that behavior and is responsible for
 * adding back coverage of the disk/RAM gates from the new entry point.
 *
 * NOTE: the Advanced disclosure was removed in Task 2.1 of unship-local.
 * The provider/key/model controls now live inside the stateful Connection block
 * (issue 1): always reachable, but collapsed behind a summary row once connected.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor, within, act } from "@testing-library/react";
import { Enhancement } from "../Enhancement";
import type { AppConfig } from "../../lib/api/config";
import * as api_config from "../../lib/api/config";
import * as api_consent from "../../lib/api/consent";
import * as api_enhancement from "../../lib/api/enhancement";
import type { AppEvent } from "../../lib/socket/events";
import type { DailyUseCapabilityReport } from "../../lib/dailyUseCapabilities";
import type { AdvancedCapabilityReport } from "../../lib/advancedCapabilities";

const CAPABILITIES: DailyUseCapabilityReport = {
  schema_version: 1,
  platform: "Windows",
  capabilities: [
    { id: "cloud-enhancement", state: "available", reason: "implemented", presentation: "enable" },
    { id: "managed-local-enhancement", state: "available", reason: "implemented", presentation: "enable" },
    { id: "ollama-enhancement", state: "external-dependency", reason: "endpoint-required", presentation: "explain" },
  ],
};
const MAC_CAPABILITIES: DailyUseCapabilityReport = {
  schema_version: 1,
  platform: "Darwin",
  capabilities: [
    { id: "cloud-enhancement", state: "available", reason: "shared-transport", presentation: "enable" },
    { id: "managed-local-enhancement", state: "not-implemented", reason: "phase5-pending", presentation: "disable" },
    { id: "ollama-enhancement", state: "external-dependency", reason: "endpoint-required", presentation: "explain" },
  ],
};
const ADVANCED_CAPABILITIES: AdvancedCapabilityReport = {
  schema_version: 1,
  platform: "Windows",
  capabilities: [
    { id: "app-identity", state: "available", reason: "implemented", presentation: "enable" },
    { id: "app-path-browse", state: "available", reason: "implemented", presentation: "enable" },
    { id: "browser-page-identity", state: "available", reason: "implemented", presentation: "enable" },
  ],
};
const MAC_ADVANCED_CAPABILITIES: AdvancedCapabilityReport = {
  schema_version: 1,
  platform: "Darwin",
  capabilities: [
    { id: "app-identity", state: "available", reason: "appkit-context", presentation: "enable" },
    { id: "app-path-browse", state: "unavailable", reason: "raw-path-boundary-unavailable", presentation: "disable" },
    { id: "browser-page-identity", state: "available", reason: "accessibility-host-only", presentation: "enable" },
  ],
};
const UNSUPPORTED_ADVANCED_CAPABILITIES: AdvancedCapabilityReport = {
  schema_version: 1,
  platform: "Plan9",
  capabilities: [
    { id: "app-identity", state: "unavailable", reason: "unsupported-platform", presentation: "disable" },
    { id: "app-path-browse", state: "unavailable", reason: "unsupported-platform", presentation: "disable" },
    { id: "browser-page-identity", state: "unavailable", reason: "unsupported-platform", presentation: "disable" },
  ],
};

let savedTestConfig: AppConfig | null = null;
let savedTestRevision: number | null = null;
beforeEach(() => {
  savedTestConfig = null;
  savedTestRevision = null;
  vi.spyOn(api_config, "getConfig").mockImplementation(async () => savedTestConfig ?? makeConfig());
  vi.spyOn(api_enhancement, "getEnhancementThinking").mockResolvedValue({ kind: "unknown", options: [], default: "default", effective: "default", preference_key: "test-model" });
  vi.spyOn(api_consent, "getBYOKConsentContext").mockResolvedValue({ has_consent: true, matches_config: true } as api_consent.BYOKConsentContext);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Minimal fetch mock that returns a single-entry local model catalog.
// The catalog now ships ONLY gemma3:1b — Ollama is the escape hatch for
// users who want a different local model.
// ---------------------------------------------------------------------------
const LOCAL_MODELS_RESPONSE = {
  provider: "local",
  bundled: [
    {
      id: "gemma4-e2b-instruct",
      label: "Gemma 4 E2B Instruct",
      disk_mb: 2962,
      ram_mb: 1280,
      // Threshold the LowRamDialog compares against (decoupled from ram_mb).
      // Catalog ships 8 GB; tests below pin to this so the gate behavior is
      // exercised at the production threshold.
      min_system_ram_mb: 8192,
      recommended: true,
    },
  ],
  installed_ids: [],
  available_ids: ["gemma4-e2b-instruct"],
  active_id: "gemma4-e2b-instruct",
  running: false,
};

const STREAM_1_MINI_ROW = {
  id: "stream-1-mini",
  label: "Stream-1 Mini",
  disk_mb: 462,
  ram_mb: 1024,
  min_system_ram_mb: 4096,
  recommended: true,
  source: "production",
  selectable: true,
  downloadable: true,
  prompt_key: "stream-mini",
  serving_contract: "wrap-v2",
  trained_modes: ["chat", "email"],
  capabilities: { family: "trained-mode", editable_prompt: false, supported_modes: ["chat", "email"] },
};

const MULTI_PRODUCTION_MODELS_RESPONSE = {
  ...LOCAL_MODELS_RESPONSE,
  bundled: [
    STREAM_1_MINI_ROW,
    { ...LOCAL_MODELS_RESPONSE.bundled[0], recommended: false, source: "production" },
  ],
  available_ids: ["stream-1-mini", "gemma4-e2b-instruct"],
};

const CANDIDATE_MODELS_RESPONSE = {
  provider: "local",
  bundled: [
    {
      id: "gemma4-e2b-instruct",
      label: "Gemma 4 E2B Instruct",
      disk_mb: 2962,
      ram_mb: 1280,
      source: "production",
      artifact_available: false,
      selectable: true,
      downloadable: true,
      active: false,
    },
    {
      id: "stream-1",
      label: "Stream-1",
      disk_mb: 697,
      ram_mb: 1536,
      source: "production",
      artifact_available: false,
      selectable: false,
      downloadable: true,
      active: false,
    },
    ...[
      ["b1emoji_r1-s1", "350M", "Ship pick", "Incumbent", true, true],
      ["w1wrap350m_r2-s2", "350M", "Review candidate", "General-Lite contender", false, false],
      ["w5wrap_email-s3", "350M", "Review candidate", "Email-specific option", false, false],
      ["w6wrap_oclean-s1", "350M", "Review candidate", "Obey-clean contender", false, false],
      ["w10wrap_doless-s1", "350M", "Review candidate", "Does-less option", false, false],
      ["w2wrap1p2b-s3", "1.2B", "Review candidate", "H2H/mic-validated incumbent", true, false],
      ["w8wrap_closure-s1", "1.2B", "Review candidate", "Best closure r1 artifact", false, false],
    ].map(([id, parameterSize, role, status, artifactAvailable, active]) => ({
      id,
      label: id,
      disk_mb: 0,
      ram_mb: 0,
      source: "candidate",
      experiment_id: id,
      parameter_size: parameterSize,
      role,
      status,
      serving_contract: id === "b1emoji_r1-s1" ? "raw-v1" : "wrap-v2",
      trained_modes: ["chat", "code"],
      artifact_available: artifactAvailable,
      selectable: artifactAvailable,
      downloadable: false,
      active,
      prompt_key: "stream",
    })),
  ],
  installed_ids: [],
  // stream-1 is intentionally omitted to exercise the unpublished-R2 gate.
  available_ids: ["gemma4-e2b-instruct"],
  active_id: "b1emoji_r1-s1",
  running: false,
};

function makeFetchMock(status: number, body: unknown): ReturnType<typeof vi.fn> {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  });
}

function makeCandidateFetchMock(): ReturnType<typeof vi.fn> {
  return vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (
      typeof url === "string"
      && url.includes("/api/enhancement/local-activate")
    ) {
      const modelId = JSON.parse(String(init?.body ?? "{}")).model_id;
      return Promise.resolve(jsonResponse(200, {
        status: "activated",
        model_id: modelId,
      }));
    }
    return Promise.resolve(jsonResponse(200, CANDIDATE_MODELS_RESPONSE));
  });
}

// A few running apps for the RunningAppPicker (issue 2). The default local
// catalog mock above returns this object shape for ALL urls; the routing mock
// below distinguishes /api/apps/running so the picker has real rows.
// A tiny 1x1 transparent PNG data-URI stands in for a real app-icon payload.
const TINY_PNG =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

// Icons: Slack and Code carry a data-URI; Discord has none (null) so the
// picker's text fallback path is exercised in the same list.
const RUNNING_APPS_RESPONSE = [
  { process_name: "slack.exe", display_name: "Slack", icon: TINY_PNG },
  { process_name: "code.exe", display_name: "Visual Studio Code", icon: TINY_PNG },
  { process_name: "discord.exe", display_name: "Discord", icon: null },
];

const BUILTIN_PROMPT_RESPONSE = { mode: "chat", prompt: "Seeded chat prompt body." };

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

// URL-routing fetch mock for the Profiles tests: real running-app list on
// /api/apps/running, install-aware resolution on /api/apps/resolve (every
// requested name is reported installed so the profile chips render, mirroring
// a machine where the mapped apps exist), a builtin prompt on
// /api/enhancement/builtin-prompt, and the local-model catalog everywhere else.
function makeRoutingFetchMock(): ReturnType<typeof vi.fn> {
  return vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (typeof url === "string" && url.includes("/api/apps/running")) {
      return Promise.resolve(jsonResponse(200, RUNNING_APPS_RESPONSE));
    }
    if (typeof url === "string" && url.includes("/api/apps/installed")) {
      return Promise.resolve(jsonResponse(200, []));
    }
    if (typeof url === "string" && url.includes("/api/apps/resolve")) {
      let names: string[] = [];
      try {
        names = JSON.parse(String(init?.body ?? "{}")).names ?? [];
      } catch {
        names = [];
      }
      const resolved = names.map((name) => {
        const key = name.toLowerCase();
        const known = RUNNING_APPS_RESPONSE.find(
          (a) => a.process_name.toLowerCase() === key,
        );
        return {
          process_name: key,
          installed: true,
          display_name: known?.display_name ?? name,
          icon: known?.icon ?? null,
        };
      });
      return Promise.resolve(jsonResponse(200, resolved));
    }
    if (typeof url === "string" && url.includes("/api/enhancement/builtin-prompt")) {
      return Promise.resolve(jsonResponse(200, BUILTIN_PROMPT_RESPONSE));
    }
    return Promise.resolve(jsonResponse(200, LOCAL_MODELS_RESPONSE));
  });
}

// A no-op subscribe function that satisfies the type contract.
function makeSubscribe() {
  return (_handler: (event: AppEvent) => void) => () => {};
}

function appEventHarness() {
  let handler: ((event: AppEvent) => void) | undefined;
  return {
    subscribe(next: (event: AppEvent) => void) {
      handler = next;
      return () => {
        handler = undefined;
      };
    },
    emit(event: AppEvent) {
      act(() => handler?.(event));
    },
  };
}

// Minimal AppConfig with local provider.
function makeConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    primary_shortcut: "right shift",
    push_to_talk_shortcut: "right shift",
    toggle_shortcut: "",
    paste_last_shortcut: "",
    paste_last_original_shortcut: "",
    stt_model_selection_mode: "automatic",
    stt_backend: "parakeet",
    parakeet_model: "parakeet-tdt-0.6b-v2",
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
    clipboard_clear_delay: 1000,
    input_style: "clipboard",
    input_style_user_set: false,
    keystroke_delay_ms: 0,
    model_timeout: 10,
    audio_feedback: true,
    audio_feedback_volume: 1,
    start_sound_path: null,
    stop_sound_path: null,
    error_sound_path: null,
    max_recording_duration: 60,
    system_volume_mode: "off",
    recording_mode: "push_to_talk",
    run_on_startup: false,
    audio_input_device: "default",
    show_recording_indicator: true,
    show_recording_control: false,
    show_live_preview: false,
    setup_completed: true,
    dismissed_offer_ids: [],
    shown_offer_ids: [],
    enhancement_enabled: false,
    enhancement_provider: "local",
    enhancement_api_key: "",
    enhancement_gemini_api_key: "",
    enhancement_openai_api_key: "",
    enhancement_anthropic_api_key: "",
    enhancement_ollama_endpoint: "",
    enhancement_ollama_model: "",
    enhancement_gemini_model: "",
    enhancement_openai_model: "",
    enhancement_anthropic_model: "",
    enhancement_mode: "chat",
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
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function enhancementElement(
  configOverrides: Partial<AppConfig> = {},
  {
    onConfigChange = vi.fn(),
    configSaveRevision = 0,
    onRequestPrivacyModal = vi.fn(),
    acknowledgedProviders = [] as string[],
    onConsentChanged = vi.fn(),
    subscribe = makeSubscribe(),
    send = vi.fn(() => true),
    connected = true,
    capabilities = CAPABILITIES,
    advancedCapabilities = ADVANCED_CAPABILITIES,
  } = {},
) {
  const config = makeConfig(configOverrides);
  if (savedTestConfig === null || savedTestRevision !== configSaveRevision) {
    savedTestConfig = config;
    savedTestRevision = configSaveRevision;
  }
  return (
    <Enhancement
      config={config}
      onConfigChange={onConfigChange}
      configSaveRevision={configSaveRevision}
      onRequestPrivacyModal={onRequestPrivacyModal}
      acknowledgedProviders={acknowledgedProviders}
      onConsentChanged={onConsentChanged}
      subscribe={subscribe}
      send={send}
      connected={connected}
      capabilities={capabilities}
      advancedCapabilities={advancedCapabilities}
    />
  );
}

function renderEnhancement(
  configOverrides: Partial<AppConfig> = {},
  options: Parameters<typeof enhancementElement>[1] = {},
) {
  return render(enhancementElement(configOverrides, options));
}

describe("Enhancement recipient admission", () => {
  beforeEach(() => { globalThis.fetch = makeFetchMock(200, LOCAL_MODELS_RESPONSE); });

  it("blocks probes and prompt tests when only historical provider consent exists", async () => {
    vi.mocked(api_consent.getBYOKConsentContext).mockResolvedValue({ has_consent: false, matches_config: true } as api_consent.BYOKConsentContext);
    const check = vi.spyOn(api_enhancement, "checkEnhancement");
    renderEnhancement({ enhancement_provider: "openai", enhancement_enabled: true, enhancement_openai_api_key: "test-key" }, { acknowledgedProviders: ["openai"] });
    expect(await screen.findByRole("button", { name: "Review privacy notice" })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Chat" }));
    fireEvent.click(await screen.findByText("Try it"));
    fireEvent.change(screen.getByLabelText("Sample input"), { target: { value: "sample" } });
    expect(screen.getByRole("button", { name: "Run prompt test" })).toBeDisabled();
    expect(screen.getByText("Review the privacy notice to test prompts with this connection.")).toBeInTheDocument();
    expect(check).not.toHaveBeenCalled();
  });

  it("blocks an accepted draft until its exact target reaches backend config", async () => {
    vi.mocked(api_consent.getBYOKConsentContext).mockResolvedValue({ has_consent: true, matches_config: false } as api_consent.BYOKConsentContext);
    const check = vi.spyOn(api_enhancement, "checkEnhancement");
    renderEnhancement({ enhancement_provider: "openai", enhancement_enabled: true, enhancement_openai_api_key: "test-key", enhancement_openai_model: "new-model" });
    await waitFor(() => expect(api_consent.getBYOKConsentContext).toHaveBeenCalledWith({ provider: "openai", model: "new-model" }));
    expect(screen.getByRole("button", { name: "Review privacy notice" })).toBeInTheDocument();
    expect(check).not.toHaveBeenCalled();
  });

  it("invalidates permission immediately when an endpoint is typed before debounce", async () => {
    vi.mocked(api_consent.getBYOKConsentContext).mockResolvedValueOnce({ has_consent: true, matches_config: true } as api_consent.BYOKConsentContext)
      .mockReturnValue(new Promise(() => {}));
    const check = vi.spyOn(api_enhancement, "checkEnhancement").mockResolvedValue({ connected: true, message: "Connected" });
    const onConfigChange = vi.fn();
    renderEnhancement({ enhancement_provider: "ollama", enhancement_enabled: true }, { onConfigChange });
    await waitFor(() => expect(check).toHaveBeenCalledOnce());
    const summary = await screen.findByRole("button", { name: /Ollama.*Server on this device/, expanded: false });
    if (summary.getAttribute("aria-expanded") === "false") fireEvent.click(summary);
    fireEvent.change(await screen.findByLabelText("Endpoint"), { target: { value: "https://new.example/v1" } });
    expect(screen.getByRole("button", { name: "Review privacy notice" })).toBeInTheDocument();
    expect(onConfigChange).not.toHaveBeenCalled();
    expect(check).toHaveBeenCalledOnce();
  });

  it("turning Enhancement off blocks a connected provider's prompt test", async () => {
    renderEnhancement({ enhancement_provider: "openai", enhancement_enabled: false, enhancement_openai_api_key: "test-key" });
    fireEvent.click(await screen.findByRole("button", { name: "Edit Chat" }));
    fireEvent.click(await screen.findByText("Try it"));
    fireEvent.change(screen.getByLabelText("Sample input"), { target: { value: "sample" } });
    expect(screen.getByRole("button", { name: "Run prompt test" })).toBeDisabled();
    expect(screen.getAllByText("Turn on Enhancement to test prompts or check the connection.").length).toBeGreaterThan(0);
  });
});

async function expandLocalConnection() {
  const localButtons = await screen.findAllByRole("button", { name: /Local/ });
  const summary = localButtons.find(
    (button) =>
      button.hasAttribute("aria-expanded")
      && !button.hasAttribute("aria-haspopup"),
  );
  if (!summary) throw new Error("Local connection summary not found");
  if (summary.getAttribute("aria-expanded") === "false") {
    fireEvent.click(summary);
  }
}

describe("Enhancement thinking and workspace recovery", () => {
  const anthropic = {
    enhancement_provider: "anthropic",
    enhancement_anthropic_model: "claude-haiku-5-5",
    enhancement_anthropic_api_key: "test-key",
    enhancement_enabled: true,
    enhancement_local_nudge_seen: true,
  };
  const configurable: api_enhancement.EnhancementThinking = {
    kind: "configurable", options: ["off", "low", "medium", "high"],
    default: "off", effective: "off", preference_key: "anthropic-model",
  };
  beforeEach(() => {
    globalThis.fetch = makeFetchMock(200, LOCAL_MODELS_RESPONSE);
    vi.mocked(api_enhancement.getEnhancementThinking).mockResolvedValue(configurable);
    vi.spyOn(api_enhancement, "checkEnhancement").mockResolvedValue({ connected: true, message: "Connected" });
  });
  async function expandConnection() {
    const summary = (await screen.findAllByRole("button", { name: /Anthropic.*sent to Anthropic/ })).find((button) => button.hasAttribute("aria-expanded"))!;
    if (summary.getAttribute("aria-expanded") === "false") fireEvent.click(summary);
  }
  async function advanced() {
    await waitFor(() => expect(api_enhancement.getEnhancementThinking).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
  }

  it("saves only a backend-keyed thinking choice and leaves the timeout unchanged", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({ ...anthropic, enhancement_timeout_seconds: 5 }, { onConfigChange });
    await advanced();
    const trigger = await screen.findByRole("button", { name: "Thinking: Off" });
    expect(trigger).toHaveAttribute("aria-describedby", "enhancement-thinking-help");
    fireEvent.click(trigger);
    expect(screen.getByRole("option", { name: /Off.*Default/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: "High" }));
    expect(onConfigChange).toHaveBeenLastCalledWith({ enhancement_thinking_levels: { "anthropic-model": "high" } });
    expect(screen.getByLabelText("Enhancement timeout in seconds")).toHaveValue(5);
  });

  it("shows only verified mandatory choices and describes their minimum", async () => {
    vi.mocked(api_enhancement.getEnhancementThinking).mockResolvedValue({
      ...configurable, kind: "mandatory", options: ["minimal", "low", "medium", "high"], default: "minimal", effective: "minimal",
    });
    renderEnhancement(anthropic);
    await advanced();
    fireEvent.click(await screen.findByRole("button", { name: "Thinking: Minimal" }));
    expect(screen.queryByRole("option", { name: /^Off/ })).toBeNull();
    expect(screen.getByText("This model requires thinking. Minimal is its lowest setting.")).toBeInTheDocument();
  });

  it.each([
    ["unknown", "default", "Provider default", "Thinking settings are not available for this connection. The provider's default applies."],
    ["fixed", "off", "Off", "Fixed for this model."],
  ] as const)("renders truthful %s status without an interactive control", async (kind, effective, label, help) => {
    vi.mocked(api_enhancement.getEnhancementThinking).mockResolvedValue({ ...configurable, kind, options: [], effective });
    renderEnhancement(anthropic);
    await advanced();
    expect(await screen.findByText(help)).toBeInTheDocument();
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Thinking:/ })).toBeNull();
  });

  it("keeps a saved workspace hidden until a typed workspace error", async () => {
    vi.mocked(api_enhancement.checkEnhancement).mockResolvedValue({ connected: false, message: "upstream arbitrary workspace prose" });
    renderEnhancement({ ...anthropic, enhancement_anthropic_workspace_id: "wrkspc_saved_test" });
    await screen.findByRole("status", { name: /^Error:/ });
    await expandConnection();
    expect(screen.queryByLabelText("Workspace ID")).toBeNull();
    expect(screen.queryByText("upstream arbitrary workspace prose")).toBeNull();
  });

  it("reveals recovery, saves on blur, and stays editable after success and clearing", async () => {
    vi.mocked(api_enhancement.checkEnhancement).mockResolvedValueOnce({ connected: false, message: "bounded", error_code: "workspace_required" });
    const onConfigChange = vi.fn();
    const view = renderEnhancement(anthropic, { onConfigChange });
    const field = await screen.findByLabelText("Workspace ID");
    expect(screen.getByLabelText("Workspace ID")).toHaveAccessibleDescription(/This key needs a workspace/);
    fireEvent.change(field, { target: { value: "wrkspc_test" } });
    expect(screen.getByRole("status", { name: "Connection not checked" })).toBeInTheDocument();
    fireEvent.blur(field);
    expect(onConfigChange).toHaveBeenLastCalledWith({ enhancement_anthropic_workspace_id: "wrkspc_test" });
    view.rerender(enhancementElement({ ...anthropic, enhancement_anthropic_workspace_id: "wrkspc_test" }, { onConfigChange, configSaveRevision: 1 }));
    await screen.findByRole("status", { name: "Cloud provider connected" });
    expect(screen.getByLabelText("Workspace ID")).toHaveValue("wrkspc_test");
    fireEvent.click(screen.getByRole("button", { name: "Clear Workspace ID" }));
    expect(onConfigChange).toHaveBeenLastCalledWith({ enhancement_anthropic_workspace_id: "" });
    expect(screen.getByLabelText("Workspace ID")).toHaveValue("");
  });

  it("replaces a key and clears its workspace in one patch, hiding recovery immediately", async () => {
    vi.mocked(api_enhancement.checkEnhancement).mockResolvedValueOnce({ connected: false, message: "bounded", error_code: "workspace_unavailable" });
    const onConfigChange = vi.fn();
    renderEnhancement({ ...anthropic, enhancement_anthropic_workspace_id: "wrkspc_old_test" }, { onConfigChange });
    await screen.findByLabelText("Workspace ID");
    fireEvent.click(screen.getByRole("button", { name: "Replace API key" }));
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "replacement-test-key" } });
    expect(screen.queryByLabelText("Workspace ID")).toBeNull();
    fireEvent.blur(screen.getByLabelText("API key"));
    expect(onConfigChange).toHaveBeenLastCalledWith({ enhancement_anthropic_api_key: "replacement-test-key", enhancement_anthropic_workspace_id: "" });
  });

  it("resets recovery when switching provider, including switching back to the same key", async () => {
    vi.mocked(api_enhancement.checkEnhancement).mockResolvedValueOnce({ connected: false, message: "bounded", error_code: "workspace_required" });
    const view = renderEnhancement(anthropic);
    await screen.findByLabelText("Workspace ID");
    view.rerender(enhancementElement({ ...anthropic, enhancement_provider: "openai", enhancement_openai_api_key: "another-test-key" }, { configSaveRevision: 1 }));
    expect(screen.queryByLabelText("Workspace ID")).toBeNull();
    view.rerender(enhancementElement(anthropic, { configSaveRevision: 2 }));
    await screen.findByRole("status", { name: "Cloud provider connected" });
    expect(screen.queryByLabelText("Workspace ID")).toBeNull();
  });

  it("ignores a late workspace failure after a credential draft and blocks new probes", async () => {
    const old = deferred<{ connected: boolean; message: string; error_code: string }>();
    vi.mocked(api_enhancement.checkEnhancement).mockReturnValueOnce(old.promise);
    const view = renderEnhancement(anthropic);
    await waitFor(() => expect(api_enhancement.checkEnhancement).toHaveBeenCalledOnce());
    await expandConnection();
    fireEvent.click(screen.getByRole("button", { name: "Replace API key" }));
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "draft-test-key" } });
    view.rerender(enhancementElement(anthropic, { configSaveRevision: 1 }));
    await act(async () => { old.resolve({ connected: false, message: "bounded", error_code: "workspace_required" }); });
    expect(api_enhancement.checkEnhancement).toHaveBeenCalledOnce();
    expect(screen.queryByLabelText("Workspace ID")).toBeNull();
    expect(screen.getByRole("status", { name: "Connection not checked" })).toBeInTheDocument();
  });

  it("ignores a late success as soon as the workspace draft changes", async () => {
    vi.mocked(api_enhancement.checkEnhancement).mockResolvedValueOnce({ connected: false, message: "bounded", error_code: "workspace_required" });
    const view = renderEnhancement(anthropic);
    fireEvent.change(await screen.findByLabelText("Workspace ID"), { target: { value: "wrkspc_first" } });
    fireEvent.blur(screen.getByLabelText("Workspace ID"));
    const old = deferred<{ connected: boolean; message: string }>();
    vi.mocked(api_enhancement.checkEnhancement).mockReturnValueOnce(old.promise);
    view.rerender(enhancementElement({ ...anthropic, enhancement_anthropic_workspace_id: "wrkspc_first" }, { configSaveRevision: 1 }));
    await waitFor(() => expect(api_enhancement.checkEnhancement).toHaveBeenCalledTimes(2));
    fireEvent.change(screen.getByLabelText("Workspace ID"), { target: { value: "wrkspc_second" } });
    await act(async () => { old.resolve({ connected: true, message: "Connected" }); });
    expect(screen.queryByRole("status", { name: "Cloud provider connected" })).toBeNull();
    expect(screen.getByRole("status", { name: "Connection not checked" })).toBeInTheDocument();
  });

  it("does not probe an optimistic model when an older queued save is acknowledged", async () => {
    const view = renderEnhancement(anthropic);
    await waitFor(() => expect(api_enhancement.checkEnhancement).toHaveBeenCalledOnce());
    vi.mocked(api_config.getConfig).mockResolvedValue(makeConfig(anthropic));
    view.rerender(enhancementElement({ ...anthropic, enhancement_anthropic_model: "new-test-model" }, { configSaveRevision: 1 }));
    await act(async () => {});
    expect(api_enhancement.checkEnhancement).toHaveBeenCalledOnce();
    expect(screen.getByRole("status", { name: "Connection not checked" })).toBeInTheDocument();
  });

  it("preserves thinking and connection evidence when only the cleanup profile changes", async () => {
    const config = { ...anthropic, enhancement_thinking_levels: { "anthropic-model": "high" } };
    const view = renderEnhancement(config);
    await screen.findByRole("status", { name: "Cloud provider connected" });
    await advanced();
    expect(await screen.findByRole("button", { name: "Thinking: High" })).toBeInTheDocument();
    view.rerender(enhancementElement({ ...config, default_profile_id: "code" }, { configSaveRevision: 1 }));
    await act(async () => {});
    expect(screen.getByRole("status", { name: "Cloud provider connected" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Thinking: High" })).toBeInTheDocument();
    expect(api_enhancement.checkEnhancement).toHaveBeenCalledOnce();
  });

  it("restores each exact model's remembered choice after switching away and back", async () => {
    vi.mocked(api_enhancement.getEnhancementThinking).mockImplementation(async () => ({
      ...configurable,
      preference_key: savedTestConfig?.enhancement_anthropic_model === "other-model" ? "other-model-key" : "anthropic-model",
    }));
    const config = { ...anthropic, enhancement_thinking_levels: { "anthropic-model": "high", "other-model-key": "low" } };
    const view = renderEnhancement(config);
    await advanced();
    expect(await screen.findByRole("button", { name: "Thinking: High" })).toBeInTheDocument();
    view.rerender(enhancementElement({ ...config, enhancement_anthropic_model: "other-model" }, { configSaveRevision: 1 }));
    expect(await screen.findByRole("button", { name: "Thinking: Low" })).toBeInTheDocument();
    view.rerender(enhancementElement(config, { configSaveRevision: 2 }));
    expect(await screen.findByRole("button", { name: "Thinking: High" })).toBeInTheDocument();
  });

  it("keeps a newer workspace draft when an older save returns", async () => {
    vi.mocked(api_enhancement.checkEnhancement).mockResolvedValueOnce({ connected: false, message: "bounded", error_code: "workspace_required" });
    const view = renderEnhancement(anthropic);
    fireEvent.change(await screen.findByLabelText("Workspace ID"), { target: { value: "wrkspc_first" } });
    fireEvent.blur(screen.getByLabelText("Workspace ID"));
    fireEvent.change(screen.getByLabelText("Workspace ID"), { target: { value: "wrkspc_newer" } });
    view.rerender(enhancementElement({ ...anthropic, enhancement_anthropic_workspace_id: "wrkspc_first" }, { configSaveRevision: 1 }));
    await act(async () => {});
    expect(screen.getByLabelText("Workspace ID")).toHaveValue("wrkspc_newer");
    expect(api_enhancement.checkEnhancement).toHaveBeenCalledOnce();
  });
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("Enhancement — default provider is local", () => {
  beforeEach(() => {
    globalThis.fetch = makeFetchMock(200, LOCAL_MODELS_RESPONSE);
  });

  it("renders the page heading", async () => {
    renderEnhancement();
    expect(screen.getByText("Enhancement")).toBeInTheDocument();
  });

  it("disables excluded managed local cleanup from the Mac capability report", async () => {
    renderEnhancement(
      { enhancement_provider: "local", enhancement_enabled: false },
      { capabilities: MAC_CAPABILITIES },
    );
    expect(
      screen.getByText("Managed on-device cleanup is not available on this platform yet."),
    ).toBeTruthy();
    expect(screen.getByRole("switch", { name: /ai enhancement/i })).toBeDisabled();
  });

  it("uses the neutral toggle description copy", async () => {
    renderEnhancement();
    expect(screen.getByText("Clean up dictation with an AI model.")).toBeInTheDocument();
  });

  it("rechecks the currently selected slow model instead of the auto check", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/enhancement/benchmark/start")) {
        return Promise.resolve(jsonResponse(200, { status: "started" }));
      }
      return Promise.resolve(jsonResponse(200, {
        ...LOCAL_MODELS_RESPONSE,
        installed_ids: ["gemma4-e2b-instruct"],
      }));
    });
    globalThis.fetch = fetchMock;
    renderEnhancement({
      enhancement_local_model: "gemma4-e2b-instruct",
      enhancement_local_gpu_verdicts: {
        "gemma4-e2b-instruct": "marginal",
      },
    });

    fireEvent.click(await screen.findByRole("button", { name: /Local/ }));
    expect(await screen.findByText("Runs slowly on this computer.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        String(url).includes("/api/enhancement/benchmark/start"),
      );
      expect(call).toBeDefined();
      expect(JSON.parse(String(call![1]?.body))).toEqual({
        mode: "manual",
        model_id: "gemma4-e2b-instruct",
      });
    });
    expect(screen.queryByRole("button", { name: /Local model:/ })).toBeNull();
  });

  it("uses a manual setup benchmark when only Gemma is available", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/enhancement/benchmark/start")) {
        return Promise.resolve(jsonResponse(200, { status: "started" }));
      }
      return Promise.resolve(jsonResponse(200, LOCAL_MODELS_RESPONSE));
    });
    globalThis.fetch = fetchMock;
    renderEnhancement();

    await expandLocalConnection();
    await screen.findByRole("button", { name: /^Download to use · / });
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.click(await screen.findByRole("button", { name: "Start speed check" }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        String(url).includes("/api/enhancement/benchmark/start"),
      );
      expect(JSON.parse(String(call![1]?.body))).toEqual({
        mode: "manual",
        model_id: "gemma4-e2b-instruct",
      });
    });
  });

  it("first setup checks Stream-1 Mini even when the old Customizable default is stored", async () => {
    const handlers = new Set<(event: AppEvent) => void>();
    const subscribe = (next: (event: AppEvent) => void) => {
      handlers.add(next);
      return () => { handlers.delete(next); };
    };
    const emit = (event: AppEvent) => act(() => handlers.forEach((handler) => handler(event)));
    const onConfigChange = vi.fn();
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/enhancement/benchmark/start")) {
        return Promise.resolve(jsonResponse(200, { status: "started" }));
      }
      return Promise.resolve(jsonResponse(200, MULTI_PRODUCTION_MODELS_RESPONSE));
    });
    globalThis.fetch = fetchMock;
    renderEnhancement(
      { enhancement_local_model: "gemma4-e2b-instruct", enhancement_local_gpu_verdicts: {} },
      { onConfigChange, subscribe },
    );

    await expandLocalConnection();
    await screen.findByRole("button", { name: /Local model:/ });
    fireEvent.click(screen.getByRole("switch"));
    expect(await screen.findByRole("heading", { name: "Set up local enhancement" })).toBeInTheDocument();
    expect(screen.getByText(/we'll get it first \(462 MB\)\./)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start speed check" }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        String(url).includes("/api/enhancement/benchmark/start"),
      );
      expect(JSON.parse(String(call![1]?.body))).toEqual({ mode: "auto" });
    });
    emit({
      type: "benchmark.result", mode: "auto", verdict: "viable", model_downloaded: true,
      freshly_downloaded: true, measured_model_id: "stream-1-mini", measured_tok_per_s: 70,
      verdicts: { "stream-1-mini": "viable" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Start using it" }));
    // ONE combined patch activates Stream-1 Mini with its verdict.
    expect(onConfigChange).toHaveBeenCalledWith({
      enhancement_enabled: true,
      enhancement_provider: "local",
      enhancement_local_gpu_verdicts: { "stream-1-mini": "viable" },
      enhancement_local_model: "stream-1-mini",
    });
  });

  it("keeps a user's earlier model choice once a verdict exists", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/enhancement/benchmark/start")) {
        return Promise.resolve(jsonResponse(200, { status: "started" }));
      }
      return Promise.resolve(jsonResponse(200, MULTI_PRODUCTION_MODELS_RESPONSE));
    });
    globalThis.fetch = fetchMock;
    renderEnhancement({
      enhancement_local_model: "gemma4-e2b-instruct",
      enhancement_local_gpu_verdicts: { "stream-1-mini": "viable" },
    });

    await expandLocalConnection();
    await screen.findByRole("button", { name: /Local model:/ });
    fireEvent.click(screen.getByRole("switch"));
    expect(await screen.findByRole("heading", { name: "Try Customizable" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start speed check" }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        String(url).includes("/api/enhancement/benchmark/start"),
      );
      expect(JSON.parse(String(call![1]?.body))).toEqual({ mode: "manual", model_id: "gemma4-e2b-instruct" });
    });
  });

  it("falls back from an unavailable stored selection without changing config", async () => {
    const unavailableSelectionResponse = {
      ...MULTI_PRODUCTION_MODELS_RESPONSE,
      available_ids: ["gemma4-e2b-instruct"],
    };
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/enhancement/benchmark/start")) {
        return Promise.resolve(jsonResponse(200, { status: "started" }));
      }
      return Promise.resolve(jsonResponse(200, unavailableSelectionResponse));
    });
    const onConfigChange = vi.fn();
    globalThis.fetch = fetchMock;
    renderEnhancement(
      { enhancement_local_model: "stream-1-mini" },
      { onConfigChange },
    );

    await expandLocalConnection();
    await screen.findByRole("button", { name: /^Download to use · / });
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.click(await screen.findByRole("button", { name: "Start speed check" }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        String(url).includes("/api/enhancement/benchmark/start"),
      );
      expect(JSON.parse(String(call![1]?.body))).toEqual({
        mode: "manual",
        model_id: "gemma4-e2b-instruct",
      });
    });
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ enhancement_local_model: expect.anything() }),
    );
  });

  it("hides a one-choice model selector and keeps the helper line under Provider", async () => {
    globalThis.fetch = makeFetchMock(200, {
      ...LOCAL_MODELS_RESPONSE,
      installed_ids: ["gemma4-e2b-instruct"],
    });
    renderEnhancement({
      enhancement_local_gpu_verdicts: { "gemma4-e2b-instruct": "marginal" },
    });

    await expandLocalConnection();
    const recheck = await screen.findByRole("button", { name: "Check again" });
    const providerBlock = screen.getByText("Provider").parentElement!;
    expect(within(providerBlock).getByRole("button", { name: "Check again" })).toBe(recheck);
    expect(screen.queryByRole("button", { name: /Local model:/ })).toBeNull();
    expect(screen.queryByText(/Cleanup runs on this computer ·/)).toBeNull();
    expect(screen.queryByText("You can rewrite its cleanup instructions in Profiles.")).toBeNull();
  });

  it("keeps a multi-choice selector and places its helper line beneath it", async () => {
    globalThis.fetch = makeFetchMock(200, MULTI_PRODUCTION_MODELS_RESPONSE);
    renderEnhancement();

    await expandLocalConnection();
    const modelTrigger = await screen.findByRole("button", { name: /Local model:/ });
    const modelBlock = screen.getByText("Model").parentElement!;
    expect(within(modelBlock).getByRole("button", {
      name: "Download to use · 2.9 GB",
    })).toBeInTheDocument();
    expect(within(modelBlock).getByRole("button", { name: /Local model:/ }))
      .toBe(modelTrigger);
  });

  it("keeps Lockdown failures local and never routes to Gemini", async () => {
    const harness = appEventHarness();
    const onConfigChange = vi.fn();
    const onRequestPrivacyModal = vi.fn();
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/enhancement/benchmark/start")) {
        return Promise.resolve(jsonResponse(200, { status: "started" }));
      }
      return Promise.resolve(jsonResponse(200, LOCAL_MODELS_RESPONSE));
    });
    globalThis.fetch = fetchMock;
    renderEnhancement(
      {
        lockdown_mode: true,
        enhancement_provider: "gemini",
        enhancement_local_model: "gemma4-e2b-instruct",
      },
      { onConfigChange, onRequestPrivacyModal, subscribe: harness.subscribe },
    );

    await expandLocalConnection();
    await screen.findByRole("button", { name: /^Download to use · / });
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.click(await screen.findByRole("button", { name: "Start speed check" }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        String(url).includes("/api/enhancement/benchmark/start"),
      );
      expect(JSON.parse(String(call![1]?.body))).toEqual({
        mode: "manual",
        model_id: "gemma4-e2b-instruct",
      });
    });
    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
    });
    expect(await screen.findByRole("heading", {
      name: "Checking how fast it runs",
    })).toBeInTheDocument();
    harness.emit({
      type: "benchmark.result",
      verdict: "unviable",
      model_downloaded: true,
      mode: "manual",
      measured_model_id: "gemma4-e2b-instruct",
      measured_tok_per_s: 20,
      verdicts: { "gemma4-e2b-instruct": "unviable" },
    });

    expect(await screen.findByRole("button", { name: "Dismiss" }))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Gemini/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onConfigChange).toHaveBeenCalledWith({
      enhancement_local_gpu_verdicts: {
        "gemma4-e2b-instruct": "unviable",
      },
    });
    expect(onRequestPrivacyModal).not.toHaveBeenCalled();
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ enhancement_provider: "gemini" }),
    );
  });

  it("unconfigured cloud provider renders the setup card expanded (key field visible)", () => {
    // issue 1: a cloud provider with NO stored key is "unconfigured" — the
    // Connection block renders the guided setup card EXPANDED, so the provider
    // dropdown + API key field are in the DOM without any user action.
    renderEnhancement({ enhancement_provider: "gemini" });
    expect(screen.getByText("Connect a cleanup model")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Paste API key")).toBeInTheDocument();
  });
});

describe("Enhancement — Stream-1 Mini local picker", () => {
  function catalog(overrides: Record<string, unknown> = {}) {
    return { ...MULTI_PRODUCTION_MODELS_RESPONSE, ...overrides };
  }

  async function openPicker() {
    await expandLocalConnection();
    const trigger = await screen.findByRole("button", { name: /Local model:/ });
    fireEvent.click(trigger);
    return { trigger, panel: await screen.findByRole("listbox", { name: "Local model" }) };
  }

  it("renders a one-line ready trigger with no badge, description or helper line", async () => {
    globalThis.fetch = makeFetchMock(200, catalog({ installed_ids: ["stream-1-mini"] }));
    renderEnhancement({
      enhancement_local_model: "stream-1-mini",
      enhancement_local_gpu_verdicts: { "stream-1-mini": "viable" },
    });
    await expandLocalConnection();
    const trigger = await screen.findByRole("button", { name: "Local model: Stream-1 Mini" });
    expect(trigger).toHaveTextContent(/^Stream-1 Mini$/);
    expect(trigger.querySelector('[data-model-chip="mark"]')).not.toBeNull();
    const modelBlock = screen.getByText("Model").parentElement!;
    expect(within(modelBlock).queryByRole("button", { name: /Download to use|Check again/ })).toBeNull();
    expect(screen.queryByText("Run the speed check again")).toBeNull();
  });

  it("explains each model only in the open panel, with Recommended on Stream-1 Mini in the list", async () => {
    globalThis.fetch = makeFetchMock(200, catalog({ installed_ids: ["stream-1-mini"] }));
    renderEnhancement({
      enhancement_local_model: "stream-1-mini",
      enhancement_local_gpu_verdicts: { "stream-1-mini": "viable" },
    });
    const { trigger, panel } = await openPicker();
    expect(trigger).not.toHaveTextContent("Recommended");

    const [mini, custom] = within(panel).getAllByRole("option");
    expect(mini).toHaveTextContent("Stream-1 MiniRecommended");
    expect(mini).toHaveTextContent("Fast enhancement for Chat and Email");
    expect(custom).toHaveAccessibleName(/^Customizable\s*Write your own prompts\. Built on Gemma 4\.\s*2\.9 GB$/);
    expect(custom).not.toHaveTextContent("Recommended");
    expect(custom.querySelector('[data-model-chip="pen"]')).not.toBeNull();
    // A missing model shows its size as the only trailing meta and keeps its
    // download action visible at rest.
    expect(custom).toHaveTextContent("2.9 GB");
    expect(within(panel).getByRole("button", { name: "Download Customizable, 2.9 GB" })).toBeInTheDocument();
    // The Gemma identity rides on a hover and on the row description.
    expect(within(custom).getByText("Gemma 4")).toHaveAttribute("data-description-hover");
    expect(custom).toHaveAccessibleDescription(
      "Not downloaded, 2.9 GB. Powered by Google's Gemma 4 E2B Instruct (4-bit). Write your own cleanup prompts. About 3.1 GB, runs entirely on your computer.",
    );
    expect(mini).toHaveAccessibleDescription("Downloaded");
  });

  it("lets Tab move from a row into its action without closing the panel", async () => {
    globalThis.fetch = makeFetchMock(200, catalog({ installed_ids: ["stream-1-mini", "gemma4-e2b-instruct"] }));
    renderEnhancement({
      enhancement_local_model: "stream-1-mini",
      enhancement_local_gpu_verdicts: { "stream-1-mini": "viable" },
    });
    const { panel } = await openPicker();
    const mini = within(panel).getByRole("option", { name: /Stream-1 Mini/ });
    await waitFor(() => expect(mini).toHaveFocus());
    fireEvent.keyDown(mini, { key: "Tab" });
    expect(screen.getByRole("listbox", { name: "Local model" })).toBe(panel);

    const tabbable: HTMLElement[] = Array.from(panel.querySelectorAll<HTMLButtonElement>("button:not([disabled])"))
      .filter((button) => button.tabIndex !== -1);
    const trash = within(panel).getByRole("button", { name: "Delete Stream-1 Mini" });
    expect(tabbable.indexOf(trash)).toBe(tabbable.indexOf(mini) + 1);
    const custom = within(panel).getByRole("option", { name: /Customizable/ });
    expect(tabbable.indexOf(within(panel).getByRole("button", { name: "Delete Customizable" })))
      .toBe(tabbable.indexOf(custom) + 1);
  });

  it("keeps a too-slow installed model disabled but deletable", async () => {
    globalThis.fetch = makeFetchMock(200, catalog({ installed_ids: ["stream-1-mini", "gemma4-e2b-instruct"] }));
    renderEnhancement({
      enhancement_local_model: "stream-1-mini",
      enhancement_local_gpu_verdicts: { "stream-1-mini": "viable", "gemma4-e2b-instruct": "unviable" },
    });
    const { panel } = await openPicker();
    const custom = within(panel).getByRole("option", { name: /Customizable/ });
    expect(custom).toBeDisabled();
    expect(custom).toHaveTextContent("Too slow on this computer");
    expect(within(panel).getByRole("button", { name: "Delete Customizable" })).toBeInTheDocument();
  });

  it("shows the slow dot on the trigger and offers Check again only for a slow result", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/enhancement/benchmark/start")) {
        return Promise.resolve(jsonResponse(200, { status: "started" }));
      }
      return Promise.resolve(jsonResponse(200, catalog({ installed_ids: ["stream-1-mini"] })));
    });
    globalThis.fetch = fetchMock;
    renderEnhancement({
      enhancement_local_model: "stream-1-mini",
      enhancement_local_gpu_verdicts: { "stream-1-mini": "marginal" },
    });
    await expandLocalConnection();
    const trigger = await screen.findByRole("button", { name: /Local model:/ });
    expect(within(trigger).getByText("Runs slowly here")).toHaveClass("sr-only");
    expect(screen.getByText("Runs slowly on this computer.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        String(url).includes("/api/enhancement/benchmark/start"),
      );
      expect(JSON.parse(String(call![1]?.body))).toEqual({ mode: "manual", model_id: "stream-1-mini" });
    });
  });

  it("Change model in the profile editor closes it and opens the local picker without switching", async () => {
    const onConfigChange = vi.fn();
    globalThis.fetch = makeFetchMock(200, catalog({ installed_ids: ["stream-1-mini"] }));
    renderEnhancement({
      enhancement_enabled: true,
      enhancement_local_model: "stream-1-mini",
      enhancement_local_gpu_verdicts: { "stream-1-mini": "viable" },
      profiles: [
        { id: "chat", name: "Chat", prompt: "Chat prompt" },
        { id: "slack", name: "Slack replies", prompt: "Keep it casual." },
      ],
    }, { onConfigChange });
    await screen.findByRole("button", { name: /Local/ });
    // Wait for the catalog so the editor sees Stream-1 Mini's fixed prompt.
    await waitFor(() => expect(
      (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length,
    ).toBeGreaterThan(0));
    fireEvent.click(await screen.findByRole("button", { name: "Edit Slack replies" }));
    const editor = await screen.findByRole("dialog", { name: "Edit Slack replies" });
    expect(await within(editor).findByRole("textbox", { name: "Slack replies cleanup prompt" }))
      .toHaveTextContent("Keep it casual.");
    fireEvent.click(within(editor).getByRole("button", { name: "Change model" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit Slack replies" })).toBeNull());
    const panel = await screen.findByRole("listbox", { name: "Local model" });
    expect(within(panel).getAllByRole("option")).toHaveLength(2);
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ enhancement_local_model: expect.anything() }),
    );
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.some(([url]) =>
      String(url).includes("local-download"),
    )).toBe(false);
  });

  it("shows download progress on the trigger and the helper line", async () => {
    const handlers = new Set<(event: AppEvent) => void>();
    const harness = {
      subscribe(next: (event: AppEvent) => void) {
        handlers.add(next);
        return () => { handlers.delete(next); };
      },
      emit(event: AppEvent) {
        act(() => handlers.forEach((handler) => handler(event)));
      },
    };
    globalThis.fetch = makeFetchMock(200, catalog({ installed_ids: ["stream-1-mini"] }));
    renderEnhancement(
      { enhancement_local_model: "gemma4-e2b-instruct" },
      { subscribe: harness.subscribe },
    );
    await expandLocalConnection();
    const trigger = await screen.findByRole("button", { name: /Local model: Customizable/ });
    fireEvent.click(await screen.findByRole("button", { name: "Download to use · 2.9 GB" }));
    await waitFor(() => expect(within(screen.getByText("Model").parentElement!).getByText("Downloading…")).toBeInTheDocument());
    harness.emit({
      type: "download.progress",
      kind: "local_llm",
      downloaded: 1126 * 1024 * 1024,
      total: 2962 * 1024 * 1024,
      speed_bps: 1,
    });
    expect(trigger).toHaveTextContent("38%");
    expect(trigger.querySelector("[data-local-model-progress]")).toHaveStyle({ width: "38%" });
    expect(screen.getByText("Downloading · 1.1 GB of 2.9 GB")).toBeInTheDocument();
  });
});

describe("Enhancement — Connection block (issue 1)", () => {
  beforeEach(() => {
    globalThis.fetch = makeFetchMock(200, LOCAL_MODELS_RESPONSE);
  });

  it("connected cloud provider collapses to a summary row (key field hidden until expanded)", async () => {
    // gemini WITH a stored key + acknowledged = "connected" -> the block
    // collapses to a one-line summary and the API key field is NOT mounted.
    renderEnhancement(
      { enhancement_provider: "gemini", enhancement_gemini_api_key: "stored-key" },
      { acknowledgedProviders: ["gemini"] },
    );

    // Summary row present; controls collapsed. The connection STATUS now lives
    // in the top-right heading pill, so the collapsed summary row carries only
    // the provider name (+ model for cloud), not "· Connected".
    const summary = await screen.findByRole("button", { name: /Gemini/, expanded: false });
    expect(summary).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/· Connected/)).toBeNull();
    expect(screen.queryByPlaceholderText("Paste API key")).toBeNull();

    // Clicking the summary expands the controls inline. A provider WITH a stored
    // key renders the saved-key chip (masked fingerprint + "Replace API key"),
    // not a bare input, so assert the chip's replace affordance appears.
    fireEvent.click(summary);
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Replace API key" }),
      ).toBeInTheDocument();
    });
  });

  it("local provider collapses to a provider-name summary (no API key field, no model name)", async () => {
    // Local needs no key: the summary reads just "Local" (status is in the
    // heading pill, and the local model name is never exposed). There's never
    // an API key field. Provider defaults to local in makeConfig().
    renderEnhancement();
    const summary = await screen.findByRole("button", { name: /Local/, expanded: false });
    expect(summary).toHaveAttribute("aria-expanded", "false");
    // No "· Ready" status text and no leaked local model name in the summary.
    expect(screen.queryByText(/· Ready/)).toBeNull();
    expect(screen.queryByText(/Gemma/)).toBeNull();
    expect(screen.queryByPlaceholderText("Paste API key")).toBeNull();
  });

  it("enabling AI Enhancement while unconfigured opens the setup card (no silent error)", async () => {
    // gemini WITH a key but NOT acknowledged would normally route to the
    // privacy modal; gemini with NO key is unconfigured and already shows the
    // setup card. Use an acknowledged gemini with a key removed mid-flight:
    // here we assert the unconfigured path (no key) keeps the setup card open
    // and the toggle write never fires the old "Add API key first" error.
    const onConfigChange = vi.fn();
    renderEnhancement(
      { enhancement_provider: "gemini" },
      { onConfigChange },
    );

    // Setup card is already expanded (unconfigured). Flip the master toggle on.
    const toggle = screen.getByRole("switch");
    fireEvent.click(toggle);

    // No enhancement_enabled write (routed into setup, not enabled), and the
    // setup card stays visible. The old error pill copy must NOT appear.
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ enhancement_enabled: true }),
    );
    expect(screen.getByText("Connect a cleanup model")).toBeInTheDocument();
    expect(screen.queryByText("Add API key first")).toBeNull();
  });
});

describe("Enhancement - persisted Ollama connection checks", () => {
  const ollamaConfig = {
    enhancement_enabled: true,
    enhancement_provider: "ollama",
    enhancement_ollama_endpoint: "http://localhost:11434",
    enhancement_ollama_model: "llama3.2",
  } satisfies Partial<AppConfig>;

  beforeEach(() => {
    globalThis.fetch = makeFetchMock(200, {
      ...LOCAL_MODELS_RESPONSE,
      connected: true,
      message: "Connected",
    });
  });

  it("discloses onward Ollama processing even for a loopback server", async () => {
    renderEnhancement(ollamaConfig);

    const summary = await screen.findByRole("button", {
      name: /Ollama.*Server on this device; may use cloud models/,
      expanded: false,
    });
    expect(
      summary.querySelector('[data-provider-icon="remote"]'),
    ).toBeInTheDocument();
    expect(
      within(summary).getByRole("tooltip", {
        name: "Server on this device; may use cloud models",
      }),
    ).toBeInTheDocument();
  });

  it("uses remote Ollama copy, a cloud glyph, and the configured host in accessible text", async () => {
    renderEnhancement({
      ...ollamaConfig,
      enhancement_ollama_endpoint: "http://workstation.local:11434",
    });

    const summary = await screen.findByRole("button", {
      name: /Ollama.*Sent to workstation\.local for cleanup\./,
      expanded: false,
    });
    expect(
      summary.querySelector('[data-provider-icon="remote"]'),
    ).toBeInTheDocument();
    expect(
      within(summary).getByRole("tooltip", {
        name: "Sent to workstation.local for cleanup.",
      }),
    ).toBeInTheDocument();
  });

  it("waits for the save signal, shows loading, and connects without a remount", async () => {
    const modelLoad = deferred<ReturnType<typeof jsonResponse>>();
    let checkCount = 0;
    globalThis.fetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/enhancement/check")) {
        checkCount += 1;
        if (checkCount === 1) {
          return Promise.resolve(jsonResponse(200, {
            connected: true,
            message: "Connected",
          }));
        }
        return modelLoad.promise;
      }
      return Promise.resolve(jsonResponse(200, LOCAL_MODELS_RESPONSE));
    });

    const view = renderEnhancement(ollamaConfig);
    await screen.findByRole("status", { name: "Cloud provider connected" });

    const changedModel = {
      ...ollamaConfig,
      enhancement_ollama_model: "qwen3:8b",
    };
    view.rerender(enhancementElement(changedModel, { configSaveRevision: 0 }));

    expect(checkCount).toBe(1);
    expect(screen.queryByText("Loading model...")).toBeNull();

    view.rerender(enhancementElement(changedModel, { configSaveRevision: 1 }));

    expect(await screen.findByText("Loading model...")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Ollama model loading" })).toBeInTheDocument();
    expect(screen.queryByText("Enhancement is disabled")).toBeNull();
    expect(checkCount).toBe(2);

    await act(async () => {
      modelLoad.resolve(jsonResponse(200, {
        connected: true,
        message: "Connected",
      }));
    });

    expect(
      await screen.findByRole("status", { name: "Cloud provider connected" }),
    ).toHaveTextContent("Connected");
  });

  it("rechecks a persisted endpoint change", async () => {
    let checkCount = 0;
    globalThis.fetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/enhancement/check")) {
        checkCount += 1;
        return Promise.resolve(jsonResponse(200, {
          connected: true,
          message: "Connected",
        }));
      }
      return Promise.resolve(jsonResponse(200, LOCAL_MODELS_RESPONSE));
    });

    const view = renderEnhancement(ollamaConfig);
    await waitFor(() => expect(checkCount).toBe(1));

    const changedEndpoint = {
      ...ollamaConfig,
      enhancement_ollama_endpoint: "http://workstation.local:11434",
    };
    view.rerender(enhancementElement(changedEndpoint, { configSaveRevision: 0 }));
    expect(checkCount).toBe(1);

    view.rerender(enhancementElement(changedEndpoint, { configSaveRevision: 1 }));
    await waitFor(() => expect(checkCount).toBe(2));
  });

  it("surfaces a matching Ollama probe failure after loading", async () => {
    const modelLoad = deferred<ReturnType<typeof jsonResponse>>();
    globalThis.fetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/enhancement/check")) return modelLoad.promise;
      return Promise.resolve(jsonResponse(200, LOCAL_MODELS_RESPONSE));
    });

    renderEnhancement(ollamaConfig);
    expect(await screen.findByText("Loading model...")).toBeInTheDocument();

    await act(async () => {
      modelLoad.resolve(jsonResponse(200, {
        connected: false,
        message: "Ollama is unavailable",
      }));
    });

    expect(
      await screen.findByRole("status", { name: "Error: Could not connect. Check your connection settings and try again." }),
    ).toBeInTheDocument();
  });

  it("ignores a superseded model probe that finishes last", async () => {
    const oldModelLoad = deferred<ReturnType<typeof jsonResponse>>();
    const newModelLoad = deferred<ReturnType<typeof jsonResponse>>();
    let checkCount = 0;
    globalThis.fetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/enhancement/check")) {
        checkCount += 1;
        return checkCount === 1 ? oldModelLoad.promise : newModelLoad.promise;
      }
      return Promise.resolve(jsonResponse(200, LOCAL_MODELS_RESPONSE));
    });

    const view = renderEnhancement(ollamaConfig);
    expect(await screen.findByText("Loading model...")).toBeInTheDocument();

    view.rerender(enhancementElement(
      { ...ollamaConfig, enhancement_ollama_model: "qwen3:8b" },
      { configSaveRevision: 1 },
    ));
    await waitFor(() => expect(checkCount).toBe(2));

    await act(async () => {
      newModelLoad.resolve(jsonResponse(200, {
        connected: true,
        message: "Connected",
      }));
    });
    expect(
      await screen.findByRole("status", { name: "Cloud provider connected" }),
    ).toHaveTextContent("Connected");

    await act(async () => {
      oldModelLoad.resolve(jsonResponse(200, {
        connected: false,
        message: "Enhancement is disabled",
      }));
    });

    await waitFor(() => {
      expect(
        screen.getByRole("status", { name: "Cloud provider connected" }),
      ).toHaveTextContent("Connected");
      expect(screen.queryByText("Enhancement is disabled")).toBeNull();
    });
  });
});

describe("Enhancement - development candidate model selector", () => {
  beforeEach(() => {
    globalThis.fetch = makeCandidateFetchMock();
  });

  async function openModelOptions() {
    const localButtons = await screen.findAllByRole("button", { name: /Local/ });
    const summary = localButtons.find(
      (button) => button.hasAttribute("aria-expanded") && !button.hasAttribute("aria-haspopup"),
    );
    if (!summary) throw new Error("Local connection summary not found");
    if (summary.getAttribute("aria-expanded") === "false") {
      fireEvent.click(summary);
    }
    const trigger = await screen.findByRole("button", { name: /Local model:/ });
    fireEvent.click(trigger);
    return trigger;
  }

  it("shows every candidate with exact metadata while hiding unpublished R2 rows", async () => {
    renderEnhancement();
    await openModelOptions();

    expect(screen.queryByText("Stream-1")).toBeNull();
    expect(screen.getByText("Development candidates")).toBeInTheDocument();

    const expectedCandidates = [
      ["b1emoji_r1-s1", "350M · Ship pick · Incumbent"],
      ["w1wrap350m_r2-s2", "350M · Review candidate · General-Lite contender"],
      ["w5wrap_email-s3", "350M · Review candidate · Email-specific option"],
      ["w6wrap_oclean-s1", "350M · Review candidate · Obey-clean contender"],
      ["w10wrap_doless-s1", "350M · Review candidate · Does-less option"],
      ["w2wrap1p2b-s3", "1.2B · Review candidate · H2H/mic-validated incumbent"],
      ["w8wrap_closure-s1", "1.2B · Review candidate · Best closure r1 artifact"],
    ];
    for (const [id, metadata] of expectedCandidates) {
      expect(screen.getByText(id)).toHaveTextContent(id);
      expect(screen.getByText(metadata)).toBeInTheDocument();
    }

    expect(screen.getAllByText("Artifact available")).toHaveLength(2);
    expect(screen.getAllByText("Artifact unavailable")).toHaveLength(5);
    expect(screen.getByText("Active model")).toBeInTheDocument();

    const missing = screen.getByRole("option", {
      name: /w8wrap_closure-s1.*Artifact unavailable/,
    });
    expect(missing).toBeDisabled();
    expect(missing).toHaveAttribute("aria-disabled", "true");
  });

  it("activates an available candidate before updating the visible config state", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({}, { onConfigChange });
    await openModelOptions();

    fireEvent.click(screen.getByRole("option", { name: /w2wrap1p2b-s3/ }));

    await waitFor(() => {
      const activationCall = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.find(
        ([url]) => String(url).includes("/api/enhancement/local-activate"),
      );
      expect(activationCall).toBeTruthy();
      expect(JSON.parse(String(activationCall![1]?.body))).toEqual({
        model_id: "w2wrap1p2b-s3",
      });
      expect(onConfigChange).toHaveBeenCalledWith({
        enhancement_local_model: "w2wrap1p2b-s3",
      });
      expect(activationCall![0]).toBeDefined();
      expect(
        (globalThis.fetch as ReturnType<typeof vi.fn>).mock.invocationCallOrder[
          (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.indexOf(activationCall!)
        ],
      ).toBeLessThan(onConfigChange.mock.invocationCallOrder[0]);
    });

    await openModelOptions();
    expect(
      screen.getByRole("option", { name: /w2wrap1p2b-s3.*Active model/ }),
    ).toBeInTheDocument();
  });

  it("guards unavailable candidates and never offers candidate download or delete actions", async () => {
    renderEnhancement();
    await openModelOptions();

    const missing = screen.getByRole("option", { name: /w1wrap350m_r2-s2/ });
    fireEvent.click(missing);

    expect(
      (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.some(
        ([url]) => String(url).includes("/api/enhancement/local-activate"),
      ),
    ).toBe(false);
    expect(screen.queryByRole("button", { name: /Download b1emoji_r1-s1/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Download w1wrap350m_r2-s2/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Delete / })).toBeNull();
  });

  it("does not offer the managed speed recheck for a development candidate", async () => {
    renderEnhancement({
      enhancement_local_model: "b1emoji_r1-s1",
      enhancement_local_gpu_verdicts: { "b1emoji_r1-s1": "marginal" },
    });
    const summary = await screen.findByRole("button", { name: /Local/ });
    fireEvent.click(summary);

    expect(
      await screen.findByText("Development candidate · Artifact available"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check again" })).toBeNull();
  });

  it("uses the locked selected-candidate detail copy for present and missing artifacts", async () => {
    const availableRender = renderEnhancement({
      enhancement_local_model: "b1emoji_r1-s1",
    });
    const availableSummary = await screen.findByRole("button", { name: /Local/ });
    fireEvent.click(availableSummary);
    expect(
      await screen.findByText("Development candidate · Artifact available"),
    ).toBeInTheDocument();

    availableRender.unmount();
    renderEnhancement({ enhancement_local_model: "w1wrap350m_r2-s2" });
    const missingSummary = await screen.findByRole("button", { name: /Local/ });
    fireEvent.click(missingSummary);
    expect(
      await screen.findByText("Artifact unavailable in the candidate model directory."),
    ).toBeInTheDocument();
  });
});

// NOTE: toggle-guard tests for the local provider (modal-skip when installed,
// "Download model first" error when missing) were removed with the Local model
// section in Task 2.4 of ai-enhance-tweaks. Task 2.5 (toggle-on confirm dialog)
// replaces this behavior and is responsible for re-adding coverage of the
// confirm-then-download flow from the toggle entry point.

// NOTE: the Advanced disclosure describe block was removed in unship-local
// Task 2.1 — the disclosure shell is gone and all its tests are obsolete.

// NOTE: rendering tests for the local-model status row were removed alongside
// the standalone "Local model" section in Task 2.4 of ai-enhance-tweaks.
// Catalog-resolution and install-state behavior is now expressed through the
// provider dropdown (Phase 2 work) and the new toggle-on confirm flow (Task 2.5).

describe("Enhancement — picking a cloud provider chains DataLeavesDialog -> PrivacyModal", () => {
  beforeEach(() => {
    globalThis.fetch = makeFetchMock(200, LOCAL_MODELS_RESPONSE);
  });

  it("clicking gemini in the provider dropdown opens DataLeavesDialog (NOT PrivacyModal directly)", async () => {
    const onConfigChange = vi.fn();
    const onRequestPrivacyModal = vi.fn();

    renderEnhancement(
      {
        enhancement_provider: "anthropic",
        enhancement_api_key: "test-key",
      },
      { onConfigChange, onRequestPrivacyModal, acknowledgedProviders: ["anthropic"] },
    );

    const trigger = await screen.findByRole("button", { name: /Anthropic/ });
    fireEvent.click(trigger);

    const geminiOption = await screen.findByRole("button", { name: /^Gemini\./ });
    fireEvent.click(geminiOption);

    // DataLeavesDialog now fires BEFORE PrivacyModal.
    await waitFor(() => {
      expect(screen.getByText("Review where your text goes")).toBeInTheDocument();
    });
    // PrivacyModal must NOT be requested yet — Dialog B precedes it.
    expect(onRequestPrivacyModal).not.toHaveBeenCalled();
    // Config is NOT written: the chain fires BEFORE any config write so
    // Cancel leaves the dropdown on the previous selection.
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ enhancement_provider: "gemini" }),
    );
  });

  it("DataLeavesDialog also fires for Ollama (endpoint may be remote)", async () => {
    const onConfigChange = vi.fn();
    const onRequestPrivacyModal = vi.fn();

    renderEnhancement(
      {
        enhancement_provider: "anthropic",
        enhancement_api_key: "test-key",
      },
      { onConfigChange, onRequestPrivacyModal, acknowledgedProviders: ["anthropic"] },
    );

    const trigger = await screen.findByRole("button", { name: /Anthropic/ });
    fireEvent.click(trigger);
    const ollamaOption = await screen.findByRole("button", { name: /^Ollama\./ });
    fireEvent.click(ollamaOption);

    await waitFor(() => {
      expect(screen.getByText("Review where your text goes")).toBeInTheDocument();
      expect(screen.getByText("Going to Ollama")).toBeInTheDocument();
    });
    expect(onRequestPrivacyModal).not.toHaveBeenCalled();
  });

  it("DataLeavesDialog Continue chains into PrivacyModal", async () => {
    const onRequestPrivacyModal = vi.fn();

    renderEnhancement(
      {
        enhancement_provider: "anthropic",
        enhancement_api_key: "test-key",
      },
      { onRequestPrivacyModal, acknowledgedProviders: ["anthropic"] },
    );

    const trigger = await screen.findByRole("button", { name: /Anthropic/ });
    fireEvent.click(trigger);
    const geminiOption = await screen.findByRole("button", { name: /^Gemini\./ });
    fireEvent.click(geminiOption);

    await waitFor(() => {
      expect(screen.getByText("Review where your text goes")).toBeInTheDocument();
    });

    // Click Continue inside DataLeavesDialog
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(onRequestPrivacyModal).toHaveBeenCalledWith("gemini");
  });

  it("DataLeavesDialog Cancel reverts dropdown (no config write, no PrivacyModal)", async () => {
    const onConfigChange = vi.fn();
    const onRequestPrivacyModal = vi.fn();

    renderEnhancement(
      {
        enhancement_provider: "anthropic",
        enhancement_api_key: "test-key",
      },
      { onConfigChange, onRequestPrivacyModal, acknowledgedProviders: ["anthropic"] },
    );

    const trigger = await screen.findByRole("button", { name: /Anthropic/ });
    fireEvent.click(trigger);
    const geminiOption = await screen.findByRole("button", { name: /^Gemini\./ });
    fireEvent.click(geminiOption);

    await waitFor(() => {
      expect(screen.getByText("Review where your text goes")).toBeInTheDocument();
    });

    // Click Cancel inside DataLeavesDialog
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    // Dialog closes, no privacy modal, no config write for the candidate.
    await waitFor(() => {
      expect(screen.queryByText("Review where your text goes")).toBeNull();
    });
    expect(onRequestPrivacyModal).not.toHaveBeenCalled();
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ enhancement_provider: "gemini" }),
    );
  });

  it("provider-only history cannot skip recipient privacy review", async () => {
    const onConfigChange = vi.fn();
    const onRequestPrivacyModal = vi.fn();

    renderEnhancement(
      {
        enhancement_provider: "anthropic",
        enhancement_api_key: "test-key",
      },
      {
        onConfigChange,
        onRequestPrivacyModal,
        acknowledgedProviders: ["anthropic", "gemini"],
      },
    );

    const trigger = await screen.findByRole("button", { name: /Anthropic/ });
    fireEvent.click(trigger);

    const geminiOption = await screen.findByRole("button", { name: /^Gemini\./ });
    fireEvent.click(geminiOption);

    // Historical provider names do not prove permission for this recipient/model.
    expect(onRequestPrivacyModal).not.toHaveBeenCalled();
    expect(screen.getByText("Review where your text goes")).toBeInTheDocument();
    expect(onConfigChange).not.toHaveBeenCalledWith({ enhancement_provider: "gemini" });
  });
});

// NOTE: Low-RAM dialog tests were removed because they drove the flow through
// the row's Download button, which no longer exists. Task 2.5 (toggle-on
// confirm + auto-enable) re-enters requestLocalDownload from the new confirm
// dialog and is the right place to re-add LowRamDialog coverage.

describe("Enhancement — collapsed summary model label (round 02 issue 3)", () => {
  // The curated Gemini catalog carries one structured recommendation.
  const GEMINI_MODELS_RESPONSE = {
    provider: "gemini",
    bundled: [
      {
        id: "gemini-3.5-flash-lite",
        label: "Gemini 3.5 Flash-Lite",
        recommended: true,
      },
      { id: "gemini-3.7-flash", label: "Gemini 3.7 Flash" },
    ],
  };

  function makeGeminiFetchMock(): ReturnType<typeof vi.fn> {
    return vi.fn().mockImplementation((url: string) => {
      if (typeof url === "string" && url.includes("/api/enhancement/models")) {
        return Promise.resolve(jsonResponse(200, GEMINI_MODELS_RESPONSE));
      }
      if (typeof url === "string" && url.includes("/api/apps/running")) {
        return Promise.resolve(jsonResponse(200, RUNNING_APPS_RESPONSE));
      }
      return Promise.resolve(jsonResponse(200, LOCAL_MODELS_RESPONSE));
    });
  }

  beforeEach(() => {
    globalThis.fetch = makeGeminiFetchMock();
  });

  it("shows the clean model name in the collapsed summary", async () => {
    // Connected gemini collapses to a summary; the model label reads the clean
    // model name and never leaks a status chip word into the summary row.
    renderEnhancement(
      {
        enhancement_provider: "gemini",
        enhancement_gemini_api_key: "stored-key",
        enhancement_gemini_model: "gemini-3.5-flash-lite",
      },
      { acknowledgedProviders: ["gemini"] },
    );

    await waitFor(() => {
      expect(screen.getByText("Gemini 3.5 Flash-Lite")).toBeInTheDocument();
    });
  });

  it("preserves an unlisted stored model through the custom model path", async () => {
    renderEnhancement(
      {
        enhancement_provider: "gemini",
        enhancement_gemini_api_key: "stored-key",
        enhancement_gemini_model: "gemini-3.1-pro-preview",
      },
      { acknowledgedProviders: ["gemini"] },
    );

    const summary = await screen.findByRole("button", { name: /Gemini/, expanded: false });
    fireEvent.click(summary);
    expect(await screen.findByRole("button", { name: "Model: gemini-3.1-pro-preview" })).toBeInTheDocument();
  });

  it("renders only the Recommended chip in the expanded model picker", async () => {
    renderEnhancement(
      {
        enhancement_provider: "gemini",
        enhancement_gemini_api_key: "stored-key",
        enhancement_gemini_model: "gemini-3.5-flash-lite",
      },
      { acknowledgedProviders: ["gemini"] },
    );

    const summary = await screen.findByRole("button", { name: /Gemini/, expanded: false });
    fireEvent.click(summary);

    await waitFor(() => {
      expect(screen.getByText("Recommended")).toBeInTheDocument();
    });
    expect(screen.queryByText("Free Tier")).toBeNull();
    expect(screen.queryByText("Show older models")).toBeNull();
  });
});

describe("Enhancement — profile rows + editor (round 04)", () => {
  beforeEach(() => {
    globalThis.fetch = makeRoutingFetchMock();
  });

  it("enables Mac app and website rules while hiding only unsupported raw-path Browse", async () => {
    renderEnhancement(
      { default_profile_id: "chat" },
      { advancedCapabilities: MAC_ADVANCED_CAPABILITIES },
    );

    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));
    const editorDialog = await screen.findByRole("dialog", { name: "Edit Code" });
    const add = within(editorDialog).getByRole("button", { name: "Add" });
    expect(add).toBeEnabled();
    fireEvent.click(add);

    const addDialog = await screen.findByRole("dialog", { name: "Add a rule" });
    expect(within(addDialog).getByRole("radio", { name: "App" })).toBeEnabled();
    expect(within(addDialog).getByRole("radio", { name: "Website" })).toBeEnabled();
    expect(within(addDialog).queryByRole("button", { name: /Browse/ })).toBeNull();
  });

  it("disables adding Power Mode rules when both authoritative identities are unsupported", async () => {
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    renderEnhancement(
      { default_profile_id: "chat" },
      { advancedCapabilities: UNSUPPORTED_ADVANCED_CAPABILITIES },
    );

    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));
    const editorDialog = await screen.findByRole("dialog", { name: "Edit Code" });
    expect(within(editorDialog).getByRole("button", { name: "Add" })).toBeDisabled();
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("/api/apps/")),
    ).toBe(false);
  });

  it("derives Browse visibility from its entry presentation, not the report platform string", async () => {
    renderEnhancement(
      { default_profile_id: "chat" },
      {
        advancedCapabilities: {
          ...ADVANCED_CAPABILITIES,
          platform: "Darwin",
        },
      },
    );

    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));
    const editorDialog = await screen.findByRole("dialog", { name: "Edit Code" });
    fireEvent.click(within(editorDialog).getByRole("button", { name: "Add" }));
    const addDialog = await screen.findByRole("dialog", { name: "Add a rule" });
    expect(within(addDialog).getByRole("button", { name: /Browse/ })).toBeInTheDocument();
  });

  it("fetches app inventories only while Add App is open", async () => {
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    renderEnhancement({ default_profile_id: "chat" });

    await screen.findByRole("button", { name: "Edit Code" });
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("/api/apps/running")),
    ).toBe(false);
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("/api/apps/installed")),
    ).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Edit Code" }));
    const editorDialog = await screen.findByRole("dialog", { name: "Edit Code" });
    fireEvent.click(within(editorDialog).getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(([url]) =>
          String(url).includes("/api/apps/running"),
        ),
      ).toHaveLength(1);
      expect(
        fetchMock.mock.calls.filter(([url]) =>
          String(url).includes("/api/apps/installed"),
        ),
      ).toHaveLength(1);
    });
  });

  it("clears picker inventory when Add App closes", async () => {
    const secondRunning = deferred<ReturnType<typeof jsonResponse>>();
    const secondInstalled = deferred<ReturnType<typeof jsonResponse>>();
    const routingFetch = makeRoutingFetchMock();
    let runningCalls = 0;
    let installedCalls = 0;
    globalThis.fetch = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (url.includes("/api/apps/running")) {
        runningCalls += 1;
        if (runningCalls === 2) return secondRunning.promise;
      }
      if (url.includes("/api/apps/installed")) {
        installedCalls += 1;
        if (installedCalls === 2) return secondInstalled.promise;
      }
      return routingFetch(url, init);
    });

    renderEnhancement({ default_profile_id: "chat" });
    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));
    const editorDialog = await screen.findByRole("dialog", { name: "Edit Code" });
    fireEvent.click(within(editorDialog).getByRole("button", { name: "Add" }));
    let addDialog = await screen.findByRole("dialog", { name: "Add a rule" });
    expect(await within(addDialog).findByText("Slack")).toBeInTheDocument();

    fireEvent.click(within(addDialog).getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Add a rule" })).toBeNull();
    });

    fireEvent.click(within(editorDialog).getByRole("button", { name: "Add" }));
    addDialog = await screen.findByRole("dialog", { name: "Add a rule" });
    expect(within(addDialog).queryByText("Slack")).toBeNull();
    expect(runningCalls).toBe(2);
    expect(installedCalls).toBe(2);
  });

  it("deletes legacy persistent Power identity caches at runtime", async () => {
    localStorage.setItem("ws.powermode.resolvedApps.v1", '{"slack.exe":{}}');
    localStorage.setItem("ws.powermode.siteIcons.v1", '{"example.com":"data:"}');

    renderEnhancement({ default_profile_id: "chat" });

    await waitFor(() => {
      expect(localStorage.getItem("ws.powermode.resolvedApps.v1")).toBeNull();
      expect(localStorage.getItem("ws.powermode.siteIcons.v1")).toBeNull();
    });
  });

  it("clicking anywhere on a profile row opens its editor (whole-row button)", async () => {
    // Issue 8: the whole row is one button (no radio, no pencil). Its
    // accessible name is "Edit <name>". Clicking it opens the editor dialog.
    renderEnhancement({ default_profile_id: "chat" });
    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));
    expect(await screen.findByRole("dialog", { name: "Edit Code" })).toBeInTheDocument();
  });

  it("rows have NO default radio (issue 5)", async () => {
    renderEnhancement({ default_profile_id: "chat" });
    await screen.findByRole("button", { name: "Edit Code" });
    expect(screen.queryByRole("radio")).toBeNull();
  });

  it("the editor's Make default button sets default_profile_id to the profile id", async () => {
    const onConfigChange = vi.fn();
    // Default starts at "chat"; open Code's editor (not default -> "Make default").
    renderEnhancement({ default_profile_id: "chat" }, { onConfigChange });
    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Code" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Make default" }));
    expect(onConfigChange).toHaveBeenCalledWith(
      expect.objectContaining({ default_profile_id: "code" }),
    );
  });

  it("the editor's Remove default button on the current default clears default_profile_id (no-default state)", async () => {
    const onConfigChange = vi.fn();
    // Default is "code"; opening Code's editor shows "Remove as default"; clicking
    // it writes default_profile_id="" (issue 13).
    renderEnhancement({ default_profile_id: "code" }, { onConfigChange });
    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Code" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove as default" }));
    expect(onConfigChange).toHaveBeenCalledWith(
      expect.objectContaining({ default_profile_id: "" }),
    );
  });

  it("the no-default caption renders only when default_profile_id is empty", async () => {
    renderEnhancement({ default_profile_id: "" });
    await screen.findByRole("button", { name: "Edit Code" });
    expect(
      screen.getByText("No default profile. Apps you haven't routed get raw text."),
    ).toBeInTheDocument();
  });

  it("the no-default caption is absent when a default is set", async () => {
    renderEnhancement({ default_profile_id: "chat" });
    await screen.findByRole("button", { name: "Edit Code" });
    expect(
      screen.queryByText("No default profile. Apps you haven't routed get raw text."),
    ).toBeNull();
  });

  it("editing a custom prompt persists it to the profile", async () => {
    const onConfigChange = vi.fn();
    // Add a custom profile so its prompt is editable (built-in chat/code are
    // read-only now). Default stays chat.
    renderEnhancement(
      {
        default_profile_id: "chat",
        profiles: [
          { id: "chat", name: "Chat", prompt: "Chat prompt" },
          { id: "code", name: "Code", prompt: "Code prompt" },
          { id: "custom1", name: "Email", prompt: "Email prompt" },
        ],
      },
      { onConfigChange },
    );

    fireEvent.click(await screen.findByRole("button", { name: "Edit Email" }));
    const textarea = await screen.findByLabelText("Email cleanup prompt");
    fireEvent.change(textarea, { target: { value: "New email prompt." } });

    await waitFor(() => {
      expect(onConfigChange).toHaveBeenCalledWith(
        expect.objectContaining({
          profiles: expect.arrayContaining([
            expect.objectContaining({ id: "custom1", prompt: "New email prompt." }),
          ]),
        }),
      );
    });
  });

  it("editing Email's sign-off persists it only on the Email profile", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement(
      {
        default_profile_id: "chat",
        profiles: [
          { id: "chat", name: "Chat", prompt: "Chat prompt" },
          { id: "code", name: "Code", prompt: "Code prompt" },
          { id: "email", name: "Email", prompt: "Email prompt" },
          {
            id: "custom1",
            name: "Custom",
            prompt: "Custom prompt",
            signature: "Stale custom sign-off",
          },
        ],
      },
      { onConfigChange },
    );

    fireEvent.click(await screen.findByRole("button", { name: "Edit Email" }));
    const signature = await screen.findByLabelText("Signature");
    fireEvent.change(signature, { target: { value: "Regards,\nAlex" } });

    expect(onConfigChange).toHaveBeenCalledWith(
      expect.objectContaining({
        profiles: [
          { id: "chat", name: "Chat", prompt: "Chat prompt" },
          { id: "code", name: "Code", prompt: "Code prompt" },
          {
            id: "email",
            name: "Email",
            prompt: "Email prompt",
            signature: "Regards,\nAlex",
          },
          {
            id: "custom1",
            name: "Custom",
            prompt: "Custom prompt",
            signature: "Stale custom sign-off",
          },
        ],
      }),
    );
  });

  it("a built-in profile's prompt is hidden in the editor", async () => {
    renderEnhancement({ default_profile_id: "chat" });
    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Code" });
    // Bundled prompt text is not rendered. Users reach editable prompt examples
    // by creating a custom profile and choosing a Template.
    expect(within(dialog).queryByText("Code prompt")).toBeNull();
    expect(within(dialog).queryByLabelText("Code cleanup prompt")).toBeNull();
    expect(
      within(dialog).getByText(/uses Whisperstream's built-in Code cleanup style/),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("Built-in")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Template" })).toBeNull();
  });

  it("assigning an app from the editor's Add modal writes power_mode_map to that profile", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({ default_profile_id: "chat" }, { onConfigChange });

    // Open the Code editor, click its plain "Add" button -> the AddAppModal
    // opens pre-targeted to Code. Pick Slack (selects it), then confirm with the
    // footer "Add" button.
    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));
    const editorDialog = await screen.findByRole("dialog", { name: "Edit Code" });
    fireEvent.click(within(editorDialog).getByRole("button", { name: "Add" }));

    const slack = await screen.findByText("Slack");
    fireEvent.click(slack);

    // The AddAppModal's footer Add button (a different dialog). With one rule
    // staged the footer reads "Add rule".
    const addDialog = await screen.findByRole("dialog", { name: "Add a rule" });
    fireEvent.click(within(addDialog).getByRole("button", { name: "Add rule" }));

    expect(onConfigChange).toHaveBeenCalledWith(
      expect.objectContaining({
        power_mode_map: expect.objectContaining({ "slack.exe": "code" }),
      }),
    );
  });

  it("picking two apps in the Add modal lands both in power_mode_map in ONE write", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({ default_profile_id: "chat" }, { onConfigChange });

    // Open the Code editor and its Add modal, stage Slack AND Discord (the
    // pending list accumulates), then commit with the footer button.
    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));
    const editorDialog = await screen.findByRole("dialog", { name: "Edit Code" });
    fireEvent.click(within(editorDialog).getByRole("button", { name: "Add" }));

    fireEvent.click(await screen.findByText("Slack"));
    fireEvent.click(await screen.findByText("Discord"));

    const addDialog = await screen.findByRole("dialog", { name: "Add a rule" });
    // The footer label carries the count once more than one rule is staged.
    fireEvent.click(within(addDialog).getByRole("button", { name: "Add 2 rules" }));

    // BOTH keys land in a single power_mode_map write (the host merges them; a
    // per-key assign loop would snapshot the map and keep only the last key).
    const call = onConfigChange.mock.calls.find(
      ([patch]) => patch && "power_mode_map" in patch,
    );
    expect(call).toBeTruthy();
    expect(call![0].power_mode_map).toMatchObject({
      "slack.exe": "code",
      "discord.exe": "code",
    });
  });

  it("removing an app chip deletes its power_mode_map entry", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement(
      { default_profile_id: "chat", power_mode_map: { "slack.exe": "code" } },
      { onConfigChange },
    );

    // Open Code (it owns slack.exe); remove the chip.
    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));

    const removeChip = await screen.findByRole("button", {
      name: /Remove .* from Code/,
    });
    fireEvent.click(removeChip);

    expect(onConfigChange).toHaveBeenCalledWith(
      expect.objectContaining({ power_mode_map: {} }),
    );
  });

  it("an app routed to another profile can be moved here and reassigns on commit", async () => {
    const onConfigChange = vi.fn();
    // Slack is routed to "chat"; open the "Code" editor and route it here.
    renderEnhancement(
      { default_profile_id: "chat", power_mode_map: { "slack.exe": "chat" } },
      { onConfigChange },
    );

    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));
    const editorDialog = await screen.findByRole("dialog", { name: "Edit Code" });
    fireEvent.click(within(editorDialog).getByRole("button", { name: "Add" }));

    const addDialog = await screen.findByRole("dialog", { name: "Add a rule" });
    // Slack appears in the picker even though it's routed to Chat (other-profile
    // apps are no longer hidden — only target-profile apps are). Scope to the
    // modal because the Chat profile row also shows a Slack chip.
    const slack = await within(addDialog).findByText("Slack");
    fireEvent.click(slack);

    // A move confirmation names the source profile; confirm with "Move here".
    expect(
      within(addDialog).getByText(/Move it to Code\?/),
    ).toBeInTheDocument();
    fireEvent.click(within(addDialog).getByRole("button", { name: "Move here" }));
    fireEvent.click(within(addDialog).getByRole("button", { name: "Add rule" }));

    // The commit overwrites slack.exe -> code (off chat; the old profile no
    // longer lists it).
    expect(onConfigChange).toHaveBeenCalledWith(
      expect.objectContaining({
        power_mode_map: expect.objectContaining({ "slack.exe": "code" }),
      }),
    );
    const call = onConfigChange.mock.calls.find(
      ([patch]) => patch && "power_mode_map" in patch,
    );
    expect(call![0].power_mode_map["slack.exe"]).toBe("code");
  });

  it("an app routed to the TARGET profile stays hidden in the Add modal", async () => {
    // Slack is already routed to Code; opening Code's Add modal must NOT list it.
    renderEnhancement({
      default_profile_id: "chat",
      power_mode_map: { "slack.exe": "code" },
    });

    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));
    const editorDialog = await screen.findByRole("dialog", { name: "Edit Code" });
    fireEvent.click(within(editorDialog).getByRole("button", { name: "Add" }));

    const addDialog = await screen.findByRole("dialog", { name: "Add a rule" });
    // Discord (routed nowhere) is listed; Slack (routed to the target) is not.
    expect(await within(addDialog).findByText("Discord")).toBeInTheDocument();
    expect(within(addDialog).queryByText("Slack")).toBeNull();
  });

  it("a site_map entry renders a globe (SiteGlyph) chip in the profile row stack", async () => {
    renderEnhancement({
      default_profile_id: "chat",
      site_map: { "github.com": "code" },
    });

    // Locate the Code row by its whole-row button and confirm it contains an
    // SVG (the SiteGlyph globe in the icon stack).
    const codeRow = await screen.findByRole("button", { name: "Edit Code" });
    expect(codeRow.querySelector("svg")).not.toBeNull();
  });

  it("the section pill reads 'Add profile' and the description uses the locked copy", async () => {
    renderEnhancement({ default_profile_id: "chat" });

    const addProfileBtn = await screen.findByRole("button", { name: "Add profile" });
    expect(addProfileBtn).toBeInTheDocument();
    // No '+ New profile' ghost link, no 'Add rule' section pill (issue 6).
    expect(screen.queryByRole("button", { name: "+ New profile" })).toBeNull();
    expect(screen.queryByRole("button", { name: "New profile" })).toBeNull();
    expect(
      screen.getByText("Each app or website dictates through its matching profile."),
    ).toBeInTheDocument();
  });

  it("removing a site chip via the editor deletes its site_map entry", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement(
      { default_profile_id: "chat", site_map: { "github.com": "code" } },
      { onConfigChange },
    );

    fireEvent.click(await screen.findByRole("button", { name: "Edit Code" }));

    const removeChip = await screen.findByRole("button", {
      name: /Remove github\.com from Code/,
    });
    fireEvent.click(removeChip);

    expect(onConfigChange).toHaveBeenCalledWith(
      expect.objectContaining({ site_map: {} }),
    );
  });
});

describe("Enhancement — draft profile flow (issue 7)", () => {
  beforeEach(() => {
    globalThis.fetch = makeRoutingFetchMock();
  });

  it("'+ Add profile' opens a draft editor without persisting a profile", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({ default_profile_id: "chat" }, { onConfigChange });

    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));

    // The draft editor opens (Create button present); no config write yet.
    expect(await screen.findByRole("button", { name: "Create" })).toBeInTheDocument();
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ profiles: expect.anything() }),
    );
  });

  it("cancelling a draft leaves profiles unchanged (no Untitled)", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({ default_profile_id: "chat" }, { onConfigChange });

    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));
    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    // Editor closed; no profile was ever written.
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "New profile" })).toBeNull();
    });
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ profiles: expect.anything() }),
    );
  });

  it("releases an uncommitted draft when the page unmounts", async () => {
    const onConfigChange = vi.fn();
    const view = renderEnhancement({}, { onConfigChange });
    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));
    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    fireEvent.change(within(dialog).getByLabelText("Profile Name"), {
      target: { value: "Uncommitted draft" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Make default" }));
    view.unmount();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ profiles: expect.anything() }),
    );
    renderEnhancement({}, { onConfigChange });
    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));
    const fresh = await screen.findByRole("dialog", { name: "New profile" });
    expect(within(fresh).getByLabelText("Profile Name")).toHaveValue("New profile");
    expect(within(fresh).getByRole("button", { name: "Make default" })).toBeInTheDocument();
  });

  it("Create persists the new profile in one config write", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({ default_profile_id: "chat" }, { onConfigChange });

    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));
    const dialog = await screen.findByRole("dialog", { name: "New profile" });

    const nameInput = within(dialog).getByLabelText("Profile Name");
    fireEvent.change(nameInput, { target: { value: "Email" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(onConfigChange).toHaveBeenCalledWith(
      expect.objectContaining({
        profiles: expect.arrayContaining([
          expect.objectContaining({ name: "Email" }),
        ]),
      }),
    );
  });

  it("a draft starts with a blank prompt (no Chat auto-seed)", async () => {
    renderEnhancement({ default_profile_id: "chat" });

    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));
    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    // The draft's prompt textarea is empty.
    const textarea = within(dialog).getByLabelText(/cleanup prompt/);
    expect((textarea as HTMLTextAreaElement).value).toBe("");
  });

  it("Create flushes draft rules into power_mode_map in the same write", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({ default_profile_id: "chat" }, { onConfigChange });

    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));
    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    fireEvent.change(within(dialog).getByLabelText("Profile Name"), {
      target: { value: "Email" },
    });

    // Add a rule via the draft's Add button -> AddAppModal. Clicking a listed
    // app STAGES it in the modal's pending list; the footer Add then commits it
    // to the draft's pending rules (NOT power_mode_map yet, since it's a draft).
    fireEvent.click(within(dialog).getByRole("button", { name: "Add" }));
    const addDialog = await screen.findByRole("dialog", { name: "Add a rule" });
    fireEvent.click(await within(addDialog).findByText("Slack"));
    // Commit the staged rule to the draft via the modal footer ("Add rule").
    fireEvent.click(within(addDialog).getByRole("button", { name: "Add rule" }));

    // No power_mode_map write while still a draft.
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ power_mode_map: expect.anything() }),
    );

    // Create flushes the profile + the pending rule in one write.
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    const call = onConfigChange.mock.calls.find(
      ([patch]) => patch && "profiles" in patch && "power_mode_map" in patch,
    );
    expect(call).toBeTruthy();
    const [patch] = call!;
    expect(Object.values(patch.power_mode_map)).toContain(
      patch.profiles.find((p: { name: string }) => p.name === "Email").id,
    );
  });

  it("Create with Make default points default_profile_id at the new profile", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({ default_profile_id: "chat" }, { onConfigChange });

    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));
    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    fireEvent.change(within(dialog).getByLabelText("Profile Name"), {
      target: { value: "Email" },
    });

    // Make the not-yet-created draft the default. The intent is buffered until
    // Create, where it must be applied to default_profile_id in the same write.
    fireEvent.click(within(dialog).getByRole("button", { name: "Make default" }));
    // The label flips, confirming the buffered state is reflected back.
    expect(
      within(dialog).getByRole("button", { name: "Remove as default" }),
    ).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));

    const call = onConfigChange.mock.calls.find(
      ([patch]) => patch && "profiles" in patch && "default_profile_id" in patch,
    );
    expect(call).toBeTruthy();
    const [patch] = call!;
    const created = patch.profiles.find(
      (p: { name: string }) => p.name === "Email",
    );
    expect(patch.default_profile_id).toBe(created.id);
  });

  it("Create without Make default leaves the existing default untouched", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({ default_profile_id: "chat" }, { onConfigChange });

    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));
    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    fireEvent.change(within(dialog).getByLabelText("Profile Name"), {
      target: { value: "Email" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));

    const call = onConfigChange.mock.calls.find(
      ([patch]) => patch && "profiles" in patch && "default_profile_id" in patch,
    );
    expect(call).toBeTruthy();
    const [patch] = call!;
    // "creating is not selecting": the default pointer stays where it was.
    expect(patch.default_profile_id).toBe("chat");
  });
});

describe("Enhancement — Custom provider", () => {
  beforeEach(() => {
    globalThis.fetch = makeFetchMock(200, LOCAL_MODELS_RESPONSE);
  });

  it("provider dropdown lists a 'Custom' option", async () => {
    // Start with anthropic in unconfigured state (enhancement_anthropic_api_key
    // unset, so storedKey is empty) so the Connection block force-expands and
    // the provider dropdown trigger is directly visible.
    renderEnhancement(
      { enhancement_provider: "anthropic", enhancement_api_key: "test-key" },
      { acknowledgedProviders: ["anthropic"] },
    );

    const trigger = await screen.findByRole("button", { name: /Anthropic/ });
    fireEvent.click(trigger);

    const customOption = await screen.findByRole("button", { name: /^Custom\./ });
    expect(customOption).toBeInTheDocument();
  });

  it("with enhancement_provider='custom' the controls render Base URL, Model, and API key inputs", () => {
    // No enhancement_custom_api_key -> connectionState="unconfigured" ->
    // Connection block force-expands, showing the custom branch controls.
    renderEnhancement({ enhancement_provider: "custom" });

    expect(screen.getByLabelText("Base URL")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Model:/ })).toBeInTheDocument();
    expect(screen.getByLabelText("API key")).toBeInTheDocument();
  });

  it("with enhancement_provider='custom' no cloud model <select> is rendered", () => {
    renderEnhancement({ enhancement_provider: "custom" });

    // All cloud providers use the app Dropdown, including Custom.
    // No native select is introduced.
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("picking Custom from the dropdown opens DataLeavesDialog (not a direct config write)", async () => {
    const onConfigChange = vi.fn();
    const onRequestPrivacyModal = vi.fn();

    renderEnhancement(
      { enhancement_provider: "anthropic", enhancement_api_key: "test-key" },
      { onConfigChange, onRequestPrivacyModal, acknowledgedProviders: ["anthropic"] },
    );

    const trigger = await screen.findByRole("button", { name: /Anthropic/ });
    fireEvent.click(trigger);

    const customOption = await screen.findByRole("button", { name: /^Custom\./ });
    fireEvent.click(customOption);

    // DataLeavesDialog must fire BEFORE PrivacyModal.
    await waitFor(() => {
      expect(screen.getByText("Review where your text goes")).toBeInTheDocument();
    });
    // PrivacyModal must NOT be requested yet.
    expect(onRequestPrivacyModal).not.toHaveBeenCalled();
    // Config must NOT be written for the candidate yet.
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ enhancement_provider: "custom" }),
    );
  });
});

describe("Enhancement — API key field (round 02)", () => {
  beforeEach(() => {
    globalThis.fetch = makeFetchMock(200, LOCAL_MODELS_RESPONSE);
  });

  // Distinctive middle so we can prove the full key is never rendered.
  const STORED = "AIzaSyTESTmiddleSECRET1234abcd3f9a";
  // The masked fingerprint the BACKEND computes and sends. The client never sees
  // the full key (Option 2 keeps plaintext server-side).
  const FINGERPRINT = "AIza••••••••••3f9a";

  async function expandSavedGemini(withFingerprint = true) {
    renderEnhancement(
      {
        enhancement_provider: "gemini",
        enhancement_gemini_api_key: STORED,
        ...(withFingerprint
          ? { enhancement_gemini_api_key_fingerprint: FINGERPRINT }
          : {}),
      },
      { acknowledgedProviders: ["gemini"] },
    );
    const summary = await screen.findByRole("button", { name: /Gemini/, expanded: false });
    fireEvent.click(summary);
    return screen.findByRole("button", { name: "Replace API key" });
  }

  it("a saved key renders the server-provided fingerprint, never the raw key", async () => {
    const chip = await expandSavedGemini();
    expect(chip.textContent).toContain(FINGERPRINT); // server fingerprint
    expect(chip.textContent).not.toContain("middleSECRET"); // full key never rendered
    // No reveal control: Option 2 keeps the plaintext on the backend.
    expect(screen.queryByRole("button", { name: "Show API key" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Hide API key" })).toBeNull();
    // The raw input is not mounted while a saved key shows.
    expect(screen.queryByPlaceholderText("Paste API key")).toBeNull();
  });

  it("with a saved key but no fingerprint yet, shows neutral dots (never the key)", async () => {
    const chip = await expandSavedGemini(false);
    expect(chip.textContent).toContain("•");
    expect(chip.textContent).not.toContain("middleSECRET");
  });

  it("clicking the chip (Replace) opens an editable input to paste a new key", async () => {
    await expandSavedGemini();
    fireEvent.click(screen.getByRole("button", { name: "Replace API key" }));
    await waitFor(() => {
      expect(screen.getByPlaceholderText("Paste API key")).toBeInTheDocument();
    });
  });
});

describe("Enhancement — controls layout (round 03)", () => {
  beforeEach(() => {
    globalThis.fetch = makeFetchMock(200, LOCAL_MODELS_RESPONSE);
  });

  it("custom: the OpenAI-compatible spec link renders as the subtext (under the dropdown)", async () => {
    renderEnhancement(
      {
        enhancement_provider: "custom",
        enhancement_custom_base_url: "https://openrouter.ai/api/v1",
      },
      { acknowledgedProviders: ["custom"] },
    );
    const link = await screen.findByRole("link", { name: "OpenAI-compatible Chat Completions" });
    expect(link).toHaveAttribute(
      "href",
      "https://platform.openai.com/docs/api-reference/chat",
    );
    // Base URL is the primary top-row field; Model + API key move full-width below.
    expect(screen.getByLabelText("Base URL")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Model:/ })).toBeInTheDocument();
  });

  it("Gemini setup does not render separate pricing prose or a key link", async () => {
    renderEnhancement(
      { enhancement_provider: "gemini", enhancement_gemini_api_key: "" },
      { acknowledgedProviders: ["gemini"] },
    );
    expect(await screen.findByLabelText("API key")).toBeInTheDocument();
    expect(
      screen.getByText("Pick a provider and add its API key."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /free key/i })).toBeNull();
    expect(screen.queryByText(/Gemini is free/i)).toBeNull();
  });

  it("custom collapsed summary shows host and model as distinct segments", async () => {
    renderEnhancement(
      {
        enhancement_provider: "custom",
        enhancement_custom_base_url: "https://openrouter.ai/api/v1",
        enhancement_custom_model: "google/gemini-3.1-flash-lite",
        enhancement_custom_api_key: "sk-or-test", // gitleaks:allow
      },
      { acknowledgedProviders: ["custom"] },
    );
    // Connected custom collapses to a summary carrying host + model segments.
    const summary = await screen.findByRole("button", { name: /Custom/, expanded: false });
    expect(summary).toHaveAttribute("aria-expanded", "false");
    expect(summary.textContent).toContain("openrouter.ai");
    expect(summary.textContent).toContain("google/gemini-3.1-flash-lite");
  });
});

describe("Enhancement advanced timeout and chunk controls", () => {
  beforeEach(() => {
    globalThis.fetch = makeFetchMock(200, LOCAL_MODELS_RESPONSE);
  });

  it("renders the locked copy and accessible numeric fields", () => {
    renderEnhancement();
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));

    expect(screen.getByText("Enhancement timeout")).toBeInTheDocument();
    expect(screen.getByText("seconds")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Chunking" })).toBeInTheDocument();
    expect(screen.getByText("Clean in chunks")).toBeInTheDocument();
    expect(screen.getByText("Limit first chunk to 60 words")).toBeInTheDocument();

    const timeout = screen.getByRole("spinbutton", {
      name: "Enhancement timeout in seconds",
    });
    const words = screen.getByRole("spinbutton", { name: "Words per chunk" });
    expect(timeout).toHaveValue(5);
    expect(words).toHaveValue(120);
    expect(timeout).toHaveAttribute("aria-describedby", "enhancement-timeout-help");
    expect(words).toHaveAttribute(
      "aria-describedby",
      "enhancement-words-per-chunk-help",
    );
  });

  it("disables dependent controls without changing their displayed values", () => {
    renderEnhancement({
      enhancement_chunked_cleanup: false,
      enhancement_words_per_chunk: 240,
      enhancement_limit_first_chunk: false,
    });
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));

    expect(screen.getByRole("spinbutton", { name: "Words per chunk" })).toBeDisabled();
    expect(screen.getByRole("spinbutton", { name: "Words per chunk" })).toHaveValue(240);
    expect(
      screen.getByRole("switch", { name: "Limit first chunk to 60 words" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("switch", { name: "Limit first chunk to 60 words" }),
    ).toHaveAttribute("aria-checked", "false");
  });

  it("emits focused config patches for all four controls", () => {
    const onConfigChange = vi.fn();
    renderEnhancement({}, { onConfigChange });
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));

    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Enhancement timeout in seconds" }),
      { target: { value: "30" } },
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "Words per chunk" }), {
      target: { value: "200" },
    });
    fireEvent.click(screen.getByRole("switch", { name: "Clean in chunks" }));
    fireEvent.click(
      screen.getByRole("switch", { name: "Limit first chunk to 60 words" }),
    );

    expect(onConfigChange).toHaveBeenCalledWith({
      enhancement_timeout_seconds: 30,
    });
    expect(onConfigChange).toHaveBeenCalledWith({
      enhancement_words_per_chunk: 200,
    });
    expect(onConfigChange).toHaveBeenCalledWith({
      enhancement_chunked_cleanup: false,
    });
    expect(onConfigChange).toHaveBeenCalledWith({
      enhancement_limit_first_chunk: false,
    });
  });
});

describe("Enhancement output safeguards", () => {
  beforeEach(() => {
    globalThis.fetch = makeFetchMock(200, LOCAL_MODELS_RESPONSE);
  });

  it.each(["local", "openai", "ollama", "custom"])(
    "allows %s safeguards to be turned off and back on",
    async (provider) => {
      const onConfigChange = vi.fn();
      const config = { enhancement_provider: provider, enhancement_safeguards_enabled: true };
      const view = renderEnhancement(config, { onConfigChange });
      await act(async () => {});
      fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
      const toggle = screen.getByRole("switch", { name: "Output safeguards" });
      expect(toggle).toHaveAttribute("aria-checked", "true");
      expect(toggle).toHaveAttribute("aria-describedby", "enhancement-safeguards-help");
      fireEvent.click(toggle);
      expect(onConfigChange).toHaveBeenLastCalledWith({ enhancement_safeguards_enabled: false });

      view.rerender(enhancementElement(
        { ...config, enhancement_safeguards_enabled: false },
        { onConfigChange, configSaveRevision: 1 },
      ));
      await act(async () => {});
      expect(toggle).toHaveAttribute("aria-checked", "false");
      fireEvent.click(toggle);
      expect(onConfigChange).toHaveBeenLastCalledWith({ enhancement_safeguards_enabled: true });
      if (provider === "local") {
        expect(screen.getByText(/Repair unambiguous digit errors/)).toBeInTheDocument();
      }
    },
  );
});

// ---------------------------------------------------------------------------
// Profile icons (transparent-indicators task 1.3)
// ---------------------------------------------------------------------------

describe("Enhancement - profile stamps and icon persistence", () => {
  beforeEach(() => {
    globalThis.fetch = makeRoutingFetchMock();
  });

  it("every profile row leads with its stamp, derived for built-ins", async () => {
    renderEnhancement({
      default_profile_id: "chat",
      profiles: [
        { id: "chat", name: "Chat", prompt: "Chat prompt" },
        { id: "code", name: "Code", prompt: "Code prompt" },
        { id: "custom1", name: "Slack", prompt: "Slack prompt" },
        { id: "custom2", name: "Research", prompt: "R", icon: "search" },
      ],
    });
    const rows = await screen.findAllByRole("button", { name: /^Edit / });
    const stamps = rows.map((row) =>
      row.querySelector("[data-testid=\"profile-mark\"]")?.getAttribute("data-profile-icon"),
    );
    expect(stamps).toEqual(["chat", "code", "letter", "search"]);
  });

  it("the stamp is decorative; the row keeps its single accessible name", async () => {
    renderEnhancement({ default_profile_id: "chat" });
    const row = await screen.findByRole("button", { name: "Edit Code" });
    const stamp = row.querySelector("[data-testid=\"profile-mark\"]");
    expect(stamp?.getAttribute("aria-hidden")).toBe("true");
  });

  it("picking an icon for an existing custom profile persists the key", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement(
      {
        default_profile_id: "chat",
        profiles: [
          { id: "chat", name: "Chat", prompt: "Chat prompt" },
          { id: "custom1", name: "Slack", prompt: "Slack prompt" },
        ],
      },
      { onConfigChange },
    );

    fireEvent.click(await screen.findByRole("button", { name: "Edit Slack" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Slack" });
    fireEvent.click(within(dialog).getByRole("button", { name: /Choose profile icon/ }));
    fireEvent.click(within(dialog).getByRole("radio", { name: "Sparkle" }));

    await waitFor(() => {
      expect(onConfigChange).toHaveBeenCalledWith(
        expect.objectContaining({
          profiles: expect.arrayContaining([
            expect.objectContaining({ id: "custom1", icon: "sparkle" }),
          ]),
        }),
      );
    });
  });

  it("choosing the letter fallback deletes the stored key rather than blanking it", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement(
      {
        default_profile_id: "chat",
        profiles: [
          { id: "chat", name: "Chat", prompt: "Chat prompt" },
          { id: "custom1", name: "Slack", prompt: "Slack prompt", icon: "sparkle" },
        ],
      },
      { onConfigChange },
    );

    fireEvent.click(await screen.findByRole("button", { name: "Edit Slack" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Slack" });
    fireEvent.click(within(dialog).getByRole("button", { name: /Choose profile icon/ }));
    fireEvent.click(within(dialog).getAllByRole("radio")[0]);

    await waitFor(() => expect(onConfigChange).toHaveBeenCalled());
    const patch = onConfigChange.mock.calls
      .map(([p]) => p)
      .reverse()
      .find((p) => p && "profiles" in p);
    const saved = patch.profiles.find((p: { id: string }) => p.id === "custom1");
    expect("icon" in saved).toBe(false);
  });

  it("a built-in profile has no icon grid and never writes an icon key", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({ default_profile_id: "chat" }, { onConfigChange });
    fireEvent.click(await screen.findByRole("button", { name: "Edit Chat" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Chat" });
    expect(within(dialog).queryByRole("radiogroup", { name: "Profile icon" })).toBeNull();
    expect(within(dialog).getByLabelText("Chat uses its built-in icon")).toBeInTheDocument();
  });

  it("a draft buffers its icon and Create flushes it in the same write", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({ default_profile_id: "chat" }, { onConfigChange });

    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));
    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    fireEvent.change(within(dialog).getByLabelText("Profile Name"), {
      target: { value: "Research" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: /Choose profile icon/ }));
    fireEvent.click(within(dialog).getByRole("radio", { name: "Search" }));

    // Nothing persisted while the draft is open.
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ profiles: expect.anything() }),
    );

    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(onConfigChange).toHaveBeenCalledWith(
      expect.objectContaining({
        profiles: expect.arrayContaining([
          expect.objectContaining({ name: "Research", icon: "search" }),
        ]),
      }),
    );
  });

  it("cancelling a draft discards its icon selection", async () => {
    const onConfigChange = vi.fn();
    renderEnhancement({ default_profile_id: "chat" }, { onConfigChange });

    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));
    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    fireEvent.click(within(dialog).getByRole("button", { name: /Choose profile icon/ }));
    fireEvent.click(within(dialog).getByRole("radio", { name: "Search" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "New profile" })).toBeNull();
    });
    expect(onConfigChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ profiles: expect.anything() }),
    );
  });

  it("the draft editor preview follows the buffered selection", async () => {
    renderEnhancement({ default_profile_id: "chat" });
    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));
    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    fireEvent.click(within(dialog).getByRole("button", { name: /Choose profile icon/ }));
    fireEvent.click(within(dialog).getByRole("radio", { name: "Globe" }));
    await waitFor(() => {
      expect(
        screen.getByTestId("profile-mark-preview").getAttribute("data-profile-icon"),
      ).toBe("globe");
    });
  });
});

describe("hardware check catalog readiness", () => {
  it("keeps the nudge open while models load and opens the check once ready", async () => {
    const catalog = deferred<ReturnType<typeof jsonResponse>>();
    const fallback = makeFetchMock(200, LOCAL_MODELS_RESPONSE);
    globalThis.fetch = vi.fn((...args: Parameters<typeof fetch>) => {
      if (String(args[0]).includes("provider=local")) return catalog.promise as unknown as Promise<Response>;
      return fallback(...args);
    });
    renderEnhancement({ enhancement_provider: "gemini", enhancement_local_nudge_seen: false });
    const loading = await screen.findByRole("button", { name: "Loading models…" });
    expect(loading).toBeDisabled();
    fireEvent.click(loading);
    expect(screen.getByRole("dialog", { name: "Try cleanup on your device" })).toBeInTheDocument();
    await act(async () => catalog.resolve(jsonResponse(200, LOCAL_MODELS_RESPONSE)));
    fireEvent.click(await screen.findByRole("button", { name: "Check my hardware" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Try cleanup on your device" })).not.toBeInTheDocument());
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("keeps an unavailable catalog retryable instead of dismissing the nudge", async () => {
    globalThis.fetch = makeFetchMock(200, { ...LOCAL_MODELS_RESPONSE, bundled: [], available_ids: [] });
    renderEnhancement({ enhancement_provider: "gemini", enhancement_local_nudge_seen: false });
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(screen.getByRole("dialog", { name: "Try cleanup on your device" })).toBeInTheDocument();
    await screen.findByRole("button", { name: "Try again" });
  });
});
