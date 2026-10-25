import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SlowModelConsentDialog } from "../SlowModelConsentDialog";
import type { AppEvent } from "../../lib/socket/events";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function benchmarkHarness() {
  let handler: ((event: AppEvent) => void) | undefined;
  const subscribe = vi.fn((next: (event: AppEvent) => void) => {
    handler = next;
    return () => {
      handler = undefined;
    };
  });
  return {
    subscribe,
    emit(event: AppEvent) {
      act(() => handler?.(event));
    },
  };
}

describe("SlowModelConsentDialog fresh benchmark", () => {
  it("replaces the pre-sample marquee with a solid semantic live fill", () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: () => Promise.resolve('{"status":"started"}'),
      json: () => Promise.resolve({ status: "started" }),
    });
    const harness = benchmarkHarness();

    const { container } = render(
      <SlowModelConsentDialog
        open
        modelId="stream-1-mini"
        variant="fresh"
        subscribe={harness.subscribe}
        onUse={vi.fn()}
        onKeep={vi.fn()}
      />,
    );

    expect(
      container.querySelector('[data-speed-mode="marquee"]'),
    ).toBeInTheDocument();

    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "stream-1-mini",
      wps: 15,
    });

    expect(
      container.querySelector(
        '[data-speed-mode="fill"][data-speed-tone="rose"]',
      ),
    ).toBeInTheDocument();
    expect(container.querySelector('[data-speed-fill="solid"]')).toHaveStyle({
      background: "var(--color-rose)",
    });
    expect(
      container.querySelector("[data-speed-fill-shimmer]"),
    ).not.toBeInTheDocument();
  });
});
