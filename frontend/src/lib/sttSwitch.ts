/**
 * One backend STT switch operation, as the frontend has to reason about it.
 *
 * Two things live here because they are the same concern seen from two sides:
 *
 *  - **Lifecycle.** A switch is owned by a backend worker thread. The frontend
 *    persists the target routing, asks the worker to run, and then owns nothing
 *    until the worker reports a terminal `download.complete` / `download.error`
 *    / `download.cancelled`. In particular `POST /api/download/cancel` joins
 *    that thread with a 10 second timeout and answers `cancelling` when the
 *    thread is STILL RUNNING. Treating that as "cancelled" drops the worker's
 *    routing rollback on the floor: the frontend stops tracking the operation,
 *    so the terminal event that follows finds nothing to reconcile and the UI
 *    keeps showing the target model that was never activated.
 *
 *  - **Failure copy.** The backend message is deliberately generic because it
 *    does not know which model the user asked for. The frontend does, so it
 *    composes the exact locked string (copy-review.md -> Activation and
 *    download errors) and never renders a raw HTTP payload.
 *
 * Pure and dependency-free on purpose: `App.tsx` is a thin adapter over these
 * decisions, and they are unit-tested directly rather than through a mirror.
 */

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

/** The frontend's view of one in-flight switch. */
export interface SttSwitchLifecycle {
  /** True from the moment the target routing is persisted until the backend
   *  reports a terminal event. While true a rollback is still owed, so the
   *  authoritative config and artifact inventory must be re-read when that
   *  event lands. */
  rollbackPending: boolean;
}

export const IDLE_STT_SWITCH: SttSwitchLifecycle = { rollbackPending: false };

/** Target routing is persisted and the backend worker accepted it. */
export function sttSwitchStarted(): SttSwitchLifecycle {
  return { rollbackPending: true };
}

/** The start was refused (dictation live, another model operation) or never
 *  reached the worker, so nothing was handed over and nothing is owed. */
export function sttSwitchReleased(): SttSwitchLifecycle {
  return IDLE_STT_SWITCH;
}

/** `POST /api/download/cancel` outcomes, normalized. `cancelling` is the one
 *  that matters: the worker outlived the backend's join window. */
export type SttCancelStatus =
  | "cancelled"
  | "cancelling"
  | "not_running"
  | "unknown";

export function normalizeSttCancelStatus(
  status: string | null | undefined,
): SttCancelStatus {
  const value = (status ?? "").trim().toLowerCase();
  if (value === "cancelling") return "cancelling";
  if (value === "cancelled") return "cancelled";
  if (value === "not_running") return "not_running";
  return "unknown";
}

export interface SttCancelDecision {
  /** Release the confirm/progress dialog and unfreeze the model controls. */
  releaseSwitchUi: boolean;
  /** Re-read config and the artifact inventory right now, because no terminal
   *  event is coming. */
  reconcileNow: boolean;
  next: SttSwitchLifecycle;
}

/**
 * What a cancel response means for the pending routing operation.
 *
 * `failed` is for a cancel POST that threw: the request outcome is unknown, so
 * the user is released from the dialog (never trap them in a modal) but the
 * rollback tracking is deliberately KEPT, because a worker that is still
 * running will still emit its terminal event.
 */
export function sttCancelDecision(
  state: SttSwitchLifecycle,
  status: string | null | undefined,
  { failed = false }: { failed?: boolean } = {},
): SttCancelDecision {
  if (failed) {
    return { releaseSwitchUi: true, reconcileNow: false, next: state };
  }
  switch (normalizeSttCancelStatus(status)) {
    case "cancelling":
      // The worker is mid-cleanup. It still owns the rollback and will report
      // it, so the operation stays pending and the UI stays in its cancelling
      // state rather than claiming an outcome the backend has not reached.
      return { releaseSwitchUi: false, reconcileNow: false, next: state };
    case "not_running":
      // There is no worker, so no terminal event will arrive. Reconcile here
      // or the UI would hold a target that nothing is going to activate.
      return {
        releaseSwitchUi: true,
        reconcileNow: state.rollbackPending,
        next: sttSwitchReleased(),
      };
    default:
      // `cancelled`: the worker finished inside the join window, so it has
      // already broadcast its terminal event (or is about to). Tracking stays
      // until that event so the reconcile happens exactly once.
      return { releaseSwitchUi: true, reconcileNow: false, next: state };
  }
}

