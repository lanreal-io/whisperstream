// @vitest-environment node

/**
 * Vitest coverage for the local-LLM API helpers added in Task 2.1.
 *
 * Tests:
 *   - getEnhancementModels("local") parses the expected response shape.
 *   - startLocalLLMDownload posts the right URL with the right body.
 *   - startLocalLLMDownload resolves on {status:"started"} and rejects on 4xx.
 *   - cancelLocalLLMDownload posts the right URL.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { AGENT_PROVIDER_LIFECYCLE_API, getSetupCodexPluginDetail, armSetupDeliveryPreview } from "./api/agents";
import { BASE } from "./api/client";
import { activateLocalCandidate, getEnhancementModels, startLocalLLMDownload, cancelLocalLLMDownload } from "./api/enhancement";
import { enhanceDevTranscript, startTranscriptBakeoff, cancelTranscriptBakeoff, retryTranscriptBakeoffResult, TranscriptBakeoffApiError } from "./api/transcripts";
import { disableLockdown, enableLockdown } from "./api/lockdown";
import { getSpeechStatus, getSpeechVoices, speakSample, startSpeechModelDownload } from "./api/speech";
import { saveConfig } from "./api/config";
import type { AppConfig } from "./api/config";
import * as logCapture from "./logCapture";

// ---------------------------------------------------------------------------
// fetch mock helpers
// ---------------------------------------------------------------------------

function makeFetchMock(
  status: number,
  body: unknown,
): ReturnType<typeof vi.fn> {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  });
}

let originalFetch: typeof globalThis.fetch;

beforeEach(() => {
  originalFetch = globalThis.fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("coding-agent lifecycle adapters", () => {
  it("sends force only for an explicit Connect", async () => {
    globalThis.fetch = makeFetchMock(200, { ok: true, status: "installed" });
    await AGENT_PROVIDER_LIFECYCLE_API.claude.enable({ force: true });
    await AGENT_PROVIDER_LIFECYCLE_API.claude.enable();
    expect(globalThis.fetch).toHaveBeenNthCalledWith(1, `${BASE}/api/claude-hud/enable`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ force: true }),
    });
    expect(globalThis.fetch).toHaveBeenNthCalledWith(2, `${BASE}/api/claude-hud/enable`, { method: "POST" });
  });

  it("routes Codex lifecycle actions and its private setup receipt independently", async () => {
    globalThis.fetch = makeFetchMock(200, {
      ok: true,
      status: "installed",
      trust_required: true,
    });

    await AGENT_PROVIDER_LIFECYCLE_API.codex.enable();
    await AGENT_PROVIDER_LIFECYCLE_API.codex.status();
    await AGENT_PROVIDER_LIFECYCLE_API.codex.disable();
    await getSetupCodexPluginDetail();

    expect(globalThis.fetch).toHaveBeenNthCalledWith(
      1,
      `${BASE}/api/codex-hud/enable`,
      { method: "POST" },
    );
    expect(globalThis.fetch).toHaveBeenNthCalledWith(
      2,
      `${BASE}/api/codex-hud/status`,
      undefined,
    );
    expect(globalThis.fetch).toHaveBeenNthCalledWith(
      3,
      `${BASE}/api/codex-hud/disable`,
      { method: "POST" },
    );
    expect(globalThis.fetch).toHaveBeenNthCalledWith(
      4,
      `${BASE}/api/setup/agent-speak/codex-plugin-detail`,
      undefined,
    );
  });
});

// ---------------------------------------------------------------------------
// getEnhancementModels("local")
// ---------------------------------------------------------------------------

describe("getEnhancementModels('local')", () => {
  it("parses the full local response shape", async () => {
    const mockBody = {
      provider: "local",
      bundled: [
        {
          id: "gemma4-e2b-instruct",
          label: "Gemma 4 E2B Instruct",
          disk_mb: 2962,
          ram_mb: 1600,
          recommended: true,
          gguf_repo: "unsloth/gemma-4-E2B-it-GGUF",
          gguf_filename: "gemma-4-E2B-it-Q4_K_M.gguf",
          source: "production",
          experiment_id: null,
          parameter_size: null,
          role: null,
          status: null,
          serving_contract: null,
          trained_modes: null,
          artifact_available: true,
          selectable: true,
          downloadable: true,
          active: true,
        },
      ],
      installed_ids: ["gemma4-e2b-instruct"],
      active_id: "gemma4-e2b-instruct",
      running: true,
    };
    globalThis.fetch = makeFetchMock(200, mockBody);

    const result = await getEnhancementModels("local");

    expect(result.provider).toBe("local");
    expect(result.bundled).toHaveLength(1);

    // First entry: all seven catalog keys
    const first = result.bundled[0];
    expect(first.id).toBe("gemma4-e2b-instruct");
    expect(first.label).toBe("Gemma 4 E2B Instruct");
    expect(first.disk_mb).toBe(2962);
    expect(first.ram_mb).toBe(1600);
    expect(first.recommended).toBe(true);
    expect(first.gguf_repo).toBe("unsloth/gemma-4-E2B-it-GGUF");
    expect(first.gguf_filename).toBe("gemma-4-E2B-it-Q4_K_M.gguf");
    expect(first.source).toBe("production");
    expect(first.experiment_id).toBeNull();
    expect(first.parameter_size).toBeNull();
    expect(first.role).toBeNull();
    expect(first.status).toBeNull();
    expect(first.serving_contract).toBeNull();
    expect(first.trained_modes).toBeNull();
    expect(first.artifact_available).toBe(true);
    expect(first.selectable).toBe(true);
    expect(first.downloadable).toBe(true);
    expect(first.active).toBe(true);

    // Local-only fields
    expect(result.installed_ids).toEqual(["gemma4-e2b-instruct"]);
    expect(result.active_id).toBe("gemma4-e2b-instruct");
    expect(result.running).toBe(true);
  });

  it("sends provider=local in the query string", async () => {
    globalThis.fetch = makeFetchMock(200, {
      provider: "local",
      bundled: [],
    });

    await getEnhancementModels("local");

    expect(globalThis.fetch).toHaveBeenCalledOnce();
    const calledUrl = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls[0][0] as string;
    expect(calledUrl).toContain("provider=local");
    expect(calledUrl).toContain("/api/enhancement/models");
    expect(calledUrl).not.toContain("include_all");
  });

  it("returns empty installed_ids when field is absent (older backend)", async () => {
    // Backend may not send installed_ids on non-local endpoints.
    globalThis.fetch = makeFetchMock(200, {
      provider: "local",
      bundled: [],
    });

    const result = await getEnhancementModels("local");
    // installed_ids is typed as optional — verify the caller handles absence
    expect(result.installed_ids).toBeUndefined();
  });

  it("keeps candidate metadata optional for legacy model rows", async () => {
    globalThis.fetch = makeFetchMock(200, {
      provider: "local",
      bundled: [{ id: "stream-1", label: "Stream 1" }],
    });

    const result = await getEnhancementModels("local");
    const legacy = result.bundled[0];

    expect(legacy.source).toBeUndefined();
    expect(legacy.experiment_id).toBeUndefined();
    expect(legacy.artifact_available).toBeUndefined();
    expect(legacy.selectable).toBeUndefined();
    expect(legacy.downloadable).toBeUndefined();
    expect(legacy.active).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// activateLocalCandidate
// ---------------------------------------------------------------------------

describe("activateLocalCandidate", () => {
  it("POSTs the exact candidate id to /api/enhancement/local-activate", async () => {
    globalThis.fetch = makeFetchMock(200, {
      status: "activated",
      model_id: "w2wrap1p2b-s3",
    });

    const result = await activateLocalCandidate("w2wrap1p2b-s3");

    expect(result).toEqual({
      status: "activated",
      model_id: "w2wrap1p2b-s3",
    });
    const [calledUrl, init] = (globalThis.fetch as ReturnType<typeof vi.fn>)
      .mock.calls[0] as [string, RequestInit];
    expect(calledUrl).toContain("/api/enhancement/local-activate");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({ "Content-Type": "application/json" });
    expect(JSON.parse(init.body as string)).toEqual({
      model_id: "w2wrap1p2b-s3",
    });
  });

  it("rejects when the candidate artifact is unavailable", async () => {
    globalThis.fetch = makeFetchMock(409, {
      error: "artifact_unavailable",
      model_id: "w8wrap_closure-s1",
    });

    await expect(
      activateLocalCandidate("w8wrap_closure-s1"),
    ).rejects.toThrow(/409/);
  });
});

describe("enhanceDevTranscript", () => {
  it("POSTs the active model and exact execution mode", async () => {
    globalThis.fetch = makeFetchMock(200, {
      model_id: "qwen35_iclv3-s1",
      serving_contract: "chatml",
      mode: "batched",
      profile_mode: "code",
      chunk_count: 3,
      elapsed_ms: 814,
      output: "const answer = 42;",
    });

    const result = await enhanceDevTranscript(
      "saved transcript",
      "qwen35_iclv3-s1",
      "batched",
    );

    expect(result.output).toBe("const answer = 42;");
    const [calledUrl, init] = (globalThis.fetch as ReturnType<typeof vi.fn>)
      .mock.calls[0] as [string, RequestInit];
    expect(calledUrl).toContain(
      "/api/dev/transcripts/saved%20transcript/enhance",
    );
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      model_id: "qwen35_iclv3-s1",
      mode: "batched",
    });
  });

  it("preserves the backend error code and fidelity metadata", async () => {
    globalThis.fetch = makeFetchMock(409, {
      error: "unsupported_profile_mode",
      model_id: "b1emoji_r1-s1",
      profile_mode: "email",
    });

    await expect(
      enhanceDevTranscript("saved-1", "b1emoji_r1-s1", "whole"),
    ).rejects.toMatchObject({
      status: 409,
      code: "unsupported_profile_mode",
      modelId: "b1emoji_r1-s1",
      profileMode: "email",
    });
  });
});

describe("startLocalLLMDownload", () => {
  it("POSTs to /api/enhancement/local-download/start with the model_id body", async () => {
    globalThis.fetch = makeFetchMock(200, { status: "started" });

    await startLocalLLMDownload("gemma4-e2b-instruct");

    expect(globalThis.fetch).toHaveBeenCalledOnce();
    const [calledUrl, init] = (globalThis.fetch as ReturnType<typeof vi.fn>)
      .mock.calls[0] as [string, RequestInit];
    expect(calledUrl).toContain("/api/enhancement/local-download/start");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({ "Content-Type": "application/json" });
    expect(JSON.parse(init.body as string)).toEqual({
      model_id: "gemma4-e2b-instruct",
    });
  });

  it("resolves without throwing on {status:'started'} response", async () => {
    globalThis.fetch = makeFetchMock(200, { status: "started" });
    await expect(
      startLocalLLMDownload("gemma4-e2b-instruct"),
    ).resolves.toBeUndefined();
  });

  it("rejects with a useful error on 400 unknown model_id", async () => {
    globalThis.fetch = makeFetchMock(400, { detail: "Unknown model_id: bad-model" });
    await expect(
      startLocalLLMDownload("bad-model"),
    ).rejects.toThrow(/400/);
  });

  it("rejects with a useful error on 409 busy (download already in progress)", async () => {
    globalThis.fetch = makeFetchMock(409, { detail: "Download already in progress" });
    await expect(
      startLocalLLMDownload("gemma4-e2b-instruct"),
    ).rejects.toThrow(/409/);
  });
});

// ---------------------------------------------------------------------------
// cancelLocalLLMDownload
// ---------------------------------------------------------------------------

describe("cancelLocalLLMDownload", () => {
  it("POSTs to /api/enhancement/local-download/cancel", async () => {
    globalThis.fetch = makeFetchMock(200, { status: "cancelled" });

    await cancelLocalLLMDownload();

    expect(globalThis.fetch).toHaveBeenCalledOnce();
    const [calledUrl, init] = (globalThis.fetch as ReturnType<typeof vi.fn>)
      .mock.calls[0] as [string, RequestInit];
    expect(calledUrl).toContain("/api/enhancement/local-download/cancel");
    expect(init.method).toBe("POST");
  });

  it("resolves without throwing on success", async () => {
    globalThis.fetch = makeFetchMock(200, { status: "cancelled" });
    await expect(cancelLocalLLMDownload()).resolves.toBeUndefined();
  });
});

describe("saveConfig output guard mode", () => {
  it("types and echoes the backend-only rollout field", async () => {
    const mode: AppConfig["enhancement_output_guard_mode"] = "log";
    globalThis.fetch = makeFetchMock(200, {
      enhancement_output_guard_mode: mode,
    });

    const result = await saveConfig({
      enhancement_output_guard_mode: mode,
    });

    const [, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      enhancement_output_guard_mode: "log",
    });
    expect(result.enhancement_output_guard_mode).toBe("log");
  });
});

describe("experimental transcript Bakeoff API", () => {
  it("starts with typed model/profile selections and templates the record ID in diagnostics", async () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
    globalThis.fetch = makeFetchMock(202, {
      run_id: "run-secret-92",
      generation: 7,
      total_count: 2,
    });

    const result = await startTranscriptBakeoff("record-secret-92", [
      { model_id: "candidate-a", profile_id: "chat" },
      { model_id: "candidate-a", profile_id: "code" },
    ]);

    expect(result.generation).toBe(7);
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls[0] as [string, RequestInit];
    expect(url).toContain("/api/experimental/transcripts/record-secret-92/bakeoff");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      selections: [
        { model_id: "candidate-a", profile_id: "chat" },
        { model_id: "candidate-a", profile_id: "code" },
      ],
    });
    expect(log.mock.calls.flat().join(" ")).not.toContain("record-secret-92");
    expect(log.mock.calls.flat().join(" ")).toContain(
      "/api/experimental/transcripts/{record_id}/bakeoff",
    );
  });

  it("normalizes run IDs for cancel and retry", async () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
    globalThis.fetch = makeFetchMock(202, {
      run_id: "run-secret-92",
      generation: 8,
      pair_index: 1,
    });

    await cancelTranscriptBakeoff("run-secret-92");
    await retryTranscriptBakeoffResult("run-secret-92", 1);

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls as [
      string,
      RequestInit,
    ][];
    expect(calls[0][0]).toContain(
      "/api/experimental/bakeoffs/run-secret-92/cancel",
    );
    expect(calls[1][0]).toContain(
      "/api/experimental/bakeoffs/run-secret-92/retry",
    );
    expect(JSON.parse(calls[1][1].body as string)).toEqual({ pair_index: 1 });
    expect(log.mock.calls.flat().join(" ")).not.toContain("run-secret-92");
    expect(log.mock.calls.flat().join(" ")).toContain(
      "/api/experimental/bakeoffs/{run_id}/cancel",
    );
  });

  it("preserves stable recovery codes and memory facts", async () => {
    globalThis.fetch = makeFetchMock(409, {
      error: "insufficient_current_memory",
      required_mb: 4096,
      available_mb: 2048,
    });

    const error = await startTranscriptBakeoff("record", [
      { model_id: "one", profile_id: "chat" },
      { model_id: "two", profile_id: "chat" },
    ]).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(TranscriptBakeoffApiError);
    expect(error).toMatchObject({
      status: 409,
      code: "insufficient_current_memory",
      requiredMb: 4096,
      availableMb: 2048,
    });
  });
});

describe("Lockdown diagnostic API boundary", () => {
  it("keeps diagnostics active and broadcasts entry after success", async () => {
    const prepare = vi.spyOn(logCapture, "prepareForLockdownEnable");
    const changed = vi.spyOn(logCapture, "broadcastLockdownChanged");
    globalThis.fetch = makeFetchMock(200, {
      recovery_code: null,
      config: { lockdown_mode: true },
    });

    await enableLockdown({ historyEnabled: false });

    expect(prepare).toHaveBeenCalledOnce();
    expect(changed).toHaveBeenCalledWith(true);
  });

  it("reconciles a failed enable before rejecting", async () => {
    const prepare = vi.spyOn(logCapture, "prepareForLockdownEnable");
    const recover = vi
      .spyOn(logCapture, "recoverFromFailedLockdownEnable")
      .mockResolvedValue(undefined);
    globalThis.fetch = makeFetchMock(500, { error: "enable_failed" });

    await expect(enableLockdown({ historyEnabled: false })).rejects.toThrow();

    expect(prepare).toHaveBeenCalledOnce();
    expect(recover).toHaveBeenCalledOnce();
  });

  it("broadcasts exit only after a successful disable", async () => {
    const changed = vi.spyOn(logCapture, "broadcastLockdownChanged");
    globalThis.fetch = makeFetchMock(200, {
      ok: true,
      config: { lockdown_mode: false },
    });

    await disableLockdown("password");

    expect(changed).toHaveBeenCalledWith(false);
  });
});

describe("adaptive speech API", () => {
  it("reads the structured selection and per-engine status", async () => {
    const body = {
      requested: "auto",
      preferred_engine: "kokoro",
      resolved_engine: "supertonic",
      active_engine: "supertonic",
      reason: "missing_kokoro",
      memory: { total_mb: 32768, available_mb: 8192, eligible: true },
      engines: {
        supertonic: {
          key: "supertonic",
          label: "Supertonic",
          installed: true,
          downloading: false,
          download_bytes: 128774318,
          installed_bytes: 145316356,
        },
        kokoro: {
          key: "kokoro",
          label: "Kokoro",
          installed: false,
          downloading: false,
          download_bytes: 318413913,
          installed_bytes: 345395578,
        },
      },
      current_download: null,
      recommended_downloads: ["kokoro"],
    };
    globalThis.fetch = makeFetchMock(200, body);

    const result = await getSpeechStatus();

    expect(result.reason).toBe("missing_kokoro");
    expect(result.engines.kokoro.download_bytes).toBe(318413913);
    expect(result.recommended_downloads).toEqual(["kokoro"]);
  });

  it("requests voices for one concrete engine", async () => {
    globalThis.fetch = makeFetchMock(200, {
      engine: "kokoro",
      engine_label: "Kokoro",
      voices: [
        {
          id: "af_heart",
          sid: "af_heart",
          label: "Heart",
          group: "female",
        },
      ],
    });

    const result = await getSpeechVoices("kokoro");

    const [url] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls[0] as [string, RequestInit];
    expect(url).toContain("/api/speech/voices?engine=kokoro");
    expect(result.voices[0]).toEqual({
      id: "af_heart",
      sid: "af_heart",
      label: "Heart",
      group: "female",
    });
  });

  it("preserves Supertonic presentation metadata and numeric IDs", async () => {
    globalThis.fetch = makeFetchMock(200, {
      engine: "supertonic",
      engine_label: "Supertonic",
      voices: [
        {
          id: 0,
          sid: 0,
          label: "Ava",
          group: "female",
          reference_label: "Voice 0",
        },
      ],
    });

    const result = await getSpeechVoices("supertonic");

    expect(result.voices[0]).toEqual({
      id: 0,
      sid: 0,
      label: "Ava",
      group: "female",
      reference_label: "Voice 0",
    });
    expect(typeof result.voices[0].id).toBe("number");
  });

  it("posts concrete engine identity for downloads", async () => {
    globalThis.fetch = makeFetchMock(200, {
      status: "started",
      engine: "supertonic",
    });

    await startSpeechModelDownload("supertonic");

    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls[0] as [string, RequestInit];
    expect(url).toContain("/api/speech/download/start");
    expect(JSON.parse(init.body as string)).toEqual({
      engine: "supertonic",
    });
  });

  it("posts the visible concrete engine and voice for samples", async () => {
    globalThis.fetch = makeFetchMock(200, { ok: true });

    await speakSample("kokoro", "af_bella");

    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls[0] as [string, RequestInit];
    expect(url).toContain("/api/speech/speak-sample");
    expect(JSON.parse(init.body as string)).toEqual({
      engine: "kokoro",
      voice: "af_bella",
    });
  });

  it("posts delivery preview override payload", async () => {
    globalThis.fetch = makeFetchMock(200, { ok: true });

    await armSetupDeliveryPreview({
      show_summary_text: true,
      agent_speech_enabled: false,
    });

    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls[0] as [string, RequestInit];
    expect(url).toContain("/api/setup/agent-speak/delivery-preview");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      show_summary_text: true,
      agent_speech_enabled: false,
    });
  });
});

