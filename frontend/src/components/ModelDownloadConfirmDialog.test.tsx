import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps, ReactNode } from "react";
import type { AppEvent } from "../lib/socket/events";
import { ModelDownloadConfirmDialog } from "./ModelDownloadConfirmDialog";
import type {
  DownloadDialogFailure,
  DownloadDialogFailureCopy,
} from "./ModelDownloadConfirmDialog";
import { sttDownloadFailureCopy } from "../lib/sttSwitch";

vi.mock("framer-motion", async () => {
  const React = await import("react");
  const motionProps = new Set([
    "animate",
    "exit",
    "initial",
    "transition",
  ]);
  const motionComponents = new Map<
    string,
    ({ children, ...props }: Record<string, unknown>) => ReactNode
  >();
  const motion = new Proxy(
    {},
    {
      get: (_target, tag: string) => {
        let Component = motionComponents.get(tag);
        if (!Component) {
          Component = ({ children, ...props }: Record<string, unknown>) => {
            for (const key of motionProps) delete props[key];
            return React.createElement(tag, props, children as ReactNode);
          };
          motionComponents.set(tag, Component);
        }
        return Component;
      },
    },
  );
  return {
    motion,
    AnimatePresence: ({ children }: { children: ReactNode }) => <>{children}</>,
  };
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  vi.clearAllMocks();
});

