import { WINDOWS_CAPABILITIES } from "../../test/platformFixtures";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppConfig } from "../../lib/api/config";
import type { SttArtifactId, SttStatusResponse } from "../../lib/api/models";
import type { AppEvent } from "../../lib/socket/events";
import { makeConfig } from "../../pages/__tests__/homeConfigFixture";
import { OnboardingOverlay } from "./OnboardingOverlay";

const PARAKEET_V2: SttArtifactId = "nemo-parakeet-tdt-0.6b-v2";
const PARAKEET_V3: SttArtifactId = "nemo-parakeet-tdt-0.6b-v3";
const QWEN: SttArtifactId = "qwen3-asr-0.6b-int8";

const getSttStatus = vi.fn<() => Promise<SttStatusResponse>>();
const saveConfig = vi.fn<(config: Partial<AppConfig>) => Promise<AppConfig>>();
const startDownload = vi.fn<() => Promise<void>>();
const getModelsDiskFree = vi.fn<
  () => Promise<{ free_mb: number; required_mb: number; sufficient: boolean }>
>();

vi.mock("../../lib/api/models", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/models")>();
  return {
    ...actual,
    getSttStatus: () => getSttStatus(),
    startDownload: () => startDownload(),
    getModelsDiskFree: () => getModelsDiskFree(),
  };
});
vi.mock("../../lib/api/config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/config")>();
  return {
    ...actual,
    saveConfig: (config: Partial<AppConfig>) => saveConfig(config),
  };
});

// Keep the shortcut step inert so its capture wiring does not obscure routing
// assertions. StepTest deliberately remains real: these tests cover the full
// language selection -> exact activation -> Step 3 readiness boundary.
vi.mock("./StepShortcut", () => ({
  StepShortcut: () => <div>Shortcut step</div>,
}));
vi.mock("../TitleBar", () => ({ TitleBar: () => null }));

const subscribers = new Set<(event: AppEvent) => void>();

function emit(event: AppEvent) {
  for (const subscriber of subscribers) subscriber(event);
}

function sttStatus({
  configured,
  loaded,
  downloaded,
}: {
  configured: SttArtifactId;
  loaded: SttArtifactId | null;
  downloaded: SttArtifactId[];
}): SttStatusResponse {
  const artifacts = ([PARAKEET_V3, PARAKEET_V2, QWEN] as const).map((id) => ({
    id,
    backend: id === QWEN ? ("qwen" as const) : ("parakeet" as const),
    downloaded: downloaded.includes(id),
    active: id === loaded,
    size_bytes: downloaded.includes(id) ? 600_000_000 : 0,
  }));
  return {
    configured_artifact_id: configured,
    loaded_artifact_id: loaded,
    artifacts,
    unused_size_bytes: 0,
    parakeet: {
      downloaded: downloaded.some((id) => id !== QWEN),
      active: loaded !== null && loaded !== QWEN,
    },
    qwen: {
      downloaded: downloaded.includes(QWEN),
      active: loaded === QWEN,
    },
    cohere: { downloaded: false, active: false },
  };
}

