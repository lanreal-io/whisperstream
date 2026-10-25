import type { ReactNode } from "react";
import { HoverTooltip } from "./HoverTooltip";

/**
 * The app's segmented mode pills: a row of mutually exclusive choices where
 * the active one is filled with the accent gradient.
 *
 * Reach for this for ANY "pick one of these modes" row. Before it existed the
 * recipe was hand-copied at four call sites, and they had already drifted on
 * the active shadow: Audio and the Power Mode profile editor used
 * `--shadow-frost-glow`, while Controls' Input Style and the Agent Speak
 * settings used the flatter `--shadow-glass-sm`. That one token is the whole
 * difference between the pills the owner called "nice" and the ones they
 * called "bad", so `--shadow-frost-glow` is canonical here and the flat
 * variant is gone.
 *
 * NOT for the App|Website control in `AddAppModal`. That is a different
 * species: a true segmented *track* (an inset groove with a sliding filled
 * pill, `role="radiogroup"`/`role="radio"`), not separate bordered pills. Do
 * not fold it into this component without deciding you want that visual and
 * semantic change.
 */
export interface ModeOption<T extends string> {
  value: T;
  label: ReactNode;
  /** Hover gloss for a non-obvious choice. Renders a `HoverTooltip`. */
  hint?: string;
  /** Accessible name when `label` alone is not descriptive enough. */
  ariaLabel?: string;
  disabled?: boolean;
  /** Opt-in declarative QA hook: stamps `data-ui-qa-hover-target` on this pill
   *  so a deterministic UI QA state can hover exactly this option and freeze
   *  its `hint` bubble. Undefined for every other consumer, which therefore
   *  renders byte-identically. */
  uiQaHoverTarget?: boolean;
}

interface ModeSelectorProps<T extends string> {
  value: T | undefined;
  options: ModeOption<T>[];
  onChange: (value: T) => void;
  /** Accessible name for the group, e.g. "Input style". */
  ariaLabel: string;
  /** Extra classes on the group wrapper (layout only). */
  className?: string;
}

export function ModeSelector<T extends string>({
  value,
  options,
  onChange,
  ariaLabel,
  className = "",
}: ModeSelectorProps<T>) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className={`flex flex-wrap gap-1.5 ${className}`.trim()}
    >
      {options.map((option) => {
        const isActive = value === option.value;
        const pill = (
          <button
            key={option.value}
            type="button"
            onClick={() => onChange(option.value)}
            aria-pressed={isActive}
            aria-label={option.ariaLabel}
            disabled={option.disabled}
            data-ui-qa-hover-target={option.uiQaHoverTarget ? "true" : undefined}
            className={`rounded-full px-3 py-1.5 text-[12px] font-medium transition-all duration-200 ${
              option.disabled ? "cursor-not-allowed opacity-45" : "cursor-pointer"
            }`}
            style={{
              background: isActive
                ? "var(--accent-fill)"
                : "var(--surface-control)",
              color: isActive ? "#fff" : "var(--color-steel)",
              border: isActive
                ? "1px solid var(--border-accent)"
                : "1px solid var(--border-hairline)",
              // The caught-light inset on the inactive pill is deliberately a
              // theme-invariant white specular rim, not a surface fill.
              boxShadow: isActive
                ? "var(--shadow-frost-glow)"
                : "inset 0 1px 0 rgb(255 255 255 / calc(0.6 * var(--rim-scale)))",
            }}
          >
            {option.label}
          </button>
        );

        return option.hint ? (
          <HoverTooltip key={option.value} content={option.hint}>
            {pill}
          </HoverTooltip>
        ) : (
          pill
        );
      })}
    </div>
  );
}
