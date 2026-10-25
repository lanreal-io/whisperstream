// @vitest-environment node

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getPermissions, openPermissionSettings, requestPermission } from "./api/system";

let originalFetch: typeof globalThis.fetch;

beforeEach(() => {
  originalFetch = globalThis.fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("getPermissions", () => {
  it("consumes the stable query-only capability report", async () => {
    const report = {
      schema_version: 1 as const,
      platform: "macos",
      permissions: [
        {
          kind: "microphone" as const,
          state: "not-determined" as const,
          query_supported: true,
          request_supported: true,
        },
        {
          kind: "accessibility" as const,
          state: "unknown" as const,
          query_supported: true,
          request_supported: true,
        },
        {
          kind: "input-monitoring" as const,
          state: "granted" as const,
          query_supported: true,
          request_supported: true,
        },
      ],
    };
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(report),
    });

    await expect(getPermissions()).resolves.toEqual(report);
    expect(globalThis.fetch).toHaveBeenCalledOnce();
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls[0];
    expect(url).toContain("/api/permissions");
    expect(init).toBeUndefined();
  });

  it.each([
    ["request", requestPermission],
    ["recovery", openPermissionSettings],
  ] as const)("marks the %s call as an explicit user action", async (path, action) => {
    const outcome = {
      schema_version: 1 as const,
      kind: "microphone" as const,
      action: path === "request" ? ("request" as const) : ("open-settings" as const),
      status: "unchanged" as const,
      before: "granted" as const,
      after: "granted" as const,
      failure: null,
      relaunch_required: false,
    };
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(outcome),
    });

    await expect(action("microphone")).resolves.toEqual(outcome);
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain(`/api/permissions/${path}`);
    expect(init).toEqual({
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Whisperstream-User-Intent": "permission-action-v1",
      },
      body: JSON.stringify({ kind: "microphone" }),
    });
  });
});
