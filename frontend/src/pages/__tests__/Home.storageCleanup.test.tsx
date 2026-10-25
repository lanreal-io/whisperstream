import { WINDOWS_CAPABILITIES } from "../../test/platformFixtures";
/**
 * Vitest coverage for the bulk unused-model storage cleanup under Model
 * storage location (Task 3.3).
 *
 * Locks the contract the locked scope and design require:
 *   - the action exists only when the AUTHORITATIVE inventory reports
 *     reclaimable bytes, so it never advertises work that is not there;
 *   - its confirmation names the exact stored artifacts, including the
 *     Parakeet variants the picker hides behind one choice, and the total it
 *     frees;
 *   - the configured and loaded artifacts are never in that list;
 *   - the outcome is read back from the backend rather than asserted from an
 *     optimistic mutation, so the action only disappears once the inventory
 *     says it should;
 *   - every locked success / empty / busy / failure string is used.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import {
  render,
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
import type { SttBackend } from "../../lib/languages";

const PARAKEET_V2: SttArtifactId = "nemo-parakeet-tdt-0.6b-v2";
const PARAKEET_V3: SttArtifactId = "nemo-parakeet-tdt-0.6b-v3";
const QWEN: SttArtifactId = "qwen3-asr-0.6b-int8";
const COHERE: SttArtifactId = "cohere-transcribe-03-2026-int8";

const MIB = 1024 ** 2;

const getSttStatus = vi.fn<() => Promise<SttStatusResponse>>();
const deleteUnusedSttModels = vi.fn<() => Promise<unknown>>();

vi.mock("../../lib/api/models", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/models")>();
  return {
    ...actual,
    getSttStatus: (...args: []) => getSttStatus(...args),
    deleteUnusedSttModels: (...args: []) => deleteUnusedSttModels(...args),
  };
});

// Home fetches /api/stats on mount. Keep that request pending: these tests
// exercise storage cleanup, not the stats payload.
vi.stubGlobal(
  "fetch",
  vi.fn(() => new Promise(() => {})),
);

/** Authoritative exact-artifact inventory, shaped like the backend's. Sizes
 *  are per-artifact so `unused_size_bytes` is a real sum rather than a
 *  hand-written constant that could disagree with the list. */
function sttStatus({
  configured,
  loaded = configured,
  sizes,
}: {
  configured: SttArtifactId;
  loaded?: SttArtifactId | null;
  sizes: Partial<Record<SttArtifactId, number>>;
}): SttStatusResponse {
  const artifacts = ([PARAKEET_V3, PARAKEET_V2, QWEN, COHERE] as SttArtifactId[]).map(
    (id) => ({
      id,
      backend: (id === QWEN ? "qwen" : id === COHERE ? "cohere" : "parakeet") as SttBackend,
      downloaded: (sizes[id] ?? 0) > 0,
      active: id === loaded,
      size_bytes: sizes[id] ?? 0,
    }),
  );
  return {
    configured_artifact_id: configured,
    loaded_artifact_id: loaded ?? null,
    artifacts,
    unused_size_bytes: artifacts
      .filter((a) => a.id !== configured && a.id !== loaded)
      .reduce((total, a) => total + a.size_bytes, 0),
    parakeet: {
      downloaded: true,
      active: configured === PARAKEET_V2 || configured === PARAKEET_V3,
    },
    qwen: { downloaded: (sizes[QWEN] ?? 0) > 0, active: configured === QWEN },
    cohere: {
      downloaded: (sizes[COHERE] ?? 0) > 0,
      active: configured === COHERE,
    },
  };
}

const NOOP = () => {};

