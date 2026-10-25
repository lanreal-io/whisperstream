import { type ClaudeCardSurface, type MeasuredClaudeCard } from "../geometry";
import { useRef, useState, useLayoutEffect } from "react";

/**
 * The ONE measure-the-painted-card routine (design decision 17). Every Claude
 * surface calls it and reports into one `measuredCardSize` state in `HUD`,
 * which feeds the CSS box and the native bounds/region together. Do not add a
 * second measurement path: two surfaces of the same object must not drift
 * apart in how they are sized, and the OS clip must never be derived from
 * anything other than the rect that was actually painted.
 *
 * The element it observes carries `data-claude-card-measure` so the painted
 * card, and only the painted card, is what gets measured.
 */
export function useMeasuredClaudeCard(
  surface: ClaudeCardSurface,
  onMeasured?: (measured: MeasuredClaudeCard) => void,
  radius?: number,
  /** Reply surfaces: how much taller the card may still grow on its own
   *  (the reply field filling up), read at each measurement. */
  growthReserve?: () => number,
): {
  cardRef: React.RefObject<HTMLElement | null>;
  measuredCardSize: { width: number; height: number } | null;
} {
  const cardRef = useRef<HTMLElement | null>(null);
  const growthReserveRef = useRef(growthReserve);
  growthReserveRef.current = growthReserve;
  const [measuredCardSize, setMeasuredCardSize] = useState<{
    width: number;
    height: number;
  } | null>(null);

  useLayoutEffect(() => {
    const card = cardRef.current;
    if (!card) return;
    const measureCard = () => {
      const next = {
        width: Math.round(card.offsetWidth),
        height: Math.round(card.offsetHeight),
      };
      setMeasuredCardSize((current) =>
        current !== null &&
        current.width === next.width &&
        current.height === next.height
          ? current
          : next,
      );
      onMeasured?.({
        surface,
        width: next.width,
        height: next.height,
        radius,
        growthReserveH: growthReserveRef.current?.(),
      });
    };
    measureCard();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measureCard);
    observer.observe(card);
    return () => observer.disconnect();
  }, [onMeasured, surface, radius]);

  return { cardRef, measuredCardSize };
}
