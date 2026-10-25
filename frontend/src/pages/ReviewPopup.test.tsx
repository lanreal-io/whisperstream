import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GITHUB_ISSUE_URL, GITHUB_STAR_URL, ReviewContent } from "./ReviewPopup";

const mocks = vi.hoisted(() => ({
  open: vi.fn(async (_url: string) => {}),
  getLockdownStatus: vi.fn(async () => ({ enabled: false, managed: false })),
}));
vi.mock("@tauri-apps/plugin-shell", () => ({ open: mocks.open }));
vi.mock("../lib/api/lockdown", () => ({ getLockdownStatus: mocks.getLockdownStatus }));

const STAR_NAME = "Star Whisperstream on GitHub (opens your browser)";
const ISSUE_NAME = "Open an issue on GitHub (opens your browser)";
const ERROR_COPY = "Couldn't open your browser. Try again, or use the link in About.";
const LOCKDOWN_COPY = "Unavailable in Lockdown Mode.";

beforeEach(() => {
  mocks.open.mockImplementation(async () => {});
  mocks.getLockdownStatus.mockImplementation(async () => ({ enabled: false, managed: false }));
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderContent(props: Partial<Parameters<typeof ReviewContent>[0]> = {}) {
  const onDismiss = vi.fn();
  render(<ReviewContent onDismiss={onDismiss} {...props} />);
  return { onDismiss };
}

describe("ReviewContent steps", () => {
  it("asks the question with Yes primary and Not really secondary, collecting nothing", () => {
    renderContent();
    expect(screen.getByRole("heading", { name: "Enjoying Whisperstream?" })).toBeInTheDocument();
    const group = screen.getByRole("group", { name: "Enjoying Whisperstream?" });
    const buttons = Array.from(group.querySelectorAll("button")).map((b) => b.textContent);
    expect(buttons).toEqual(["Yes", "Not really"]);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(mocks.open).not.toHaveBeenCalled();
  });

  it("Yes leads to the star step with its caption", () => {
    renderContent();
    fireEvent.click(screen.getByRole("button", { name: "Yes" }));
    expect(screen.getByRole("heading", { name: "Glad you like it" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: STAR_NAME })).toHaveTextContent("Star on GitHub");
    expect(screen.getByText("A star on GitHub helps more people find Whisperstream.")).toBeInTheDocument();
  });

  it("Not really leads to the issue step with its caption", () => {
    renderContent();
    fireEvent.click(screen.getByRole("button", { name: "Not really" }));
    expect(screen.getByRole("heading", { name: "Sorry to hear that" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: ISSUE_NAME })).toHaveTextContent("Open an issue");
    expect(screen.getByText("Tell us what's getting in your way with an issue on GitHub.")).toBeInTheDocument();
  });
});

describe("ReviewContent GitHub handoff", () => {
  it("opens the exact repository URL and records github only after a successful open", async () => {
    let resolveOpen: () => void = () => {};
    mocks.open.mockImplementation(() => new Promise<void>((resolve) => { resolveOpen = resolve; }));
    const { onDismiss } = renderContent({ initialStep: "star" });
    fireEvent.click(screen.getByRole("button", { name: STAR_NAME }));
    await waitFor(() => expect(mocks.open).toHaveBeenCalledWith(GITHUB_STAR_URL));
    expect(GITHUB_STAR_URL).toBe("https://github.com/lanreal-io/whisperstream");
    expect(onDismiss).not.toHaveBeenCalled();
    await act(async () => { resolveOpen(); });
    await waitFor(() => expect(onDismiss).toHaveBeenCalledWith("github"));
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(mocks.getLockdownStatus.mock.invocationCallOrder[0])
      .toBeLessThan(mocks.open.mock.invocationCallOrder[0]);
  });

  it("opens the exact issue chooser URL", async () => {
    const { onDismiss } = renderContent({ initialStep: "issue" });
    fireEvent.click(screen.getByRole("button", { name: ISSUE_NAME }));
    await waitFor(() => expect(onDismiss).toHaveBeenCalledWith("github"));
    expect(mocks.open).toHaveBeenCalledWith("https://github.com/lanreal-io/whisperstream/issues/new/choose");
    expect(GITHUB_ISSUE_URL).toBe("https://github.com/lanreal-io/whisperstream/issues/new/choose");
  });

  it("swaps the caption for the error on a failed open and retries", async () => {
    mocks.open.mockRejectedValueOnce(new Error("no browser"));
    const { onDismiss } = renderContent({ initialStep: "star" });
    fireEvent.click(screen.getByRole("button", { name: STAR_NAME }));
    expect(await screen.findByRole("alert")).toHaveTextContent(ERROR_COPY);
    expect(screen.queryByText("A star on GitHub helps more people find Whisperstream.")).not.toBeInTheDocument();
    expect(onDismiss).not.toHaveBeenCalled();
    const cta = screen.getByRole("button", { name: STAR_NAME });
    expect(cta).toBeEnabled();
    fireEvent.click(cta);
    await waitFor(() => expect(onDismiss).toHaveBeenCalledWith("github"));
    expect(mocks.open).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["enabled", async () => ({ enabled: true, managed: false })],
    ["managed", async () => ({ enabled: false, managed: true })],
    ["incomplete", async () => ({ enabled: false })],
    ["failed", async () => { throw new Error("offline"); }],
  ])("blocks the open with a fresh Lockdown read when policy is %s", async (_label, status) => {
    mocks.getLockdownStatus.mockImplementation(status as never);
    const { onDismiss } = renderContent({ initialStep: "issue" });
    fireEvent.click(screen.getByRole("button", { name: ISSUE_NAME }));
    expect(await screen.findByRole("alert")).toHaveTextContent(LOCKDOWN_COPY);
    expect(mocks.getLockdownStatus).toHaveBeenCalledTimes(1);
    expect(mocks.open).not.toHaveBeenCalled();
    expect(onDismiss).not.toHaveBeenCalled();
    // A later attempt reads policy again rather than reusing the last answer.
    fireEvent.click(screen.getByRole("button", { name: ISSUE_NAME }));
    await waitFor(() => expect(mocks.getLockdownStatus).toHaveBeenCalledTimes(2));
    expect(mocks.open).not.toHaveBeenCalled();
  });
});

describe("ReviewContent snooze", () => {
  it.each(["question", "star", "issue"] as const)("X snoozes on the %s step", (step) => {
    const { onDismiss } = renderContent({ initialStep: step });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onDismiss).toHaveBeenCalledWith("later");
    expect(mocks.open).not.toHaveBeenCalled();
  });

  it("Esc snoozes once", () => {
    const { onDismiss } = renderContent({ initialStep: "star" });
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(onDismiss).toHaveBeenCalledWith("later");
  });

  it("renders seeded QA notices in the caption slot", () => {
    renderContent({ initialStep: "issue", initialNotice: "lockdown" });
    expect(screen.getByRole("alert")).toHaveTextContent(LOCKDOWN_COPY);
    expect(screen.queryByText("Tell us what's getting in your way with an issue on GitHub.")).not.toBeInTheDocument();
  });
});
