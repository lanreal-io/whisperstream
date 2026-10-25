import { WINDOWS_CAPABILITIES } from "../../test/platformFixtures";
/**
 * Vitest coverage for Home's Advanced manual-selection toggle and the
 * conditional two-choice model picker (Task 3.2).
 *
 * Locks the contract the locked scope and design require:
 *   - Automatic keeps the model row an informational label, so an ordinary user
 *     is never asked to make a model decision;
 *   - Manual shows the picker only when BOTH user-facing models serve the
 *     selected language, and keeps a label otherwise;
 *   - the Advanced toggle reports the intent and lets the host own routing;
 *   - a downloaded inactive model can be deleted from its own row, the active
 *     one cannot, and the locked deletion copy is used throughout;
 *   - `Force selected language` exists only on Manual + Qwen;
 *   - an in-flight switch disables the model controls and is announced.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import {
  render,
  act,
  screen,
  cleanup,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { Home } from "../Home";
import { makeConfig } from "./homeConfigFixture";
import type { AppConfig } from "../../lib/api/config";
import type { SttArtifactId, SttStatusResponse } from "../../lib/api/models";
import type { AppEvent } from "../../lib/socket/events";
import type { SttBackend } from "../../lib/languages";

const PARAKEET_V2: SttArtifactId = "nemo-parakeet-tdt-0.6b-v2";
const PARAKEET_V3: SttArtifactId = "nemo-parakeet-tdt-0.6b-v3";
const QWEN: SttArtifactId = "qwen3-asr-0.6b-int8";
const COHERE: SttArtifactId = "cohere-transcribe-03-2026-int8";

const getSttStatus = vi.fn<() => Promise<SttStatusResponse>>();
const deleteSttModel = vi.fn<(modelId: string) => Promise<unknown>>();

vi.mock("../../lib/api/models", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/models")>();
  return {
    ...actual,
    getSttStatus: (...args: []) => getSttStatus(...args),
    deleteSttModel: (...args: [string]) => deleteSttModel(...args),
  };
});

// Home fetches /api/stats on mount. Keep that request pending: these tests
// exercise model selection, not the stats payload.
vi.stubGlobal(
  "fetch",
  vi.fn(() => new Promise(() => {})),
);

/** Authoritative exact-artifact inventory, shaped like the backend's. */
function sttStatus({
  configured,
  loaded = configured,
  downloaded,
}: {
  configured: SttArtifactId;
  loaded?: SttArtifactId | null;
  downloaded: SttArtifactId[];
}): SttStatusResponse {
  const artifacts = ([PARAKEET_V3, PARAKEET_V2, QWEN, COHERE] as SttArtifactId[]).map(
    (id) => ({
      id,
      backend: (id === QWEN ? "qwen" : id === COHERE ? "cohere" : "parakeet") as SttBackend,
      downloaded: downloaded.includes(id),
      active: id === loaded,
      size_bytes: downloaded.includes(id) ? 600_000_000 : 0,
    }),
  );
  return {
    configured_artifact_id: configured,
    loaded_artifact_id: loaded ?? null,
    artifacts,
    unused_size_bytes: artifacts
      .filter((a) => a.downloaded && a.id !== configured && a.id !== loaded)
      .reduce((total, a) => total + a.size_bytes, 0),
    parakeet: {
      downloaded: true,
      active: configured === PARAKEET_V2 || configured === PARAKEET_V3,
    },
    qwen: { downloaded: downloaded.includes(QWEN), active: configured === QWEN },
    cohere: {
      downloaded: downloaded.includes(COHERE),
      active: configured === COHERE,
    },
  };
}

const NOOP = () => {};

interface RenderOverrides {
  subscribe?: (handler: (event: AppEvent) => void) => () => void;
  onConfigChange?: (patch: Partial<AppConfig>) => void;
  onSelectionModeChange?: (manual: boolean) => void;
  onSttBackendChange?: (backend: SttBackend) => void;
  onShowStatus?: (message: string, isError: boolean) => void;
  switchingToModel?: string | null;
  downloadInFlight?: boolean;
  sttInventoryRevision?: number;
}

