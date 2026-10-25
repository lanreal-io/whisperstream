import { emit, listen } from "@tauri-apps/api/event";

const MAX_ENTRIES = 500;
const MAX_LINE_BYTES = 2_048;
const MAX_TOTAL_BYTES = 128 * 1_024;

const buffer: string[] = [];
// Bumped whenever the ring changes, so sync can skip unchanged rings.
let bufferGeneration = 0;
let initialized = false;
let capturePaused = false;

const originalConsole = {
  log: console.log,
  warn: console.warn,
  error: console.error,
};

const ERROR_TYPES = new Set([
  "AbortError",
  "AggregateError",
  "DataCloneError",
  "EncodingError",
  "Error",
  "EvalError",
  "HttpError",
  "InvalidCharacterError",
  "InvalidStateError",
  "NetworkError",
  "NotAllowedError",
  "NotFoundError",
  "NotReadableError",
  "NotSupportedError",
  "OperationError",
  "QuotaExceededError",
  "RangeError",
  "ReferenceError",
  "SecurityError",
  "SyntaxError",
  "TimeoutError",
  "TypeError",
  "URIError",
]);

function timestamp(): string {
  return new Date().toISOString();
}

const SENSITIVE_FIELD_RE = /\b((?:file|filename|path|url|endpoint|process|domain|title|device|hostname)|(?:session|record|recording|prompt|interaction|request|source)_?id)\s*=\s*.*?(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)/gi;
const RAW_ERROR_FIELD_RE = /\b(?:error|message|reason)\s*=.*$/gi;
const URL_RE = /\b(?:https?|file):\/\/\S+/gi;
const WINDOWS_PATH_RE = /(?:\b[A-Z]:[\\/]|\\\\)[^\s]+/gi;
const UNIX_USER_PATH_RE = /\/(?:Users|home|tmp|var\/tmp)\/[^\s]+/g;
const EMAIL_RE = /\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+\b/gi;
const SECRET_FIELD_RE = /\b(password|passphrase|recovery_?code|api_?key|license_?(?:key|secret)|access_?token|refresh_?token|auth(?:orization)?|bearer|secret)\s*[=:]\s*.*?(?=\s+[A-Za-z_][A-Za-z0-9_]*\s*[=:]|$)/gi;
const RECOVERY_CODE_RE = /\b[A-Z0-9]{4}(?:-[A-Z0-9]{4}){4}\b/gi;

export function errorType(value: unknown): string {
  if (value instanceof Error && ERROR_TYPES.has(value.name)) {
    return value.name;
  }
  return "UnknownError";
}

function truncateUtf8(value: string, maxBytes: number): string {
  const bytes = new TextEncoder().encode(value);
  if (bytes.byteLength <= maxBytes) return value;
  return new TextDecoder().decode(bytes.slice(0, maxBytes));
}

function sanitizeText(value: string): string {
  return truncateUtf8(
    value
      .replace(/[\u0000-\u001f\u007f]/g, " ")
      .replace(URL_RE, "<redacted-url>")
      .replace(WINDOWS_PATH_RE, "<redacted-path>")
      .replace(UNIX_USER_PATH_RE, "<redacted-path>")
      .replace(EMAIL_RE, "<redacted-email>")
      .replace(RECOVERY_CODE_RE, "<redacted-recovery-code>")
      .replace(SECRET_FIELD_RE, (_match, field: string) => `${field.toLowerCase()}=<redacted>`)
      .replace(SENSITIVE_FIELD_RE, (_match, field: string) => `${field.toLowerCase()}=<redacted>`)
      .replace(RAW_ERROR_FIELD_RE, (match) => `${match.split("=", 1)[0].toLowerCase()}=<redacted>`)
      .replace(/[^\x20-\x7e]/g, "")
      .replace(/\s+/g, " ")
      .trim(),
    MAX_LINE_BYTES,
  );
}

function formatArgument(value: unknown): string | null {
  if (typeof value === "string") return sanitizeText(value);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value instanceof Error) return `error_type=${errorType(value)}`;
  return null;
}

