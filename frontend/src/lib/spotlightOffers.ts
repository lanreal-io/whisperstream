/* ---------------------------------------------------------------------------
   Feature-spotlight / upgrade-offer registry.

   Offers are declarative data. One <FeatureSpotlight> component renders any of
   them; it knows nothing about Gemini or Cohere. Adding an Agent Speak or
   Command Mode offer later is one object here plus one hero component.

   `pickOffer` is the pure launch selector: given the registry, the launch
   context, and the set of already-dismissed ids, it returns the single
   highest-priority eligible offer to show (or null). It excludes `devOnly`
   offers from the production launch pool unless `includeDevOnly` is set (the
   dev force-hook / dialog registry passes true).
--------------------------------------------------------------------------- */
import { createElement } from "react";
import type { ReactNode } from "react";
import type { AppConfig } from "./api/config";
import { GeminiFlashHero } from "../components/spotlight/heroes/GeminiFlashHero";
import { CohereTranscribeHero } from "../components/spotlight/heroes/CohereTranscribeHero";
import { HistoryRecoveryHero } from "../components/spotlight/heroes/HistoryRecoveryHero";

/** Cheap, model-agnostic hardware eligibility signal, read at launch. `capable`
 *  answers "could this machine plausibly run a heavier model?" without loading
 *  or benchmarking anything. */
export interface HardwareSignal {
  capable: boolean;
}

/** Everything an offer's targeting predicate may consult at launch. */
export interface SpotlightContext {
  config: AppConfig;
  hardware: HardwareSignal;
  lockdown: boolean;
  /** Standard history is on with no recovery key yet (recovery status). */
  historyRecoveryNeedsSetup?: boolean;
  /** "mac" / "windows" for platform wording. */
  platform?: "mac" | "windows";
}

/** Navigation surface handed to a `deep-link` CTA's `run`. The App supplies the
 *  concrete implementations so the registry stays decoupled from App state. */
export interface SpotlightNav {
  /** Navigate the main window to a settings tab (e.g. "enhancement"). */
  goToTab: (tab: string) => void;
  /** Consent-safe Gemini provider switch + auto-select gemini-3.5-flash +
   *  deep-link to the Enhancement page. */
  switchToGeminiFlash: () => void;
  /** Open the required recovery key setup on the Transcripts tab. */
  openHistoryRecoverySetup?: () => void;
}

export type SpotlightCta =
  | { kind: "deep-link"; label: string; run: (nav: SpotlightNav) => void }
  | { kind: "benchmark"; label: string; benchModelId: string }
  | { kind: "external"; label: string; url: string };

export interface SpotlightOffer {
  /** Stable id; persisted in AppConfig.dismissed_offer_ids. Case-sensitive. */
  id: string;
  /** Small uppercase announcement pill (e.g. "NEW", "NOW AVAILABLE"). */
  eyebrow: string;
  headline: string;
  body: string;
  /** Platform wording for the body; falls back to `body`. */
  bodyFor?: (ctx: Pick<SpotlightContext, "platform">) => string;
  /** Small print under the body. */
  note?: string;
  /** Non-skippable: no Maybe later, Esc and backdrop do nothing. Recorded as
   *  resolved when shown, so it appears once even if the app quits. */
  required?: boolean;
  /** Hero node for the spotlight's hero band (animated SVG, reduced-motion aware). */
  hero: ReactNode;
  /** Single accent per card. Default "frost"; "gold" reserved for future
   *  Pro/trial-flavored offers. */
  accent: "frost" | "gold";
  cta: SpotlightCta;
  /** Predicate over launch config/hardware deciding whether this offer is
   *  relevant to this user right now. */
  targeting: (ctx: SpotlightContext) => boolean;
  /** Lower shows first. */
  priority: number;
  /** When true, excluded from the production launch pool (stubbed / not ready
   *  for real users). Still reachable via the dev force-hook and dialog registry. */
  devOnly?: boolean;
}

export interface PickOfferOptions {
  /** Include devOnly offers (dev force-hook / dialog registry). Default false. */
  includeDevOnly?: boolean;
}

/** Pure launch selector: first eligible, undismissed offer by ascending
 *  priority, or null. Never throws — a targeting predicate that throws is
 *  treated as "not eligible". */
