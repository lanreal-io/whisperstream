import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BenchmarkFlow } from "../BenchmarkFlow";
import type { AppEvent } from "../../lib/socket/events";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function benchmarkHarness() {
  let handler: ((event: AppEvent) => void) | undefined;
  const unsubscribe = vi.fn(() => {
    handler = undefined;
  });
  const subscribe = vi.fn((next: (event: AppEvent) => void) => {
    handler = next;
    return unsubscribe;
  });
  return {
    subscribe,
    unsubscribe,
    emit(event: AppEvent) {
      act(() => handler?.(event));
    },
  };
}

function okResponse(body: unknown = { status: "started" }) {
  return {
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  };
}

describe("BenchmarkFlow approved copy", () => {
  it("uses the scoped local introduction while measuring the model", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();

    render(
      <BenchmarkFlow
        startPhase="prefilter"
        failureFallback="gemini"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={vi.fn()}
        onVerdict={vi.fn()}
      />,
    );

    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
    });

    expect(
      await screen.findByText(
        "Checking how fast it runs",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Everything stays/)).toBeNull();
  });

  it("keeps the useful winning result and scopes cleanup to this computer", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();

    render(
      <BenchmarkFlow
        startPhase="prefilter"
        failureFallback="gemini"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={vi.fn()}
        onVerdict={vi.fn()}
      />,
    );

    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
    });
    await screen.findByText("Checking how fast it runs");
    harness.emit({
      type: "benchmark.result",
      verdict: "viable",
      model_downloaded: true,
      measured_model_id: "gemma4-e2b-instruct",
      measured_tok_per_s: 60,
    });

    expect(
      await screen.findByText(
        "Enhancement uses graphics acceleration. Enhancement runs on this computer.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Nothing leaves/)).toBeNull();
  });

  it("uses one Gemini CTA without pricing prose or a separate key link", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();
    const onVerdict = vi.fn();

    render(
      <BenchmarkFlow
        startPhase="prefilter"
        failureFallback="gemini"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={vi.fn()}
        onVerdict={onVerdict}
      />,
    );

    harness.emit({
      type: "benchmark.result",
      verdict: "unviable",
      model_downloaded: true,
      measured_model_id: "stream-1-mini",
      measured_tok_per_s: 10,
    });

    const cta = await screen.findByRole("button", {
      name: "Use Gemini's free tier",
    });
    expect(screen.queryByRole("link", { name: /key/i })).toBeNull();
    expect(screen.queryByText(/free Google key/i)).toBeNull();
    fireEvent.click(cta);
    expect(onVerdict).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "unviable" }),
    );
  });

  it("treats infrastructure failure as retryable without firing a verdict", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();
    const onCancel = vi.fn();
    const onVerdict = vi.fn();

    render(
      <BenchmarkFlow
        startPhase="prefilter"
        benchMode="manual"
        benchModelId="gemma4-e2b-instruct"
        failureFallback="gemini"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={onCancel}
        onVerdict={onVerdict}
      />,
    );

    harness.emit({
      type: "benchmark.result",
      verdict: "unviable",
      outcome: "infrastructure_error",
      model_downloaded: false,
      mode: "manual",
    });

    expect(await screen.findByText("Couldn't complete the speed check"))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText(/too slow for enhancement/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Gemini/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onCancel).toHaveBeenCalledOnce();
    expect(onVerdict).not.toHaveBeenCalled();
  });

  it("uses retry and dismiss for measured Lockdown failures", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();
    const onVerdict = vi.fn();

    render(
      <BenchmarkFlow
        startPhase="prefilter"
        benchMode="manual"
        benchModelId="gemma4-e2b-instruct"
        failureFallback="local"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={vi.fn()}
        onVerdict={onVerdict}
      />,
    );

    harness.emit({
      type: "benchmark.result",
      verdict: "unviable",
      model_downloaded: true,
      measured_model_id: "gemma4-e2b-instruct",
      measured_tok_per_s: 20,
      verdicts: { "gemma4-e2b-instruct": "unviable" },
      mode: "manual",
    });

    expect(await screen.findByText(/Local enhancement stays off/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Gemini/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onVerdict).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "unviable",
        verdicts: { "gemma4-e2b-instruct": "unviable" },
      }),
    );
  });
});