function renderVoiceDialog(
  overrides: Partial<ComponentProps<typeof ModelDownloadConfirmDialog>> = {},
) {
  const handlers: Array<(event: AppEvent) => void> = [];
  const props: ComponentProps<typeof ModelDownloadConfirmDialog> = {
    targetModel: {
      name: "Kokoro",
      downloadBytes: 318_413_913,
      installedBytes: 345_395_578,
    },
    eventKind: "tts",
    targetEngine: "kokoro",
    subscribe: vi.fn((handler: (event: AppEvent) => void) => {
      handlers.push(handler);
      return () => {};
    }),
    onConfirm: vi.fn().mockResolvedValue(undefined),
    onComplete: vi.fn(),
    onCancel: vi.fn(),
    onCancelDownload: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  const view = render(<ModelDownloadConfirmDialog {...props} />);
  return { props, handlers, view };
}

function emitToAll(
  handlers: Array<(event: AppEvent) => void>,
  event: AppEvent,
) {
  act(() => {
    for (const handler of handlers) {
      handler(event);
    }
  });
}

describe("ModelDownloadConfirmDialog voice-model lifecycle", () => {
  it("uses registry-backed copy and stacks as an accessible modal", () => {
    renderVoiceDialog();

    const dialog = screen.getByRole("dialog", {
      name: "Download this model?",
    });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog.parentElement).toHaveClass("z-[60]");
    expect(
      screen.getByText(
        "Kokoro is about 345 MB. It downloads once, then runs on your CPU. Continue?",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/318 MB/)).not.toBeInTheDocument();
  });

  it("does not treat an Enter already handled by the model picker as confirmation", () => {
    const { props } = renderVoiceDialog();
    const selectionKey = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    selectionKey.preventDefault();
    act(() => { window.dispatchEvent(selectionKey); });
    expect(props.onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "Download this model?" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Enter" });
    expect(props.onConfirm).toHaveBeenCalledTimes(1);
  });

  it("shows a synchronous start error and retries through the same start path", async () => {
    const onConfirm = vi
      .fn()
      .mockRejectedValueOnce(new Error("500: unavailable"))
      .mockResolvedValueOnce(undefined);
    renderVoiceDialog({ onConfirm });

    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(
      await screen.findByRole("heading", { name: "Download failed" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(onConfirm).toHaveBeenCalledTimes(2);
    expect(
      await screen.findByRole("heading", { name: "Downloading Kokoro" }),
    ).toBeInTheDocument();
  });

  it("shows preparation feedback at 100% without completing early", async () => {
    const { props, handlers } = renderVoiceDialog();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByRole("heading", { name: "Downloading Kokoro" });

    emitToAll(handlers, {
      type: "download.progress",
      downloaded: 318_413_913,
      total: 318_413_913,
      speed_bps: 10_000_000,
      kind: "tts",
      engine: "kokoro",
    });

    expect(
      screen.getByRole("heading", { name: "Preparing Kokoro" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Finishing setup. This may take a moment."),
    ).toBeInTheDocument();
    expect(screen.getByText("100%")).toBeInTheDocument();
    expect(screen.queryByText("Estimating…")).not.toBeInTheDocument();
    expect(props.onComplete).not.toHaveBeenCalled();
  });

  it("ignores another engine reaching 100%", async () => {
    const { handlers } = renderVoiceDialog();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByRole("heading", { name: "Downloading Kokoro" });

    emitToAll(handlers, {
      type: "download.progress",
      downloaded: 145_000_000,
      total: 145_000_000,
      speed_bps: 10_000_000,
      kind: "tts",
      engine: "supertonic",
    });

    expect(
      screen.getByRole("heading", { name: "Downloading Kokoro" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Preparing Kokoro" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
  });

  it("disables cancellation while setup is finishing", async () => {
    const { props, handlers } = renderVoiceDialog();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByRole("heading", { name: "Downloading Kokoro" });

    emitToAll(handlers, {
      type: "download.progress",
      downloaded: 318_413_913,
      total: 318_413_913,
      speed_bps: 10_000_000,
      kind: "tts",
      engine: "kokoro",
    });

    const finishingButton = screen.getByRole("button", {
      name: "Finishing up...",
    });
    expect(finishingButton).toBeDisabled();
    fireEvent.click(finishingButton);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(props.onCancelDownload).not.toHaveBeenCalled();
  });

  it("completes only after the matching completion settle timer", async () => {
    vi.useFakeTimers();
    const { props, handlers } = renderVoiceDialog();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    await act(async () => {});

    emitToAll(handlers, {
      type: "download.progress",
      downloaded: 318_413_913,
      total: 318_413_913,
      speed_bps: 10_000_000,
      kind: "tts",
      engine: "kokoro",
    });
    emitToAll(handlers, {
      type: "download.complete",
      kind: "tts",
      engine: "kokoro",
    });

    act(() => vi.advanceTimersByTime(299));
    expect(props.onComplete).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(props.onComplete).toHaveBeenCalledTimes(1);
  });

  it("shows a download error during finalization and retry resets preparation", async () => {
    const { handlers } = renderVoiceDialog();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByRole("heading", { name: "Downloading Kokoro" });

    emitToAll(handlers, {
      type: "download.progress",
      downloaded: 318_413_913,
      total: 318_413_913,
      speed_bps: 10_000_000,
      kind: "tts",
      engine: "kokoro",
    });
    emitToAll(handlers, {
      type: "download.error",
      message: "Archive verification failed",
      kind: "tts",
      engine: "kokoro",
    });

    expect(
      await screen.findByRole("heading", { name: "Download failed" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(
      await screen.findByRole("heading", { name: "Downloading Kokoro" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
  });

  it("treats an in-flight target cancellation as cancellation and ignores other models", async () => {
    const { props, handlers } = renderVoiceDialog();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByRole("heading", { name: "Downloading Kokoro" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(props.onCancelDownload).toHaveBeenCalledTimes(1);
    emitToAll(handlers, {
      type: "download.cancelled",
      kind: "tts",
      engine: "supertonic",
    });
    expect(props.onCancel).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("heading", { name: "Download failed" }),
    ).not.toBeInTheDocument();

    emitToAll(handlers, {
      type: "download.cancelled",
      kind: "tts",
      engine: "kokoro",
    });
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it("does not apply a delayed completion after the dialog is cancelled and unmounted", () => {
    vi.useFakeTimers();
    const { props, handlers, view } = renderVoiceDialog();

    emitToAll(handlers, {
      type: "download.complete",
      kind: "tts",
      engine: "kokoro",
    });
    view.unmount();
    act(() => vi.advanceTimersByTime(300));

    expect(props.onComplete).not.toHaveBeenCalled();
  });
});

// The STT flow supplies `mapFailure` because the backend's raw strings are HTTP
// status lines and JSON payloads that name no recovery step. Everything else
// keeps rendering the backend message, which is the pre-existing behavior.
describe("ModelDownloadConfirmDialog failure copy", () => {
  const sttFailureCopy = (
    failure: DownloadDialogFailure,
  ): DownloadDialogFailureCopy =>
    sttDownloadFailureCopy("Qwen3 ASR", failure);

  function renderSttDialog(
    overrides: Partial<ComponentProps<typeof ModelDownloadConfirmDialog>> = {},
  ) {
    return renderVoiceDialog({
      targetModel: undefined,
      targetBackend: "qwen",
      eventKind: undefined,
      targetEngine: undefined,
      mapFailure: sttFailureCopy,
      ...overrides,
    });
  }

  it("accepts only untagged STT lifecycle events", async () => {
    const { props, handlers } = renderSttDialog();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByRole("heading", { name: "Downloading Qwen3 ASR" });

    // Every other model family is tagged. Undefined `eventKind` means STT's
    // untagged stream only, not a wildcard across all model operations.
    emitToAll(handlers, {
      type: "download.progress",
      downloaded: 10,
      total: 10,
      speed_bps: 10,
      kind: "local_llm",
    });
    emitToAll(handlers, {
      type: "download.error",
      message: "TTS failed",
      kind: "tts",
      engine: "kokoro",
    });
    emitToAll(handlers, {
      type: "download.cancelled",
      kind: "relocate",
    });
    emitToAll(handlers, {
      type: "download.complete",
      kind: "local_llm",
    });

    expect(
      screen.getByRole("heading", { name: "Downloading Qwen3 ASR" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("TTS failed")).toBeNull();
    expect(props.onCancel).not.toHaveBeenCalled();
    expect(props.onComplete).not.toHaveBeenCalled();

    emitToAll(handlers, {
      type: "download.progress",
      downloaded: 10,
      total: 10,
      speed_bps: 10,
    });
    expect(
      screen.getByRole("heading", { name: "Preparing Qwen3 ASR" }),
    ).toBeInTheDocument();
  });

  it("replaces a refused start's payload with the locked recovery step", async () => {
    renderSttDialog({
      onConfirm: vi
        .fn()
        .mockRejectedValue(
          new Error('409: {"status":"busy","reason":"dictation_active"}'),
        ),
    });

    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    expect(
      await screen.findByRole("heading", { name: "Could not switch models" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Finish your current dictation, then switch models."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/409/)).not.toBeInTheDocument();
    expect(screen.queryByText(/dictation_active/)).not.toBeInTheDocument();
  });

  it("names another model operation rather than a connection problem", async () => {
    const { handlers } = renderSttDialog();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    emitToAll(handlers, {
      type: "download.error",
      message: "Could not switch models. Try again.",
      reason: "operation_busy",
      restored: false,
    });

    expect(
      await screen.findByText(
        "Whisperstream is already updating a model. Try again when it finishes.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps the download heading and connection advice for a transfer failure", async () => {
    const { handlers } = renderSttDialog();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    emitToAll(handlers, {
      type: "download.error",
      message: "Could not switch models. Whisperstream restored your previous settings.",
      reason: "download_failed",
      restored: true,
    });

    expect(
      await screen.findByRole("heading", { name: "Download failed" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Qwen3 ASR did not finish downloading. Check your connection and try again.",
      ),
    ).toBeInTheDocument();
  });

  it("confirms the rollback for an activation failure without calling it a download failure", async () => {
    const { handlers } = renderSttDialog();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    emitToAll(handlers, {
      type: "download.error",
      message: "Could not switch models. Whisperstream restored your previous settings.",
      reason: "activation_failed",
      restored: true,
    });

    expect(
      await screen.findByRole("heading", { name: "Could not switch models" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Could not switch to Qwen3 ASR. Whisperstream restored your previous settings.",
      ),
    ).toBeInTheDocument();
  });

  it("still shows the backend message when no mapping is supplied", async () => {
    const { handlers } = renderVoiceDialog();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    emitToAll(handlers, {
      type: "download.error",
      kind: "tts",
      engine: "kokoro",
      message: "Disk full",
    });

    expect(await screen.findByText("Disk full")).toBeInTheDocument();
  });
});


it("names Cohere and its actual download size before starting", async () => {
  const { props } = renderVoiceDialog({ targetModel: undefined, targetBackend: "cohere", eventKind: undefined, targetEngine: undefined });
  expect(screen.getByText("Cohere Transcribe is about 2.9 GB. It downloads once, then runs on your CPU. Continue?")).toBeInTheDocument();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Download" })); });
  expect(props.onConfirm).toHaveBeenCalledOnce();
  expect(screen.getByText("Downloading Cohere Transcribe")).toBeInTheDocument();
});
