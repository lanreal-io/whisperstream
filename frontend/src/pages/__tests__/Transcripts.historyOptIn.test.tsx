import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppConfig } from "../../lib/api/config";
import type { DailyUseCapabilityReport } from "../../lib/dailyUseCapabilities";
import { ExperimentalProvider } from "../../lib/experimental";
import { Transcripts } from "../Transcripts";

const mocks = vi.hoisted(() => ({
  enableTranscriptHistory: vi.fn(),
  listTranscripts: vi.fn(),
  getHistoryRecoveryStatus: vi.fn(),
  setupHistoryRecovery: vi.fn(),
  confirmHistoryRecovery: vi.fn(),
}));
vi.mock("../../lib/api/config", async () => ({
    ...await vi.importActual<typeof import("../../lib/api/config")>("../../lib/api/config"),
  }));
vi.mock("../../lib/api/transcripts", async () => ({
    ...await vi.importActual<typeof import("../../lib/api/transcripts")>("../../lib/api/transcripts"),
    enableTranscriptHistory: mocks.enableTranscriptHistory,
    listTranscripts: mocks.listTranscripts,
  }));
vi.mock("../../lib/api/historyRecovery", async () => ({
    ...await vi.importActual<typeof import("../../lib/api/historyRecovery")>("../../lib/api/historyRecovery"),
    getHistoryRecoveryStatus: mocks.getHistoryRecoveryStatus,
    setupHistoryRecovery: mocks.setupHistoryRecovery,
    confirmHistoryRecovery: mocks.confirmHistoryRecovery,
  }));
vi.mock("../../lib/lockdownRecovery", () => ({
  downloadRecoveryFile: vi.fn().mockResolvedValue("saved"),
  printRecoveryFile: vi.fn(),
}));

const KEY = "WSRK-1111-2222-3333-4444";

