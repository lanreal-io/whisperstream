import { shortcutDisplayParts } from "../../lib/shortcutDisplay";

interface KeyChipProps {
  keys: string;
}

// Inline display of the user's shortcut. Splits on `+` and renders each
// token as a small white-pearl pill — the same idiom ShortcutCapture uses
// for live key feedback (white glass with frost glow, slate text). The
// frost gradient is reserved in this app for primary CTAs, not key labels.
//
// Locked rules (design.md §Design Decisions):
//   - Key names come from shortcutDisplayParts, so they match the
//     ShortcutCapture chip (Mac glyphs, worded sides like "Right Shift").
//   - Multi-key shortcuts render as `[Ctrl] + [Alt] + [Space]` with thin
//     `+` separators.
export function KeyChip({ keys }: KeyChipProps) {
  const tokens = shortcutDisplayParts(keys || "");

  if (tokens.length === 0) {
    return (
      <span
        className="inline-block align-middle px-2 py-[2px] mx-[2px] rounded-full text-[11px] font-medium text-steel/70"
        style={{
          background: "var(--surface-control)",
          border: "1px solid var(--border-standard)",
          boxShadow: "0 2px 6px color-mix(in srgb, var(--color-frost) 15%, transparent)",
        }}
      >
        …
      </span>
    );
  }

  return (
    <span className="inline-flex items-center gap-1 align-middle">
      {tokens.map((token, i) => (
        <span key={`${token}-${i}`} className="inline-flex items-center gap-1">
          <span
            className="inline-block px-2.5 py-[2px] rounded-full text-[11px] font-medium text-slate tracking-wide"
            style={{
              background: "var(--surface-control)",
              border: "1px solid var(--border-hairline)",
              boxShadow:
                "inset 0 1px 0 rgb(255 255 255 / calc(0.95 * var(--rim-scale))), 0 2px 8px color-mix(in srgb, var(--color-frost) 20%, transparent)",
            }}
          >
            {token}
          </span>
          {i < tokens.length - 1 && (
            <span className="text-[11px] text-steel/40">+</span>
          )}
        </span>
      ))}
    </span>
  );
}
