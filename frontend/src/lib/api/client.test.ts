// @vitest-environment node
import { afterEach, expect, it, vi } from "vitest";
import { BASE, fetchJSON } from "./client";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("redacts record identifiers and queries while preserving structured errors", async () => {
  const log = vi.spyOn(console, "log").mockImplementation(() => {});
  const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 403, text: async () => JSON.stringify({ error_code: "eula_required", detail: "private detail" }) }));
  await expect(fetchJSON(`${BASE}/api/transcripts/private-record/audio?secret=private-query`)).rejects.toMatchObject({ errorCode: "eula_required" });
  const lines = JSON.stringify([log.mock.calls, errorLog.mock.calls]);
  expect(lines).toContain("/api/transcripts/{record_id}/audio");
  expect(lines).not.toMatch(/private-record|private-query|private detail/);
});

it("preserves network failure identity without logging its content", async () => {
  vi.spyOn(console, "log").mockImplementation(() => {});
  const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
  const failure = new TypeError("private connection detail");
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(failure));
  await expect(fetchJSON(`${BASE}/api/config`)).rejects.toBe(failure);
  expect(JSON.stringify(errorLog.mock.calls)).toContain("status=network error_type=TypeError");
  expect(JSON.stringify(errorLog.mock.calls)).not.toContain("private connection detail");
});
