import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReportBugDialog } from "./ReportBugDialog";

const mocks = vi.hoisted(() => ({ exportLogs: vi.fn() }));

vi.mock("../lib/api/diagnostics", () => ({
  exportLogs: mocks.exportLogs,
}));

function renderDialog() {
  const handlers = {
    onOpenIssues: vi.fn(),
    onEmail: vi.fn(),
    onClose: vi.fn(),
  };
  render(<ReportBugDialog open {...handlers} />);
  return handlers;
}

describe("ReportBugDialog", () => {
  beforeEach(() => mocks.exportLogs.mockReset());

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("asks for logs first and focuses the export action", () => {
    renderDialog();

    expect(screen.getByRole("dialog", { name: "Include your logs" })).toBeInTheDocument();
    expect(screen.getByText(/only way I can see what went wrong/)).toBeInTheDocument();
    expect(screen.getByText("Whisperstream never uploads your logs.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export logs" })).toHaveFocus();
    expect(mocks.exportLogs).not.toHaveBeenCalled();
  });

  it("continues without logs straight to GitHub Issues", () => {
    const { onOpenIssues } = renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Continue without logs" }));

    expect(onOpenIssues).toHaveBeenCalledTimes(1);
    expect(mocks.exportLogs).not.toHaveBeenCalled();
  });

  it("offers GitHub Issues and email once logs are saved", async () => {
    mocks.exportLogs.mockResolvedValue("saved");
    const { onOpenIssues, onEmail } = renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Export logs" }));

    const github = await screen.findByRole("button", { name: /Continue to GitHub Issues/ });
    expect(screen.getByRole("dialog", { name: "Logs saved" })).toBeInTheDocument();
    expect(screen.getByText(/GitHub attachments are public/)).toBeInTheDocument();
    await waitFor(() => expect(github).toHaveFocus());

    fireEvent.click(github);
    expect(onOpenIssues).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Email logs instead" }));
    expect(onEmail).toHaveBeenCalledTimes(1);
  });

  it("disables the export action and ignores Escape while preparing", async () => {
    let resolve: (value: string) => void = () => {};
    mocks.exportLogs.mockReturnValue(new Promise((r) => { resolve = r; }));
    const { onClose } = renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Export logs" }));

    const preparing = await screen.findByRole("button", { name: "Preparing…" });
    expect(preparing).toBeDisabled();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();

    resolve("saved");
    await screen.findByRole("dialog", { name: "Logs saved" });
  });

  it("returns to the request quietly when the save is cancelled", async () => {
    mocks.exportLogs.mockResolvedValue("cancelled");
    renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Export logs" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Export logs" })).toBeEnabled());
    expect(screen.getByRole("dialog", { name: "Include your logs" })).toBeInTheDocument();
  });

  it("lets the user retry or continue when export fails", async () => {
    mocks.exportLogs.mockRejectedValueOnce(new Error("500")).mockResolvedValueOnce("saved");
    const { onOpenIssues } = renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Export logs" }));

    await screen.findByRole("dialog", { name: "Couldn't export logs" });
    fireEvent.click(screen.getByRole("button", { name: "Continue without logs" }));
    expect(onOpenIssues).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByRole("dialog", { name: "Logs saved" });
    expect(mocks.exportLogs).toHaveBeenCalledTimes(2);
  });

  it("explains Lockdown when export is denied and offers only Close", async () => {
    mocks.exportLogs.mockResolvedValue("denied");
    const { onClose } = renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Export logs" }));

    await screen.findByText("Log export is unavailable in Lockdown Mode.");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on Escape and backdrop click", () => {
    const { onClose } = renderDialog();

    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.click(screen.getByTestId("report-bug-backdrop"));

    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("renders nothing when closed", () => {
    render(<ReportBugDialog open={false} onOpenIssues={vi.fn()} onEmail={vi.fn()} onClose={vi.fn()} />);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
