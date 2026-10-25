import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppConfig } from "../../lib/api/config";
import {
  LockdownScopeReviewDialog,
  needsLockdownScopeReview,
} from "../LockdownScopeReviewDialog";
import {
  LOCKDOWN_ACK_VERSION,
  LockdownSetupModal,
} from "../LockdownSetupModal";
import { LockdownManageDialog } from "../LockdownManageDialog";
import { LockdownDisableDialog } from "../LockdownDisableDialog";
import { ChangePasswordDialog, ViewRecoveryCodeDialog } from "../ChangePasswordDialog";

const apiMocks = vi.hoisted(() => ({
  acknowledgeLockdown: vi.fn(),
  enableLockdown: vi.fn(),
  disableLockdown: vi.fn(),
  resetLockdown: vi.fn(),
  viewRecoveryCode: vi.fn(),
  changeLockdownPassword: vi.fn(),
}));

vi.mock("../../lib/api/config", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api/config")>(
    "../../lib/api/config",
  );
  return {
    ...actual,
  };
});
vi.mock("../../lib/api/lockdown", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api/lockdown")>(
    "../../lib/api/lockdown",
  );
  return {
    ...actual,
    acknowledgeLockdown: apiMocks.acknowledgeLockdown,
    enableLockdown: apiMocks.enableLockdown,
    disableLockdown: apiMocks.disableLockdown,
    resetLockdown: apiMocks.resetLockdown,
    viewRecoveryCode: apiMocks.viewRecoveryCode,
    changeLockdownPassword: apiMocks.changeLockdownPassword,
  };
});

const recoveryFiles = vi.hoisted(() => ({
  downloadRecoveryFile: vi.fn(),
  printRecoveryFile: vi.fn(),
}));
vi.mock("../../lib/lockdownRecovery", () => recoveryFiles);

// Synthetic test value (12+ characters, the new-password minimum).
const NEW_PASSWORD = "synthetic-password";

const syncedConfig = { lockdown_mode: true } as AppConfig;

