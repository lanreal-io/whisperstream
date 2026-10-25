import { afterEach, describe, expect, it, vi } from "vitest";
import {
  confirmHistoryRecovery,
  historyRecoveryErrorCode,
  resetHistory,
  setHistoryRecoveryPassword,
  unlockHistoryWithRecoveryKey,
  unlockHistoryWithRecoveryPassword,
} from "./historyRecovery";

function mockFetch(status: number, body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sentBody(fetchMock: ReturnType<typeof vi.fn>) {
  return JSON.parse(fetchMock.mock.calls[0][1].body as string);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("history recovery client", () => {
  it("sends exactly one secret to unlock", async () => {
    let fetchMock = mockFetch(200, { ok: true });
    await unlockHistoryWithRecoveryKey("ABCD-EFGH-JKLM-NPQR-STUV");
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/api\/history\/recovery\/unlock$/);
    expect(sentBody(fetchMock)).toEqual({ recovery_key: "ABCD-EFGH-JKLM-NPQR-STUV" });

    fetchMock = mockFetch(200, { ok: false, error: "recovery_password_invalid", lockout_remaining_seconds: 0 });
    const result = await unlockHistoryWithRecoveryPassword("wrong");
    expect(sentBody(fetchMock)).toEqual({ password: "wrong" });
    expect(result.error).toBe("recovery_password_invalid");
  });

  it("names the shown key on confirm, omits an empty password and removes it with null", async () => {
    let fetchMock = mockFetch(200, { ok: true });
    await confirmHistoryRecovery("gen-1");
    expect(sentBody(fetchMock)).toEqual({ generation: "gen-1" });

    fetchMock = mockFetch(200, { ok: true, has_recovery_password: false });
    await setHistoryRecoveryPassword(null);
    expect(sentBody(fetchMock)).toEqual({});
  });

  it("maps backend error codes", async () => {
    mockFetch(400, { ok: false, error: "password_too_common" });
    const error = await confirmHistoryRecovery("gen-1", "password1234").catch((e) => e);
    expect(historyRecoveryErrorCode(error)).toBe("password_too_common");

    mockFetch(400, { ok: false, error: "confirm_required" });
    const resetError = await resetHistory("delete").catch((e) => e);
    expect(historyRecoveryErrorCode(resetError)).toBe("confirm_required");

    expect(historyRecoveryErrorCode(new Error("network down"))).toBeNull();
    expect(
      historyRecoveryErrorCode(new Error('403: {"detail":"history_disabled_by_organization"}')),
    ).toBe("history_disabled_by_organization");
  });
});

describe("stale recovery envelope", () => {
  it("recognizes recovery_key_stale", () => {
    expect(historyRecoveryErrorCode(new Error('409: {"ok":false,"error":"recovery_key_stale"}'))).toBe("recovery_key_stale");
  });
});
