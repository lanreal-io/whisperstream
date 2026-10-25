import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RECONNECT_BASE_MS, STABLE_CONNECTION_MS, SYSTEM_WAKE_EVENT, useAppSocket } from "./connection";

const tauri = vi.hoisted(() => ({
  native: false,
  wake: null as (() => void) | null,
  unlisten: vi.fn(),
}));
vi.mock("@tauri-apps/api/core", () => ({ isTauri: () => tauri.native }));
vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(async (event: string, handler: () => void) => {
    if (event === SYSTEM_WAKE_EVENT) tauri.wake = handler;
    return tauri.unlisten;
  }),
}));

class TestSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static instances: TestSocket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn(() => { this.readyState = 3; this.onclose?.(); });
  constructor(readonly url: string) { TestSocket.instances.push(this); }
  open() { this.readyState = 1; this.onopen?.(); }
  receive(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) }); }
}

afterEach(() => { tauri.native = false; tauri.wake = null; tauri.unlisten.mockClear(); cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); TestSocket.instances = []; });
function setup() { vi.stubGlobal("WebSocket", TestSocket); return renderHook(() => useAppSocket()); }

describe("socket connection ownership", () => {
  it("opens a transport only while enabled", () => {
    vi.stubGlobal("WebSocket", TestSocket);
    const { result, rerender, unmount } = renderHook(({ enabled }) => useAppSocket(enabled), { initialProps: { enabled: false } });
    expect(TestSocket.instances).toHaveLength(0);
    expect(result.current.connected).toBe(false);
    rerender({ enabled: true });
    act(() => TestSocket.instances[0].open());
    expect(result.current.connected).toBe(true);
    rerender({ enabled: false });
    expect(TestSocket.instances[0].close).toHaveBeenCalledOnce();
    expect(result.current.connected).toBe(false);
    unmount();
  });

  it("rejects late messages and opens from a retired connection", () => {
    vi.useFakeTimers();
    const { result } = setup();
    const listener = vi.fn();
    result.current.subscribe(listener);
    const retired = TestSocket.instances[0];
    act(() => retired.close());
    act(() => vi.advanceTimersByTime(RECONNECT_BASE_MS));
    act(() => retired.open());
    act(() => retired.receive({ type: "recording", active: true }));
    expect(result.current.connected).toBe(false);
    expect(listener).not.toHaveBeenCalled();
    act(() => TestSocket.instances[1].open());
    expect(result.current.connected).toBe(true);
  });

  it("owns one connection per hook and releases its own transport", () => {
    const first = setup();
    const second = setup();
    expect(TestSocket.instances).toHaveLength(2);
    first.unmount();
    expect(TestSocket.instances[0].close).toHaveBeenCalledOnce();
    expect(TestSocket.instances[1].close).not.toHaveBeenCalled();
    second.unmount();
  });

  it("validates agent payloads before delivery and removes subscriptions", () => {
    const { result } = setup();
    const listener = vi.fn();
    const unsubscribe = result.current.subscribe(listener);
    act(() => TestSocket.instances[0].receive({ type: "agent.response_queue.snapshot", active: { text: "sensitive", session_id: "bad" } }));
    expect(listener).not.toHaveBeenCalled();
    act(() => TestSocket.instances[0].receive({ type: "recording", active: true }));
    expect(listener).toHaveBeenCalledOnce();
    unsubscribe();
    act(() => TestSocket.instances[0].receive({ type: "recording", active: false }));
    expect(listener).toHaveBeenCalledOnce();
  });

  it("delivers only well-formed screen-reader status events", () => {
    const { result } = setup();
    const listener = vi.fn();
    result.current.subscribe(listener);
    act(() => TestSocket.instances[0].receive({ type: "accessibility.screen_reader", active: "yes" }));
    act(() => TestSocket.instances[0].receive({ type: "accessibility.screen_reader" }));
    expect(listener).not.toHaveBeenCalled();
    act(() => TestSocket.instances[0].receive({ type: "accessibility.screen_reader", active: true, extra: 1 }));
    expect(listener).toHaveBeenCalledExactlyOnceWith({ type: "accessibility.screen_reader", active: true });
  });

  it("reconnects without replaying payloads and cancels pending reconnect on cleanup", () => {
    vi.useFakeTimers();
    const { result, unmount } = setup();
    const listener = vi.fn();
    result.current.subscribe(listener);
    act(() => TestSocket.instances[0].open());
    expect(result.current.connected).toBe(true);
    act(() => TestSocket.instances[0].close());
    expect(result.current.connected).toBe(false);
    expect(result.current.reconnectAttempt).toBe(1);
    act(() => vi.advanceTimersByTime(1000));
    expect(TestSocket.instances).toHaveLength(2);
    act(() => TestSocket.instances[1].open());
    expect(result.current.everConnected).toBe(true);
    expect(result.current.reconnectAttempt).toBe(1);
    act(() => vi.advanceTimersByTime(STABLE_CONNECTION_MS));
    expect(result.current.reconnectAttempt).toBe(0);
    expect(listener).not.toHaveBeenCalled();
    act(() => TestSocket.instances[1].close());
    unmount();
    act(() => vi.advanceTimersByTime(30000));
    expect(TestSocket.instances).toHaveLength(2);
  });

  it("keeps backing off when the server accepts and immediately closes", () => {
    vi.useFakeTimers();
    const { result, unmount } = setup();
    act(() => TestSocket.instances[0].open());
    act(() => TestSocket.instances[0].close());
    expect(result.current.reconnectAttempt).toBe(1);
    let expectedDelay = RECONNECT_BASE_MS;
    for (let attempt = 1; attempt <= 4; attempt += 1) {
      const before = TestSocket.instances.length;
      act(() => vi.advanceTimersByTime(expectedDelay - 1));
      expect(TestSocket.instances).toHaveLength(before);
      act(() => vi.advanceTimersByTime(1));
      expect(TestSocket.instances).toHaveLength(before + 1);
      const socket = TestSocket.instances[before];
      act(() => socket.open());
      expect(result.current.connected).toBe(true);
      act(() => vi.advanceTimersByTime(100));
      act(() => socket.close());
      expect(result.current.reconnectAttempt).toBe(attempt + 1);
      expectedDelay *= 2;
    }
    unmount();
  });

  it("needs the full stable period before the backoff resets", () => {
    vi.useFakeTimers();
    const { result, unmount } = setup();
    act(() => TestSocket.instances[0].close());
    act(() => vi.advanceTimersByTime(RECONNECT_BASE_MS));
    act(() => TestSocket.instances[1].open());
    act(() => vi.advanceTimersByTime(STABLE_CONNECTION_MS - 1));
    expect(result.current.reconnectAttempt).toBe(1);
    act(() => TestSocket.instances[1].close());
    expect(result.current.reconnectAttempt).toBe(2);
    act(() => vi.advanceTimersByTime(RECONNECT_BASE_MS * 2 - 1));
    expect(TestSocket.instances).toHaveLength(2);
    act(() => vi.advanceTimersByTime(1));
    expect(TestSocket.instances).toHaveLength(3);
    unmount();
  });
});