beforeEach(() => {
  apiMocks.acknowledgeLockdown.mockResolvedValue({
    ok: true,
    ack_version: "3",
  });
  apiMocks.enableLockdown.mockResolvedValue({
    recovery_code: "WS-RECOVERY-123",
    config: syncedConfig,
  });
  apiMocks.disableLockdown.mockResolvedValue({ ok: true, config: syncedConfig });
  apiMocks.resetLockdown.mockResolvedValue({ ok: true, config: syncedConfig });
  apiMocks.viewRecoveryCode.mockResolvedValue({
    ok: true,
    recovery_code: "WS-RECOVERY-456",
  });
  apiMocks.changeLockdownPassword.mockResolvedValue({ ok: true });
  recoveryFiles.downloadRecoveryFile.mockResolvedValue("saved");
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function setupProps(
  overrides: Partial<Parameters<typeof LockdownSetupModal>[0]> = {},
) {
  return {
    open: true,
    idleLockSeconds: 300,
    onClose: vi.fn(),
    onConfigSynced: vi.fn(),
    onLockRefresh: vi.fn(),
    ...overrides,
  };
}

describe("Lockdown version-3 acknowledgment", () => {
  it("renders the approved scope and benefits, persists version 3, and focuses the safe action", async () => {
    const props = setupProps();
    render(<LockdownSetupModal {...props} />);

    const dialog = screen.getByRole("dialog", {
      name: "Before you turn on Lockdown Mode",
    });
    expect(dialog).toHaveAccessibleDescription(
      "Lockdown applies to Whisperstream transcription, cleanup, and saved history.",
    );
    expect(
      screen.getByText("Transcription and cleanup run on this device."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Saved transcript text and audio are encrypted behind your password.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Safe diagnostic logs stay local unless you export and share them.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("History locks after inactivity.")).toBeInTheDocument();
    expect(
      screen.queryByText(/Cloud providers are turned off/),
    ).not.toBeInTheDocument();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus(),
    );

    const continueButton = screen.getByRole("button", { name: "Continue" });
    expect(continueButton).toBeDisabled();
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "I understand what Lockdown Mode covers and that it does not replace my compliance obligations.",
      }),
    );
    fireEvent.click(continueButton);

    await waitFor(() =>
      expect(apiMocks.acknowledgeLockdown).toHaveBeenCalledWith("3"),
    );
    expect(LOCKDOWN_ACK_VERSION).toBe("3");
    expect(
      screen.getByRole("heading", { name: "Keep a transcript history?" }),
    ).toBeInTheDocument();
  });

  it("keeps the already-enabled recovery step open after Escape and backdrop dismissal", async () => {
    const onClose = vi.fn();
    const onConfigSynced = vi.fn();
    const onLockRefresh = vi.fn();
    const { container } = render(
      <LockdownSetupModal
        {...setupProps({
          ackAccepted: true,
          onClose,
          onConfigSynced,
          onLockRefresh,
        })}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: /Keep encrypted history/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: NEW_PASSWORD },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: NEW_PASSWORD },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(
      await screen.findByRole("heading", { name: "Save your recovery key" }),
    ).toBeInTheDocument();
    expect(apiMocks.enableLockdown).toHaveBeenCalledOnce();
    expect(onConfigSynced).toHaveBeenCalledWith(syncedConfig);

    fireEvent.keyDown(window, { key: "Escape" });
    expect(
      await screen.findByText(
        "Lockdown Mode is already on. Save your recovery key before finishing.",
      ),
    ).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: "Cancel setup" }),
    ).not.toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Return to recovery key" }),
      ).toHaveFocus(),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Return to recovery key" }),
    );
    fireEvent.click(container.querySelector(".absolute.inset-0")!);
    expect(
      await screen.findByText(
        "Lockdown Mode is already on. Save your recovery key before finishing.",
      ),
    ).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(
      screen.getByRole("button", { name: "Return to recovery key" }),
    );
    const finish = screen.getByRole("button", { name: "Finish setup" });
    expect(finish).toHaveAttribute("aria-disabled", "true");
    expect(finish).toHaveAccessibleDescription(
      "Copy, print, or download your recovery key to continue.",
    );
    fireEvent.click(finish);
    expect(
      screen.getByRole("heading", { name: "Save your recovery key" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Copy recovery key" }));
    await waitFor(() =>
      expect(finish).toHaveAccessibleDescription(
        "Confirm where you saved it to continue.",
      ),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "I've saved my recovery key somewhere other than this computer.",
      }),
    );
    await waitFor(() => expect(finish).not.toHaveAttribute("aria-disabled"));
    expect(
      screen.getByText(
        "If you lose both your password and recovery key, your history can't be restored. Reset deletes it instead.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(finish);
    fireEvent.click(screen.getByRole("button", { name: "Done" }));

    expect(onClose).toHaveBeenCalledOnce();
    expect(onLockRefresh).toHaveBeenCalled();
  });
});

describe("existing Lockdown scope review", () => {
  it("targets enabled records with an older acknowledgment", () => {
    expect(needsLockdownScopeReview(true, "1")).toBe(true);
    expect(needsLockdownScopeReview(true, "2")).toBe(true);
    expect(needsLockdownScopeReview(true, "3")).toBe(false);
    expect(needsLockdownScopeReview(true, null)).toBe(false);
    expect(needsLockdownScopeReview(false, "1")).toBe(false);
  });

  it("shows only the compact scope, blocks ordinary dismissal, and traps focus", async () => {
    const onContinue = vi.fn().mockResolvedValue(undefined);
    const onTurnOff = vi.fn();
    render(
      <LockdownScopeReviewDialog
        open
        onContinue={onContinue}
        onTurnOff={onTurnOff}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "Review Lockdown Mode" });
    expect(dialog).toHaveAccessibleDescription(
      "Lockdown applies to Whisperstream transcription, cleanup, and saved history. Safe diagnostic logs stay local unless you export and share them.",
    );
    expect(screen.queryByText(/password setup/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Claude Code/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/provider/i)).not.toBeInTheDocument();

    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.click(screen.getByTestId("lockdown-scope-review-backdrop"));
    expect(onContinue).not.toHaveBeenCalled();
    expect(onTurnOff).not.toHaveBeenCalled();
    expect(dialog).toBeInTheDocument();

    const continueButton = screen.getByRole("button", { name: "Continue" });
    const turnOffButton = screen.getByRole("button", {
      name: "Turn off Lockdown Mode",
    });
    await waitFor(() => expect(continueButton).toHaveFocus());
    fireEvent.keyDown(window, { key: "Tab" });
    expect(turnOffButton).toHaveFocus();
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(continueButton).toHaveFocus();

    fireEvent.click(turnOffButton);
    expect(onTurnOff).toHaveBeenCalledOnce();
    fireEvent.click(continueButton);
    await waitFor(() => expect(onContinue).toHaveBeenCalledOnce());
  });
});