function enforceBufferBounds(): void {
  while (buffer.length > MAX_ENTRIES) buffer.shift();
  let totalBytes = buffer.reduce(
    (total, line) => total + new TextEncoder().encode(line).byteLength,
    0,
  );
  while (buffer.length > 0 && totalBytes > MAX_TOTAL_BYTES) {
    const removed = buffer.shift();
    if (removed) totalBytes -= new TextEncoder().encode(removed).byteLength;
  }
}

function pushEntry(level: string, args: unknown[]): void {
  if (capturePaused) return;
  const message = args.map(formatArgument).filter(Boolean).join(" ");
  if (!message) return;
  buffer.push(
    truncateUtf8(`[${timestamp()}] [${level}] ${message}`, MAX_LINE_BYTES),
  );
  enforceBufferBounds();
  bufferGeneration += 1;
}

export function initLogCapture(): void {
  if (initialized) return;
  initialized = true;

  console.log = (...args: unknown[]) => {
    pushEntry("INFO", args);
    originalConsole.log.apply(console, args);
  };

  console.warn = (...args: unknown[]) => {
    pushEntry("WARN", args);
    originalConsole.warn.apply(console, args);
  };

  console.error = (...args: unknown[]) => {
    pushEntry("ERROR", args);
    originalConsole.error.apply(console, args);
  };

  window.onerror = (
    _message: string | Event,
    _source?: string,
    _lineno?: number,
    _colno?: number,
    error?: Error,
  ) => {
    pushEntry("ERROR", [`[App] unhandled error error_type=${errorType(error)}`]);
  };

  window.onunhandledrejection = (event: PromiseRejectionEvent) => {
    pushEntry("ERROR", [
      `[App] unhandled rejection error_type=${errorType(event.reason)}`,
    ]);
  };
}

export function getLogBuffer(): string[] {
  return [...buffer];
}

export function clearLogBuffer(): void {
  buffer.length = 0;
  bufferGeneration += 1;
}

export function pauseLogCapture(): void {
  capturePaused = true;
}

export function resumeLogCapture(): void {
  capturePaused = false;
}

export const LOCKDOWN_CHANGED_EVENT = "lockdown.changed" as const;
const LOCKDOWN_CHANNEL = "whisperstream-lockdown-v1";

export interface LockdownChangedPayload {
  enabled: boolean;
}

function isLockdownChangedPayload(value: unknown): value is LockdownChangedPayload {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { enabled?: unknown }).enabled === "boolean"
  );
}

/** Track and broadcast Lockdown transitions without suppressing safe diagnostics. */
export function applyLockdownCaptureState(_enabled: boolean): void {
  resumeLogCapture();
}

/** Called synchronously by the initiating webview before an enable request. */
export function prepareForLockdownEnable(): void {
  resumeLogCapture();
}

/**
 * Reconcile a rejected enable against the effective backend boundary. A
 * partial enable may have left either the config flag or envelope behind, so
 * propagate the authoritative state while safe capture remains active.
 */
export async function recoverFromFailedLockdownEnable(): Promise<void> {
  const enabled = await fetchEffectiveLockdownEnabled();
  if (enabled === null) return;
  broadcastLockdownChanged(enabled);
}

let lockdownChannel: BroadcastChannel | null = null;
let lockdownListenersStarted = false;

function receiveLockdownTransition(payload: unknown): void {
  if (!isLockdownChangedPayload(payload)) return;
  applyLockdownCaptureState(payload.enabled);
}

function startLockdownTransitionListeners(): void {
  if (lockdownListenersStarted || typeof window === "undefined") return;
  lockdownListenersStarted = true;

  if (typeof BroadcastChannel !== "undefined") {
    lockdownChannel = new BroadcastChannel(LOCKDOWN_CHANNEL);
    lockdownChannel.addEventListener("message", (event: MessageEvent<unknown>) => {
      receiveLockdownTransition(event.data);
    });
  }

  // Tauri's app-wide event reaches every WebView2 window. In browser/dev mode
  // the native bridge rejects and BroadcastChannel remains the fallback.
  void listen<LockdownChangedPayload>(LOCKDOWN_CHANGED_EVENT, (event) => {
    receiveLockdownTransition(event.payload);
  }).catch(() => undefined);
}

