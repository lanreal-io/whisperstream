import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { CloudModelPicker } from "./CloudModelPicker";

afterEach(cleanup);

function Harness() {
  const [value, setValue] = useState("legacy-custom");
  const [saved, setSaved] = useState<string[]>([]);
  return <CloudModelPicker value={value} saved={saved} catalog={[{ value: "bundled", label: "Bundled" }]} onChange={(id, history) => { setValue(id); setSaved(history); }} />;
}

it("retains old and new models while switching, and deletes active or inactive items", () => {
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Add custom model" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: " new-custom " } });
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  fireEvent.click(screen.getByRole("button", { name: "Model: new-custom" }));
  expect(screen.getByRole("option", { name: "legacy-custom" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("option", { name: "Bundled" }));
  fireEvent.click(screen.getByRole("button", { name: "Model: Bundled" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete saved model legacy-custom" }));
  expect(screen.queryByRole("option", { name: "legacy-custom" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("option", { name: "new-custom" }));
  fireEvent.click(screen.getByRole("button", { name: "Model: new-custom" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete saved model new-custom" }));
  expect(screen.getByRole("button", { name: "Model: Choose a model" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "new-custom" })).not.toBeInTheDocument();
});

it("cancels partial entries and exposes delete actions to keyboard focus", () => {
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Add custom model" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "unfinished" } });
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Escape" });
  fireEvent.click(screen.getByRole("button", { name: "Model: legacy-custom" }));
  const row = screen.getByRole("option", { name: "legacy-custom" });
  fireEvent.keyDown(row, { key: "Tab" });
  expect(screen.getByRole("listbox")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Delete saved model legacy-custom" })).not.toHaveAttribute("tabindex", "-1");
  expect(screen.queryByRole("option", { name: "unfinished" })).not.toBeInTheDocument();
});
