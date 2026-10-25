import { fetchJSON, BASE } from "./client";

export const PERMISSION_ACTION_HEADERS = {
  "Content-Type": "application/json",
  "X-Whisperstream-User-Intent": "permission-action-v1",
} as const;


export interface AudioDevice {
  id: string;
  name: string;
  channels: number;
  sample_rate: number;
  is_default: boolean;
}


export type PermissionKind =
  | "microphone"
  | "accessibility"
  | "input-monitoring";


export type PermissionState =
  | "not-determined"
  | "granted"
  | "denied"
  | "restricted"
  | "not-required"
  | "unsupported"
  | "unknown";


export interface PermissionSnapshot {
  kind: PermissionKind;
  state: PermissionState;
  query_supported: boolean;
  request_supported: boolean;
}


export interface PermissionReport {
  schema_version: 1;
  platform: string;
  permissions: PermissionSnapshot[];
  required_for_operations?: { dictation: PermissionKind[] };
}


export type PermissionAction = "request" | "open-settings";


export type PermissionActionStatus =
  | "completed"
  | "declined"
  | "unchanged"
  | "failed"
  | "unsupported";


export type PermissionActionFailure =
  | "intent-required"
  | "intent-reused"
  | "not-requestable"
  | "recovery-unavailable"
  | "native-error";


export interface PermissionActionOutcome {
  schema_version: 1;
  kind: PermissionKind;
  action: PermissionAction;
  status: PermissionActionStatus;
  before: PermissionState;
  after: PermissionState;
  failure: PermissionActionFailure | null;
  relaunch_required: boolean;
}


export async function getDevices(): Promise<AudioDevice[]> {
  return fetchJSON(`${BASE}/api/devices`);
}


export interface ShortcutReadiness {
  state: "ready" | "pending" | "failed" | "blocked" | "disabled";
  failure: null | "permission_required" | "native_error" | "busy" | "shutdown";
}


export async function getShortcutReadiness(): Promise<ShortcutReadiness> {
  return fetchJSON(`${BASE}/api/shortcuts/readiness`);
}


/** Recovers the listener only; never requests OS access. */
export async function reconcileShortcuts(): Promise<ShortcutReadiness> {
  return fetchJSON(`${BASE}/api/shortcuts/reconcile`, {
    method: "POST", headers: PERMISSION_ACTION_HEADERS, body: JSON.stringify({}),
  });
}


/** Query capability state only. This call never requests OS access. */
export async function getPermissions(): Promise<PermissionReport> {
  return fetchJSON(`${BASE}/api/permissions`);
}


/** Explicit user action only. The backend mints and consumes its intent locally. */
export async function requestPermission(
  kind: PermissionKind,
): Promise<PermissionActionOutcome> {
  return fetchJSON(`${BASE}/api/permissions/request`, {
    method: "POST",
    headers: PERMISSION_ACTION_HEADERS,
    body: JSON.stringify({ kind }),
  });
}


/** Explicit user action only. Opens System Settings when recovery is available. */
export async function openPermissionSettings(
  kind: PermissionKind,
): Promise<PermissionActionOutcome> {
  return fetchJSON(`${BASE}/api/permissions/recovery`, {
    method: "POST",
    headers: PERMISSION_ACTION_HEADERS,
    body: JSON.stringify({ kind }),
  });
}


export interface StartupStatusResponse {
  enabled: boolean;
  supported: boolean;
}


export async function getStartup(): Promise<StartupStatusResponse> {
  return fetchJSON(`${BASE}/api/startup`);
}


export async function setStartup(enabled: boolean): Promise<StartupStatusResponse> {
  return fetchJSON(`${BASE}/api/startup`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}





export async function getHealth(): Promise<{ status: string; model_loaded: boolean }> {
  return fetchJSON(`${BASE}/api/health`);
}


export async function getScreenReaderStatus(
  init?: Pick<RequestInit, "signal">,
): Promise<{ active: boolean }> {
  return fetchJSON(`${BASE}/api/accessibility/screen-reader`, init);
}


/** Total physical RAM detection (Dialog A pre-check before local model download).
 *  Backend returns ``{total_ram_mb: <int>}`` on success and
 *  ``{total_ram_mb: null}`` on detection failure (psutil exception, exotic
 *  platform). Callers MUST treat null as "skip the warning" rather than
 *  blocking — the warning fails open. */
export type SystemInfo = { total_ram_mb: number | null };


export async function getSystemInfo(): Promise<SystemInfo> {
  return fetchJSON(`${BASE}/api/system/info`);
}


export async function getVersion(): Promise<{ version: string }> {
  return fetchJSON(`${BASE}/api/version`);
}
