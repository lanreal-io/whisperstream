/**
 * Vitest + Testing Library coverage for LowRamDialog.
 *
 * Tests cover:
 *   - Renders the locked heading and body (which interpolates the user's
 *     own system RAM but never names the model's RAM requirement).
 *   - "Use Gemini's free tier" (primary, gradient pill) triggers onSwitchToCloud.
 *   - "Download anyway" (secondary, glass pill) triggers onContinue.
 *   - Escape triggers onDismiss.
 *   - Renders nothing when open=false.
 *   - No supporting provider-price prose is rendered.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { LowRamDialog } from "../LowRamDialog";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function makeProps(overrides: Partial<Parameters<typeof LowRamDialog>[0]> = {}) {
  return {
    open: true,
    systemRamGb: 4,
    onSwitchToCloud: vi.fn(),
    onContinue: vi.fn(),
    onDismiss: vi.fn(),
    ...overrides,
  };
}

describe("LowRamDialog", () => {
  it("renders the locked heading", () => {
    render(<LowRamDialog {...makeProps()} />);
    expect(
      screen.getByText("Your machine may not have enough memory"),
    ).toBeInTheDocument();
  });

  it("interpolates the system RAM value into the body", () => {
    render(<LowRamDialog {...makeProps({ systemRamGb: 6 })} />);
    // The body lives in a single <p>. We search by partial text since the
    // RAM value is wrapped in a <span> for tabular-nums.
    expect(
      screen.getByText(
        (_, node) =>
          node?.tagName === "P" &&
          (node.textContent ?? "").includes("6") &&
          (node.textContent ?? "").includes("Your machine has") &&
          (node.textContent ?? "").includes("additional memory"),
      ),
    ).toBeInTheDocument();
  });

  it("body does NOT name the model's required RAM (vague copy)", () => {
    // Regression guard: post-RAM-threshold-cleanup the dialog stops
    // claiming a specific GB figure for the on-device model.
    render(<LowRamDialog {...makeProps({ systemRamGb: 4 })} />);
    expect(
      screen.queryByText(/recommended for this model/),
    ).toBeNull();
  });

  it("does not render supporting provider-price prose", () => {
    render(<LowRamDialog {...makeProps()} />);
    expect(screen.queryByText(/Cloud providers need an API key/)).toBeNull();
    expect(screen.queryByText(/OpenAI and Anthropic are paid/)).toBeNull();
  });

  it("primary button uses the single Gemini CTA and triggers onSwitchToCloud", () => {
    const onSwitchToCloud = vi.fn();
    render(<LowRamDialog {...makeProps({ onSwitchToCloud })} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Use Gemini's free tier" }),
    );
    expect(onSwitchToCloud).toHaveBeenCalledOnce();
  });

  it("secondary button 'Download anyway' triggers onContinue", () => {
    const onContinue = vi.fn();
    render(<LowRamDialog {...makeProps({ onContinue })} />);
    fireEvent.click(screen.getByRole("button", { name: "Download anyway" }));
    expect(onContinue).toHaveBeenCalledOnce();
  });

  it("Escape key triggers onDismiss", () => {
    const onDismiss = vi.fn();
    render(<LowRamDialog {...makeProps({ onDismiss })} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it("renders nothing when open=false", () => {
    render(<LowRamDialog {...makeProps({ open: false })} />);
    expect(
      screen.queryByText("Your machine may not have enough memory"),
    ).toBeNull();
  });
});
