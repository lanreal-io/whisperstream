import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { About } from "./About";
import type { DailyUseCapabilityReport } from "../lib/dailyUseCapabilities";

const CAPABILITIES: DailyUseCapabilityReport = {
  schema_version: 1,
  platform: "Windows",
  capabilities: [
    { id: "auto-update", state: "available", reason: "implemented", presentation: "enable" },
  ],
};

const mocks = vi.hoisted(() => ({
  lockdown: vi.fn().mockResolvedValue({ enabled: false, managed: false }),
  open: vi.fn<(url: string) => Promise<void>>(),
  exportLogs: vi.fn().mockResolvedValue("saved"),
  saveBlob: vi.fn().mockResolvedValue("saved"),
}));

vi.mock("@tauri-apps/plugin-shell", () => ({
  open: mocks.open,
}));

vi.mock("../lib/api/lockdown", () => ({
    getLockdownStatus: mocks.lockdown,
  }));
vi.mock("../lib/api/system", () => ({
    getVersion: vi.fn().mockResolvedValue({ version: "1.0.0" }),
  }));
vi.mock("../lib/api/diagnostics", () => ({
    exportLogs: mocks.exportLogs,
  }));

vi.mock("../lib/saveBlob", () => ({ saveBlob: mocks.saveBlob }));
// Builds without an updater provider register none.
vi.mock("../lib/officialUpdater", () => ({ officialUpdater: null, loadOfficialUpdatePopup: null }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mocks.lockdown.mockResolvedValue({ enabled: false, managed: false });
  vi.unstubAllGlobals();
});

beforeEach(() => {
  mocks.exportLogs.mockReset().mockResolvedValue("saved");
});