/** Apply locally, then notify every other webview through native/browser seams. */
export function broadcastLockdownChanged(enabled: boolean): void {
  const payload: LockdownChangedPayload = { enabled };
  applyLockdownCaptureState(enabled);
  lockdownChannel?.postMessage(payload);
  void emit(LOCKDOWN_CHANGED_EVENT, payload).catch(() => undefined);
}

async function fetchEffectiveLockdownEnabled(): Promise<boolean | null> {
  const base = (import.meta as any).env?.VITE_API_URL || "http://localhost:38420";
  try {
    const response = await fetch(`${base}/api/lockdown/status`);
    if (!response.ok) return null;
    const status = (await response.json()) as unknown;
    if (
      typeof status === "object" &&
      status !== null &&
      typeof (status as { enabled?: unknown }).enabled === "boolean"
    ) {
      return (status as { enabled: boolean }).enabled;
    }
  } catch {
    // The caller remains paused and empty.
  }
  return null;
}

let syncInterval: ReturnType<typeof setInterval> | null = null;

// Identifies which webview these logs came from, so the backend can keep a
// separate buffer per source instead of one window's sync clobbering another's.
function getSource(): string {
  if (typeof window === "undefined") return "unknown";
  const path = window.location.pathname;
  if (path === "/popup/review") return "popup-review";
  if (path === "/popup/update") return "popup-update";
  if (path === "/popup/eula") return "popup-eula";
  if (path === "/hud") return "hud";
  if (path === "/snap-guide") return "snap-guide";
  if (path === "/recording-control") return "recording-control";
  return "main";
}

/** Upper bound for one log-sync POST. */
export const LOG_SYNC_TIMEOUT_MS = 5_000;
export const LOG_SYNC_INTERVAL_MS = 10_000;

// Generation of the ring the backend last acknowledged (-1: never).
let lastSyncedGeneration = -1;
let syncInFlight: Promise<void> | null = null;

/**
 * POST the whole ring once (the backend keeps the latest ring per source), but
 * only if it changed since the last successful sync. Single-flight: an
 * in-flight request is never overlapped. A periodic tick joins it; a flush
 * waits for it and then syncs anything logged meanwhile. Each request is
 * bounded by LOG_SYNC_TIMEOUT_MS.
 */
function syncToBackend(mode: "periodic" | "flush" = "periodic"): Promise<void> {
  if (syncInFlight !== null) {
    if (mode === "periodic") return syncInFlight;
    return syncInFlight.then(() => syncToBackend("periodic"));
  }
  if (capturePaused || buffer.length === 0) return Promise.resolve();
  const generation = bufferGeneration;
  if (generation === lastSyncedGeneration) return Promise.resolve();
  const base = (import.meta as any).env?.VITE_API_URL || "http://localhost:38420";
  const body = JSON.stringify({ source: getSource(), frontend_logs: [...buffer] });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), LOG_SYNC_TIMEOUT_MS);
  const request = fetch(`${base}/api/logs/frontend`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    signal: controller.signal,
  })
    .then((response) => {
      if (response.ok) lastSyncedGeneration = generation;
    })
    .catch(() => undefined)
    .finally(() => {
      clearTimeout(timer);
      if (syncInFlight === request) syncInFlight = null;
    });
  syncInFlight = request;
  return request;
}

export function startLogSync(): void {
  if (syncInterval) return;
  syncInterval = setInterval(() => {
    void syncToBackend("periodic");
  }, LOG_SYNC_INTERVAL_MS);
  window.addEventListener("beforeunload", () => {
    void syncToBackend("flush");
  });
}

/**
 * Install capture/sync exactly once. Lockdown does not suppress the sanitized
 * local diagnostic ring or its localhost sync.
 */
export async function startLockdownAwareLogCapture(): Promise<void> {
  initLogCapture();
  startLogSync();
  startLockdownTransitionListeners();
  resumeLogCapture();
}

// Force an immediate sync. Use this from popups before calling getCurrentWindow().hide()
// so the popup's logs reach the backend before the user loses access to them.
export function flushLogSync(): Promise<void> {
  return syncToBackend("flush");
}
