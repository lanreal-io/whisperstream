import { describe, it, expect } from "vitest";
import { OFFERS, pickOffer } from "./spotlightOffers";
import type { SpotlightContext, HardwareSignal } from "./spotlightOffers";
import type { AppConfig } from "./api/config";

const gemini = OFFERS.find((o) => o.id === "gemini-3-5-flash")!;
const cohere = OFFERS.find((o) => o.id === "cohere-transcribe")!;

function ctx(
  configOverrides: Partial<AppConfig> = {},
  hardware: HardwareSignal = { capable: true },
  lockdown = false,
): SpotlightContext {
  return {
    config: {
      enhancement_provider: "gemini",
      // A now-retired model, so the default context represents a user "on an
      // older Gemini model" who should see the switch-to-3.5-Flash offer.
      enhancement_gemini_model: "gemini-2.5-flash-lite",
      parakeet_model: "parakeet-tdt-v3",
      ...configOverrides,
    } as AppConfig,
    hardware,
    lockdown,
  };
}

describe("Gemini offer targeting", () => {
  it("is shown to a user on a retired Gemini model", () => {
    expect(gemini.targeting(ctx())).toBe(true);
  });
  it("is hidden for every current-lineup Gemini model", () => {
    // The new free-tier default must never self-nag on first launch, and no
    // other current model should either.
    for (const model of [
      "gemini-3.5-flash-lite",
      "gemini-3.1-flash-lite",
      "gemini-3.7-flash",
      "gemini-3.5-flash",
      "gemini-3.1-pro-preview",
    ]) {
      expect(gemini.targeting(ctx({ enhancement_gemini_model: model }))).toBe(false);
    }
  });
  it("is hidden when on another provider", () => {
    expect(gemini.targeting(ctx({ enhancement_provider: "openai" }))).toBe(false);
  });
  it("is hidden under lockdown", () => {
    expect(gemini.targeting(ctx({}, { capable: true }, true))).toBe(false);
  });
});

describe("Cohere offer targeting", () => {
  it("is shown when hardware is capable and not under lockdown", () => {
    expect(cohere.targeting(ctx())).toBe(true);
  });
  it("is hidden when hardware is not capable", () => {
    expect(cohere.targeting(ctx({}, { capable: false }))).toBe(false);
  });
  it("is hidden under lockdown", () => {
    expect(cohere.targeting(ctx({}, { capable: true }, true))).toBe(false);
  });
  it("is hidden when Cohere is already the active STT model", () => {
    expect(cohere.targeting(ctx({ parakeet_model: "cohere-transcribe" }))).toBe(false);
  });
  it("is marked devOnly", () => {
    expect(cohere.devOnly).toBe(true);
  });
});

describe("pickOffer", () => {
  it("returns the highest-priority eligible offer (Gemini before Cohere)", () => {
    // includeDevOnly so both are in the pool; Gemini (priority 10) wins.
    expect(pickOffer(OFFERS, ctx(), [], { includeDevOnly: true })?.id).toBe("gemini-3-5-flash");
  });
  it("excludes devOnly offers from the production pool", () => {
    // Gemini dismissed + Cohere is devOnly -> production pool is empty.
    expect(pickOffer(OFFERS, ctx(), ["gemini-3-5-flash"])).toBeNull();
  });
  it("returns Cohere when devOnly is included and Gemini is dismissed", () => {
    expect(pickOffer(OFFERS, ctx(), ["gemini-3-5-flash"], { includeDevOnly: true })?.id).toBe(
      "cohere-transcribe",
    );
  });
  it("returns null when a dismissed id removes the only eligible offer", () => {
    // Only Gemini is eligible (Cohere devOnly excluded); dismissing it -> null.
    expect(pickOffer(OFFERS, ctx({ enhancement_provider: "gemini" }), ["gemini-3-5-flash"])).toBeNull();
  });
  it("ignores a targeting predicate that throws", () => {
    const throwing = {
      ...gemini,
      id: "boom",
      targeting: () => {
        throw new Error("nope");
      },
    };
    expect(() => pickOffer([throwing], ctx(), [], { includeDevOnly: true })).not.toThrow();
    expect(pickOffer([throwing], ctx(), [], { includeDevOnly: true })).toBeNull();
  });
});

describe("History recovery required offer", () => {
  const recovery = OFFERS.find((o) => o.id === "history-recovery-setup")!;

  it("targets history users who need a recovery key, never under Lockdown", () => {
    expect(recovery.required).toBe(true);
    expect(recovery.targeting({ ...ctx(), historyRecoveryNeedsSetup: true })).toBe(true);
    expect(recovery.targeting({ ...ctx(), historyRecoveryNeedsSetup: false })).toBe(false);
    expect(recovery.targeting(ctx())).toBe(false);
    expect(recovery.targeting({ ...ctx({}, { capable: true }, true), historyRecoveryNeedsSetup: true })).toBe(false);
  });

  it("outranks every other offer", () => {
    expect(
      pickOffer(OFFERS, { ...ctx(), historyRecoveryNeedsSetup: true }, [], { includeDevOnly: true })?.id,
    ).toBe("history-recovery-setup");
  });

  it("uses the locked platform wording", () => {
    expect(recovery.eyebrow).toBe("One-time setup");
    expect(recovery.headline).toBe("Add a recovery key for your transcripts");
    expect(recovery.note).toBe("Saving continues while you finish this.");
    expect(recovery.cta.label).toBe("Set up recovery key");
    expect(recovery.bodyFor?.({ platform: "windows" })).toBe(
      "Your history is saved on this PC with no backup. A recovery key lets you get it back if this PC loses its encryption key. It takes a minute.",
    );
    expect(recovery.bodyFor?.({ platform: "mac" })).toContain("saved on this Mac");
  });
});
