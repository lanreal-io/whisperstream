// @vitest-environment node

import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  tauri: true,
  invoke: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => mocks.tauri,
  invoke: mocks.invoke,
}));

import { applyWindowLifecycle } from "./windowLifecycle";

describe("authoritative platform window lifecycle", () => {
  beforeEach(() => {
    mocks.tauri = true;
    mocks.invoke.mockReset();
  });

  it("uses the native Tauri command without user-agent platform inference", async () => {
    mocks.invoke.mockResolvedValue(undefined);
    await expect(applyWindowLifecycle("review", "show")).resolves.toBe(true);
    expect(mocks.invoke).toHaveBeenCalledWith("apply_window_lifecycle", {
      generation: 1,
      role: "review",
      action: "show",
    });
  });

  it("falls back only outside Tauri and fails closed on a native rejection", async () => {
    mocks.tauri = false;
    await expect(applyWindowLifecycle("main", "show")).resolves.toBe(false);
    expect(mocks.invoke).not.toHaveBeenCalled();

    mocks.tauri = true;
    mocks.invoke.mockRejectedValue(new Error("unsupported"));
    await expect(applyWindowLifecycle("main", "show")).rejects.toThrow("unsupported");
  });
});
