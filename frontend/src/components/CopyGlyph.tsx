/**
 * The app's copy glyph: two offset rounded sheets on the 14px icon grid,
 * stroke=currentColor at 1.5. Extracted from the Transcripts card header so
 * the HUD reply rail draws the same mark instead of a look-alike.
 *
 * Decorative (`aria-hidden`); the wrapping button carries the accessible name.
 */
export function CopyGlyph({ size = 13 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="4.5" y="4.5" width="7" height="7" rx="1.5" />
      <path d="M9.5 4.5V3a1.5 1.5 0 00-1.5-1.5H3A1.5 1.5 0 001.5 3v5A1.5 1.5 0 003 9.5h1.5" />
    </svg>
  );
}
