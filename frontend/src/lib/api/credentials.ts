import { BASE, fetchJSON } from "./client";

export async function removeEnhancementKey(): Promise<{
  status: string;
  provider: string;
  consent_records_removed: number;
}> {
  return fetchJSON(`${BASE}/api/enhancement/remove-key`, { method: "POST" });
}
