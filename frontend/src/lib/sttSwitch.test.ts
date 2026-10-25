/**
 * The STT switch operation's lifecycle decisions and failure copy.
 *
 * The regression these pin: `POST /api/download/cancel` joins the worker thread
 * with a 10 second timeout and answers `cancelling` when the thread is STILL
 * RUNNING. The frontend used to clear its pending routing operation on any
 * cancel response, so the worker's rollback landed after the frontend had
 * stopped listening: the terminal event found nothing to reconcile and the UI
 * kept showing a target model that was never activated.
 */
import { describe, expect, it } from "vitest";
import {
  IDLE_STT_SWITCH,
  normalizeSttCancelStatus,
  sttCancelDecision,
  sttDownloadFailureCopy,
  sttRoutingSaveFailureMessage,
  sttStartRejectionMessage,
  sttSwitchFailureMessage,
  sttSwitchReleased,
  sttSwitchStarted,
  sttTerminalDecision,
} from "./sttSwitch";

describe("normalizeSttCancelStatus", () => {
  it("recognizes every backend status", () => {
    expect(normalizeSttCancelStatus("cancelling")).toBe("cancelling");
    expect(normalizeSttCancelStatus("cancelled")).toBe("cancelled");
    expect(normalizeSttCancelStatus("not_running")).toBe("not_running");
  });

  it("treats an absent or unrecognized status as unknown", () => {
    expect(normalizeSttCancelStatus(undefined)).toBe("unknown");
    expect(normalizeSttCancelStatus(null)).toBe("unknown");
    expect(normalizeSttCancelStatus("")).toBe("unknown");
    expect(normalizeSttCancelStatus("weird")).toBe("unknown");
  });
});

describe("sttCancelDecision", () => {
  it("keeps the pending operation when the worker outlived the join timeout", () => {
    const state = sttSwitchStarted();
    const decision = sttCancelDecision(state, "cancelling");

    expect(decision.releaseSwitchUi).toBe(false);
    expect(decision.reconcileNow).toBe(false);
    expect(decision.next.rollbackPending).toBe(true);
  });

  it("releases the UI on a completed cancel but leaves the terminal event to reconcile", () => {
    const decision = sttCancelDecision(sttSwitchStarted(), "cancelled");

    expect(decision.releaseSwitchUi).toBe(true);
    expect(decision.reconcileNow).toBe(false);
    expect(decision.next.rollbackPending).toBe(true);
  });

  it("reconciles immediately when there is no worker to report back", () => {
    const decision = sttCancelDecision(sttSwitchStarted(), "not_running");

    expect(decision.releaseSwitchUi).toBe(true);
    expect(decision.reconcileNow).toBe(true);
    expect(decision.next.rollbackPending).toBe(false);
  });

  it("does not reconcile a not_running cancel that owned no routing", () => {
    const decision = sttCancelDecision(IDLE_STT_SWITCH, "not_running");

    expect(decision.reconcileNow).toBe(false);
  });

  it("releases the user from the dialog but keeps tracking when the cancel request itself failed", () => {
    const decision = sttCancelDecision(sttSwitchStarted(), undefined, {
      failed: true,
    });

    expect(decision.releaseSwitchUi).toBe(true);
    expect(decision.reconcileNow).toBe(false);
    expect(decision.next.rollbackPending).toBe(true);
  });
});

describe("sttTerminalDecision", () => {
  it("reloads config and the inventory after a delayed cancel", () => {
    // The full delayed-cancel sequence: routing committed, cancel answered
    // `cancelling`, and only later does the worker report.
    let state = sttSwitchStarted();
    state = sttCancelDecision(state, "cancelling").next;
    expect(state.rollbackPending).toBe(true);

    const decision = sttTerminalDecision(state, "download.cancelled");
    expect(decision.reloadConfig).toBe(true);
    expect(decision.refreshInventory).toBe(true);
    expect(decision.next.rollbackPending).toBe(false);
  });

  it("reloads config after a rollback-bearing failure", () => {
    const decision = sttTerminalDecision(sttSwitchStarted(), "download.error");

    expect(decision.reloadConfig).toBe(true);
    expect(decision.refreshInventory).toBe(true);
    expect(decision.next.rollbackPending).toBe(false);
  });

  it("refreshes the inventory but not config after a success", () => {
    const decision = sttTerminalDecision(
      sttSwitchStarted(),
      "download.complete",
    );

    expect(decision.reloadConfig).toBe(false);
    expect(decision.refreshInventory).toBe(true);
    expect(decision.next.rollbackPending).toBe(false);
  });

  it("ignores a terminal event for an operation this window never owned", () => {
    const decision = sttTerminalDecision(
      sttSwitchReleased(),
      "download.cancelled",
    );

    expect(decision.reloadConfig).toBe(false);
    expect(decision.refreshInventory).toBe(false);
  });

  it("reconciles only once across a cancel and its terminal event", () => {
    let state = sttSwitchStarted();
    state = sttCancelDecision(state, "cancelled").next;
    state = sttTerminalDecision(state, "download.cancelled").next;

    expect(sttTerminalDecision(state, "download.cancelled").reloadConfig).toBe(
      false,
    );
  });
});

