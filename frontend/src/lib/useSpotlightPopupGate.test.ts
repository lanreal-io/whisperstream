import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useSpotlightPopupGate } from "./useSpotlightPopupGate";
import { WebviewWindow } from "@tauri-apps/api/webviewWindow";

const state = vi.hoisted(() => ({ native: true, visible: false, handlers: new Map<string, () => void>(), unlisten: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ isTauri: () => state.native }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async (name: string, handler: () => void) => { state.handlers.set(name, handler); return state.unlisten; }) }));
vi.mock("@tauri-apps/api/webviewWindow", () => ({ WebviewWindow: { getByLabel: vi.fn(async () => ({ isVisible: async () => state.visible })) } }));
afterEach(() => { cleanup(); vi.useRealTimers(); state.visible = false; state.native = true; state.handlers.clear(); vi.clearAllMocks(); });
it("defers from the existing show signal through native hide, then releases its observers", async () => {
  vi.useFakeTimers();
  const { result, unmount } = renderHook(useSpotlightPopupGate);
  await act(async () => {});
  expect(result.current).toBe(true);
  await act(async () => { state.handlers.get("popup-error:show")!(); });
  expect(result.current).toBe(false);
  state.visible = true;
  await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
  expect(result.current).toBe(false);
  state.visible = false;
  await act(async () => { state.handlers.get("popup-error:hidden")!(); });
  expect(result.current).toBe(true);
  unmount();
  await act(async () => {});
  expect(state.unlisten).toHaveBeenCalledTimes(12);
});
it("owns no queries or listeners when no spotlight offer is eligible", async () => {
  const { result } = renderHook(() => useSpotlightPopupGate(false));
  await act(async () => {});
  expect(result.current).toBe(true);
  expect(WebviewWindow.getByLabel).not.toHaveBeenCalled();
  expect(state.handlers.size).toBe(0);
});
it("does not repeatedly query while physical show/hidden events keep state current", async () => {
  vi.useFakeTimers();
  const { result } = renderHook(useSpotlightPopupGate);
  await act(async () => {});
  vi.mocked(WebviewWindow.getByLabel).mockClear();
  await act(async () => { state.handlers.get("popup-review:show")!(); state.handlers.get("popup-review:shown")!(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(14000); });
  expect(result.current).toBe(false);
  expect(WebviewWindow.getByLabel).not.toHaveBeenCalled();
  await act(async () => { state.handlers.get("popup-review:hidden")!(); });
  expect(result.current).toBe(true);
});
it("does not wait on native windows in a browser", () => {
  state.native = false;
  const { result } = renderHook(useSpotlightPopupGate);
  expect(result.current).toBe(true);
  expect(state.handlers.size).toBe(0);
});
