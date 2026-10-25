import { WINDOWS_CAPABILITIES, WINDOWS_ADVANCED_CAPABILITIES } from "../../test/platformFixtures";
/**
 * Managed Lockdown authority UI (Task 2.5).
 *
 * An administrator machine policy can force Lockdown Mode on
 * (HKLM\SOFTWARE\Policies\Whisperstream\ManagedLockdown). The backend already
 * refuses disable/reset with a stable `managed_by_organization` code; these
 * tests pin the frontend contract so no surface offers a change the backend
 * will refuse, and so the two causes stay separate:
 *
 *   1. Lockdown Mode explains why cloud enhancement is unavailable.
 *   2. "Managed by your organization." explains why the user cannot turn
 *      Lockdown Mode off.
 *
 * Managed copy must never replace (1) on a dependent setting.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { Home } from "../../pages/Home";
import { Enhancement } from "../../pages/Enhancement";
import { LockdownManageDialog } from "../LockdownManageDialog";
import { LockdownScopeReviewDialog } from "../LockdownScopeReviewDialog";
import { useLockdown } from "../../lib/useLockdown";
import type { AppConfig } from "../../lib/api/config";
import type { LockdownStatus } from "../../lib/api/lockdown";
import type { LockStatus } from "../../lib/useLockdown";

const apiMocks = vi.hoisted(() => ({
  getLockdownStatus: vi.fn(),
}));

vi.mock("../../lib/api/config", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api/config")>(
    "../../lib/api/config",
  );
  return {
    ...actual,
  };
});
vi.mock("../../lib/api/lockdown", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api/lockdown")>(
    "../../lib/api/lockdown",
  );
  return {
    ...actual,
    getLockdownStatus: apiMocks.getLockdownStatus,
  };
});

const LOCAL_MODELS_RESPONSE = {
  provider: "local",
  bundled: [
    {
      id: "gemma4-e2b-instruct",
      label: "Gemma 4 E2B Instruct",
      disk_mb: 2962,
      ram_mb: 1280,
      min_system_ram_mb: 8192,
      recommended: true,
    },
  ],
  discovered: [],
  installed_ids: [],
  available_ids: ["gemma4-e2b-instruct"],
  active_id: "gemma4-e2b-instruct",
  running: false,
};

beforeEach(() => {
  // Home fetches /api/stats and Enhancement fetches the local catalogue on
  // mount. Neither payload matters here (the assertions are on static
  // authority copy), so leave Home's stats request pending and answer
  // everything else with the catalogue shape.
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      if (String(url).includes("/api/stats")) return new Promise(() => {});
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify(LOCAL_MODELS_RESPONSE)),
        json: () => Promise.resolve(LOCAL_MODELS_RESPONSE),
      });
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function makeStatus(overrides: Partial<LockdownStatus> = {}): LockdownStatus {
  return {
    enabled: true,
    locked: false,
    lockout_remaining_seconds: 0,
    idle_lock_seconds: 300,
    has_recovery_code: false,
    has_envelope: false,
    history_enabled: false,
    acknowledged_version: "2",
    ...overrides,
  };
}

function makeLockStatus(overrides: Partial<LockStatus> = {}): LockStatus {
  return {
    locked: false,
    lockoutRemainingSeconds: 0,
    enabled: true,
    hasEnvelope: false,
    historyEnabled: false,
    acknowledgedVersion: "2",
    managed: false,
    refresh: vi.fn(),
    ...overrides,
  };
}

function makeConfig(overrides: Partial<AppConfig> = {}): AppConfig {
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
    language: "en",
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
    setup_completed: true,
    enhancement_enabled: false,
    enhancement_provider: "local",
    enhancement_api_key: "",
    enhancement_gemini_api_key: "",
    enhancement_openai_api_key: "",
    enhancement_anthropic_api_key: "",
    enhancement_ollama_endpoint: "http://localhost:11434",
    enhancement_ollama_model: "llama3.2",
    enhancement_gemini_model: "gemini-2.5-flash-lite",
    enhancement_openai_model: "gpt-4o-mini",
    enhancement_anthropic_model: "claude-haiku-4-5-20251001",
    enhancement_custom_base_url: "",
    enhancement_custom_model: "",
    enhancement_custom_api_key: "",
    enhancement_mode: "default",
    enhancement_custom_prompt: "",
    profiles: [{ id: "chat", name: "Chat", prompt: "Chat prompt" }],
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
    history_enabled: false,
    save_audio: false,
    transcript_retention_days: 7,
    lockdown_mode: true,
    lockdown_idle_lock_seconds: 300,
    theme: "light",
    claude_hud_enabled: false,
    claude_hud_show_text: true,
    agent_speech_mode: "off",
    agent_speech_voice: 0,
    agent_speech_speed: 1.0,
    agent_speech_volume: 1.0,
    ...overrides,
  } as AppConfig;
}

const NOOP = () => {};

function renderHome(lockStatus: LockStatus, config = makeConfig()) {
  return render(
    <Home
      config={config}
      startupStatus={{ state: "disabled", failure: null }}
      capabilities={WINDOWS_CAPABILITIES}
      onStartupChange={NOOP}
      onConfigChange={NOOP}
      subscribe={() => () => {}}
      onRequestOpenLanguagePopup={NOOP}
      onRequestLanguageChange={NOOP}
      downloadInFlight={false}
      onSelectionModeChange={NOOP}
      onSttBackendChange={NOOP}
      onShowStatus={NOOP}
      lockStatus={lockStatus}
      onConfigReload={NOOP}
      onConfigReplace={NOOP}
    />,
  );
}

/** The Lockdown Mode master switch, found by its accessible name. */
function lockdownToggle(): HTMLElement {
  return screen.getByRole("switch", { name: "Lockdown Mode" });
}

