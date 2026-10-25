import { LogicalSize, LogicalPosition } from "@tauri-apps/api/dpi";
import { currentMonitor } from "@tauri-apps/api/window";

export const SNAP_THRESHOLD = 30;
const WINDOW_WIDTH = 160;
const WINDOW_HEIGHT = 80;
// PILL_WIDTH is the max compact status width; positioning math uses it so
// blocked and Claude attention copy always fit.
export const PILL_WIDTH = 120;
export const PILL_WIDTH_COMPACT = 80; // 32px bar content + 24px padding each side (2.2:1 pill ratio)
// The stamped vessel: 22px pad + 16px profile stamp + 8px gap + 32px bars +
// 22px pad. Strictly inside PILL_WIDTH (120), so MODE_GEOMETRY, PAD_X/PAD_Y,
// the 160x80 window, and every persisted anchor transform are untouched.
// Width is decided by STAMP PRESENCE, never by dictation state, so the
// recording -> processing crossfade never resizes the pill.
export const PILL_WIDTH_PROFILE = 100;
// Gap between the stamp and the living element it precedes.
export const PILL_PROFILE_GAP = 8;
export const PILL_HEIGHT = 36;
const PAD_X = (WINDOW_WIDTH - PILL_WIDTH) / 2;
const PAD_Y = (WINDOW_HEIGHT - PILL_HEIGHT) / 2;
export const RECORDING_CONTROL_SEED_W = 36;
export const RECORDING_CONTROL_SEED_H = 10;
export const RECORDING_CONTROL_LAUNCHER_W = 80;
export const RECORDING_CONTROL_ACTIVE_W = 112;
export const RECORDING_CONTROL_NATIVE_PAD = 8;
/**
 * The outer drop shadow's offset-y and blur, in logical px, for BOTH themes.
 *
 * These are named because the native region has to contain them. A CSS blur of
 * B reaches about B/2 past the shape, so the shadow's extent past the card is
 * `offsetY + blur / 2` downward, `blur / 2` sideways, and `blur / 2 - offsetY`
 * upward. `HUD_REGION_EDGE_BLEED` must cover the largest of those, or the
 * unantialiased Win32 region cuts a curve straight through dense shadow and the
 * corners read as stair-stepped. See `HUD_REGION_EDGE_BLEED`.
 *
 * Was `0 5px 14px`, which reached 12px below the card against a 1px bleed: that
 * is the whole of the "jagged bottom-left / bottom-right corners, top corners
 * fine" report. Top corners looked clean because the same shadow only reached
 * 2px upward, which the old bleed nearly covered.
 */
export const HUD_ELEVATION_OFFSET_Y = 2;
export const HUD_ELEVATION_BLUR = 8;
/** How far the elevation paints beyond the card on its longest side. */
export const HUD_ELEVATION_EXTENT =
  HUD_ELEVATION_OFFSET_Y + HUD_ELEVATION_BLUR / 2;

// One fixed backing window for every persistent-control state. The native
// region is compact at rest and expands only to this pill-sized surface.
export const HUD_CONTROL_WINDOW_W = 132;
export const HUD_CONTROL_WINDOW_H =
  RECORDING_CONTROL_NATIVE_PAD +
  PILL_HEIGHT +
  RECORDING_CONTROL_NATIVE_PAD; // 52
// The dedicated permanent-control HWND never hosts the 112px active controls.
// Its largest painted state is the 80x36 launcher, with two pixels of
// transparent layout pad inside a fixed, unshaped native window.
export const DEDICATED_RECORDING_CONTROL_CONTENT_PAD = 2;
export const DEDICATED_RECORDING_CONTROL_WINDOW_W =
  RECORDING_CONTROL_LAUNCHER_W +
  DEDICATED_RECORDING_CONTROL_CONTENT_PAD * 2;
export const DEDICATED_RECORDING_CONTROL_WINDOW_H =
  PILL_HEIGHT + DEDICATED_RECORDING_CONTROL_CONTENT_PAD * 2;

export const PREVIEW_WIDTH = 360;
export const PREVIEW_HEIGHT = 192;
export const PREVIEW_CARD_W = 320;
export const PREVIEW_CARD_H = 160;
export const PREVIEW_CARD_RADIUS = 24;
export const PREVIEW_PAD_X = (PREVIEW_WIDTH - PREVIEW_CARD_W) / 2;
export const PREVIEW_PAD_Y = (PREVIEW_HEIGHT - PREVIEW_CARD_H) / 2;
export const PREVIEW_FONT_SIZE = 14;
export const PREVIEW_LINE_HEIGHT = 1.46;
export const PREVIEW_COMPACT_LINE_COUNT = 3;
export const PREVIEW_COMPACT_WELL_H =
  PREVIEW_FONT_SIZE * PREVIEW_LINE_HEIGHT * PREVIEW_COMPACT_LINE_COUNT;
export const PREVIEW_BOTTOM_THRESHOLD = 4;
export const PREVIEW_EXPANDED_WIDTH = 560;
export const PREVIEW_EXPANDED_CARD_W = 520;
export const PREVIEW_EXPANDED_MIN_CARD_H = 360;
export const PREVIEW_EXPANDED_SIDE_MARGIN = 24;
export const PREVIEW_EXPANDED_TOP_MARGIN = 24;
export const PREVIEW_EXPANDED_BOTTOM_MARGIN = 24;
const PREVIEW_EXPANDED_WORK_AREA_VERTICAL_RESERVE =
  PREVIEW_EXPANDED_TOP_MARGIN +
  PREVIEW_EXPANDED_BOTTOM_MARGIN +
  (PREVIEW_HEIGHT - PREVIEW_CARD_H);
export const PREVIEW_CARD_NON_TEXT_H = PREVIEW_CARD_H - PREVIEW_COMPACT_WELL_H;

