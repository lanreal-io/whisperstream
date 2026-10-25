// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/event", () => ({
  emit: vi.fn(async () => undefined),
  listen: vi.fn(async () => () => undefined),
}));

const originalConsole = { log: console.log, warn: console.warn, error: console.error };
let capture: typeof import("./logCapture");
let fetchMock: ReturnType<typeof vi.fn>;

function postedLines(call: number): string[] {
  const init = fetchMock.mock.calls[call][1] as RequestInit;
  return JSON.parse(String(init.body)).frontend_logs;
}

beforeEach(async () => {
  vi.useFakeTimers();
  console.log = vi.fn();
  console.warn = vi.fn();
  console.error = vi.fn();
  vi.resetModules();
  capture = await import("./logCapture");
  capture.initLogCapture();
  fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  console.log = originalConsole.log;
  console.warn = originalConsole.warn;
  console.error = originalConsole.error;
});

describe("frontend log sync", () => {
  it("skips periodic POSTs when nothing new was logged since the last success", async () => {
    capture.startLogSync();
    console.log("[App] first line");
    await vi.advanceTimersByTimeAsync(capture.LOG_SYNC_INTERVAL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(capture.LOG_SYNC_INTERVAL_MS * 3);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    console.log("[App] second line");
    await vi.advanceTimersByTimeAsync(capture.LOG_SYNC_INTERVAL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(postedLines(1).join("\n")).toContain("first line");
    expect(postedLines(1).join("\n")).toContain("second line");
  });

  it("retries an unchanged ring after a failed POST", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 503 }));
    console.log("[App] line");
    await capture.flushLogSync();
    await capture.flushLogSync();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await capture.flushLogSync();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("never overlaps requests and aborts a hung POST at the timeout", async () => {
    const signals: AbortSignal[] = [];
    fetchMock.mockImplementation((_url: string, init: RequestInit) => {
      signals.push(init.signal as AbortSignal);
      return new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      });
    });
    capture.startLogSync();
    console.log("[App] line");
    await vi.advanceTimersByTimeAsync(capture.LOG_SYNC_INTERVAL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    console.log("[App] while in flight");
    void capture.flushLogSync();
    await vi.advanceTimersByTimeAsync(capture.LOG_SYNC_TIMEOUT_MS - 1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(signals[0].aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(1);
    expect(signals[0].aborted).toBe(true);
    // The queued flush follows only after the first request settles.
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("lets a flush deliver lines logged during an in-flight periodic sync", async () => {
    let release!: () => void;
    fetchMock.mockImplementationOnce(
      () => new Promise<Response>((resolve) => {
        release = () => resolve(new Response(null, { status: 204 }));
      }),
    );
    capture.startLogSync();
    console.log("[App] before");
    await vi.advanceTimersByTimeAsync(capture.LOG_SYNC_INTERVAL_MS);
    console.log("[App] popup closing");
    const flushed = capture.flushLogSync();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    release();
    await flushed;
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(postedLines(1).join("\n")).toContain("popup closing");
  });

  it("syncs on beforeunload when the ring changed", async () => {
    capture.startLogSync();
    console.log("[App] closing");
    window.dispatchEvent(new Event("beforeunload"));
    await vi.advanceTimersByTimeAsync(0);
    // Earlier tests' module instances keep their own unload listeners, so
    // look for this instance's POST rather than counting calls.
    const bodies = fetchMock.mock.calls.map((_call, index) => postedLines(index).join("\n"));
    expect(bodies.some((body) => body.includes("closing"))).toBe(true);
  });
});