function renderHome(
  config: AppConfig = makeConfig(),
  overrides: { downloadInFlight?: boolean } = {},
) {
  const onShowStatus = vi.fn();
  render(
    <Home
      config={config}
      startupStatus={{ state: "disabled", failure: null }}
      capabilities={WINDOWS_CAPABILITIES}
      onStartupChange={NOOP}
      onConfigChange={NOOP}
      subscribe={() => () => {}}
      onRequestOpenLanguagePopup={NOOP}
      onRequestLanguageChange={NOOP}
      downloadInFlight={overrides.downloadInFlight ?? false}
      switchingToModel={null}
      onSelectionModeChange={NOOP}
      onSttBackendChange={NOOP}
      onShowStatus={onShowStatus}
      onConfigReload={NOOP}
      onConfigReplace={NOOP}
    />,
  );
  return { onShowStatus };
}

/** The cleanup action lives in the Advanced section, beneath the storage row. */
async function openAdvanced() {
  await waitFor(() => expect(getSttStatus).toHaveBeenCalled());
  await waitFor(() => expect(screen.getByText("Advanced")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
}

function cleanupAction() {
  return screen.queryByRole("button", { name: "Delete unused models" });
}

beforeEach(() => {
  getSttStatus.mockReset();
  deleteUnusedSttModels.mockReset();
  getSttStatus.mockResolvedValue(
    sttStatus({
      configured: PARAKEET_V3,
      sizes: { [PARAKEET_V3]: 600 * MIB, [PARAKEET_V2]: 590 * MIB, [QWEN]: 937 * MIB },
    }),
  );
  deleteUnusedSttModels.mockResolvedValue({
    status: "deleted",
    removed_model_ids: [PARAKEET_V2, QWEN],
  });
});

afterEach(() => {
  cleanup();
});

describe("Cleanup action visibility", () => {
  it("stays hidden when nothing is reclaimable", async () => {
    getSttStatus.mockResolvedValue(
      sttStatus({ configured: PARAKEET_V3, sizes: { [PARAKEET_V3]: 600 * MIB } }),
    );
    renderHome();
    await openAdvanced();

    expect(screen.getByText("Model storage location")).toBeInTheDocument();
    expect(cleanupAction()).toBeNull();
  });

  it("appears once the inventory reports unused bytes", async () => {
    renderHome();
    await openAdvanced();

    await waitFor(() => expect(cleanupAction()).not.toBeNull());
  });

  it("is frozen while a model switch owns the recognizer", async () => {
    renderHome(makeConfig(), { downloadInFlight: true });
    await openAdvanced();

    await waitFor(() => expect(cleanupAction()).not.toBeNull());
    expect(cleanupAction()).toBeDisabled();
  });
});

describe("Bulk deletion confirmation", () => {
  async function openConfirmation() {
    await openAdvanced();
    await waitFor(() => expect(cleanupAction()).not.toBeNull());
    fireEvent.click(cleanupAction()!);
    return screen.findByRole("dialog");
  }

  it("names the exact unused artifacts and the total it frees", async () => {
    renderHome();
    const dialog = await openConfirmation();

    expect(within(dialog).getByText("Delete unused models?")).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "This deletes these unused models and frees 1.5 GB:",
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["NVIDIA Parakeet (English)", "Qwen3 ASR"]);
  });

  it("never lists the configured or the loaded artifact", async () => {
    // Mid-transition: configured has moved to Qwen while Parakeet English is
    // still the loaded recognizer. Both are protected, so only multilingual
    // Parakeet is reclaimable.
    getSttStatus.mockResolvedValue(
      sttStatus({
        configured: QWEN,
        loaded: PARAKEET_V2,
        sizes: {
          [PARAKEET_V3]: 600 * MIB,
          [PARAKEET_V2]: 590 * MIB,
          [QWEN]: 937 * MIB,
        },
      }),
    );
    renderHome();
    const dialog = await openConfirmation();

    expect(
      within(dialog)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["NVIDIA Parakeet (Multilingual)"]);
  });

  it("keeping the models closes without calling the backend", async () => {
    renderHome();
    const dialog = await openConfirmation();

    fireEvent.click(within(dialog).getByRole("button", { name: "Keep models" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(deleteUnusedSttModels).not.toHaveBeenCalled();
  });
});

describe("Bulk deletion outcomes", () => {
  async function confirmDeletion() {
    await openAdvanced();
    await waitFor(() => expect(cleanupAction()).not.toBeNull());
    fireEvent.click(cleanupAction()!);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Delete unused models" }),
    );
  }

  it("reports success only after the refreshed inventory agrees", async () => {
    const { onShowStatus } = renderHome();
    await openAdvanced();
    await waitFor(() => expect(cleanupAction()).not.toBeNull());

    // The refresh that follows the delete is what the UI believes, not the
    // click.
    getSttStatus.mockResolvedValue(
      sttStatus({ configured: PARAKEET_V3, sizes: { [PARAKEET_V3]: 600 * MIB } }),
    );
    fireEvent.click(cleanupAction()!);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Delete unused models" }),
    );

    await waitFor(() => expect(deleteUnusedSttModels).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(onShowStatus).toHaveBeenCalledWith("Unused models deleted", false),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(cleanupAction()).toBeNull());
  });

  it("does not claim a deletion when the backend removed nothing", async () => {
    const { onShowStatus } = renderHome();
    deleteUnusedSttModels.mockResolvedValue({
      status: "deleted",
      removed_model_ids: [],
    });
    getSttStatus.mockResolvedValue(
      sttStatus({ configured: PARAKEET_V3, sizes: { [PARAKEET_V3]: 600 * MIB } }),
    );

    await confirmDeletion();

    await waitFor(() =>
      expect(onShowStatus).toHaveBeenCalledWith(
        "There are no unused models to delete.",
        false,
      ),
    );
  });

  it("reports a partial cleanup from the refreshed inventory", async () => {
    const { onShowStatus } = renderHome();
    deleteUnusedSttModels.mockResolvedValue({
      status: "deleted",
      removed_model_ids: [PARAKEET_V2],
    });
    // Qwen survived (a locked file), so the action must remain.
    getSttStatus.mockResolvedValue(
      sttStatus({
        configured: PARAKEET_V3,
        sizes: { [PARAKEET_V3]: 600 * MIB, [QWEN]: 937 * MIB },
      }),
    );

    await confirmDeletion();

    await waitFor(() =>
      expect(onShowStatus).toHaveBeenCalledWith(
        "Could not delete all unused models. Try again.",
        true,
      ),
    );
    await waitFor(() => expect(cleanupAction()).not.toBeNull());
  });

  it("explains a dictation-busy refusal without claiming a deletion", async () => {
    const { onShowStatus } = renderHome();
    deleteUnusedSttModels.mockRejectedValue(
      new Error('409: {"status":"error","reason":"dictation_active"}'),
    );

    await confirmDeletion();

    await waitFor(() =>
      expect(onShowStatus).toHaveBeenCalledWith(
        "Finish your current dictation, then delete unused models.",
        true,
      ),
    );
    await waitFor(() => expect(cleanupAction()).not.toBeNull());
  });

  it("explains an operation-busy refusal", async () => {
    const { onShowStatus } = renderHome();
    deleteUnusedSttModels.mockRejectedValue(
      new Error('409: {"status":"error","reason":"operation_busy"}'),
    );

    await confirmDeletion();

    await waitFor(() =>
      expect(onShowStatus).toHaveBeenCalledWith(
        "Whisperstream is already updating a model. Try again when it finishes.",
        true,
      ),
    );
  });

  it("reports an unexplained failure with the locked recovery step", async () => {
    const { onShowStatus } = renderHome();
    deleteUnusedSttModels.mockRejectedValue(new Error("500: boom"));

    await confirmDeletion();

    await waitFor(() =>
      expect(onShowStatus).toHaveBeenCalledWith(
        "Could not delete all unused models. Try again.",
        true,
      ),
    );
  });
});
