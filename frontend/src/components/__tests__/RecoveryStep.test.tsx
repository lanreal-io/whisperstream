import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  RECOVERY_GATE_HINT_CONFIRM,
  RECOVERY_GATE_HINT_SAVE,
  RecoveryGateButton,
  RecoveryStep,
  checkOptionalRecoveryPassword,
  type RecoveryPasswordDraft,
} from "../RecoveryStep";
import type { PasswordRuleError } from "../../lib/passwordRule";

const files = vi.hoisted(() => ({
  downloadRecoveryFile: vi.fn(),
  printRecoveryFile: vi.fn(),
}));
vi.mock("../../lib/lockdownRecovery", () => files);

const KEY = "WSRK-1111-2222-3333-4444";
const HINT_ID = "recovery-gate-hint";

function Harness({
  optional = false,
  secret = KEY,
  serverError = null,
  onDone = () => {},
}: {
  optional?: boolean;
  secret?: string;
  serverError?: PasswordRuleError | null;
  onDone?: (draft: RecoveryPasswordDraft | null) => void;
}) {
  const [hint, setHint] = useState<string | null>(RECOVERY_GATE_HINT_SAVE);
  const [draft, setDraft] = useState<RecoveryPasswordDraft | null>(null);
  const [error, setError] = useState<PasswordRuleError | null>(serverError);
  return (
    <>
      <RecoveryStep
        secret={secret}
        fileVariant={optional ? "history" : "lockdown"}
        platform="mac"
        checkboxLabel="I saved my recovery key somewhere other than this Mac."
        onGateChange={(_open, next) => setHint(next)}
        optionalPassword={
          optional
            ? {
                value: draft,
                onChange: (next) => {
                  setDraft(next);
                  setError(null);
                },
                error,
              }
            : undefined
        }
      />
      <RecoveryGateButton
        hint={hint}
        hintId={HINT_ID}
        onClick={() => {
          const failure = checkOptionalRecoveryPassword(draft);
          if (failure) {
            setError(failure);
            return;
          }
          onDone(draft);
        }}
      >
        Done
      </RecoveryGateButton>
    </>
  );
}

beforeEach(() => {
  files.downloadRecoveryFile.mockResolvedValue("saved");
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const doneButton = () => screen.getByRole("button", { name: "Done" });

describe("RecoveryStep gate", () => {
  it("starts closed with a focusable button and the save hint", () => {
    const onDone = vi.fn();
    render(<Harness onDone={onDone} />);

    const button = doneButton();
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).not.toBeDisabled();
    expect(button).toHaveAttribute("aria-describedby", HINT_ID);
    expect(screen.getByRole("tooltip")).toHaveTextContent(RECOVERY_GATE_HINT_SAVE);
    expect(screen.getByRole("tooltip").id).toBe(HINT_ID);
    // No visible gate line inside the step itself.
    expect(screen.getAllByText(RECOVERY_GATE_HINT_SAVE)).toHaveLength(1);

    fireEvent.click(button);
    expect(onDone).not.toHaveBeenCalled();
  });

  it("needs a save action even when the checkbox is ticked", () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("tooltip")).toHaveTextContent(RECOVERY_GATE_HINT_SAVE);
    expect(doneButton()).toHaveAttribute("aria-disabled", "true");
  });

  it("switches to the confirm hint after Copy, then opens with the checkbox", async () => {
    const onDone = vi.fn();
    render(<Harness onDone={onDone} />);

    fireEvent.click(screen.getByRole("button", { name: "Copy recovery key" }));
    await waitFor(() =>
      expect(screen.getByRole("tooltip")).toHaveTextContent(RECOVERY_GATE_HINT_CONFIRM),
    );
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(KEY);
    expect(screen.getByRole("button", { name: "Copy recovery key" })).toHaveTextContent("Copied");
    expect(screen.getByRole("status")).toHaveTextContent("Recovery key copied.");

    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(doneButton()).not.toHaveAttribute("aria-disabled");
    expect(doneButton()).not.toHaveAttribute("aria-describedby");
    fireEvent.click(doneButton());
    expect(onDone).toHaveBeenCalledWith(null);
  });

  it("counts Print and passes the file variant", () => {
    render(<Harness optional />);
    fireEvent.click(screen.getByRole("button", { name: "Print recovery key" }));
    expect(files.printRecoveryFile).toHaveBeenCalledWith(KEY, {
      variant: "history",
      platform: "mac",
    });
    expect(screen.getByRole("button", { name: "Print recovery key" })).toHaveTextContent("Printed");
    expect(screen.getByRole("tooltip")).toHaveTextContent(RECOVERY_GATE_HINT_CONFIRM);
  });

  it("counts a saved Download but not a cancelled or failed one", async () => {
    files.downloadRecoveryFile.mockResolvedValueOnce("cancelled");
    render(<Harness />);
    const download = () => screen.getByRole("button", { name: "Download recovery key" });

    fireEvent.click(download());
    await waitFor(() => expect(files.downloadRecoveryFile).toHaveBeenCalledTimes(1));
    expect(download()).toHaveTextContent("Download");
    expect(screen.getByRole("tooltip")).toHaveTextContent(RECOVERY_GATE_HINT_SAVE);

    files.downloadRecoveryFile.mockRejectedValueOnce(new Error("write failed"));
    fireEvent.click(download());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't save the file. Try again.",
    );
    expect(screen.getByRole("tooltip")).toHaveTextContent(RECOVERY_GATE_HINT_SAVE);

    fireEvent.click(download());
    await waitFor(() => expect(download()).toHaveTextContent("Saved"));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(files.downloadRecoveryFile).toHaveBeenLastCalledWith(KEY, {
      variant: "lockdown",
      platform: "mac",
    });
  });

  it("resets the gate for a new key", async () => {
    const { rerender } = render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Copy recovery key" }));
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());

    rerender(<Harness secret="WSRK-5555-6666-7777-8888" />);
    expect(screen.getByRole("tooltip")).toHaveTextContent(RECOVERY_GATE_HINT_SAVE);
    expect(screen.getByRole("checkbox")).toHaveAttribute("aria-checked", "false");
  });

  it("labels the key block and reads it in groups", () => {
    render(<Harness />);
    const code = screen.getByLabelText("Recovery key", { selector: "code" });
    expect(code).toHaveTextContent(KEY);
    expect(code.querySelectorAll("span > span")).toHaveLength(5);
  });
});

