import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getDevices } from "../../lib/api/system";
import { MicPicker } from "./MicPicker";


vi.mock("../../lib/api/system", async (loadOriginal) => {
  const original = await loadOriginal<typeof import("../../lib/api/system")>();
  return {
    ...original,
    getDevices: vi.fn(),
  };
});


const DEVICE = {
  id: "macos-input-stable",
  name: "Studio Mic",
  channels: 1,
  sample_rate: 48_000,
  is_default: false,
};


afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});


describe("onboarding microphone identity", () => {
  it("persists Mac selections as ID and label", async () => {
    vi.mocked(getDevices).mockResolvedValueOnce([DEVICE]);
    const onChange = vi.fn();
    render(
      <MicPicker
        value=""
        savedLabel=""
        selectionMode="opaque-id"
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Microphone:/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Studio Mic" }));

    expect(onChange).toHaveBeenCalledWith({
      audio_input_device: "macos-input-stable",
      audio_input_device_label: "Studio Mic",
    });
  });

  it("preserves the exact Windows name-only patch", async () => {
    vi.mocked(getDevices).mockResolvedValueOnce([DEVICE]);
    const onChange = vi.fn();
    render(
      <MicPicker
        value=""
        savedLabel=""
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Microphone:/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Studio Mic" }));

    expect(onChange).toHaveBeenCalledWith({ audio_input_device: "Studio Mic" });
  });

  it("shows stale saved state without exposing the opaque ID", () => {
    render(
      <MicPicker
        value="macos-input-private-token"
        savedLabel="Travel Mic"
        savedAvailable={false}
        selectionMode="opaque-id"
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText("Travel Mic")).toBeInTheDocument();
    expect(screen.getByText(/Unavailable/)).toBeInTheDocument();
    expect(screen.queryByText("macos-input-private-token")).not.toBeInTheDocument();
  });

  it("clears both Mac fields when Default is selected", async () => {
    vi.mocked(getDevices).mockResolvedValueOnce([DEVICE]);
    const onChange = vi.fn();
    render(
      <MicPicker
        value={DEVICE.id}
        savedLabel={DEVICE.name}
        savedAvailable
        selectionMode="opaque-id"
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Microphone:/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Default" }));

    expect(onChange).toHaveBeenCalledWith({
      audio_input_device: "",
      audio_input_device_label: "",
    });
  });

  it("shows unknown rather than unavailable when lazy inventory loading fails", async () => {
    vi.mocked(getDevices).mockRejectedValueOnce(new Error("inventory failed"));
    render(
      <MicPicker
        value="macos-input-private-token"
        savedLabel="Travel Mic"
        savedAvailable={false}
        selectionMode="opaque-id"
        onChange={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Microphone:/ }));
    expect(await screen.findByText("Couldn't load microphones.")).toBeInTheDocument();
    expect(screen.getByText(/Availability unknown/)).toBeInTheDocument();
    expect(screen.queryByText(/\(Unavailable\)/)).not.toBeInTheDocument();
    expect(screen.queryByText("macos-input-private-token")).not.toBeInTheDocument();
  });
});
