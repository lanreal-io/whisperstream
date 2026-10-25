import { describe, expect, it, vi } from "vitest";

import { waitForBackendBeforeMount } from "./backendReadiness";
import mainSource from "../main.tsx?raw";

describe("packaged backend mount readiness", () => {
  it("retries transient startup failures until health is ready", async () => {
    const fetchHealth = vi
      .fn<() => Promise<Response>>()
      .mockRejectedValueOnce(new TypeError("not listening"))
      .mockResolvedValueOnce(new Response(null, { status: 503 }))
      .mockResolvedValueOnce(new Response("{}", { status: 200 }));
    const delay = vi.fn(async () => undefined);

    await expect(
      waitForBackendBeforeMount({
        native: true,
        attempts: 4,
        fetchHealth,
        delay,
      }),
    ).resolves.toEqual({ state: "ready", attempts: 3 });
    expect(fetchHealth).toHaveBeenCalledTimes(3);
    expect(delay).toHaveBeenCalledTimes(2);
  });

  it("does not delay ordinary browser or development mounting", async () => {
    const fetchHealth = vi.fn<() => Promise<Response>>();
    const delay = vi.fn(async () => undefined);

    await expect(
      waitForBackendBeforeMount({
        native: false,
        fetchHealth,
        delay,
      }),
    ).resolves.toEqual({ state: "not-required", attempts: 0 });
    expect(fetchHealth).not.toHaveBeenCalled();
    expect(delay).not.toHaveBeenCalled();
  });

  it("bounds an unavailable packaged backend and then permits failure UI", async () => {
    const fetchHealth = vi.fn(async () => new Response(null, { status: 503 }));
    const delay = vi.fn(async () => undefined);

    await expect(
      waitForBackendBeforeMount({
        native: true,
        attempts: 3,
        fetchHealth,
        delay,
      }),
    ).resolves.toEqual({ state: "unavailable", attempts: 3 });
    expect(fetchHealth).toHaveBeenCalledTimes(3);
    expect(delay).toHaveBeenCalledTimes(2);
  });

  it("resolves packaged health and log capture before loading a window root", () => {
    const health = mainSource.indexOf("await waitForBackendBeforeMount()");
    const capture = mainSource.indexOf("startLockdownAwareLogCapture()");
    const load = mainSource.indexOf("async function loadWindowRoot()");
    expect(health).toBeGreaterThan(-1);
    expect(capture).toBeGreaterThan(health);
    expect(load).toBeGreaterThan(capture);
    expect(mainSource).toContain(
      'if (backendReadiness.state === "ready") await logCaptureReadiness;',
    );
  });
});
