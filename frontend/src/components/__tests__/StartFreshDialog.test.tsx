import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StartFreshDialog } from "../StartFreshDialog";

const api = vi.hoisted(() => ({ resetHistory: vi.fn() }));
vi.mock("../../lib/api/historyRecovery", async () => ({
  ...await vi.importActual<typeof import("../../lib/api/historyRecovery")>("../../lib/api/historyRecovery"),
  resetHistory: api.resetHistory,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderDialog(props: Partial<Parameters<typeof StartFreshDialog>[0]> = {}) {
  const onCancel = vi.fn();
  const onDeleted = vi.fn();
  render(
    <StartFreshDialog
      open
      recordCount={142}
      hasAudio
      hasRecoveryPassword
      onCancel={onCancel}
      onDeleted={onDeleted}
      {...props}
    />,
  );
  return { onCancel, onDeleted };
}

describe("StartFreshDialog", () => {
  it("explains the trade first with focus on the safe action", async () => {
    const { onCancel } = renderDialog();
    expect(screen.getByRole("heading", { name: "Start fresh without your history?" })).toBeInTheDocument();
    expect(screen.getByText(
      "Without the recovery key or recovery password, your saved transcripts can't be opened by anyone, including us.",
    )).toBeInTheDocument();
    expect(screen.getByText("All 142 saved transcripts and their audio are deleted.")).toBeInTheDocument();
    expect(screen.getByText("Saving starts again with a new recovery key.")).toBeInTheDocument();
    expect(screen.getByText("If you find your key later, it won't work on the new history.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Go back" })).toHaveFocus());
    fireEvent.click(screen.getByRole("button", { name: "Go back" }));
    expect(onCancel).toHaveBeenCalledOnce();
    expect(api.resetHistory).not.toHaveBeenCalled();
  });

  it.each([
    [1, true, "Your 1 saved transcript and its audio are deleted."],
    [1, false, "Your 1 saved transcript is deleted."],
    [7, false, "All 7 saved transcripts are deleted."],
  ] as const)("uses the count and audio variant (%s, audio %s)", (n, audio, text) => {
    renderDialog({ recordCount: n, hasAudio: audio, hasRecoveryPassword: false });
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.getByText(
      "Without the recovery key, your saved transcripts can't be opened by anyone, including us.",
    )).toBeInTheDocument();
  });

  it("arms the red button only after typing DELETE, then deletes", async () => {
    api.resetHistory.mockResolvedValue({ ok: true });
    const { onDeleted } = renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByRole("heading", { name: "Delete all saved transcripts?" })).toBeInTheDocument();
    expect(screen.getByText(
      "This permanently deletes all 142 saved transcripts without opening them. It can't be undone.",
    )).toBeInTheDocument();
    const field = screen.getByLabelText("Type DELETE to confirm");
    await waitFor(() => expect(field).toHaveFocus());
    const danger = screen.getByRole("button", { name: "Delete all saved transcripts" });
    expect(danger).toBeDisabled();
    fireEvent.change(field, { target: { value: "delete" } });
    expect(danger).toBeEnabled();
    fireEvent.click(danger);
    await waitFor(() => expect(onDeleted).toHaveBeenCalledOnce());
    expect(api.resetHistory).toHaveBeenCalledWith("DELETE");
  });

  it("uses the singular confirm body and reports a failure", async () => {
    api.resetHistory.mockRejectedValue(new Error("500: {}"));
    const { onDeleted } = renderDialog({ recordCount: 1 });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByText(
      "This permanently deletes your 1 saved transcript without opening it. It can't be undone.",
    )).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Type DELETE to confirm"), { target: { value: "DELETE" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete all saved transcripts" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't delete your history. Try again.");
    expect(onDeleted).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Keep my transcripts" }));
  });
});
