import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { FeatureSpotlight } from "./FeatureSpotlight";
import type { SpotlightOffer } from "../lib/spotlightOffers";

vi.mock("framer-motion", async () => {
  const React = await import("react");
  const motionProps = new Set(["animate", "exit", "initial", "transition", "whileHover", "whileTap"]);
  const motionComponents = new Map<string, ({ children, ...props }: Record<string, unknown>) => ReactNode>();
  const motion = new Proxy(
    {},
    {
      get: (_t, tag: string) => {
        let C = motionComponents.get(tag);
        if (!C) {
          C = ({ children, ...props }: Record<string, unknown>) => {
            for (const key of motionProps) delete props[key];
            return React.createElement(tag, props, children as ReactNode);
          };
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

function makeOffer(overrides: Partial<SpotlightOffer> = {}): SpotlightOffer {
  return {
    id: "test-offer",
    eyebrow: "NOW AVAILABLE",
    headline: "Test headline here",
    body: "Test body copy for the spotlight.",
    hero: <div data-testid="hero" />,
    accent: "frost",
    cta: { kind: "deep-link", label: "Do the thing", run: () => {} },
    targeting: () => true,
    priority: 10,
    ...overrides,
  };
}

describe("FeatureSpotlight", () => {
  it("renders the offer's eyebrow, headline, body, hero, and CTA label", () => {
    render(<FeatureSpotlight offer={makeOffer()} onAct={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.getByText("NOW AVAILABLE")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Test headline here" })).toBeTruthy();
    expect(screen.getByText("Test body copy for the spotlight.")).toBeTruthy();
    expect(screen.getByTestId("hero")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Do the thing" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Maybe later" })).toBeTruthy();
  });

  it("calls onAct with the offer when the primary CTA is clicked", () => {
    const onAct = vi.fn();
    const offer = makeOffer();
    render(<FeatureSpotlight offer={offer} onAct={onAct} onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Do the thing" }));
    expect(onAct).toHaveBeenCalledWith(offer);
  });

  it("calls onDismiss on Maybe later, backdrop click, and Escape", () => {
    const onDismiss = vi.fn();
    const offer = makeOffer();
    const { container } = render(<FeatureSpotlight offer={offer} onAct={vi.fn()} onDismiss={onDismiss} />);

    fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
    expect(onDismiss).toHaveBeenCalledWith(offer);

    // Backdrop is the first absolute-inset-0 element inside the fixed wrapper.
    const backdrop = container.querySelector(".fixed.inset-0 > .absolute.inset-0") as HTMLElement;
    fireEvent.click(backdrop);
    expect(onDismiss).toHaveBeenCalledTimes(2);

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(onDismiss).toHaveBeenCalledTimes(3);
  });

  it("mount-focuses the safe 'Maybe later' button", () => {
    render(<FeatureSpotlight offer={makeOffer()} onAct={vi.fn()} onDismiss={vi.fn()} />);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Maybe later" }));
  });

  it("traps Tab and restores prior focus", () => {
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    trigger.focus();
    const { unmount } = render(<FeatureSpotlight offer={makeOffer()} onAct={vi.fn()} onDismiss={vi.fn()} />);
    const safe = screen.getByRole("button", { name: "Maybe later" });
    const primary = screen.getByRole("button", { name: "Do the thing" });
    expect(document.activeElement).toBe(safe);
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(primary);
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(safe);
    unmount();
    expect(document.activeElement).toBe(trigger);
    trigger.remove();
  });

  it("swaps the body for benchmarkSlot and hides the offer content", () => {
    render(
      <FeatureSpotlight
        offer={makeOffer()}
        onAct={vi.fn()}
        onDismiss={vi.fn()}
        benchmarkSlot={<div data-testid="bench-panel">bench</div>}
      />,
    );
    expect(screen.getByTestId("bench-panel")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Test headline here" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Maybe later" })).toBeNull();
  });
});

describe("required offers", () => {
  it("has no Maybe later and ignores Esc and the backdrop", () => {
    const onDismiss = vi.fn();
    const onAct = vi.fn();
    const { container } = render(
      <FeatureSpotlight
        offer={makeOffer({ required: true, note: "Saving continues while you finish this." })}
        onAct={onAct}
        onDismiss={onDismiss}
      />,
    );
    expect(screen.queryByRole("button", { name: "Maybe later" })).toBeNull();
    expect(screen.getByText("Saving continues while you finish this.")).toBeInTheDocument();
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveFocus();
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.click(container.querySelector(".absolute.inset-0")!);
    expect(onDismiss).not.toHaveBeenCalled();
    fireEvent.click(buttons[0]);
    expect(onAct).toHaveBeenCalledOnce();
  });
});

