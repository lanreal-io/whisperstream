/**
 * Vitest coverage for the bulk unused-model deletion confirm (Task 3.3).
 *
 * The copy is locked.
 * This is the only surface allowed to distinguish Parakeet's stored artifacts,
 * so the friendly `(English)` / `(Multilingual)` qualifiers appear here and
 * nowhere else. The safe action owns mount focus and Enter stays unbound.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup, fireEvent, within } from "@testing-library/react";
import { DeleteUnusedSttModelsDialog } from "../DeleteUnusedSttModelsDialog";

afterEach(() => {
  cleanup();
});

function renderDialog(
  overrides: { open?: boolean; modelNames?: string[]; busy?: boolean } = {},
) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <DeleteUnusedSttModelsDialog
      open={overrides.open ?? true}
      modelNames={
        overrides.modelNames ?? [
          "NVIDIA Parakeet (English)",
          "NVIDIA Parakeet (Multilingual)",
          "Qwen3 ASR",
        ]
      }
      totalSize="1.7 GB"
      busy={overrides.busy}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />,
  );
  return { onConfirm, onCancel };
}

describe("DeleteUnusedSttModelsDialog", () => {
  it("uses the locked bulk-deletion copy with the formatted total", () => {
    renderDialog();

    expect(screen.getByText("Delete unused models?")).toBeInTheDocument();
    expect(
      screen.getByText("This deletes these unused models and frees 1.7 GB:"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Delete unused models" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Keep models" }),
    ).toBeInTheDocument();
  });

  it("lists the exact artifacts as a semantic list", () => {
    renderDialog();

    const items = within(screen.getByRole("list")).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "NVIDIA Parakeet (English)",
      "NVIDIA Parakeet (Multilingual)",
      "Qwen3 ASR",
    ]);
  });

  it("carries the shared modal accessibility contract", () => {
    renderDialog();

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-labelledby");
    expect(dialog).toHaveAttribute("aria-describedby");
  });

  it("focuses the safe action on mount", () => {
    renderDialog();
    expect(screen.getByRole("button", { name: "Keep models" })).toHaveFocus();
  });

  it("confirms and cancels through their own actions", () => {
    const { onConfirm, onCancel } = renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Delete unused models" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Keep models" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("cancels on Escape", () => {
    const { onCancel } = renderDialog();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("locks both actions and Escape while the delete is in flight", () => {
    const { onCancel } = renderDialog({ busy: true });

    expect(
      screen.getByRole("button", { name: "Delete unused models" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Keep models" })).toBeDisabled();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("renders nothing when closed", () => {
    renderDialog({ open: false });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
