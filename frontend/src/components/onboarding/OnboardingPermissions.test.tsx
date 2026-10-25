import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WINDOWS_CAPABILITIES } from "../../test/platformFixtures";
import { makeConfig } from "../../pages/__tests__/homeConfigFixture";
import type { PermissionKind, PermissionState } from "../../lib/api/system";
import { resetPermissionRequestAttemptsForTests, usePermissionRecovery } from "../../lib/usePermissionRecovery";
import { OnboardingOverlay } from "./OnboardingOverlay";
import { StepPermissions } from "./StepPermissions";

const api = vi.hoisted(() => ({
  getPermissions: vi.fn(), requestPermission: vi.fn(), openPermissionSettings: vi.fn(),
  getShortcutReadiness: vi.fn(), reconcileShortcuts: vi.fn(), saveConfig: vi.fn(),
}));
vi.mock("../../lib/api/models", async importOriginal => ({
    ...await importOriginal<typeof import("../../lib/api/models")>(),
    getSttStatus: vi.fn(async () => null),
    startDownload: vi.fn(async () => {}),
    getModelsDiskFree: vi.fn(async () => ({ free_mb: 20000, required_mb: 1000, sufficient: true })),
  }));
vi.mock("../../lib/api/system", async importOriginal => ({
    ...await importOriginal<typeof import("../../lib/api/system")>(),
    getPermissions: api.getPermissions,
    requestPermission: api.requestPermission,
    openPermissionSettings: api.openPermissionSettings,
    getShortcutReadiness: api.getShortcutReadiness,
    reconcileShortcuts: api.reconcileShortcuts,
  }));
vi.mock("../../lib/api/config", async importOriginal => ({
    ...await importOriginal<typeof import("../../lib/api/config")>(),
    saveConfig: api.saveConfig,
  }));
vi.mock("../TitleBar", () => ({ TitleBar: () => null }));
vi.mock("./StepShortcut", () => ({ StepShortcut: () => <div>Shared shortcut step</div> }));
vi.mock("./StepTest", () => ({ StepTest: () => <div>Shared test step</div> }));

