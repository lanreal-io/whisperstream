import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PermissionRestart } from "./PermissionRestart";

const native = vi.hoisted(() => ({ isTauri: vi.fn(), relaunch: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ isTauri: native.isTauri }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: native.relaunch }));
beforeEach(() => {
  vi.stubEnv("DEV", false);
  native.isTauri.mockReturnValue(true);
  native.relaunch.mockReset();
});
afterEach(() => { cleanup(); vi.unstubAllEnvs(); });

it("restarts only on an explicit click and disables repeated clicks", async () => {
  native.relaunch.mockImplementation(() => new Promise(() => {}));
  render(<PermissionRestart />);
  expect(native.relaunch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Restart app" }));
  expect(screen.getByRole("button", { name: "Restarting..." })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Restarting..." }));
  expect(native.relaunch).toHaveBeenCalledOnce();
});
it("provides a manual exit path and retry if native restart fails", async () => {
  native.relaunch.mockRejectedValue(new Error("unavailable"));
  render(<PermissionRestart />);
  fireEvent.click(screen.getByRole("button", { name: "Restart app" }));
  await screen.findByRole("alert");
  await waitFor(() => expect(screen.getByRole("button", { name: "Restart app" })).toBeEnabled());
});
it("does not restart only the shell in a source session with an external backend", () => {
  vi.stubEnv("DEV", true);
  render(<PermissionRestart />);
  expect(screen.getByText(/Stop the dev launcher/)).toBeTruthy();
  expect(screen.queryByRole("button")).toBeNull();
  expect(native.relaunch).not.toHaveBeenCalled();
});
it("gives manual instructions in a browser without invoking native IPC", () => {
  native.isTauri.mockReturnValue(false);
  render(<PermissionRestart />);
  expect(screen.getByText(/Quit Whisperstream from its app menu/)).toBeTruthy();
  expect(screen.queryByRole("button")).toBeNull();
});
it("honors a permission operation in progress", () => {
  render(<PermissionRestart disabled />);
  fireEvent.click(screen.getByRole("button", { name: "Restart app" }));
  expect(native.relaunch).not.toHaveBeenCalled();
});

it("keeps the packaged restart action compact inside the permission row", () => {
  render(<PermissionRestart compact />);
  expect(screen.getByRole("button", { name: "Restart app" })).toBeEnabled();
  expect(screen.queryByText(/Already enabled/)).toBeNull();
  expect(native.relaunch).not.toHaveBeenCalled();
});