type Handlers = {
  onConfigChange: (patch: Partial<AppConfig>) => void;
  onSelectionModeChange: (manual: boolean) => void;
  onSttBackendChange: (backend: SttBackend) => void;
  onShowStatus: (message: string, isError: boolean) => void;
};

function makeHandlers(overrides: RenderOverrides): Handlers {
  return {
    onConfigChange: overrides.onConfigChange ?? vi.fn(),
    onSelectionModeChange: overrides.onSelectionModeChange ?? vi.fn(),
    onSttBackendChange: overrides.onSttBackendChange ?? vi.fn(),
    onShowStatus: overrides.onShowStatus ?? vi.fn(),
  };
}

function homeElement(
  config: AppConfig,
  overrides: RenderOverrides,
  handlers: Handlers,
) {
  return (
    <Home
      config={config}
      startupStatus={{ state: "disabled", failure: null }}
      capabilities={WINDOWS_CAPABILITIES}
      onStartupChange={NOOP}
      onConfigChange={handlers.onConfigChange}
      subscribe={overrides.subscribe ?? (() => () => {})}
      onRequestOpenLanguagePopup={NOOP}
      onRequestLanguageChange={NOOP}
      downloadInFlight={overrides.downloadInFlight ?? false}
      switchingToModel={overrides.switchingToModel ?? null}
      sttInventoryRevision={overrides.sttInventoryRevision}
      onSelectionModeChange={handlers.onSelectionModeChange}
      onSttBackendChange={handlers.onSttBackendChange}
      onShowStatus={handlers.onShowStatus}
      onConfigReload={NOOP}
      onConfigReplace={NOOP}
    />
  );
}

function renderHome(config: AppConfig, overrides: RenderOverrides = {}) {
  const handlers = makeHandlers(overrides);
  render(homeElement(config, overrides, handlers));
  return handlers;
}

function renderHomeWithRerender(
  config: AppConfig,
  overrides: RenderOverrides = {},
) {
  const handlers = makeHandlers(overrides);
  const view = render(homeElement(config, overrides, handlers));
  return {
    ...handlers,
    rerender(nextConfig: AppConfig, nextOverrides: RenderOverrides = {}) {
      view.rerender(
        homeElement(nextConfig, { ...overrides, ...nextOverrides }, handlers),
      );
    },
  };
}

/** Flush the mount-time artifact-inventory fetch so its state update lands
 *  inside act() even in tests that assert on static copy. */
async function settled() {
  await waitFor(() => expect(getSttStatus).toHaveBeenCalled());
  await waitFor(() => expect(screen.getByText("Advanced")).toBeInTheDocument());
}

function openAdvanced() {
  fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
}

function modelPicker() {
  return screen.queryByRole("button", { name: /^Voice recognition model:/ });
}

beforeEach(() => {
  getSttStatus.mockReset();
  deleteSttModel.mockReset();
  getSttStatus.mockResolvedValue(
    sttStatus({ configured: PARAKEET_V3, downloaded: [PARAKEET_V3, QWEN] }),
  );
  deleteSttModel.mockResolvedValue({ status: "deleted" });
});

afterEach(() => {
  cleanup();
});

describe("Automatic mode", () => {
  it("offers curated choices without enabling Manual", async () => {
    renderHome(makeConfig({ language: "de", stt_backend: "parakeet" }));
    await waitFor(() => expect(getSttStatus).toHaveBeenCalled());

    expect(modelPicker()).not.toBeNull();
    expect(screen.queryByText("Fastest")).toBeNull();
    fireEvent.click(modelPicker()!);
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.getByText("Fastest")).toBeInTheDocument();
    expect(screen.getByText("Best quality")).toBeInTheDocument();
    expect(modelPicker()).toHaveTextContent("NVIDIA Parakeet");
  });

  it("offers the manual toggle in Advanced, switched off", async () => {
    const { onSelectionModeChange } = renderHome(makeConfig());
    await settled();
    openAdvanced();

    expect(screen.getByText("Choose speech model manually")).toBeInTheDocument();
    expect(
      screen.getByText("Keep a compatible speech model when you change languages."),
    ).toBeInTheDocument();

    const toggle = screen.getByRole("switch", {
      name: "Choose speech model manually",
    });
    expect(toggle).toHaveAttribute("aria-checked", "false");

    fireEvent.click(toggle);
    expect(onSelectionModeChange).toHaveBeenCalledWith(true);
  });

  it("hides the Qwen language hint, which only applies to Manual Qwen", async () => {
    renderHome(makeConfig({ stt_backend: "qwen", language: "ja" }));
    await settled();
    openAdvanced();

    expect(screen.queryByText("Force selected language")).toBeNull();
  });
});

