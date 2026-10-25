import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EnhancementRecoveryHost } from "./EnhancementRecoveryHost";
import { cancelLocalLLMDownload, getLocalRecovery, startLocalLLMDownload } from "../lib/api/enhancement";
import type { AppEvent } from "../lib/socket/events";

vi.mock("../lib/api/enhancement", async (original) => ({
    ...await original<typeof import("../lib/api/enhancement")>(),
    getLocalRecovery: vi.fn(),
    startLocalLLMDownload: vi.fn(),
    cancelLocalLLMDownload: vi.fn().mockResolvedValue({}),
  }));
vi.mock("../lib/logCapture", () => ({ flushLogSync: vi.fn().mockResolvedValue(undefined), errorType: () => "Error" }));

const model = { id: "gemma4-e2b-instruct", label: "Gemma 4 E2B", disk_mb: 1600 };
const handlers = new Set<(event: AppEvent) => void>();
const subscribe = (handler: (event: AppEvent) => void) => { handlers.add(handler); return () => { handlers.delete(handler); }; };
const emit = (event: AppEvent) => act(() => { [...handlers].forEach(h => h(event)); });
const props = { enabled: true, connected: true, blocked: false, subscribe };
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks(); handlers.clear();
  vi.mocked(getLocalRecovery).mockResolvedValue({ model, busy: false });
  vi.mocked(startLocalLLMDownload).mockResolvedValue(undefined);
});
describe("missing local enhancement recovery", () => {
  it("waits for hydrated setup and reconnects without depending on a one-shot event", async () => {
    const view = render(<EnhancementRecoveryHost {...props} enabled={false} />);
    expect(getLocalRecovery).not.toHaveBeenCalled();
    view.rerender(<EnhancementRecoveryHost {...props} />);
    await screen.findByRole("dialog");
    expect(startLocalLLMDownload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Not now"));
    emit({ type: "enhancement.error", provider: "local", recording_id: 1 } as AppEvent);
    await waitFor(() => expect(getLocalRecovery).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("ignores stale probes when setup or provider changes", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof getLocalRecovery>>) => void;
    vi.mocked(getLocalRecovery).mockReturnValue(new Promise(r => { resolve = r; }));
    const view = render(<EnhancementRecoveryHost {...props} />);
    view.rerender(<EnhancementRecoveryHost {...props} enabled={false} />);
    await act(async () => { resolve({ model, busy: false }); });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it.each([{ model: null, busy: false }, { model, busy: true }])("does not prompt for healthy or busy status", async (status) => {
    vi.mocked(getLocalRecovery).mockResolvedValue(status);
    render(<EnhancementRecoveryHost {...props} />);
    await waitFor(() => expect(getLocalRecovery).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("downloads only after confirmation and ignores other model completions", async () => {
    render(<EnhancementRecoveryHost {...props} />);
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByText("Download again"));
    await waitFor(() => expect(startLocalLLMDownload).toHaveBeenCalledWith(model.id, true));
    emit({ type: "download.complete", kind: "local_llm", model_id: "stream-1" });
    expect(screen.getByText("Restoring local enhancement")).toBeInTheDocument();
    vi.mocked(getLocalRecovery).mockResolvedValue({ model: null, busy: false });
    emit({ type: "download.complete", kind: "local_llm", model_id: model.id });
    await waitFor(() => expect(screen.queryByText("Restoring local enhancement")).not.toBeInTheDocument());
  });
  it("keeps keyboard focus inside confirmation and dismisses with Escape", async () => {
    render(<EnhancementRecoveryHost {...props} />);
    const dialog = await screen.findByRole("dialog");
    const later = screen.getByText("Not now"), download = screen.getByText("Download again");
    await waitFor(() => expect(later).toHaveFocus());
    fireEvent.keyDown(later, { key: "Tab", shiftKey: true });
    expect(download).toHaveFocus();
    fireEvent.keyDown(download, { key: "Tab" });
    expect(later).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("starts only once in StrictMode and retries a failed transfer", async () => {
    render(<StrictMode><EnhancementRecoveryHost {...props} /></StrictMode>);
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByText("Download again"));
    await waitFor(() => expect(startLocalLLMDownload).toHaveBeenCalledTimes(1));
    emit({ type: "download.error", kind: "local_llm", model_id: model.id, message: "Download interrupted." });
    fireEvent.click(await screen.findByText("Retry"));
    await waitFor(() => expect(startLocalLLMDownload).toHaveBeenCalledTimes(2));
  });
  it("reconciles a completion missed while disconnected", async () => {
    const view = render(<EnhancementRecoveryHost {...props} />);
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByText("Download again"));
    await waitFor(() => expect(startLocalLLMDownload).toHaveBeenCalledTimes(1));
    view.rerender(<EnhancementRecoveryHost {...props} connected={false} />);
    vi.mocked(getLocalRecovery).mockResolvedValue({ model: null, busy: false });
    view.rerender(<EnhancementRecoveryHost {...props} />);
    await waitFor(() => expect(screen.queryByText("Restoring local enhancement")).not.toBeInTheDocument());
  });
  it("cancels only after its start is acknowledged and closes without a socket event", async () => {
    let started!: () => void;
    vi.mocked(startLocalLLMDownload).mockReturnValue(new Promise<void>(resolve => { started = resolve; }));
    render(<EnhancementRecoveryHost {...props} />);
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByText("Download again"));
    fireEvent.click(await screen.findByText("Cancel"));
    expect(cancelLocalLLMDownload).not.toHaveBeenCalled();
    await act(async () => { started(); });
    await waitFor(() => expect(screen.queryByText("Restoring local enhancement")).not.toBeInTheDocument());
    expect(cancelLocalLLMDownload).toHaveBeenCalledTimes(1);
  });
});

it("keeps restored-model speed review distinct from a failed download", async () => {
  render(<EnhancementRecoveryHost {...props} />);
  await screen.findByRole("dialog");
  fireEvent.click(screen.getByText("Download again"));
  await waitFor(() => expect(startLocalLLMDownload).toHaveBeenCalledTimes(1));
  const message = "Model downloaded. Open Enhancement to review its speed check before enabling local cleanup.";
  emit({ type: "download.error", kind: "local_llm", model_id: model.id,
    reason: "model_benchmark_required", restored: true, message });
  expect(await screen.findByText("Review the speed check")).toBeInTheDocument();
  expect(screen.getByText(message)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  await waitFor(() => expect(screen.queryByText("Review the speed check")).toBeNull());
  expect(startLocalLLMDownload).toHaveBeenCalledTimes(1);
});