describe("RecoveryStep optional password", () => {
  async function openGate() {
    fireEvent.click(screen.getByRole("button", { name: "Copy recovery key" }));
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
  }

  it("is absent without the optionalPassword prop", () => {
    render(<Harness />);
    expect(screen.queryByRole("button", { name: /Also add a recovery password/ })).toBeNull();
    expect(screen.queryByLabelText("Recovery password")).toBeNull();
  });

  it("is collapsed by default and expands to the password fields", () => {
    render(<Harness optional />);
    const toggle = screen.getByRole("button", { name: "Also add a recovery password Optional" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Recovery password")).toBeNull();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Another way to restore history. It doesn't lock anything.")).toBeInTheDocument();
    expect(screen.getByLabelText("Recovery password")).toBeInTheDocument();
    expect(screen.getByLabelText("Confirm recovery password")).toBeInTheDocument();
    expect(screen.getByText("At least 12 characters.")).toBeInTheDocument();
  });

  it("collapsing clears the draft so Done adds no password", async () => {
    const onDone = vi.fn();
    render(<Harness optional onDone={onDone} />);
    const toggle = screen.getByRole("button", { name: /Also add a recovery password/ });
    fireEvent.click(toggle);
    fireEvent.change(screen.getByLabelText("Recovery password"), { target: { value: "short" } });
    fireEvent.click(toggle);
    await openGate();
    fireEvent.click(doneButton());
    expect(onDone).toHaveBeenCalledWith(null);
  });

  it("an expanded but empty section is valid", async () => {
    const onDone = vi.fn();
    render(<Harness optional onDone={onDone} />);
    fireEvent.click(screen.getByRole("button", { name: /Also add a recovery password/ }));
    await openGate();
    fireEvent.click(doneButton());
    expect(onDone).toHaveBeenCalledWith({ password: "", confirm: "" });
  });

  it("shows too-short and mismatch errors on Done", async () => {
    const onDone = vi.fn();
    render(<Harness optional onDone={onDone} />);
    fireEvent.click(screen.getByRole("button", { name: /Also add a recovery password/ }));
    await openGate();

    fireEvent.change(screen.getByLabelText("Recovery password"), { target: { value: "short" } });
    fireEvent.change(screen.getByLabelText("Confirm recovery password"), { target: { value: "short" } });
    fireEvent.click(doneButton());
    expect(screen.getByRole("alert")).toHaveTextContent("Use at least 12 characters.");
    expect(screen.getByLabelText("Recovery password")).toHaveAttribute("aria-invalid", "true");

    fireEvent.change(screen.getByLabelText("Recovery password"), {
      target: { value: "a quiet morning frost" },
    });
    fireEvent.change(screen.getByLabelText("Confirm recovery password"), {
      target: { value: "a quiet morning fros" },
    });
    fireEvent.click(doneButton());
    expect(screen.getByRole("alert")).toHaveTextContent("Passwords don't match.");
    expect(screen.getByLabelText("Confirm recovery password")).toHaveAttribute("aria-invalid", "true");
    expect(onDone).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Confirm recovery password"), {
      target: { value: "a quiet morning frost" },
    });
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(doneButton());
    expect(onDone).toHaveBeenCalledWith({
      password: "a quiet morning frost",
      confirm: "a quiet morning frost",
    });
  });

  it("shows the backend too-common message", () => {
    render(
      <RecoveryStep
        secret={KEY}
        fileVariant="history"
        checkboxLabel="I saved my recovery key somewhere other than this PC."
        onGateChange={() => {}}
        optionalPassword={{
          value: { password: "passwordpassword", confirm: "passwordpassword" },
          onChange: () => {},
          error: "too-common",
        }}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "That password is too common. Try a longer phrase.",
    );
    expect(screen.getByLabelText("Recovery password")).toHaveAttribute("aria-invalid", "true");
  });
});

describe("checkOptionalRecoveryPassword", () => {
  it("validates only a non-empty expanded section", () => {
    expect(checkOptionalRecoveryPassword(null)).toBeNull();
    expect(checkOptionalRecoveryPassword({ password: "", confirm: "" })).toBeNull();
    expect(checkOptionalRecoveryPassword({ password: "", confirm: "x" })).toBe("too-short");
    expect(
      checkOptionalRecoveryPassword({ password: "twelve chars", confirm: "twelve chars" }),
    ).toBeNull();
  });
});
