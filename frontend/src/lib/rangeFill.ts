import type { CSSProperties } from "react";

/**
 * Returns the inline style that drives a range slider's fill via the global
 * --range-fill custom property consumed by globals.css's "Range slider"
 * rule (see design.md's "Slider consolidation" section). Value is clamped to
 * the [min, max] range and rounded to an integer percent; a degenerate range
 * (max === min) safely returns 0% instead of dividing by zero.
 */
export function rangeFill(value: number, min: number, max: number): CSSProperties {
  const ratio = max === min ? 0 : (value - min) / (max - min);
  const clamped = Math.min(1, Math.max(0, ratio));
  const percent = Math.round(clamped * 100);
  return { "--range-fill": `${percent}%` } as CSSProperties;
}
