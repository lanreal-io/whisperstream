import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Home } from "../Home";
import type { DailyUseCapabilityReport } from "../../lib/dailyUseCapabilities";
import { makeConfig } from "./homeConfigFixture";

vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));

afterEach(cleanup);

const V2 = "nemo-parakeet-tdt-0.6b-v2";
const V3 = "nemo-parakeet-tdt-0.6b-v3";

const CAPABILITIES: DailyUseCapabilityReport = {
  schema_version: 1,
  platform: "Darwin",
  capabilities: [
    {
      id: "model-lifecycle",
      state: "available",
      reason: "fluidaudio-verified",
      presentation: "enable",
    },
  ],
};

// The config the backend returns after language normalization: English routes
// to Parakeet v2 (sherpa worker on Mac), every other Parakeet language to v3.
function renderHome(language: string, parakeetModel: string) {
  const config = makeConfig({
    language,
    stt_backend: "parakeet",
    stt_model_selection_mode: "automatic",
    parakeet_model: parakeetModel,
  });
  render(
    <Home
      config={config}
      startupStatus={{ state: "disabled", failure: null }}
      onStartupChange={() => {}}
      onConfigChange={() => {}}
      subscribe={() => () => {}}
      onRequestOpenLanguagePopup={() => {}}
      onRequestLanguageChange={() => {}}
      onSelectionModeChange={() => {}}
      onSttBackendChange={() => {}}
      onShowStatus={() => {}}
      // Onboarding's background download holds the displayed family at its
      // initial value, so this exposes what Home seeds it from.
      downloadInFlight={true}
      onConfigReload={() => {}}
      onConfigReplace={() => {}}
      capabilities={CAPABILITIES}
      connected={false}
    />,
  );
}

describe("Home Parakeet model on Mac", () => {
  it("follows the backend's English v2 selection", () => {
    renderHome("en", V2);
    expect(screen.getByText("NVIDIA Parakeet")).toBeTruthy();
    expect(screen.queryByText("Multilingual")).toBeNull();
  });

  it("shows multilingual v3 for a non-English Parakeet language", () => {
    renderHome("de", V3);
    expect(screen.getByText("Multilingual")).toBeTruthy();
  });
});
