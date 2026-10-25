import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Home } from "./Home";
import type { AppConfig } from "../lib/api/config";
import type { DailyUseCapabilityReport } from "../lib/dailyUseCapabilities";
import type { LaunchAtLoginStatus } from "../lib/autostart";

vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));

afterEach(cleanup);

const CONFIG = {
  language: "en",
  stt_backend: "parakeet",
  parakeet_model: "nemo-parakeet-tdt-0.6b-v3",
  model_storage_dir: "",
  data_dir: "",
  theme: "light",
  clipboard_behavior: true,
  system_volume_mode: "off",
  lockdown_mode: false,
  setup_completed: true,
} as AppConfig;

const CAPABILITIES: DailyUseCapabilityReport = {
  schema_version: 1,
  platform: "Darwin",
  capabilities: [
    {
      id: "launch-at-login",
      state: "available",
      reason: "smappservice-main-app",
      presentation: "enable",
    },
    {
      id: "model-lifecycle",
      state: "available",
      reason: "fluidaudio-verified",
      presentation: "enable",
    },
    {
      id: "qwen-stt",
      state: "unavailable",
      reason: "adapter-unavailable",
      presentation: "disable",
    },
  ],
};

function renderStartup(
  status: LaunchAtLoginStatus,
  capabilities: DailyUseCapabilityReport = CAPABILITIES,
  onStartupChange = vi.fn(),
) {
  render(
    <Home
      config={CONFIG}
      startupStatus={status}
      onStartupChange={onStartupChange}
      onConfigChange={() => {}}
      subscribe={() => () => {}}
      onRequestOpenLanguagePopup={() => {}}
      onRequestLanguageChange={() => {}}
      onSelectionModeChange={() => {}}
      onSttBackendChange={() => {}}
      onShowStatus={() => {}}
      downloadInFlight={false}
      onConfigReload={() => {}}
      onConfigReplace={() => {}}
      capabilities={capabilities}
      connected={false}
    />,
  );
}

describe("Run on Startup truth", () => {
  it("does not collapse requires-approval into enabled or disabled", () => {
    renderStartup({ state: "requires-approval", failure: null });
    expect(
      screen.getByText("Approval is required in System Settings before startup can be enabled."),
    ).toBeTruthy();
    const toggle = screen.getByRole("switch", { name: "Run on Startup" });
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(toggle.getAttribute("aria-disabled")).toBe("true");
  });

  it("disables the control when native state is unavailable", () => {
    renderStartup({ state: "unavailable", failure: "native-failure" });
    expect(screen.getByText("Startup status is unavailable right now.")).toBeTruthy();
    expect(
      screen.getByRole("switch", { name: "Run on Startup" }).getAttribute("aria-disabled"),
    ).toBe("true");
  });

  it("allows explicit Mac registration when the native record is missing", () => {
    const change = vi.fn();
    renderStartup({ state: "not-found", failure: "not-found" }, CAPABILITIES, change);
    const toggle = screen.getByRole("switch", { name: "Run on Startup" });
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(toggle.getAttribute("aria-disabled")).not.toBe("true");
    expect(change).not.toHaveBeenCalled();
    fireEvent.click(toggle);
    expect(change).toHaveBeenCalledWith(true);
  });

  it("does not offer missing-record recovery when the platform capability is unavailable", () => {
    renderStartup({ state: "not-found", failure: "not-found" }, {
      ...CAPABILITIES,
      capabilities: CAPABILITIES.capabilities.map(c => c.id === "launch-at-login"
        ? { ...c, state: "unavailable", presentation: "disable" } : c),
    });
    expect(screen.getByRole("switch", { name: "Run on Startup" }).getAttribute("aria-disabled")).toBe("true");
  });

  it("keeps the permission recovery surface absent on Windows", () => {
    renderStartup(
      { state: "enabled", failure: null },
      { ...CAPABILITIES, platform: "Windows" },
    );
    expect(screen.queryByText("Permissions")).toBeNull();
  });
});
