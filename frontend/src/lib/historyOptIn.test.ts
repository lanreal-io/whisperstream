import { afterEach, describe, expect, it, vi } from "vitest";
import { enableTranscriptHistory } from "./api/transcripts";

afterEach(() => vi.unstubAllGlobals());
describe("history opt-in request", () => {
  it.each([true, false])("sends explicit history consent and only requests Keychain when needed (%s)", async (keychainConsent) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await enableTranscriptHistory(keychainConsent);
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/api\/transcripts\/enable$/);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({ history_consent: true, ...(keychainConsent ? { keychain_consent: true } : {}) });
  });
});
