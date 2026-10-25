export type BadgeTone = "frost" | "neutral" | "mint" | "rose" | "gold";

const TONE: Record<BadgeTone, { background: string; border: string }> = {
  frost: {
    background: "color-mix(in srgb, var(--color-frost) 15%, transparent)",
    border: "color-mix(in srgb, var(--color-frost) 30%, transparent)",
  },
  neutral: { background: "var(--surface-control)", border: "var(--border-hairline)" },
  mint: {
    background: "color-mix(in srgb, var(--color-mint) 14%, transparent)",
    border: "color-mix(in srgb, var(--color-mint) 32%, transparent)",
  },
  rose: {
    background: "color-mix(in srgb, var(--color-rose) 14%, transparent)",
    border: "color-mix(in srgb, var(--color-rose) 32%, transparent)",
  },
  gold: {
    background: "color-mix(in srgb, var(--color-gold) 15%, transparent)",
    border: "color-mix(in srgb, var(--color-gold) 32%, transparent)",
  },
};

export function Badge({
  label,
  tone = "neutral",
  size = "md",
  className = "",
}: {
  label: string;
  tone?: BadgeTone;
  size?: "sm" | "md";
  className?: string;
}) {
  const sz =
    size === "sm" ? "text-[9px] px-1.5 py-[1px]" : "text-[10px] px-2.5 py-0.5";
  return (
    <span
      className={`inline-flex items-center rounded-full font-display font-semibold uppercase tracking-[0.08em] text-steel shrink-0 ${sz} ${className}`}
      style={{
        background: TONE[tone].background,
        border: `1px solid ${TONE[tone].border}`,
        boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.6 * var(--rim-scale)))",
      }}
    >
      {label}
    </span>
  );
}
