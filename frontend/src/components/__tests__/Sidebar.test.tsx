import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { Sidebar, type TabId } from "../Sidebar";

vi.mock("framer-motion", () => {
  function motionProps<T extends HTMLElement>(
    props: React.HTMLAttributes<T>,
  ) {
    const {
      initial: _initial,
      animate: _animate,
      exit: _exit,
      transition: _transition,
      whileHover: _whileHover,
      whileTap: _whileTap,
      layoutId: _layoutId,
      ...domProps
    } = props as React.HTMLAttributes<T> & {
      initial?: unknown;
      animate?: unknown;
      exit?: unknown;
      transition?: unknown;
      whileHover?: unknown;
      whileTap?: unknown;
      layoutId?: unknown;
    };
    return domProps;
  }

  return {
    motion: {
      div: (props: React.HTMLAttributes<HTMLDivElement>) => (
        <div data-motion-element="true" {...motionProps(props)} />
      ),
      p: (props: React.HTMLAttributes<HTMLParagraphElement>) => (
        <p data-motion-element="true" {...motionProps(props)} />
      ),
      button: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) => (
        <button data-motion-element="true" {...motionProps(props)} />
      ),
    },
    AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

afterEach(cleanup);

function SidebarHarness() {
  const [activeTab, setActiveTab] = useState<TabId>("home");
  return (
    <Sidebar
      activeTab={activeTab}
      onTabChange={setActiveTab}
      status={null}
    />
  );
}

describe("Sidebar active surface", () => {
  it("renders directly inside the newly selected tab without a shared-layout transition", () => {
    render(<SidebarHarness />);

    expect(screen.getByTestId("active-tab-surface").parentElement).toBe(
      screen.getByRole("button", { name: "Home" }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Controls" }));

    const surface = screen.getByTestId("active-tab-surface");
    expect(surface.parentElement).toBe(screen.getByRole("button", { name: "Controls" }));
    expect(surface).not.toHaveAttribute("data-motion-element");
  });
});

it("keeps Fixtures out of desktop navigation even with dev tools enabled", () => {
  render(<Sidebar activeTab="home" onTabChange={() => {}} status={null} devEnabled />);
  expect(screen.queryByRole("button", { name: /fixture/i })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /UI Test/ })).toBeInTheDocument();
});

it("shows no edition, Pro or trial affordance", () => {
  render(<Sidebar activeTab="home" onTabChange={vi.fn()} status={null} />);
  expect(screen.queryByText("All features included")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", {name:"Pro"})).not.toBeInTheDocument();
  expect(screen.queryByText(/trial|activate/i)).not.toBeInTheDocument();
});

it("marks a tab that needs attention with its reason", () => {
  const { rerender } = render(<Sidebar activeTab="home" onTabChange={vi.fn()} status={null} attention={{ transcripts: "Recovery setup needed" }} />);
  const transcripts = screen.getByRole("button", { name: /Transcripts/ });
  expect(transcripts).toHaveAccessibleDescription("Recovery setup needed");
  rerender(<Sidebar activeTab="home" onTabChange={vi.fn()} status={null} />);
  expect(screen.getByRole("button", { name: /Transcripts/ })).not.toHaveAttribute("aria-describedby");
});
