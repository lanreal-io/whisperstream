import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LogsDownloadStep } from "./LogsDownloadStep";

vi.mock("../lib/api/diagnostics", () => ({
    exportLogs: vi.fn().mockResolvedValue("cancelled"),
  }));

afterEach(cleanup);

describe("LogsDownloadStep Lockdown-safe export copy", () => {
  it("states the content, upload, and selected-folder boundaries", () => {
    render(
      <LogsDownloadStep
        onDownloaded={vi.fn()}
        onSkip={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    expect(
      screen.getByText(/technical status only, never transcript or audio content/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/does not upload it/i)).toBeInTheDocument();
    expect(screen.getByText(/other apps may sync the folder you choose/i)).toBeInTheDocument();
  });
});