describe("Manual mode", () => {
  const manualDual = makeConfig({
    stt_model_selection_mode: "manual",
    language: "de",
    stt_backend: "parakeet",
    parakeet_model: PARAKEET_V3,
  });

  it("shows all compatible models in the picker", async () => {
    renderHome(manualDual);
    await waitFor(() => expect(getSttStatus).toHaveBeenCalled());

    const trigger = modelPicker();
    expect(trigger).not.toBeNull();
    expect(trigger?.getAttribute("aria-label")).toBe(
      "Voice recognition model: NVIDIA Parakeet",
    );
    openAdvanced();
    fireEvent.click(trigger!);
    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(options).toHaveLength(3);
    expect(options[2]).toHaveTextContent("Cohere Transcribe");
  });

  it("keeps a plain label when only one model serves the language", async () => {
    getSttStatus.mockResolvedValue(
      sttStatus({ configured: PARAKEET_V3, downloaded: [PARAKEET_V3, QWEN] }),
    );
    renderHome(
      makeConfig({
        stt_model_selection_mode: "manual",
        language: "bg",
        stt_backend: "parakeet",
        parakeet_model: PARAKEET_V3,
      }),
    );
    await waitFor(() => expect(getSttStatus).toHaveBeenCalled());

    expect(modelPicker()).toBeNull();
    expect(await screen.findByText("NVIDIA Parakeet")).toBeInTheDocument();
  });

  it("reports a picker choice to the host", async () => {
    const { onSttBackendChange } = renderHome(manualDual);
    await waitFor(() => expect(getSttStatus).toHaveBeenCalled());

    openAdvanced();
    fireEvent.click(screen.getByRole("button", { name: /^Voice recognition model:/ }));
    fireEvent.click(
      within(screen.getByRole("listbox")).getByRole("option", {
        name: /Qwen3 ASR/,
      }),
    );

    expect(onSttBackendChange).toHaveBeenCalledWith("qwen");
  });

  it("presents Cohere as a manual explicit-language model", async () => {
    getSttStatus.mockResolvedValue(
      sttStatus({
        configured: COHERE,
        downloaded: [PARAKEET_V3, QWEN, COHERE],
      }),
    );
    renderHome(
      makeConfig({
        stt_model_selection_mode: "manual",
        language: "de",
        stt_backend: "cohere",
      }),
    );
    await settled();

    expect(screen.getByText("Cohere Transcribe")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Cohere's speech-to-text model. Supports 14 languages, uses your selected language, and requires more memory.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Models vary in speed and transcription quality.")).toBeInTheDocument();

    openAdvanced();
    expect(
      screen.queryByRole("switch", { name: "Force selected language" }),
    ).toBeNull();
  });

  it("turning the toggle off reports the intent to the host", async () => {
    const { onSelectionModeChange } = renderHome(manualDual);
    await settled();
    openAdvanced();

    const toggle = screen.getByRole("switch", {
      name: "Choose speech model manually",
    });
    expect(toggle).toHaveAttribute("aria-checked", "true");

    fireEvent.click(toggle);
    expect(onSelectionModeChange).toHaveBeenCalledWith(false);
  });

  it("shows the Qwen language hint only while Qwen is manually selected", async () => {
    renderHome(manualDual);
    await settled();
    openAdvanced();
    expect(screen.queryByText("Force selected language")).toBeNull();

    cleanup();
    getSttStatus.mockResolvedValue(
      sttStatus({ configured: QWEN, downloaded: [PARAKEET_V3, QWEN] }),
    );
    renderHome(
      makeConfig({
        stt_model_selection_mode: "manual",
        language: "de",
        stt_backend: "qwen",
      }),
    );
    await settled();
    openAdvanced();
    expect(await screen.findByText("Force selected language")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Tell Qwen which language to listen for instead of detecting it.",
      ),
    ).toBeInTheDocument();
  });
});

