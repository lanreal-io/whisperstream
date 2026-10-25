import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModelRowActions } from "./ModelRowActions";
import { localModelCapabilities } from "./localModels";

afterEach(cleanup);
describe("model catalog boundaries", () => {
  it("prefers server capabilities over legacy prompt metadata and model identity", () => {
    const capabilities = { family: "customizable" as const, editable_prompt: true, supported_modes: ["email" as const] };
    expect(localModelCapabilities({ id: "unknown", label: "Unknown", disk_mb: 0, ram_mb: 0, prompt_key: "stream", capabilities })).toBe(capabilities);
  });

  it("keeps older backends compatible without recognizing model IDs", () => {
    expect(localModelCapabilities({ id: "new-mini", label: "New", disk_mb: 0, ram_mb: 0, prompt_key: "stream-mini", trained_modes: ["chat", "email"] })).toEqual({
      family: "trained-mode", editable_prompt: false, supported_modes: ["chat", "email"],
    });
    // A retired prompt family no longer gets special controls.
    expect(localModelCapabilities({ id: "legacy", label: "Legacy", disk_mb: 0, ram_mb: 0, prompt_key: "retired" })).toEqual({
      family: "customizable", editable_prompt: true, supported_modes: null,
    });
  });

  it("focuses confirmation, cancels safely, and deletes only on confirmation", () => {
    const onDelete = vi.fn();
    render(<ModelRowActions installed modelId="selected" modelLabel="Stream-1 Mini" sizeLabel="462 MB" onDelete={onDelete} onDownload={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Delete Stream-1 Mini" }));
    expect(screen.getByRole("button", { name: "Delete Stream-1 Mini" })).toHaveFocus();
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Keep Stream-1 Mini" }));
    expect(onDelete).not.toHaveBeenCalled();
    // Cancelling returns focus to the trash so keyboard users stay in place.
    expect(screen.getByRole("button", { name: "Delete Stream-1 Mini" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Delete Stream-1 Mini" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Stream-1 Mini" }));
    expect(onDelete).toHaveBeenCalledExactlyOnceWith("selected");
  });

  it("cancels the confirm on Escape and returns focus to the trash", () => {
    const onDelete = vi.fn();
    render(<ModelRowActions installed modelId="m" modelLabel="Customizable" sizeLabel="3.1 GB" onDelete={onDelete} onDownload={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Delete Customizable" }));
    const confirm = screen.getByRole("button", { name: "Delete Customizable" });
    expect(confirm).toHaveFocus();
    fireEvent.keyDown(confirm, { key: "Escape" });
    expect(screen.queryByRole("button", { name: "Keep Customizable" })).toBeNull();
    expect(screen.getByRole("button", { name: "Delete Customizable" })).toHaveFocus();
    expect(onDelete).not.toHaveBeenCalled();
  });

  it("names the model and size on the always-visible download action", () => {
    const onDownload = vi.fn();
    render(<ModelRowActions installed={false} modelId="m" modelLabel="Customizable" sizeLabel="3.1 GB" onDelete={vi.fn()} onDownload={onDownload} />);
    const download = screen.getByRole("button", { name: "Download Customizable, 3.1 GB" });
    // Visible at rest: no opacity-0 reveal on the download glyph.
    expect(download.className).not.toContain("opacity-0");
    fireEvent.click(download);
    expect(onDownload).toHaveBeenCalledOnce();
  });

  it("renders no action while the model is downloading", () => {
    const { container } = render(<ModelRowActions installed={false} downloading modelId="m" modelLabel="Customizable" sizeLabel="3.1 GB" onDelete={vi.fn()} onDownload={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("resets pending confirmation when installation changes", () => {
    const props = { modelId: "model", modelLabel: "Model", sizeLabel: "1 MB", onDelete: vi.fn(), onDownload: vi.fn() };
    const { rerender } = render(<ModelRowActions {...props} installed />);
    fireEvent.click(screen.getByRole("button", { name: "Delete Model" }));
    rerender(<ModelRowActions {...props} installed={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Download Model, 1 MB" }));
    expect(props.onDownload).toHaveBeenCalledOnce();
    rerender(<ModelRowActions {...props} installed />);
    expect(screen.queryByRole("button", { name: "Keep Model" })).not.toBeInTheDocument();
  });
});
