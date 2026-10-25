import { useEffect, useState } from "react";
import { useSpotlightPopupGate } from "../lib/useSpotlightPopupGate";
import type { AppConfig } from "../lib/api/config";
import { WHATS_NEW_ID } from "../lib/whatsNew";
import { WhatsNewTour } from "./WhatsNewTour";

/* ---------------------------------------------------------------------------
   WhatsNewTourHost: shows the one-time "What's new in Whisperstream 2.0" tour
   once WHATS_NEW_ID is absent from config.dismissed_offer_ids and the launch
   gates are clear (the caller computes them with launchOverlayGates, which
   deliberately ignores Lockdown). Only "Start dictating" resolves the ID;
   quitting mid-tour leaves it unresolved so it shows again next launch.

   Dev preview: `?forceSpotlight=whats-new-2-0` shows the tour regardless of
   gates and never persists.
--------------------------------------------------------------------------- */

function forcedTour(): boolean {
  if (!import.meta.env.DEV || typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("forceSpotlight") === WHATS_NEW_ID;
}

export interface WhatsNewTourHostProps {
  config: AppConfig;
  /** Launch gates for the tour (see launchOverlayGates). */
  gatesClear: boolean;
  /** Persist the resolved ID. Not called for the dev preview. */
  onResolveOffer: (offerId: string) => void;
  /** Fired after completion; the app switches to Home. */
  onStartDictating: () => void;
  /** Fired when the tour first appears this session. */
  onShow?: () => void;
}

export function WhatsNewTourHost({ config, gatesClear, onResolveOffer, onStartDictating, onShow }: WhatsNewTourHostProps) {
  const [forced] = useState(forcedTour);
  const [done, setDone] = useState(false);
  const eligible = !done && !(config.dismissed_offer_ids ?? []).includes(WHATS_NEW_ID);
  const popupsClear = useSpotlightPopupGate(!forced && gatesClear && eligible);
  const visible = forced ? !done : eligible && gatesClear && popupsClear;

  useEffect(() => {
    if (visible && !forced) onShow?.();
  }, [visible, forced, onShow]);

  if (!visible) return null;

  const complete = () => {
    if (!forced) onResolveOffer(WHATS_NEW_ID);
    setDone(true);
    onStartDictating();
  };

  return <WhatsNewTour onComplete={complete} />;
}