// --- Claude HUD panel constants (Task 2.2) ---
// Exported so Task 2.3 (state-machine wiring) can consume them without re-declaring.
//
// The compact family (`panel`) still uses a fixed WebView big enough to hold
// every compact footprint (identity pill, rail pill, permission pill).
// PANEL_CARD_W/H and their pads are only the unmeasured fallback; the live
// painted footprint is centered inside this fixed window by `hudModeGeometry`.
export const PANEL_WIDTH = 560;
// The pill and the glance card share one resting window (the glance card's:
// RESPONSE_MIN_H + 2 * QUESTION_PAD_Y), so switching between them or
// promoting a summary response next to a plain one never resizes it.
export const PANEL_HEIGHT = 252;
export const PANEL_CARD_W = 320;
export const PANEL_CARD_H = 168;
export const PANEL_PAD_X = (PANEL_WIDTH - PANEL_CARD_W) / 2;   // 20
export const PANEL_PAD_Y = (PANEL_HEIGHT - PANEL_CARD_H) / 2;
// `QUESTION_CARD_W` (360) and `QUESTION_CARD_H` (320) are retired (design
// decisions 16 and 18). One fixed height served BOTH the question card and the
// expanded response, and both take their natural height now, so keeping it
// would silently pin one of them. The shared width is `CLAUDE_CARD_W`; the
// height comes from the one measured-card path below. These two pads are the
// card's margin inside its window, and because each card window is now derived
// as card + 2 * pad, the pad IS the centered offset (it used to disagree with
// true centering by 40px, which is what put the frost rectangle beside the
// card rather than around it).
export const QUESTION_PAD_X = 20;
export const QUESTION_PAD_Y = 16;
export const PILL_SPRING = { type: "spring" as const, damping: 20, stiffness: 300 };
/** The glance card's grow from the pill: PILL_SPRING's stiffness, critically
 *  damped. The underdamped spring overshot the card's width by about 25px
 *  and read as the card opening too big, then settling (owner, 2026-10-24). */
export const GLANCE_SPRING = { type: "spring" as const, damping: 35, stiffness: 300 };

// --- Shared Claude card geometry (Task 1.2, design.md Reference Code section G
// and the painted-footprint table) ---
// These are **painted card footprints** (the measured rect the Win32 region is
// built from), not WebView sizes. The WebView is always larger than the
// footprint; native geometry work (Task 3.1/3.2) consumes these, not the other
// way around. Since Task 3.1 these ARE the live geometry for both card
// surfaces: `MODE_GEOMETRY` derives `question` and `response` from
// `CLAUDE_CARD_W` plus the measured card height, clamped by the per-surface
// bounds below.
export const CLAUDE_CARD_W = 520;
export const CLAUDE_CARD_RADIUS = 22;
export const RESPONSE_MIN_H = 220;
export const RESPONSE_MAX_H = 720;
export const QUESTION_MIN_H = 240;
export const QUESTION_MAX_H = 620;
export const CLAUDE_CARD_WORK_AREA_RESERVE = 48;

// --- Glance card and centered reading console (agent-speak-centered-console
// design.md) ---
// The glance card is the written summary at the pill: narrower than the old
// shared reading width, natural height. Expanding moves the reply into the
// console, a reading surface that opens centered on the HUD's display. Its
// size (edge and corner resize) and its offset from the center (dragged by
// the identity row, snapping back to center on either axis) are the user's,
// each remembered per display. Resizing keeps the console's own center.
export const GLANCE_CARD_W = 440;
export const CONSOLE_DEFAULT_W = 860;
export const CONSOLE_DEFAULT_H = 630;
export const CONSOLE_MIN_W = 640;
export const CONSOLE_MIN_H = 420;
/** Kept clear of the work-area edges when clamping the console. */
export const CONSOLE_WORK_AREA_MARGIN = 24;
/** The console's transparent window pad (room for the elevation shadow). */
export const CONSOLE_PAD_X = 20;
export const CONSOLE_PAD_Y = 16;
const CONSOLE_SIZE_STORAGE_KEY = "hud-console-size";
const CONSOLE_OFFSET_STORAGE_KEY = "hud-console-offset";

// Compact family.
export const CLAUDE_PILL_H = 48;
export const CLAUDE_PILL_MIN_W = 200;
// Give session names room while keeping short labels content-fit.
export const CLAUDE_PILL_MAX_W = 520;
export const CLAUDE_PILL_RADIUS = 24;
// Replies share the expanded reading width and grow to the monitor ceiling.
export const CLAUDE_RAIL_PILL_W = 520;
export const CLAUDE_RAIL_PILL_RADIUS = 20;
export const CLAUDE_RAIL_MIN_H = 30;
export const CLAUDE_RAIL_MAX_H = RESPONSE_MAX_H - 150;

// Permission pill footprint (design.md painted-footprint table): radius 20,
// natural height, min 74, max 128. The width tracks CLAUDE_RAIL_PILL_W so the
// two compact surfaces stay the same object on screen.
export const CLAUDE_PERMISSION_PILL_W = CLAUDE_RAIL_PILL_W;
export const CLAUDE_PERMISSION_PILL_RADIUS = 20;
export const CLAUDE_PERMISSION_MIN_H = 74;
export const CLAUDE_PERMISSION_MAX_H = 128;

/** The painted ceiling for a Claude card surface: its own max, further clamped
 *  by the monitor work area less the reserve (design decision 18). Shared by
 *  the expanded response and, from Task 2.3, the question card. */
/** The Claude surfaces that report a painted footprint through the one shared
 *  measurement routine. `response` and `question` are the two surfaces of the
 *  one card object: they share a width and differ only in their height bounds
 *  (decision 18). `panel` (the compact identity/rail pill) and `permission`
 *  keep their own footprints, and report through the same routine only so
 *  Task 3.2 can shape their native region from the painted rect rather than
 *  from the WebView. */
export type ClaudeCardSurface =
  | "response"
  // The glance card. Its own surface, not "response": a console measurement
  // still current while collapsing to the pill must never size its window.
  | "summary"
  | "question"
  | "panel"
  | "permission";

/** The painted rect of whichever Claude card is mounted. Exactly one card is
 *  ever on screen, so this is a single value, tagged with the surface that
 *  produced it so a stale measurement can never size the other surface. */
export interface MeasuredClaudeCard {
  surface: ClaudeCardSurface;
  width: number;
  height: number;
  /** Reply surfaces only: the tallest window this visible surface has had.
   *  The window never shrinks below it until the surface hides, so a card
   *  getting shorter (a Sent rail, a shorter final, an edit) or a pill/glance
   *  switch makes no native call. */
  retainedWindowH?: number;
  /** Reply surfaces only: how much taller the card can still grow while it is
   *  on screen (the glance card's reply field filling to its five-line cap).
   *  The window is sized for that height up front, so typing never resizes
   *  it (owner, 2026-10-25). */
  growthReserveH?: number;
  /** The painted corner radius of that rect, for the surfaces whose radius is
   *  not fixed by their mode (the compact pill is 18 at rest and 20 with a
   *  rail open). Omitted means "the mode's own radius". */
  radius?: number;
}

/**
 * Reply surfaces (the compact pill, the compact question pill and the glance
 * card) keep a fixed native window while they are on screen (owner,
 * 2026-10-24). Their card animates in CSS inside it, pinned to the window's
 * bottom edge, so a capture opening the reply row, text wrapping onto a new
 * line or a draft landing makes no native call. The window grows only when
 * the card outgrows it, in whole steps, so even long dictation resizes it a
 * handful of times rather than once per line. The empty area above the card
 * is click-through (`useHudClickThrough`).
 */