describe("BenchmarkFlow recheck routing and device progress", () => {
  it("posts a manual benchmark for the selected Gemma model", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse());
    globalThis.fetch = fetchMock;
    const harness = benchmarkHarness();

    render(
      <BenchmarkFlow
        startPhase="prefilter"
        mode="recheck"
        benchMode="manual"
        benchModelId="gemma4-e2b-instruct"
        failureFallback="gemini"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={vi.fn()}
        onVerdict={vi.fn()}
      />,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/enhancement/benchmark/start"),
    )!;
    expect(JSON.parse(String(init.body))).toEqual({
      mode: "manual",
      model_id: "gemma4-e2b-instruct",
    });
  });

  it("shows each serial device and resets the live meter on handoff", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();

    const { container } = render(
      <BenchmarkFlow
        startPhase="prefilter"
        mode="recheck"
        benchMode="manual"
        benchModelId="gemma4-e2b-instruct"
        failureFallback="gemini"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={vi.fn()}
        onVerdict={vi.fn()}
      />,
    );

    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "NVIDIA GeForce RTX 4070 Ti SUPER",
      device_index: 0,
    });
    await screen.findByText("Testing on NVIDIA GeForce RTX 4070 Ti SUPER");
    expect(screen.getByRole("status")).toHaveAttribute(
      "data-live-measurement",
      "false",
    );
    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "NVIDIA GeForce RTX 4070 Ti SUPER",
      device_index: 0,
      wps: 330,
    });

    expect(
      container.querySelector('[data-speed-mode="fill"]'),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveAttribute(
      "data-live-measurement",
      "true",
    );
    expect(screen.getByText("Live measurement.")).toHaveClass("sr-only");
    expect(container.querySelector('[data-live-dot="active"]')).toHaveStyle({
      background: "var(--color-frost)",
    });

    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "AMD Radeon(TM) Graphics",
      device_index: 1,
    });

    expect(
      screen.getByText("Testing on AMD Radeon(TM) Graphics"),
    ).toBeInTheDocument();
    expect(
      container.querySelector('[data-speed-mode="marquee"]'),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveAttribute(
      "data-live-measurement",
      "false",
    );
    expect(container.querySelector('[data-live-dot="idle"]')).toHaveStyle({
      background: "var(--accent-fill)",
    });
    expect(
      screen.queryByText("Live measurement."),
    ).not.toBeInTheDocument();
  });

  it("classifies the single-model live fill and keeps its chip static", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();

    const { container } = render(
      <BenchmarkFlow
        startPhase="prefilter"
        mode="recheck"
        benchMode="manual"
        benchModelId="gemma4-e2b-instruct"
        failureFallback="gemini"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={vi.fn()}
        onVerdict={vi.fn()}
      />,
    );

    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "AMD Radeon(TM) Graphics",
      device_index: 0,
    });
    await screen.findByText("Testing on AMD Radeon(TM) Graphics");
    const marquee = container.querySelector<HTMLElement>(
      '[data-speed-fill="marquee"]',
    );
    expect(marquee).toBeInTheDocument();
    // Reproduce the browser state that exposed the bug: Framer Motion leaves
    // the marquee's in-flight x transform on its DOM node. The solid fill must
    // mount as a different node so that transform cannot carry across modes.
    marquee!.style.transform = "translateX(126px)";

    const emitWps = (wps: number) => {
      harness.emit({
        type: "benchmark.progress",
        phase: "running",
        model_id: "gemma4-e2b-instruct",
        device_name: "AMD Radeon(TM) Graphics",
        device_index: 0,
        wps,
      });
    };

    emitWps(15);
    const solidFill = container.querySelector<HTMLElement>(
      '[data-speed-fill="solid"]',
    );
    expect(solidFill).not.toBe(marquee);
    expect(solidFill).not.toHaveStyle({ transform: "translateX(126px)" });
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

    emitWps(27);
    expect(
      container.querySelector(
        '[data-speed-mode="fill"][data-speed-tone="caution"]',
      ),
    ).toBeInTheDocument();
    expect(container.querySelector('[data-speed-fill="solid"]')).toHaveStyle({
      background: "var(--color-caution)",
    });

    emitWps(38);
    expect(
      container.querySelector(
        '[data-speed-mode="fill"][data-speed-tone="mint"]',
      ),
    ).toBeInTheDocument();
    expect(container.querySelector('[data-speed-fill="solid"]')).toHaveStyle({
      background: "var(--color-mint)",
    });

    expect(
      container.querySelector(
        '[data-model-chip-status="active"][data-model-chip-motion="static"]',
      ),
    ).toBeInTheDocument();
    expect(
      container.querySelector("[data-model-chip-shimmer]"),
    ).not.toBeInTheDocument();
    expect(
      container.querySelector("[data-model-chip-pulse]"),
    ).not.toBeInTheDocument();
  });

  it("uses a single public model in the final edition", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();
    render(<BenchmarkFlow startPhase="prefilter" failureFallback="local" currentModelId="gemma4-e2b-instruct" subscribe={harness.subscribe}
      onCancel={vi.fn()} onVerdict={vi.fn()} />);
    harness.emit({type:"benchmark.progress",phase:"running",model_id:"gemma4-e2b-instruct",wps:15});
    await screen.findByText("Checking how fast it runs");
    expect(screen.queryByText("Stream-1")).toBeNull();
    expect(screen.queryByText("Finding the right model")).toBeNull();
  });

  it("keeps the authoritative achieved speed mint on recheck success", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();

    const { container } = render(
      <BenchmarkFlow
        startPhase="prefilter"
        mode="recheck"
        benchMode="manual"
        benchModelId="gemma4-e2b-instruct"
        failureFallback="gemini"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={vi.fn()}
        onVerdict={vi.fn()}
      />,
    );

    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "NVIDIA GeForce RTX 4070 Ti SUPER",
      device_index: 0,
      wps: 330,
    });
    harness.emit({
      type: "benchmark.result",
      verdict: "viable",
      model_downloaded: true,
      mode: "manual",
      measured_model_id: "gemma4-e2b-instruct",
      measured_tok_per_s: 429,
      verdicts: { "gemma4-e2b-instruct": "viable" },
      device: "NVIDIA GeForce RTX 4070 Ti SUPER",
    });

    await screen.findByText("Local enhancement still runs here");
    expect(
      container.querySelector(
        '[data-speed-mode="fill"][data-speed-tone="mint"]',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("words / sec")).toBeInTheDocument();
    expect(
      screen.queryByText("Live measurement."),
    ).not.toBeInTheDocument();
  });

  it.each([
    ["viable", 65, "Target met", "mint", true],
    ["marginal", 45, "Near target", "caution", false],
    ["unviable", 20, "Below target", "rose", false],
  ] as const)(
    "holds the %s terminal device reading with its semantic label",
    async (verdict, terminalTps, label, tone, hasCheck) => {
      globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
      const harness = benchmarkHarness();

      const { container } = render(
        <BenchmarkFlow
          startPhase="prefilter"
          mode="recheck"
          benchMode="manual"
          benchModelId="gemma4-e2b-instruct"
          failureFallback="gemini"
          currentModelId="gemma4-e2b-instruct"
          subscribe={harness.subscribe}
          onCancel={vi.fn()}
          onVerdict={vi.fn()}
        />,
      );

      harness.emit({
        type: "benchmark.progress",
        phase: "running",
        model_id: "gemma4-e2b-instruct",
        device_name: "Test GPU",
        device_index: 0,
        wps: 9,
      });
      await screen.findByText("Testing on Test GPU");
      vi.useFakeTimers();
      harness.emit({
        type: "benchmark.progress",
        phase: "device_complete",
        model_id: "gemma4-e2b-instruct",
        device_name: "Test GPU",
        device_index: 0,
        measured_tok_per_s: terminalTps,
        predicted_n: 256,
        verdict,
      });

      const expectedWps = Math.round(terminalTps / 1.3);
      expect(screen.getByRole("status")).toHaveTextContent(label);
      expect(
        container.querySelector(
          `[data-speed-tone="${tone}"][data-speed-target-wps="${expectedWps}"]`,
        ),
      ).toBeInTheDocument();
      expect(screen.getByText(String(expectedWps))).toBeInTheDocument();
      expect(container.querySelector('[data-speed-fill="solid"]')).toHaveStyle({
        width: `${(Math.min(expectedWps, 60) / 60) * 100}%`,
      });
      expect(
        container.querySelector("[data-device-completion-check]") !== null,
      ).toBe(hasCheck);

      act(() => vi.advanceTimersByTime(999));
      expect(screen.getByRole("status")).toHaveTextContent(label);
    },
  );

  it("buffers the next device start and ticks until the completed score has dwelled", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();

    const { container } = render(
      <BenchmarkFlow
        startPhase="prefilter"
        mode="recheck"
        benchMode="manual"
        benchModelId="gemma4-e2b-instruct"
        failureFallback="gemini"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={vi.fn()}
        onVerdict={vi.fn()}
      />,
    );

    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "NVIDIA GeForce RTX 4070 Ti SUPER",
      device_index: 0,
      wps: 120,
    });
    await screen.findByText(
      "Testing on NVIDIA GeForce RTX 4070 Ti SUPER",
    );
    vi.useFakeTimers();
    harness.emit({
      type: "benchmark.progress",
      phase: "device_complete",
      model_id: "gemma4-e2b-instruct",
      device_name: "NVIDIA GeForce RTX 4070 Ti SUPER",
      device_index: 0,
      measured_tok_per_s: 156,
      predicted_n: 256,
      verdict: "viable",
    });

    // These arrive while NVIDIA's completed reading is held.
    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "AMD Radeon(TM) Graphics",
      device_index: 1,
    });
    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "AMD Radeon(TM) Graphics",
      device_index: 1,
      wps: 9,
    });

    act(() => vi.advanceTimersByTime(999));
    expect(
      screen.getByText(
        "Testing on NVIDIA GeForce RTX 4070 Ti SUPER",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Target met");
    expect(
      container.querySelector('[data-speed-target-wps="120"]'),
    ).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(1));
    expect(
      screen.getByText("Testing on AMD Radeon(TM) Graphics"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Target met")).not.toBeInTheDocument();
    expect(
      container.querySelector(
        '[data-speed-tone="rose"][data-speed-target-wps="9"]',
      ),
    ).toBeInTheDocument();
  });

  it("buffers the final result until the last device completion dwell ends", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();

    render(
      <BenchmarkFlow
        startPhase="prefilter"
        mode="recheck"
        benchMode="manual"
        benchModelId="gemma4-e2b-instruct"
        failureFallback="gemini"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={vi.fn()}
        onVerdict={vi.fn()}
      />,
    );

    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "NVIDIA GeForce RTX 4070 Ti SUPER",
      device_index: 0,
      wps: 120,
    });
    await screen.findByText(
      "Testing on NVIDIA GeForce RTX 4070 Ti SUPER",
    );
    vi.useFakeTimers();
    harness.emit({
      type: "benchmark.progress",
      phase: "device_complete",
      model_id: "gemma4-e2b-instruct",
      device_name: "NVIDIA GeForce RTX 4070 Ti SUPER",
      device_index: 0,
      measured_tok_per_s: 156,
      predicted_n: 256,
      verdict: "viable",
    });
    harness.emit({
      type: "benchmark.result",
      verdict: "viable",
      model_downloaded: true,
      mode: "manual",
      measured_model_id: "gemma4-e2b-instruct",
      measured_tok_per_s: 156,
      verdicts: { "gemma4-e2b-instruct": "viable" },
      device: "NVIDIA GeForce RTX 4070 Ti SUPER",
    });

    act(() => vi.advanceTimersByTime(999));
    expect(screen.getByRole("status")).toHaveTextContent("Target met");
    expect(
      screen.queryByText("Local enhancement still runs here"),
    ).not.toBeInTheDocument();

    act(() => vi.advanceTimersByTime(1));
    // Let Framer Motion finish the benchmark -> receipt crossfade after the
    // buffered result has been replayed by the dwell timer.
    vi.useRealTimers();
    expect(
      await screen.findByText("Local enhancement still runs here"),
    ).toBeInTheDocument();
  });

  it("preserves FIFO order across a queued second completion and final result", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();

    const { container } = render(
      <BenchmarkFlow
        startPhase="prefilter"
        mode="recheck"
        benchMode="manual"
        benchModelId="gemma4-e2b-instruct"
        failureFallback="gemini"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={vi.fn()}
        onVerdict={vi.fn()}
      />,
    );

    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "NVIDIA GPU",
      device_index: 0,
      wps: 100,
    });
    await screen.findByText("Testing on NVIDIA GPU");
    vi.useFakeTimers();

    harness.emit({
      type: "benchmark.progress",
      phase: "device_complete",
      model_id: "gemma4-e2b-instruct",
      device_name: "NVIDIA GPU",
      device_index: 0,
      measured_tok_per_s: 130,
      predicted_n: 256,
      verdict: "viable",
    });
    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "AMD GPU",
      device_index: 1,
    });
    harness.emit({
      type: "benchmark.progress",
      phase: "running",
      model_id: "gemma4-e2b-instruct",
      device_name: "AMD GPU",
      device_index: 1,
      wps: 9,
    });
    harness.emit({
      type: "benchmark.progress",
      phase: "device_complete",
      model_id: "gemma4-e2b-instruct",
      device_name: "AMD GPU",
      device_index: 1,
      measured_tok_per_s: 45,
      predicted_n: 256,
      verdict: "marginal",
    });
    harness.emit({
      type: "benchmark.result",
      verdict: "marginal",
      model_downloaded: true,
      mode: "manual",
      measured_model_id: "gemma4-e2b-instruct",
      measured_tok_per_s: 45,
      verdicts: { "gemma4-e2b-instruct": "marginal" },
      device: "AMD GPU",
    });

    act(() => vi.advanceTimersByTime(999));
    expect(screen.getByRole("status")).toHaveTextContent("Target met");
    expect(screen.getByText("Testing on NVIDIA GPU")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(1));
    expect(screen.getByRole("status")).toHaveTextContent("Near target");
    expect(screen.getByText("Testing on AMD GPU")).toBeInTheDocument();
    expect(
      container.querySelector(
        '[data-speed-tone="caution"][data-speed-target-wps="35"]',
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("The customizable model runs slowly here"),
    ).not.toBeInTheDocument();

    act(() => vi.advanceTimersByTime(999));
    expect(screen.getByRole("status")).toHaveTextContent("Near target");

    act(() => vi.advanceTimersByTime(1));
    vi.useRealTimers();
    expect(
      await screen.findByText("Customizable runs slowly here"),
    ).toBeInTheDocument();
  });

  it("clears a pending dwell and buffered events when unmounted", () => {
    vi.useFakeTimers();
    globalThis.fetch = vi.fn().mockResolvedValue(okResponse());
    const harness = benchmarkHarness();

    const { unmount } = render(
      <BenchmarkFlow
        startPhase="prefilter"
        mode="recheck"
        benchMode="manual"
        benchModelId="gemma4-e2b-instruct"
        failureFallback="gemini"
        currentModelId="gemma4-e2b-instruct"
        subscribe={harness.subscribe}
        onCancel={vi.fn()}
        onVerdict={vi.fn()}
      />,
    );

    harness.emit({
      type: "benchmark.progress",
      phase: "device_complete",
      model_id: "gemma4-e2b-instruct",
      device_name: "Test GPU",
      device_index: 0,
      measured_tok_per_s: 65,
      predicted_n: 256,
      verdict: "viable",
    });
    harness.emit({
      type: "benchmark.result",
      verdict: "viable",
      model_downloaded: true,
      measured_tok_per_s: 65,
    });

    unmount();
    expect(harness.unsubscribe).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
    expect(() => act(() => vi.runAllTimers())).not.toThrow();
  });
});

