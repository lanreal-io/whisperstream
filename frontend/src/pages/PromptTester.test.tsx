/**
 * Vitest coverage for the per-profile "Try it" prompt tester (issue 4).
 *
 * Tests:
 *   - Entering sample input + Run calls previewEnhancement with the row's
 *     in-editor prompt and renders the returned output inline.
 *   - When the gate is "disconnected" the Run button is disabled and the
 *     "Connect a model to test prompts." hint is shown.
 *   - When the gate is "warming" the warming hint is shown.
 *   - A provider failure renders the error fallback string.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { PromptTester } from "./Enhancement";
import * as api_enhancement from "../lib/api/enhancement";

const insetStyle = {};

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  // globals:false in vitest.config means Testing Library's auto-cleanup is
  // off; clear the jsdom tree between tests so duplicate renders don't collide.
  cleanup();
  vi.restoreAllMocks();
});

describe("PromptTester", () => {
  it.each([
    ["disabled", "Turn on Enhancement to test prompts or check the connection."],
    ["consent", "Review the privacy notice to test prompts with this connection."],
  ] as const)("blocks requests when %s and retains the sample", (gate, hint) => {
    const spy = vi.spyOn(api_enhancement, "previewEnhancement");
    render(<PromptTester systemPrompt="P" gate={gate} insetStyle={{}} reducedMotion />);
    fireEvent.click(screen.getByText("Try it"));
    fireEvent.change(screen.getByLabelText("Sample input"), { target: { value: "sample" } });
    expect(screen.getByRole("button", { name: "Run prompt test" })).toBeDisabled();
    expect(screen.getByText(hint)).toBeInTheDocument();
    expect(screen.getByLabelText("Sample input")).toHaveValue("sample");
    expect(spy).not.toHaveBeenCalled();
  });

  it("stops retrying when permission is revoked after the editor opens", async () => {
    const spy = vi.spyOn(api_enhancement, "previewEnhancement").mockRejectedValue(Object.assign(new Error("private server detail"), { errorCode: "consent_required" }));
    render(<PromptTester systemPrompt="P" gate="ready" insetStyle={{}} reducedMotion />);
    fireEvent.click(screen.getByText("Try it"));
    fireEvent.change(screen.getByLabelText("Sample input"), { target: { value: "sample" } });
    fireEvent.click(screen.getByRole("button", { name: "Run prompt test" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Review the privacy notice before using this connection.");
    expect(screen.getByRole("button", { name: "Run prompt test" })).toBeDisabled();
    expect(spy).toHaveBeenCalledOnce();
    expect(screen.queryByText(/private server detail/)).not.toBeInTheDocument();
  });
  it("Run calls previewEnhancement with the in-editor prompt and renders output", async () => {
    const spy = vi
      .spyOn(api_enhancement, "previewEnhancement")
      .mockResolvedValue("I'll be five minutes late.");

    render(
      <PromptTester
        systemPrompt="MY IN-EDITOR PROMPT"
        gate="ready"
        insetStyle={insetStyle}
        reducedMotion
      />,
    );

    // Collapsed by default — expand the disclosure.
    fireEvent.click(screen.getByText("Try it"));

    const input = screen.getByLabelText("Sample input");
    fireEvent.change(input, {
      target: { value: "tell him i'll be 5 min late" },
    });

    fireEvent.click(screen.getByRole("button", { name: "Run prompt test" }));

    // Called with (sample, in-editor prompt).
    expect(spy).toHaveBeenCalledWith(
      "tell him i'll be 5 min late",
      "MY IN-EDITOR PROMPT",
    );

    // Output renders inline.
    await waitFor(() =>
      expect(screen.getByText("I'll be five minutes late.")).toBeInTheDocument(),
    );
  });

  it("disables Run and shows the connect hint when disconnected", async () => {
    const spy = vi.spyOn(api_enhancement, "previewEnhancement");

    render(
      <PromptTester
        systemPrompt="P"
        gate="disconnected"
        insetStyle={insetStyle}
        reducedMotion
      />,
    );

    fireEvent.click(screen.getByText("Try it"));
    fireEvent.change(screen.getByLabelText("Sample input"), {
      target: { value: "some sample" },
    });

    const run = screen.getByRole("button", { name: "Run prompt test" });
    expect(run).toBeDisabled();

    // Clicking a disabled button is a no-op; the API must not be called.
    fireEvent.click(run);
    expect(spy).not.toHaveBeenCalled();

    expect(
      screen.getByText("Connect a model to test prompts."),
    ).toBeInTheDocument();
  });

  it("shows the warming hint when the local model is warming up", () => {
    render(
      <PromptTester
        systemPrompt="P"
        gate="warming"
        insetStyle={insetStyle}
        reducedMotion
      />,
    );

    fireEvent.click(screen.getByText("Try it"));

    expect(
      screen.getByText("Warming up the local model…"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Run prompt test" }),
    ).toBeDisabled();
  });

  it("renders the error fallback when the provider call fails", async () => {
    vi.spyOn(api_enhancement, "previewEnhancement").mockRejectedValue(
      new Error("500"),
    );

    render(
      <PromptTester
        systemPrompt="P"
        gate="ready"
        insetStyle={insetStyle}
        reducedMotion
      />,
    );

    fireEvent.click(screen.getByText("Try it"));
    fireEvent.change(screen.getByLabelText("Sample input"), {
      target: { value: "some sample" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run prompt test" }));

    await waitFor(() =>
      expect(
        screen.getByText("Could not run the test. Check your connection."),
      ).toBeInTheDocument(),
    );
  });
});
