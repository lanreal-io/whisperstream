import { getCurrentWindow } from "@tauri-apps/api/window";
import { applyWindowLifecycle } from "../lib/windowLifecycle";
import { usesNativeWindowControls } from "../lib/windowChrome";

export function TitleBar() {
  const minimize = async () => {
    if (await applyWindowLifecycle("main", "minimize")) return;
    try { await getCurrentWindow().minimize(); } catch {}
  };

  const close = async () => {
    if (await applyWindowLifecycle("main", "close-request")) return;
    try { await getCurrentWindow().close(); } catch {}
  };

  if (usesNativeWindowControls()) {
    return <div data-tauri-drag-region className="absolute top-0 left-0 right-0 h-10 z-20" />;
  }

  return (
    <div
      data-tauri-drag-region
      className="absolute top-0 left-0 right-0 h-10 z-20 flex items-center justify-end gap-1.5 pr-5 pt-1"
    >
      {/* Minimize */}
      <button
        onClick={minimize}
        className="w-7 h-7 rounded-full flex items-center justify-center text-steel/40 hover:text-steel hover:bg-steel/10 transition-colors cursor-default"
        aria-label="Minimize"
      >
        <svg width="10" height="1" viewBox="0 0 10 1" fill="none">
          <rect width="10" height="1.2" rx="0.6" fill="currentColor" />
        </svg>
      </button>

      {/* Close */}
      <button
        onClick={close}
        className="w-7 h-7 rounded-full flex items-center justify-center text-steel/40 hover:text-rose hover:bg-rose/10 transition-colors cursor-default"
        aria-label="Close"
      >
        <svg width="8" height="8" viewBox="0 0 8 8" fill="none">
          <path
            d="M1 1L7 7M7 1L1 7"
            stroke="currentColor"
            strokeWidth="1.3"
            strokeLinecap="round"
          />
        </svg>
      </button>
    </div>
  );
}
