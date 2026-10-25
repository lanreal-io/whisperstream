import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { WhatsNewTour } from "./WhatsNewTour";
import { WHATS_NEW_ID, WHATS_NEW_STEPS } from "../lib/whatsNew";

vi.mock("framer-motion", async () => {
  const React = await import("react");
  const motionProps = new Set(["animate", "exit", "initial", "transition", "layout", "whileHover", "whileTap"]);
  const motionComponents = new Map<string, ({ children, ...props }: Record<string, unknown>) => ReactNode>();
  const motion = new Proxy(
    {},
    {
      get: (_t, tag: string) => {
        let C = motionComponents.get(tag);
        if (!C) {
          C = React.forwardRef<unknown, Record<string, unknown>>(({ children, ...props }, ref) => {
            for (const key of motionProps) delete props[key];
            return React.createElement(tag, { ...props, ref }, children as ReactNode);
          }) as unknown as ({ children, ...props }: Record<string, unknown>) => ReactNode;
          motionComponents.set(tag, C);
        }
        return C;
      },
    },
  );
  return {
    motion,
    AnimatePresence: ({ children }: { children: ReactNode }) => <>{children}</>,
    useReducedMotion: () => false,
  };
});

afterEach(cleanup);

const headline = () => screen.getByRole("heading", { level: 2 }).textContent;

describe("WhatsNewTour", () => {
  it("exports the stable persistence ID and the three locked steps", () => {
    expect(WHATS_NEW_ID).toBe("whats-new-2-0");
    expect(WHATS_NEW_STEPS.map((s) => s.headline)).toEqual([
      "Try Cohere Transcribe",
      "Boost names and terms",
      "Meet Stream-1 Mini",
    ]);
    for (const s of WHATS_NEW_STEPS) {
      for (const text of [s.eyebrow, s.headline, s.body, s.hint]) expect(text).not.toContain("—");
    }
  });

  it("renders step 1 as a labelled dialog with no Back and no close control", () => {
    render(<WhatsNewTour onComplete={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "What's new in Whisperstream 2.0" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(headline()).toBe("Try Cohere Transcribe");
    expect(screen.getByText("New speech model")).toBeInTheDocument();
    expect(screen.getByText("Change your Voice Recognition Model on Home.")).toBeInTheDocument();
    expect(screen.getByText("Step 1 of 3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Back" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Next"]);
  });

  it("focuses the primary button on mount and after each step change", () => {
    render(<WhatsNewTour onComplete={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Next" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("button", { name: "Next" })).toHaveFocus();
  });

  it("moves forward and back through the steps", () => {
    render(<WhatsNewTour onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(headline()).toBe("Boost names and terms");
    expect(screen.getByText("Requires AI enhancement.")).toBeInTheDocument();
    expect(screen.getByText("Step 2 of 3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(headline()).toBe("Try Cohere Transcribe");
    expect(screen.queryByRole("button", { name: "Back" })).not.toBeInTheDocument();
  });

  it("announces the step politely on change but not on mount", () => {
    render(<WhatsNewTour onComplete={vi.fn()} />);
    const live = document.querySelector('[aria-live="polite"]');
    expect(live).not.toBeNull();
    expect(live?.textContent).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(live?.textContent).toBe("Step 2 of 3: Dictionary Boost");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(live?.textContent).toBe("Step 3 of 3: Stream-1 Mini");
  });

  it("final step shows Start dictating, which calls onComplete", () => {
    const onComplete = vi.fn();
    render(<WhatsNewTour onComplete={onComplete} initialStep={3} />);
    expect(headline()).toBe("Meet Stream-1 Mini");
    expect(screen.getByText("Select the Local provider in Enhancement.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Next" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start dictating" }));
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it("only completes from the final step", () => {
    const onComplete = vi.fn();
    render(<WhatsNewTour onComplete={onComplete} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(onComplete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Start dictating" }));
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it("Escape and backdrop clicks do nothing", () => {
    const onComplete = vi.fn();
    const { container } = render(<WhatsNewTour onComplete={onComplete} initialStep={2} />);
    const esc = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    act(() => {
      window.dispatchEvent(esc);
    });
    expect(esc.defaultPrevented).toBe(true);
    fireEvent.click(container.querySelector("[data-whats-new-backdrop]")!);
    expect(onComplete).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(headline()).toBe("Boost names and terms");
  });

  it("traps Tab focus over the visible buttons", () => {
    render(<WhatsNewTour onComplete={vi.fn()} initialStep={2} />);
    const back = screen.getByRole("button", { name: "Back" });
    const next = screen.getByRole("button", { name: "Next" });
    expect(next).toHaveFocus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(back).toHaveFocus();
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(next).toHaveFocus();
  });

  it("clamps an out-of-range initialStep", () => {
    render(<WhatsNewTour onComplete={vi.fn()} initialStep={9} />);
    expect(headline()).toBe("Meet Stream-1 Mini");
  });
});