describe("Model deletion from the picker", () => {
  const manualDual = makeConfig({
    stt_model_selection_mode: "manual",
    language: "de",
    stt_backend: "parakeet",
    parakeet_model: PARAKEET_V3,
  });

  async function openPicker() {
    await waitFor(() => expect(getSttStatus).toHaveBeenCalled());
    openAdvanced();
    fireEvent.click(screen.getByRole("button", { name: /^Voice recognition model:/ }));
    return screen.getByRole("listbox");
  }

  it("never offers the active model for deletion", async () => {
    renderHome(manualDual);
    const panel = await openPicker();

    expect(
      within(panel).queryByRole("button", { name: "Delete NVIDIA Parakeet" }),
    ).toBeNull();
    expect(
      within(panel).getByText("Current model: NVIDIA Parakeet"),
    ).toBeInTheDocument();
  });

  it("deletes a downloaded inactive model through the locked confirmation", async () => {
    const { onShowStatus } = renderHome(manualDual);
    const panel = await openPicker();

    fireEvent.click(
      within(panel).getByRole("button", { name: "Delete Qwen3 ASR" }),
    );

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Delete Qwen3 ASR?")).toBeInTheDocument();

    getSttStatus.mockResolvedValue(
      sttStatus({ configured: PARAKEET_V3, downloaded: [PARAKEET_V3] }),
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete model" }));

    await waitFor(() => expect(deleteSttModel).toHaveBeenCalledWith(QWEN));
    await waitFor(() =>
      expect(onShowStatus).toHaveBeenCalledWith("Qwen3 ASR deleted", false),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("explains a busy refusal without claiming the model was deleted", async () => {
    const { onShowStatus } = renderHome(manualDual);
    const panel = await openPicker();

    deleteSttModel.mockRejectedValue(
      new Error('409: {"status":"error","reason":"dictation_active"}'),
    );
    fireEvent.click(
      within(panel).getByRole("button", { name: "Delete Qwen3 ASR" }),
    );
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete model" }));

    await waitFor(() =>
      expect(onShowStatus).toHaveBeenCalledWith(
        "Finish your current dictation, then delete the model.",
        true,
      ),
    );
  });

  it("reports an unexplained failure with the locked recovery step", async () => {
    const { onShowStatus } = renderHome(manualDual);
    const panel = await openPicker();

    deleteSttModel.mockRejectedValue(new Error("500: boom"));
    fireEvent.click(
      within(panel).getByRole("button", { name: "Delete Qwen3 ASR" }),
    );
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete model" }));

    await waitFor(() =>
      expect(onShowStatus).toHaveBeenCalledWith(
        "Could not delete Qwen3 ASR. Try again.",
        true,
      ),
    );
  });

  it("offers a download instead of a trash for an absent model", async () => {
    getSttStatus.mockResolvedValue(
      sttStatus({ configured: PARAKEET_V3, downloaded: [PARAKEET_V3] }),
    );
    renderHome(manualDual);
    const panel = await openPicker();

    expect(panel.querySelector('[role="tooltip"]')).toHaveTextContent(
      "Download Qwen3 ASR",
    );
    expect(
      within(panel).queryByRole("button", { name: "Delete Qwen3 ASR" }),
    ).toBeNull();
  });
});

describe("Authoritative loaded model", () => {
  const manualDual = makeConfig({
    stt_model_selection_mode: "manual",
    language: "de",
    stt_backend: "parakeet",
    parakeet_model: PARAKEET_V3,
  });

  const configuredQwen = makeConfig({
    stt_model_selection_mode: "manual",
    language: "de",
    stt_backend: "qwen",
  });

  /** Config names Qwen, the process is still holding Parakeet. */
  function mismatched() {
    getSttStatus.mockResolvedValue(
      sttStatus({
        configured: QWEN,
        loaded: PARAKEET_V3,
        downloaded: [PARAKEET_V3, QWEN],
      }),
    );
  }

  async function pickerLabelled(name: string) {
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /^Voice recognition model:/ }),
      ).toHaveAttribute("aria-label", `Voice recognition model: ${name}`),
    );
  }

  // `stt_backend` / `parakeet_model` name the routing TARGET. Only
  // `loaded_artifact_id` says what the process is running, and they disagree
  // exactly when it matters.
  it("names the loaded model, not the configured one, when they disagree", async () => {
    mismatched();
    renderHome(configuredQwen);

    await pickerLabelled("NVIDIA Parakeet");
  });

  it("lets the user re-pick the configured model when it never loaded", async () => {
    mismatched();
    const { onSttBackendChange } = renderHome(configuredQwen);
    await pickerLabelled("NVIDIA Parakeet");
    openAdvanced();

    fireEvent.click(
      screen.getByRole("button", { name: /^Voice recognition model:/ }),
    );
    fireEvent.click(
      within(screen.getByRole("listbox")).getByRole("option", {
        name: /Qwen3 ASR/,
      }),
    );

    // Showing the configured target as current would make this click a no-op
    // and strand the user on a model they did not choose.
    expect(onSttBackendChange).toHaveBeenCalledWith("qwen");
  });

  it("protects both the configured and the loaded artifact from deletion", async () => {
    mismatched();
    renderHome(configuredQwen);
    await pickerLabelled("NVIDIA Parakeet");
    openAdvanced();

    fireEvent.click(
      screen.getByRole("button", { name: /^Voice recognition model:/ }),
    );
    const panel = screen.getByRole("listbox");

    expect(
      within(panel).queryByRole("button", { name: "Delete Qwen3 ASR" }),
    ).toBeNull();
    expect(
      within(panel).queryByRole("button", { name: "Delete NVIDIA Parakeet" }),
    ).toBeNull();
  });

  it("shows loading instead of calling the configured target current when nothing is loaded", async () => {
    getSttStatus.mockResolvedValue(
      sttStatus({ configured: QWEN, loaded: null, downloaded: [QWEN] }),
    );
    renderHome(
      makeConfig({
        stt_model_selection_mode: "manual",
        language: "ja",
        stt_backend: "qwen",
      }),
    );

    expect(await screen.findByText("Loading model...")).toBeInTheDocument();
    expect(screen.queryByText("Qwen3 ASR")).toBeNull();
    expect(modelPicker()).toBeNull();
  });

  it("uses the configured target only when authoritative status is unavailable", async () => {
    getSttStatus.mockRejectedValue(new Error("status unavailable"));
    renderHome(
      makeConfig({
        stt_model_selection_mode: "manual",
        language: "ja",
        stt_backend: "qwen",
      }),
    );

    expect(await screen.findByText("Qwen3 ASR")).toBeInTheDocument();
    await waitFor(() => expect(getSttStatus).toHaveBeenCalled());
  });

  it("re-reads the inventory when a settled operation leaves config unchanged", async () => {
    const { rerender } = renderHomeWithRerender(manualDual, {
      sttInventoryRevision: 0,
    });
    await waitFor(() => expect(getSttStatus).toHaveBeenCalledTimes(1));

    // A rollback restores every routing field, so config alone reports nothing.
    rerender(manualDual, { sttInventoryRevision: 1 });
    await waitFor(() => expect(getSttStatus).toHaveBeenCalledTimes(2));
  });
});

