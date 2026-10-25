import { ShortcutCapture } from "../ShortcutCapture";
import type { AppConfig } from "../../lib/api/config";
import type { AppEvent } from "../../lib/socket/events";

interface StepShortcutProps {
  config: AppConfig;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  send: (msg: Record<string, unknown>) => boolean;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  connected: boolean;
}

// Step 2 — glass panel with a centered ShortcutCapture. Panel dimensions
// (560x280) match Step 3 so the surface feels stable while content morphs
// between the two steps. The recording-mode pills were removed: onboarding
// always binds push-to-talk (hold-to-record); the toggle hotkey is
// discoverable later in Controls.
export function StepShortcut({
  config,
  onConfigChange,
  send,
  subscribe,
  connected,
}: StepShortcutProps) {
  return (
    <div
      className="rounded-[var(--radius-card)] mx-auto flex flex-col"
      style={{
        width: 560,
        minHeight: 280,
        padding: 28,
        background: "var(--surface-card)",
        backdropFilter: "blur(24px)",
        border: "1px solid var(--border-hairline)",
        borderTop: "1px solid var(--border-emphasis)",
        boxShadow: "var(--shadow-glass-sm)",
      }}
    >
      <h2 className="font-display text-[24px] font-light tracking-tight text-slate leading-tight">
        Set your shortcut
      </h2>
      <p className="mt-2 text-[12px] text-steel">
        Hold this key to record, release to stop. You can change it anytime in settings.
      </p>

      {/* Capture button centered in the space the mode pills vacated,
          so the panel stays balanced at its fixed minHeight. */}
      <div className="flex-1 flex items-center justify-center">
        <ShortcutCapture
          value={config.push_to_talk_shortcut}
          onChange={(keys) => onConfigChange({ push_to_talk_shortcut: keys })}
          send={send}
          subscribe={subscribe}
          connected={connected}
          captureTarget="push_to_talk"
          clearable={false}
        />
      </div>
    </div>
  );
}
