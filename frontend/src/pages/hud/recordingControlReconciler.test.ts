import { afterEach, expect, it, vi } from "vitest";
import { createRecordingControlReconciler, RECORDING_CONTROL_NATIVE_TIMEOUT_MS } from "./recordingControlReconciler";

afterEach(() => vi.useRealTimers());

it("does not retry an unchanged target after an immediate native failure", async () => {
  vi.useFakeTimers();
  const apply = vi.fn().mockRejectedValue(new Error("unavailable"));
  const runner = createRecordingControlReconciler(apply, vi.fn());
  runner.request(true);
  await vi.advanceTimersByTimeAsync(60000);
  expect(apply).toHaveBeenCalledOnce();
  runner.dispose();
});

it("retains only the newest desired state behind a stalled native command", async () => {
  vi.useFakeTimers();
  let settle!: () => void;
  const blocked = new Promise<void>((resolve) => { settle = resolve; });
  const calls: string[] = [];
  const apply = vi.fn<Parameters<typeof createRecordingControlReconciler>[0]>(async (show, step) => {
    calls.push(show ? "show" : "hide");
    if (show) await step(() => blocked);
  });
  const runner = createRecordingControlReconciler(apply, async (step) => { await step(async () => { calls.push("recover-hide"); }); });
  runner.request(true);
  for (let i = 0; i < 10000; i += 1) runner.request(i % 2 === 0);
  expect(apply).toHaveBeenCalledOnce();
  await vi.advanceTimersByTimeAsync(RECORDING_CONTROL_NATIVE_TIMEOUT_MS * 10);
  expect(calls).toEqual(["show"]);
  settle();
  await vi.advanceTimersByTimeAsync(0);
  expect(calls).toEqual(["show", "recover-hide", "hide"]);
  runner.dispose();
});

it("retires pending work on disposal without issuing parallel recovery", async () => {
  vi.useFakeTimers();
  let settle!: () => void;
  const apply = vi.fn<Parameters<typeof createRecordingControlReconciler>[0]>(async (_show, step) => step(() => new Promise<void>((resolve) => { settle = resolve; })));
  const recover = vi.fn();
  const runner = createRecordingControlReconciler(apply, recover);
  runner.request(true);
  runner.request(false);
  runner.dispose();
  settle();
  await vi.advanceTimersByTimeAsync(0);
  expect(apply).toHaveBeenCalledOnce();
  expect(recover).not.toHaveBeenCalled();
});

it("keeps the same outstanding owner through effect replay and recovers after it settles", async () => {
  vi.useFakeTimers();
  let settle!: () => void;
  const blocked = new Promise<void>((resolve) => { settle = resolve; });
  const calls: string[] = [];
  let first = true;
  const runner = createRecordingControlReconciler(async (show, step) => {
    calls.push(show ? "show" : "hide");
    if (first) { first = false; await step(() => blocked); }
  }, async (step) => { await step(async () => { calls.push("recover-hide"); }); });
  runner.request(true);
  runner.suspend();
  runner.resume();
  runner.request(false);
  expect(calls).toEqual(["show"]);
  settle();
  await vi.advanceTimersByTimeAsync(0);
  expect(calls).toEqual(["show", "recover-hide", "hide"]);
  runner.dispose();
});