export const REPLY_SURFACE_WINDOW_STEP = 160;
/** The card's gap to the window's bottom edge (room for the elevation). */
export const REPLY_SURFACE_BOTTOM_PAD = QUESTION_PAD_Y;

/** The window height for a reply surface card: the mode's resting window,
 *  plus whole steps once the card (and its pads) no longer fits, and never
 *  less than the window this visible surface already has (`retainedH`). */
export function replySurfaceWindowHeight(
  cardH: number,
  baseWindowH: number,
  retainedH = 0,
): number {
  const needed = cardH + REPLY_SURFACE_BOTTOM_PAD * 2;
  const over = Math.max(0, needed - baseWindowH);
  const stepped =
    baseWindowH + Math.ceil(over / REPLY_SURFACE_WINDOW_STEP) * REPLY_SURFACE_WINDOW_STEP;
  // Never step past the card ceiling's own window, so it stays on screen.
  const ceilingWindowH =
    claudeCardCeiling(0, RESPONSE_MAX_H) + REPLY_SURFACE_BOTTOM_PAD * 2;
  return Math.max(needed, Math.min(Math.max(stepped, retainedH), ceilingWindowH));
}

export function claudeCardCeiling(minHeight: number, maxHeight: number): number {
  const available =
    typeof window !== "undefined" ? (window.screen?.availHeight ?? 0) : 0;
  if (available <= 0) return maxHeight;
  return Math.max(
    minHeight,
    Math.min(maxHeight, available - CLAUDE_CARD_WORK_AREA_RESERVE),
  );
}

export type HudGeometryMode =
  | "seed"
  | "launcher"
  | "arming"
  | "active"
  | "pill"
  | "preview"
  | "question"
  | "summary"
  | "response"
  | "panel"
  | "permission";

/**
 * The expanded control geometries share one `132x52` backing window.
 * `launcher -> arming -> active` therefore issues no native bounds mutation.
 */
const HUD_EXPANDED_CONTROL_MODES: ReadonlySet<HudGeometryMode> = new Set([
  "launcher",
  "arming",
  "active",
]);

export function isHudExpandedControlMode(mode: HudGeometryMode | null): boolean {
  return mode !== null && HUD_EXPANDED_CONTROL_MODES.has(mode);
}

/**
 * The control family: every mode whose card shares the fixed pill backing
 * surface.
 */
export function isHudControlFamilyMode(mode: HudGeometryMode): boolean {
  return mode === "seed" || isHudExpandedControlMode(mode);
}

export interface HudPoint {
  x: number;
  y: number;
}

interface HudGeometry {
  size: LogicalSize;
  position: LogicalPosition | null;
  previewCardSize?: {
    width: number;
    height: number;
  };
}

export const MODE_GEOMETRY: Record<
  HudGeometryMode,
  { windowW: number; windowH: number; cardW: number; cardH: number; padX: number; padY: number }
> = {
  seed: {
    windowW: HUD_CONTROL_WINDOW_W,
    windowH: HUD_CONTROL_WINDOW_H,
    cardW: RECORDING_CONTROL_SEED_W,
    cardH: RECORDING_CONTROL_SEED_H,
    padX: (HUD_CONTROL_WINDOW_W - RECORDING_CONTROL_SEED_W) / 2, // 48
    padY:
      HUD_CONTROL_WINDOW_H -
      RECORDING_CONTROL_NATIVE_PAD -
      RECORDING_CONTROL_SEED_H, // 34
  },
  // Expanded control modes differ only in card width. Their bounds and 8px
  // vertical inset are identical, so hover and HUD-origin state changes never
  // resize or reposition the backing WebView.
  launcher: {
    windowW: HUD_CONTROL_WINDOW_W,
    windowH: HUD_CONTROL_WINDOW_H,
    cardW: RECORDING_CONTROL_LAUNCHER_W,
    cardH: PILL_HEIGHT,
    padX: (HUD_CONTROL_WINDOW_W - RECORDING_CONTROL_LAUNCHER_W) / 2, // 26
    padY: RECORDING_CONTROL_NATIVE_PAD,
  },
  // Arming is the active window, exactly. Entering it on click is what makes
  // the expansion immediate, and holding those identical numbers is what makes
  // the authoritative confirmation a pure content crossfade: the reconciler
  // compares applied numbers, so arming -> active issues no native mutation.
  // Do NOT deduplicate this row onto `active`; the identity is the point.
  arming: {
    windowW: HUD_CONTROL_WINDOW_W,
    windowH: HUD_CONTROL_WINDOW_H,
    cardW: RECORDING_CONTROL_ACTIVE_W,
    cardH: PILL_HEIGHT,
    padX: (HUD_CONTROL_WINDOW_W - RECORDING_CONTROL_ACTIVE_W) / 2, // 10
    padY: RECORDING_CONTROL_NATIVE_PAD,
  },
  active: {
    windowW: HUD_CONTROL_WINDOW_W,
    windowH: HUD_CONTROL_WINDOW_H,
    cardW: RECORDING_CONTROL_ACTIVE_W,
    cardH: PILL_HEIGHT,
    padX: (HUD_CONTROL_WINDOW_W - RECORDING_CONTROL_ACTIVE_W) / 2, // 10
    padY: RECORDING_CONTROL_NATIVE_PAD,
  },
  pill: {
    windowW: WINDOW_WIDTH,
    windowH: WINDOW_HEIGHT,
    cardW: PILL_WIDTH,
    cardH: PILL_HEIGHT,
    padX: PAD_X,
    padY: PAD_Y,
  },
  preview: {
    windowW: PREVIEW_WIDTH,
    windowH: PREVIEW_HEIGHT,
    cardW: PREVIEW_CARD_W,
    cardH: PREVIEW_CARD_H,
    padX: PREVIEW_PAD_X,
    padY: PREVIEW_PAD_Y,
  },
  // The compact Claude family's fixed WebView and unmeasured fallback
  // footprint. A valid panel measurement replaces only the footprint and pads
  // in `hudModeGeometry`; the window itself never changes.
  panel: {
    windowW: PANEL_WIDTH,
    windowH: PANEL_HEIGHT,
    cardW: PANEL_CARD_W,
    cardH: PANEL_CARD_H,
    padX: PANEL_PAD_X,
    padY: PANEL_PAD_Y,
  },
  // The two card surfaces. These rows carry the RESTING footprint only: the
  // shared width and each surface's own minimum height. The live height comes
  // from the one measured-card path (`hudModeGeometry`), so nothing here pins
  // a card that is content-measured. Window = card + 2 * pad in both axes, so
  // the pad is exactly the centered offset the HUD root lays the card out at.
  question: {
    windowW: CLAUDE_CARD_W + QUESTION_PAD_X * 2,
    windowH: QUESTION_MIN_H + QUESTION_PAD_Y * 2,
    cardW: CLAUDE_CARD_W,
    cardH: QUESTION_MIN_H,
    padX: QUESTION_PAD_X,
    padY: QUESTION_PAD_Y,
  },
  // The centered console at its default size. `resolveHudGeometry` replaces
  // the size with the remembered one for the display and centers it; this row
  // is the fallback when no monitor is known.
  response: {
    windowW: CONSOLE_DEFAULT_W + CONSOLE_PAD_X * 2,
    windowH: CONSOLE_DEFAULT_H + CONSOLE_PAD_Y * 2,
    cardW: CONSOLE_DEFAULT_W,
    cardH: CONSOLE_DEFAULT_H,
    padX: CONSOLE_PAD_X,
    padY: CONSOLE_PAD_Y,
  },
  // The glance card: measured natural height at the pill, centered in the
  // pill's own window so the two reply surfaces share one native window.
  summary: {
    windowW: PANEL_WIDTH,
    windowH: PANEL_HEIGHT,
    cardW: GLANCE_CARD_W,
    cardH: RESPONSE_MIN_H,
    padX: (PANEL_WIDTH - GLANCE_CARD_W) / 2,
    padY: QUESTION_PAD_Y,
  },
  // The permission ask. It used to render inside the 160x80 dictation `pill`
  // window, which is narrower than its own footprint, so the OS clip could
  // never describe it. It gets the same treatment as the card surfaces: window
  // = footprint + 2 * pad, height from the one measured-card path.
  permission: {
    windowW: CLAUDE_PERMISSION_PILL_W + QUESTION_PAD_X * 2,
    windowH: CLAUDE_PERMISSION_MIN_H + QUESTION_PAD_Y * 2,
    cardW: CLAUDE_PERMISSION_PILL_W,
    cardH: CLAUDE_PERMISSION_MIN_H,
    padX: QUESTION_PAD_X,
    padY: QUESTION_PAD_Y,
  },
};

