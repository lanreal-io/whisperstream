import { BASE } from "../../lib/api/client";
import { HUD_COMMAND_HEADERS } from "../../lib/api/dictation";
import { RECORDING_CONTROL_VISIBILITY_STORAGE_KEY } from "../../lib/recordingControl";
import { SCREEN_READER_STATUS_STORAGE_KEY } from "../../lib/accessibility";

export const RECORDING_CONTROL_COLLAPSE_MS = 120;

export type HudDictationCommand = "start" | "complete" | "cancel";

export async function postHudDictationCommand(
  command: HudDictationCommand,
): Promise<void> {
  const response = await fetch(`${BASE}/api/dictation/${command}`, {
    method: "POST",
    headers: HUD_COMMAND_HEADERS,
  });
  if (!response.ok) {
    throw new Error(`dictation_${command}_rejected`);
  }
}

export function readRecordingControlVisibility(): boolean {
  try {
    return (
      localStorage.getItem(RECORDING_CONTROL_VISIBILITY_STORAGE_KEY) === "true"
    );
  } catch {
    return false;
  }
}

export function readScreenReaderStatus(): boolean | null {
  try {
    const stored = localStorage.getItem(SCREEN_READER_STATUS_STORAGE_KEY);
    // A cached positive detection is safe to trust immediately. A cached
    // negative may be stale if assistive technology started since the main
    // window last wrote it, so keep the persistent control suppressed until
    // a current runtime probe, config event, or lifecycle event confirms false.
    return stored === "true" ? true : null;
  } catch {
    return null;
  }
}
