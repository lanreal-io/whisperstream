import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DictionaryImportModal } from "./DictionaryImportModal";

const mocks = vi.hoisted(() => ({ save: vi.fn(), open: vi.fn(), read: vi.fn(), listen: vi.fn(), stop: vi.fn(), scale: vi.fn() }));
vi.mock("../lib/saveBlob", () => ({ saveBlob: mocks.save }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: mocks.open }));
vi.mock("@tauri-apps/plugin-fs", () => ({ readFile: mocks.read }));
vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => ({ scaleFactor: mocks.scale, onDragDropEvent: mocks.listen }) }));
const config = { word_boosts: ["Existing"], word_filters: [], word_replacements: {} };
const choose = (text: string, name = "vocabulary.csv") => {
  const file = new File([text], name, { type: "text/csv" });
  Object.defineProperty(file, "arrayBuffer", { value: async () => new TextEncoder().encode(text).buffer });
  fireEvent.change(screen.getByLabelText("Choose a vocabulary CSV file"), { target: { files: [file] } });
};
beforeEach(() => {
  mocks.save.mockResolvedValue("saved");
  mocks.listen.mockResolvedValue(mocks.stop);
  mocks.scale.mockResolvedValue(2);
});
afterEach(() => {
  cleanup(); vi.restoreAllMocks(); vi.clearAllMocks();
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});