export interface HudRuntimeViewport {
  mode: HudGeometryMode;
  width: number;
  height: number;
  /**
   * The browser starts at the production maximum, then settles to the
   * measured-card viewport published by the exact-route HUD root.
   */
  settleFromRoot?: true;
}

/**
 * Project live HUD geometry into a serializable browser-QA bootstrap.
 * Dynamic cards start at their existing production ceiling so their natural
 * painted height can be measured without being constrained by the fallback
 * minimum. The runner then settles to `hudModeGeometry`'s exact result.
 */
export function hudRuntimeViewportForMode(
  mode: HudGeometryMode,
): HudRuntimeViewport {
  const geometry = MODE_GEOMETRY[mode];
  const maximumCardHeight =
    mode === "summary" || mode === "panel"
      ? RESPONSE_MAX_H
      : mode === "question"
        ? QUESTION_MAX_H
        : mode === "permission"
          ? CLAUDE_PERMISSION_MAX_H
          : null;
  return {
    mode,
    width: geometry.windowW,
    height:
      maximumCardHeight === null
        ? geometry.windowH
        : maximumCardHeight + geometry.padY * 2,
    ...(maximumCardHeight === null ? {} : { settleFromRoot: true as const }),
  };
}

type HudModeGeometry = (typeof MODE_GEOMETRY)[HudGeometryMode];

/** Which measured surface owns each card geometry mode, and the bounds its
 *  height is clamped to (decision 18: separate bounds, shared width). */
const CLAUDE_CARD_MODE_BOUNDS: Partial<
  Record<
    HudGeometryMode,
    { surface: ClaudeCardSurface; minHeight: number; maxHeight: number }
  >
> = {
  summary: {
    surface: "summary",
    minHeight: 0,
    maxHeight: RESPONSE_MAX_H,
  },
  question: {
    surface: "question",
    minHeight: QUESTION_MIN_H,
    maxHeight: QUESTION_MAX_H,
  },
  permission: {
    surface: "permission",
    minHeight: CLAUDE_PERMISSION_MIN_H,
    maxHeight: CLAUDE_PERMISSION_MAX_H,
  },
};

/**
 * The live geometry for a mode, with the measured painted card folded in.
 *
 * This is the single place a Claude card's dynamic height becomes a number.
 * Everything downstream (window size, window position, the pill-anchor
 * round-trip, and Task 3.2's native region) goes through it, so a card can
 * never be sized one way for the CSS box and another for the OS.
 *
 * It follows the shape of the expanded-preview path (`resolveHudGeometry`'s
 * `naturalTextHeight` / `previewCardSize`): measure the painted surface, clamp
 * it, hand it to the native bounds. There is deliberately no second mechanism.
 *
 * A measurement from the OTHER surface is ignored: exactly one card is mounted
 * at a time, and a leftover response measurement must not size a question card
 * during the frame after a swap. A missing measurement falls back to the
 * surface's minimum, never to `restricted: false`-style "whatever the window
 * is".
 */
export function hudModeGeometry(
  mode: HudGeometryMode,
  measuredCard: MeasuredClaudeCard | null = null,
): HudModeGeometry {
  const base = MODE_GEOMETRY[mode];
  // Read from whichever surface measured last: a pill/glance switch keeps the
  // window it had before the new surface's first measurement lands.
  const retainedH = measuredCard?.retainedWindowH ?? 0;
  if (mode === "panel") {
    const cardW =
      measuredCard !== null && measuredCard.surface === "panel"
        ? Math.round(measuredCard.width)
        : 0;
    const cardH =
      measuredCard !== null && measuredCard.surface === "panel"
        ? Math.round(measuredCard.height)
        : 0;
    const validMeasurement =
      Number.isFinite(cardW) &&
      Number.isFinite(cardH) &&
      cardW > 0 &&
      cardH > 0 &&
      cardW <= base.windowW &&
      cardH <= claudeCardCeiling(0, RESPONSE_MAX_H);
    if (!validMeasurement) {
      if (retainedH <= base.windowH) return base;
      return { ...base, windowH: retainedH, padY: retainedH - base.cardH - REPLY_SURFACE_BOTTOM_PAD };
    }
    // `padY` is the card's TOP offset: the card is pinned to the bottom pad,
    // so the pill-anchor round-trip depends on the window height alone.
    const windowH = replySurfaceWindowHeight(cardH, base.windowH, retainedH);
    return {
      ...base,
      windowH,
      cardW,
      cardH,
      padX: (base.windowW - cardW) / 2,
      padY: windowH - cardH - REPLY_SURFACE_BOTTOM_PAD,
    };
  }
  const bounds = CLAUDE_CARD_MODE_BOUNDS[mode];
  if (bounds === undefined) return base;
  const ceiling = claudeCardCeiling(bounds.minHeight, bounds.maxHeight);
  const natural =
    measuredCard !== null && measuredCard.surface === bounds.surface
      ? Math.round(measuredCard.height)
      : 0;
  const cardH =
    natural > 0
      ? Math.max(bounds.minHeight, Math.min(ceiling, natural))
      : base.cardH;
  if (mode === "summary") {
    // The glance card is a reply surface: stepped window, bottom-pinned card,
    // with room above it for the card at its tallest.
    const reserve =
      natural > 0 ? Math.max(0, Math.round(measuredCard?.growthReserveH ?? 0)) : 0;
    const windowH = replySurfaceWindowHeight(
      Math.min(ceiling, cardH + reserve),
      base.windowH,
      retainedH,
    );
    return { ...base, cardH, windowH, padY: windowH - cardH - REPLY_SURFACE_BOTTOM_PAD };
  }
  if (cardH === base.cardH) return base;
  return { ...base, cardH, windowH: cardH + base.padY * 2 };
}

