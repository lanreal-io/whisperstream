import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HistoryRecoverySetupDialog } from "../HistoryRecoverySetupDialog";

const api = vi.hoisted(() => ({
  enableTranscriptHistory: vi.fn(),
  setupHistoryRecovery: vi.fn(),
  confirmHistoryRecovery: vi.fn(),
}));
vi.mock("../../lib/api/transcripts", async () => ({
  ...await vi.importActual<typeof import("../../lib/api/transcripts")>("../../lib/api/transcripts"),
  enableTranscriptHistory: api.enableTranscriptHistory,
}));
vi.mock("../../lib/api/historyRecovery", async () => ({
  ...await vi.importActual<typeof import("../../lib/api/historyRecovery")>("../../lib/api/historyRecovery"),
  setupHistoryRecovery: api.setupHistoryRecovery,
  confirmHistoryRecovery: api.confirmHistoryRecovery,
}));
vi.mock("../../lib/lockdownRecovery", () => ({
  downloadRecoveryFile: vi.fn().mockResolvedValue("saved"),
  printRecoveryFile: vi.fn(),
}));

const KEY = "WSRK-1111-2222-3333-4444";

beforeEach(() => {
  api.enableTranscriptHistory.mockResolvedValue({ ok: true, needs_recovery_setup: true });
  api.setupHistoryRecovery.mockResolvedValue({ ok: true, recovery_key: KEY });
  api.confirmHistoryRecovery.mockResolvedValue({ ok: true });
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderDialog(props: Partial<Parameters<typeof HistoryRecoverySetupDialog>[0]> = {}) {
  const onComplete = vi.fn();
  const onCancel = vi.fn();
  render(
    <HistoryRecoverySetupDialog
      open
      platform="mac"
      reason="enable"
      saveAudio
      onComplete={onComplete}
      onCancel={onCancel}
      {...props}
    />,
  );
  return { onComplete, onCancel };
}

describe("HistoryRecoverySetupDialog", () => {
  it("opens existing-user setup on the key step with the required copy", async () => {
    const { onComplete } = renderDialog({ reason: "required", platform: "windows" });
    expect(await screen.findByRole("heading", { name: "Save your recovery key" })).toBeInTheDocument();
    expect(screen.getByText(
      "If this PC ever loses its key and you don't have your recovery key, saved transcripts can't be opened.",
    )).toBeInTheDocument();
    expect(api.enableTranscriptHistory).not.toHaveBeenCalled();
    expect(await screen.findByLabelText("Recovery key", { selector: "code" })).toHaveTextContent(KEY);
    // No Back on a step with no intro; the X is the way out.
    expect(screen.queryByRole("button", { name: "Back" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel setup" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Download recovery key" }));
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Done" })).not.toHaveAttribute("aria-disabled"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
  });

  it("uses the Start fresh copy for a new key", async () => {
    renderDialog({ reason: "start-fresh" });
    expect(await screen.findByRole("heading", { name: "Save your new recovery key" })).toBeInTheDocument();
    expect(screen.getByText(
      "Your old history was deleted. This key protects everything you save from now on.",
    )).toBeInTheDocument();
  });

  it("shows a retry when a key can't be created", async () => {
    api.setupHistoryRecovery.mockRejectedValueOnce(new Error("500: {}"));
    renderDialog({ reason: "required" });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't set up your recovery key. Try again.",
    );
    const done = screen.getByRole("button", { name: "Done" });
    expect(done).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByLabelText("Recovery key", { selector: "code" })).toHaveTextContent(KEY);
  });

  it("reports a confirm failure without closing", async () => {
    api.confirmHistoryRecovery.mockRejectedValueOnce(new Error("500: {}"));
    const { onComplete } = renderDialog({ reason: "required" });
    fireEvent.click(await screen.findByRole("button", { name: "Copy recovery key" }));
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Done" })).not.toHaveAttribute("aria-disabled"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't set up your recovery key. Try again.",
    );
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("returns to the intro with Back after enabling", async () => {
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Enable saving" }));
    await screen.findByRole("heading", { name: "Keep a recovery key" });
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByRole("heading", { name: "Save transcripts on this Mac?" })).toBeInTheDocument();
  });

  it("shows the Keychain wait line while enabling on Mac", async () => {
    let resolve!: (value: unknown) => void;
    api.enableTranscriptHistory.mockReturnValueOnce(new Promise((r) => { resolve = r; }));
    const { onCancel } = renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Enable saving" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Waiting for Keychain. Allow access to Whisperstream's encryption key if asked.",
    );
    expect(screen.getByRole("button", { name: "Enabling..." })).toBeDisabled();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).not.toHaveBeenCalled();
    resolve({ ok: true, needs_recovery_setup: false });
  });
});
