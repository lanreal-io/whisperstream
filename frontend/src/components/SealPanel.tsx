/**
 * SealPanel — the signature lockdown-mode composition.
 *
 * A Level-3 deepened-frost surface (opacity stepped from the normal 0.30 to
 * 0.45 — the glass "freezes harder" when sealed) with a left-aligned
 * ShieldSealGlyph and a mint-dot hardened-status checklist.
 *
 * Used in:
 *  - Surface A (Privacy & Security settings, expanded sub-controls)
 *  - Surface B Step 3 (done screen, compact=true)
 *  - Lock screen footer (compact=true)
 *
 * Design decisions:
 *  - Decision 1: no new hue; opacity shift only (0.30 -> 0.45)
 *  - Decision 2: --color-mint = the hardened-status checklist dots only
 */

/** Shield-seal glyph built on the existing ShieldMiniIcon recipe from
 *  Enhancement.tsx (1.5 stroke / currentColor / round joins), extended with
 *  an inner check mark to read as "sealed + verified." Lives here so it is
 *  always co-located with SealPanel. NOT a new icon set; it is one glyph. */
export function ShieldSealGlyph({
  size = 16,
  className = "",
}: {
  size?: number;
  className?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M8 1.8 L13 3.6 V8.2 C13 11.4 10.7 13.6 8 14.4 C5.3 13.6 3 11.4 3 8.2 V3.6 Z" />
      <path d="M5.8 8.1 L7.3 9.6 L10.3 6.2" />
    </svg>
  );
}

export interface SealPanelProps {
  /** Checklist items displayed as mint-dot rows. */
  items: string[];
  /** Panel heading. Default matches the locked copy decision A5. */
  heading?: string;
  /** Compact layout (reduced padding) for use in the done screen + lock screen footer. */
  compact?: boolean;
}

/**
 * SealPanel — deepened-frost Level-3 surface with a shield-seal glyph and
 * a mint-dot hardened-status checklist.
 *
 * Props:
 *   items    - checklist label strings (use the A6 locked strings verbatim)
 *   heading  - defaults to "Protections active" (locked copy decision A5)
 *   compact  - reduces padding for use in small surfaces (Step 3, lock footer)
 */
export function SealPanel({
  items,
  heading = "Protections active",
  compact = false,
}: SealPanelProps) {
  return (
    <div
      className={`rounded-[var(--radius-row)] ${compact ? "p-4" : "p-5"}`}
      style={{
        // Deepened-frost Level-3 raised surface: the glass "freezes harder" when
        // sealed. Routed to the raised-surface token so it flips light/dark; the
        // specular top rim uses --rim-specular (dims in dark instead of a hard
        // white line). Nested inside dialog cards (radius-card 28) -> radius-row.
        // Steel edge: a white-alpha border vanishes on the white modal in Light.
        background: "var(--surface-raised)",
        border: "1px solid var(--border-hairline-steel)",
        boxShadow: "inset 0 1px 0 var(--rim-specular)",
      }}
    >
      <div className="flex items-center gap-2 mb-3">
        <ShieldSealGlyph size={16} className="text-steel" />
        <span className="text-[13px] font-medium text-slate">{heading}</span>
      </div>
      <ul className="space-y-1.5">
        {items.map((label) => (
          <li key={label} className="flex items-start gap-2 text-[12px] text-steel">
            {/* Mint dot: --color-mint = hardened-status checklist only (Decision 2). */}
            <span
              aria-hidden="true"
              className="mt-[5px] w-1.5 h-1.5 rounded-full shrink-0 bg-mint"
            />
            <span>{label}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
