import { BASE, logApiRequest, logApiError, logApiResponse } from "./client";
import { errorType } from "../logCapture";


export async function dismissReview(reason: "later" | "not-now" | "never" | "github"): Promise<void> {
  const method = "POST";
  const path = "/api/review/dismiss";
  const t0 = performance.now();
  logApiRequest(method, path);
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    });
  } catch (fetchErr) {
    logApiError(method, path, "network", errorType(fetchErr));
    return;
  }
  const durationMs = performance.now() - t0;
  if (!res.ok) {
    logApiError(method, path, res.status, "HttpError");
  } else {
    logApiResponse(method, path, res.status, durationMs);
  }
}