beforeEach(() => {
  mocks.listTranscripts.mockResolvedValue({ items: [], next_cursor: null, availability: "empty" });
  mocks.enableTranscriptHistory.mockResolvedValue({ ok: true, needs_recovery_setup: true });
  mocks.getHistoryRecoveryStatus.mockResolvedValue({
    lockdown: false, has_recovery_key: false, has_recovery_password: false, key_missing: false,
    needs_setup: false, record_count: 0, has_audio: false,
    key_lockout_remaining_seconds: 0, password_lockout_remaining_seconds: 0,
  });
  mocks.setupHistoryRecovery.mockResolvedValue({ ok: true, recovery_key: KEY, generation: "gen-1" });
  mocks.confirmHistoryRecovery.mockResolvedValue({ ok: true });
  vi.stubGlobal("fetch", vi.fn(() => { throw new Error("Unexpected network access"); }));
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

function setup(platform = "Darwin", overrides: Partial<AppConfig> = {}, lockedEnvelope = false, historyDisabled = false) {
  const onConfigChange = vi.fn();
  const onRequestLockdownSetup = vi.fn();
  render(<ExperimentalProvider enabled={false}><Transcripts
    config={{ history_enabled: false, save_audio: true, transcript_retention_days: 7, lockdown_mode: false, ...overrides } as AppConfig}
    capabilities={{ platform } as DailyUseCapabilityReport}
    lockStatus={{ locked: false, hasEnvelope: lockedEnvelope, historyDisabled } as never}
    onConfigChange={onConfigChange} onRequestLockdownSetup={onRequestLockdownSetup} subscribe={() => () => {}}
  /></ExperimentalProvider>);
  return { onConfigChange, onRequestLockdownSetup };
}

async function openFromPaused(name = "Save transcripts on this Mac?") {
  fireEvent.click(await screen.findByRole("switch", { name: "Save transcripts" }));
  return screen.findByRole("dialog", { name });
}

async function saveKeyAndFinish() {
  fireEvent.click(await screen.findByRole("button", { name: "Copy recovery key" }));
  fireEvent.click(screen.getByRole("checkbox", { name: /I saved my recovery key somewhere other than/ }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Done" })).not.toHaveAttribute("aria-disabled"));
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
}

describe("turning on transcript history", () => {
  it("explains saving on Mac before any key access and cancels with the X or Esc", async () => {
    const { onConfigChange } = setup();
    expect(await screen.findByText("Transcript history is off")).toBeInTheDocument();
    await openFromPaused();
    expect(screen.getByText(/This permission covers only that key/)).toBeInTheDocument();
    expect(screen.getByText(
      "If this Mac loses its Keychain key and you also lose your recovery key and recovery password, your saved transcripts can't be recovered.",
    )).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Not now" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable saving" })).toHaveFocus();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await openFromPaused();
    fireEvent.click(screen.getByRole("button", { name: "Cancel setup" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mocks.enableTranscriptHistory).not.toHaveBeenCalled();
    expect(onConfigChange).not.toHaveBeenCalled();
  });

  it("shows only the loss disclosure on Windows", async () => {
    setup("Windows");
    await openFromPaused("Save transcripts on this PC?");
    expect(screen.queryByText(/Keychain/)).not.toBeInTheDocument();
    expect(screen.getByText(
      "If this PC loses its encryption key and you also lose your recovery key and recovery password, your saved transcripts can't be recovered.",
    )).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Enable saving" }));
    expect(await screen.findByRole("heading", { name: "Keep a recovery key" })).toBeInTheDocument();
    expect(mocks.enableTranscriptHistory).toHaveBeenCalledWith(true);
    expect(screen.getByText(
      "History opens on this PC without a password. This key is your backup if this PC ever loses its encryption key.",
    )).toBeInTheDocument();
    expect(screen.getByRole("checkbox", {
      name: "I saved my recovery key somewhere other than this PC.",
    })).toBeInTheDocument();
  });

  it("enables, then requires the recovery key gate before Done confirms", async () => {
    const { onConfigChange } = setup();
    await openFromPaused();
    fireEvent.click(screen.getByRole("button", { name: "Enable saving" }));
    expect(await screen.findByRole("heading", { name: "Keep a recovery key" })).toBeInTheDocument();
    expect(onConfigChange).toHaveBeenCalledWith({ history_enabled: true });
    expect(mocks.setupHistoryRecovery).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("Recovery key", { selector: "code" })).toHaveTextContent(KEY);

    const done = screen.getByRole("button", { name: "Done" });
    expect(done).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(done);
    expect(mocks.confirmHistoryRecovery).not.toHaveBeenCalled();

    await saveKeyAndFinish();
    await waitFor(() => expect(mocks.confirmHistoryRecovery).toHaveBeenCalledWith("gen-1", undefined));
    expect(await screen.findByText("Saving is on. Your recovery key is set.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("sends the optional recovery password and shows the backend rule error", async () => {
    setup();
    await openFromPaused();
    fireEvent.click(screen.getByRole("button", { name: "Enable saving" }));
    await screen.findByRole("heading", { name: "Keep a recovery key" });
    fireEvent.click(screen.getByRole("button", { name: /Also add a recovery password/ }));
    fireEvent.change(screen.getByLabelText("Recovery password"), { target: { value: "passwordpassword" } });
    fireEvent.change(screen.getByLabelText("Confirm recovery password"), { target: { value: "passwordpassword" } });
    mocks.confirmHistoryRecovery.mockRejectedValueOnce(new Error('400: {"ok":false,"error":"password_too_common"}'));
    await saveKeyAndFinish();
    expect(await screen.findByText("That password is too common. Try a longer phrase.")).toBeInTheDocument();
    expect(mocks.confirmHistoryRecovery).toHaveBeenCalledWith("gen-1", "passwordpassword");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("finishes without a key step when recovery already exists", async () => {
    mocks.enableTranscriptHistory.mockResolvedValueOnce({ ok: true, needs_recovery_setup: false });
    const { onConfigChange } = setup("Darwin", { mac_history_opt_in: true });
    await openFromPaused();
    fireEvent.click(screen.getByRole("button", { name: "Enable saving" }));
    expect(await screen.findByText("Saving is on. Your recovery key is set.")).toBeInTheDocument();
    expect(onConfigChange).toHaveBeenCalledWith({ history_enabled: true });
    expect(mocks.setupHistoryRecovery).not.toHaveBeenCalled();
  });

  it.each([
    ["Darwin", "Save transcripts on this Mac?", "Saving is still off. Keychain didn't allow access. Try again."],
    ["Windows", "Save transcripts on this PC?", "Saving is still off. Windows couldn't protect the encryption key. Try again."],
  ])("keeps saving off on denial and allows retry (%s)", async (platform, title, message) => {
    const { onConfigChange } = setup(platform, { save_audio: false });
    mocks.enableTranscriptHistory.mockRejectedValueOnce(new Error('503: {"error":"history_key_unavailable"}'));
    await openFromPaused(title);
    expect(screen.getByText(/only text will be saved/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Enable saving" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(onConfigChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Enable saving" }));
    await waitFor(() => expect(onConfigChange).toHaveBeenCalledOnce());
  });

  it("hands a missing protected key to the missing-key screen", async () => {
    setup();
    mocks.enableTranscriptHistory.mockRejectedValueOnce(new Error('409: {"error":"history_key_missing"}'));
    await openFromPaused();
    const statusCalls = mocks.getHistoryRecoveryStatus.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Enable saving" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mocks.getHistoryRecoveryStatus.mock.calls.length).toBeGreaterThan(statusCalls);
  });

  it("cancelling at the key step leaves saving on", async () => {
    const { onConfigChange } = setup();
    await openFromPaused();
    fireEvent.click(screen.getByRole("button", { name: "Enable saving" }));
    await screen.findByRole("heading", { name: "Keep a recovery key" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel setup" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(onConfigChange).toHaveBeenCalledWith({ history_enabled: true });
    expect(mocks.confirmHistoryRecovery).not.toHaveBeenCalled();
  });

  it("opens the same setup from the settings toggle", async () => {
    setup("Windows");
    fireEvent.click(screen.getByRole("button", { name: "Transcript settings" }));
    const settings = await screen.findByRole("menu", { name: "Transcript settings" });
    fireEvent.click(within(settings).getAllByRole("switch")[0]);
    expect(await screen.findByRole("dialog", { name: "Save transcripts on this PC?" })).toBeInTheDocument();
    expect(mocks.enableTranscriptHistory).not.toHaveBeenCalled();
  });

  it("uses password consent without a key step for an unlocked Lockdown envelope", async () => {
    const { onConfigChange } = setup("Darwin", { lockdown_mode: true }, true);
    await openFromPaused();
    expect(screen.getByText(/protected by your Lockdown password/)).toBeInTheDocument();
    expect(screen.queryByText(/Keychain/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Enable saving" }));
    await waitFor(() => expect(onConfigChange).toHaveBeenCalledOnce());
    expect(mocks.enableTranscriptHistory).toHaveBeenCalledWith(false);
    expect(mocks.setupHistoryRecovery).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByText("Saving is on. Your recovery key is set.")).not.toBeInTheDocument();
    expect(mocks.getHistoryRecoveryStatus).not.toHaveBeenCalled();
  });

  it.each(["Windows", "Darwin"])("re-enables Lockdown history directly once opted in on %s", async (platform) => {
    const { onConfigChange } = setup(platform, { lockdown_mode: true, mac_history_opt_in: true }, true);
    fireEvent.click(await screen.findByRole("switch", { name: "Save transcripts" }));
    expect(onConfigChange).toHaveBeenCalledWith({ history_enabled: true });
    expect(mocks.enableTranscriptHistory).not.toHaveBeenCalled();
  });

  it("routes no-envelope Lockdown history to password setup", async () => {
    const { onRequestLockdownSetup, onConfigChange } = setup("Darwin", { lockdown_mode: true });
    fireEvent.click(await screen.findByRole("button", { name: "Set up password" }));
    expect(onRequestLockdownSetup).toHaveBeenCalledOnce();
    expect(onConfigChange).not.toHaveBeenCalled();
    expect(mocks.enableTranscriptHistory).not.toHaveBeenCalled();
  });
});


describe("organization-disabled history", () => {
  it.each([false, true])("blocks saving regardless of Lockdown master (%s) and stale config", async (lockdown) => {
    const { onConfigChange, onRequestLockdownSetup } = setup("Windows", {
      lockdown_mode: lockdown, history_enabled: true, save_audio: true,
    }, false, true);
    expect(await screen.findByRole("heading", { name: "History is disabled" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set up password" })).not.toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "Save transcripts" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Transcript settings" }));
    const menu = await screen.findByRole("menu", { name: "Transcript settings" });
    for (const label of ["Save transcripts", "Save audio"]) {
      const toggle = within(menu).getByRole("switch", { name: label });
      expect(toggle).toBeDisabled();
      expect(toggle).toHaveAttribute("aria-checked", "false");
      fireEvent.click(toggle);
    }
    expect(onConfigChange).not.toHaveBeenCalled();
    expect(onRequestLockdownSetup).not.toHaveBeenCalled();
    expect(mocks.enableTranscriptHistory).not.toHaveBeenCalled();
  });
});