describe("useLockdown managed state", () => {
  it("reads no-history authority independently of Managed Lockdown", async () => {
    apiMocks.getLockdownStatus.mockResolvedValue(makeStatus({ enabled: false, managed: false, history_disabled: true }));
    const { result } = renderHook(() => useLockdown(() => () => {}));
    await waitFor(() => expect(result.current.historyDisabled).toBe(true));
    expect(result.current.managed).toBe(false);
  });

  it("reads managed authority from the backend status and fails safe", async () => {
    apiMocks.getLockdownStatus.mockResolvedValue(makeStatus({ managed: true }));
    const managed = renderHook(() => useLockdown(() => () => {}));
    await waitFor(() => expect(managed.result.current.managed).toBe(true));
    expect(managed.result.current.enabled).toBe(true);
    managed.unmount();

    // An older backend omits the field entirely: unmanaged, never a crash.
    apiMocks.getLockdownStatus.mockResolvedValue(makeStatus());
    const absent = renderHook(() => useLockdown(() => () => {}));
    await waitFor(() => expect(absent.result.current.enabled).toBe(true));
    expect(absent.result.current.managed).toBe(false);
    absent.unmount();

    // Only the exact boolean counts as managed authority.
    apiMocks.getLockdownStatus.mockResolvedValue(
      makeStatus({ managed: "yes" as unknown as boolean }),
    );
    const loose = renderHook(() => useLockdown(() => () => {}));
    await waitFor(() => expect(loose.result.current.enabled).toBe(true));
    expect(loose.result.current.managed).toBe(false);
  });
});

describe("Home Lockdown row under managed policy", () => {
  it("keeps the master control ON, disabled, and explained in accessible text", () => {
    renderHome(makeLockStatus({ managed: true }));

    const toggle = lockdownToggle();
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAttribute("aria-disabled", "true");
    // The explanation is real text tied to the control, not a hover-only tip:
    // a disabled control never receives hover on keyboard or touch.
    expect(toggle).toHaveAccessibleDescription("Managed by your organization.");
    expect(screen.getByText("Managed by your organization.")).toBeInTheDocument();
  });

  it("still explains what Lockdown Mode does, in addition to who owns it", () => {
    renderHome(makeLockStatus({ managed: true }));

    expect(
      screen.getByText(
        "On-device transcription and cleanup. Saved history locked.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Designed to support HIPAA-conscious workflows."),
    ).toBeInTheDocument();
  });

  it("shows the master control ON even if the config echo has not caught up", () => {
    renderHome(
      makeLockStatus({ managed: true }),
      makeConfig({ lockdown_mode: false }),
    );

    expect(lockdownToggle()).toHaveAttribute("aria-checked", "true");
  });

  it("keeps Manage reachable so password and history setup stay available", () => {
    renderHome(makeLockStatus({ managed: true }));

    expect(screen.getByRole("button", { name: "Manage" })).toBeEnabled();
  });

  it("leaves the control interactive and unlabelled by policy when unmanaged", () => {
    renderHome(makeLockStatus({ managed: false }));

    const toggle = lockdownToggle();
    expect(toggle).toBeEnabled();
    expect(toggle).not.toHaveAttribute("aria-disabled");
    expect(
      screen.queryByText("Managed by your organization."),
    ).not.toBeInTheDocument();
  });
});

