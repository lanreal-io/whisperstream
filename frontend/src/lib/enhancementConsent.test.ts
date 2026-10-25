// @vitest-environment node
import { describe, it, expect, vi, afterEach } from "vitest";
import { getBYOKConsentContext, recordBYOKConsent, type BYOKConsentContext } from "./api/consent";
import { type AppConfig } from "./api/config";
import { consentRequest, consentConfigPatch, enhancementErrorMessage } from "./enhancementConsent";

afterEach(() => { vi.unstubAllGlobals(); });

describe("recipient consent API", () => {
  it("sends draft settings only to the context endpoint", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ context_token: "token" }) });
    vi.stubGlobal("fetch", fetch);
    await getBYOKConsentContext({ provider: "custom", endpoint: "https://example.com/v2", model: "new-model" });
    expect(fetch.mock.calls[0][0]).toMatch(/\/api\/byok-consent\/context$/);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ provider: "custom", endpoint: "https://example.com/v2", model: "new-model" });
  });
  it("echoes the exact reviewed identity, model and canonical body when accepting", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
    vi.stubGlobal("fetch", fetch);
    const context = { provider: "custom", endpoint: "https://example.com:8443/v2", model: "new-model", context_token: "reviewed-token", dialog_body_text: "Exact\nbody" } as BYOKConsentContext;
    await recordBYOKConsent(context);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(context);
    expect(consentConfigPatch(context)).toEqual({ enhancement_provider: "custom", enhancement_enabled: true, enhancement_custom_base_url: context.endpoint, enhancement_custom_model: context.model });
  });
  it("preserves stable errors without showing response contents", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 409, text: async () => JSON.stringify({ error_code: "consent_changed", detail: "not for display" }) }));
    try { await getBYOKConsentContext({ provider: "openai" }); throw new Error("should reject"); }
    catch (error) { expect(enhancementErrorMessage(error)).toBe("Connection settings changed. Review the updated privacy notice."); }
  });
  it("binds named providers to their configured model", () => {
    expect(consentRequest({ enhancement_provider: "openai", enhancement_openai_model: "selected-model" } as AppConfig)).toEqual({ provider: "openai", model: "selected-model" });
  });
});