describe("About feature request action", () => {
  it("reports Lockdown denial without claiming a successful export", async () => {
    mocks.exportLogs.mockResolvedValueOnce("denied");
    render(<About capabilities={CAPABILITIES} />);
    fireEvent.click(screen.getByRole("button", { name: "Export logs" }));
    expect(
      await screen.findByRole("button", { name: "Unavailable in Lockdown" }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Exported" })).toBeNull();
  });

  it("keeps ordinary save-panel cancellation as a silent no-change outcome", async () => {
    mocks.exportLogs.mockResolvedValueOnce("cancelled");
    render(<About capabilities={CAPABILITIES} />);
    fireEvent.click(screen.getByRole("button", { name: "Export logs" }));
    expect(await screen.findByRole("button", { name: "Export logs" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Exported" })).toBeNull();
  });

  it("names the feature request, exposes GitHub, and opens the form", async () => {
    mocks.open.mockResolvedValue(undefined);
    render(<About capabilities={CAPABILITIES} />);

    const button = await screen.findByRole("button", {
      name: "Request a feature on GitHub, opens in your browser",
    });
    expect(button).toHaveTextContent("Request a feature");
    expect(button.querySelector("svg")).toHaveAttribute("aria-hidden", "true");

    fireEvent.click(button);
    await waitFor(() => expect(mocks.open).toHaveBeenCalledWith(
      "https://github.com/lanreal-io/whisperstream/issues/new?template=feature_request.yml",
    ));

  });

  it("uses GitHub help and offers no update action without the official updater", async () => {
    render(<About capabilities={CAPABILITIES} />);
    expect(await screen.findByRole("button", { name: "Report a bug on GitHub, opens in your browser" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Read documentation, opens in your browser" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /check for updates/i })).toBeNull();
    expect(screen.queryByText("Updates unavailable")).toBeNull();
    expect(screen.queryByText(/All features are free|Rebuild from newer source/)).not.toBeInTheDocument();
    expect(mocks.open).not.toHaveBeenCalled();
  });

});

it("does not claim a cancelled log download succeeded", async () => {
  mocks.exportLogs.mockResolvedValueOnce("cancelled");
  render(<About capabilities={CAPABILITIES} />);
  fireEvent.click(screen.getByRole("button", {name: "Export logs"}));
  await waitFor(() => expect(screen.getByRole("button", {name: "Export logs"})).not.toBeDisabled());
  expect(screen.queryByText("Downloaded")).not.toBeInTheDocument();
});

it("downloads both legal documents from bundled assets", async () => {
  const fetchDocument = vi.fn().mockResolvedValue({ok:true, blob:async () => new Blob(["%PDF-1.4"])});
  vi.stubGlobal("fetch", fetchDocument);
  render(<About capabilities={CAPABILITIES} />);
  fireEvent.click(screen.getByRole("button", {name:"Download Privacy policy (PDF)"}));
  await waitFor(() => expect(mocks.saveBlob).toHaveBeenCalledWith(expect.any(Blob), "Whisperstream-Privacy.pdf", "pdf"));
  fireEvent.click(screen.getByRole("button", {name:"Download License agreement (PDF)"}));
  await waitFor(() => expect(mocks.saveBlob).toHaveBeenCalledWith(expect.any(Blob), "Whisperstream-License.pdf", "pdf"));
  expect(fetchDocument.mock.calls.every(([url]) => !/^https?:/.test(url) && url.includes(".pdf"))).toBe(true);
  expect(mocks.open).not.toHaveBeenCalled();
});

describe("About log privacy copy", () => {
  it("shows no log sharing caption or mode qualifier", async () => {
    render(<About capabilities={CAPABILITIES} />);

    expect(screen.queryByText(/Review logs before sharing/)).not.toBeInTheDocument();
    await screen.findByRole("button", {name:"Report a bug on GitHub, opens in your browser"});
    expect(screen.queryByText(/In Lockdown/i)).not.toBeInTheDocument();

  });
});


it("does not open a browser after Lockdown becomes active", async () => {
  render(<About capabilities={CAPABILITIES} />);
  const button = await screen.findByRole("button", {name:"Report a bug on GitHub, opens in your browser"});
  mocks.lockdown.mockResolvedValue({enabled:true,managed:true});
  fireEvent.click(button);
  await waitFor(() => expect(mocks.lockdown).toHaveBeenCalledTimes(2));
  expect(mocks.open).not.toHaveBeenCalled();
});

it("reacts to current Lockdown state without showing external actions", async () => {
  const view = render(<About lockdownEnabled={false} />);
  await screen.findByRole("button", {name:"Report a bug on GitHub, opens in your browser"});
  view.rerender(<About lockdownEnabled={true} />);
  expect(screen.queryByRole("button", {name:"Report a bug on GitHub, opens in your browser"})).not.toBeInTheDocument();
  expect(screen.getByText("External links are unavailable in Lockdown Mode.")).toBeInTheDocument();
});


it.each([
  ["Request a feature on GitHub, opens in your browser", "https://github.com/lanreal-io/whisperstream/issues/new?template=feature_request.yml"],
  ["Website, opens in your browser", "https://whisperstream.io"],
  ["Changelog, opens in your browser", "https://whisperstream.io/changelog"],
])("opens only the fixed %s destination after a click", async (name, destination) => {
  render(<About capabilities={CAPABILITIES} />);
  const button = await screen.findByRole("button", {name});
  expect(mocks.open).not.toHaveBeenCalled();
  fireEvent.click(button);
  await waitFor(() => expect(mocks.open).toHaveBeenCalledWith(destination));
});

it.each([{enabled: false}, {enabled: false, managed: true}, {enabled: true, managed: false}])(
  "hides external actions when policy is blocked or incomplete: %j", async (status) => {
    mocks.lockdown.mockResolvedValue(status);
    render(<About capabilities={CAPABILITIES} />);
    await waitFor(() => expect(mocks.lockdown).toHaveBeenCalled());
    expect(screen.queryByRole("button", {name: "Report a bug on GitHub, opens in your browser"})).not.toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /Request a feature/})).not.toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Export logs"})).toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Save documentation"})).toBeInTheDocument();
});

it("shows a retryable failure when opening a link fails", async () => {
  mocks.open.mockRejectedValueOnce(new Error("shell unavailable"));
  render(<About capabilities={CAPABILITIES} />);
  fireEvent.click(await screen.findByRole("button", {name: "Request a feature on GitHub, opens in your browser"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not open the link");
  fireEvent.click(screen.getByRole("button", {name: "Request a feature on GitHub, opens in your browser"}));
  await waitFor(() => expect(mocks.open).toHaveBeenCalledTimes(2));
});

it("refuses a handoff when the current policy cannot be read", async () => {
  render(<About capabilities={CAPABILITIES} />);
  const button = await screen.findByRole("button", {name: "Report a bug on GitHub, opens in your browser"});
  mocks.lockdown.mockRejectedValueOnce(new Error("offline"));
  fireEvent.click(button);
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(mocks.open).not.toHaveBeenCalled();
});

it("shows real log export failures without reporting success", async () => {
  mocks.exportLogs.mockRejectedValueOnce(new Error("disk full"));
  render(<About capabilities={CAPABILITIES} />);
  fireEvent.click(screen.getByRole("button", {name: "Export logs"}));
  expect(await screen.findByRole("button", {name: "Couldn't export"})).toBeInTheDocument();
  expect(screen.queryByText("Downloaded")).not.toBeInTheDocument();
});


it.each([
  ["Report a bug on GitHub, opens in your browser", "https://github.com/lanreal-io/whisperstream/issues/new?template=bug_report.yml"],
  ["Read documentation, opens in your browser", "https://whisperstream.io/docs"],
  ["Source code, opens in your browser", "https://github.com/lanreal-io/whisperstream"],
])("opens only the fixed destination for %s", async (name,url) => {
  render(<About />);
  fireEvent.click(await screen.findByRole("button",{name}));
  await waitFor(()=>expect(mocks.open).toHaveBeenCalledWith(url));
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});


it.each([{enabled: true, managed: false}, {enabled: false, managed: true}, {enabled: false}])(
  "refuses a feature request when fresh policy is blocked or incomplete: %j", async (status) => {
    render(<About capabilities={CAPABILITIES} />);
    const button = await screen.findByRole("button", {name: /Request a feature on GitHub/});
    mocks.lockdown.mockResolvedValueOnce(status);
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("External links are unavailable");
    expect(mocks.open).not.toHaveBeenCalled();
});

it("refuses a feature request when fresh policy cannot be read", async () => {
  render(<About capabilities={CAPABILITIES} />);
  const button = await screen.findByRole("button", {name: /Request a feature on GitHub/});
  mocks.lockdown.mockRejectedValueOnce(new Error("offline"));
  fireEvent.click(button);
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(mocks.open).not.toHaveBeenCalled();
});
