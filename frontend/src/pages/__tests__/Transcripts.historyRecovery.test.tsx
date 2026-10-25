import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppConfig } from "../../lib/api/config";
import type { HistoryRecoveryStatus } from "../../lib/api/historyRecovery";
import type { DailyUseCapabilityReport } from "../../lib/dailyUseCapabilities";
import { ExperimentalProvider } from "../../lib/experimental";
import { Transcripts } from "../Transcripts";

const mocks = vi.hoisted(() => ({
  listTranscripts: vi.fn(),
  enableTranscriptHistory: vi.fn(),
  getHistoryRecoveryStatus: vi.fn(),
  unlockHistoryWithRecoveryKey: vi.fn(),
  resetHistory: vi.fn(),
  setupHistoryRecovery: vi.fn(),
  confirmHistoryRecovery: vi.fn(),
}));
vi.mock("../../lib/api/transcripts", async () => ({
  ...await vi.importActual<typeof import("../../lib/api/transcripts")>("../../lib/api/transcripts"),
  listTranscripts: mocks.listTranscripts,
  enableTranscriptHistory: mocks.enableTranscriptHistory,
}));
vi.mock("../../lib/api/historyRecovery", async () => ({
  ...await vi.importActual<typeof import("../../lib/api/historyRecovery")>("../../lib/api/historyRecovery"),
  getHistoryRecoveryStatus: mocks.getHistoryRecoveryStatus,
  unlockHistoryWithRecoveryKey: mocks.unlockHistoryWithRecoveryKey,
  resetHistory: mocks.resetHistory,
  setupHistoryRecovery: mocks.setupHistoryRecovery,
  confirmHistoryRecovery: mocks.confirmHistoryRecovery,
}));
vi.mock("../../lib/lockdownRecovery", () => ({
  downloadRecoveryFile: vi.fn().mockResolvedValue("saved"),
  printRecoveryFile: vi.fn(),
}));

const baseStatus: HistoryRecoveryStatus = {
  lockdown: false, has_recovery_key: true, has_recovery_password: false, key_missing: false,
  key_mismatch: false,
  needs_setup: false, record_count: 3, has_audio: false,
  key_lockout_remaining_seconds: 0, password_lockout_remaining_seconds: 0,
};

