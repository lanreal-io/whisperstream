/* What's new in Whisperstream 2.0: the one-time, non-skippable three-step tour.
   Copy is locked in (Owner revision). The
   ID is persisted in config.dismissed_offer_ids alongside spotlight offers. */

export const WHATS_NEW_ID = "whats-new-2-0";

export const WHATS_NEW_LABEL = "What's new in Whisperstream 2.0";

export type WhatsNewHero = "cohere" | "boost" | "stream1";

export interface WhatsNewStep {
  /** Feature name for the polite step announcement. */
  feature: string;
  /** Sentence case; uppercased by CSS. */
  eyebrow: string;
  headline: string;
  body: string;
  /** The single bullet hint line under the body. */
  hint: string;
  hero: WhatsNewHero;
}

export const WHATS_NEW_STEPS: readonly WhatsNewStep[] = [
  {
    feature: "Cohere Transcribe",
    eyebrow: "New speech model",
    headline: "Try Cohere Transcribe",
    body: "A slower model for when accuracy matters most, especially in European languages.",
    hint: "Change your Voice Recognition Model on Home.",
    hero: "cohere",
  },
  {
    feature: "Dictionary Boost",
    eyebrow: "New in Dictionary",
    headline: "Boost names and terms",
    body: "Add names and specific terms to help Whisperstream spell them right.",
    hint: "Requires AI enhancement.",
    hero: "boost",
  },
  {
    feature: "Stream-1 Mini",
    eyebrow: "Made by Whisperstream",
    headline: "Meet Stream-1 Mini",
    body: "On-device cleanup and formatting. Its light 0.6B model runs on more computers.",
    hint: "Select the Local provider in Enhancement.",
    hero: "stream1",
  },
];

export interface LaunchOverlayGateInput {
  /** Setup complete, connected, launch beat elapsed, not dictating, no other dialog. */
  overlaysClear: boolean;
  lockdown: boolean;
  dismissedOfferIds: readonly string[];
  /** True once the tour has appeared in this app session. */
  tourShownThisSession: boolean;
}

/** Launch ordering for the tour and the spotlight offers. The tour is
 *  informational and sends nothing, so Lockdown does not suppress it. Spotlight
 *  offers keep their Lockdown gate, wait for the tour ID to be resolved, and
 *  defer to the next launch when the tour showed this session. */
export function launchOverlayGates({
  overlaysClear,
  lockdown,
  dismissedOfferIds,
  tourShownThisSession,
}: LaunchOverlayGateInput): { tour: boolean; spotlight: boolean } {
  const tourResolved = dismissedOfferIds.includes(WHATS_NEW_ID);
  return {
    tour: overlaysClear,
    spotlight: overlaysClear && !lockdown && tourResolved && !tourShownThisSession,
  };
}
