import { useEffect, useRef, useState } from "react";
import { useSpotlightPopupGate } from "../lib/useSpotlightPopupGate";
import type { AppConfig } from "../lib/api/config";
import { FeatureSpotlight } from "./FeatureSpotlight";
import { SpotlightBenchmark } from "./spotlight/SpotlightBenchmark";
import { OFFERS, pickOffer } from "../lib/spotlightOffers";
import type { SpotlightNav, SpotlightOffer } from "../lib/spotlightOffers";

/* ---------------------------------------------------------------------------
   FeatureSpotlightHost — owns launch selection, dismissal persistence, and the
   benchmark hand-off for the reusable upgrade-offer overlay. Mounts inside
   ReadyApp (post-EULA), at lowest priority: it only auto-shows once `gatesClear`
   is true (setup complete, not under lockdown, and the launch-time
   update/error/EULA popups have had their moment).

   Dev preview: `?forceSpotlight=<offerId>` shows any offer (including devOnly
   ones) regardless of gates, and never persists a dismissal.
--------------------------------------------------------------------------- */

function benchmarkLabel(benchModelId: string): string {
  return benchModelId === "cohere-transcribe" ? "Cohere transcribe" : benchModelId;
}

function forcedOfferId(): string | null {
  if (!import.meta.env.DEV || typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("forceSpotlight");
}

export interface FeatureSpotlightHostProps {
  config: AppConfig;
  lockdownEnabled: boolean;
  /** Cheap, model-agnostic "could this machine plausibly run a heavier model?"
   *  signal. Only gates the (devOnly) Cohere offer today. */
  hardwareCapable: boolean;
  nav: SpotlightNav;
  /** Persist that an offer is resolved (dismissed or acted on) so it never
   *  re-nags. No-op for the dev-forced preview. */
  onResolveOffer: (offerId: string) => void;
  /** True once setup is complete, lockdown is off, and higher-priority launch
   *  popups have had their chance. The host defers until then. */
  gatesClear: boolean;
  /** Standard history needs a recovery key (required one-time offer). */
  historyRecoveryNeedsSetup?: boolean;
  platform?: "mac" | "windows";
}

export function FeatureSpotlightHost({
  config,
  lockdownEnabled,
  hardwareCapable,
  nav,
  onResolveOffer,
  gatesClear,
  historyRecoveryNeedsSetup = false,
  platform,
}: FeatureSpotlightHostProps) {
  const ctx = {
    config,
    hardware: { capable: hardwareCapable },
    lockdown: lockdownEnabled,
    historyRecoveryNeedsSetup,
    platform,
  };
  const [offer, setOffer] = useState<SpotlightOffer | null>(null);
  const [inBenchmark, setInBenchmark] = useState(false);
  const forcedRef = useRef(false);
  const selectedRef = useRef(false);
  const forced = forcedOfferId();
  const eligible = offer !== null || (!selectedRef.current && pickOffer(
    OFFERS, ctx, config.dismissed_offer_ids ?? [], { includeDevOnly: false },
  ) !== null);
  const popupsClear = useSpotlightPopupGate(!forced && gatesClear && eligible);

  // Dev force-hook: show the requested offer immediately, once, ignoring gates.
  useEffect(() => {
    if (!forced || forcedRef.current) return;
    forcedRef.current = true;
    const match = OFFERS.find((o) => o.id === forced);
    if (match) setOffer(match);
  }, [forced]);

  // Production launch selection: pick the first eligible, undismissed offer once
  // the gates are clear. Runs at most once per session.
  useEffect(() => {
    if (forced || selectedRef.current || !gatesClear || !popupsClear) return;
    const picked = pickOffer(
      OFFERS,
      ctx,
      config.dismissed_offer_ids ?? [],
      { includeDevOnly: false },
    );
    if (picked) {
      selectedRef.current = true;
      setOffer(picked);
      // A required offer shows once: record it now, since it has no dismissal
      // and quitting must not bring it back. The Transcripts gate remains.
      if (picked.required) onResolveOffer(picked.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [forced, gatesClear, popupsClear, config, hardwareCapable, lockdownEnabled, historyRecoveryNeedsSetup, platform]);

  if (!offer) return null;

  const isForced = forced != null;
  // Pause for dictation and higher-priority surfaces without resolving the offer.
  if (!isForced && (!gatesClear || !popupsClear)) return null;
  const displayOffer = offer.bodyFor ? { ...offer, body: offer.bodyFor({ platform }) } : offer;
  const resolve = () => {
    if (!isForced) onResolveOffer(offer.id);
  };
  const close = () => {
    setInBenchmark(false);
    setOffer(null);
  };

  const handleAct = (o: SpotlightOffer) => {
    switch (o.cta.kind) {
      case "deep-link":
        o.cta.run(nav);
        resolve();
        close();
        break;
      case "benchmark":
        setInBenchmark(true);
        break;
      case "external":
        if (typeof window !== "undefined") window.open(o.cta.url, "_blank", "noopener");
        resolve();
        close();
        break;
    }
  };

  const handleDismiss = () => {
    resolve();
    close();
  };

  const benchmarkSlot =
    inBenchmark && offer.cta.kind === "benchmark" ? (
      <SpotlightBenchmark
        modelLabel={benchmarkLabel(offer.cta.benchModelId)}
        onBack={() => setInBenchmark(false)}
        onUse={() => {
          // STUB: no real backend yet. Resolve the offer and close.
          resolve();
          close();
        }}
      />
    ) : undefined;

  return (
    <FeatureSpotlight offer={displayOffer} onAct={handleAct} onDismiss={handleDismiss} benchmarkSlot={benchmarkSlot} />
  );
}
