/**
 * Vitest coverage for the targeted speech-model deletion confirm (Task 3.2).
 *
 * The copy is locked
 * and the safe action owns mount focus, so a stray Enter can never delete.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { DeleteSttModelDialog } from "../DeleteSttModelDialog";

afterEach(() => {
  cleanup();
});

function renderDialog(overrides: { open?: boolean } = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <DeleteSttModelDialog
      open={overrides.open ?? true}
      modelName="Qwen3 ASR"
      onConfirm={onConfirm}
      onCancel={onCancel}
    />,
  );
  return { onConfirm, onCancel };
}

describe("DeleteSttModelDialog", () => {
  it("uses the locked deletion copy", () => {
    renderDialog();

    expect(screen.getByText("Delete Qwen3 ASR?")).toBeInTheDocument();
    expect(
      screen.getByText(
        "This deletes the downloaded model from this device. You can download it again anytime.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Delete model" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Keep model" })).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-modal", "true");
  });

  it("focuses the safe action on mount", () => {
    renderDialog();
    expect(screen.getByRole("button", { name: "Keep model" })).toHaveFocus();
  });

  it("confirms and cancels through their own actions", () => {
    const { onConfirm, onCancel } = renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Delete model" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Keep model" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("cancels on Escape and renders nothing when closed", () => {
    const { onCancel } = renderDialog();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);

    cleanup();
    renderDialog({ open: false });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
