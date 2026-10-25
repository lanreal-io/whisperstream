

export const RECORDING_CONTROL_VISIBILITY_STORAGE_KEY =
  "ws-recording-control-visible";

export const RECORDING_CONTROL_VISIBILITY_EVENT =
  "recording-control-visibility-changed";

export const RECORDING_CONTROL_START_PENDING_EVENT =
  "recording-control-start-pending";

export const HUD_MAIN_VISIBILITY_EVENT = "hud-main-visibility-changed";

/** Native pointer enter (true) or exit (false) for the recording control.
 *  macOS WebKit drops DOM hover in the control's never-key panel. */
export const RECORDING_CONTROL_HOVER_EVENT = "recording-control-hover";

export const RECORDING_CONTROL_WINDOW_LABEL = "recording-control";

/**
 * Architecture seam for the isolated permanent-control WebView.
 *
 * Production always uses the dedicated window. The seam remains explicit so
 * focused tests can cover the retired shared-window rollback path while the
 * dedicated-window suite pins the shipping architecture.
 */
export function usesDedicatedRecordingControlWindow(): boolean {
  return true;
}
