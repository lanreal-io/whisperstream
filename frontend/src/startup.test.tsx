import { afterEach, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";

const startup = vi.hoisted(() => ({
  started: () => {},
  ready: undefined as undefined | ((state: string) => void),
  capture: vi.fn(async () => undefined),
}));
vi.mock("./lib/backendReadiness", () => ({
  waitForBackendBeforeMount: () => new Promise((resolve) => {
    startup.ready = (state) => resolve({ state, attempts: 1 });
    startup.started();
  }),
}));
vi.mock("./lib/logCapture", () => ({ startLockdownAwareLogCapture: startup.capture }));
vi.mock("./App", () => ({ default: () => <div data-testid="loaded-app">Ready</div> }));

afterEach(() => { document.body.innerHTML = ""; });

it.each(["ready", "unavailable"])("paints loading, then handles backend %s", async (state) => {
  vi.resetModules();
  startup.capture.mockClear();
  document.body.innerHTML = '<div id="root"></div>';
  const started = new Promise<void>((resolve) => { startup.started = resolve; });
  const loading = import("./main");
  // main paints synchronously before awaiting backend readiness. Module loading
  // time is not part of the curtain's render deadline.
  await Promise.race([started, loading]);
  expect(document.querySelector('[data-testid="loading-curtain"]')).not.toBeNull();
  expect(document.body.textContent).toContain("Starting Whisperstream");
  expect(document.querySelector('[data-testid="loaded-app"]')).toBeNull();
  expect(startup.capture).not.toHaveBeenCalled();
  startup.ready!(state);
  await loading;
  if (state === "ready") {
    await waitFor(() => expect(document.querySelector('[data-testid="loaded-app"]')).not.toBeNull());
    expect(document.querySelector('[data-testid="loading-curtain"]')).toBeNull();
  } else {
    await waitFor(() => expect(document.querySelector('[data-testid="startup-failure-curtain"]')).not.toBeNull());
    expect(document.querySelector('[data-testid="loaded-app"]')).toBeNull();
  }
});
