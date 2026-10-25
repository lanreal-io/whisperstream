import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import type { AppEvent } from "../../lib/socket/events";
import type { TranscriptBakeoffSelection } from "../../lib/api/transcripts";
import { cancelTranscriptBakeoff, retryTranscriptBakeoffResult, startTranscriptBakeoff, TranscriptBakeoffApiError } from "../../lib/api/transcripts";
import { errorType } from "../../lib/logCapture";
import { bakeoffSessionReducer, INITIAL_BAKEOFF_SESSION_STATE, type BakeoffStartFailure } from "./bakeoffSession";

/** Mounted once per page. Closing only hides; authority changes release content. */
export function useBakeoffSession(
  bakeoffEnabled: boolean,
  subscribe: (callback: (event: AppEvent) => void) => () => void,
) {
  // The Bakeoff session lives here so a transcript card never owns decrypted
  // results or process-residency details; cards only emit the trigger.
  const [bakeoffState, dispatchBakeoff] = useReducer(
    bakeoffSessionReducer,
    INITIAL_BAKEOFF_SESSION_STATE,
  );
  const bakeoffTriggerRef = useRef<HTMLButtonElement | null>(null);
  const [bakeoffStarting, setBakeoffStarting] = useState(false);
  const [bakeoffStartFailure, setBakeoffStartFailure] =
    useState<BakeoffStartFailure | null>(null);
  const [bakeoffCancelling, setBakeoffCancelling] = useState(false);
  const [bakeoffRetrying, setBakeoffRetrying] = useState<number | null>(null);
  const [bakeoffRetryFailure, setBakeoffRetryFailure] = useState<{
    pairIndex: number;
    failure: BakeoffStartFailure;
  } | null>(null);

  const openBakeoff = useCallback(
    (transcriptId: string, trigger: HTMLButtonElement) => {
      if (!bakeoffEnabled) return;
      bakeoffTriggerRef.current = trigger;
      dispatchBakeoff({ type: "open", transcriptId });
    },
    [bakeoffEnabled],
  );

  const closeBakeoff = useCallback(() => {
    const trigger = bakeoffTriggerRef.current;
    dispatchBakeoff({ type: "close" });
    queueMicrotask(() => {
      if (trigger?.isConnected) trigger.focus({ preventScroll: true });
    });
  }, []);

  const resetBakeoff = useCallback(() => {
    bakeoffTriggerRef.current = null;
    dispatchBakeoff({ type: "reset" });
  }, []);

  // The only Bakeoff write path. `run.requested` bumps the authority epoch
  // BEFORE the request, so a delayed acceptance for a superseded lineup is
  // rejected by the reducer instead of resurrecting queued pairs.
  const bakeoffClientGeneration = bakeoffState.clientGeneration;
  const bakeoffTranscriptId = bakeoffState.session?.transcriptId ?? null;
  const runBakeoff = useCallback(
    async (selections: TranscriptBakeoffSelection[]) => {
      if (!bakeoffEnabled || !bakeoffTranscriptId || bakeoffStarting) return;
      const requestGeneration = bakeoffClientGeneration + 1;
      setBakeoffStartFailure(null);
      setBakeoffStarting(true);
      dispatchBakeoff({ type: "run.requested", selections });
      try {
        const result = await startTranscriptBakeoff(
          bakeoffTranscriptId,
          selections,
        );
        dispatchBakeoff({
          type: "run.accepted",
          clientGeneration: requestGeneration,
          result,
          selections,
        });
      } catch (error) {
        // Content-free: only the stable recovery code reaches the workspace.
        console.error(
          `[Bakeoff] start failed error_type=${errorType(error)}`,
        );
        dispatchBakeoff({
          type: "run.rejected",
          clientGeneration: requestGeneration,
        });
        setBakeoffStartFailure(
          error instanceof TranscriptBakeoffApiError
            ? {
                code: error.code,
                requiredMb: error.requiredMb,
                availableMb: error.availableMb,
              }
            : { code: null },
        );
      } finally {
        setBakeoffStarting(false);
      }
    },
    [
      bakeoffClientGeneration,
      bakeoffEnabled,
      bakeoffStarting,
      bakeoffTranscriptId,
    ],
  );

  // Cancel remaining is content-free cleanup: the backend marks every
  // unfinished pair cancelled under a fresh generation and leaves completed
  // results alone. The cancelled lanes arrive over the WebSocket, so nothing
  // is guessed here.
  const bakeoffRunId = bakeoffState.session?.runId ?? null;
  const cancelBakeoff = useCallback(async () => {
    if (!bakeoffEnabled || !bakeoffRunId || bakeoffCancelling) return;
    setBakeoffCancelling(true);
    try {
      await cancelTranscriptBakeoff(bakeoffRunId);
    } catch (error) {
      console.error(`[Bakeoff] cancel failed error_type=${errorType(error)}`);
    } finally {
      setBakeoffCancelling(false);
    }
  }, [bakeoffCancelling, bakeoffEnabled, bakeoffRunId]);

  // One failed or cancelled lane, re-run in its existing slot. Inference is
  // serialized server-side, so at most one retry is ever in flight.
  const retryBakeoffPair = useCallback(
    async (pairIndex: number) => {
      if (!bakeoffEnabled || !bakeoffRunId || bakeoffRetrying !== null) return;
      setBakeoffRetryFailure(null);
      setBakeoffRetrying(pairIndex);
      try {
        await retryTranscriptBakeoffResult(bakeoffRunId, pairIndex);
      } catch (error) {
        console.error(`[Bakeoff] retry failed error_type=${errorType(error)}`);
        setBakeoffRetryFailure({
          pairIndex,
          failure:
            error instanceof TranscriptBakeoffApiError
              ? {
                  code: error.code,
                  requiredMb: error.requiredMb,
                  availableMb: error.availableMb,
                }
              : { code: null },
        });
      } finally {
        setBakeoffRetrying(null);
      }
    },
    [bakeoffEnabled, bakeoffRetrying, bakeoffRunId],
  );

  const editBakeoffLineup = useCallback(() => {
    setBakeoffStartFailure(null);
    setBakeoffRetryFailure(null);
    dispatchBakeoff({ type: "edit" });
  }, []);

  // A new transcript, a reset, or a revoked session drops the stale lineup
  // failure with the session it belonged to.
  useEffect(() => {
    setBakeoffStartFailure(null);
    setBakeoffRetryFailure(null);
  }, [bakeoffTranscriptId]);

  // A new run supersedes the previous run's lane-level refusal.
  useEffect(() => {
    setBakeoffRetryFailure(null);
  }, [bakeoffRunId]);

  // A second page-level subscription is intentional: ListView owns history
  // refreshes, while Transcripts owns the memory-only Bakeoff authority state.
  useEffect(() => {
    return subscribe((event) => {
      switch (event.type) {
        case "enhancement.bakeoff.started":
        case "enhancement.bakeoff.progress":
        case "enhancement.bakeoff.result":
        case "enhancement.bakeoff.revoked":
          dispatchBakeoff({ type: "event", event });
          break;
      }
    });
  }, [subscribe]);

  // Lockdown and gate revocation synchronously remove the trigger, then this
  // effect scrubs any decrypted results retained by the page state.
  useLayoutEffect(() => {
    if (!bakeoffEnabled) resetBakeoff();
  }, [bakeoffEnabled, resetBakeoff]);

  // Task 3.2 will call the same close command from its X/Esc handlers. Keeping
  // it active here gives the state owner a tested focus-restoration contract.
  useEffect(() => {
    if (!bakeoffState.session?.open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeBakeoff();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [bakeoffState.session?.open, closeBakeoff]);

  useEffect(() => {
    if (!bakeoffState.session) bakeoffTriggerRef.current = null;
  }, [bakeoffState.session]);

  useEffect(() => {
    return () => {
      // The reducer is component-local, so unmount releases every output. Drop
      // the only retained DOM handle as part of the same privacy boundary.
      bakeoffTriggerRef.current = null;
    };
  }, []);

  return {
    bakeoffState, bakeoffStarting, bakeoffStartFailure, bakeoffCancelling,
    bakeoffRetrying, bakeoffRetryFailure, openBakeoff, closeBakeoff,
    runBakeoff, cancelBakeoff, retryBakeoffPair, editBakeoffLineup,
  };
}
