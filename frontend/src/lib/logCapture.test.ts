// @vitest-environment jsdom

import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const tauri = vi.hoisted(() => ({
  emit: vi.fn(async () => undefined),
  listen: vi.fn(async () => () => undefined),
  handler: null as ((event: { payload: unknown }) => void) | null,
}));

vi.mock("@tauri-apps/api/event", () => ({
  emit: tauri.emit,
  listen: vi.fn(async (_event: string, handler: (event: { payload: unknown }) => void) => {
    tauri.listen();
    tauri.handler = handler;
    return () => undefined;
  }),
}));

let capture: typeof import("./logCapture");
const logSpy = vi.spyOn(console, "log").mockImplementation(() => undefined);
const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);

const forbidden = [
  "SecretRecording",
  "Secret Person",
  "PrivateEditor",
  "private.example",
  "Acquisition Notes",
  "Conference Microphone",
  "session-secret-91",
  "record-secret-92",
  "PRIVATE_EXCEPTION_MESSAGE",
  "SENSITIVE_CUSTOM_ERROR_NAME",
  "person@example.com",
  "hunter2",
  "ABCD-EFGH-IJKL-MNOP-QRST",
  "sk-private",
];

function expectCanariesAbsent(text: string): void {
  for (const canary of forbidden) expect(text).not.toContain(canary);
  expect(text).not.toContain("🔒秘密");
}

beforeAll(async () => {
  capture = await import("./logCapture");
  capture.initLogCapture();
});

beforeEach(() => {
  capture.resumeLogCapture();
  capture.clearLogBuffer();
});

afterAll(() => {
  capture.clearLogBuffer();
  logSpy.mockRestore();
  warnSpy.mockRestore();
  errorSpy.mockRestore();
});

describe("diagnostic capture minimization", () => {
  it("uses a finite error-class allowlist", () => {
    const error = new Error("PRIVATE_EXCEPTION_MESSAGE");
    error.name = "SENSITIVE_CUSTOM_ERROR_NAME";

    expect(capture.errorType(new TypeError("private"))).toBe("TypeError");
    expect(capture.errorType(error)).toBe("UnknownError");
    expect(capture.errorType("PRIVATE_EXCEPTION_MESSAGE")).toBe("UnknownError");
  });

  it("omits arbitrary object bodies and redacts paths, identities, and errors", () => {
    const hostile = {
      get privateValue(): never {
        throw new Error("PRIVATE_EXCEPTION_MESSAGE");
      },
    };
    const error = new Error("PRIVATE_EXCEPTION_MESSAGE");
    error.name = "SENSITIVE_CUSTOM_ERROR_NAME";

    console.error(
      "[App] filename=SecretRecording.wav " +
        "path=C:\\Users\\Secret Person\\private.txt " +
        "process=PrivateEditor.exe domain=private.example " +
        "title=Acquisition Notes device=Conference Microphone " +
        "session_id=session-secret-91 record_id=record-secret-92 " +
        "email=person@example.com password=hunter2 " +
        "recovery_code=ABCD-EFGH-IJKL-MNOP-QRST api_key=sk-private " +
        "reason=PRIVATE_EXCEPTION_MESSAGE 🔒秘密",
      hostile,
      error,
    );

    const text = capture.getLogBuffer().join("\n");
    expectCanariesAbsent(text);
    expect(text).toContain("error_type=UnknownError");
    expect(text).toContain("reason=<redacted>");
  });

  it("bounds every line, the entry count, and total retained bytes", () => {
    for (let index = 0; index < 600; index += 1) {
      console.log(`[App] heartbeat ordinal=${index} payload=${"x".repeat(4096)}`);
    }

    const lines = capture.getLogBuffer();
    const encoder = new TextEncoder();
    expect(lines.length).toBeLessThanOrEqual(500);
    expect(
      Math.max(...lines.map((line) => encoder.encode(line).byteLength)),
    ).toBeLessThanOrEqual(2048);
    expect(
      lines.reduce((total, line) => total + encoder.encode(line).byteLength, 0),
    ).toBeLessThanOrEqual(128 * 1024);
  });

  it("clears idempotently and honors the pause/resume seam", () => {
    console.log("[App] before pause");
    capture.pauseLogCapture();
    console.log("[App] PRIVATE_EXCEPTION_MESSAGE while paused");
    capture.clearLogBuffer();
    capture.clearLogBuffer();
    expect(capture.getLogBuffer()).toEqual([]);

    capture.resumeLogCapture();
    console.log("[App] after resume");
    expect(capture.getLogBuffer()).toHaveLength(1);
    expect(capture.getLogBuffer()[0]).toContain("after resume");
  });

  it("does not retain unhandled error or rejection messages", () => {
    const error = new Error("PRIVATE_EXCEPTION_MESSAGE");
    error.name = "SENSITIVE_CUSTOM_ERROR_NAME";
    window.onerror?.("PRIVATE_EXCEPTION_MESSAGE", "C:\\Users\\Secret Person\\app.ts", 1, 2, error);
    window.onunhandledrejection?.({ reason: error } as PromiseRejectionEvent);

    const text = capture.getLogBuffer().join("\n");
    expectCanariesAbsent(text);
    expect(text.match(/error_type=UnknownError/g)).toHaveLength(2);
  });
});

