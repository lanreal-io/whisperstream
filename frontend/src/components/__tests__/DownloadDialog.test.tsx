/**
 * Vitest + Testing Library coverage for DownloadDialog.
 *
 * Task 2.4 requirements:
 *   - With kind="local_llm" subscribe filter, ignores Parakeet-tagged events
 *     (events without a kind tag) and vice versa.
 *   - startFn is called on mount (not the default Parakeet startDownload).
 *   - cancelFn is called when Cancel is clicked (not the default Parakeet cancelDownload).
 *   - download.complete event (kind=local_llm) calls onComplete.
 *   - download.error event shows the error phase with a Retry button.
 *   - Retry calls startFn again.
 *   - download.cancelled event calls onCancel.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, act, cleanup, waitFor } from "@testing-library/react";
import { DownloadDialog } from "../DownloadDialog";
import type { AppEvent, EventHandler } from "../../lib/socket/events";
import { subscribeToDownloadEvents } from "../../lib/socket/downloads";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Minimal subscribe factory that records the handler and allows manual emit. */
function makeRawSubscribe() {
  let registered: EventHandler | null = null;
  const unsubscribeMock = vi.fn();
  const rawSubscribe = vi.fn((handler: EventHandler) => {
    registered = handler;
    return unsubscribeMock;
  });
  const emit = (event: AppEvent) => {
    if (registered) registered(event);
  };
  return { rawSubscribe, emit, unsubscribeMock };
}

/** Returns a subscribe function filtered to kind="local_llm", matching what
 *  Enhancement.tsx passes to DownloadDialog for local GGUF downloads. */
function makeLocalLLMSubscribe(rawSubscribe: ReturnType<typeof makeRawSubscribe>["rawSubscribe"]) {
  return (handler: (event: AppEvent) => void) =>
    subscribeToDownloadEvents(
      rawSubscribe,
      handler as Parameters<typeof subscribeToDownloadEvents>[1],
      "local_llm",
    );
}

