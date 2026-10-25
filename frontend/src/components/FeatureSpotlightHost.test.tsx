import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeatureSpotlightHost, type FeatureSpotlightHostProps } from "./FeatureSpotlightHost";
import type { AppConfig } from "../lib/api/config";

afterEach(() => { cleanup(); window.history.replaceState({}, "", "/"); });
function props(overrides: Partial<FeatureSpotlightHostProps> = {}): FeatureSpotlightHostProps {
  return {
    config: { enhancement_provider: "gemini", enhancement_gemini_model: "retired-model", dismissed_offer_ids: [] } as unknown as AppConfig,
    lockdownEnabled: false, hardwareCapable: true,
    nav: { goToTab: vi.fn(), switchToGeminiFlash: vi.fn() }, onResolveOffer: vi.fn(), gatesClear: true,
    ...overrides,
  };
}

describe("source Spotlight lifecycle", () => {
  it("waits for idle gates, pauses an open offer, and resumes without resolving it", () => {
    const p = props();
    const { rerender } = render(<FeatureSpotlightHost {...p} gatesClear={false} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    rerender(<FeatureSpotlightHost {...p} />);
    expect(screen.getByRole("dialog", { name: "Gemini 3.5 Flash is here" })).toBeTruthy();
    rerender(<FeatureSpotlightHost {...p} gatesClear={false} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(p.onResolveOffer).not.toHaveBeenCalled();
    rerender(<FeatureSpotlightHost {...p} />);
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("retries eligibility after configuration hydration without consuming the launch", () => {
    const p = props();
    const { rerender } = render(<FeatureSpotlightHost {...p} config={{ ...p.config, enhancement_provider: "local" }} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    rerender(<FeatureSpotlightHost {...p} />);
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("resolves once and respects the persisted dismissal on a later launch", () => {
    const p = props();
    const { rerender, unmount } = render(<FeatureSpotlightHost {...p} />);
    fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
    expect(p.onResolveOffer).toHaveBeenCalledExactlyOnceWith("gemini-3-5-flash");
    rerender(<FeatureSpotlightHost {...p} gatesClear={false} />);
    rerender(<FeatureSpotlightHost {...p} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    unmount();
    render(<FeatureSpotlightHost {...p} config={{ ...p.config, dismissed_offer_ids: ["gemini-3-5-flash"] }} />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("uses the existing consent-safe navigation action and resolves the offer", () => {
    const p = props();
    render(<FeatureSpotlightHost {...p} />);
    fireEvent.click(screen.getByRole("button", { name: "Switch to 3.5 Flash" }));
    expect(p.nav.switchToGeminiFlash).toHaveBeenCalledOnce();
    expect(p.onResolveOffer).toHaveBeenCalledExactlyOnceWith("gemini-3-5-flash");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps forced development previews independent of gates and persistence", () => {
    window.history.replaceState({}, "", "/?forceSpotlight=gemini-3-5-flash");
    const p = props({ gatesClear: false, lockdownEnabled: true });
    render(<FeatureSpotlightHost {...p} />);
    fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(p.onResolveOffer).not.toHaveBeenCalled();
  });

  it("shows the required recovery prompt once, recording it when shown", () => {
    const openHistoryRecoverySetup = vi.fn();
    const p = props({
      config: { enhancement_provider: "local", dismissed_offer_ids: [] } as unknown as AppConfig,
      nav: { goToTab: vi.fn(), switchToGeminiFlash: vi.fn(), openHistoryRecoverySetup },
      historyRecoveryNeedsSetup: true,
      platform: "windows",
    });
    render(<FeatureSpotlightHost {...p} />);
    expect(screen.getByRole("dialog", { name: "Add a recovery key for your transcripts" })).toBeTruthy();
    expect(screen.getByText(/saved on this PC with no backup/)).toBeTruthy();
    expect(p.onResolveOffer).toHaveBeenCalledWith("history-recovery-setup");
    fireEvent.click(screen.getByRole("button", { name: "Set up recovery key" }));
    expect(openHistoryRecoverySetup).toHaveBeenCalledOnce();
  });

  it("never shows the recovery prompt under Lockdown", () => {
    const p = props({
      config: { enhancement_provider: "local", dismissed_offer_ids: [] } as unknown as AppConfig,
      lockdownEnabled: true,
      historyRecoveryNeedsSetup: true,
    });
    render(<FeatureSpotlightHost {...p} />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
