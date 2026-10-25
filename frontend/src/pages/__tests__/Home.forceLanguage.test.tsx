/**
 * Vitest coverage for the Qwen "Force selected language" advanced toggle.
 *
 * Locks the gating contract Home.tsx must honor. The rule tightened in the
 * model-selection round: the hint is only ever effective on Manual + Qwen
 * (scope.md), and routing clears the stored value on every other exit, so the
 * row is ABSENT rather than present-but-disabled everywhere else. A disabled
 * row would advertise a setting that cannot apply and would still be there
 * after the value it controls had been cleared underneath it.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { Home } from "../Home";
import { makeConfig } from "./homeConfigFixture";
import type { AppConfig } from "../../lib/api/config";
import type { DailyUseCapabilityReport } from "../../lib/dailyUseCapabilities";

const CAPABILITIES: DailyUseCapabilityReport = {
  schema_version: 1,
  platform: "Windows",
  capabilities: [
    { id: "launch-at-login", state: "available", reason: "implemented", presentation: "enable" },
    { id: "model-lifecycle", state: "available", reason: "implemented", presentation: "enable" },
    { id: "qwen-stt", state: "available", reason: "implemented", presentation: "enable" },
  ],
};
const MAC_CAPABILITIES: DailyUseCapabilityReport = {
  schema_version: 1,
  platform: "Darwin",
  capabilities: [
    { id: "launch-at-login", state: "available", reason: "smappservice-main-app", presentation: "enable" },
    { id: "model-lifecycle", state: "available", reason: "fluidaudio-verified", presentation: "enable" },
    { id: "qwen-stt", state: "unavailable", reason: "adapter-unavailable", presentation: "disable" },
  ],
};

// Home fetches /api/stats and the STT artifact inventory on mount. Keep both
// requests pending: these tests exercise static settings copy and controls.
vi.stubGlobal(
  "fetch",
  vi.fn(() => new Promise(() => {})),
);

afterEach(() => {
  cleanup();
});

const NOOP = () => {};

function renderHome(
  config: AppConfig,
  onConfigChange = NOOP,
  capabilities = CAPABILITIES,
) {
  return render(
    <Home
      config={config}
      startupStatus={{ state: "disabled", failure: null }}
      onStartupChange={NOOP}
      onConfigChange={onConfigChange}
      subscribe={() => () => {}}
      onRequestOpenLanguagePopup={NOOP}
      onRequestLanguageChange={NOOP}
      downloadInFlight={false}
      onSelectionModeChange={NOOP}
      onSttBackendChange={NOOP}
      onShowStatus={NOOP}
      onConfigReload={NOOP}
      onConfigReplace={NOOP}
      capabilities={capabilities}
    />,
  );
}

function openAdvanced() {
  fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
}

describe("Force selected language toggle", () => {
  it("keeps the Mac FluidAudio path private behind the native storage picker", () => {
    renderHome(
      makeConfig({
        stt_backend: "parakeet",
        data_dir: "/private/owner/app-data",
        model_storage_dir: "/private/owner/custom-models",
      }),
      NOOP,
      MAC_CAPABILITIES,
    );
    openAdvanced();
    expect(screen.getAllByText("Managed by Whisperstream").length).toBeGreaterThan(0);
    expect(screen.queryByText("/private/owner/custom-models")).toBeNull();
    expect(screen.getByRole("button", { name: /browse for model storage/i })).toBeTruthy();
  });

  it("is absent while Parakeet is manually selected", () => {
    renderHome(
      makeConfig({
        stt_model_selection_mode: "manual",
        stt_backend: "parakeet",
      }),
    );
    openAdvanced();

    expect(screen.queryByText("Force selected language")).toBeNull();
    // No switch is disabled: the manual-selection toggle is live and the hint
    // is not rendered at all.
    const disabled = screen
      .getAllByRole("switch")
      .filter((el) => el.getAttribute("aria-disabled") === "true");
    expect(disabled.length).toBe(0);
  });

  it("is absent in Automatic even when Qwen is the active model", () => {
    renderHome(
      makeConfig({ stt_model_selection_mode: "automatic", stt_backend: "qwen" }),
    );
    openAdvanced();

    expect(screen.queryByText("Force selected language")).toBeNull();
  });

  it("is available while Qwen is manually selected", () => {
    const onConfigChange = vi.fn();
    renderHome(
      makeConfig({ stt_model_selection_mode: "manual", stt_backend: "qwen" }),
      onConfigChange,
    );
    openAdvanced();

    const toggle = screen.getByRole("switch", {
      name: "Force selected language",
    });
    expect(toggle.getAttribute("aria-disabled")).toBeNull();

    fireEvent.click(toggle);
    expect(onConfigChange).toHaveBeenCalledWith({ qwen_force_language: true });
  });

  it("shows Qwen as active after switching to a dual-support language", () => {
    renderHome(
      makeConfig({
        stt_model_selection_mode: "manual",
        stt_backend: "qwen",
        language: "de",
        qwen_force_language: false,
      }),
    );

    expect(screen.getByText("Qwen3 ASR")).toBeTruthy();
    expect(screen.queryByText("NVIDIA Parakeet")).toBeNull();

    openAdvanced();
    expect(screen.getByText("Force selected language")).toBeTruthy();
    const disabled = screen
      .getAllByRole("switch")
      .filter((el) => el.getAttribute("aria-disabled") === "true");
    expect(disabled.length).toBe(0);
  });
});

describe("Lockdown standing copy", () => {
  it("uses the lean scoped status without a normal certification disclaimer", () => {
    renderHome(makeConfig({ lockdown_mode: true }));

    expect(
      screen.getByText(
        "On-device transcription and cleanup. Saved history locked.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Designed to support HIPAA-conscious workflows."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/not a HIPAA certification/i)).not.toBeInTheDocument();
  });
});