/** Minimal default props for DownloadDialog. */
function makeProps(overrides: Partial<Parameters<typeof DownloadDialog>[0]> = {}) {
  return {
    title: "Downloading your on-device AI",
    subtitle: "Runs on your machine after this. About 2962 MB.",
    subscribe: (_handler: (event: AppEvent) => void) => () => {},
    onComplete: vi.fn(),
    onCancel: vi.fn().mockResolvedValue(undefined),
    startFn: vi.fn().mockResolvedValue(undefined),
    cancelFn: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// startFn is called on mount
// ---------------------------------------------------------------------------

describe("DownloadDialog — startFn", () => {
  it("calls startFn on mount", () => {
    const startFn = vi.fn().mockResolvedValue(undefined);
    render(<DownloadDialog {...makeProps({ startFn })} />);
    expect(startFn).toHaveBeenCalledOnce();
  });

  it("does NOT call the default startDownload when startFn is provided", async () => {
    // The default startDownload imports from api.ts — if startFn is correctly
    // wired, it should be called instead. Since startFn is provided here and
    // called once, the absence of a fetch call confirms the override works.
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
      text: () => Promise.resolve("{}"),
    });
    globalThis.fetch = fetchSpy;
    const startFn = vi.fn().mockResolvedValue(undefined);
    render(<DownloadDialog {...makeProps({ startFn })} />);
    expect(startFn).toHaveBeenCalledOnce();
    // No fetch call should have been made for the download start endpoint.
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("transitions to error phase when startFn rejects with a non-409 error", async () => {
    const startFn = vi.fn().mockRejectedValue(new Error("500: server error"));
    render(<DownloadDialog {...makeProps({ startFn })} />);
    await waitFor(() => {
      expect(screen.getByText("Download failed")).toBeInTheDocument();
    });
  });

  it("stays in downloading phase when startFn rejects with 409 (already in progress)", async () => {
    const startFn = vi.fn().mockRejectedValue(new Error("409: busy"));
    render(<DownloadDialog {...makeProps({ startFn })} />);
    // 409 is silently ignored — the dialog joins the existing download via WS.
    // The title should still be visible (downloading phase, not error).
    await waitFor(() => {
      expect(screen.getByText("Downloading your on-device AI")).toBeInTheDocument();
    });
    expect(screen.queryByText("Download failed")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// WS event discriminator: kind="local_llm" vs Parakeet (no kind)
// ---------------------------------------------------------------------------

describe("DownloadDialog — kind discriminator via subscribe prop", () => {
  it("calls onComplete when download.complete fires with kind=local_llm", async () => {
    const { rawSubscribe, emit } = makeRawSubscribe();
    const onComplete = vi.fn();
    const subscribe = makeLocalLLMSubscribe(rawSubscribe);

    render(
      <DownloadDialog
        {...makeProps({
          subscribe,
          onComplete,
          startFn: vi.fn().mockResolvedValue(undefined),
        })}
      />,
    );

    await act(async () => {
      emit({ type: "download.complete", kind: "local_llm" });
    });

    await waitFor(() => {
      expect(onComplete).toHaveBeenCalledOnce();
    });
  });

  it("does NOT call onComplete when download.complete fires without kind (Parakeet event)", async () => {
    const { rawSubscribe, emit } = makeRawSubscribe();
    const onComplete = vi.fn();
    const subscribe = makeLocalLLMSubscribe(rawSubscribe);

    render(
      <DownloadDialog
        {...makeProps({
          subscribe,
          onComplete,
          startFn: vi.fn().mockResolvedValue(undefined),
        })}
      />,
    );

    await act(async () => {
      // Parakeet event: no kind tag
      emit({ type: "download.complete" });
    });

    // onComplete must NOT have been called — the Parakeet event was filtered out.
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("shows error phase when download.error fires with kind=local_llm", async () => {
    const { rawSubscribe, emit } = makeRawSubscribe();
    const subscribe = makeLocalLLMSubscribe(rawSubscribe);

    render(
      <DownloadDialog
        {...makeProps({
          subscribe,
          startFn: vi.fn().mockResolvedValue(undefined),
        })}
      />,
    );

    await act(async () => {
      emit({ type: "download.error", message: "Disk full", kind: "local_llm" });
    });

    await waitFor(() => {
      expect(screen.getByText("Download failed")).toBeInTheDocument();
      expect(screen.getByText("Disk full")).toBeInTheDocument();
    });
  });

  it("does NOT enter error phase when download.error fires without kind (Parakeet event)", async () => {
    const { rawSubscribe, emit } = makeRawSubscribe();
    const subscribe = makeLocalLLMSubscribe(rawSubscribe);

    render(
      <DownloadDialog
        {...makeProps({
          subscribe,
          startFn: vi.fn().mockResolvedValue(undefined),
        })}
      />,
    );

    await act(async () => {
      emit({ type: "download.error", message: "Parakeet error" });
    });

    // Error phase must NOT have been triggered.
    expect(screen.queryByText("Download failed")).toBeNull();
  });

  it("Parakeet subscribe (undefined kind) ignores local_llm events", async () => {
    // Simulate the Parakeet/General-tab path: subscribe with undefined kind.
    const { rawSubscribe, emit } = makeRawSubscribe();
    const onComplete = vi.fn();

    // Parakeet-style subscribe: kind=undefined (only forwards events with no kind tag)
    const parakeetSubscribe = (handler: (event: AppEvent) => void) =>
      subscribeToDownloadEvents(
        rawSubscribe,
        handler as Parameters<typeof subscribeToDownloadEvents>[1],
        undefined,
      );

    render(
      <DownloadDialog
        {...makeProps({
          subscribe: parakeetSubscribe,
          onComplete,
          startFn: vi.fn().mockResolvedValue(undefined),
        })}
      />,
    );

    await act(async () => {
      // Local LLM event — must be ignored by the Parakeet subscribe.
      emit({ type: "download.complete", kind: "local_llm" });
    });

    expect(onComplete).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// cancelFn is called on Cancel click
// ---------------------------------------------------------------------------

describe("DownloadDialog — cancelFn", () => {
  it("calls cancelFn when Cancel is clicked in downloading phase", async () => {
    const cancelFn = vi.fn().mockResolvedValue(undefined);
    render(<DownloadDialog {...makeProps({ cancelFn })} />);

    const cancelBtn = screen.getByRole("button", { name: "Cancel" });
    await act(async () => {
      fireEvent.click(cancelBtn);
    });

    expect(cancelFn).toHaveBeenCalledOnce();
  });

  it("does NOT call the default cancelDownload when cancelFn is provided", async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
      text: () => Promise.resolve("{}"),
    });
    globalThis.fetch = fetchSpy;
    const cancelFn = vi.fn().mockResolvedValue(undefined);
    render(<DownloadDialog {...makeProps({ cancelFn })} />);

    // Reset fetch spy after startFn is called (which may call fetch in default mode)
    fetchSpy.mockClear();

    const cancelBtn = screen.getByRole("button", { name: "Cancel" });
    await act(async () => {
      fireEvent.click(cancelBtn);
    });

    expect(cancelFn).toHaveBeenCalledOnce();
    // No fetch call to the cancel endpoint.
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("calls onCancel when download.cancelled WS event fires after cancel request", async () => {
    const { rawSubscribe, emit } = makeRawSubscribe();
    const onCancel = vi.fn().mockResolvedValue(undefined);
    const cancelFn = vi.fn().mockResolvedValue(undefined);
    const subscribe = makeLocalLLMSubscribe(rawSubscribe);

    render(
      <DownloadDialog
        {...makeProps({
          subscribe,
          onCancel,
          cancelFn,
          startFn: vi.fn().mockResolvedValue(undefined),
        })}
      />,
    );

    // Click Cancel
    const cancelBtn = screen.getByRole("button", { name: "Cancel" });
    await act(async () => {
      fireEvent.click(cancelBtn);
    });

    // Simulate WS cancelled event arriving
    await act(async () => {
      emit({ type: "download.cancelled", kind: "local_llm" });
    });

    await waitFor(() => {
      expect(onCancel).toHaveBeenCalledOnce();
    });
  });
});

// ---------------------------------------------------------------------------
// Retry calls startFn again
// ---------------------------------------------------------------------------

describe("DownloadDialog — error phase retry", () => {
  it("calls startFn again when Retry is clicked after an error", async () => {
    const { rawSubscribe, emit } = makeRawSubscribe();
    const startFn = vi.fn().mockResolvedValue(undefined);
    const subscribe = makeLocalLLMSubscribe(rawSubscribe);

    render(
      <DownloadDialog
        {...makeProps({
          subscribe,
          startFn,
        })}
      />,
    );

    // Trigger error phase via WS event
    await act(async () => {
      emit({ type: "download.error", message: "Network error", kind: "local_llm" });
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    });

    // startFn was called once on mount
    expect(startFn).toHaveBeenCalledTimes(1);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    });

    // startFn should be called again on retry
    expect(startFn).toHaveBeenCalledTimes(2);
  });
});