describe("sttSwitchFailureMessage", () => {
  it("confirms the rollback when the backend performed one", () => {
    expect(
      sttSwitchFailureMessage("NVIDIA Parakeet", "activation_failed", true),
    ).toBe(
      "Could not switch to NVIDIA Parakeet. Whisperstream restored your previous settings.",
    );
  });

  it("never claims a rollback that failed", () => {
    expect(
      sttSwitchFailureMessage("NVIDIA Parakeet", "activation_failed", false),
    ).toBe("Could not switch to NVIDIA Parakeet. Try again.");
  });

  it("uses the locked download copy for a transfer failure", () => {
    expect(sttSwitchFailureMessage("Qwen3 ASR", "download_failed", true)).toBe(
      "Qwen3 ASR did not finish downloading. Check your connection and try again.",
    );
  });

  it("distinguishes another model operation from dictation", () => {
    expect(sttSwitchFailureMessage("Qwen3 ASR", "operation_busy", false)).toBe(
      "Whisperstream is already updating a model. Try again when it finishes.",
    );
    expect(
      sttSwitchFailureMessage("Qwen3 ASR", "dictation_active", false),
    ).toBe("Finish your current dictation, then switch models.");
  });
});

describe("sttStartRejectionMessage", () => {
  it("names dictation as the blocker when the backend says so", () => {
    expect(
      sttStartRejectionMessage(
        '409: {"status":"busy","reason":"dictation_active"}',
        "Qwen3 ASR",
      ),
    ).toBe("Finish your current dictation, then switch models.");
  });

  it("names another model operation for a busy refusal", () => {
    expect(
      sttStartRejectionMessage(
        '409: {"status":"busy","reason":"operation_busy"}',
        "Qwen3 ASR",
      ),
    ).toBe("Whisperstream is already updating a model. Try again when it finishes.");
  });

  it("falls back to the locked recovery step for anything else", () => {
    expect(sttStartRejectionMessage("500: boom", "NVIDIA Parakeet")).toBe(
      "Could not switch to NVIDIA Parakeet. Try again.",
    );
    expect(sttStartRejectionMessage(undefined, "NVIDIA Parakeet")).toBe(
      "Could not switch to NVIDIA Parakeet. Try again.",
    );
  });
});

describe("sttRoutingSaveFailureMessage", () => {
  it("uses locked recovery copy without exposing transport detail", () => {
    const message = sttRoutingSaveFailureMessage("Qwen3 ASR");
    expect(message).toBe("Could not switch to Qwen3 ASR. Try again.");
    expect(message).not.toMatch(/save|500|payload/i);
  });
});

describe("sttDownloadFailureCopy", () => {
  it("never leaks the raw payload of a refused start", () => {
    const copy = sttDownloadFailureCopy("Qwen3 ASR", {
      source: "start",
      message: '409: {"status":"busy","reason":"dictation_active"}',
    });

    expect(copy.title).toBe("Could not switch models");
    expect(copy.message).toBe(
      "Finish your current dictation, then switch models.",
    );
    expect(copy.message).not.toContain("409");
  });

  it("keeps the download heading for a transfer failure", () => {
    const copy = sttDownloadFailureCopy("Qwen3 ASR", {
      source: "lifecycle",
      reason: "download_failed",
      restored: true,
      message: "Could not switch models. Whisperstream restored your previous settings.",
    });

    expect(copy.title).toBe("Download failed");
    expect(copy.message).toBe(
      "Qwen3 ASR did not finish downloading. Check your connection and try again.",
    );
  });

  it("does not call an activation failure a download failure", () => {
    const copy = sttDownloadFailureCopy("Qwen3 ASR", {
      source: "lifecycle",
      reason: "activation_failed",
      restored: true,
    });

    expect(copy.title).toBe("Could not switch models");
    expect(copy.message).toBe(
      "Could not switch to Qwen3 ASR. Whisperstream restored your previous settings.",
    );
  });

  it("reports an unrestored activation failure without promising a rollback", () => {
    const copy = sttDownloadFailureCopy("Qwen3 ASR", {
      source: "lifecycle",
      reason: "activation_failed",
      restored: false,
    });

    expect(copy.message).toBe("Could not switch to Qwen3 ASR. Try again.");
  });
});