export interface HudWindowRegion {
  x: number;
  y: number;
  width: number;
  height: number;
  cornerRadius: number;
}

/**
 * How far every native HUD region extends past its painted card.
 *
 * `CreateRoundRectRgn` is a binary, unantialiased clip, so the region boundary
 * must fall where the surface has already faded to nothing. A card's own edge
 * needs only a pixel, but the elevation is a soft shadow that paints
 * `HUD_ELEVATION_EXTENT` px beyond the card - and a hard curve cut through a
 * soft gradient is exactly what made the bottom corners stair-step while the
 * top corners (which the downward-offset shadow barely reaches) stayed clean.
 *
 * Two bounds, both load-bearing:
 *   lower - must be >= HUD_ELEVATION_EXTENT, or the shadow gets cut again.
 *   upper - must be <= the smallest window pad across MODE_GEOMETRY, or the
 *           region would spill outside its own window and Windows would clip it
 *           back to the window rect, reintroducing the same hard cut. The
 *           binding pad is the expanded-control family's 8px padY.
 *
 * 8 satisfies both, and is already the pad the recording-control seed ships, so
 * every HUD surface now shares one rule instead of the old 8-vs-1 split.
 * `HUD.test.tsx` pins both bounds.
 */
export const HUD_REGION_EDGE_BLEED = 8;

/** Which measured surface paints the card for a Claude geometry mode. Every
 *  mode listed here ALWAYS paints a rounded card, so none of them may fall
 *  back to `restricted: false`. */
const CLAUDE_REGION_SURFACES: Partial<
  Record<HudGeometryMode, { surface: ClaudeCardSurface; radius: number }>
> = {
  panel: { surface: "panel", radius: CLAUDE_RAIL_PILL_RADIUS },
  question: { surface: "question", radius: CLAUDE_CARD_RADIUS },
  // `response` (the centered console) is absent: it is a persistent,
  // resizable surface in the clean Tauri family and installs no region.
  summary: { surface: "summary", radius: CLAUDE_CARD_RADIUS },
  permission: {
    surface: "permission",
    radius: CLAUDE_PERMISSION_PILL_RADIUS,
  },
};

/** The rounded region around a painted rect centered in its window, plus the
 *  1px antialias bleed. The HUD root centers every non-control presentation,
 *  and each card window is derived as footprint + 2 * pad, so "centered" and
 *  "at the pad" are the same number. */
function centeredHudRegion(
  windowW: number,
  windowH: number,
  paintedW: number,
  paintedH: number,
  cornerRadius: number,
): HudWindowRegion {
  const width = Math.round(paintedW);
  const height = Math.round(paintedH);
  // A card whose size has a different parity from its window centers on a
  // half pixel, and `CreateRoundRectRgn` has no antialiasing to spend on it:
  // the clip lands half a pixel inside the painted curve on one side, which
  // is what flattened the compact pill's ends into visible steps. Round the
  // region outward on both edges so the clip is always integral and never
  // cuts the card. Every already-even surface is byte-identical to before.
  const left = Math.floor((windowW - width) / 2) - HUD_REGION_EDGE_BLEED;
  const right = Math.ceil((windowW + width) / 2) + HUD_REGION_EDGE_BLEED;
  const top = Math.floor((windowH - height) / 2) - HUD_REGION_EDGE_BLEED;
  const bottom = Math.ceil((windowH + height) / 2) + HUD_REGION_EDGE_BLEED;
  const regionW = right - left;
  const regionH = bottom - top;
  return {
    x: left,
    y: top,
    width: regionW,
    height: regionH,
    // The bleed keeps the region concentric with the painted curve, so the
    // radius grows with it. Clamp to the native validator's own ceiling
    // (`corner_radius > width.min(height) / 2` is rejected in lib.rs): a card
    // measured mid-transition can be shorter than its own target radius
    // implies, and a rejected command aborts the whole mutation - region,
    // bounds, and show together.
    cornerRadius: Math.min(
      cornerRadius + HUD_REGION_EDGE_BLEED,
      Math.min(regionW, regionH) / 2,
    ),
  };
}

/**
 * Native paint/hit regions. The resting seed keeps its compact hit target.
 * Expanded control states share one rounded region around the maximum active
 * pill, so launcher -> arming -> active remains mutation-free while WebView2
 * can never expose the rectangular backing surface.
 *
 * Every Claude surface (the compact/rail pill, the permission pill, the
 * question card, and the expanded response) gets the same treatment, built
 * from the MEASURED painted card rect at that state's design.md radius. Before
 * this, those modes returned `null`, which the reconciler applies as
 * `restricted: false` - a full, unrounded, window-sized rectangle. That is
 * incident 7's mechanism: the OS clip described the
 * WebView instead of the painted control, so every pixel WebView2 repainted
 * inside it was a legitimate, presentable, hit-testable pixel. Hence the errant
 * frost rectangle beside the card and the dead hit band above the question.
 *
 * A stale measurement clips live UI, which is worse than an oversized region,
 * so a surface with no measurement for this frame takes `previousRegion` (the
 * region already applied for this same mode) and, failing that, the mode's
 * resting footprint - which is exactly the rect the bounds in the same command
 * describe, so it can never clip more than the window already does. It never
 * falls back to `restricted: false`.
 *
 * The Preview calculation remains available for geometry invariants and
 * dormant compatibility coverage. The presentation reconciler does not call
 * it for shipping Live Preview; Preview is in the clean Tauri family and never
 * installs an HRGN.
 */
