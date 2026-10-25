import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PermissionActionOutcome, PermissionKind, PermissionReport, PermissionState } from "../lib/api/system";
import {
  parsePermissionReport,
  resetPermissionRequestAttemptsForTests,
} from "../lib/usePermissionRecovery";
import { PermissionsSummary } from "./PermissionsSummary";

const mocks = vi.hoisted(() => ({
  getPermissions: vi.fn(),
  getShortcutReadiness: vi.fn(),
  reconcileShortcuts: vi.fn(),
  requestPermission: vi.fn(),
  openPermissionSettings: vi.fn(),
  invoke: vi.fn(),
  isVisible: vi.fn(),
  unlisten: vi.fn(),
  focusHandler: null as null | ((event: { payload: boolean }) => void),
}));

vi.mock("../lib/api/system", async (importOriginal) => ({
    ...(await importOriginal<typeof import("../lib/api/system")>()),
    getPermissions: mocks.getPermissions,
    getShortcutReadiness: mocks.getShortcutReadiness,
    reconcileShortcuts: mocks.reconcileShortcuts,
    requestPermission: mocks.requestPermission,
    openPermissionSettings: mocks.openPermissionSettings,
  }));

vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));
vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({
    isVisible: mocks.isVisible,
    onFocusChanged: vi.fn(async (handler: (event: { payload: boolean }) => void) => {
      mocks.focusHandler = handler;
      return mocks.unlisten;
    }),
  }),
}));

const KINDS: PermissionKind[] = ["microphone", "accessibility", "input-monitoring"];

function report(
  testedKind: PermissionKind = "microphone",
  state: PermissionState = "granted",
  querySupported = true,
  requestSupported = true,
): PermissionReport {
  return {
    schema_version: 1,
    platform: "macos",
    permissions: KINDS.map((kind) => ({
      kind,
      state: kind === testedKind ? state : "granted",
      query_supported: kind === testedKind ? querySupported : true,
      request_supported: kind === testedKind ? requestSupported : true,
    })),
  };
}

function outcome(
  kind: PermissionKind,
  status: PermissionActionOutcome["status"],
  after: PermissionState,
  action: PermissionActionOutcome["action"] = "request",
): PermissionActionOutcome {
  const failed = status === "failed" || status === "unsupported";
  return {
    schema_version: 1,
    kind,
    action,
    status,
    before: after,
    after,
    failure: failed ? "native-error" : null,
    relaunch_required: true,
  };
}

