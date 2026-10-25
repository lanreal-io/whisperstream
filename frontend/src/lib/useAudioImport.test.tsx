import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAudioImport } from "./useAudioImport";

const mocks = vi.hoisted(() => ({
  choose: vi.fn(),
  importSelections: vi.fn(),
  release: vi.fn(),
  importPaths: vi.fn(),
  open: vi.fn(),
  onDragDropEvent: vi.fn(),
  unlisten: vi.fn(),
}));

vi.mock("./nativeFileWorkflows", () => ({
  chooseMacosImportAudio: mocks.choose,
  releaseMacosNativeSelections: mocks.release,
}));
vi.mock("./api/transcripts", () => ({
    importAudio: mocks.importPaths,
    importAudioSelections: mocks.importSelections,
  }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: mocks.open }));
vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({ onDragDropEvent: mocks.onDragDropEvent }),
}));

describe("useAudioImport native picker outcomes", () => {
  beforeEach(() => {
    mocks.choose.mockReset();
    mocks.importSelections.mockReset();
    mocks.release.mockReset().mockResolvedValue(undefined);
    mocks.importPaths.mockReset().mockResolvedValue(undefined);
    mocks.open.mockReset().mockResolvedValue(null);
    mocks.unlisten.mockReset();
    mocks.onDragDropEvent.mockReset().mockResolvedValue(mocks.unlisten);
  });

  it("delivers Windows picker paths unchanged without using Mac tokens", async () => {
    const paths = ["C:\\Audio\\note.WAV", "C:\\Audio\\song.mp3", "C:\\Audio\\notes.txt"];
    mocks.open.mockResolvedValue(paths);
    const { result } = renderHook(() => useAudioImport(true));
    await act(() => result.current.chooseFiles());
    expect(mocks.open).toHaveBeenCalledWith(expect.objectContaining({ multiple: true }));
    expect(mocks.importPaths).toHaveBeenCalledWith(paths.slice(0, 2));
    expect(mocks.choose).not.toHaveBeenCalled();
    expect(mocks.importSelections).not.toHaveBeenCalled();
    expect(mocks.release).not.toHaveBeenCalled();
  });

  it("imports Windows native drops only while active and releases the listener", async () => {
    const { result, rerender, unmount } = renderHook(
      ({ active }) => useAudioImport(active), { initialProps: { active: false } },
    );
    expect(mocks.onDragDropEvent).not.toHaveBeenCalled();
    rerender({ active: true });
    await waitFor(() => expect(mocks.onDragDropEvent).toHaveBeenCalledTimes(1));
    const onDrop = mocks.onDragDropEvent.mock.calls[0][0];
    act(() => onDrop({ payload: { type: "enter", paths: [] } }));
    expect(result.current.dragActive).toBe(true);
    act(() => onDrop({ payload: { type: "drop", paths: ["C:\\Audio\\native.flac", "C:\\Audio\\skip.exe"] } }));
    expect(result.current.dragActive).toBe(false);
    expect(mocks.importPaths).toHaveBeenCalledWith(["C:\\Audio\\native.flac"]);
    expect(mocks.choose).not.toHaveBeenCalled();
    expect(mocks.importSelections).not.toHaveBeenCalled();
    expect(mocks.release).not.toHaveBeenCalled();
    rerender({ active: false });
    expect(mocks.unlisten).toHaveBeenCalledTimes(1);
    unmount();
    expect(mocks.unlisten).toHaveBeenCalledTimes(1);
  });

  it("keeps explicit cancellation silent", async () => {
    mocks.choose.mockResolvedValue({
      status: "cancelled",
      selections: [],
      destination_token: null,
    });
    const { result } = renderHook(() => useAudioImport(true, true));

    await act(() => result.current.chooseFiles());

    expect(result.current.pickerError).toBe(false);
    expect(result.current.importUnavailable).toBe(false);
    expect(mocks.importSelections).not.toHaveBeenCalled();
  });

  it("surfaces native failure and releases tokens after an unavailable import", async () => {
    mocks.choose
      .mockResolvedValueOnce({
        status: "failed",
        selections: [],
        destination_token: null,
      })
      .mockResolvedValueOnce({
        status: "selected",
        selections: [{ token: "macos-native:one", display_name: "one.wav" }],
        destination_token: null,
      });
    mocks.importSelections.mockRejectedValue(new Error("history unavailable"));
    const { result } = renderHook(() => useAudioImport(true, true));

    await act(() => result.current.chooseFiles());
    expect(result.current.pickerError).toBe(true);

    await act(() => result.current.chooseFiles());
    await waitFor(() => expect(result.current.importUnavailable).toBe(true));
    expect(mocks.release).toHaveBeenCalledWith(["macos-native:one"]);
  });
});
