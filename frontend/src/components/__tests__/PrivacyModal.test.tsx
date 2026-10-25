import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { PrivacyModal } from "../PrivacyModal";
import * as api_consent from "../../lib/api/consent";

function context(endpoint = "https://example.com:8443/v1"): api_consent.BYOKConsentContext {
  const lines = ["Privacy notice", "Custom endpoint", `Endpoint: ${endpoint}`, "Model: selected-model",
    "Requests go to this endpoint over HTTPS.", "What is sent", "Synthetic disclosure from the backend.",
    "Saved keys", "Windows account scope.", "Your choice", "Review the changed recipient.",
    "Whisperstream privacy policy: bundled PRIVACY.pdf"];
  return { provider: "custom", endpoint, model: "selected-model", context_token: endpoint,
    recipient_identity: "recipient", has_consent: false, matches_config: true,
    dialog_body_text: lines.join("\n"), lines, links: [], legal_identity: { privacy_policy_version: "edition-version" } };
}

beforeEach(() => { vi.spyOn(api_consent, "getBYOKConsentContext").mockResolvedValue(context()); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("PrivacyModal recipient evidence", () => {
  it("renders every canonical body line and submits exactly the loaded context", async () => {
    const onConfirm = vi.fn();
    render(<PrivacyModal provider="custom" customBaseUrl="https://example.com:8443/v1" model="selected-model" onConfirm={onConfirm} onCancel={vi.fn()} />);
    await screen.findByText("Synthetic disclosure from the backend.");
    for (const line of context().lines) expect(screen.getByText(line)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Allow connection" }));
    expect(onConfirm).toHaveBeenCalledWith(context());
    expect(api_consent.getBYOKConsentContext).toHaveBeenCalledWith({ provider: "custom", endpoint: "https://example.com:8443/v1", model: "selected-model" });
  });
  it("blocks consent while loading and cancels without accepting", () => {
    vi.mocked(api_consent.getBYOKConsentContext).mockReturnValue(new Promise(() => {}));
    const cancel = vi.fn(), confirm = vi.fn();
    render(<PrivacyModal provider="custom" onConfirm={confirm} onCancel={cancel} />);
    expect(screen.getByRole("button", { name: "Allow connection" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(cancel).toHaveBeenCalledOnce();
    expect(confirm).not.toHaveBeenCalled();
  });
  it("rejects a body that differs from the displayed canonical lines", async () => {
    vi.mocked(api_consent.getBYOKConsentContext).mockResolvedValue({ ...context(), dialog_body_text: "different" });
    render(<PrivacyModal provider="custom" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the privacy notice. Try again.");
    expect(screen.getByRole("button", { name: "Allow connection" })).toBeDisabled();
  });
  it("shows endpoint errors without rendering raw backend content", async () => {
    vi.mocked(api_consent.getBYOKConsentContext).mockRejectedValue(Object.assign(new Error("secret URL"), { errorCode: "endpoint_https_required" }));
    render(<PrivacyModal provider="custom" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Remote endpoints require HTTPS.");
    expect(screen.queryByText(/secret URL/)).not.toBeInTheDocument();
  });
  it("keeps the notice open on secure save failure and preserves the reviewed target", async () => {
    const onConfirm = vi.fn().mockRejectedValue(Object.assign(new Error("private"), { errorCode: "secure_storage_failed" }));
    render(<PrivacyModal provider="custom" onConfirm={onConfirm} onCancel={vi.fn()} />);
    await screen.findByText("Synthetic disclosure from the backend.");
    fireEvent.click(screen.getByRole("button", { name: "Allow connection" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not save the key securely.");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(`Endpoint: ${context().endpoint}`)).toBeInTheDocument();
  });
  it("refreshes a stale acceptance without silently retrying it", async () => {
    const onConfirm = vi.fn().mockRejectedValue(Object.assign(new Error(), { errorCode: "consent_changed" }));
    render(<PrivacyModal provider="custom" onConfirm={onConfirm} onCancel={vi.fn()} />);
    await screen.findByText("Synthetic disclosure from the backend.");
    fireEvent.click(screen.getByRole("button", { name: "Allow connection" }));
    await waitFor(() => expect(api_consent.getBYOKConsentContext).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("alert")).toHaveTextContent("Connection settings changed.");
    expect(onConfirm).toHaveBeenCalledOnce();
  });
  it("cannot accept an earlier response after the endpoint changes", async () => {
    let finishOld!: (value: api_consent.BYOKConsentContext) => void;
    vi.mocked(api_consent.getBYOKConsentContext).mockReturnValueOnce(new Promise((resolve) => { finishOld = resolve; }))
      .mockResolvedValueOnce(context("https://new.example/v1"));
    const confirm = vi.fn();
    const { rerender } = render(<PrivacyModal provider="custom" customBaseUrl="https://old.example/v1" onConfirm={confirm} onCancel={vi.fn()} />);
    rerender(<PrivacyModal provider="custom" customBaseUrl="https://new.example/v1" onConfirm={confirm} onCancel={vi.fn()} />);
    await screen.findByText("Endpoint: https://new.example/v1");
    finishOld(context("https://old.example/v1"));
    fireEvent.click(screen.getByRole("button", { name: "Allow connection" }));
    expect(confirm).toHaveBeenCalledWith(context("https://new.example/v1"));
  });
});