describe("Lockdown Manage dialog under managed policy", () => {
  function renderManage(
    overrides: Partial<Parameters<typeof LockdownManageDialog>[0]> = {},
  ) {
    return render(
      <LockdownManageDialog
        open
        managed
        hasEnvelope
        idleLockSeconds={300}
        onIdleLockChange={vi.fn()}
        onChangePassword={vi.fn()}
        onViewRecovery={vi.fn()}
        onStartSaving={vi.fn()}
        onDisable={vi.fn()}
        onClose={vi.fn()}
        {...overrides}
      />,
    );
  }

  it("removes the turn-off action and names the authority instead", () => {
    renderManage();

    expect(
      screen.queryByRole("button", { name: "Turn off Lockdown Mode" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Your organization requires Lockdown Mode on this device."),
    ).toBeInTheDocument();
  });

  it("preserves password, recovery key, and idle-lock management", () => {
    renderManage();

    expect(
      screen.getByRole("button", { name: "Change password" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "View recovery key" }),
    ).toBeEnabled();
    expect(
      screen.getByText("Lock transcripts after idle"),
    ).toBeInTheDocument();
  });

  it("preserves encrypted-history setup in the no-envelope variant", () => {
    renderManage({ hasEnvelope: false });

    expect(
      screen.getByRole("button", { name: "Start saving transcripts" }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "Turn off Lockdown Mode" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Your organization requires Lockdown Mode on this device."),
    ).toBeInTheDocument();
  });

  it("removes password setup when the independent history restriction is active", () => {
    renderManage({ hasEnvelope: false, historyDisabled: true });
    expect(screen.queryByRole("button", { name: "Start saving transcripts" })).not.toBeInTheDocument();
    expect(screen.getByText("Transcript and audio history are disabled by your organization.")).toBeInTheDocument();
    expect(screen.getByText("On-device transcription and cleanup. History is disabled.")).toBeInTheDocument();
  });

  it("keeps the turn-off action for an unmanaged install", () => {
    renderManage({ managed: false });

    expect(
      screen.getByRole("button", { name: "Turn off Lockdown Mode" }),
    ).toBeEnabled();
    expect(
      screen.queryByText(/organization/i),
    ).not.toBeInTheDocument();
  });
});

describe("Lockdown scope review under managed policy", () => {
  it("is Continue-only so the required review offers no refused action", async () => {
    const onContinue = vi.fn().mockResolvedValue(undefined);
    render(
      <LockdownScopeReviewDialog
        open
        managed
        onContinue={onContinue}
        onTurnOff={vi.fn()}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "Review Lockdown Mode" });
    expect(
      within(dialog).queryByRole("button", { name: "Turn off Lockdown Mode" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Your organization requires Lockdown Mode on this device."),
    ).toBeInTheDocument();
    // Focus lands on Continue, so the authority sentence has to be part of the
    // dialog's announced description rather than text a screen-reader user only
    // reaches by browsing backwards.
    expect(dialog).toHaveAccessibleDescription(
      /Your organization requires Lockdown Mode on this device\./,
    );

    const continueButton = screen.getByRole("button", { name: "Continue" });
    await waitFor(() => expect(continueButton).toHaveFocus());
  });

  it("keeps the turn-off action for an unmanaged install", () => {
    render(
      <LockdownScopeReviewDialog
        open
        onContinue={vi.fn().mockResolvedValue(undefined)}
        onTurnOff={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: "Turn off Lockdown Mode" }),
    ).toBeEnabled();
  });
});

describe("dependent settings keep naming Lockdown Mode", () => {
  it("keeps the Enhancement cause copy about Lockdown, not the organization", async () => {
    render(
      <Enhancement
        capabilities={WINDOWS_CAPABILITIES}
        advancedCapabilities={WINDOWS_ADVANCED_CAPABILITIES}
        config={makeConfig({ lockdown_mode: true })}
        onConfigChange={NOOP}
        onRequestPrivacyModal={NOOP}
        acknowledgedProviders={[]}
        onConsentChanged={NOOP}
        subscribe={() => () => {}}
        send={() => true}
        connected
        lockStatus={makeLockStatus({ managed: true })}
        onNavigateHome={NOOP}
      />,
    );

    expect(
      await screen.findByText(
        "Lockdown limits AI cleanup to the on-device model.",
      ),
    ).toBeInTheDocument();
    // Managed authority belongs at the Lockdown control, not on every setting
    // Lockdown restricts. The direct reason must stay visible here.
    expect(screen.queryByText(/organization/i)).not.toBeInTheDocument();
  });
});
