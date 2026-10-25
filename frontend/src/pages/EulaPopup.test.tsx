import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EulaPopup } from "./EulaPopup";
import type { EulaEmitPayload } from "../lib/api/consent";

const mocks = vi.hoisted(() => ({
  accept: vi.fn(async () => ({})),
  exit: vi.fn(async () => undefined),
  save: vi.fn(async (): Promise<"saved" | "cancelled"> => "saved"),
}));

vi.mock("../lib/api/consent", () => ({
    acceptEula: mocks.accept,
    getEulaStatus: vi.fn(),
  }));
vi.mock("../lib/eulaTerms", () => ({
  saveExactEulaTerms: mocks.save,
}));
vi.mock("../lib/logCapture", () => ({
  errorType: () => "Error",
  flushLogSync: vi.fn(async () => undefined),
}));
vi.mock("@tauri-apps/api/event", () => ({ emit: vi.fn(async () => undefined) }));
vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({ hide: vi.fn(async () => undefined), listen: vi.fn() }),
}));
vi.mock("@tauri-apps/plugin-process", () => ({ exit: mocks.exit }));

const payload: EulaEmitPayload = {
  mode: "substantive-update",
  eulaText: "EXACT TERMS\r\nVersion 2.0\r\n",
  eulaVersion: "2.0",
  eulaSha256: "a".repeat(64),
  softwareVersion: "0.8.0",
  changeBullets: ["A separately approved material change."],
};

const reacknowledgementPayload: EulaEmitPayload = {
  ...payload,
  mode: "reacknowledge",
  eulaVersion: "1.5",
  eulaSha256: "9".repeat(64),
  softwareVersion: "1.5.0",
  changeBullets: [
    "Model downloads now use Whisperstream's Cloudflare R2 endpoint instead of contacting Hugging Face from the production app.",
    "Opening Enhancement can request the model catalog to check which optional models are available.",
    "Lockdown Mode keeps bounded, content-safe diagnostics on your device for troubleshooting. Nothing is uploaded automatically, and its separate access log is excluded from support exports.",
  ],
};

