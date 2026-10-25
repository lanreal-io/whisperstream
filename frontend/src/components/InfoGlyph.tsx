/**
 * The app's circled-i info glyph: a bare 12x12 SVG, no bordered circle
 * wrapper. Same recipe as `Enhancement.tsx`'s `InfoMiniIcon` and the
 * `Home.tsx` `ModelInfoTooltip` icon (stroke=currentColor, 1.5 stroke,
 * round caps/joins). Extracted once so `InfoDot` (`HudConfigModal.tsx`,
 * `SetupStepVoice.tsx`) can share it instead of drawing a literal "i"
 * character (agent-onboarding-improvements scope.md #6, design.md §6).
 *
 * Decorative by default (`aria-hidden="true"`); the button that wraps it
 * carries the accessible name via `aria-label`. Spreads `SVGProps` so a
 * caller can override `aria-hidden` or pass a `className`.
 */
export function InfoGlyph(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 12 12"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      <circle cx="6" cy="6" r="4.5" />
      <path d="M6 5.5v3" />
      <path d="M6 3.6v0.01" />
    </svg>
  );
}
