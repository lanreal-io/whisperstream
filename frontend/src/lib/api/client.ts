
import { errorType } from "../logCapture";


export const BASE = import.meta.env.VITE_API_URL || "http://localhost:38420";


// ---------------------------------------------------------------------------
// [API] request / response logging helper
// Exempted paths: /api/logs/frontend and /api/logs/export — logging those
// would cause recursive sync spam when logCapture flushes to the backend.
// ---------------------------------------------------------------------------
export function extractPath(url: string): string {
  try {
    return new URL(url).pathname;
  } catch {
    return "invalid";
  }
}


export function safeApiPath(path: string): string {
  const match = path.match(/^\/api\/transcripts\/[^/]+(\/audio)?$/);
  if (match) return `/api/transcripts/{record_id}${match[1] ?? ""}`;
  if (/^\/api\/dev\/transcripts\/[^/]+\/enhance$/.test(path)) {
    return "/api/dev/transcripts/{record_id}/enhance";
  }
  if (/^\/api\/experimental\/transcripts\/[^/]+\/reenhance$/.test(path)) {
    return "/api/experimental/transcripts/{record_id}/reenhance";
  }
  if (/^\/api\/experimental\/transcripts\/[^/]+\/bakeoff$/.test(path)) {
    return "/api/experimental/transcripts/{record_id}/bakeoff";
  }
  if (/^\/api\/experimental\/bakeoffs\/[^/]+\/cancel$/.test(path)) {
    return "/api/experimental/bakeoffs/{run_id}/cancel";
  }
  if (/^\/api\/experimental\/bakeoffs\/[^/]+\/retry$/.test(path)) {
    return "/api/experimental/bakeoffs/{run_id}/retry";
  }
  return path;
}


export function isLogExempt(path: string): boolean {
  return (
    path.includes("/api/logs/frontend") ||
    path.includes("/api/logs/export") ||
    path.includes("/api/accessibility/screen-reader")
  );
}


export function logApiRequest(method: string, path: string): void {
  if (isLogExempt(path)) return;
  console.log(`[API] request method=${method} path=${safeApiPath(path)}`);
}


export function logApiResponse(method: string, path: string, status: number, durationMs: number): void {
  if (isLogExempt(path)) return;
  console.log(`[API] response method=${method} path=${safeApiPath(path)} status=${status} duration_ms=${Math.round(durationMs)}`);
}


export function logApiError(method: string, path: string, status: number | "network", errorName: string): void {
  if (isLogExempt(path)) return;
  console.error(`[API] error method=${method} path=${safeApiPath(path)} status=${status} error_type=${errorName}`);
}


export async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const path = extractPath(url);
  const t0 = performance.now();
  logApiRequest(method, path);
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (err) {
    logApiError(method, path, "network", errorType(err));
    throw err;
  }
  const durationMs = performance.now() - t0;
  if (!res.ok) {
    const text = await res.text();
    logApiError(method, path, res.status, "HttpError");
    const error: Error & { errorCode?: string } = new Error(`${res.status}: ${text}`);
    try { error.errorCode = JSON.parse(text)?.error_code; } catch { /* Non-JSON error. */ }
    throw error;
  }
  logApiResponse(method, path, res.status, durationMs);
  return res.json();
}
