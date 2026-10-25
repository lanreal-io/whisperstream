import { type CSSProperties } from "react";

// The HUD floats over arbitrary desktop content, not the app canvas. Its two
// app-theme variants therefore need stronger, more opaque material than the
// in-window surfaces: glacial blue in Light, charcoal in Dark. Both carry a
// contrasting perimeter and tinted elevation so they survive over white Gmail
// and dark application chrome without sampling the desktop behind WebView2.
export type HudDesktopTheme = "light" | "dark";
type HudDesktopPalette = CSSProperties & Record<`--${string}`, string>;

export const HUD_LIGHT_DESKTOP_SURFACE = "rgba(237, 244, 250, 0.98)";
export const HUD_DARK_DESKTOP_SURFACE = "rgba(19, 29, 40, 0.98)"; // 6
export const HUD_LIGHT_DESKTOP_ELEVATION =
  "inset 0 1px 0 rgba(255,255,255,0.92), inset 0 -1px 0 rgba(70,94,115,0.12), 0 1px 2px rgba(39,62,82,0.24), 0 2px 8px rgba(39,62,82,0.16)";
export const HUD_DARK_DESKTOP_ELEVATION =
  "inset 0 1px 0 rgba(255,255,255,0.16), inset 0 -1px 0 rgba(255,255,255,0.06), 0 1px 2px rgba(8,13,20,0.38), 0 2px 8px rgba(8,13,20,0.22)";
// The agent cards (glance card, console, compact and question surfaces) drop
// the top specular line. On a 22px corner at 1x the 1px inset highlight bends
// into a stepped crescent; the owner chose to remove it (2026-10-24). The
// lower rim and both drops stay. The dictation pill keeps the full material.
export const HUD_LIGHT_CARD_ELEVATION =
  "inset 0 -1px 0 rgba(70,94,115,0.12), 0 1px 2px rgba(39,62,82,0.24), 0 2px 8px rgba(39,62,82,0.16)";
export const HUD_DARK_CARD_ELEVATION =
  "inset 0 -1px 0 rgba(255,255,255,0.06), 0 1px 2px rgba(8,13,20,0.38), 0 2px 8px rgba(8,13,20,0.22)";
export const HUD_LIGHT_PERSISTENT_CONTROL_HIGHLIGHT =
  "inset 0 1px 0 rgba(255,255,255,0.92)";
export const HUD_DARK_PERSISTENT_CONTROL_HIGHLIGHT =
  "inset 0 1px 0 rgba(255,255,255,0.16)";
export const HUD_LIGHT_DESKTOP_BORDER = "rgba(78, 105, 128, 0.32)";
export const HUD_DARK_DESKTOP_BORDER = "rgba(255, 255, 255, 0.1)";

export const HUD_LIGHT_DESKTOP_PALETTE: HudDesktopPalette = {
  "--surface-overlay": HUD_LIGHT_DESKTOP_SURFACE,
  "--surface-control": "rgba(255, 255, 255, 0.48)",
  "--surface-raised": "rgba(255, 255, 255, 0.48)",
  "--surface-hover": "rgba(255, 255, 255, 0.66)",
  "--color-slate": "#314657",
  "--color-steel": "#667B8D",
  "--frost-muted": "#6C91B2",
  "--rim-specular": "rgba(255, 255, 255, 0.92)",
  "--border-hairline": "rgba(78, 105, 128, 0.22)",
  // The app's `--divider` is a 25% white etch tuned for translucent blue glass;
  // on this near-white desktop surface it is invisible. Steel-tinted, and
  // deliberately lighter than the hairline above (system.md border hierarchy).
  "--divider": "rgba(78, 105, 128, 0.18)",
  "--hud-desktop-border": HUD_LIGHT_DESKTOP_BORDER,
  "--hud-desktop-elevation": HUD_LIGHT_DESKTOP_ELEVATION,
  "--hud-card-elevation": HUD_LIGHT_CARD_ELEVATION,
  // Agent console reading tokens (agent-speak-fixes, Variant B frost panes).
  // HUD-only: this window applies its own palette, so they need no
  // globals.css theme block. Accent text measures about 4.9:1 on the light
  // overlay surface.
  "--border-accent": "rgba(85, 144, 196, 0.45)",
  "--hud-frost-deep": "#5590C4",
  "--hud-accent-text": "#3A6E9E",
  "--hud-pane": "rgba(255, 255, 255, 0.66)",
  "--hud-pane-recessed": "rgba(78, 105, 128, 0.06)",
  "--hud-pane-rim": "rgba(255, 255, 255, 0.92)",
  "--hud-wash": "rgba(127, 178, 222, 0.20)",
  "--hud-user-tint": "rgba(127, 178, 222, 0.13)",
  "--hud-code-bg": "rgba(78, 105, 128, 0.08)",
};

export const HUD_DARK_DESKTOP_PALETTE: HudDesktopPalette = {
  "--surface-overlay": HUD_DARK_DESKTOP_SURFACE,
  "--surface-control": "rgba(255, 255, 255, 0.09)",
  "--surface-raised": "rgba(255, 255, 255, 0.09)",
  "--surface-hover": "rgba(255, 255, 255, 0.13)",
  "--color-slate": "#E9F1F8",
  "--color-steel": "#A6B9CC",
  "--frost-muted": "#7FA3C9",
  "--rim-specular": "rgba(255, 255, 255, 0.16)",
  "--border-hairline": "rgba(255, 255, 255, 0.1)",
  // Pinned so the dark divider cannot drift with the app token.
  "--divider": "rgba(255, 255, 255, 0.12)",
  "--hud-desktop-border": HUD_DARK_DESKTOP_BORDER,
  "--hud-desktop-elevation": HUD_DARK_DESKTOP_ELEVATION,
  "--hud-card-elevation": HUD_DARK_CARD_ELEVATION,
  // Agent console reading tokens; see the light palette. Accent text measures
  // about 8.9:1 on the dark overlay surface. The pane rim is dimmer than
  // `--rim-specular` (0.16), which reads as a bright line on every pane.
  "--border-accent": "rgba(127, 178, 222, 0.38)",
  "--hud-frost-deep": "#6BA3D6",
  "--hud-accent-text": "#93C0E6",
  "--hud-pane": "rgba(255, 255, 255, 0.055)",
  "--hud-pane-recessed": "rgba(0, 0, 0, 0.22)",
  "--hud-pane-rim": "rgba(255, 255, 255, 0.07)",
  "--hud-wash": "rgba(127, 178, 222, 0.13)",
  "--hud-user-tint": "rgba(127, 178, 222, 0.10)",
  "--hud-code-bg": "rgba(0, 0, 0, 0.28)",
};