describe("Switch in flight", () => {
  it("disables the model controls and announces the target", async () => {
    renderHome(
      makeConfig({
        stt_model_selection_mode: "manual",
        language: "de",
        stt_backend: "parakeet",
        parakeet_model: PARAKEET_V3,
      }),
      { downloadInFlight: true, switchingToModel: "Qwen3 ASR" },
    );
    await waitFor(() => expect(getSttStatus).toHaveBeenCalled());

    expect(screen.getByText("Switching to Qwen3 ASR")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Voice recognition model:/ }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /^Dictation language:/ }),
    ).toBeDisabled();

    openAdvanced();
    expect(
      screen.getByRole("switch", { name: "Choose speech model manually" }),
    ).toHaveAttribute("aria-disabled", "true");
  });
});


describe("Home live snapshots", () => {
  it("leaves cold-start loading after the real model-ready event without navigation", async () => {
    let listener: ((event: AppEvent) => void) | undefined;
    const subscribe = (handler: (event: AppEvent) => void) => {
      listener = handler;
      return () => { listener = undefined; };
    };
    getSttStatus.mockResolvedValueOnce(sttStatus({
      configured: PARAKEET_V3, loaded: null, downloaded: [PARAKEET_V3],
    }));
    renderHome(makeConfig({ language: "de", stt_backend: "parakeet" }), { subscribe });
    await waitFor(() => expect(screen.getByText("Loading model...")).toBeInTheDocument());
    act(() => listener?.({ type: "model.loaded" }));
    await waitFor(() => expect(screen.queryByText("Loading model...")).toBeNull());
    expect(modelPicker()).not.toBeNull();
  });

  it("re-reads the loaded model after a background download swaps it", async () => {
    let listener: ((event: AppEvent) => void) | undefined;
    const subscribe = (handler: (event: AppEvent) => void) => {
      listener = handler;
      return () => { listener = undefined; };
    };
    getSttStatus.mockResolvedValue(sttStatus({
      configured: PARAKEET_V2, loaded: PARAKEET_V3, downloaded: [PARAKEET_V3],
    }));
    renderHome(makeConfig({ language: "en", parakeet_model: PARAKEET_V2 }), { subscribe });
    await waitFor(() => expect(screen.getByText("Multilingual")).toBeInTheDocument());
    getSttStatus.mockResolvedValue(sttStatus({
      configured: PARAKEET_V2, loaded: PARAKEET_V2, downloaded: [PARAKEET_V2, PARAKEET_V3],
    }));
    act(() => listener?.({ type: "download.complete", kind: "parakeet" } as AppEvent));
    await waitFor(() => expect(screen.queryByText("Multilingual")).toBeNull());
  });

  it("refreshes aggregate counts after completion and ignores an older pending response", async () => {
    let listener: ((event: AppEvent) => void) | undefined;
    let resolveOld!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { resolveOld = resolve; });
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementationOnce(() => pending);
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({
      total_transcriptions: 24, total_words: 210, total_recording_ms: 1000,
    }) } as Response);
    renderHome(makeConfig(), { subscribe: (handler) => {
      listener = handler;
      return () => { listener = undefined; };
    } });
    await settled();
    act(() => listener?.({ type: "transcription.result", text: "synthetic", duration_ms: 1000 }));
    await waitFor(() => expect(screen.getByText("210")).toBeInTheDocument());
    await act(async () => resolveOld({ ok: true, json: async () => ({
      total_transcriptions: 17, total_words: 140, total_recording_ms: 1000,
    }) } as Response));
    expect(screen.getByText("210")).toBeInTheDocument();
    expect(screen.queryByText("140")).toBeNull();
  });
});


