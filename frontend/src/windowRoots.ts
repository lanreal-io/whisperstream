export type WindowRootId =
  | "app"
  | "hud"
  | "recording-control"
  | "snap-guide"
  | "review-popup"
  | "update-popup"
  | "eula-popup"
  | "error-popup"
  | "dialog-showcase"
  | "ui-qa-showcase";

const WINDOW_ROOTS: Readonly<Record<string, WindowRootId>> = {
  "/": "app",
  "/hud": "hud",
  "/recording-control": "recording-control",
  "/snap-guide": "snap-guide",
  "/popup/review": "review-popup",
  "/popup/update": "update-popup",
  "/popup/eula": "eula-popup",
  "/popup/error": "error-popup",
};

export const STATIC_WINDOW_ROOT_PATHS = Object.freeze(
  Object.keys(WINDOW_ROOTS),
);

export function findUnregisteredStaticWindowPaths(
  configuredPaths: readonly string[],
): string[] {
  const registeredPaths = new Set(STATIC_WINDOW_ROOT_PATHS);
  return [
    ...new Set(
      configuredPaths
        .filter((url) => url.startsWith("/"))
        .map((url) => url.split(/[?#]/, 1)[0]),
    ),
  ].filter((path) => !registeredPaths.has(path));
}

export const WINDOW_ROOT_MODULE_SUFFIXES = {
  app: "/src/App.tsx",
  hud: "/src/pages/HUD.tsx",
  "recording-control": "/src/pages/RecordingControlWindow.tsx",
  "snap-guide": "/src/pages/SnapGuide.tsx",
  "review-popup": "/src/pages/ReviewPopup.tsx",
  "eula-popup": "/src/pages/EulaPopup.tsx",
  "error-popup": "/src/pages/ErrorPopup.tsx",
} as const satisfies Record<
  // The update popup comes from the build's edition, which names its own
  // window roots for the split guard (see edition.ts).
  Exclude<WindowRootId, "update-popup" | "dialog-showcase" | "ui-qa-showcase">,
  string
>;

/**
 * Resolve the single React root owned by a Tauri webview pathname.
 *
 * Keep this table in sync with `src-tauri/tauri.conf.json`. The caller loads
 * the returned root through a dynamic import so each startup-created webview
 * parses only its own application graph.
 */
export function resolveWindowRoot(
  pathname: string,
  development: boolean,
): WindowRootId {
  if (development && pathname === "/debug/dialogs") {
    return "dialog-showcase";
  }
  if (development && pathname === "/debug/ui-qa") {
    return "ui-qa-showcase";
  }
  return WINDOW_ROOTS[pathname] ?? "app";
}