describe("standing recovery and reset copy", () => {
  it("keeps Manage concise and uses the restore-versus-delete warning", () => {
    render(
      <LockdownManageDialog
        open
        hasEnvelope
        idleLockSeconds={300}
        onIdleLockChange={vi.fn()}
        onChangePassword={vi.fn()}
        onViewRecovery={vi.fn()}
        onStartSaving={vi.fn()}
        onDisable={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(
      screen.getByText(
        "On-device transcription and cleanup. Saved history locked.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "If you lose both your password and recovery key, your history can't be restored. Reset deletes it instead.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "View recovery key" }),
    ).toBeInTheDocument();
  });

  it("uses the approved destructive-reset consequence without erasure claims", () => {
    render(
      <LockdownDisableDialog
        open
        hasEnvelope
        onCancel={vi.fn()}
        onDisabled={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "I forgot my password" }));

    expect(
      screen.getByText(
        "This deletes your locked history without opening it and turns off Lockdown Mode. It can't be undone.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/permanently/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/erased while/i)).not.toBeInTheDocument();
  });

  it("shows a regenerated key on the shared recovery step with the full gate", async () => {
    const onCancel = vi.fn();
    render(
      <ViewRecoveryCodeDialog
        open
        onCancel={onCancel}
        onRegenerated={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("heading", { name: "View recovery key" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Enter your password to generate a new recovery key. This invalidates your current key.",
      ),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Transcript password"), {
      target: { value: "password123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate a new key" }));

    expect(
      await screen.findByRole("heading", { name: "Your new recovery key" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Your previous recovery key no longer works."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Recovery key", { selector: "code" })).toHaveTextContent(
      "WS-RECOVERY-456",
    );
    // No optional recovery password in Lockdown.
    expect(
      screen.queryByRole("button", { name: /Also add a recovery password/ }),
    ).not.toBeInTheDocument();

    const done = screen.getByRole("button", { name: "Done" });
    // The gate effect can land a tick after the awaited key screen renders.
    await waitFor(() => expect(done).toHaveAttribute("aria-disabled", "true"));
    fireEvent.click(done);
    expect(onCancel).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Print recovery key" }));
    expect(recoveryFiles.printRecoveryFile).toHaveBeenCalledWith(
      "WS-RECOVERY-456",
      expect.objectContaining({ variant: "lockdown" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Download recovery key" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Download recovery key" }),
      ).toHaveTextContent("Saved"),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "I've saved my recovery key somewhere other than this computer.",
      }),
    );
    await waitFor(() => expect(done).not.toHaveAttribute("aria-disabled"));
    fireEvent.click(done);
    expect(onCancel).toHaveBeenCalledOnce();
  });
});

describe("View recovery key dismissal", () => {
  it("keeps the new key on screen until it is saved", async () => {
    const onCancel = vi.fn();
    const { container } = render(<ViewRecoveryCodeDialog open onCancel={onCancel} />);
    fireEvent.change(screen.getByLabelText("Transcript password"), { target: { value: "password123" } });
    fireEvent.click(screen.getByRole("button", { name: "Generate a new key" }));
    await screen.findByRole("heading", { name: "Your new recovery key" });
    // The key screen renders after an awaited request, so its gate effect can
    // land a tick later; dismiss only once the gate is in place.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Done" })).toHaveAttribute("aria-disabled", "true"),
    );

    fireEvent.keyDown(window, { key: "Escape" });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Copy, print, or download your recovery key to continue.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Return to recovery key" }));
    fireEvent.click(screen.getByRole("button", { name: "Copy recovery key" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Copy recovery key" })).toHaveTextContent("Copied"),
    );
    fireEvent.click(container.ownerDocument.querySelector(".absolute.inset-0")!);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Confirm where you saved it to continue.",
    );
    expect(onCancel).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("checkbox", {
      name: "I've saved my recovery key somewhere other than this computer.",
    }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("still cancels freely before a key is generated", () => {
    const onCancel = vi.fn();
    render(<ViewRecoveryCodeDialog open onCancel={onCancel} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledOnce();
  });
});

describe("Lockdown 12-character password rule", () => {
  async function reachPasswordStep() {
    render(<LockdownSetupModal {...setupProps({ ackAccepted: true })} />);
    fireEvent.click(
      screen.getByRole("button", { name: /Keep encrypted history/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByText("Use at least 12 characters.")).toBeInTheDocument();
  }

  it("keeps setup Continue disabled below 12 characters", async () => {
    await reachPasswordStep();
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "elevenchars" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "elevenchars" },
    });
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "twelve-chars" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "twelve-chars" },
    });
    expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled();
  });

  it("shows the backend too-common message on setup", async () => {
    apiMocks.enableLockdown.mockRejectedValueOnce(
      new Error('400: {"ok":false,"error_code":"password_too_common","message":"x"}'),
    );
    await reachPasswordStep();
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "passwordpassword" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "passwordpassword" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That password is too common. Try a longer phrase.",
    );
    expect(
      screen.queryByRole("heading", { name: "Save your recovery key" }),
    ).not.toBeInTheDocument();
  });

  it("applies the rule and backend message to Change password", async () => {
    render(
      <ChangePasswordDialog open onCancel={vi.fn()} onChanged={vi.fn()} />,
    );
    expect(screen.getByText("Use at least 12 characters.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Current password"), {
      target: { value: "old-pass" },
    });
    fireEvent.change(screen.getByLabelText("New password"), {
      target: { value: "elevenchars" },
    });
    fireEvent.change(screen.getByLabelText("Confirm new password"), {
      target: { value: "elevenchars" },
    });
    const submit = screen.getByRole("button", { name: "Change password" });
    expect(submit).toBeDisabled();

    apiMocks.changeLockdownPassword.mockRejectedValueOnce(
      new Error('400: {"ok":false,"error_code":"password_too_common","message":"x"}'),
    );
    fireEvent.change(screen.getByLabelText("New password"), {
      target: { value: "passwordpassword" },
    });
    fireEvent.change(screen.getByLabelText("Confirm new password"), {
      target: { value: "passwordpassword" },
    });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That password is too common. Try a longer phrase.",
    );
    expect(apiMocks.changeLockdownPassword).toHaveBeenCalledWith(
      "old-pass",
      "passwordpassword",
    );
  });
});

describe("Mac history consent in Lockdown setup", () => {
  it("requires the Keychain explanation before the password-backed enable request", async () => {
    render(<LockdownSetupModal {...setupProps({ ackAccepted: true, initialStep: "transcripts", requiresKeychainConsent: true })} />);
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByRole("heading", { name: "Save transcripts on this Mac?" })).toBeInTheDocument();
    expect(screen.getByText(/This permission covers only that key/)).toBeInTheDocument();
    expect(apiMocks.enableLockdown).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Continue to password" }));
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "synthetic-password" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "synthetic-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(apiMocks.enableLockdown).toHaveBeenCalledWith(expect.objectContaining({ keychainConsent: true, historyEnabled: true })));
  });

  it("does not request Keychain consent for the history-off branch", async () => {
    render(<LockdownSetupModal {...setupProps({ ackAccepted: true, requiresKeychainConsent: true })} />);
    fireEvent.click(screen.getByText("Don't save transcripts"));
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.queryByText(/Keychain/)).not.toBeInTheDocument();
    expect(apiMocks.enableLockdown).not.toHaveBeenCalled();
  });
});


describe("organization-disabled history setup", () => {
  it("enables voluntary Lockdown only without history or a password", async () => {
    render(<LockdownSetupModal {...setupProps({ historyDisabled: true, ackAccepted: true, initialStep: "transcripts" })} />);
    expect(await screen.findByRole("heading", { name: "History is disabled" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Keep encrypted history/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(apiMocks.enableLockdown).toHaveBeenCalledWith({ historyEnabled: false }));
  });
});
