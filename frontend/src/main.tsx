import React, { type ComponentType } from "react";
import ReactDOM from "react-dom/client";
import { resolveWindowRoot } from "./windowRoots";
import { loadOfficialUpdatePopup } from "./lib/officialUpdater";
import "./styles/globals.css";
import { flushSync } from "react-dom";
import { StartupFailureCurtain } from "./components/StartupFailureCurtain";
import { LoadingCurtain } from "./components/LoadingCurtain";
import { startLockdownAwareLogCapture } from "./lib/logCapture";
import { waitForBackendBeforeMount } from "./lib/backendReadiness";

// Paint a neutral main-window surface before any backend-dependent work.
// Auxiliary windows retain their transparent, hidden startup contract.
const root = ReactDOM.createRoot(document.getElementById("root")!);
if (resolveWindowRoot(window.location.pathname, import.meta.env.DEV) === "app") {
  flushSync(() => root.render(<LoadingCurtain label="Starting Whisperstream…" />));
}

const backendReadiness = await waitForBackendBeforeMount();
const logCaptureReadiness = startLockdownAwareLogCapture();
if (backendReadiness.state === "ready") await logCaptureReadiness;

if (import.meta.env.VITE_NATIVE_UI_TEST === "1") {
  await import("@whisperstream/native-ui-test-guest");
  // Source-only native QA exercises the actual picker grants and bundled bytes.
  Object.assign(window, { nativeDocumentation: await import("./lib/offlineDocumentation") });
  console.info("[NativeUiTest] frontend-log-marker guest=ready");
}


const pathname = window.location.pathname;
if (import.meta.env.DEV && pathname === "/debug/ui-qa") {
  const requestedTheme = new URLSearchParams(window.location.search).get(
    "theme",
  );
  if (requestedTheme === "light" || requestedTheme === "dark") {
    document.documentElement.setAttribute("data-theme", requestedTheme);
  }
}
// The dialog and UI QA showcases are development tooling that source releases
// omit, so they register only when their module exists (the glob is empty
// otherwise) and their routes fall back to the main app.
const loadShowcaseModule = import.meta.env.DEV
  ? import.meta.glob<{ DialogShowcase: ComponentType; UiQaShowcase: ComponentType }>(
      "./pages/DialogShowcase.tsx",
    )["./pages/DialogShowcase.tsx"]
  : undefined;
const loadDialogShowcaseRoot = loadShowcaseModule
  ? async () => (await loadShowcaseModule()).DialogShowcase
  : null;
const loadUiQaShowcaseRoot = loadShowcaseModule
  ? async () => (await loadShowcaseModule()).UiQaShowcase
  : null;
// The marketing demo stage is optional development tooling in the same way.
const loadDemoStageModule = import.meta.env.DEV
  ? import.meta.glob<{ DemoStage: ComponentType }>("./dev/demo/DemoStage.tsx")[
      "./dev/demo/DemoStage.tsx"
    ]
  : undefined;

function assertUnreachableWindowRoot(rootId: never): never {
  throw new Error(`Window root has no dynamic loader: ${String(rootId)}`);
}

async function loadWindowRoot(): Promise<ComponentType> {
  const rootId = resolveWindowRoot(
    window.location.pathname,
    import.meta.env.DEV,
  );
  switch (rootId) {
    case "hud":
      return (await import("./pages/HUD")).HUD;
    case "recording-control":
      return (await import("./pages/RecordingControlWindow"))
        .RecordingControlWindow;
    case "snap-guide":
      return (await import("./pages/SnapGuide")).SnapGuide;
    case "review-popup":
      return (await import("./pages/ReviewPopup")).ReviewPopup;
    case "update-popup":
      // Only the official installer's private updater creates this window.
      return loadOfficialUpdatePopup ? loadOfficialUpdatePopup() : () => null;
    case "eula-popup":
      return (await import("./pages/EulaPopup")).EulaPopup;
    case "error-popup":
      return (await import("./pages/ErrorPopup")).ErrorPopup;
    case "dialog-showcase":
      return loadDialogShowcaseRoot
        ? loadDialogShowcaseRoot()
        : (await import("./App")).default;
    case "ui-qa-showcase":
      return loadUiQaShowcaseRoot
        ? loadUiQaShowcaseRoot()
        : (await import("./App")).default;
    case "demo-stage":
      return loadDemoStageModule
        ? (await loadDemoStageModule()).DemoStage
        : (await import("./App")).default;
    case "app":
      return (await import("./App")).default;
    default:
      return assertUnreachableWindowRoot(rootId);
  }
}

async function mountWindowRoot(): Promise<void> {
  if (
    backendReadiness.state === "unavailable" &&
    resolveWindowRoot(window.location.pathname, import.meta.env.DEV) === "app"
  ) {
    root.render(<StartupFailureCurtain />);
    return;
  }
  const WindowRoot = await loadWindowRoot();
  root.render(
    <React.StrictMode>
      <WindowRoot />
    </React.StrictMode>,
  );
}

void mountWindowRoot();
