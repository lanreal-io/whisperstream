import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createNativeMutationRunner,
  type NativeMutationControl,
} from "./nativeMutationChain";

const TIMEOUT = 1_000;

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

let onTimeout: ReturnType<typeof vi.fn>;
let recover: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  onTimeout = vi.fn();
  recover = vi.fn(async () => undefined);
});

afterEach(() => {
  vi.useRealTimers();
});

function runner() {
  return createNativeMutationRunner({ timeoutMs: TIMEOUT, onTimeout, recover });
}

describe("createNativeMutationRunner", () => {
  it("runs settling steps in order with no recovery", async () => {
    const order: string[] = [];
    const first = deferred();
    const chain = runner();
    chain.request(async () => {
      order.push("first:start");
      await first.promise;
      order.push("first:end");
    });
    chain.request(async () => { order.push("second"); });
    await vi.advanceTimersByTimeAsync(TIMEOUT - 1);
    expect(order).toEqual(["first:start"]);
    first.resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(order).toEqual(["first:start", "first:end", "second"]);
    await vi.advanceTimersByTimeAsync(TIMEOUT * 5);
    expect(onTimeout).not.toHaveBeenCalled();
    expect(chain.phase()).toBe("idle");
  });

  it("coalesces requests made while a step is outstanding to the newest", async () => {
    const ran: string[] = [];
    const first = deferred();
    const chain = runner();
    chain.request(async () => { await first.promise; ran.push("first"); });
    chain.request(async () => { ran.push("stale"); });
    chain.request(async () => { ran.push("newest"); });
    first.resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(ran).toEqual(["first", "newest"]);
  });

  it("holds all native work in recovery until a timed-out step settles", async () => {
    const hung = deferred();
    const controls: NativeMutationControl[] = [];
    const ran: string[] = [];
    recover.mockImplementation(async () => { ran.push("recover"); });
    const chain = runner();
    chain.request(async (control) => {
      controls.push(control);
      await hung.promise;
      ran.push(control.abandoned ? "old:late-abandoned" : "old:live");
    });
    await vi.advanceTimersByTimeAsync(TIMEOUT);
    expect(onTimeout).toHaveBeenCalledOnce();
    expect(chain.phase()).toBe("recovering");
    expect(controls[0].abandoned).toBe(true);

    chain.request(async () => { ran.push("successor"); });
    await vi.advanceTimersByTimeAsync(TIMEOUT * 10);
    // No successor native work while the uncertain call may still land.
    expect(ran).toEqual([]);

    hung.resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(recover).toHaveBeenCalledOnce();
    // The late completion ran first, then recovery, then the successor.
    expect(ran).toEqual(["old:late-abandoned", "recover", "successor"]);
    expect(chain.phase()).toBe("idle");
  });

  it("reapplies the latest desired state after recovery even without a new request", async () => {
    const hung = deferred();
    let runs = 0;
    const chain = runner();
    chain.request(async (control) => {
      runs += 1;
      if (runs === 1) await hung.promise;
      expect(control.abandoned).toBe(false);
    });
    await vi.advanceTimersByTimeAsync(TIMEOUT);
    hung.resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(runs).toBe(2);
    expect(recover).toHaveBeenCalledOnce();
  });

  it("keeps exactly one call outstanding across repeated hangs", async () => {
    let outstanding = 0;
    let maxOutstanding = 0;
    const releases: (() => void)[] = [];
    const chain = runner();
    const hangingStep = async () => {
      outstanding += 1;
      maxOutstanding = Math.max(maxOutstanding, outstanding);
      await new Promise<void>((resolve) => releases.push(resolve));
      outstanding -= 1;
    };
    chain.request(hangingStep);
    for (let round = 0; round < 3; round += 1) {
      await vi.advanceTimersByTimeAsync(TIMEOUT);
      chain.request(hangingStep);
      chain.request(hangingStep);
      await vi.advanceTimersByTimeAsync(TIMEOUT * 3);
      expect(outstanding).toBe(1);
      releases.shift()!();
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(maxOutstanding).toBe(1);
    expect(onTimeout).toHaveBeenCalledTimes(3);
    expect(recover).toHaveBeenCalledTimes(3);
  });

  it("absorbs rejections and synchronous throws", async () => {
    const chain = runner();
    const ran: string[] = [];
    chain.request(async () => { throw new Error("native"); });
    chain.request(() => { throw new Error("sync"); });
    chain.request(async () => { ran.push("after"); });
    await vi.advanceTimersByTimeAsync(0);
    expect(ran).toEqual(["after"]);
    expect(onTimeout).not.toHaveBeenCalled();
  });

  it("retries recovery when the recover step itself times out", async () => {
    const hung = deferred();
    const recoverHung = deferred();
    const ran: string[] = [];
    let recoverRuns = 0;
    recover.mockImplementation(async () => {
      recoverRuns += 1;
      ran.push(`recover:${recoverRuns}`);
      if (recoverRuns === 1) await recoverHung.promise;
    });
    const chain = runner();
    chain.request(async () => { await hung.promise; });
    await vi.advanceTimersByTimeAsync(TIMEOUT);
    chain.request(async () => { ran.push("desired"); });
    hung.resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(ran).toEqual(["recover:1"]);
    await vi.advanceTimersByTimeAsync(TIMEOUT);
    expect(chain.phase()).toBe("recovering");
    expect(ran).toEqual(["recover:1"]);
    recoverHung.resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(ran).toEqual(["recover:1", "recover:2", "desired"]);
  });
});