beforeEach(() => {
  mocks.listTranscripts.mockResolvedValue({ items: [], next_cursor: null, availability: "key-missing" });
  mocks.getHistoryRecoveryStatus.mockResolvedValue({ ...baseStatus, key_missing: true });
  mocks.setupHistoryRecovery.mockResolvedValue({ ok: true, recovery_key: "WSRK-1111-2222-3333-4444" });
  mocks.confirmHistoryRecovery.mockResolvedValue({ ok: true });
  mocks.enableTranscriptHistory.mockResolvedValue({ ok: true, needs_recovery_setup: false });
  vi.stubGlobal("fetch", vi.fn(() => { throw new Error("Unexpected network access"); }));
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

function setup(overrides: Partial<AppConfig> = {}) {
  const onConfigChange = vi.fn();
  render(<ExperimentalProvider enabled={false}><Transcripts
    config={{ history_enabled: true, save_audio: true, transcript_retention_days: 7, lockdown_mode: false, mac_history_opt_in: true, ...overrides } as AppConfig}
    capabilities={{ platform: "Darwin" } as DailyUseCapabilityReport}
    lockStatus={{ locked: false, enabled: false, hasEnvelope: false } as never}
    onConfigChange={onConfigChange} subscribe={() => () => {}}
  /></ExperimentalProvider>);
  return { onConfigChange };
}

describe("Transcripts whose key can't read saved history", () => {
  it("offers the same restore screen as a missing key", async () => {
    mocks.listTranscripts.mockResolvedValue({ items: [], next_cursor: null, availability: "not-decryptable" });
    mocks.getHistoryRecoveryStatus.mockResolvedValue({ ...baseStatus, key_mismatch: true });
    setup();
    expect(await screen.findByRole("heading", { name: "Restore your transcript history" })).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Search transcripts...")).toBeNull();
  });
});

describe("Transcripts with a missing key", () => {
  it("replaces the list with the restore screen and restores", async () => {
    mocks.unlockHistoryWithRecoveryKey.mockResolvedValue({ ok: true });
    setup();
    expect(await screen.findByRole("heading", { name: "Restore your transcript history" })).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Search transcripts...")).toBeNull();
    expect(screen.queryByText(/encryption key is unavailable/)).toBeNull();

    mocks.getHistoryRecoveryStatus.mockResolvedValue(baseStatus);
    mocks.listTranscripts.mockResolvedValue({ items: [], next_cursor: null, availability: "empty" });
    fireEvent.change(screen.getByLabelText("Recovery key"), { target: { value: "WSRK-1111-2222-3333-4444" } });
    fireEvent.click(screen.getByRole("button", { name: "Restore history" }));
    expect(await screen.findByText("History restored.")).toBeInTheDocument();
    expect(screen.getByText(
      "The key is back in Keychain, so you won't need it again on this Mac.",
    )).toBeInTheDocument();
    // The restore screen unmounts after the follow-up status refresh resolves.
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Restore your transcript history" })).toBeNull(),
    );
  });

  it("Start fresh deletes, then sets up a new key", async () => {
    mocks.resetHistory.mockResolvedValue({ ok: true });
    setup();
    fireEvent.click(await screen.findByRole("button", { name: "Lost your key? Start fresh" }));
    expect(screen.getByText("All 3 saved transcripts are deleted.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    mocks.getHistoryRecoveryStatus.mockResolvedValue({ ...baseStatus, has_recovery_key: false });
    fireEvent.change(screen.getByLabelText("Type DELETE to confirm"), { target: { value: "DELETE" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete all saved transcripts" }));

    expect(await screen.findByRole("heading", { name: "Save your new recovery key" })).toBeInTheDocument();
    expect(mocks.resetHistory).toHaveBeenCalledWith("DELETE");
    // Saving was already on, so it isn't re-enabled.
    expect(mocks.enableTranscriptHistory).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Copy recovery key" }));
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Done" })).not.toHaveAttribute("aria-disabled"));
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(await screen.findByText("Saving is on. Your recovery key is set.")).toBeInTheDocument();
  });

  it("turns saving back on after Start fresh when it was off", async () => {
    mocks.resetHistory.mockResolvedValue({ ok: true });
    const { onConfigChange } = setup({ history_enabled: false });
    fireEvent.click(await screen.findByRole("button", { name: "Lost your key? Start fresh" }));
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.change(screen.getByLabelText("Type DELETE to confirm"), { target: { value: "DELETE" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete all saved transcripts" }));
    await waitFor(() => expect(mocks.enableTranscriptHistory).toHaveBeenCalledWith(true));
    expect(onConfigChange).toHaveBeenCalledWith({ history_enabled: true });
    expect(await screen.findByRole("heading", { name: "Save your new recovery key" })).toBeInTheDocument();
  });

  it("offers only Start fresh when no recovery was ever set up", async () => {
    mocks.getHistoryRecoveryStatus.mockResolvedValue({ ...baseStatus, key_missing: true, has_recovery_key: false, has_recovery_password: false });
    setup();
    expect(await screen.findByText("No recovery key was set up, so they can't be restored.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Recovery key")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Start fresh" }));
    expect(screen.getByRole("heading", { name: "Start fresh without your history?" })).toBeInTheDocument();
  });

  it("never shows the restore screen under Lockdown", async () => {
    render(<ExperimentalProvider enabled={false}><Transcripts
      config={{ history_enabled: true, save_audio: true, transcript_retention_days: 7, lockdown_mode: true } as AppConfig}
      capabilities={{ platform: "Darwin" } as DailyUseCapabilityReport}
      lockStatus={{ locked: false, enabled: true, hasEnvelope: true } as never}
      onConfigChange={vi.fn()} subscribe={() => () => {}}
    /></ExperimentalProvider>);
    await waitFor(() => expect(mocks.listTranscripts).toHaveBeenCalled());
    expect(mocks.getHistoryRecoveryStatus).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "Restore your transcript history" })).toBeNull();
  });
});

describe("Transcripts for existing users without a recovery key", () => {
  beforeEach(() => {
    mocks.listTranscripts.mockResolvedValue({ items: [], next_cursor: null, availability: "ready" });
    mocks.getHistoryRecoveryStatus.mockResolvedValue({ ...baseStatus, has_recovery_key: false, needs_setup: true, record_count: 142 });
  });

  async function finishRequiredSetup() {
    expect(await screen.findByRole("heading", { name: "Save your recovery key" })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Copy recovery key" }));
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Done" })).not.toHaveAttribute("aria-disabled"));
    mocks.getHistoryRecoveryStatus.mockResolvedValue(baseStatus);
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
  }

  it("shows only the gate, then lifts it after setup", async () => {
    setup();
    expect(await screen.findByRole("heading", { name: "Finish recovery setup" })).toBeInTheDocument();
    expect(screen.getByText("Saving is on. 142 transcripts saved.")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Search transcripts...")).toBeNull();
    expect(screen.queryByRole("button", { name: "Transcript settings" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Set up recovery key" }));
    await finishRequiredSetup();
    expect(await screen.findByText("Recovery key set. Your transcripts are backed up.")).toBeInTheDocument();
    // The notice renders in the same commit that lifts the gate, but the gate
    // fades out through AnimatePresence and unmounts only when its exit
    // completes, one animation frame later even with skipAnimations.
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Finish recovery setup" })).toBeNull(),
    );
    expect(screen.getByPlaceholderText("Search transcripts...")).toBeInTheDocument();
  });

  it("cancelling the required setup keeps the gate", async () => {
    setup();
    fireEvent.click(await screen.findByRole("button", { name: "Set up recovery key" }));
    await screen.findByRole("heading", { name: "Save your recovery key" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel setup" }));
    expect(await screen.findByRole("heading", { name: "Finish recovery setup" })).toBeInTheDocument();
    expect(mocks.confirmHistoryRecovery).not.toHaveBeenCalled();
  });

  it("opens the required setup from the launch prompt request", async () => {
    render(<ExperimentalProvider enabled={false}><Transcripts
      config={{ history_enabled: true, save_audio: true, transcript_retention_days: 7, lockdown_mode: false } as AppConfig}
      capabilities={{ platform: "Darwin" } as DailyUseCapabilityReport}
      lockStatus={{ locked: false, enabled: false, hasEnvelope: false } as never}
      onConfigChange={vi.fn()} subscribe={() => () => {}} recoverySetupRequest={1}
    /></ExperimentalProvider>);
    expect(await screen.findByRole("heading", { name: "Save your recovery key" })).toBeInTheDocument();
  });
});

describe("Recovery settings link", () => {
  it("opens settings from the setup note", async () => {
    mocks.listTranscripts.mockResolvedValue({ items: [], next_cursor: null, availability: "empty" });
    mocks.getHistoryRecoveryStatus.mockResolvedValue({ ...baseStatus, has_recovery_key: false });
    mocks.enableTranscriptHistory.mockResolvedValue({ ok: true, needs_recovery_setup: false });
    setup({ history_enabled: false });
    fireEvent.click(await screen.findByRole("switch", { name: "Save transcripts" }));
    fireEvent.click(await screen.findByRole("button", { name: "Enable saving" }));
    fireEvent.click(await screen.findByRole("button", { name: "Recovery settings" }));
    expect(await screen.findByRole("heading", { name: "Recovery" })).toBeInTheDocument();
  });
});


describe("Recovery settings in the gear menu", () => {
  it("opens recovery settings any time a confirmed key exists", async () => {
    mocks.listTranscripts.mockResolvedValue({ items: [], next_cursor: null, availability: "empty" });
    mocks.getHistoryRecoveryStatus.mockResolvedValue(baseStatus);
    setup();
    fireEvent.click(await screen.findByRole("button", { name: "Transcript settings" }));
    const item = await screen.findByRole("menuitem", { name: "Recovery settings" });
    fireEvent.click(item);
    expect(await screen.findByRole("heading", { name: "Recovery" })).toBeInTheDocument();
    // The menu leaves through an exit animation, so its removal lands in a
    // later render than the dialog heading.
    await waitFor(() =>
      expect(screen.queryByRole("menu", { name: "Transcript settings" })).toBeNull(),
    );
  });

  it("is absent without a confirmed key", async () => {
    mocks.listTranscripts.mockResolvedValue({ items: [], next_cursor: null, availability: "empty" });
    mocks.getHistoryRecoveryStatus.mockResolvedValue({ ...baseStatus, has_recovery_key: false });
    setup();
    fireEvent.click(await screen.findByRole("button", { name: "Transcript settings" }));
    await screen.findByRole("menu", { name: "Transcript settings" });
    expect(screen.queryByRole("menuitem", { name: "Recovery settings" })).toBeNull();
  });

  it("is absent under Lockdown", async () => {
    mocks.listTranscripts.mockResolvedValue({ items: [], next_cursor: null, availability: "empty" });
    mocks.getHistoryRecoveryStatus.mockResolvedValue(baseStatus);
    render(<ExperimentalProvider enabled={false}><Transcripts
      config={{ history_enabled: true, save_audio: true, transcript_retention_days: 7, lockdown_mode: true } as AppConfig}
      capabilities={{ platform: "Darwin" } as DailyUseCapabilityReport}
      lockStatus={{ locked: false, enabled: true, hasEnvelope: true } as never}
      onConfigChange={vi.fn()} subscribe={() => () => {}}
      recovery={{ status: baseStatus, refresh: vi.fn() }}
    /></ExperimentalProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Transcript settings" }));
    await screen.findByRole("menu", { name: "Transcript settings" });
    expect(screen.queryByRole("menuitem", { name: "Recovery settings" })).toBeNull();
  });
});
