import type { CSSProperties } from "react";

/** The HUD's quiet status text: reply hints, queue depth and the reply
 *  countdown on the pill, the glance card and the console footer. One style
 *  so the surfaces never drift apart (owner, 2026-10-24). Only the size
 *  differs: the pill's caption under the session name is smaller. */
export const HUD_META_SIZE = 10.5;
export const HUD_META_CAPTION_SIZE = 9;
/** Exported for the pill caption, whose opacity is animated in and out. */
export const HUD_META_OPACITY = 0.7;

export function hudMetaText(fontSize: number = HUD_META_SIZE): CSSProperties {
  return {
    fontFamily: "var(--font-agent)",
    fontSize,
    fontWeight: 500,
    letterSpacing: "0.01em",
    color: "var(--color-steel)",
    opacity: HUD_META_OPACITY,
    fontVariantNumeric: "tabular-nums",
  };
}
