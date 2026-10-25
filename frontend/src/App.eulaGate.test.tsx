import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EulaEmitPayload } from "./lib/api/consent";

const mocks = vi.hoisted(() => ({ socket: vi.fn() }));

vi.mock("./lib/socket/connection", async (importOriginal) => {
  const original = await importOriginal<typeof import("./lib/socket/connection")>();
  return {
    ...original,
    useAppSocket: mocks.socket,
  };
});
vi.mock("./pages/EulaPopup", () => ({
  EULA_INLINE_BACKDROP_STYLE: {},
  EulaPopup: ({ initialPayload }: { initialPayload?: EulaEmitPayload }) => (
    <div data-testid="mock-eula-gate">
      {initialPayload?.mode}:{initialPayload?.eulaVersion}:
      {initialPayload?.eulaText}:{initialPayload?.changeBullets?.join("|")}
    </div>
  ),
}));

import { EulaConsentGate } from "./App";

const substantiveStatus = {
  state: "substantive_update_required",
  acceptance: {
    eula_version: "1.3",
    eula_accepted_at: "2026-01-01T00:00:00Z",
    software_version: "0.7.0",
    eula_sha256: "b".repeat(64),
  },
  notice_id: "eula-2.0",
  requirement_kind: "substantive_update",
  summary: ["Approved summary"],
  policy_reason: "valid",
  grant_reason: "acceptance_target_mismatch",
  bundled_version: "2.0",
  bundled_sha256: "a".repeat(64),
  bundled_text: "exact terms",
  software_version: "0.8.0",
  needs_first_run: false,
  needs_amendment: true,
  needs_reacknowledgement: false,
  pinned_version: "1.3",
};

describe("consent-first App hydration", () => {
  beforeEach(() => {
    mocks.socket.mockReset();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      expect(String(input)).toContain("/api/eula/status");
      return new Response(JSON.stringify(substantiveStatus), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }));
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("does not construct the content WebSocket while updated terms are pending", async () => {
    render(<EulaConsentGate />);
    await screen.findByTestId("mock-eula-gate");
    expect(screen.getByTestId("mock-eula-gate")).toHaveTextContent("Approved summary");
    expect(mocks.socket).not.toHaveBeenCalled();
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("retries a transient status failure before hydrating the real EULA gate", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock
      .mockRejectedValueOnce(new Error("backend not listening yet"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify(substantiveStatus), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );

    render(<EulaConsentGate />);

    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId("loading-curtain")).toBeInTheDocument();
    expect(screen.queryByTestId("mock-eula-gate")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(mocks.socket).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(screen.getByTestId("mock-eula-gate")).toHaveTextContent(
      "substantive-update:2.0:exact terms:Approved summary",
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(mocks.socket).not.toHaveBeenCalled();
  });
});

it("stops retrying a dead backend and retries only after user action", async () => {
  vi.useFakeTimers();
  mocks.socket.mockReset();
  const fetchMock = vi.fn().mockRejectedValue(new Error("connection refused"));
  vi.stubGlobal("fetch", fetchMock);
  try {
    render(<EulaConsentGate />);
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(screen.getByTestId("startup-failure-curtain")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(mocks.socket).not.toHaveBeenCalled();
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(substantiveStatus)));
    await act(async () => { screen.getByRole("button", { name: "Try again" }).click(); });
    expect(screen.getByTestId("mock-eula-gate")).toBeInTheDocument();
    expect(mocks.socket).not.toHaveBeenCalled();
  } finally {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  }
});