describe("socket immediate reconnect", () => {
  function waitingOnBackoff() {
    vi.useFakeTimers();
    const hook = setup();
    act(() => TestSocket.instances[0].open());
    // Two failed retries leave the next timer attempt several seconds out.
    act(() => TestSocket.instances[0].close());
    act(() => vi.advanceTimersByTime(RECONNECT_BASE_MS));
    act(() => TestSocket.instances[1].close());
    expect(hook.result.current.reconnectAttempt).toBe(2);
    expect(TestSocket.instances).toHaveLength(2);
    return hook;
  }

  it.each([
    ["online", () => window.dispatchEvent(new Event("online"))],
    ["focus", () => window.dispatchEvent(new Event("focus"))],
    ["visible", () => document.dispatchEvent(new Event("visibilitychange"))],
  ])("retries on %s without waiting for the backoff timer", (_name, trigger) => {
    const { result, unmount } = waitingOnBackoff();
    act(trigger);
    expect(TestSocket.instances).toHaveLength(3);
    expect(result.current.reconnectAttempt).toBe(0);
    act(() => TestSocket.instances[2].open());
    expect(result.current.connected).toBe(true);
    // The cancelled backoff timer must not open a second transport.
    act(() => vi.advanceTimersByTime(RECONNECT_BASE_MS * 4));
    expect(TestSocket.instances).toHaveLength(3);
    unmount();
  });

  it("retries on the native wake event and releases its listener", async () => {
    tauri.native = true;
    const { unmount } = waitingOnBackoff();
    await act(async () => {});
    expect(tauri.wake).not.toBeNull();
    act(() => tauri.wake?.());
    expect(TestSocket.instances).toHaveLength(3);
    unmount();
    expect(tauri.unlisten).toHaveBeenCalledOnce();
  });

  it("leaves an open or connecting transport alone", () => {
    const { unmount } = setup();
    act(() => window.dispatchEvent(new Event("focus")));
    expect(TestSocket.instances).toHaveLength(1);
    act(() => TestSocket.instances[0].open());
    act(() => window.dispatchEvent(new Event("online")));
    expect(TestSocket.instances).toHaveLength(1);
    unmount();
  });

  it("stops listening after cleanup", () => {
    const { unmount } = setup();
    unmount();
    act(() => window.dispatchEvent(new Event("online")));
    expect(TestSocket.instances).toHaveLength(1);
  });
});
