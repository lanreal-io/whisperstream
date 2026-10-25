// The one expand/collapse control for the HUD's reading surfaces: the glance
// card's expand, the console's collapse and Live Preview's toggle
// (agent-speak-centered-console design.md). An icon-only pill so it reads as
// a control, not a label. Light uses the frost accent; dark stays neutral so
// the only blue on a dark HUD is content. Both values are theme tokens.
export const HUD_EXPAND_TOGGLE_W = 40;
export const HUD_EXPAND_TOGGLE_H = 28;

/** Outward arrows to expand, inward to collapse; 14px at stroke 1.4. */
const EXPAND_PATH = "M8.5 2.5h3v3M11.5 2.5L8 6M5.5 11.5h-3v-3M2.5 11.5L6 8";
const COLLAPSE_PATH = "M8 2.5v3.5h3.5M8 6l3.5-3.5M6 11.5V8H2.5M6 8l-3.5 3.5";

export function HudExpandToggle({
  expanded,
  label,
  onToggle,
  testId,
}: {
  /** True when the surface is open, so the control collapses it. */
  expanded: boolean;
  /** Accessible name and native tooltip. */
  label: string;
  onToggle: () => void;
  testId?: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      // Pointer-only like the HUD's other controls: the View shortcut and
      // Escape are the keyboard paths.
      tabIndex={-1}
      data-testid={testId}
      data-hud-interactive="true"
      data-expanded={expanded ? "true" : "false"}
      onPointerDown={(event) => event.stopPropagation()}
      onDoubleClick={(event) => event.stopPropagation()}
      onClick={(event) => {
        event.stopPropagation();
        onToggle();
      }}
      className="flex shrink-0 cursor-pointer items-center justify-center rounded-full bg-[var(--surface-control)] p-0 transition-colors duration-150 hover:bg-[var(--surface-hover)] active:scale-[0.96]"
      style={{
        width: HUD_EXPAND_TOGGLE_W,
        height: HUD_EXPAND_TOGGLE_H,
        border: "1px solid var(--hud-expand-border)",
        color: "var(--hud-expand-icon)",
      }}
    >
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
        <path
          d={expanded ? COLLAPSE_PATH : EXPAND_PATH}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  );
}
