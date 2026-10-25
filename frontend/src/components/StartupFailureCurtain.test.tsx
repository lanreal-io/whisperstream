import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { StartupFailureCurtain } from "./StartupFailureCurtain";

const mocks = vi.hoisted(() => ({ relaunch: vi.fn(), native: vi.fn(() => true) }));
vi.mock("@tauri-apps/api/core", () => ({ isTauri: mocks.native }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: mocks.relaunch }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("offers retry and reports an unsuccessful native restart", async () => {
  const retry = vi.fn();
  mocks.relaunch.mockRejectedValueOnce(new Error("not available"));
  render(<StartupFailureCurtain onRetry={retry} />);
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(retry).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "Restart app" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Quit Whisperstream");
  expect(mocks.relaunch).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "Restart app" })).toBeEnabled();
});