describe("substantive EULA update gate", () => {
  beforeEach(() => {
    mocks.accept.mockClear();
    mocks.exit.mockClear();
    mocks.save.mockReset().mockResolvedValue("saved");
  });
  afterEach(cleanup);

  it("shows complete terms rather than historical change bullets on first run", () => {
    render(<EulaPopup inline initialPayload={{ ...payload, mode: "first-run" }} />);

    expect(screen.getByRole("region", { name: "End User License Agreement, scrollable" })).toHaveTextContent("EXACT TERMS");
    expect(screen.queryByText(payload.changeBullets![0])).not.toBeInTheDocument();
    expect(screen.queryByText("What changed")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "I agree" })).toBeEnabled();
    expect(mocks.accept).not.toHaveBeenCalled();
  });

  it("shows the approved Option A boundary with safe initial focus", async () => {
    render(<EulaPopup inline initialPayload={payload} />);

    const heading = screen.getByRole("heading", { name: "Review updated terms" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.getByText("End User License Agreement, version 2.0")).toBeInTheDocument();
    expect(screen.getByText(/dictation, imports, enhancement, transcript access, and Agent Speak stay unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/does not change the agreement that governed an earlier release/)).toBeInTheDocument();
    expect(screen.getByText(/Installing or relaunching this release does not accept/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accept updated terms" })).toBeEnabled();
  });

  it("bounds a long update summary while keeping the decision controls outside it", () => {
    render(
      <EulaPopup
        inline
        initialPayload={{
          ...payload,
          changeBullets: Array.from(
            { length: 8 },
            (_, index) => `Material change ${index + 1} with enough detail to wrap across several lines in the fixed-height startup gate.`,
          ),
        }}
      />,
    );

    const summary = screen.getByRole("region", {
      name: "Summary of updated terms, scrollable",
    });
    expect(summary).toHaveStyle({ height: "246px", overflowY: "auto" });
    expect(summary).not.toContainElement(
      screen.getByRole("button", { name: "Accept updated terms" }),
    );
    expect(summary).not.toContainElement(
      screen.getByRole("button", { name: "Decline and quit" }),
    );
  });

  it("shows the policy-selected 1.5 re-acknowledgement summary", () => {
    render(<EulaPopup inline initialPayload={reacknowledgementPayload} />);

    expect(screen.getByRole("heading", { name: "We've updated our terms" })).toBeInTheDocument();
    for (const bullet of reacknowledgementPayload.changeBullets ?? []) {
      expect(screen.getByText(bullet)).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "I agree" })).toBeEnabled();
  });

  it("reviews and saves the exact policy-selected bytes without treating review as acceptance", async () => {
    render(<EulaPopup inline initialPayload={payload} />);
    fireEvent.click(screen.getByRole("button", { name: "Read complete terms" }));
    const terms = screen.getByRole("region", { name: "End User License Agreement, version 2.0" });
    await waitFor(() => expect(terms).toHaveFocus());
    expect(terms).toHaveTextContent("EXACT TERMS");

    fireEvent.click(screen.getByRole("button", { name: "Save a copy" }));
    await screen.findByText("Terms saved.");
    expect(mocks.save).toHaveBeenCalledWith(payload.eulaText, "2.0");
    expect(mocks.accept).not.toHaveBeenCalled();
  });

  it("returns silently when the save dialog is cancelled", async () => {
    mocks.save.mockResolvedValueOnce("cancelled");
    render(<EulaPopup inline initialPayload={payload} />);
    fireEvent.click(screen.getByRole("button", { name: "Save a copy" }));
    await waitFor(() => expect(mocks.save).toHaveBeenCalledOnce());
    expect(screen.queryByText("Terms saved.")).toBeNull();
    expect(screen.queryByText("Couldn't save the terms. Try again.")).toBeNull();
    expect(mocks.accept).not.toHaveBeenCalled();
  });

  it("treats Escape, silence, and unmount as nonacceptance", async () => {
    const view = render(<EulaPopup inline initialPayload={payload} />);
    await waitFor(() => expect(
      screen.getByRole("heading", { name: "Review updated terms" }),
    ).toHaveFocus());

    fireEvent.keyDown(window, { key: "Escape" });
    await Promise.resolve();
    view.unmount();

    expect(mocks.accept).not.toHaveBeenCalled();
    expect(mocks.exit).not.toHaveBeenCalled();
  });

  it("records only an explicit acceptance and refreshes the consent authority", async () => {
    const onAccepted = vi.fn(async () => undefined);
    render(<EulaPopup inline initialPayload={payload} onAccepted={onAccepted} />);
    fireEvent.keyDown(window, { key: "Enter" });
    expect(mocks.accept).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Accept updated terms" }));
    await waitFor(() => expect(onAccepted).toHaveBeenCalledOnce());
    expect(mocks.accept).toHaveBeenCalledWith({
      eula_version: "2.0",
      eula_sha256: "a".repeat(64),
    });
  });

  it("keeps the gate closed when acceptance persistence fails", async () => {
    const onAccepted = vi.fn(async () => undefined);
    mocks.accept.mockRejectedValueOnce(new Error("disk full"));
    render(<EulaPopup inline initialPayload={payload} onAccepted={onAccepted} />);

    fireEvent.click(screen.getByRole("button", { name: "Accept updated terms" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't save your acceptance. Nothing was accepted. Try again.",
    );
    expect(onAccepted).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Accept updated terms" })).toBeEnabled();
  });

  it("declines by exiting without calling the acceptance endpoint", async () => {
    render(<EulaPopup inline initialPayload={payload} />);
    fireEvent.click(screen.getByRole("button", { name: "Decline and quit" }));
    await waitFor(() => expect(mocks.exit).toHaveBeenCalledWith(0));
    expect(mocks.accept).not.toHaveBeenCalled();
  });

  it("fails closed when the policy configuration is invalid", () => {
    render(
      <EulaPopup
        inline
        initialPayload={{ ...payload, mode: "configuration-error", eulaText: "", eulaSha256: "" }}
      />,
    );
    expect(screen.getByRole("button", { name: "Read complete terms" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save a copy" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Accept updated terms" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't save your acceptance. Nothing was accepted. Try again.");
  });
});
