import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RecoveryUnlockScreen } from "../RecoveryUnlockScreen";

const api = vi.hoisted(() => ({
  unlockLockdown: vi.fn(),
  unlockHistoryWithRecoveryKey: vi.fn(),
  unlockHistoryWithRecoveryPassword: vi.fn(),
}));
vi.mock("../../lib/api/lockdown", async () => ({
  ...await vi.importActual<typeof import("../../lib/api/lockdown")>("../../lib/api/lockdown"),
  unlockLockdown: api.unlockLockdown,
}));
vi.mock("../../lib/api/historyRecovery", async () => ({
  ...await vi.importActual<typeof import("../../lib/api/historyRecovery")>("../../lib/api/historyRecovery"),
  unlockHistoryWithRecoveryKey: api.unlockHistoryWithRecoveryKey,
  unlockHistoryWithRecoveryPassword: api.unlockHistoryWithRecoveryPassword,
}));

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("RecoveryUnlockScreen, missing key", () => {
  it("restores with the recovery key and keeps saving paused until then", async () => {
    api.unlockHistoryWithRecoveryKey.mockResolvedValue({ ok: true });
    const onUnlocked = vi.fn();
    render(<RecoveryUnlockScreen variant="missing-key" platform="mac" hasRecoveryPassword onUnlocked={onUnlocked} onStartFresh={vi.fn()} />);

    expect(screen.getByText("Key missing")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Restore your transcript history" })).toBeInTheDocument();
    expect(screen.getByText(
      "Keychain on this Mac no longer has the key for your saved transcripts. Enter your recovery key or recovery password to restore it.",
    )).toBeInTheDocument();
    expect(screen.getByText("Saving is paused until you restore or start fresh.")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Restore with" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Recovery key"), { target: { value: " WSRK-1111-2222-3333-4444 " } });
    fireEvent.click(screen.getByRole("button", { name: "Restore history" }));
    await waitFor(() => expect(onUnlocked).toHaveBeenCalledOnce());
    expect(api.unlockHistoryWithRecoveryKey).toHaveBeenCalledWith("WSRK-1111-2222-3333-4444");
  });

  it("shows the wrong-key line, then the password path with its own error", async () => {
    api.unlockHistoryWithRecoveryKey.mockResolvedValue({ ok: false, error: "recovery_key_invalid", lockout_remaining_seconds: 0 });
    api.unlockHistoryWithRecoveryPassword.mockResolvedValue({ ok: false, error: "recovery_password_invalid", lockout_remaining_seconds: 0 });
    render(<RecoveryUnlockScreen variant="missing-key" platform="mac" hasRecoveryPassword onUnlocked={vi.fn()} onStartFresh={vi.fn()} />);

    fireEvent.change(screen.getByLabelText("Recovery key"), { target: { value: "WRONG" } });
    fireEvent.click(screen.getByRole("button", { name: "Restore history" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That recovery key didn't match. Check it and try again.",
    );

    fireEvent.click(screen.getByRole("button", { name: "Recovery password" }));
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.change(screen.getByLabelText("Recovery password"), { target: { value: "not it at all" } });
    fireEvent.click(screen.getByRole("button", { name: "Restore history" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect recovery password. Try again.");
    expect(api.unlockHistoryWithRecoveryPassword).toHaveBeenCalledWith("not it at all");
  });

  it("hides the password tab and uses key-only copy when no password is set", () => {
    render(<RecoveryUnlockScreen variant="missing-key" platform="windows" onUnlocked={vi.fn()} onStartFresh={vi.fn()} />);
    expect(screen.queryByRole("group", { name: "Restore with" })).toBeNull();
    expect(screen.getByText(
      "Windows on this PC no longer has the key for your saved transcripts. Enter your recovery key to restore it.",
    )).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Lost your key? Start fresh" })).toBeInTheDocument();
  });

  it("counts down a backoff while Start fresh stays reachable", async () => {
    vi.useFakeTimers();
    const onStartFresh = vi.fn();
    render(<RecoveryUnlockScreen variant="missing-key" platform="mac" hasRecoveryPassword initialLockoutSeconds={61} onUnlocked={vi.fn()} onStartFresh={onStartFresh} />);
    expect(screen.getByText("Too many attempts. Try again in 1:01.")).toBeInTheDocument();
    expect(screen.getByLabelText("Recovery key")).toBeDisabled();
    act(() => { vi.advanceTimersByTime(2000); });
    expect(screen.getByText("Too many attempts. Try again in 0:59.")).toBeInTheDocument();
    // The password counter is separate, so that tab is usable.
    fireEvent.click(screen.getByRole("button", { name: "Recovery password" }));
    expect(screen.getByLabelText("Recovery password")).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Lost both? Start fresh" }));
    expect(onStartFresh).toHaveBeenCalledOnce();
  });

  it("starts a backoff returned by a failed attempt", async () => {
    api.unlockHistoryWithRecoveryKey.mockResolvedValue({ ok: false, error: "recovery_key_invalid", lockout_remaining_seconds: 60 });
    render(<RecoveryUnlockScreen variant="missing-key" platform="mac" onUnlocked={vi.fn()} onStartFresh={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Recovery key"), { target: { value: "WRONG" } });
    fireEvent.click(screen.getByRole("button", { name: "Restore history" }));
    expect(await screen.findByText("Too many attempts. Try again in 1:00.")).toBeInTheDocument();
  });

  it.each([
    ["mac", "Couldn't save the key to Keychain. Allow access if asked, then try again."],
    ["windows", "Couldn't save the key to your Windows account. Try again."],
  ] as const)("explains a denied restore on %s", async (platform, message) => {
    api.unlockHistoryWithRecoveryKey.mockRejectedValue(new Error('503: {"ok":false,"error":"restore_access_denied"}'));
    render(<RecoveryUnlockScreen variant="missing-key" platform={platform} onUnlocked={vi.fn()} onStartFresh={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Recovery key"), { target: { value: "WSRK" } });
    fireEvent.click(screen.getByRole("button", { name: "Restore history" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
  });
});

describe("RecoveryUnlockScreen, no recovery set up", () => {
  it("offers only Start fresh with the explanation", () => {
    const onStartFresh = vi.fn();
    render(<RecoveryUnlockScreen variant="missing-key" platform="windows" hasRecoveryKey={false} onUnlocked={vi.fn()} onStartFresh={onStartFresh} />);
    expect(screen.getByText("Windows on this PC no longer has the key for your saved transcripts.")).toBeInTheDocument();
    expect(screen.getByText("No recovery key was set up, so they can't be restored.")).toBeInTheDocument();
    expect(screen.getByText("Saving is paused until you restore or start fresh.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByLabelText("Recovery password")).toBeNull();
    expect(screen.queryByRole("button", { name: "Restore history" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Start fresh" }));
    expect(onStartFresh).toHaveBeenCalledOnce();
  });

  it("keeps the entry form when a recovery password exists without a key", () => {
    render(<RecoveryUnlockScreen variant="missing-key" platform="mac" hasRecoveryKey={false} hasRecoveryPassword onUnlocked={vi.fn()} onStartFresh={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Restore history" })).toBeInTheDocument();
  });
});

describe("RecoveryUnlockScreen, stale envelope", () => {
  it("shows the generic restore error", async () => {
    api.unlockHistoryWithRecoveryKey.mockRejectedValue(new Error('409: {"ok":false,"error":"recovery_key_stale"}'));
    render(<RecoveryUnlockScreen variant="missing-key" platform="mac" onUnlocked={vi.fn()} onStartFresh={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Recovery key"), { target: { value: "WSRK" } });
    fireEvent.click(screen.getByRole("button", { name: "Restore history" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't restore history. Try again.");
  });
});

describe("RecoveryUnlockScreen, Lockdown", () => {
  it("keeps the Lockdown copy and offers the recovery key path", async () => {
    api.unlockLockdown.mockResolvedValue({ ok: true });
    const onUnlocked = vi.fn();
    render(<RecoveryUnlockScreen onUnlocked={onUnlocked} />);
    expect(screen.getByRole("heading", { name: "Transcripts are locked" })).toBeInTheDocument();
    expect(screen.queryByText("Key missing")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Use recovery key instead" }));
    expect(screen.getByText(
      "Enter the recovery key you saved when you turned on Lockdown Mode. You'll set a new password next.",
    )).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Recovery key"), { target: { value: "WS-RECOVERY" } });
    fireEvent.click(screen.getByRole("button", { name: "Unlock" }));
    await waitFor(() => expect(onUnlocked).toHaveBeenCalledOnce());
    expect(api.unlockLockdown).toHaveBeenCalledWith({ recovery_code: "WS-RECOVERY" });
  });

  it("shows the Lockdown wrong-password line", async () => {
    api.unlockLockdown.mockResolvedValue({ ok: false, lockout_remaining_seconds: 0 });
    render(<RecoveryUnlockScreen onUnlocked={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Transcript password"), { target: { value: "nope" } });
    fireEvent.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect password. Try again.");
  });
});
