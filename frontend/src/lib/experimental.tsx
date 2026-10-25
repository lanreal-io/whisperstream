// Experimental-build gate for unfinished dogfood features such as transcript
// Re-enhance. Mirrors the dev-tools gate (`import.meta.env.DEV && devEnabled`)
// but adds a second compile-time branch for personal dogfood builds.
//
// Two halves, both must be true for gated UI to render:
//   1. Compile-time (EXPERIMENTAL_BUILD): true in dev Vite bundles
//      (import.meta.env.DEV) and in dogfood builds where
//      `scripts/build.py --experimental` sets VITE_EXPERIMENTAL=1. Statically
//      false in production bundles, so Vite dead-code-eliminates every gated
//      branch — the experimental UI can never ship in a release installer.
//   2. Runtime (experimentalEnabled): the backend probe
//      GET /api/experimental/enabled, true only when WHISPERSTREAM_EXPERIMENTAL=1
//      AND the build kept the modules (_build_flags.EXPERIMENTAL_AVAILABLE).
//
// Units 0b/0c gate their UI with `useExperimental()` (anywhere in the tree) or
// `isExperimentalVisible(experimentalEnabled)` (with an explicit probe value).
import { createContext, useContext, type ReactNode } from "react";

/**
 * Compile-time experimental-build gate. `true` in dev Vite bundles and in
 * `--experimental` dogfood builds; statically `false` in production bundles, so
 * every `EXPERIMENTAL_BUILD && ...` branch is dead-code-eliminated from a release.
 */
export const EXPERIMENTAL_BUILD =
  import.meta.env.DEV || import.meta.env.VITE_EXPERIMENTAL === "1";

/**
 * Combine the compile-time build gate with the backend runtime probe. Gated UI
 * renders only when BOTH are true. Prefer `useExperimental()` inside components;
 * use this when you already hold the probe value (e.g. App's `experimentalEnabled`).
 */
export function isExperimentalVisible(experimentalEnabled: boolean): boolean {
  return EXPERIMENTAL_BUILD && experimentalEnabled;
}

// Default false: a bare consumer with no provider (e.g. a Tauri popup window)
// never shows experimental UI.
const ExperimentalContext = createContext(false);

/**
 * Wraps the app and exposes the resolved experimental-visibility flag to any
 * descendant via `useExperimental()`. `enabled` is App's backend-probe state;
 * the provider folds it together with the compile-time gate.
 */
export function ExperimentalProvider({
  enabled,
  children,
}: {
  enabled: boolean;
  children: ReactNode;
}) {
  return (
    <ExperimentalContext.Provider value={isExperimentalVisible(enabled)}>
      {children}
    </ExperimentalContext.Provider>
  );
}

/**
 * `true` only in an experimental build (dev or `--experimental`) with the backend
 * gate on. Units 0b/0c wrap their gated UI in `if (useExperimental())` / a
 * `{useExperimental() && ...}` branch.
 */
export function useExperimental(): boolean {
  return useContext(ExperimentalContext);
}