export function hudWindowRegionForMode(
  mode: HudGeometryMode,
  measuredCard: MeasuredClaudeCard | null = null,
  previousRegion: HudWindowRegion | null = null,
  previewGeometry: {
    windowW: number;
    windowH: number;
    cardW: number;
    cardH: number;
  } | null = null,
): HudWindowRegion | null {
  if (mode === "preview") {
    const geometry = previewGeometry ?? MODE_GEOMETRY.preview;
    return centeredHudRegion(
      geometry.windowW,
      geometry.windowH,
      geometry.cardW,
      geometry.cardH,
      PREVIEW_CARD_RADIUS,
    );
  }
  const claude = CLAUDE_REGION_SURFACES[mode];
  if (claude !== undefined) {
    const geometry = hudModeGeometry(mode, measuredCard);
    const measured =
      measuredCard !== null &&
      measuredCard.surface === claude.surface &&
      measuredCard.width > 0 &&
      measuredCard.height > 0
        ? measuredCard
        : null;
    if (measured === null) {
      return (
        previousRegion ??
        centeredHudRegion(
          geometry.windowW,
          geometry.windowH,
          geometry.cardW,
          geometry.cardH,
          claude.radius,
        )
      );
    }
    // The card surfaces take their painted height from `hudModeGeometry`, the
    // one place a measured card height becomes a number, so the region and the
    // window bounds in this same command can never disagree. The compact
    // family keeps a fixed WebView, so its painted rect is the measurement.
    const fixedWebView = mode === "panel";
    return centeredHudRegion(
      geometry.windowW,
      geometry.windowH,
      fixedWebView ? measured.width : geometry.cardW,
      fixedWebView ? measured.height : geometry.cardH,
      measured.radius ?? claude.radius,
    );
  }
  // The control family is laid out from its own pad rather than centered, so it
  // cannot use `centeredHudRegion` - but it takes the same bleed. Before, the
  // seed used the 8px control pad and the expanded modes used a 1px bleed, and
  // that split is why the expanded launcher/active pill showed the same cut
  // bottom corners the Claude surfaces did.
  if (mode === "seed") {
    const geometry = MODE_GEOMETRY[mode];
    const height = geometry.cardH + HUD_REGION_EDGE_BLEED * 2;
    return {
      x: geometry.padX - HUD_REGION_EDGE_BLEED,
      y: geometry.padY - HUD_REGION_EDGE_BLEED,
      width: geometry.cardW + HUD_REGION_EDGE_BLEED * 2,
      height,
      cornerRadius: height / 2,
    };
  }
  if (isHudExpandedControlMode(mode)) {
    const geometry = MODE_GEOMETRY.active;
    return {
      x: geometry.padX - HUD_REGION_EDGE_BLEED,
      y: geometry.padY - HUD_REGION_EDGE_BLEED,
      width: geometry.cardW + HUD_REGION_EDGE_BLEED * 2,
      height: geometry.cardH + HUD_REGION_EDGE_BLEED * 2,
      cornerRadius: geometry.cardH / 2 + HUD_REGION_EDGE_BLEED,
    };
  }
  return null;
}

/** Convert the canonical top-left of the compact pill into a mode's window
 *  origin. `measuredCard` must be the SAME measurement used to recover the
 *  anchor afterwards, or a dragged card comes back at the wrong place. */
export function windowPositionFromPillAnchor(
  mode: HudGeometryMode,
  anchor: HudPoint,
  measuredCard: MeasuredClaudeCard | null = null,
): HudPoint {
  const geometry = hudModeGeometry(mode, measuredCard);
  return {
    x:
      anchor.x + PILL_WIDTH / 2 - geometry.cardW / 2 - geometry.padX,
    y: anchor.y + PILL_HEIGHT - geometry.cardH - geometry.padY,
  };
}

/** Recover the same canonical compact-pill anchor after dragging any HUD mode. */
export function pillAnchorFromWindowPosition(
  mode: HudGeometryMode,
  position: HudPoint,
  measuredCard: MeasuredClaudeCard | null = null,
): HudPoint {
  const geometry = hudModeGeometry(mode, measuredCard);
  return {
    x:
      position.x + geometry.padX + geometry.cardW / 2 - PILL_WIDTH / 2,
    y: position.y + geometry.padY + geometry.cardH - PILL_HEIGHT,
  };
}

/** Place the fixed dedicated launcher-sized HWND around the canonical pill. */
export function dedicatedRecordingControlPositionFromPillAnchor(
  anchor: HudPoint,
): HudPoint {
  return {
    x:
      anchor.x +
      PILL_WIDTH / 2 -
      DEDICATED_RECORDING_CONTROL_WINDOW_W / 2,
    y:
      anchor.y +
      PILL_HEIGHT -
      DEDICATED_RECORDING_CONTROL_WINDOW_H +
      DEDICATED_RECORDING_CONTROL_CONTENT_PAD,
  };
}

/** Recover the canonical compact-pill anchor after a dedicated-window drag. */
export function pillAnchorFromDedicatedRecordingControlPosition(
  position: HudPoint,
): HudPoint {
  return {
    x:
      position.x +
      DEDICATED_RECORDING_CONTROL_WINDOW_W / 2 -
      PILL_WIDTH / 2,
    y:
      position.y +
      DEDICATED_RECORDING_CONTROL_WINDOW_H -
      DEDICATED_RECORDING_CONTROL_CONTENT_PAD -
      PILL_HEIGHT,
  };
}

/**
 * The card's on-screen rect for a mode at a given pill anchor. The expanded
 * control window reserves the tooltip band without moving the pill, so every
 * expanded mode must produce the same pill rect for a given anchor (the same
 * bottom edge and the same center). Asserted in the tests.
 */
export function pillRectFromAnchor(
  mode: HudGeometryMode,
  anchor: HudPoint,
  measuredCard: MeasuredClaudeCard | null = null,
): { x: number; y: number; width: number; height: number } {
  const geometry = hudModeGeometry(mode, measuredCard);
  const position = windowPositionFromPillAnchor(mode, anchor, measuredCard);
  return {
    x: position.x + geometry.padX,
    y: position.y + geometry.padY,
    width: geometry.cardW,
    height: geometry.cardH,
  };
}

function normalizeScaleFactor(scaleFactor: number): number {
  return Number.isFinite(scaleFactor) && scaleFactor > 0 ? scaleFactor : 1;
}

/** A console card size in logical px. */
export interface ConsoleSize {
  width: number;
  height: number;
}

/** A live resize request, bound to the display it was made on. */
export interface ConsoleSizeRequest extends ConsoleSize {
  displayKey: string;
}

