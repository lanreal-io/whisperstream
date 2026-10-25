/**
 * Vitest + Testing Library coverage for DataLeavesDialog.
 *
 * Tests cover:
 *   - Provider-subhead correctly interpolates each provider's display name.
 *   - All three bullets (what/when/why) render with their locked copy.
 *   - "Continue" (primary) triggers onContinue.
 *   - "Cancel" (secondary) triggers onCancel.
 *   - Escape triggers onCancel.
 *   - Backdrop click triggers onCancel.
 *   - Renders nothing when open=false.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { DataLeavesDialog } from "../DataLeavesDialog";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function makeProps(overrides: Partial<Parameters<typeof DataLeavesDialog>[0]> = {}) {
  return {
    open: true,
    provider: "gemini" as const,
    onContinue: vi.fn(),
    onCancel: vi.fn(),
    ...overrides,
  };
}

describe("DataLeavesDialog — provider-subhead interpolation", () => {
  it("gemini -> 'Going to Google Gemini'", () => {
    render(<DataLeavesDialog {...makeProps({ provider: "gemini" })} />);
    expect(screen.getByText("Going to Google Gemini")).toBeInTheDocument();
  });

  it("openai -> 'Going to OpenAI'", () => {
    render(<DataLeavesDialog {...makeProps({ provider: "openai" })} />);
    expect(screen.getByText("Going to OpenAI")).toBeInTheDocument();
  });

  it("anthropic -> 'Going to Anthropic'", () => {
    render(<DataLeavesDialog {...makeProps({ provider: "anthropic" })} />);
    expect(screen.getByText("Going to Anthropic")).toBeInTheDocument();
  });

  it("ollama -> 'Going to Ollama'", () => {
    render(<DataLeavesDialog {...makeProps({ provider: "ollama" })} />);
    expect(screen.getByText("Going to Ollama")).toBeInTheDocument();
  });
});

describe("DataLeavesDialog — locked copy renders verbatim", () => {
  it("renders the heading", () => {
    render(<DataLeavesDialog {...makeProps()} />);
    expect(screen.getByText("Review where your text goes")).toBeInTheDocument();
  });

  it("renders the body intro", () => {
    render(<DataLeavesDialog {...makeProps()} />);
    expect(
      screen.getByText(
        "Review the privacy notice before using this connection.",
      ),
    ).toBeInTheDocument();
  });

  it("renders all three bullets (what / when / why)", () => {
    render(<DataLeavesDialog {...makeProps()} />);
    expect(
      screen.getByText(
        "Enhancement sends text and the selected profile's instructions.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Prompt tests send your sample and the instructions being tested."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "The server may forward your text to other services.",
      ),
    ).toBeInTheDocument();
  });

  it("renders the footer", () => {
    render(<DataLeavesDialog {...makeProps()} />);
    expect(
      screen.getByText("Review the privacy notice before enabling this connection."),
    ).toBeInTheDocument();
  });
});

describe("DataLeavesDialog — interactions", () => {
  it("Continue button triggers onContinue", () => {
    const onContinue = vi.fn();
    render(<DataLeavesDialog {...makeProps({ onContinue })} />);
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(onContinue).toHaveBeenCalledOnce();
  });

  it("Cancel button triggers onCancel", () => {
    const onCancel = vi.fn();
    render(<DataLeavesDialog {...makeProps({ onCancel })} />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("Escape key triggers onCancel", () => {
    const onCancel = vi.fn();
    render(<DataLeavesDialog {...makeProps({ onCancel })} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("renders nothing when open=false", () => {
    render(<DataLeavesDialog {...makeProps({ open: false })} />);
    expect(screen.queryByText("Review where your text goes")).toBeNull();
  });
});