function renderOnboarding(config: AppConfig) {
  const onConfigChange = vi.fn<(patch: Partial<AppConfig>) => void>();
  render(
    <OnboardingOverlay
      capabilities={WINDOWS_CAPABILITIES}
      config={config}
      modelLoaded={true}
      send={() => true}
      subscribe={(handler) => {
        subscribers.add(handler);
        return () => subscribers.delete(handler);
      }}
      connected={true}
      onConfigChange={onConfigChange}
      onFinish={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  return onConfigChange;
}

beforeEach(() => {
  subscribers.clear();
  saveConfig.mockImplementation(async (patch) => ({
    ...makeConfig(),
    ...patch,
  }));
  startDownload.mockResolvedValue();
  getModelsDiskFree.mockResolvedValue({
    free_mb: 20_000,
    required_mb: 1_500,
    sufficient: true,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("OnboardingOverlay exact STT routing", () => {
  it("restarts Automatic routing and activates Qwen after interrupted Parakeet onboarding", async () => {
    const config = makeConfig({
      language: "ja",
      stt_model_selection_mode: "manual",
      stt_backend: "parakeet",
      parakeet_model: PARAKEET_V2,
      qwen_force_language: true,
      setup_completed: false,
    });
    getSttStatus.mockResolvedValue(
      sttStatus({
        configured: PARAKEET_V2,
        loaded: PARAKEET_V2,
        downloaded: [PARAKEET_V2],
      }),
    );

    const onConfigChange = renderOnboarding(config);

    await waitFor(() => expect(startDownload).toHaveBeenCalledOnce());
    expect(saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        language: "ja",
        stt_model_selection_mode: "automatic",
        stt_backend: "qwen",
        qwen_force_language: false,
      }),
    );
    expect(onConfigChange).toHaveBeenCalledWith({
      language: "ja",
      stt_model_selection_mode: "automatic",
      stt_backend: "qwen",
      parakeet_model: PARAKEET_V2,
      qwen_force_language: false,
    });
    expect(getModelsDiskFree).toHaveBeenCalledOnce();
  });

  it("activates English Parakeet when interrupted onboarding still has Qwen loaded", async () => {
    const config = makeConfig({
      language: "en",
      stt_model_selection_mode: "manual",
      stt_backend: "qwen",
      parakeet_model: PARAKEET_V3,
      qwen_force_language: true,
      setup_completed: false,
    });
    getSttStatus.mockResolvedValue(
      sttStatus({
        configured: QWEN,
        loaded: QWEN,
        downloaded: [QWEN, PARAKEET_V2],
      }),
    );

    renderOnboarding(config);

    await waitFor(() => expect(startDownload).toHaveBeenCalledOnce());
    expect(saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        language: "en",
        stt_model_selection_mode: "automatic",
        stt_backend: "parakeet",
        parakeet_model: PARAKEET_V2,
        qwen_force_language: false,
      }),
    );
    expect(getModelsDiskFree).not.toHaveBeenCalled();
  });

  it("clears stale Manual and Qwen-force state even when the recommended exact target is loaded", async () => {
    const config = makeConfig({
      language: "fr",
      stt_model_selection_mode: "manual",
      stt_backend: "qwen",
      parakeet_model: PARAKEET_V3,
      qwen_force_language: true,
      setup_completed: false,
    });
    getSttStatus.mockResolvedValue(
      sttStatus({
        configured: QWEN,
        loaded: PARAKEET_V3,
        downloaded: [QWEN, PARAKEET_V3],
      }),
    );

    renderOnboarding(config);

    await waitFor(() => expect(saveConfig).toHaveBeenCalledOnce());
    expect(saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        language: "fr",
        stt_model_selection_mode: "automatic",
        stt_backend: "parakeet",
        parakeet_model: PARAKEET_V3,
        qwen_force_language: false,
      }),
    );
    expect(startDownload).not.toHaveBeenCalled();
  });

  it("does not reactivate an already-loaded exact target", async () => {
    const config = makeConfig({
      language: "en",
      stt_model_selection_mode: "automatic",
      stt_backend: "parakeet",
      parakeet_model: PARAKEET_V2,
      qwen_force_language: false,
      setup_completed: false,
    });
    getSttStatus.mockResolvedValue(
      sttStatus({
        configured: PARAKEET_V2,
        loaded: PARAKEET_V2,
        downloaded: [PARAKEET_V2],
      }),
    );

    renderOnboarding(config);

    await waitFor(() => expect(saveConfig).toHaveBeenCalledOnce());
    expect(startDownload).not.toHaveBeenCalled();
    expect(getModelsDiskFree).not.toHaveBeenCalled();
  });

  it("keeps Step 3 gated until Japanese Qwen is the exact loaded artifact", async () => {
    const oldParakeet = sttStatus({
      configured: PARAKEET_V2,
      loaded: PARAKEET_V2,
      downloaded: [PARAKEET_V2],
    });
    const exactQwen = sttStatus({
      configured: QWEN,
      loaded: QWEN,
      downloaded: [PARAKEET_V2, QWEN],
    });
    getSttStatus.mockResolvedValue(oldParakeet);

    renderOnboarding(
      makeConfig({
        language: "ja",
        stt_model_selection_mode: "manual",
        stt_backend: "parakeet",
        parakeet_model: PARAKEET_V2,
        setup_completed: false,
      }),
    );

    await screen.findByText("Shortcut step");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByRole("heading", { name: "Downloading the speech model" });

    // App's generic modelLoaded prop is true, but the exact loaded artifact is
    // still old Parakeet. Step 3 must not expose the dictation prompt.
    expect(screen.queryByText("Ready")).not.toBeInTheDocument();
    expect(
      screen.queryByPlaceholderText("Your transcription will appear here."),
    ).not.toBeInTheDocument();

    act(() => {
      emit({
        type: "download.progress",
        downloaded: 600,
        total: 1_000,
        speed_bps: 100,
      });
    });
    await screen.findByText("60%");

    // A terminal download event is not proof by itself. It triggers a status
    // refresh, but the still-old exact status keeps the test surface gated.
    await act(async () => {
      emit({ type: "download.complete" });
      await Promise.resolve();
    });
    await screen.findByText("Finishing up...");
    expect(screen.queryByText("Ready")).not.toBeInTheDocument();

    // The activation terminal event causes another authoritative refresh.
    getSttStatus.mockResolvedValue(exactQwen);
    await act(async () => {
      emit({ type: "model.loaded" });
      await Promise.resolve();
    });

    await screen.findByText("Ready", {}, { timeout: 2_000 });
    expect(
      screen.getByPlaceholderText("Your transcription will appear here."),
    ).toBeInTheDocument();
  });

  it("recognizes English Parakeet activated before Step 3 subscribes", async () => {
    getSttStatus.mockResolvedValue(
      sttStatus({
        configured: QWEN,
        loaded: QWEN,
        downloaded: [QWEN, PARAKEET_V2],
      }),
    );

    renderOnboarding(
      makeConfig({
        language: "en",
        stt_model_selection_mode: "manual",
        stt_backend: "qwen",
        parakeet_model: PARAKEET_V3,
        setup_completed: false,
      }),
    );

    await screen.findByText("Shortcut step");
    // Simulate a present-artifact activation completing before StepTest mounts.
    getSttStatus.mockResolvedValue(
      sttStatus({
        configured: PARAKEET_V2,
        loaded: PARAKEET_V2,
        downloaded: [QWEN, PARAKEET_V2],
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByText("Ready");
    expect(
      screen.getByPlaceholderText("Your transcription will appear here."),
    ).toBeInTheDocument();
  });

  it("opens Step 3 ready when the recommended exact artifact was already loaded", async () => {
    getSttStatus.mockResolvedValue(
      sttStatus({
        configured: PARAKEET_V3,
        loaded: PARAKEET_V3,
        downloaded: [PARAKEET_V3],
      }),
    );

    renderOnboarding(
      makeConfig({
        language: "fr",
        stt_model_selection_mode: "automatic",
        stt_backend: "parakeet",
        parakeet_model: PARAKEET_V3,
        setup_completed: false,
      }),
    );

    await screen.findByText("Shortcut step");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByText("Ready");
    expect(startDownload).not.toHaveBeenCalled();
  });
});

describe("OnboardingOverlay Enhance step", () => {
  const readyStatus = () =>
    sttStatus({ configured: PARAKEET_V3, loaded: PARAKEET_V3, downloaded: [PARAKEET_V3] });

  async function reachTest(capabilities = WINDOWS_CAPABILITIES) {
    getSttStatus.mockResolvedValue(readyStatus());
    const send = vi.fn((_msg: Record<string, unknown>) => true);
    render(
      <OnboardingOverlay
        capabilities={capabilities}
        config={makeConfig({
          language: "fr",
          stt_model_selection_mode: "automatic",
          stt_backend: "parakeet",
          parakeet_model: PARAKEET_V3,
          setup_completed: false,
        })}
        modelLoaded={true}
        send={send}
        subscribe={(handler) => {
          subscribers.add(handler);
          return () => subscribers.delete(handler);
        }}
        connected={true}
        onConfigChange={vi.fn()}
        onFinish={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByText("Shortcut step");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByText("Ready");
    return send;
  }

  it("makes Test's primary Continue and opens Enhance as the last step", async () => {
    const send = await reachTest();
    expect(screen.getByText("Step 3 of 4")).toBeInTheDocument();
    const primary = screen.getByRole("button", { name: "Continue" });
    expect(primary).toBeDisabled();
    act(() => emit({ type: "transcription.result", text: "hello", duration_ms: 500 }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("heading", { name: "Polish your dictation" })).toBeInTheDocument();
    expect(screen.getByText("Step 4 of 4")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Turn on enhancement" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Skip & finish" })).toBeInTheDocument();
    // One suppression window spans Test and Enhance.
    const suppressCalls = send.mock.calls
      .map(([msg]) => msg)
      .filter((msg) => msg.type === "inject.suppress");
    expect(suppressCalls).toEqual([{ type: "inject.suppress", suppressed: true }]);
  });

  it("keeps Test last with Finish when managed local enhancement is unavailable", async () => {
    await reachTest({
      ...WINDOWS_CAPABILITIES,
      capabilities: WINDOWS_CAPABILITIES.capabilities.map((c) =>
        c.id === "managed-local-enhancement"
          ? { ...c, state: "not-implemented", reason: "phase5-pending", presentation: "disable" }
          : c,
      ),
    });
    expect(screen.getByText("Step 3 of 3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Finish" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
  });
});