const states: Record<PermissionKind, PermissionState> = {
  microphone: "not-determined", accessibility: "unknown", "input-monitoring": "unknown",
};
const props = (platform = "Darwin") => ({
  config: makeConfig({ setup_completed: false }), modelLoaded: false,
  send: vi.fn(() => true), subscribe: () => () => {}, connected: true,
  onConfigChange: vi.fn(), onFinish: vi.fn(), capabilities: { ...WINDOWS_CAPABILITIES, platform },
});
beforeEach(() => {
  vi.clearAllMocks();
  resetPermissionRequestAttemptsForTests();
  states.microphone = "not-determined"; states.accessibility = "unknown";
  api.saveConfig.mockResolvedValue(makeConfig());
  api.getPermissions.mockImplementation(async () => ({ schema_version: 1, platform: "macos", permissions:
    Object.entries(states).map(([kind, state]) => ({ kind, state, query_supported: true, request_supported: true })) }));
  api.requestPermission.mockImplementation(async (kind: PermissionKind) => {
    const before = states[kind]; states[kind] = "granted";
    return { schema_version: 1, kind, action: "request", status: "completed", before, after: "granted", failure: null, relaunch_required: false };
  });
  api.getShortcutReadiness.mockResolvedValue({ state: "failed", failure: "native_error" });
  api.reconcileShortcuts.mockResolvedValue({ state: "ready", failure: null });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

async function reachPermissions() {
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  await screen.findByRole("heading", { name: "Allow access for dictation" });
  await screen.findByRole("button", { name: "Allow accessibility" });
}

it("adds only the Mac step, keeps independent actions gated, and suppresses injection only in shared Test", async () => {
  const p = props(); render(<OnboardingOverlay {...p} />);
  expect(screen.getByText("Step 1 of 5")).toBeTruthy();
  await reachPermissions();
  expect(screen.getByText("Step 2 of 5")).toBeTruthy();
  expect(screen.queryByText("Input Monitoring")).toBeNull();
  fireEvent.keyDown(window, { key: "Enter" });
  expect(screen.queryByText("Shared shortcut step")).toBeNull();
  const access = screen.getByRole("button", { name: "Allow accessibility" });
  fireEvent.keyDown(access, { key: "Enter" });
  expect(api.requestPermission).not.toHaveBeenCalled(); // native button activation owns this key
  fireEvent.click(access);
  await screen.findByRole("region", { name: "Accessibility: Allowed" });
  expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Allow microphone" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
  expect(api.reconcileShortcuts).toHaveBeenCalledOnce();
  expect(p.send).not.toHaveBeenCalledWith({ type: "inject.suppress", suppressed: true });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  await screen.findByText("Shared shortcut step");
  expect(screen.getByText("Step 3 of 5")).toBeTruthy();
  expect(p.send).not.toHaveBeenCalledWith({ type: "inject.suppress", suppressed: true });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  await screen.findByText("Shared test step");
  expect(screen.getByText("Step 4 of 5")).toBeTruthy();
  expect(p.send).toHaveBeenCalledWith({ type: "inject.suppress", suppressed: true });
});

it("keeps Windows on four steps (three shared plus Enhance) without querying permissions", async () => {
  const p = props("Windows"); render(<OnboardingOverlay {...p} />);
  expect(screen.getByText("Step 1 of 4")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  await screen.findByText("Shared shortcut step");
  expect(screen.getByText("Step 2 of 4")).toBeTruthy();
  expect(api.getPermissions).not.toHaveBeenCalled();
  expect(api.reconcileShortcuts).not.toHaveBeenCalled();
});

it("invalidates grants on disconnect and keeps failed skip save in the shared flow", async () => {
  const p = props(); const view = render(<OnboardingOverlay {...p} />);
  await reachPermissions();
  fireEvent.click(screen.getByRole("button", { name: "Allow microphone" }));
  await screen.findByRole("region", { name: "Microphone: Allowed" });
  view.rerender(<OnboardingOverlay {...p} connected={false} />);
  expect(screen.queryByRole("region", { name: "Microphone: Allowed" })).toBeNull();
  expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
  api.saveConfig.mockRejectedValueOnce(new Error("synthetic save failure"));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Skip & finish" })));
  expect(p.onFinish).not.toHaveBeenCalled();
  expect(screen.getByRole("alert").textContent).toContain("Couldn't finish setup");
  api.saveConfig.mockResolvedValue(makeConfig());
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Try again" })));
  expect(p.onFinish).toHaveBeenCalledOnce();
});

it("offers restart guidance for a stale Accessibility grant without unlocking Continue", async () => {
  states.microphone = "granted";
  api.requestPermission.mockResolvedValue({ schema_version: 1, kind: "accessibility", action: "request", status: "completed", before: "unknown", after: "unknown", failure: null, relaunch_required: false });
  api.openPermissionSettings.mockResolvedValue({ schema_version: 1, kind: "accessibility", action: "open-settings", status: "completed", before: "unknown", after: "unknown", failure: null, relaunch_required: true });
  render(<OnboardingOverlay {...props()} />);
  await reachPermissions();
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Allow accessibility" })); });
  screen.getByText("Already enabled? Restart to refresh access.");
  expect(screen.queryByRole("button", { name: "Check again for Accessibility" })).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
  expect(api.reconcileShortcuts).not.toHaveBeenCalled();
  states.accessibility = "granted";
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(screen.getByRole("region", { name: "Accessibility: Allowed" })).toBeTruthy();
  expect(screen.queryByText("Already enabled? Restart to refresh access.")).toBeNull();
});


it("keeps the permission row stable during a slow background poll", async () => {
  states.microphone = "granted";
  api.requestPermission.mockResolvedValue({ schema_version: 1, kind: "accessibility", action: "request", status: "completed", before: "unknown", after: "unknown", failure: null, relaunch_required: false });
  function Fixture() {
    const recovery = usePermissionRecovery(true);
    return <><StepPermissions recovery={recovery} connected /><button disabled={!recovery.ready}>Continue</button></>;
  }
  render(<Fixture />);
  await screen.findByRole("button", { name: "Allow accessibility" });
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Allow accessibility" })); });
  const settings = screen.getByRole("button", { name: "Open System Settings" });
  let finishPoll!: (value: unknown) => void;
  api.getPermissions.mockImplementationOnce(() => new Promise(resolve => { finishPoll = resolve; }));
  await act(async () => { await vi.advanceTimersByTimeAsync(250); });
  expect(finishPoll).toBeTypeOf("function");
  expect(screen.queryByText("Checking permissions...")).toBeNull();
  expect(settings).toBeEnabled();
  expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
  await act(async () => { finishPoll({ schema_version: 1, platform: "macos", permissions:
    Object.entries(states).map(([kind, state]) => ({ kind, state, query_supported: true, request_supported: true })) }); });
  expect(screen.queryByText("Checking permissions...")).toBeNull();
  expect(settings).toBeEnabled();
});
