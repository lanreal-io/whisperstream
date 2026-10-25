import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { ReviewPopup } from "./ReviewPopup";

const mocks = vi.hoisted(() => ({
  lifecycle: vi.fn(async () => true),
  dismissReview: vi.fn(async () => {}),
  open: vi.fn(async () => {}),
  getLockdownStatus: vi.fn(async () => ({ enabled: false, managed: false })),
  flushLogSync: vi.fn(async () => {}),
  handlers: new Map<string, (event: { payload?: unknown }) => void>(),
}));
vi.mock("../lib/windowLifecycle", () => ({ applyWindowLifecycle: mocks.lifecycle, publishPopupVisibility: vi.fn() }));
vi.mock("../lib/api/feedback", () => ({ dismissReview: mocks.dismissReview }));
vi.mock("../lib/api/lockdown", () => ({ getLockdownStatus: mocks.getLockdownStatus }));
vi.mock("@tauri-apps/plugin-shell", () => ({ open: mocks.open }));
vi.mock("../lib/logCapture", () => ({ flushLogSync: mocks.flushLogSync, errorType: () => "Error" }));
vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(async (name: string, handler: (event: { payload?: unknown }) => void) => {
    mocks.handlers.set(name, handler);
    return () => mocks.handlers.delete(name);
  }),
}));
vi.mock("../components/PopupShell", () => ({ PopupShell: ({ children }: { children: ReactNode }) => <div>{children}</div> }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mocks.handlers.clear();
});

it("snoozes on X through the shared native lifecycle", async () => {
  render(<ReviewPopup />);
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledWith("review", "hide"));
  expect(mocks.dismissReview).toHaveBeenCalledWith("later");
  expect(mocks.flushLogSync).toHaveBeenCalled();
});

it("snoozes on Esc", async () => {
  render(<ReviewPopup />);
  fireEvent.keyDown(window, { key: "Escape" });
  await waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledWith("review", "hide"));
  expect(mocks.dismissReview).toHaveBeenCalledWith("later");
});

it("records github and closes after a successful open", async () => {
  render(<ReviewPopup />);
  fireEvent.click(screen.getByRole("button", { name: "Yes" }));
  fireEvent.click(screen.getByRole("button", { name: "Star Whisperstream on GitHub (opens your browser)" }));
  await waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledWith("review", "hide"));
  expect(mocks.open).toHaveBeenCalledWith("https://github.com/lanreal-io/whisperstream");
  expect(mocks.dismissReview).toHaveBeenCalledTimes(1);
  expect(mocks.dismissReview).toHaveBeenCalledWith("github");
  expect(mocks.open.mock.invocationCallOrder[0]).toBeLessThan(mocks.dismissReview.mock.invocationCallOrder[0]);
});

it("keeps the popup open with no dismissal when Lockdown blocks the open", async () => {
  mocks.getLockdownStatus.mockResolvedValueOnce({ enabled: true, managed: false });
  render(<ReviewPopup />);
  fireEvent.click(screen.getByRole("button", { name: "Not really" }));
  fireEvent.click(screen.getByRole("button", { name: "Open an issue on GitHub (opens your browser)" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Unavailable in Lockdown Mode.");
  expect(mocks.open).not.toHaveBeenCalled();
  expect(mocks.dismissReview).not.toHaveBeenCalled();
  expect(mocks.lifecycle).not.toHaveBeenCalled();
});

it("resets to the question on each show, honoring a dev init step once", async () => {
  render(<ReviewPopup />);
  await waitFor(() => expect(mocks.handlers.has("popup-review:show")).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "Yes" }));
  expect(screen.getByRole("heading", { name: "Glad you like it" })).toBeInTheDocument();
  await act(async () => { mocks.handlers.get("popup-review:show")!({}); });
  expect(screen.getByRole("heading", { name: "Enjoying Whisperstream?" })).toBeInTheDocument();
  await act(async () => {
    mocks.handlers.get("popup-review:init")!({ payload: { initialStep: "issue" } });
    mocks.handlers.get("popup-review:show")!({});
  });
  expect(screen.getByRole("heading", { name: "Sorry to hear that" })).toBeInTheDocument();
  await act(async () => { mocks.handlers.get("popup-review:show")!({}); });
  expect(screen.getByRole("heading", { name: "Enjoying Whisperstream?" })).toBeInTheDocument();
});
