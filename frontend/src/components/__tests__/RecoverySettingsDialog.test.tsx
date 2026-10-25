import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RecoverySettingsDialog } from "../RecoverySettingsDialog";

const api = vi.hoisted(() => ({
  regenerateHistoryRecoveryKey: vi.fn(),
  setHistoryRecoveryPassword: vi.fn(),
  confirmHistoryRecovery: vi.fn(),
}));
vi.mock("../../lib/api/historyRecovery", async () => ({
  ...await vi.importActual<typeof import("../../lib/api/historyRecovery")>("../../lib/api/historyRecovery"),
  regenerateHistoryRecoveryKey: api.regenerateHistoryRecoveryKey,
  setHistoryRecoveryPassword: api.setHistoryRecoveryPassword,
  confirmHistoryRecovery: api.confirmHistoryRecovery,
}));
vi.mock("../../lib/lockdownRecovery", () => ({
  downloadRecoveryFile: vi.fn().mockResolvedValue("saved"),
  printRecoveryFile: vi.fn(),
}));

const KEY = "WSRK-9999-8888-7777-6666";

beforeEach(() => {
  api.regenerateHistoryRecoveryKey.mockResolvedValue({ ok: true, recovery_key: KEY, generation: "gen-new" });
  api.setHistoryRecoveryPassword.mockResolvedValue({ ok: true, has_recovery_password: true });
  api.confirmHistoryRecovery.mockResolvedValue({ ok: true });
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderDialog(hasRecoveryPassword = false) {
  const onClose = vi.fn();
  const onChanged = vi.fn();
  render(
    <RecoverySettingsDialog
      open
      platform="mac"
      hasRecoveryPassword={hasRecoveryPassword}
      onClose={onClose}
      onChanged={onChanged}
    />,
  );
  return { onClose, onChanged };
}

describe("RecoverySettingsDialog", () => {
  it("lists the key and password rows", () => {
    renderDialog();
    expect(screen.getByRole("heading", { name: "Recovery" })).toBeInTheDocument();
    expect(screen.getByText("Not set")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create new key" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add password" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove password" })).toBeNull();
  });

  it("creates a new key behind a confirm and guards the unsaved key", async () => {
    const { onClose, onChanged } = renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Create new key" }));
    expect(screen.getByRole("heading", { name: "Create a new recovery key?" })).toBeInTheDocument();
    expect(screen.getByText("Your current recovery key will stop working.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create new key" }));

    expect(await screen.findByRole("heading", { name: "Your new recovery key" })).toBeInTheDocument();
    expect(screen.getByText("Your previous recovery key no longer works.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Also add a recovery password/ })).toBeNull();
    // The new key is pending until Done confirms it.
    expect(onChanged).not.toHaveBeenCalled();

    // Esc before saving: the return prompt, not a close.
    fireEvent.keyDown(window, { key: "Escape" });
    const prompt = await screen.findByRole("alert");
    expect(prompt).toHaveTextContent("Copy, print, or download your recovery key to continue.");
    expect(onClose).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("button", { name: "Return to recovery key" })).toHaveFocus());
    fireEvent.click(screen.getByRole("button", { name: "Return to recovery key" }));
    expect(screen.queryByRole("alert")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Copy recovery key" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "I saved my recovery key somewhere other than this Mac." }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Done" })).not.toHaveAttribute("aria-disabled"));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("confirms the new key on Done and only then reports the change", async () => {
    const { onChanged, onClose } = renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Create new key" }));
    fireEvent.click(screen.getByRole("button", { name: "Create new key" }));
    await screen.findByRole("heading", { name: "Your new recovery key" });
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(api.confirmHistoryRecovery).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Copy recovery key" }));
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Done" })).not.toHaveAttribute("aria-disabled"));

    api.confirmHistoryRecovery.mockRejectedValueOnce(new Error("500: {}"));
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't create a new recovery key. Try again.");
    expect(screen.getByRole("heading", { name: "Your new recovery key" })).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(await screen.findByRole("heading", { name: "Recovery" })).toBeInTheDocument();
    // Confirming names the shown key, so a stale Done can't promote another one.
    expect(api.confirmHistoryRecovery).toHaveBeenLastCalledWith("gen-new");
    expect(onChanged).toHaveBeenCalledOnce();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("reports a failed new key", async () => {
    api.regenerateHistoryRecoveryKey.mockRejectedValueOnce(new Error("500: {}"));
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Create new key" }));
    fireEvent.click(screen.getByRole("button", { name: "Create new key" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't create a new recovery key. Try again.");
  });

  it("adds a recovery password with the rule and backend errors", async () => {
    const { onChanged } = renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Add password" }));
    expect(screen.getByRole("heading", { name: "Add a recovery password" })).toBeInTheDocument();
    expect(screen.getByText("Another way to restore history. It doesn't lock anything.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Recovery password"), { target: { value: "short" } });
    fireEvent.change(screen.getByLabelText("Confirm recovery password"), { target: { value: "short" } });
    fireEvent.click(screen.getByRole("button", { name: "Save password" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Use at least 12 characters.");

    api.setHistoryRecoveryPassword.mockRejectedValueOnce(new Error('400: {"ok":false,"error":"password_too_common"}'));
    fireEvent.change(screen.getByLabelText("Recovery password"), { target: { value: "passwordpassword" } });
    fireEvent.change(screen.getByLabelText("Confirm recovery password"), { target: { value: "passwordpassword" } });
    fireEvent.click(screen.getByRole("button", { name: "Save password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That password is too common. Try a longer phrase.");

    fireEvent.change(screen.getByLabelText("Recovery password"), { target: { value: "a long quiet phrase" } });
    fireEvent.change(screen.getByLabelText("Confirm recovery password"), { target: { value: "a long quiet phrase" } });
    fireEvent.click(screen.getByRole("button", { name: "Save password" }));
    expect(await screen.findByText("Recovery password added.")).toBeInTheDocument();
    expect(api.setHistoryRecoveryPassword).toHaveBeenLastCalledWith("a long quiet phrase");
    expect(onChanged).toHaveBeenCalled();
  });

  it("changes and removes a set password", async () => {
    renderDialog(true);
    expect(screen.getByText("Set")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(screen.getByRole("heading", { name: "Change recovery password" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Recovery password"), { target: { value: "a long quiet phrase" } });
    fireEvent.change(screen.getByLabelText("Confirm recovery password"), { target: { value: "a long quiet phrase" } });
    fireEvent.click(screen.getByRole("button", { name: "Save password" }));
    expect(await screen.findByText("Recovery password changed.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Remove password" }));
    expect(screen.getByRole("heading", { name: "Remove your recovery password?" })).toBeInTheDocument();
    expect(screen.getByText("Your recovery key still works.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove password" }));
    expect(await screen.findByText("Recovery password removed.")).toBeInTheDocument();
    expect(api.setHistoryRecoveryPassword).toHaveBeenLastCalledWith(null);
  });

  it("reports a failed removal and keeps the password with Esc", async () => {
    api.setHistoryRecoveryPassword.mockRejectedValueOnce(new Error("500: {}"));
    const { onClose } = renderDialog(true);
    fireEvent.click(screen.getByRole("button", { name: "Remove password" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't remove your recovery password. Try again.");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.getByRole("heading", { name: "Recovery" })).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});