beforeEach(() => {
  resetPermissionRequestAttemptsForTests();
  mocks.getPermissions.mockReset();
  mocks.getShortcutReadiness.mockReset().mockResolvedValue({ state: "ready", failure: null });
  mocks.reconcileShortcuts.mockReset().mockResolvedValue({ state: "ready", failure: null });
  mocks.requestPermission.mockReset();
  mocks.openPermissionSettings.mockReset();
  mocks.invoke.mockReset().mockResolvedValue(true);
  mocks.isVisible.mockReset().mockResolvedValue(true);
  mocks.unlisten.mockReset();
  mocks.focusHandler = null;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("permission report validation", () => {
  it("canonicalizes the complete schema-v1 report and rejects malformed input", () => {
    const reversed = report();
    reversed.permissions.reverse();
    expect(parsePermissionReport(reversed).permissions.map((item) => item.kind)).toEqual(KINDS);
    expect(() => parsePermissionReport({ ...reversed, schema_version: 2 })).toThrow();
    expect(() => parsePermissionReport({ ...reversed, permissions: reversed.permissions.slice(1) })).toThrow();
    expect(() => parsePermissionReport({ ...reversed, platform: "windows" })).toThrow();
  });
});

describe("PermissionsSummary presentation", () => {
  it("keeps the healthy Mac to one quiet green line", async () => {
    mocks.getPermissions.mockResolvedValue(report());
    render(<PermissionsSummary connected />);
    expect(await screen.findByText("All permissions allowed")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Check again" })).toBeTruthy();
    expect(screen.queryByText("Microphone")).toBeNull();
    expect(screen.getByRole("region", { name: "Permissions" }).children).toHaveLength(1);
  });

  it.each([
    ["not-determined", true, "Not allowed yet", "Allow"],
    ["denied", true, "Not allowed", "Open System Settings"],
    ["restricted", true, "Managed by this Mac", null],
    ["unknown", true, "Not confirmed", "Allow"],
    ["unknown", false, "Can't check access", "Open System Settings"],
    ["unsupported", false, "Unavailable", null],
  ] as const)("renders %s without inventing permission truth", async (state, querySupported, label, action) => {
    mocks.getPermissions.mockResolvedValue(report("accessibility", state, querySupported));
    render(<PermissionsSummary connected />);
    expect(await screen.findByText(label)).toBeTruthy();
    if (action) expect(screen.getByRole("button", { name: action })).toBeTruthy();
    else {
      expect(screen.queryByRole("button", { name: "Allow" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Open System Settings" })).toBeNull();
    }
  });

  it("fails closed with one prompt-free retry", async () => {
    mocks.getPermissions.mockRejectedValue(new Error("offline"));
    render(<PermissionsSummary connected />);
    expect(await screen.findByText("Couldn't confirm access")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    expect(mocks.requestPermission).not.toHaveBeenCalled();
    expect(mocks.openPermissionSettings).not.toHaveBeenCalled();
  });
});

describe("permission recovery lifecycle", () => {
  it("shares the mount GET across a StrictMode effect replay", async () => {
    mocks.getPermissions.mockResolvedValue(report());
    render(<StrictMode><PermissionsSummary connected /></StrictMode>);
    expect(await screen.findByText("All permissions allowed")).toBeTruthy();
    expect(mocks.getPermissions).toHaveBeenCalledTimes(1);
  });

  it("keeps Manual Recheck GET-only", async () => {
    mocks.getPermissions.mockResolvedValue(report());
    render(<PermissionsSummary connected />);
    const recheck = await screen.findByRole("button", { name: "Check again" });
    fireEvent.click(recheck);
    await waitFor(() => expect(mocks.getPermissions).toHaveBeenCalledTimes(2));
    expect(mocks.requestPermission).not.toHaveBeenCalled();
    expect(mocks.openPermissionSettings).not.toHaveBeenCalled();
  });

  it("makes one POST on a rapid Allow double-click and keeps polling while Settings is open", async () => {
    mocks.getPermissions.mockResolvedValue(report("accessibility", "unknown"));
    mocks.requestPermission.mockResolvedValue(outcome("accessibility", "unchanged", "unknown"));
    render(<PermissionsSummary connected />);
    const allow = await screen.findByRole("button", { name: "Allow" });
    vi.useFakeTimers();
    fireEvent.click(allow);
    fireEvent.click(allow);
    await act(async () => { await Promise.resolve(); });
    act(() => mocks.focusHandler?.({ payload: false }));
    await act(async () => { await vi.advanceTimersByTimeAsync(11500); });
    expect(mocks.requestPermission).toHaveBeenCalledTimes(1);
    expect(mocks.requestPermission).toHaveBeenCalledWith("accessibility");
    expect(mocks.getPermissions).toHaveBeenCalledTimes(8);
    expect(mocks.openPermissionSettings).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Open System Settings" })).toBeTruthy();
  });

  it("stops settlement as soon as the requested permission becomes allowed", async () => {
    mocks.getPermissions
      .mockResolvedValueOnce(report("accessibility", "unknown"))
      .mockResolvedValue(report("accessibility", "granted"));
    mocks.requestPermission.mockResolvedValue(outcome("accessibility", "unchanged", "unknown"));
    render(<PermissionsSummary connected />);
    fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
    vi.useFakeTimers();
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.runAllTimersAsync(); });
    expect(mocks.getPermissions).toHaveBeenCalledTimes(2);
    expect(screen.getByText("All permissions allowed")).toBeTruthy();
  });

  it("rejects an older Manual Recheck response after a newer action outcome", async () => {
    let resolveStale!: (value: PermissionReport) => void;
    mocks.getPermissions
      .mockResolvedValueOnce(report("accessibility", "unknown"))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveStale = resolve; }))
      .mockResolvedValue(report());
    mocks.requestPermission.mockResolvedValue(outcome("accessibility", "completed", "granted"));
    render(<PermissionsSummary connected />);
    await screen.findByText("Not confirmed");
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    fireEvent.click(screen.getByRole("button", { name: "Allow" }));
    await act(async () => { resolveStale(report("accessibility", "unknown")); });
    expect(await screen.findByText("All permissions allowed")).toBeTruthy();
    expect(screen.getByText("All permissions allowed")).toBeTruthy();
    expect(screen.queryByText("Not confirmed")).toBeNull();
  });

  it("retains the once-per-process request latch across Home remounts", async () => {
    mocks.getPermissions.mockResolvedValue(report("accessibility", "unknown"));
    mocks.requestPermission.mockResolvedValue(outcome("accessibility", "declined", "denied"));
    const first = render(<PermissionsSummary connected />);
    fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
    expect(await screen.findByRole("button", { name: "Open System Settings" })).toBeTruthy();
    first.unmount();
    render(<PermissionsSummary connected />);
    expect(await screen.findByRole("button", { name: "Open System Settings" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Allow" })).toBeNull();
    expect(mocks.requestPermission).toHaveBeenCalledTimes(1);
  });

  it("cancels settlement on disconnect and performs one GET on reconnect", async () => {
    mocks.getPermissions.mockResolvedValue(report("accessibility", "unknown"));
    mocks.requestPermission.mockResolvedValue(outcome("accessibility", "unchanged", "unknown"));
    const view = render(<PermissionsSummary connected />);
    fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
    vi.useFakeTimers();
    await act(async () => { await Promise.resolve(); });
    view.rerender(<PermissionsSummary connected={false} />);
    await act(async () => { await vi.runAllTimersAsync(); });
    expect(mocks.getPermissions).toHaveBeenCalledTimes(1);
    view.rerender(<PermissionsSummary connected />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(mocks.getPermissions).toHaveBeenCalledTimes(2);
  });

  it("cancels settlement and its listener exactly on unmount", async () => {
    mocks.getPermissions.mockResolvedValue(report("accessibility", "unknown"));
    mocks.requestPermission.mockResolvedValue(outcome("accessibility", "unchanged", "unknown"));
    const view = render(<PermissionsSummary connected />);
    fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
    vi.useFakeTimers();
    await act(async () => { await Promise.resolve(); });
    view.unmount();
    await act(async () => { await vi.runAllTimersAsync(); });
    expect(mocks.getPermissions).toHaveBeenCalledTimes(1);
    expect(mocks.unlisten).toHaveBeenCalledTimes(1);
  });

  it("coalesces real reactivation and close/reopen while ignoring owned-window focus churn", async () => {
    mocks.getPermissions.mockResolvedValue(report());
    render(<PermissionsSummary connected />);
    await screen.findByText("All permissions allowed");
    await waitFor(() => expect(mocks.focusHandler).not.toBeNull());
    vi.useFakeTimers();

    mocks.invoke.mockResolvedValueOnce(true);
    mocks.isVisible.mockResolvedValueOnce(true);
    act(() => mocks.focusHandler?.({ payload: false }));
    await act(async () => { await vi.runAllTimersAsync(); });
    act(() => mocks.focusHandler?.({ payload: true }));
    await act(async () => { await Promise.resolve(); });
    expect(mocks.getPermissions).toHaveBeenCalledTimes(1);

    mocks.invoke.mockResolvedValueOnce(false);
    mocks.isVisible.mockResolvedValueOnce(true);
    act(() => mocks.focusHandler?.({ payload: false }));
    await act(async () => { await vi.runAllTimersAsync(); });
    act(() => mocks.focusHandler?.({ payload: true }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(mocks.getPermissions).toHaveBeenCalledTimes(2);

    mocks.invoke.mockResolvedValueOnce(true);
    mocks.isVisible.mockResolvedValueOnce(false);
    act(() => mocks.focusHandler?.({ payload: false }));
    await act(async () => { await vi.runAllTimersAsync(); });
    act(() => mocks.focusHandler?.({ payload: true }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(mocks.getPermissions).toHaveBeenCalledTimes(3);
    expect(mocks.requestPermission).not.toHaveBeenCalled();
    expect(mocks.openPermissionSettings).not.toHaveBeenCalled();
  });

  it("shows navigation guidance after recovery and ignores relaunch metadata", async () => {
    mocks.getPermissions.mockResolvedValue(report("microphone", "denied"));
    mocks.openPermissionSettings.mockResolvedValue(
      outcome("microphone", "unchanged", "denied", "open-settings"),
    );
    render(<PermissionsSummary connected />);
    fireEvent.click(await screen.findByRole("button", { name: "Open System Settings" }));
    expect(await screen.findByText(/In System Settings, open Privacy/)).toBeTruthy();
    expect(screen.queryByText(/relaunch|restart/i)).toBeNull();
    expect(mocks.openPermissionSettings).toHaveBeenCalledTimes(1);
  });

  it("fails closed when an action endpoint returns the wrong action contract", async () => {
    mocks.getPermissions.mockResolvedValue(report("accessibility", "unknown"));
    mocks.requestPermission.mockResolvedValue(
      outcome("accessibility", "completed", "granted", "open-settings"),
    );
    render(<PermissionsSummary connected />);
    fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
    expect(await screen.findByText("macOS didn't respond. Open System Settings to allow access.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open System Settings" })).toBeTruthy();
    expect(screen.queryByText("All permissions allowed")).toBeNull();
  });
});

describe("dictation readiness", () => {
  it("does not require Input Monitoring for the active listener", async () => {
    mocks.getPermissions.mockResolvedValue(report("input-monitoring", "unknown", false, false));
    render(<PermissionsSummary connected />);
    expect(await screen.findByText("All permissions allowed")).toBeTruthy();
    expect(screen.queryByText("Input Monitoring")).toBeNull();
  });

  it("requires listener confirmation and bounds automatic failed reconciliation", async () => {
    mocks.getPermissions.mockResolvedValue(report());
    mocks.getShortcutReadiness.mockResolvedValue({ state: "failed", failure: "native_error" });
    mocks.reconcileShortcuts.mockResolvedValueOnce({ state: "failed", failure: "native_error" })
      .mockResolvedValueOnce({ state: "ready", failure: null });
    render(<PermissionsSummary connected />);
    expect(await screen.findByText("Access is allowed, but your shortcut isn't ready.")).toBeTruthy();
    expect(screen.queryByText("All permissions allowed")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(mocks.getShortcutReadiness).toHaveBeenCalledTimes(2));
    expect(mocks.reconcileShortcuts).toHaveBeenCalledTimes(1);
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("All permissions allowed")).toBeTruthy();
    expect(mocks.reconcileShortcuts).toHaveBeenCalledTimes(2);
    expect(mocks.requestPermission).not.toHaveBeenCalled();
  });

  it("removes stale approval on disconnect and rejects the old in-flight response", async () => {
    let resolve!: (value: PermissionReport) => void;
    mocks.getPermissions.mockResolvedValueOnce(report())
      .mockImplementationOnce(() => new Promise(res => { resolve = res; }));
    const view = render(<PermissionsSummary connected />);
    await screen.findByText("All permissions allowed");
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    view.rerender(<PermissionsSummary connected={false} />);
    await act(async () => resolve(report()));
    expect(screen.queryByText("All permissions allowed")).toBeNull();
    expect(screen.getByText("Whisperstream isn't responding. Access can't be confirmed yet.")).toBeTruthy();
  });

  it("serializes permission actions across rows", async () => {
    const missing = report("microphone", "not-determined");
    missing.permissions[1].state = "unknown";
    mocks.getPermissions.mockResolvedValue(missing);
    mocks.requestPermission.mockImplementation(() => new Promise(() => {}));
    render(<PermissionsSummary connected />);
    const buttons = await screen.findAllByRole("button", { name: "Allow" });
    fireEvent.click(buttons[0]);
    fireEvent.click(buttons[1]);
    expect(mocks.requestPermission).toHaveBeenCalledTimes(1);
  });
});

it("offers a bounded retry when a held gesture postpones reconciliation", async () => {
  mocks.getPermissions.mockResolvedValue(report());
  mocks.getShortcutReadiness.mockResolvedValue({ state: "pending", failure: "busy" });
  mocks.reconcileShortcuts.mockResolvedValueOnce({ state: "pending", failure: "busy" })
    .mockResolvedValueOnce({ state: "ready", failure: null });
  render(<PermissionsSummary connected />);
  expect(await screen.findByText("Getting your shortcut ready...")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("All permissions allowed")).toBeTruthy();
  expect(mocks.reconcileShortcuts).toHaveBeenCalledTimes(2);
});

it("polls busy readiness passively until the held gesture is released", async () => {
  mocks.getPermissions.mockResolvedValue(report());
  mocks.getShortcutReadiness.mockResolvedValueOnce({ state: "pending", failure: "busy" })
    .mockResolvedValue({ state: "ready", failure: null });
  mocks.reconcileShortcuts.mockResolvedValue({ state: "pending", failure: "busy" });
  vi.useFakeTimers();
  render(<PermissionsSummary connected />);
  for (let index = 0; index < 6; index += 1) {
    await act(async () => { await vi.runAllTimersAsync(); });
  }
  expect(screen.getByText("All permissions allowed")).toBeTruthy();
  expect(mocks.getShortcutReadiness).toHaveBeenCalledTimes(2);
  expect(mocks.reconcileShortcuts).toHaveBeenCalledTimes(1);
});

it("bounds passive busy polling and retains manual recovery", async () => {
  mocks.getPermissions.mockResolvedValue(report());
  mocks.getShortcutReadiness.mockResolvedValue({ state: "pending", failure: "busy" });
  mocks.reconcileShortcuts.mockResolvedValue({ state: "pending", failure: "busy" });
  vi.useFakeTimers();
  render(<PermissionsSummary connected />);
  for (let index = 0; index < 6; index += 1) {
    await act(async () => { await vi.runAllTimersAsync(); });
  }
  expect(mocks.getShortcutReadiness).toHaveBeenCalledTimes(5);
  expect(mocks.reconcileShortcuts).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
});

it("does not accept grants read before a revocation during reconciliation", async () => {
  mocks.getPermissions.mockResolvedValueOnce(report()).mockResolvedValue(report("accessibility", "denied"));
  mocks.getShortcutReadiness.mockResolvedValue({ state: "failed", failure: "native_error" });
  mocks.reconcileShortcuts.mockResolvedValue({ state: "ready", failure: null });
  render(<PermissionsSummary connected />);
  expect(await screen.findByText("Not allowed")).toBeTruthy();
  expect(screen.queryByText("All permissions allowed")).toBeNull();
  expect(mocks.getPermissions).toHaveBeenCalledTimes(2);
});