it("does not let an older deletion read replace a newer model-ready snapshot", async () => {
  let listener: ((event: AppEvent) => void) | undefined;
  let resolveDeletion!: (status: SttStatusResponse) => void;
  const pendingDeletion = new Promise<SttStatusResponse>((resolve) => { resolveDeletion = resolve; });
  const config = makeConfig({ language: "de", stt_backend: "parakeet", stt_model_selection_mode: "manual" });
  renderHome(config, { subscribe: (handler) => {
    listener = handler;
    return () => { listener = undefined; };
  } });
  await settled();
  openAdvanced();
  fireEvent.click(modelPicker()!);
  fireEvent.click(within(screen.getByRole("listbox")).getByRole("button", { name: "Delete Qwen3 ASR" }));
  const dialog = await screen.findByRole("dialog");
  getSttStatus.mockImplementationOnce(() => pendingDeletion);
  fireEvent.click(within(dialog).getByRole("button", { name: "Delete model" }));
  await waitFor(() => expect(getSttStatus).toHaveBeenCalledTimes(2));
  getSttStatus.mockResolvedValueOnce(sttStatus({ configured: QWEN, loaded: QWEN, downloaded: [QWEN] }));
  act(() => listener?.({ type: "model.loaded" }));
  await waitFor(() => expect(modelPicker()).toHaveAttribute("aria-label", expect.stringContaining("Qwen3 ASR")));
  await act(async () => resolveDeletion(sttStatus({ configured: PARAKEET_V3, loaded: null, downloaded: [PARAKEET_V3] })));
  expect(screen.queryByText("Loading model...")).toBeNull();
  expect(modelPicker()).toHaveAttribute("aria-label", expect.stringContaining("Qwen3 ASR"));
});
