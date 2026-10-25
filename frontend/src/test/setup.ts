import "@testing-library/jest-dom/vitest";
import { MotionGlobalConfig } from "framer-motion";
import { installNetworkGuard } from "./networkGuard";

// React 19 requires this flag to be set in test environments so act(...)
// can flush state updates without printing the noisy "current testing
// environment is not configured to support act(...)" warning.
// See: https://react.dev/reference/react/act
declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

// jsdom has no ResizeObserver. ReadingWell + WaveformPlayer (the Transcripts
// components, also used for the profile editor's read-only built-in prompt)
// construct one on mount, so stub it to a no-op for the test environment.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// Unmocked network must never reach a real backend (for example the installed
// app on localhost:38420). fetch, XMLHttpRequest and WebSocket fail closed for
// every host; tests mock what they need. See ./networkGuard.ts.
installNetworkGuard();

// Finish framer-motion animations instantly. AnimatePresence mode="wait" mounts
// the next card only after the previous one's exit animation completes; under
// CPU load that wall-clock wait could outlast findBy's one-second timeout and
// fail tests nondeterministically (BenchmarkFlow's prefilter -> success card).
MotionGlobalConfig.skipAnimations = true;