export function pickOffer(
  offers: readonly SpotlightOffer[],
  ctx: SpotlightContext,
  dismissedIds: readonly string[],
  { includeDevOnly = false }: PickOfferOptions = {},
): SpotlightOffer | null {
  const dismissed = new Set(dismissedIds);
  const eligible = offers
    .filter((o) => includeDevOnly || !o.devOnly)
    .filter((o) => !dismissed.has(o.id))
    .filter((o) => {
      try {
        return o.targeting(ctx);
      } catch {
        return false;
      }
    });
  if (eligible.length === 0) return null;
  return [...eligible].sort((a, b) => a.priority - b.priority)[0];
}

/** Current bundled Gemini lineup — keep in sync with `BUNDLED_MODELS["gemini"]`
 *  in `src/whisperstream/enhancement_models.py`. A user already on one of these
 *  is NOT "on an older Gemini model", so the "switch to 3.5 Flash" offer stays
 *  hidden for them — in particular the new-install default
 *  `gemini-3.5-flash-lite`, which must never self-nag on first launch. */
const CURRENT_GEMINI_MODELS = new Set([
  "gemini-3.5-flash-lite",
  "gemini-3.1-flash-lite",
  "gemini-3.7-flash",
  "gemini-3.5-flash",
  "gemini-3.1-pro-preview",
]);

/** The offer registry, priority-ordered. Copy is locked (see copy-review.md). */
export const OFFERS: SpotlightOffer[] = [
  {
    id: "history-recovery-setup",
    eyebrow: "One-time setup",
    headline: "Add a recovery key for your transcripts",
    body: "Your history is saved on this Mac with no backup. A recovery key lets you get it back if this Mac loses its encryption key. It takes a minute.",
    bodyFor: ({ platform }) =>
      platform === "windows"
        ? "Your history is saved on this PC with no backup. A recovery key lets you get it back if this PC loses its encryption key. It takes a minute."
        : "Your history is saved on this Mac with no backup. A recovery key lets you get it back if this Mac loses its encryption key. It takes a minute.",
    note: "Saving continues while you finish this.",
    required: true,
    hero: createElement(HistoryRecoveryHero),
    accent: "gold",
    cta: {
      kind: "deep-link",
      label: "Set up recovery key",
      run: (nav) => nav.openHistoryRecoverySetup?.(),
    },
    // Existing history users without a recovery key. Lockdown owns recovery,
    // so never under Lockdown.
    targeting: ({ lockdown, historyRecoveryNeedsSetup }) =>
      !lockdown && historyRecoveryNeedsSetup === true,
    priority: 0,
  },
  {
    id: "gemini-3-5-flash",
    eyebrow: "NOW AVAILABLE",
    headline: "Gemini 3.5 Flash is here",
    body: "You're on an older Gemini model. 3.5 Flash is faster and more accurate, with nothing else to set up.",
    hero: createElement(GeminiFlashHero),
    accent: "frost",
    cta: {
      kind: "deep-link",
      label: "Switch to 3.5 Flash",
      run: (nav) => nav.switchToGeminiFlash(),
    },
    // Shown only to a user on a RETIRED Gemini model (one not in the current
    // bundled lineup), and never under lockdown (which coerces the effective
    // provider to local + blocks cloud egress). Excluding the whole current
    // lineup — not just gemini-3.5-flash — keeps the new free-tier default
    // (gemini-3.5-flash-lite) from being told it's "on an older model".
    targeting: ({ config, lockdown }) =>
      !lockdown &&
      config.enhancement_provider === "gemini" &&
      !CURRENT_GEMINI_MODELS.has(config.enhancement_gemini_model),
    priority: 10,
  },
  {
    id: "cohere-transcribe",
    eyebrow: "NEW",
    headline: "A more accurate way to transcribe",
    body: "Cohere transcribe can be more accurate than today's model. It's heavier, so we'll download it and check your computer can keep up.",
    hero: createElement(CohereTranscribeHero),
    accent: "frost",
    cta: {
      kind: "benchmark",
      label: "Check my hardware",
      benchModelId: "cohere-transcribe",
    },
    // Gated by the cheap hardware signal, suppressed under lockdown, and not
    // shown if Cohere is already the active STT model. `devOnly` keeps it OUT of
    // the production launch pool (stubbed until the real Cohere STT backend
    // ships); drop the flag when that lands.
    targeting: ({ config, hardware, lockdown }) =>
      !lockdown && hardware.capable && config.parakeet_model !== "cohere-transcribe",
    priority: 20,
    devOnly: true,
  },
];

