export const RECORDING_CONTROL_NATIVE_TIMEOUT_MS = 5000;

/** One running native operation and one replaceable desired state. */
export function createRecordingControlReconciler(
  apply: (show: boolean, step: <T>(work: () => Promise<T>) => Promise<T>, current: () => boolean) => Promise<void>,
  recover: (step: <T>(work: () => Promise<T>) => Promise<T>) => Promise<void>,
) {
  let disposed = false;
  let suspended = false;
  let running = false;
  let dirty = false;
  let recovery = false;
  let desired = false;
  let generation = 0;
  const interrupted = new Error("native_reconciliation_interrupted");

  async function step<T>(work: () => Promise<T>): Promise<T> {
    if (disposed || suspended) throw interrupted;
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      recovery = true;
      console.warn("[HUD] dedicated control native mutation timed out");
    }, RECORDING_CONTROL_NATIVE_TIMEOUT_MS);
    try {
      const result = await work();
      if (disposed || suspended || timedOut) throw interrupted;
      return result;
    } finally {
      clearTimeout(timer);
    }
  }

  async function drain() {
    if (running || disposed || suspended) return;
    running = true;
    try {
      while (!disposed && !suspended && (dirty || recovery)) {
        if (recovery) {
          recovery = false;
          try {
            await recover(step);
            dirty = true;
          } catch {
            // A timed-out recovery also waits for physical settlement.
            if (!recovery) break;
            continue;
          }
        }
        dirty = false;
        const owner = generation;
        try {
          await apply(desired, step, () => !disposed && !suspended && owner === generation);
        } catch (error) {
          if (error !== interrupted) console.warn("[HUD] dedicated control native reconciliation failed");
        }
      }
    } finally {
      running = false;
    }
  }

  return {
    request(show: boolean) {
      if (disposed) return;
      desired = show;
      generation += 1;
      dirty = true;
      void drain();
    },
    dispose() { disposed = true; generation += 1; dirty = false; },
    suspend() { suspended = true; generation += 1; if (running) recovery = true; },
    resume() { suspended = false; void drain(); },
  };
}