/** A work area in logical px. */
export interface HudWorkArea {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** The largest card that fits the work area with its margin and window pad. */
export function consoleMaxSize(workArea: HudWorkArea): ConsoleSize {
  return {
    width: Math.max(
      1,
      Math.floor(
        workArea.width - CONSOLE_WORK_AREA_MARGIN * 2 - CONSOLE_PAD_X * 2,
      ),
    ),
    height: Math.max(
      1,
      Math.floor(
        workArea.height - CONSOLE_WORK_AREA_MARGIN * 2 - CONSOLE_PAD_Y * 2,
      ),
    ),
  };
}

/** Clamp a requested console size to [minimum, work area]. The work area wins
 *  over the minimum on a display too small to hold 640x420. */
export function clampConsoleSize(
  size: ConsoleSize,
  workArea: HudWorkArea,
): ConsoleSize {
  const max = consoleMaxSize(workArea);
  const clamp = (value: number, min: number, upper: number) =>
    Math.round(Math.min(upper, Math.max(min, Number.isFinite(value) ? value : min)));
  return {
    width: clamp(size.width, CONSOLE_MIN_W, max.width),
    height: clamp(size.height, CONSOLE_MIN_H, max.height),
  };
}

/** How far the user dragged the console's center from the work area's. */
export interface ConsoleOffset {
  dx: number;
  dy: number;
}

/** Keep a console window of this size inside the work area. */
function clampConsolePosition(
  position: { x: number; y: number },
  size: { width: number; height: number },
  workArea: HudWorkArea,
): { x: number; y: number } {
  const clampAxis = (value: number, start: number, room: number, extent: number) =>
    room <= extent ? start + (room - extent) / 2 : Math.min(start + room - extent, Math.max(start, value));
  return {
    x: Math.round(clampAxis(position.x, workArea.x, workArea.width, size.width)),
    y: Math.round(clampAxis(position.y, workArea.y, workArea.height, size.height)),
  };
}

/** The console window around a clamped card: centered in the work area, moved
 *  by the user's offset and kept on screen. */
export function consoleWindowRect(
  size: ConsoleSize,
  workArea: HudWorkArea,
  offset: ConsoleOffset = { dx: 0, dy: 0 },
): { x: number; y: number; width: number; height: number; card: ConsoleSize } {
  const card = clampConsoleSize(size, workArea);
  const width = card.width + CONSOLE_PAD_X * 2;
  const height = card.height + CONSOLE_PAD_Y * 2;
  const position = clampConsolePosition(
    {
      x: workArea.x + (workArea.width - width) / 2 + offset.dx,
      y: workArea.y + (workArea.height - height) / 2 + offset.dy,
    },
    { width, height },
    workArea,
  );
  return { ...position, width, height, card };
}

/** Where a dragged console window goes: kept on screen, and snapped to the
 *  work area's center on each axis within `SNAP_THRESHOLD`. Returns the
 *  position and the offset to remember. */
export function draggedConsolePlacement(
  position: { x: number; y: number },
  size: { width: number; height: number },
  workArea: HudWorkArea,
): { x: number; y: number; offset: ConsoleOffset } {
  const centeredX = workArea.x + (workArea.width - size.width) / 2;
  const centeredY = workArea.y + (workArea.height - size.height) / 2;
  const snapped = {
    x: Math.abs(position.x - centeredX) <= SNAP_THRESHOLD ? centeredX : position.x,
    y: Math.abs(position.y - centeredY) <= SNAP_THRESHOLD ? centeredY : position.y,
  };
  const placed = clampConsolePosition(snapped, size, workArea);
  return {
    ...placed,
    offset: {
      dx: Math.round(placed.x - centeredX),
      dy: Math.round(placed.y - centeredY),
    },
  };
}

export type ConsoleResizeEdge = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

/**
 * The size a resize drag asks for. The console stays centered, so both edges
 * on an axis move together: the dragged edge follows the pointer exactly and
 * the opposite edge mirrors it.
 */
export function resizedConsoleSize(
  start: ConsoleSize,
  edge: ConsoleResizeEdge,
  deltaX: number,
  deltaY: number,
): ConsoleSize {
  const horizontal = edge.includes("e") ? 1 : edge.includes("w") ? -1 : 0;
  const vertical = edge.includes("s") ? 1 : edge.startsWith("n") ? -1 : 0;
  return {
    width: start.width + horizontal * deltaX * 2,
    height: start.height + vertical * deltaY * 2,
  };
}

/** A stable identity for a display: its name plus its physical rectangle, so
 *  two monitors that report the same name still remember separately. */
export function consoleDisplayKey(monitor: {
  name?: string | null;
  position: { x: number; y: number };
  size: { width: number; height: number };
}): string {
  return `${monitor.name ?? "display"}@${monitor.position.x},${monitor.position.y},${monitor.size.width}x${monitor.size.height}`;
}

function readConsoleSizes(): Record<string, ConsoleSize> {
  try {
    const raw = localStorage.getItem(CONSOLE_SIZE_STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return {};
    const sizes: Record<string, ConsoleSize> = {};
    for (const [key, value] of Object.entries(parsed)) {
      if (
        typeof value === "object" &&
        value !== null &&
        Number.isFinite((value as ConsoleSize).width) &&
        Number.isFinite((value as ConsoleSize).height)
      ) {
        sizes[key] = {
          width: (value as ConsoleSize).width,
          height: (value as ConsoleSize).height,
        };
      }
    }
    return sizes;
  } catch {
    return {};
  }
}

/** The size the user last left the console at on this display, if any. */
export function readConsoleSize(displayKey: string): ConsoleSize | null {
  return readConsoleSizes()[displayKey] ?? null;
}

/** Remember the console size for this display. */
export function saveConsoleSize(displayKey: string, size: ConsoleSize): void {
  try {
    localStorage.setItem(
      CONSOLE_SIZE_STORAGE_KEY,
      JSON.stringify({
        ...readConsoleSizes(),
        [displayKey]: { width: size.width, height: size.height },
      }),
    );
  } catch {
    // Storage unavailable: the console still opens at the default size.
  }
}

function readConsoleOffsets(): Record<string, ConsoleOffset> {
  try {
    const raw = localStorage.getItem(CONSOLE_OFFSET_STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return {};
    const offsets: Record<string, ConsoleOffset> = {};
    for (const [key, value] of Object.entries(parsed)) {
      if (
        typeof value === "object" &&
        value !== null &&
        Number.isFinite((value as ConsoleOffset).dx) &&
        Number.isFinite((value as ConsoleOffset).dy)
      ) {
        offsets[key] = { dx: (value as ConsoleOffset).dx, dy: (value as ConsoleOffset).dy };
      }
    }
    return offsets;
  } catch {
    return {};
  }
}

/** Where the user last left the console on this display, if they moved it. */
export function readConsoleOffset(displayKey: string): ConsoleOffset | null {
  return readConsoleOffsets()[displayKey] ?? null;
}

/** Remember the console's offset from center for this display. */
export function saveConsoleOffset(displayKey: string, offset: ConsoleOffset): void {
  try {
    localStorage.setItem(
      CONSOLE_OFFSET_STORAGE_KEY,
      JSON.stringify({
        ...readConsoleOffsets(),
        [displayKey]: { dx: offset.dx, dy: offset.dy },
      }),
    );
  } catch {
    // Storage unavailable: the console still opens centered.
  }
}

/** The display the HUD is on, in logical px, with its remembered-size key. */
export async function currentHudDisplay(): Promise<{
  displayKey: string;
  workArea: HudWorkArea;
} | null> {
  const monitor = await currentMonitor();
  if (!monitor) return null;
  const scale = normalizeScaleFactor(monitor.scaleFactor);
  const workArea = monitor.workArea ?? {
    position: monitor.position,
    size: monitor.size,
  };
  return {
    displayKey: consoleDisplayKey(monitor),
    workArea: {
      x: workArea.position.x / scale,
      y: workArea.position.y / scale,
      width: Math.max(1, workArea.size.width / scale),
      height: Math.max(1, workArea.size.height / scale),
    },
  };
}

export async function resolveHudGeometry(
  mode: HudGeometryMode,
  expandedPreview: boolean,
  naturalTextHeight: number,
  measuredCard: MeasuredClaudeCard | null = null,
  consoleRequest: ConsoleSizeRequest | null = null,
): Promise<HudGeometry> {
  // The console is the one surface placed by the display rather than the pill
  // anchor: the live resize request (same display only), else the size
  // remembered for this display, else the default, centered and then moved
  // by the offset remembered for this display. A drag saves the offset as it
  // moves, so a reconcile mid-drag lands where the drag already is.
  if (mode === "response") {
    const fallback = MODE_GEOMETRY.response;
    const display = await currentHudDisplay();
    if (display === null) {
      return {
        size: new LogicalSize(fallback.windowW, fallback.windowH),
        position: null,
      };
    }
    const requested =
      consoleRequest !== null && consoleRequest.displayKey === display.displayKey
        ? consoleRequest
        : (readConsoleSize(display.displayKey) ?? {
            width: CONSOLE_DEFAULT_W,
            height: CONSOLE_DEFAULT_H,
          });
    const rect = consoleWindowRect(
      requested,
      display.workArea,
      readConsoleOffset(display.displayKey) ?? undefined,
    );
    return {
      size: new LogicalSize(rect.width, rect.height),
      position: new LogicalPosition(rect.x, rect.y),
    };
  }
  if (mode === "preview" && expandedPreview) {
    const fallbackCardHeight = Math.max(
      PREVIEW_EXPANDED_MIN_CARD_H,
      Math.round(naturalTextHeight + PREVIEW_CARD_NON_TEXT_H),
    );
    const fallback: HudGeometry = {
      size: new LogicalSize(
        PREVIEW_EXPANDED_WIDTH,
        fallbackCardHeight + PREVIEW_HEIGHT - PREVIEW_CARD_H,
      ),
      position: null,
      previewCardSize: {
        width: PREVIEW_EXPANDED_CARD_W,
        height: fallbackCardHeight,
      },
    };
    const monitor = await currentMonitor();
    if (!monitor) return fallback;

    const scale = normalizeScaleFactor(monitor.scaleFactor);
    const workArea = monitor.workArea ?? {
      position: monitor.position,
      size: monitor.size,
    };
    const workX = workArea.position.x / scale;
    const workY = workArea.position.y / scale;
    const workWidth = Math.max(1, workArea.size.width / scale);
    const workHeight = Math.max(1, workArea.size.height / scale);
    const windowWidth = Math.max(
      1,
      Math.min(
        PREVIEW_EXPANDED_WIDTH,
        workWidth - PREVIEW_EXPANDED_SIDE_MARGIN * 2,
      ),
    );
    const cardWidth = Math.max(
      1,
      windowWidth - (PREVIEW_EXPANDED_WIDTH - PREVIEW_EXPANDED_CARD_W),
    );
    const requiredCardHeight = Math.max(
      PREVIEW_EXPANDED_MIN_CARD_H,
      Math.round(naturalTextHeight + PREVIEW_CARD_NON_TEXT_H),
    );
    const maximumCardHeight = Math.max(
      1,
      workHeight - PREVIEW_EXPANDED_WORK_AREA_VERTICAL_RESERVE,
    );
    const cardHeight = Math.min(requiredCardHeight, maximumCardHeight);
    const windowHeight =
      cardHeight + (PREVIEW_HEIGHT - PREVIEW_CARD_H);

    return {
      size: new LogicalSize(windowWidth, windowHeight),
      position: new LogicalPosition(
        workX + (workWidth - windowWidth) / 2,
        workY + (workHeight - windowHeight) / 2,
      ),
      previewCardSize: {
        width: cardWidth,
        height: cardHeight,
      },
    };
  }

  // The Claude card surfaces fold their measured painted height in here, the
  // same way the expanded preview folds in its natural text height above.
  const modeGeometry = hudModeGeometry(mode, measuredCard);
  const size = new LogicalSize(modeGeometry.windowW, modeGeometry.windowH);
  const monitor = await currentMonitor();
  if (!monitor) return { size, position: null };

  const scale = normalizeScaleFactor(monitor.scaleFactor);
  const mx = monitor.position.x / scale;
  const my = monitor.position.y / scale;
  const screenW = monitor.size.width / scale;
  const screenH = monitor.size.height / scale;
  let pillX = mx + (screenW - PILL_WIDTH) / 2;
  let pillY = my + screenH - 160;

  const saved = localStorage.getItem("hud-position");
  if (saved) {
    try {
      const position = JSON.parse(saved);
      pillX = Math.max(mx, Math.min(position.x, mx + screenW - PILL_WIDTH));
      pillY = Math.max(my, Math.min(position.y, my + screenH - PILL_HEIGHT));
    } catch {
      // Invalid saved geometry falls back to the monitor-relative default.
    }
  }

  const rawPosition = windowPositionFromPillAnchor(
    mode,
    { x: pillX, y: pillY },
    measuredCard,
  );
  // An expanded control window is never clamped. The clamp moves the window,
  // and moving an expanded control window moves the pill - on hover, which is
  // exactly the jump this geometry exists to remove. The top edge is handled by
  // flipping to the `-below` variant; the sides by letting the transparent
  // window overhang while the bubble shifts inside it.
  const unclamped = isHudControlFamilyMode(mode);
  const panelLeft = unclamped
    ? rawPosition.x
    : Math.max(mx, Math.min(rawPosition.x, mx + screenW - modeGeometry.windowW));
  const panelTop = unclamped
    ? rawPosition.y
    : Math.max(my, Math.min(rawPosition.y, my + screenH - modeGeometry.windowH));
  return {
    size,
    position: new LogicalPosition(panelLeft, panelTop),
  };
}
