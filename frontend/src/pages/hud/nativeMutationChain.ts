/**
 * Time after which an outstanding native HUD mutation step is treated as
 * uncertain. Steps are a few Tauri invokes; the timeout does not cancel the
 * native command, which may still land later.
 */
export const NATIVE_MUTATION_STEP_TIMEOUT_MS = 5_000;

export interface NativeMutationControl {
  /** True once the step timed out. The step must stop issuing native work. */
  readonly abandoned: boolean;
}

export type NativeMutationStep = (
  control: NativeMutationControl,
) => Promise<void>;

export type NativeMutationRunnerPhase = "idle" | "running" | "recovering";

export interface NativeMutationRunner {
  /**
   * Request the latest desired state. Requests made while a step is
   * outstanding are coalesced: only the newest one runs next.
   */
  request(step: NativeMutationStep): void;
  phase(): NativeMutationRunnerPhase;
}

/**
 * Serialize native HUD mutations with at most one step outstanding.
 *
 * A timeout cannot cancel an already-issued native command, so it does not
 * release the queue. It marks the step abandoned and enters `recovering`: no
 * further native work is issued while the uncertain step is outstanding, and
 * newer requests are coalesced. When the uncertain step finally settles, the
 * `recover` step runs first (the caller forces the window into a known state
 * and records that, discarding anything the late step believed), then the
 * newest desired step runs from that state. A late completion therefore never
 * stands as applied truth. A step that never settles keeps exactly one native
 * call outstanding; a recover step that times out is retried after it settles.
 */
export function createNativeMutationRunner({
  timeoutMs = NATIVE_MUTATION_STEP_TIMEOUT_MS,
  onTimeout,
  recover,
}: {
  timeoutMs?: number;
  onTimeout: () => void;
  recover: NativeMutationStep;
}): NativeMutationRunner {
  let phase: NativeMutationRunnerPhase = "idle";
  let pending: NativeMutationStep | null = null;
  let latest: NativeMutationStep | null = null;
  let needsRecovery = false;

  const pump = () => {
    if (phase !== "idle") return;
    const recovering = needsRecovery;
    const step = recovering ? recover : pending;
    if (step === null) return;
    if (!recovering) pending = null;
    phase = "running";
    const control = { abandoned: false };
    const timer = setTimeout(() => {
      if (phase !== "running") return;
      control.abandoned = true;
      phase = "recovering";
      onTimeout();
    }, timeoutMs);
    let running: Promise<void>;
    try {
      running = step(control);
    } catch (error) {
      running = Promise.reject(error);
    }
    const settle = () => {
      clearTimeout(timer);
      if (phase === "recovering") {
        needsRecovery = true;
        // Rebuild from the newest desired state, even if nothing new arrived.
        if (pending === null) pending = latest;
      } else if (recovering) {
        needsRecovery = false;
      }
      phase = "idle";
      pump();
    };
    running.then(settle, settle);
  };

  return {
    request(step) {
      latest = step;
      pending = step;
      pump();
    },
    phase: () => phase,
  };
}
