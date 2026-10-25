import { rangeFill } from "../lib/rangeFill";

/**
 * Shared range-input atom: a native `<input type="range">` wired to the global
 * `--range-fill` fill CSS (see `rangeFill`). This is the ATOM only, not a row
 * layout — callers own the surrounding row grammar. Configure's `SliderRow`
 * (`HudConfigModal.tsx`) re-wraps this in its label-left / value-right row;
 * the Agent Speak onboarding voice step uses it inside a label-above,
 * centered band. Do not fold row layout (labels, value display) into this
 * component; keep it a plain, reusable control.
 */
export function RangeInput({
  ariaLabel,
  ariaValueText,
  min,
  max,
  step,
  value,
  onChange,
  className,
}: {
  ariaLabel: string;
  /**
   * Optional `aria-valuetext`: the value spoken to screen readers with its
   * unit (e.g. `"1.2x"`), so SR users hear the multiplier rather than a bare
   * `1.2`. Omitted by Configure's `SliderRow` (which keeps the native numeric
   * announcement); set by the Agent Speak onboarding speed control.
   */
  ariaValueText?: string;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (value: number) => void;
  className?: string;
}) {
  return (
    <input
      type="range"
      min={min}
      max={max}
      step={step}
      value={value}
      aria-label={ariaLabel}
      aria-valuetext={ariaValueText}
      onChange={(event) => onChange(Number(event.target.value))}
      className={className ?? "w-[120px]"}
      style={rangeFill(value, min, max)}
    />
  );
}
