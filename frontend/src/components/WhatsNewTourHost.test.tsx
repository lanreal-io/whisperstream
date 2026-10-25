import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { WhatsNewTourHost } from "./WhatsNewTourHost";
import { WHATS_NEW_ID, launchOverlayGates } from "../lib/whatsNew";
import type { AppConfig } from "../lib/api/config";

vi.mock("framer-motion", async () => {
  const React = await import("react");
  const motionProps = new Set(["animate", "exit", "initial", "transition", "layout", "whileHover", "whileTap"]);
  const motionComponents = new Map<string, unknown>();
  const motion = new Proxy(
    {},
    {
      get: (_t, tag: string) => {
        let C = motionComponents.get(tag);
        if (!C) {
          C = React.forwardRef<unknown, Record<string, unknown>>(({ children, ...props }, ref) => {
            for (const key of motionProps) delete props[key];
            return React.createElement(tag, { ...props, ref }, children as ReactNode);
          });
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

afterEach(() => {
  cleanup();
  window.history.replaceState({}, "", "/");
});

const cfg = (dismissed: string[] = []) => ({ dismissed_offer_ids: dismissed }) as unknown as AppConfig;

function renderHost(props: Partial<Parameters<typeof WhatsNewTourHost>[0]> = {}) {
  const onResolveOffer = vi.fn();
  const onStartDictating = vi.fn();
  const onShow = vi.fn();
  const utils = render(
    <WhatsNewTourHost
      config={cfg()}
      gatesClear
      onResolveOffer={onResolveOffer}
      onStartDictating={onStartDictating}
      onShow={onShow}
      {...props}
    />,
  );
  return { ...utils, onResolveOffer, onStartDictating, onShow };
}

function finishTour() {
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  fireEvent.click(screen.getByRole("button", { name: "Start dictating" }));
}

describe("WhatsNewTourHost", () => {
  it("shows the tour when the ID is unresolved and gates are clear", () => {
    const { onShow } = renderHost();
    expect(screen.getByRole("dialog", { name: "What's new in Whisperstream 2.0" })).toBeInTheDocument();
    expect(onShow).toHaveBeenCalled();
  });

  it("stays hidden once the ID is resolved", () => {
    const { onShow } = renderHost({ config: cfg(["gemini-3-5-flash", WHATS_NEW_ID]) });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(onShow).not.toHaveBeenCalled();
  });

  it("waits while gates are closed, then shows", () => {
    const { rerender, onResolveOffer, onStartDictating, onShow } = renderHost({ gatesClear: false });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    rerender(
      <WhatsNewTourHost config={cfg()} gatesClear onResolveOffer={onResolveOffer} onStartDictating={onStartDictating} onShow={onShow} />,
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("completion resolves the ID once, starts dictating and closes", () => {
    const { onResolveOffer, onStartDictating } = renderHost();
    finishTour();
    expect(onResolveOffer).toHaveBeenCalledTimes(1);
    expect(onResolveOffer).toHaveBeenCalledWith(WHATS_NEW_ID);
    expect(onStartDictating).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("leaves the ID unresolved when the tour is interrupted mid-way", () => {
    const { unmount, onResolveOffer } = renderHost();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    unmount();
    expect(onResolveOffer).not.toHaveBeenCalled();
  });

  it("dev preview shows regardless of gates and never persists", () => {
    window.history.replaceState({}, "", `/?forceSpotlight=${WHATS_NEW_ID}`);
    const { onResolveOffer } = renderHost({ config: cfg([WHATS_NEW_ID]), gatesClear: false });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    finishTour();
    expect(onResolveOffer).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("ignores other forced spotlight IDs", () => {
    window.history.replaceState({}, "", "/?forceSpotlight=gemini-3-5-flash");
    renderHost({ config: cfg([WHATS_NEW_ID]) });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("launchOverlayGates", () => {
  const base = { overlaysClear: true, lockdown: false, dismissedOfferIds: [] as string[], tourShownThisSession: false };

  it("shows the tour under Lockdown but never spotlight offers", () => {
    expect(launchOverlayGates({ ...base, lockdown: true })).toEqual({ tour: true, spotlight: false });
    expect(launchOverlayGates({ ...base, lockdown: true, dismissedOfferIds: [WHATS_NEW_ID] }).spotlight).toBe(false);
  });

  it("holds spotlight offers until the tour ID is resolved", () => {
    expect(launchOverlayGates(base)).toEqual({ tour: true, spotlight: false });
    expect(launchOverlayGates({ ...base, dismissedOfferIds: [WHATS_NEW_ID] })).toEqual({ tour: true, spotlight: true });
  });

  it("defers spotlight offers to the next launch after the tour showed this session", () => {
    expect(
      launchOverlayGates({ ...base, dismissedOfferIds: [WHATS_NEW_ID], tourShownThisSession: true }).spotlight,
    ).toBe(false);
  });

  it("closes both while other overlays or dictation hold the gate", () => {
    expect(launchOverlayGates({ ...base, overlaysClear: false })).toEqual({ tour: false, spotlight: false });
  });
});
