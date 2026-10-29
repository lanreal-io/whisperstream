import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ErrorPopup } from "./ErrorPopup";

vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({ listen: async () => () => {}, hide: vi.fn() }),
}));
vi.mock("@tauri-apps/api/event", () => ({
  listen: async () => () => {}, emit: vi.fn(),
}));
vi.mock("../components/PopupShell", () => ({
  PopupShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

describe("error popup fixture payload", () => {
  it("renders the supplied unavailable message rather than the code's retry copy", () => {
    render(<ErrorPopup initialPayload={{
      code: "audio_capture_failed",
      title: "Recording unavailable",
      message: "Audio capture could not finish safely. Quit and reopen Whisperstream before recording again.",
    }} />);
    expect(screen.getByText("Recording unavailable")).toBeTruthy();
    expect(screen.getByText(/Quit and reopen Whisperstream/)).toBeTruthy();
    expect(screen.queryByText("Couldn't start recording")).toBeNull();
    expect(screen.getByRole("button", { name: "Okay" })).toBeTruthy();
  });
});