describe("Dictionary import modal", () => {
  it("shows the approved compact format and downloads the example locally", async () => {
    render(<DictionaryImportModal config={config} onClose={vi.fn()} onImport={vi.fn()} />);
    expect(screen.getByText("The word header is required; replacement is optional.")).toBeInTheDocument();
    expect(screen.queryByText("TanStack")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Download example CSV"));
    await waitFor(() => expect(mocks.save).toHaveBeenCalledWith(expect.any(Blob), "vocabulary-example.csv", "csv"));
  });
  it("reviews word-only CSV, counts duplicates, and waits for persistence before success", async () => {
    let finish!: (count: number) => void;
    const onImport = vi.fn(() => new Promise<number>(resolve => { finish = resolve; }));
    render(<DictionaryImportModal config={config} onClose={vi.fn()} onImport={onImport} />);
    choose("word\nTanStack\nExisting\nVercel");
    await screen.findByRole("button", { name: "Import 2 entries" });
    expect(screen.getByText("1 duplicate skipped")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Import 2 entries" }));
    expect(screen.queryByText("Added 2 entries to Dictionary.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await act(async () => finish(2));
    expect(screen.getByText("Added 2 entries to Dictionary.")).toBeInTheDocument();
  });
  it("keeps failed imports reviewable and retryable", async () => {
    const onImport = vi.fn().mockRejectedValueOnce(new Error("network")).mockResolvedValueOnce(1);
    render(<DictionaryImportModal config={config} onClose={vi.fn()} onImport={onImport} />);
    choose("word\nTanStack");
    fireEvent.click(await screen.findByRole("button", { name: "Import 1 entry" }));
    await screen.findByText("Couldn't import these entries. Try again.");
    fireEvent.click(screen.getByRole("button", { name: "Import 1 entry" }));
    await screen.findByText("Added 1 entry to Dictionary.");
  });
  it("bounds rendered preview rows without truncating the imported data", async () => {
    const onImport = vi.fn().mockResolvedValue(125);
    render(<DictionaryImportModal config={config} onClose={vi.fn()} onImport={onImport} />);
    choose(`word\n${Array.from({ length: 125 }, (_, index) => `Name${index}`).join("\n")}`);
    const importButton = await screen.findByRole("button", { name: "Import 125 entries" });
    expect(screen.getAllByRole("row")).toHaveLength(101);
    expect(screen.getByText("Showing 100 of 125 entries. All 125 will be added when you import.")).toBeInTheDocument();
    expect(screen.queryByText("Name124")).not.toBeInTheDocument();
    fireEvent.click(importButton);
    await screen.findByText("Added 125 entries to Dictionary.");
    expect(onImport.mock.calls[0][0].rows).toHaveLength(125);
  });
  it("keeps full invalid-row totals while bounding error rendering", async () => {
    render(<DictionaryImportModal config={config} onClose={vi.fn()} onImport={vi.fn()} />);
    choose(`word,replacement\n${Array.from({ length: 125 }, () => ",Name").join("\n")}`);
    await screen.findByText("125 rows need attention");
    expect(screen.getByText("Showing 100 of 125 rows that need attention.")).toBeInTheDocument();
    expect(screen.queryByText("Row 126: Add a word or phrase.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import 0 entries" })).toBeDisabled();
  });
  it("blocks malformed rows and recovers after another selection", async () => {
    render(<DictionaryImportModal config={config} onClose={vi.fn()} onImport={vi.fn()} />);
    choose("word,replacement\nGood,\n,Philip");
    expect(await screen.findByText("Row 3: Add a word or phrase.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import 1 entry" })).toBeDisabled();
    choose("replacement,word\nPhilip,Filip");
    await waitFor(() => expect(screen.getByRole("button", { name: "Import 1 entry" })).toBeEnabled());
    expect(screen.getByText("Filip")).toBeInTheDocument();
  });
  it("contains keyboard focus, dismisses safely, and restores its opener", () => {
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([{ width: 1 }] as unknown as DOMRectList);
    const opener = document.createElement("button"); document.body.appendChild(opener); opener.focus();
    const onClose = vi.fn();
    const rendered = render(<DictionaryImportModal config={config} onClose={onClose} onImport={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Close import" })).toHaveFocus();
    expect(opener.inert).toBe(true);
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" }); expect(onClose).toHaveBeenCalledOnce();
    rendered.unmount(); expect(opener).toHaveFocus(); expect(opener.inert).toBeFalsy(); opener.remove();
  });
  it("reads native dropped files only inside the drop zone and releases listeners", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
    mocks.read.mockResolvedValue(new TextEncoder().encode("word\nNativeName"));
    const rendered = render(<DictionaryImportModal config={config} onClose={vi.fn()} onImport={vi.fn()} />);
    await waitFor(() => expect(mocks.listen).toHaveBeenCalledOnce());
    const zone = screen.getByText("Drop a CSV file here").parentElement!;
    vi.spyOn(zone, "getBoundingClientRect").mockReturnValue({ left: 100, right: 300, top: 100, bottom: 200 } as DOMRect);
    const listener = mocks.listen.mock.calls[0][0];
    await act(async () => listener({ payload: { type: "drop", position: { x: 10, y: 10 }, paths: ["C:/outside.csv"] } }));
    expect(mocks.read).not.toHaveBeenCalled();
    mocks.scale.mockResolvedValue(1);
    await act(async () => listener({ payload: { type: "drop", position: { x: 200, y: 150 }, paths: ["C:/vocabulary.csv"] } }));
    await screen.findByText("NativeName"); expect(mocks.read).toHaveBeenCalledWith("C:/vocabulary.csv");
    rendered.unmount(); expect(mocks.stop).toHaveBeenCalledOnce();
  });
  it("cleans up a native listener that finishes registering after dismissal", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
    let finish!: (stop: () => void) => void;
    mocks.listen.mockReturnValue(new Promise<() => void>(resolve => { finish = resolve; }));
    const rendered = render(<DictionaryImportModal config={config} onClose={vi.fn()} onImport={vi.fn()} />);
    await waitFor(() => expect(mocks.listen).toHaveBeenCalledOnce());
    rendered.unmount();
    await act(async () => finish(mocks.stop));
    expect(mocks.stop).toHaveBeenCalledOnce();
  });
  it("does not lose a completed native drop when leave arrives during scale lookup", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
    mocks.read.mockResolvedValue(new TextEncoder().encode("word\nDroppedName"));
    let finishScale!: (scale: number) => void;
    mocks.scale.mockReturnValue(new Promise<number>(resolve => { finishScale = resolve; }));
    render(<DictionaryImportModal config={config} onClose={vi.fn()} onImport={vi.fn()} />);
    await waitFor(() => expect(mocks.listen).toHaveBeenCalledOnce());
    vi.spyOn(screen.getByText("Drop a CSV file here").parentElement!, "getBoundingClientRect").mockReturnValue({ left: 100, right: 300, top: 100, bottom: 200 } as DOMRect);
    const listener = mocks.listen.mock.calls[0][0];
    await act(async () => {
      void listener({ payload: { type: "drop", position: { x: 200, y: 150 }, paths: ["C:/vocabulary.csv"] } });
      void listener({ payload: { type: "leave" } });
      finishScale(1);
    });
    await screen.findByText("DroppedName");
  });
  it("rejects invalid UTF-8 browser files rather than silently replacing characters", async () => {
    render(<DictionaryImportModal config={config} onClose={vi.fn()} onImport={vi.fn()} />);
    const file = new File([], "invalid.csv");
    Object.defineProperty(file, "arrayBuffer", { value: async () => new Uint8Array([0xff]).buffer });
    fireEvent.change(screen.getByLabelText("Choose a vocabulary CSV file"), { target: { files: [file] } });
    await screen.findByText("Couldn't read this file. Choose it again or select another CSV.");
  });
  it("leaves native file picker cancellation silent", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
    mocks.open.mockResolvedValue(null);
    render(<DictionaryImportModal config={config} onClose={vi.fn()} onImport={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Choose file" }));
    await waitFor(() => expect(mocks.open).toHaveBeenCalledOnce());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(mocks.read).not.toHaveBeenCalled();
  });
});
