// @vitest-environment node

import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  tauri: true,
  invoke: vi.fn(),
  getStartup: vi.fn(),
  setStartup: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => mocks.tauri,
  invoke: mocks.invoke,
}));
vi.mock("./api/system", () => ({
    getStartup: mocks.getStartup,
    setStartup: mocks.setStartup,
  }));

import { applyStartupChange, getStartupState, setStartupEnabled } from "./autostart";

describe("typed launch-at-login state", () => {
  beforeEach(() => {
    mocks.tauri = true;
    mocks.invoke.mockReset();
    mocks.getStartup.mockReset();
    mocks.setStartup.mockReset();
  });

  it("hydrates enabled and requires-approval states through the native command", async () => {
    mocks.invoke.mockResolvedValueOnce({ state: "requires-approval", failure: null });
    await expect(getStartupState()).resolves.toEqual({
      state: "requires-approval",
      failure: null,
    });
    expect(mocks.invoke).toHaveBeenCalledWith("window_launch_at_login_state");
  });

  it("returns the authoritative post-mutation state", async () => {
    mocks.invoke.mockResolvedValueOnce({ state: "enabled", failure: null });
    await expect(setStartupEnabled(true)).resolves.toEqual({
      state: "enabled",
      failure: null,
    });
    expect(mocks.invoke).toHaveBeenCalledWith("set_window_launch_at_login", {
      enabled: true,
    });
  });

  it("returns unsupported instead of a successful non-Tauri no-op", async () => {
    mocks.tauri = false;
    mocks.getStartup.mockResolvedValue({ enabled: false, supported: false });
    mocks.setStartup.mockResolvedValue({ enabled: false, supported: false });
    await expect(getStartupState()).resolves.toEqual({
      state: "unsupported",
      failure: "unsupported",
    });
    await expect(setStartupEnabled(true)).resolves.toEqual({
      state: "unsupported",
      failure: "unsupported",
    });
  });

  it("preserves the supported Windows browser fallback", async () => {
    mocks.tauri = false;
    mocks.getStartup.mockResolvedValue({ enabled: false, supported: true });
    mocks.setStartup.mockResolvedValue({ enabled: true, supported: true });
    await expect(getStartupState()).resolves.toEqual({
      state: "disabled",
      failure: null,
    });
    await expect(setStartupEnabled(true)).resolves.toEqual({
      state: "enabled",
      failure: null,
    });
  });

  it("rolls optimistic state back on a thrown or typed native failure", async () => {
    const previous = { state: "disabled", failure: null } as const;
    await expect(
      applyStartupChange(previous, true, async () => {
        throw new Error("native failure");
      }),
    ).resolves.toMatchObject({ status: previous, failed: true });
    await expect(
      applyStartupChange(previous, true, async () => ({
        state: "unavailable",
        failure: "native-failure",
      })),
    ).resolves.toMatchObject({ status: previous, failed: true });
  });
});
