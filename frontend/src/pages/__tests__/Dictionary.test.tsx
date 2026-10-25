import { useState, type ReactNode } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Dictionary } from "../Dictionary";
import { makeConfig } from "./homeConfigFixture";
import type { AppConfig } from "../../lib/api/config";

vi.mock("framer-motion", async () => {
  const React = await import("react");
  const cache = new Map<string, (props: Record<string, unknown>) => ReactNode>();
  return {
    useReducedMotion: () => true,
    AnimatePresence: ({ children }: { children: ReactNode }) => <>{children}</>,
    motion: new Proxy({}, { get: (_target, tag: string) => {
      if (!cache.has(tag)) cache.set(tag, ({ children, ...props }) => {
        for (const key of ["animate", "exit", "initial", "transition", "layout", "whileHover", "whileTap"]) delete props[key];
        return React.createElement(tag, props, children as ReactNode);
      });
      return cache.get(tag);
    } }),
  };
});
afterEach(cleanup);
function DictionaryFixture({ overrides = {}, saved = vi.fn() }: { overrides?: Partial<AppConfig>; saved?: (patch: Partial<AppConfig>) => void }) {
  const [config, setConfig] = useState(makeConfig({ setup_completed: true, enhancement_enabled: true, enhancement_vocabulary_supported: true, ...overrides }));
  return <Dictionary config={config} onConfigChange={patch => { saved(patch); setConfig(previous => ({ ...previous, ...patch })); }} onImportConfig={async build => { const patch = build(config); saved(patch); setConfig(previous => ({ ...previous, ...patch })); }} />;
}
describe("Dictionary Boost", () => {
  it("saves and edits exact-case names while deduplicating the same type", () => {
    const saved = vi.fn();
    render(<DictionaryFixture saved={saved} />);
    fireEvent.change(screen.getByLabelText("Dictionary entry word"), { target: { value: "TanStack" } });
    fireEvent.click(screen.getByRole("button", { name: "Boost" }));
    expect(saved).toHaveBeenLastCalledWith({ word_boosts: ["TanStack"] });
    fireEvent.change(screen.getByLabelText("Dictionary entry word"), { target: { value: "tanstack" } });
    fireEvent.click(screen.getByRole("button", { name: "Boost" }));
    expect(saved).toHaveBeenCalledTimes(1);
    const row = screen.getByLabelText('Boost "TanStack"');
    fireEvent.click(within(row).getByRole("button", { name: "Edit entry" }));
    fireEvent.change(screen.getByLabelText("Edit word"), { target: { value: "TanStack Query" } });
    fireEvent.keyDown(screen.getByLabelText("Edit word"), { key: "Enter" });
    expect(saved).toHaveBeenLastCalledWith({ word_boosts: ["TanStack Query"] });
  });
  it("preserves Enter-to-Filter and permits a Boost with the same source as another action", () => {
    const saved = vi.fn();
    render(<DictionaryFixture overrides={{ word_replacements: { claude: "Claude" } }} saved={saved} />);
    fireEvent.change(screen.getByLabelText("Dictionary entry word"), { target: { value: "Claude" } });
    fireEvent.click(screen.getByRole("button", { name: "Boost" }));
    expect(saved).toHaveBeenLastCalledWith({ word_boosts: ["Claude"] });
    fireEvent.change(screen.getByLabelText("Dictionary entry word"), { target: { value: "um" } });
    fireEvent.keyDown(screen.getByLabelText("Dictionary entry word"), { key: "Enter" });
    expect(saved).toHaveBeenLastCalledWith({ word_filters: ["um"] });
  });
  it("explains inactive Boost only once a Boost word is saved", () => {
    const { unmount } = render(<DictionaryFixture overrides={{ enhancement_enabled: false, word_boosts: [] }} />);
    expect(screen.queryByText("Boost is inactive while AI enhancement is off. You can still save words.")).not.toBeInTheDocument();
    unmount();
    render(<DictionaryFixture overrides={{ enhancement_enabled: false, word_boosts: ["Vercel"] }} />);
    expect(screen.getByText("Boost is inactive while AI enhancement is off. You can still save words.")).toBeInTheDocument();
  });
  it("allows saved-word management and import when enhancement is inactive", () => {
    render(<DictionaryFixture overrides={{ enhancement_enabled: false }} />);
    expect(screen.getByRole("button", { name: "Import CSV" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Dictionary entry word"), { target: { value: "Vercel" } });
    expect(screen.getByRole("button", { name: "Boost" })).toBeEnabled();
  });
  // This multi-step DOM scenario is not a five-second performance benchmark.
  it("loads a large dictionary incrementally and keeps newly added Boost words visible", () => {
    render(<DictionaryFixture overrides={{ word_filters: Array.from({ length: 225 }, (_, index) => `filter${index}`) }} />);
    expect(screen.getByText("Showing 100 of 225 entries.")).toBeInTheDocument();
    expect(screen.queryByText("filter100")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(screen.getByText("Showing 200 of 225 entries.")).toBeInTheDocument();
    expect(screen.getByText("filter199")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Dictionary entry word"), { target: { value: "TanStack" } });
    fireEvent.click(screen.getByRole("button", { name: "Boost" }));
    expect(screen.getByText("TanStack")).toBeInTheDocument();
    expect(screen.getByText("Showing 200 of 226 entries.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(screen.getByText("Showing 226 of 226 entries.")).toBeInTheDocument();
    expect(screen.getByText("filter224")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  }, 15_000);
});
