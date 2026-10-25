import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LogsDownloadStep } from "./LogsDownloadStep";

const mocks = vi.hoisted(() => ({ exportLogs: vi.fn() }));

vi.mock("../lib/api/diagnostics", () => ({
    exportLogs: mocks.exportLogs,
  }));

describe("LogsDownloadStep export outcomes", () => {
  beforeEach(() => mocks.exportLogs.mockReset());

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it.each([
    ["denied", "Unavailable in Lockdown"],
    ["cancelled", "Export logs"],
  ] as const)("does not advance or attach logs when export is %s", async (result, label) => {
    const onDownloaded = vi.fn();
    mocks.exportLogs.mockResolvedValue(result);
    render(
      <LogsDownloadStep
        onDownloaded={onDownloaded}
        onSkip={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Export logs" }));

    await waitFor(() => expect(screen.getByRole("button", { name: label })).toBeInTheDocument());
    expect(onDownloaded).not.toHaveBeenCalled();
  });
});