describe("single-model speed check intro", () => {
  it("sets up local cleanup with Stream-1 Mini only and no model chooser", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse());
    vi.stubGlobal("fetch", fetchMock);
    render(<BenchmarkFlow startPhase="intro" mode="setup" benchMode="auto"
      benchModelId="stream-1-mini" benchModelSize="462 MB" failureFallback="gemini"
      currentModelId="gemma4-e2b-instruct" subscribe={vi.fn(() => () => {})}
      onCancel={vi.fn()} onVerdict={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Set up local enhancement" })).toBeInTheDocument();
    expect(screen.getByText(
      "We'll check how fast Stream-1 Mini runs on your computer. If it isn't downloaded yet, we'll get it first (462 MB).",
    )).toBeInTheDocument();
    const card = document.querySelector("[data-benchmark-model-card]") as HTMLElement;
    expect(card).toHaveTextContent("Stream-1 Mini");
    expect(card).toHaveTextContent("Fast enhancement for Chat and Email");
    expect(card).toHaveTextContent("462 MB");
    expect(within(card).queryByRole("button")).toBeNull();
    expect(screen.queryByText("Models to check")).toBeNull();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();

    fireEvent.click(screen.getByRole("button", { name: "Start speed check" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    // The automatic check names no model: the backend measures Stream-1 Mini.
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ mode: "auto" });
    vi.unstubAllGlobals();
  });

  it("names a manually chosen model in the title and body", () => {
    render(<BenchmarkFlow startPhase="intro" benchMode="manual"
      benchModelId="gemma4-e2b-instruct" benchModelSize="3.1 GB" failureFallback="local"
      currentModelId="stream-1-mini" subscribe={vi.fn(() => () => {})}
      onCancel={vi.fn()} onVerdict={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Try Customizable" })).toBeInTheDocument();
    expect(screen.getByText(
      "We'll check how fast Customizable runs on your computer. If it isn't downloaded yet, we'll get it first (3.1 GB).",
    )).toBeInTheDocument();
    expect(screen.queryByText(/lighter one automatically/)).toBeNull();
  });
});

describe("single-model first-setup results", () => {
  function renderFirstSetup(onVerdict = vi.fn()) {
    const harness = benchmarkHarness();
    render(<BenchmarkFlow startPhase="intro" mode="setup" benchMode="auto"
      benchModelId="stream-1-mini" benchModelSize="462 MB" failureFallback="gemini"
      currentModelId="gemma4-e2b-instruct" subscribe={harness.subscribe}
      onCancel={vi.fn()} onVerdict={onVerdict} />);
    return { harness, onVerdict };
  }

  it("applies Stream-1 Mini with its measured verdict on success", async () => {
    const { harness, onVerdict } = renderFirstSetup();
    harness.emit({
      type: "benchmark.result", mode: "auto", verdict: "viable", model_downloaded: true,
      freshly_downloaded: true, measured_model_id: "stream-1-mini", measured_tok_per_s: 70,
    });
    expect(await screen.findByText("Stream-1 Mini runs well here. Enhancement stays on this computer.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start using it" }));
    expect(onVerdict).toHaveBeenCalledWith({
      kind: "viable", appliedModelId: "stream-1-mini", verdicts: { "stream-1-mini": "viable" },
    });
  });

  it("asks before using a slow Stream-1 Mini", async () => {
    const { harness, onVerdict } = renderFirstSetup();
    harness.emit({
      type: "benchmark.result", mode: "auto", verdict: "marginal", model_downloaded: true,
      measured_model_id: "stream-1-mini", measured_tok_per_s: 40,
      verdicts: { "stream-1-mini": "marginal" },
    });
    expect(await screen.findByRole("heading", { name: "Stream-1 Mini runs slowly here" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Use it anyway" }));
    expect(onVerdict).toHaveBeenCalledWith({
      kind: "marginal", appliedModelId: "stream-1-mini", verdicts: { "stream-1-mini": "marginal" },
    });
  });

  it("reports a freshly downloaded too-slow model for cleanup", async () => {
    const { harness, onVerdict } = renderFirstSetup();
    harness.emit({
      type: "benchmark.result", mode: "auto", verdict: "unviable", model_downloaded: true,
      freshly_downloaded: true, measured_model_id: "stream-1-mini", measured_tok_per_s: 15,
    });
    expect(await screen.findByText("The speed check found this computer too slow for enhancement")).toBeInTheDocument();
    expect(document.querySelectorAll("[data-model-chip-status]")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Use Gemini's free tier" }));
    expect(onVerdict).toHaveBeenCalledWith({
      kind: "unviable", freshlyDownloaded: true, verdicts: { "stream-1-mini": "unviable" },
    });
  });
});

describe("inline variant (onboarding)", () => {
  // The inline download leg subscribes a second handler (DownloadCard), so
  // events fan out to every live subscriber.
  function multiHarness() {
    const handlers = new Set<(event: AppEvent) => void>();
    const subscribe = vi.fn((next: (event: AppEvent) => void) => {
      handlers.add(next);
      return () => handlers.delete(next);
    });
    return {
      subscribe,
      emit(event: AppEvent) {
        act(() => [...handlers].forEach((h) => h(event)));
      },
    };
  }

  function renderInline(onVerdict = vi.fn(), onCancel = vi.fn()) {
    const fetchMock = vi.fn().mockResolvedValue(okResponse());
    vi.stubGlobal("fetch", fetchMock);
    const harness = multiHarness();
    const states: Array<{ phase: string }> = [];
    const onInlineStateChange = vi.fn((s: { phase: string }) => states.push(s));
    render(<BenchmarkFlow variant="inline" onInlineStateChange={onInlineStateChange}
      startPhase="prefilter" mode="setup" benchMode="auto"
      benchModelId="stream-1-mini" benchModelSize="462 MB" failureFallback="current"
      currentModelId="stream-1-mini" subscribe={harness.subscribe}
      onCancel={onCancel} onVerdict={onVerdict} />);
    const last = () => onInlineStateChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    return { harness, onVerdict, onCancel, fetchMock, last, states };
  }

  afterEach(() => vi.unstubAllGlobals());

  it("renders without the dialog shell and reports running phases", async () => {
    const { harness, last, fetchMock } = renderInline();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("heading", { name: "Testing how fast Stream-1 Mini runs here" })).toBeInTheDocument();
    expect(screen.getByText("Checking your hardware...")).toBeInTheDocument();
    expect(screen.getByText("462 MB")).toBeInTheDocument();
    expect(last()).toEqual({ phase: "running" });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    harness.emit({ type: "benchmark.progress", phase: "prefilter" });
    expect(await screen.findByRole("heading", { name: "Downloading Stream-1 Mini" })).toBeInTheDocument();
    expect(screen.getByText("This can take a few minutes.")).toBeInTheDocument();

    harness.emit({
      type: "benchmark.progress", phase: "running", model_id: "stream-1-mini",
      device_name: "NVIDIA GeForce RTX 4060", device_index: 0,
    });
    expect(await screen.findByText("Testing on NVIDIA GeForce RTX 4060")).toBeInTheDocument();
    expect(last()).toEqual({ phase: "running" });
  });

  it("applies a fast verdict on arrival and shows Enhancement is on", async () => {
    const { harness, onVerdict, last } = renderInline();
    harness.emit({
      type: "benchmark.result", mode: "auto", verdict: "viable", model_downloaded: true,
      freshly_downloaded: true, measured_model_id: "stream-1-mini", measured_tok_per_s: 70,
    });
    expect(await screen.findByRole("heading", { name: "Enhancement is on" })).toBeInTheDocument();
    expect(screen.getByText("Stream-1 Mini runs well on this computer. Try it with two quick examples.")).toBeInTheDocument();
    expect(document.querySelector("[data-model-chip-landed]")).not.toBeNull();
    expect(screen.getByText("Target met")).toBeInTheDocument();
    expect(onVerdict).toHaveBeenCalledOnce();
    expect(onVerdict).toHaveBeenCalledWith({
      kind: "viable", appliedModelId: "stream-1-mini", verdicts: { "stream-1-mini": "viable" },
    });
    expect(last()).toEqual({ phase: "success" });
    expect(screen.queryByRole("button", { name: "Start using it" })).toBeNull();
  });

  it("hands the marginal choice to the host and confirms leaving it off", async () => {
    const { harness, onVerdict, last } = renderInline();
    harness.emit({
      type: "benchmark.result", mode: "auto", verdict: "marginal", model_downloaded: true,
      measured_model_id: "stream-1-mini", measured_tok_per_s: 40,
      verdicts: { "stream-1-mini": "marginal" },
    });
    expect(await screen.findByRole("heading", { name: "Stream-1 Mini runs slowly here" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Use it anyway" })).toBeNull();
    expect(onVerdict).not.toHaveBeenCalled();
    const state = last() as { phase: string; leaveOff: () => void };
    expect(state.phase).toBe("marginal");
    act(() => state.leaveOff());
    expect(onVerdict).toHaveBeenCalledWith({
      kind: "marginal", appliedModelId: null, verdicts: { "stream-1-mini": "marginal" },
    });
    expect(await screen.findByText("Enhancement is off. You can turn it on anytime in Enhancement.")).toBeInTheDocument();
    expect(last()).toEqual({ phase: "declined" });
  });

  it("marginal Use it anyway applies the measured model", async () => {
    const { harness, onVerdict, last } = renderInline();
    harness.emit({
      type: "benchmark.result", mode: "auto", verdict: "marginal", model_downloaded: true,
      measured_model_id: "stream-1-mini", measured_tok_per_s: 40,
    });
    await screen.findByRole("heading", { name: "Stream-1 Mini runs slowly here" });
    act(() => (last() as { useAnyway: () => void }).useAnyway());
    expect(onVerdict).toHaveBeenCalledWith({
      kind: "marginal", appliedModelId: "stream-1-mini", verdicts: { "stream-1-mini": "marginal" },
    });
  });

  it("unviable stays off with no retry and no Gemini offer", async () => {
    const { harness, onVerdict, last } = renderInline();
    harness.emit({
      type: "benchmark.result", mode: "auto", verdict: "unviable", model_downloaded: true,
      freshly_downloaded: true, measured_model_id: "stream-1-mini", measured_tok_per_s: 15,
    });
    expect(await screen.findByRole("heading", { name: "This computer is too slow for enhancement" })).toBeInTheDocument();
    expect(screen.getByText("Enhancement stays off. Dictation works the same without it.")).toBeInTheDocument();
    expect(screen.queryByText(/Gemini/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Test again" })).toBeNull();
    expect(onVerdict).toHaveBeenCalledWith({
      kind: "unviable", freshlyDownloaded: true, verdicts: { "stream-1-mini": "unviable" },
    });
    expect(last()).toEqual({ phase: "failure" });
  });

  it("offers a host retry after an infrastructure error", async () => {
    const { harness, last, fetchMock } = renderInline();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    harness.emit({ type: "benchmark.result", mode: "auto", verdict: "unviable", outcome: "infrastructure_error", model_downloaded: false });
    expect(await screen.findByRole("heading", { name: "Couldn't complete the speed check" })).toBeInTheDocument();
    const state = last() as { phase: string; retry: () => void };
    expect(state.phase).toBe("infrastructure_error");
    act(() => state.retry());
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(last()).toEqual({ phase: "running" });
  });

  it("Cancel during download aborts it and returns to the host", async () => {
    const { harness, onCancel, fetchMock } = renderInline();
    harness.emit({ type: "benchmark.progress", phase: "prefilter" });
    await screen.findByRole("heading", { name: "Downloading Stream-1 Mini" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledOnce();
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/local-download/cancel"))).toBe(true),
    );
  });
});
