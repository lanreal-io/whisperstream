// Typed launch-at-login state.
//
// Packaged Tauri shells own the native registration and passive status query.
// Windows uses its existing Run-value adapter; macOS uses SMAppService for the
// main application. The browser/dev fallback consumes the backend's explicit
// support flag; only the Windows backend can mutate that fallback.

import { invoke, isTauri } from "@tauri-apps/api/core";
import { getStartup, setStartup, type StartupStatusResponse } from "./api/system";

export type LaunchAtLoginState =
  | "enabled"
  | "disabled"
  | "requires-approval"
  | "not-found"
  | "unsupported"
  | "unavailable";

export type LaunchAtLoginFailure =
  | "unsupported"
  | "not-found"
  | "invalid-state"
  | "display-unavailable"
  | "stale-generation"
  | "native-failure";

export interface LaunchAtLoginStatus {
  state: LaunchAtLoginState;
  failure: LaunchAtLoginFailure | null;
}

export interface LaunchAtLoginMutationOutcome {
  status: LaunchAtLoginStatus;
  failed: boolean;
  error: unknown;
}

const STATES = new Set<LaunchAtLoginState>([
  "enabled",
  "disabled",
  "requires-approval",
  "not-found",
  "unsupported",
  "unavailable",
]);
const FAILURES = new Set<LaunchAtLoginFailure>([
  "unsupported",
  "not-found",
  "invalid-state",
  "display-unavailable",
  "stale-generation",
  "native-failure",
]);

function parseStatus(value: unknown): LaunchAtLoginStatus {
  if (!value || typeof value !== "object") {
    throw new Error("invalid launch-at-login status");
  }
  const candidate = value as Partial<LaunchAtLoginStatus>;
  if (!candidate.state || !STATES.has(candidate.state)) {
    throw new Error("invalid launch-at-login state");
  }
  const failure = candidate.failure ?? null;
  if (failure !== null && !FAILURES.has(failure)) {
    throw new Error("invalid launch-at-login failure");
  }
  const failureState = ["not-found", "unsupported", "unavailable"].includes(
    candidate.state,
  );
  if (failureState !== (failure !== null)) {
    throw new Error("inconsistent launch-at-login status");
  }
  return {
    state: candidate.state,
    failure,
  };
}

function parseFallbackStatus(value: unknown): LaunchAtLoginStatus {
  if (!value || typeof value !== "object") {
    throw new Error("invalid startup API status");
  }
  const candidate = value as Partial<StartupStatusResponse>;
  if (
    typeof candidate.enabled !== "boolean" ||
    typeof candidate.supported !== "boolean" ||
    (!candidate.supported && candidate.enabled)
  ) {
    throw new Error("invalid startup API state");
  }
  if (!candidate.supported) {
    return { state: "unsupported", failure: "unsupported" };
  }
  return { state: candidate.enabled ? "enabled" : "disabled", failure: null };
}

export async function getStartupState(): Promise<LaunchAtLoginStatus> {
  if (isTauri()) {
    return parseStatus(await invoke("window_launch_at_login_state"));
  }
  return parseFallbackStatus(await getStartup());
}

export async function setStartupEnabled(
  enabled: boolean,
): Promise<LaunchAtLoginStatus> {
  if (isTauri()) {
    return parseStatus(
      await invoke("set_window_launch_at_login", { enabled }),
    );
  }
  return parseFallbackStatus(await setStartup(enabled));
}

export async function applyStartupChange(
  previous: LaunchAtLoginStatus,
  enabled: boolean,
  mutate: (enabled: boolean) => Promise<LaunchAtLoginStatus> = setStartupEnabled,
): Promise<LaunchAtLoginMutationOutcome> {
  try {
    const status = await mutate(enabled);
    if (status.failure) {
      return { status: previous, failed: true, error: status.failure };
    }
    return { status, failed: false, error: null };
  } catch (error) {
    return { status: previous, failed: true, error };
  }
}
