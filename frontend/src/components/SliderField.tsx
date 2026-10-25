import type { ReactNode } from "react";

/**
 * Owner trial (2026-10-25): every slider sits in a control field with its
 * value at the right ("field", the prototype's S1). If the trial is reverted,
 * "bare" (S3) keeps the same layout without the field's fill and edge. Flip
 * this one constant; the shortcut field's `SHORTCUT_DISPLAY_STYLE` is the
 * matching switch (S3 pairs with plain-text shortcuts).
 */
export const SLIDER_FIELD_STYLE: "field" | "bare" = "field";

/**
 * Layout shell for a range input and its value. The range stays the caller's
 * (`RangeInput`, or a raw input with its own keyboard handling); the global
 * `.slider-field` CSS stretches it and recolours the unfilled track so it
 * reads on the field. Callers choose the width.
 */
export function SliderField({
  children,
  value,
  className = "",
}: {
  /** The range input. */
  children: ReactNode;
  /** Trailing value: a readout or an editable box. */
  value?: ReactNode;
  className?: string;
}) {
  return (
    <div
      data-slider-field={SLIDER_FIELD_STYLE}
      className={`slider-field${SLIDER_FIELD_STYLE === "bare" ? " slider-field--bare" : ""} ${className}`.trim()}
    >
      {children}
      {value}
    </div>
  );
}

/** The plain trailing readout for a slider whose value is not typed. */
export function SliderValue({ children }: { children: ReactNode }) {
  return (
    <span className="w-10 shrink-0 pr-1.5 text-right text-[12px] font-medium tabular-nums text-slate">
      {children}
    </span>
  );
}
