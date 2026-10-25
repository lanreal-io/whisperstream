import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getDevices } from "../lib/api/system";
import { type AppConfig } from "../lib/api/config";
import type { DailyUseCapabilityReport } from "../lib/dailyUseCapabilities";
import { Audio } from "./Audio";


vi.mock("../lib/api/system", async (loadOriginal) => {
  const original = await loadOriginal<typeof import("../lib/api/system")>();
  return {
    ...original,
    getDevices: vi.fn().mockReturnValue(new Promise(() => undefined)),
  };
});


const CONFIG = {
  audio_input_device: "",
  audio_feedback: true,
  audio_feedback_volume: 1,
  system_volume_mode: "off",
} as AppConfig;

const available: DailyUseCapabilityReport = {
  schema_version: 1,
  platform: "Darwin",
  capabilities: [
    {
      id: "audio-policy",
      state: "available",
      reason: "coreaudio-output-policy",
      presentation: "enable",
    },
  ],
};

const unsupported: DailyUseCapabilityReport = {
  schema_version: 1,
  platform: "Darwin",
  capabilities: [
    {
      id: "audio-policy",
      state: "unavailable",
      reason: "software-output-control-unsupported",
      presentation: "disable",
    },
  ],
};


afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const DEVICES = [
  {
    id: "macos-input-stable",
    name: "Studio Mic",
    channels: 1,
    sample_rate: 48_000,
    is_default: false,
  },
];

function platformReport(platform: string): DailyUseCapabilityReport {
  return { ...available, platform };
}


describe("Audio platform capability", () => {
  it("enables CoreAudio-backed Lower and Mute from the authoritative report", () => {
    const onConfigChange = vi.fn();
    render(
      <Audio
        config={CONFIG}
        onConfigChange={onConfigChange}
        capabilities={available}
      />,
    );

    const lower = screen.getByRole("button", { name: "Lower" });
    const mute = screen.getByRole("button", { name: "Mute" });
    expect(lower).toBeEnabled();
    expect(mute).toBeEnabled();
    fireEvent.click(lower);
    expect(onConfigChange).toHaveBeenCalledWith({ system_volume_mode: "reduce" });
  });

  it("disables mutation and explains an unsupported platform result", () => {
    const onConfigChange = vi.fn();
    render(
      <Audio
        config={CONFIG}
        onConfigChange={onConfigChange}
        capabilities={unsupported}
      />,
    );

    expect(screen.getByRole("button", { name: "Lower" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Mute" })).toBeDisabled();
    expect(
      screen.getAllByText(
        "Lowering and muting other audio is not supported on this platform.",
      ).length,
    ).toBeGreaterThan(0);
  });

  it("fails closed while capability hydration is unavailable", () => {
    render(
      <Audio config={CONFIG} onConfigChange={vi.fn()} capabilities={null} />,
    );

    expect(screen.getByRole("button", { name: "Lower" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Mute" })).toBeDisabled();
    expect(screen.getAllByText("Checking platform support...").length).toBeGreaterThan(0);
  });
});

describe("Audio input identity", () => {
  it("persists Mac selections as ID and presentation label", async () => {
    vi.mocked(getDevices).mockResolvedValueOnce(DEVICES);
    const onConfigChange = vi.fn();
    render(
      <Audio
        config={{
          ...CONFIG,
          audio_input_device_selection_mode: "opaque-id",
        }}
        onConfigChange={onConfigChange}
        capabilities={null}
      />,
    );

    await waitFor(() => expect(getDevices).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /Input device:/ }));
    fireEvent.click(await screen.findByRole("option", { name: "Studio Mic" }));

    expect(onConfigChange).toHaveBeenCalledWith({
      audio_input_device: "macos-input-stable",
      audio_input_device_label: "Studio Mic",
    });
  });

  it("preserves the byte-for-byte Windows name patch", async () => {
    vi.mocked(getDevices).mockResolvedValueOnce(DEVICES);
    const onConfigChange = vi.fn();
    render(
      <Audio
        config={CONFIG}
        onConfigChange={onConfigChange}
        capabilities={platformReport("Darwin")}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /Input device:/ }));
    fireEvent.click(await screen.findByRole("option", { name: "Studio Mic" }));

    expect(onConfigChange).toHaveBeenCalledWith({
      audio_input_device: "Studio Mic",
    });
  });

  it("shows a stale Mac label as unavailable without rendering its opaque ID", async () => {
    vi.mocked(getDevices).mockResolvedValueOnce([]);
    render(
      <Audio
        config={{
          ...CONFIG,
          audio_input_device: "macos-input-private-token",
          audio_input_device_label: "Travel Mic",
          audio_input_device_selection_mode: "opaque-id",
          audio_input_device_available: false,
        }}
        onConfigChange={vi.fn()}
        capabilities={platformReport("Darwin")}
      />,
    );

    expect(await screen.findByRole("button", {
      name: "Input device: Travel Mic. Unavailable",
    })).toBeInTheDocument();
    expect(screen.queryByText("macos-input-private-token")).not.toBeInTheDocument();
  });

  it("clears both Mac fields when system default is selected", async () => {
    vi.mocked(getDevices).mockResolvedValueOnce(DEVICES);
    const onConfigChange = vi.fn();
    render(
      <Audio
        config={{
          ...CONFIG,
          audio_input_device: "macos-input-stable",
          audio_input_device_label: "Studio Mic",
          audio_input_device_selection_mode: "opaque-id",
        }}
        onConfigChange={onConfigChange}
        capabilities={platformReport("Darwin")}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /Input device:/ }));
    fireEvent.click(await screen.findByRole("option", { name: "Use system default" }));

    expect(onConfigChange).toHaveBeenCalledWith({
      audio_input_device: "",
      audio_input_device_label: "",
    });
  });

  it("shows unknown rather than unavailable when Mac inventory loading fails", async () => {
    vi.mocked(getDevices).mockRejectedValueOnce(new Error("inventory failed"));
    render(
      <Audio
        config={{
          ...CONFIG,
          audio_input_device: "macos-input-private-token",
          audio_input_device_label: "Travel Mic",
          audio_input_device_selection_mode: "opaque-id",
          audio_input_device_available: false,
        }}
        onConfigChange={vi.fn()}
        capabilities={null}
      />,
    );

    expect(await screen.findByRole("button", {
      name: "Input device: Travel Mic. Availability unknown",
    })).toBeInTheDocument();
    expect(screen.queryByText("macos-input-private-token")).not.toBeInTheDocument();
    expect(screen.queryByText("Unavailable")).not.toBeInTheDocument();
  });
});
