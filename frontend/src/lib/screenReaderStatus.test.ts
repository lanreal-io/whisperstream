import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppEvent, EventHandler } from "./socket/events";

const mocks = vi.hoisted(() => ({
  getScreenReaderStatus: vi.fn(),
}));

vi.mock("./api/system", () => ({
  getScreenReaderStatus: mocks.getScreenReaderStatus,
}));

import {
  SCREEN_READER_STATUS_TIMEOUT_MS,
  createScreenReaderStatusFollower,
  useScreenReaderStatusSync,
} from "./screenReaderStatus";

interface Pending {
  signal: AbortSignal;
  resolve: (active: boolean) => void;
  reject: (error: Error) => void;
}

let pending: Pending[] = [];

async function settle(): Promise<void> {
  await act(async () => {
    for (let index = 0; index < 10; index += 1) await Promise.resolve();
  });
}

beforeEach(() => {
  pending = [];
  mocks.getScreenReaderStatus.mockReset();
  mocks.getScreenReaderStatus.mockImplementation(
    (init?: { signal?: AbortSignal }) =>
      new Promise((resolve, reject) => {
        const signal = init?.signal as AbortSignal;
        signal.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
        pending.push({
          signal,
          resolve: (active) => resolve({ active }),
          reject,
        });
      }),
  );
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("createScreenReaderStatusFollower", () => {
  it("never overlaps requests and coalesces non-forced refreshes", async () => {
    const apply = vi.fn();
    const follower = createScreenReaderStatusFollower(apply);
    follower.refresh();
    follower.refresh();
    follower.refresh();
    expect(pending).toHaveLength(1);
    pending[0].resolve(true);
    await settle();
    expect(apply).toHaveBeenCalledWith(true);
    expect(pending).toHaveLength(1);
    follower.dispose();
  });

  it("queues exactly one forced follow-up after the in-flight request", async () => {
    const apply = vi.fn();
    const follower = createScreenReaderStatusFollower(apply);
    follower.refresh();
    follower.refresh(true);
    follower.refresh(true);
    expect(pending).toHaveLength(1);
    pending[0].resolve(false);
    await settle();
    expect(pending).toHaveLength(2);
    pending[1].resolve(true);
    await settle();
    expect(apply.mock.calls).toEqual([[false], [true]]);
    expect(pending).toHaveLength(2);
    follower.dispose();
  });

  it("times out a hung request and retains the last known state", async () => {
    vi.useFakeTimers();
    const apply = vi.fn();
    const follower = createScreenReaderStatusFollower(apply);
    follower.push(true);
    follower.refresh();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SCREEN_READER_STATUS_TIMEOUT_MS - 1);
    });
    expect(pending[0].signal.aborted).toBe(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(pending[0].signal.aborted).toBe(true);
    expect(apply.mock.calls).toEqual([[true]]);
    follower.refresh();
    expect(pending).toHaveLength(2);
    follower.dispose();
  });

  it("retains state on failure and lets a newer push beat an older fetch", async () => {
    const apply = vi.fn();
    const follower = createScreenReaderStatusFollower(apply);
    follower.refresh();
    pending[0].reject(new Error("offline"));
    await settle();
    expect(apply).not.toHaveBeenCalled();
    expect(follower.known()).toBe(false);

    follower.refresh();
    follower.push(true);
    pending[1].resolve(false);
    await settle();
    expect(apply.mock.calls).toEqual([[true]]);
    expect(follower.known()).toBe(true);
    follower.dispose();
  });

  it("aborts the in-flight request on dispose", () => {
    const follower = createScreenReaderStatusFollower(vi.fn());
    follower.refresh();
    follower.dispose();
    expect(pending[0].signal.aborted).toBe(true);
  });
});

