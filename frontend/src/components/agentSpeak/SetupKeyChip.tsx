/**
 * `KeyChip` for a MODAL ground — the Agents setup flow's key pill.
 *
 * Why this exists rather than reusing `components/onboarding/KeyChip`
 * (design.md decision 6, and this is a CONTRAST decision stated in both
 * themes):
 *
 *   - `KeyChip` rings with `--border-hairline`, which is white-family. Over
 *     `--surface-modal` (white at 0.96) in **Light** that is white on white
 *     with no chrome at all — the exact defect class that shipped three times
 *     in PR 1.
 *   - `KeyChip`'s fill is `--surface-control`, and in **Dark** that is white
 *     0.07, the same value as `--surface-inset-card`. On the setup flow's inset
 *     cards the fill therefore matches its ground exactly.
 *
 * Neither the fill nor the specular rim can carry the pill's shape in both
 * themes, so the STEEL ring does. `STEEL_BORDER` is the sanctioned
 * theme-invariant structural literal already used by `SpeakInsetCard`,
 * `AgentProviderInstallList`'s buttons, and `VoicePicker`'s disc ring: steel resolves
 * dark-on-light in Light and light-on-dark in Dark, so one value draws the
 * shape on both grounds. The fill matching its ground in Dark is intentional.
 *
 * Two further deltas from `KeyChip`:
 *   - the specular rim routes through `--rim-specular` instead of the
 *     hardcoded `rgba(255,255,255,0.95)`, which glares on the dark card;
 *   - `KeyChip`'s `0 2px 8px` frost drop glow is dropped. the app's depth strategy is borders plus inner light, never shadows,
 *     and a glow on a static display pill inside a dialog is decoration.
 *
 * Geometry, type scale, and title-casing are `KeyChip`'s, unchanged.
 *
 * Accessibility (copy-review.md, Accessibility Notes): the chip is used inline
 * inside sentences, so its tokens stay plain text in the parent paragraph and
 * the component adds NO `aria-label` — a per-token label would interrupt the
 * sentence. The `+` separators are decorative and `aria-hidden`, or the
 * sentence reads "Ctrl plus Alt plus".
 */

const STEEL_BORDER = "1px solid rgba(107,130,153,0.28)";

interface SetupKeyChipProps {
  keys: string;
}

// Title-case a token preserving common multi-word key names. `"right shift"`
// → `"Right Shift"`, `"ctrl"` → `"Ctrl"`. Preserves pre-existing mixed case
// (so `"F1"` stays `"F1"`). Copied from `KeyChip`, which does not export it;
// the dialog family copies rather than DRYs.
function titleCase(token: string): string {
  return token
    .trim()
    .split(/\s+/)
    .map((word) => {
      if (!word) return word;
      if (word !== word.toLowerCase() && word !== word.toUpperCase()) {
        return word;
      }
      return word[0].toUpperCase() + word.slice(1).toLowerCase();
    })
    .join(" ");
}

export function SetupKeyChip({ keys }: SetupKeyChipProps) {
  const tokens = (keys || "")
    .split("+")
    .map((t) => t.trim())
    .filter(Boolean);

  // Inherited from `ShortcutCapture`'s unbound state — not a new string.
  if (tokens.length === 0) {
    return <span className="text-[12px] text-steel">Not set</span>;
  }

  return (
    <span className="inline-flex items-center gap-1 align-middle">
      {tokens.map((token, i) => (
        <span key={`${token}-${i}`} className="inline-flex items-center gap-1">
          <span
            data-setup-key
            className="inline-block rounded-full px-2.5 py-[2px] text-[11px] font-medium tracking-wide text-slate"
            style={{
              background: "var(--surface-control)",
              border: STEEL_BORDER,
              boxShadow: "inset 0 1px 0 var(--rim-specular)",
            }}
          >
            {titleCase(token)}
          </span>
          {i < tokens.length - 1 && (
            <span
              data-setup-key-separator
              aria-hidden="true"
              className="text-[11px] text-steel/40"
            >
              +
            </span>
          )}
        </span>
      ))}
    </span>
  );
}
