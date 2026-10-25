import { isTauri } from "@tauri-apps/api/core";

const PACKAGED_HEALTH_URL = "http://localhost:38420/api/health";
const DEFAULT_ATTEMPTS = 120;
const DEFAULT_DELAY_MS = 250;

export type BackendMountReadiness = {
  state: "not-required" | "ready" | "unavailable";
  attempts: number;
};

type ReadinessOptions = {
  native?: boolean;
  attempts?: number;
  fetchHealth?: () => Promise<Response>;
  delay?: () => Promise<void>;
};

const defaultFetchHealth = () =>
  fetch(PACKAGED_HEALTH_URL, {
    cache: "no-store",
    signal: AbortSignal.timeout(DEFAULT_DELAY_MS),
  });

const defaultDelay = () =>
  new Promise<void>((resolve) => window.setTimeout(resolve, DEFAULT_DELAY_MS));

/**
 * Keep packaged one-shot hydration effects behind the shell-owned backend.
 * Browser/dev mounting remains immediate, and an unavailable packaged backend
 * shows recovery UI after a bounded window allowing the shell 30s to start.
 */
export async function waitForBackendBeforeMount(
  options: ReadinessOptions = {},
): Promise<BackendMountReadiness> {
  const native = options.native ?? (import.meta.env.PROD && isTauri());
  if (!native) return { state: "not-required", attempts: 0 };

  const attempts = options.attempts ?? DEFAULT_ATTEMPTS;
  if (!Number.isInteger(attempts) || attempts < 1) {
    throw new Error("backend readiness attempts must be a positive integer");
  }
  const fetchHealth = options.fetchHealth ?? defaultFetchHealth;
  const delay = options.delay ?? defaultDelay;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await fetchHealth();
      if (response.ok) return { state: "ready", attempts: attempt };
    } catch {
      // The engine listener is expected to be absent during early attempts.
    }
    if (attempt < attempts) await delay();
  }
  return { state: "unavailable", attempts };
}
