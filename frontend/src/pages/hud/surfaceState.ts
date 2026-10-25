import type { HudGeometryMode } from "./geometry";

/**
 * One state for every HUD surface: which surface is on screen and whether it
 * is appearing, steady or leaving.
 *
 * The surfaces follow the native window families: the compact pill, compact
 * question pill and glance card share one fixed reply window (`card`); the
 * centered console, the expanded question and the permission pill each have
 * their own; ordinary dictation, recording controls and Live Preview share
 * the dictation family and keep their own long-standing contracts.
 *
 * - **appearing:** the surface is wanted but the native window does not yet
 *   hold its geometry. Arriving from another family (`entry: "family"`), the
 *   HUD paints nothing until the window is placed, so no card is ever seen in
 *   another surface's window. The entrance runs when it turns steady, never
 *   on a mount: a card remounted while steady does not replay it.
 * - **steady:** on screen at its own geometry. Press, speech, release, draft,
 *   send and queue changes are props on the mounted card.
 * - **leaving:** going away. The surface keeps its last steady frame
 *   (`snapshot`), its layout and its window until the native hide or the
 *   next surface takes over, so it fades where it was and never morphs into
 *   the generic pill. A timed leave (the console fading before it collapses)
 *   is `timed`; once that fade has run and nothing replaces the console,
 *   `faded` asks the window to hide at once.
 *
 * `nextSurfaceFrame` is pure: the coordinator evaluates it during render from
 * the last committed frame, so StrictMode's double render cannot advance it.
 */
export type HudSurface =
  | "card"
  | "console"
  | "question"
  | "permission"
  | "dictation";

export type HudSurfacePhase = "appearing" | "steady" | "leaving";

export interface HudSurfaceFrame<S> {
  surface: HudSurface;
  phase: HudSurfacePhase;
  /** How the surface arrived: from a hidden HUD or from another family. */
  entry: "hidden" | "family";
  /** The last steady frame. While leaving it is frozen. */
  snapshot: S | null;
  /** A leave the coordinator timed (the console's fade). */
  timed: boolean;
  /** A timed leave that already faded with nothing after it. */
  faded: boolean;
}

export interface HudSurfaceInputs<S> {
  /** The surface the presentation wants, or null when hidden. */
  desired: HudSurface | null;
  /** The geometry the reconciler last applied (null once hidden). */
  appliedMode: HudGeometryMode | null;
  /** The surface's current frame, recorded while it is not leaving. */
  snapshot: S | null;
  /** The coordinator is fading this surface out before it changes. */
  timedLeave: HudSurface | null;
}

/** The surface a geometry mode belongs to. */
export function hudSurfaceForMode(mode: HudGeometryMode): HudSurface {
  switch (mode) {
    case "panel":
    case "summary":
      return "card";
    case "response":
      return "console";
    case "question":
      return "question";
    case "permission":
      return "permission";
    default:
      return "dictation";
  }
}

export function nextSurfaceFrame<S>(
  previous: HudSurfaceFrame<S> | null,
  { desired, appliedMode, snapshot, timedLeave }: HudSurfaceInputs<S>,
): HudSurfaceFrame<S> | null {
  const appliedSurface = appliedMode === null ? null : hudSurfaceForMode(appliedMode);

  if (desired === null) {
    if (previous === null) return null;
    // Never shown, or the native hide has landed: nothing remains.
    if (appliedSurface === null) return null;
    if (previous.phase === "appearing" && appliedSurface !== previous.surface) return null;
    return {
      ...previous,
      phase: "leaving",
      faded: previous.faded || (previous.timed && timedLeave !== previous.surface),
    };
  }

  if (timedLeave === desired && previous !== null && previous.surface === desired) {
    // The surface fades where it is before the presentation changes.
    return {
      ...previous,
      phase: "leaving",
      timed: true,
      snapshot: previous.phase === "leaving" ? previous.snapshot : (previous.snapshot ?? snapshot),
    };
  }

  const placed = appliedSurface === desired;
  if (previous === null || previous.surface !== desired || previous.faded) {
    return {
      surface: desired,
      phase: placed ? "steady" : "appearing",
      entry: previous === null || appliedSurface === null ? "hidden" : "family",
      snapshot,
      timed: false,
      faded: false,
    };
  }

  // Same surface: settle once placed; a leave that is called off (shown
  // again before the native hide) returns to steady with live content.
  const phase: HudSurfacePhase =
    previous.phase === "appearing" && !placed ? "appearing" : "steady";
  return { ...previous, phase, snapshot, timed: false, faded: false };
}

/** The surface hides its content until its window is in place. */
export function surfaceAwaitingWindow<S>(frame: HudSurfaceFrame<S> | null): boolean {
  return frame !== null && frame.phase === "appearing" && frame.entry === "family";
}

/** The entrance has been released: the surface is steady on screen. */
export function surfaceEntered<S>(frame: HudSurfaceFrame<S> | null): boolean {
  return frame !== null && frame.phase === "steady";
}
