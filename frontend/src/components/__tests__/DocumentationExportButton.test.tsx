import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DocumentationExportButton } from "../DocumentationExportButton";
const mocks = vi.hoisted(() => ({save:vi.fn(),reveal:vi.fn()}));
vi.mock("../../lib/offlineDocumentation", () => ({saveDocumentation:mocks.save,revealDocumentation:mocks.reveal}));
afterEach(() => {cleanup();vi.resetAllMocks();});
it("returns silently from cancellation and restores keyboard focus",async () => {
  mocks.save.mockResolvedValue(null);
  render(<DocumentationExportButton />);
  fireEvent.click(screen.getByRole("button",{name:"Save documentation"}));
  await waitFor(() => expect(screen.getByRole("button",{name:"Save documentation"})).toHaveFocus());
  expect(screen.queryByText("Documentation saved")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});
it("shows success only after all files save and opens the saved folder",async () => {
  let finish: (value:string)=>void = () => {};
  mocks.save.mockReturnValue(new Promise<string>(resolve => {finish=resolve;}));
  render(<DocumentationExportButton />);
  fireEvent.click(screen.getByRole("button",{name:"Save documentation"}));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledOnce());
  expect(screen.getByRole("button",{name:"Saving documentation..."})).toBeDisabled();
  await act(async () => {finish("C:/chosen/Whisperstream documentation");});
  fireEvent.click(screen.getByRole("button",{name:"Open folder"}));
  await waitFor(() => expect(mocks.reveal).toHaveBeenCalledWith("C:/chosen/Whisperstream documentation"));
});
it("offers retry after failure without adding a helper row",async () => {
  mocks.save.mockRejectedValue(new Error("disk full"));
  render(<DocumentationExportButton />);
  fireEvent.click(screen.getByRole("button",{name:"Save documentation"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't save. Try again");
  expect(screen.queryByText("Documentation saved")).toBeNull();
});
