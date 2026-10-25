/**
 * Platform wording and error copy for standard history recovery. Every string
 * is a locked copy decision; keep
 * full sentences per platform rather than concatenated fragments.
 */

import { historyRecoveryErrorCode } from "./api/historyRecovery";
import {
  HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE,
  isHistoryDisabledByOrganizationError,
} from "./api/lockdown";

export type HistoryPlatform = "mac" | "windows";

export function historyPlatform(platform: string | null | undefined): HistoryPlatform {
  return platform === "Darwin" ? "mac" : "windows";
}

/** "this Mac" / "this PC". */
export function placeFor(platform: HistoryPlatform): string {
  return platform === "mac" ? "this Mac" : "this PC";
}

export const HISTORY_STARTING_MESSAGE =
  "Whisperstream is still starting. Try again in a moment.";

/** Turning saving on failed; map the backend code to the locked copy. */
export function enableErrorMessage(err: unknown, platform: HistoryPlatform): string {
  if (isHistoryDisabledByOrganizationError(err)) return HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE;
  switch (historyRecoveryErrorCode(err)) {
    case "history_disabled_by_organization":
      return HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE;
    case "history_key_unavailable":
      return platform === "mac"
        ? "Saving is still off. Keychain didn't allow access. Try again."
        : "Saving is still off. Windows couldn't protect the encryption key. Try again.";
    case "history_locked":
      return "Saving is still off. Unlock your history and try again.";
    case "history_initializing":
      return HISTORY_STARTING_MESSAGE;
    default:
      return "Saving is still off. Try again.";
  }
}

/** A restore attempt failed after the secret matched. */
export function restoreErrorMessage(err: unknown, platform: HistoryPlatform): string {
  const code = historyRecoveryErrorCode(err);
  if (code === "restore_access_denied") {
    return platform === "mac"
      ? "Couldn't save the key to Keychain. Allow access if asked, then try again."
      : "Couldn't save the key to your Windows account. Try again.";
  }
  if (code === "history_initializing") return HISTORY_STARTING_MESSAGE;
  return "Couldn't restore history. Try again.";
}