export type SttTerminalEvent =
  | "download.complete"
  | "download.error"
  | "download.cancelled";

export interface SttTerminalDecision {
  /** The backend restored the previous routing before reporting, so the saved
   *  config is authoritative again and the frontend's copy is stale. */
  reloadConfig: boolean;
  /** Which artifacts exist and which one is loaded both just changed. */
  refreshInventory: boolean;
  next: SttSwitchLifecycle;
}

export function sttTerminalDecision(
  state: SttSwitchLifecycle,
  event: SttTerminalEvent,
): SttTerminalDecision {
  if (!state.rollbackPending) {
    return { reloadConfig: false, refreshInventory: false, next: state };
  }
  return {
    reloadConfig: event !== "download.complete",
    refreshInventory: true,
    next: sttSwitchReleased(),
  };
}

// ---------------------------------------------------------------------------
// Failure copy
// ---------------------------------------------------------------------------

/** Terminal failure metadata the backend attaches to `download.error`. */
export interface SttSwitchFailure {
  reason?: string;
  restored?: boolean;
}

/**
 * Locked failure copy for a terminal switch failure.
 *
 * The no-rollback variant exists because the locked decision is that a message
 * only confirms restoration when restoration actually happened.
 */
export function sttSwitchFailureMessage(
  modelName: string,
  reason: string | undefined,
  restored: boolean | undefined,
): string {
  if (reason === "operation_busy") {
    return "Whisperstream is already updating a model. Try again when it finishes.";
  }
  if (reason === "dictation_active") {
    return "Finish your current dictation, then switch models.";
  }
  if (reason === "download_failed") {
    return `${modelName} did not finish downloading. Check your connection and try again.`;
  }
  return restored === false
    ? `Could not switch to ${modelName}. Try again.`
    : `Could not switch to ${modelName}. Whisperstream restored your previous settings.`;
}

/**
 * Locked copy for a start the backend REFUSED. Nothing changed, so it reports
 * why the request was turned away rather than claiming a rollback.
 *
 * `detail` is the raw transport error text. It is inspected here and never
 * rendered: an HTTP status line or a JSON payload is not recovery guidance.
 */
export function sttStartRejectionMessage(
  detail: string | null | undefined,
  modelName: string,
): string {
  const text = detail ?? "";
  if (text.includes("dictation_active")) {
    return "Finish your current dictation, then switch models.";
  }
  if (text.includes("operation_busy") || text.includes("409")) {
    return "Whisperstream is already updating a model. Try again when it finishes.";
  }
  return `Could not switch to ${modelName}. Try again.`;
}

/** Locked recovery copy for a routing PUT that failed before an activation
 *  could start. The transport detail is logged for diagnostics, never shown to
 *  the user. Because the backend did not accept the new routing, this does not
 *  claim that a rollback occurred. */
export function sttRoutingSaveFailureMessage(modelName: string): string {
  return `Could not switch to ${modelName}. Try again.`;
}

/** Where a failure reached the frontend from. */
export type SttFailureSource = "start" | "lifecycle";

export interface SttDownloadFailure {
  source: SttFailureSource;
  /** Raw backend text. Read for classification, never rendered. */
  message?: string;
  reason?: string;
  restored?: boolean;
}

export interface SttFailureCopy {
  title: string;
  message: string;
}

/**
 * Recovery copy for the confirm-then-download dialog's error state.
 *
 * The heading follows the failure rather than always claiming a download
 * problem: an artifact that transferred fine and then failed to load is not a
 * download failure, and telling the user to check their connection would send
 * them after the wrong thing.
 */
export function sttDownloadFailureCopy(
  modelName: string,
  failure: SttDownloadFailure,
): SttFailureCopy {
  if (failure.source === "start") {
    return {
      title: "Could not switch models",
      message: sttStartRejectionMessage(failure.message, modelName),
    };
  }
  return {
    title:
      failure.reason === "download_failed"
        ? "Download failed"
        : "Could not switch models",
    message: sttSwitchFailureMessage(
      modelName,
      failure.reason,
      failure.restored,
    ),
  };
}
