import { useState, type ButtonHTMLAttributes, type ReactNode } from "react";

// ---------------------------------------------------------------------------
// SecondaryButton — the app's frost secondary button (round 03).
//
// Before this existed, every secondary button was an inline copy of a white
// fill (rgba(255,255,255,0.30)) + near-white border (rgba(255,255,255,0.40)),
// which washed out on the app's light card surfaces. This primitive carries the
// chosen Option C treatment: a white fill + STEEL-tinted hairline border + inset
// top-light, a neutral "structural line" that separates on every white surface
// including the lightest cards (round 04, superseding round 03's frost-tint
// fill). The `destructive` variant (D2) is always rose-edged so a destructive
// action (Deactivate License) is legible without a hover.
//
// It owns ONLY the contrast treatment (background, border, inset, hover). The
// caller supplies layout + text color via `className` (size, radius, flex, the
// text-slate / text-steel class), so the same primitive fits a text pill, an
// icon-only round button, and the player's rate/save chips.
// ---------------------------------------------------------------------------

type Variant = "neutral" | "destructive";

const TOKENS: Record<
  Variant,
  { bg: string; bgHover: string; border: string; borderHover: string }
> = {
  // Option C (round 04): white fill + steel-tinted hairline border. The steel
  // (#6B8299) is the app's secondary-text color, so the border reads as a
  // neutral "structural line" rather than the frost accent (supersedes round
  // 03's Option B frost-tint fill). The themed --border-hairline-steel keeps
  // that line in light (where a white-alpha hairline vanishes on white cards)
  // and is a faint white edge in dark.
  neutral: {
    bg: "var(--surface-control)",
    bgHover: "var(--surface-hover)",
    border: "var(--border-hairline-steel)",
    borderHover: "rgba(107,130,153,0.40)",
  },
  // D2: permanent rose edge so destructive intent is always visible.
  destructive: {
    bg: "rgba(196,139,139,0.06)",
    bgHover: "rgba(196,139,139,0.12)",
    border: "rgba(196,139,139,0.40)",
    borderHover: "rgba(196,139,139,0.65)",
  },
};

interface SecondaryButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  children: ReactNode;
}

export function SecondaryButton({
  variant = "neutral",
  className = "",
  style,
  disabled,
  onMouseEnter,
  onMouseLeave,
  children,
  ...rest
}: SecondaryButtonProps) {
  const [hovered, setHovered] = useState(false);
  const t = TOKENS[variant];
  const hot = hovered && !disabled;

  return (
    <button
      {...rest}
      disabled={disabled}
      onMouseEnter={(e) => {
        setHovered(true);
        onMouseEnter?.(e);
      }}
      onMouseLeave={(e) => {
        setHovered(false);
        onMouseLeave?.(e);
      }}
      className={`cursor-pointer transition-colors duration-150 disabled:cursor-default ${className}`}
      style={{
        background: hot ? t.bgHover : t.bg,
        border: `1px solid ${hot ? t.borderHover : t.border}`,
        boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale)))",
        ...style,
      }}
    >
      {children}
    </button>
  );
}