describe("useScreenReaderStatusSync", () => {
  function setup(initialConnected: boolean, initialEnabled = true) {
    const handlers = new Set<EventHandler>();
    const subscribe = vi.fn((handler: EventHandler) => {
      handlers.add(handler);
      return () => handlers.delete(handler);
    });
    const apply = vi.fn();
    const hook = renderHook(
      ({ connected, enabled }: { connected: boolean; enabled: boolean }) =>
        useScreenReaderStatusSync({ enabled, connected, subscribe, apply }),
      { initialProps: { connected: initialConnected, enabled: initialEnabled } },
    );
    const emit = (event: AppEvent) =>
      act(() => handlers.forEach((handler) => handler(event)));
    return { ...hook, apply, emit, handlers, subscribe };
  }

  it("fetches once on mount and follows pushed events without polling", async () => {
    vi.useFakeTimers();
    const { apply, emit, unmount, handlers } = setup(true);
    expect(pending).toHaveLength(1);
    pending[0].resolve(false);
    await settle();
    expect(apply).toHaveBeenLastCalledWith(false);

    emit({ type: "accessibility.screen_reader", active: true });
    expect(apply).toHaveBeenLastCalledWith(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(pending).toHaveLength(1);

    unmount();
    expect(handlers.size).toBe(0);
  });

  it("refetches on the first connect even after a successful mount fetch", async () => {
    // Fetch false, reader starts while the socket is still down (push lost),
    // then the socket opens: the first connect must refresh.
    const { rerender, apply } = setup(false);
    pending[0].resolve(false);
    await settle();
    rerender({ connected: true, enabled: true });
    await settle();
    expect(pending).toHaveLength(2);
    pending[1].resolve(true);
    await settle();
    expect(apply).toHaveBeenLastCalledWith(true);

    rerender({ connected: false, enabled: true });
    rerender({ connected: true, enabled: true });
    await settle();
    expect(pending).toHaveLength(3);
  });

  it("coalesces a first connect during the pending mount fetch into one follow-up", async () => {
    const { rerender, apply } = setup(false);
    expect(pending).toHaveLength(1);
    rerender({ connected: true, enabled: true });
    await settle();
    // No overlap: the follow-up waits for the mount request.
    expect(pending).toHaveLength(1);
    pending[0].resolve(false);
    await settle();
    expect(pending).toHaveLength(2);
    pending[1].resolve(true);
    await settle();
    expect(pending).toHaveLength(2);
    expect(apply.mock.calls).toEqual([[false], [true]]);
  });

  it("does not treat a socket already open at mount as a transition", async () => {
    setup(true);
    await settle();
    expect(pending).toHaveLength(1);
  });

  it("fetches on the first connect when the mount fetch failed", async () => {
    const { rerender, apply } = setup(false);
    pending[0].reject(new Error("backend starting"));
    await settle();
    rerender({ connected: true, enabled: true });
    await settle();
    expect(pending).toHaveLength(2);
    pending[1].resolve(true);
    await settle();
    expect(apply).toHaveBeenLastCalledWith(true);
  });

  it("makes no requests or subscriptions while disabled, including across reconnects", async () => {
    const { rerender, handlers } = setup(true, false);
    await settle();
    expect(pending).toHaveLength(0);
    expect(handlers.size).toBe(0);
    rerender({ connected: false, enabled: false });
    rerender({ connected: true, enabled: false });
    await settle();
    expect(pending).toHaveLength(0);

    rerender({ connected: true, enabled: true });
    await settle();
    expect(pending).toHaveLength(1);
    expect(handlers.size).toBe(1);
  });

  it("refetches on reconnect only while enabled and aborts when disabled", async () => {
    const { rerender, handlers, apply } = setup(true);
    pending[0].resolve(false);
    await settle();

    rerender({ connected: false, enabled: true });
    rerender({ connected: true, enabled: true });
    await settle();
    expect(pending).toHaveLength(2);

    rerender({ connected: true, enabled: false });
    expect(pending[1].signal.aborted).toBe(true);
    expect(handlers.size).toBe(0);
    await settle();
    expect(apply).toHaveBeenCalledTimes(1);

    rerender({ connected: false, enabled: false });
    rerender({ connected: true, enabled: false });
    await settle();
    expect(pending).toHaveLength(2);

    rerender({ connected: true, enabled: true });
    await settle();
    expect(pending).toHaveLength(3);
  });
});