describe("Lockdown diagnostic transitions", () => {
  it("retains the explicit non-Lockdown pause seam", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    capture.startLogSync();

    console.log("[App] pre-lock line");
    capture.pauseLogCapture();
    await capture.flushLogSync();
    window.dispatchEvent(new Event("beforeunload"));
    await vi.advanceTimersByTimeAsync(10_000);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(capture.getLogBuffer().join("\n")).toContain("pre-lock line");
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("keeps diagnostics through a rejected enable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ enabled: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    capture.prepareForLockdownEnable();
    console.log("[App] locked-sensitive-canary");

    await capture.recoverFromFailedLockdownEnable();
    console.log("[App] resumed-empty");

    const text = capture.getLogBuffer().join("\n");
    expect(text).toContain("resumed-empty");
    expect(text).toContain("locked-sensitive-canary");
  });

  it("keeps diagnostics after partial-envelope enable failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ enabled: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    capture.prepareForLockdownEnable();

    await capture.recoverFromFailedLockdownEnable();
    console.log("[App] partial-envelope-canary");

    expect(capture.getLogBuffer().join("\n")).toContain("partial-envelope-canary");
  });

  it("keeps diagnostics when failed-enable reconciliation cannot fetch status", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new Error("offline"))));
    capture.prepareForLockdownEnable();

    await capture.recoverFromFailedLockdownEnable();
    console.log("[App] fetch-failure-canary");

    expect(capture.getLogBuffer().join("\n")).toContain("fetch-failure-canary");
  });

  it("bootstraps ordinary mode from an authoritative unlocked status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ enabled: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    capture.pauseLogCapture();
    capture.clearLogBuffer();

    await capture.startLockdownAwareLogCapture();
    console.log("[App] ordinary-bootstrap-canary");

    expect(capture.getLogBuffer().join("\n")).toContain(
      "ordinary-bootstrap-canary",
    );
  });

  it("starts capture even when Lockdown status cannot be fetched", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new Error("offline"))));
    capture.resumeLogCapture();

    await capture.startLockdownAwareLogCapture();
    console.log("[App] failed-bootstrap-canary");

    expect(capture.getLogBuffer().join("\n")).toContain("failed-bootstrap-canary");
  });

  it("installs one listener without suppressing cross-webview diagnostics", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ enabled: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    capture.resumeLogCapture();
    console.log("[App] startup-canary");

    await capture.startLockdownAwareLogCapture();
    await capture.startLockdownAwareLogCapture();
    await vi.waitFor(() => expect(tauri.listen).toHaveBeenCalledTimes(1));
    console.log("[App] locked-startup-canary");
    expect(capture.getLogBuffer().join("\n")).toContain("locked-startup-canary");

    tauri.handler?.({ payload: { enabled: false } });
    console.log("[App] cross-webview-resumed");
    expect(capture.getLogBuffer().join("\n")).toContain("cross-webview-resumed");
    tauri.handler?.({ payload: { enabled: false } });
    expect(capture.getLogBuffer().join("\n")).toContain("cross-webview-resumed");

    tauri.handler?.({ payload: { enabled: true } });
    console.log("[App] cross-webview-locked");
    expect(capture.getLogBuffer().join("\n")).toContain("cross-webview-locked");
  });
});
