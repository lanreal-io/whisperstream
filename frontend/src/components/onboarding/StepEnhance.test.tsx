import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppConfig } from "../../lib/api/config";
import type { AppEvent } from "../../lib/socket/events";
import { makeConfig } from "../../pages/__tests__/homeConfigFixture";
import { StepEnhance, type FooterSpec } from "./StepEnhance";
import examples from "./enhanceExamples.json";

const saveConfig = vi.fn<(patch: Partial<AppConfig>) => Promise<AppConfig>>();
const startBenchmark = vi.fn();
const cancelLocalLLMDownload = vi.fn();
const deleteLocalModel = vi.fn();
const getEnhancementModels = vi.fn();
const getLlamaStatus = vi.fn();

vi.mock("../../lib/api/config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/config")>();
  return { ...actual, saveConfig: (patch: Partial<AppConfig>) => saveConfig(patch) };
});
vi.mock("../../lib/api/enhancement", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/enhancement")>();
  return {
    ...actual,
    startBenchmark: (...a: unknown[]) => startBenchmark(...a),
    cancelLocalLLMDownload: () => cancelLocalLLMDownload(),
    deleteLocalModel: (id: string) => deleteLocalModel(id),
    getEnhancementModels: (p: string) => getEnhancementModels(p),
    getLlamaStatus: () => getLlamaStatus(),
  };
});

const subscribers = new Set<(event: AppEvent) => void>();
function emit(event: AppEvent) {
  act(() => [...subscribers].forEach((s) => s(event)));
}

function renderStep(config = makeConfig({ enhancement_enabled: false, enhancement_provider: "gemini" })) {
  const onConfigChange = vi.fn<(patch: Partial<AppConfig>) => void>();
  const onFinish = vi.fn();
  let footer: FooterSpec | null = null;
  render(
    <StepEnhance
      config={config}
      subscribe={(h) => {
        subscribers.add(h);
        return () => subscribers.delete(h);
      }}
      onConfigChange={onConfigChange}
      onFinish={onFinish}
      onFooterChange={(spec) => {
        footer = spec;
      }}
    />,
  );
  return {
    onConfigChange,
    onFinish,
    footer: () => footer as unknown as FooterSpec,
    click: (slot: "ghost" | "secondary" | "primary") =>
      act(() => (footer as unknown as FooterSpec)[slot]!.onClick()),
  };
}

beforeEach(() => {
  subscribers.clear();
  saveConfig.mockImplementation(async (patch) => ({ ...makeConfig(), ...patch }));
  startBenchmark.mockResolvedValue({ status: "started" });
  cancelLocalLLMDownload.mockResolvedValue(undefined);
  deleteLocalModel.mockResolvedValue(undefined);
  getEnhancementModels.mockResolvedValue({
    provider: "local",
    bundled: [{ id: "stream-1-mini", label: "Stream-1 Mini", disk_mb: 462 }],
  });
  getLlamaStatus.mockResolvedValue({ state: "dormant" });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function toTryIt(h: ReturnType<typeof renderStep>) {
  h.click("primary");
  await waitFor(() => expect(startBenchmark).toHaveBeenCalled());
  emit({
    type: "benchmark.result", mode: "auto", verdict: "viable", model_downloaded: true,
    freshly_downloaded: true, measured_model_id: "stream-1-mini", measured_tok_per_s: 70,
  });
  await screen.findByRole("heading", { name: "Enhancement is on" });
  h.click("primary"); // Try it
  await screen.findByRole("heading", { name: "See what enhancement does" });
}

describe("StepEnhance", () => {
  it("shows the intro example carousel and skips without downloading", async () => {
    const h = renderStep();
    expect(screen.getByRole("heading", { name: "Polish your dictation" })).toBeInTheDocument();
    expect(screen.getByText("Optional")).toBeInTheDocument();
    const carousel = screen.getByRole("region", { name: "Enhancement examples" });
    expect(carousel).toHaveAttribute("aria-roledescription", "carousel");
    expect(carousel.querySelectorAll('[aria-roledescription="slide"]')).toHaveLength(3);
    expect(screen.getByRole("group", { name: `1 of 3: ${examples.intro[0].title}` })).toBeInTheDocument();
    expect(document.querySelector("del")).toHaveTextContent(examples.intro[0].dropped!);
    expect(
      await screen.findByText("One-time download, 462 MB. You can change this anytime in Enhancement."),
    ).toBeInTheDocument();
    expect(h.footer().ghost?.label).toBe("Skip & finish");
    expect(h.footer().primary.label).toBe("Turn on enhancement");

    h.click("ghost");
    expect(h.onFinish).toHaveBeenCalledOnce();
    expect(startBenchmark).not.toHaveBeenCalled();
    expect(saveConfig).not.toHaveBeenCalled();
    expect(h.onConfigChange).not.toHaveBeenCalled();
  });

  it("Skip & finish mid-run cancels the download and finishes", async () => {
    const h = renderStep();
    h.click("primary");
    await waitFor(() => expect(startBenchmark).toHaveBeenCalledOnce());
    expect(h.footer().primary).toMatchObject({ label: "Continue", disabled: true });
    h.click("ghost");
    expect(cancelLocalLLMDownload).toHaveBeenCalled();
    expect(h.onFinish).toHaveBeenCalledOnce();
  });

  it("a fast verdict enables local enhancement with Stream-1 Mini", async () => {
    const h = renderStep();
    h.click("primary");
    await waitFor(() => expect(startBenchmark).toHaveBeenCalled());
    emit({
      type: "benchmark.result", mode: "auto", verdict: "viable", model_downloaded: true,
      freshly_downloaded: true, measured_model_id: "stream-1-mini", measured_tok_per_s: 70,
    });
    await screen.findByRole("heading", { name: "Enhancement is on" });
    const expected = {
      enhancement_enabled: true,
      enhancement_provider: "local",
      enhancement_local_model: "stream-1-mini",
    };
    expect(saveConfig).toHaveBeenCalledWith(expect.objectContaining(expected));
    expect(h.onConfigChange).toHaveBeenCalledWith(expect.objectContaining(expected));
    expect(h.footer().primary.label).toBe("Try it");
  });

  it("an unviable verdict leaves enhancement off with Finish only", async () => {
    const h = renderStep();
    h.click("primary");
    await waitFor(() => expect(startBenchmark).toHaveBeenCalled());
    emit({
      type: "benchmark.result", mode: "auto", verdict: "unviable", model_downloaded: true,
      freshly_downloaded: false, measured_model_id: "stream-1-mini", measured_tok_per_s: 10,
      verdicts: { "stream-1-mini": "unviable" },
    });
    await screen.findByRole("heading", { name: "This computer is too slow for enhancement" });
    expect(saveConfig).toHaveBeenCalledWith({
      enhancement_local_gpu_verdicts: { "stream-1-mini": "unviable" },
    });
    for (const call of saveConfig.mock.calls) {
      expect(call[0]).not.toHaveProperty("enhancement_enabled");
      expect(call[0]).not.toHaveProperty("enhancement_provider");
    }
    expect(h.footer().ghost).toBeUndefined();
    expect(h.footer().primary.label).toBe("Finish");
  });

  it("marginal offers Use it anyway and Leave enhancement off", async () => {
    const h = renderStep();
    h.click("primary");
    await waitFor(() => expect(startBenchmark).toHaveBeenCalled());
    emit({
      type: "benchmark.result", mode: "auto", verdict: "marginal", model_downloaded: true,
      measured_model_id: "stream-1-mini", measured_tok_per_s: 40,
    });
    await screen.findByRole("heading", { name: "Stream-1 Mini runs slowly here" });
    expect(h.footer().secondary?.label).toBe("Use it anyway");
    expect(h.footer().primary.label).toBe("Leave enhancement off");
    h.click("primary");
    expect(
      await screen.findByText("Enhancement is off. You can turn it on anytime in Enhancement."),
    ).toBeInTheDocument();
    expect(h.footer().primary.label).toBe("Finish");
    expect(saveConfig).toHaveBeenCalledWith({
      enhancement_local_gpu_verdicts: { "stream-1-mini": "marginal" },
    });
  });

  it("try-it waits for the warm model, then labels an applied result", async () => {
    const h = renderStep();
    await toTryIt(h);
    expect(screen.getByText("Getting Stream-1 Mini ready…")).toBeInTheDocument();
    expect(screen.getByText(examples.tryIt[0].input)).toBeInTheDocument();
    expect(screen.queryByText(examples.tryIt[0].expected)).toBeNull();
    expect(screen.getByText("Say this")).toBeInTheDocument();
    // Before speaking the script stands alone, full width.
    expect(document.querySelector("[data-enhance-pair]")).not.toHaveAttribute("data-split");
    expect(document.querySelector('[data-slot="written"]')).toBeNull();
    expect(h.footer().primary).toMatchObject({ label: "Next example", disabled: true });

    emit({ type: "llama.status", state: "ready" });
    expect(await screen.findByText(/and read it aloud/)).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();

    emit({ type: "state.recording", recording: true });
    expect(screen.getByText("Listening…")).toBeInTheDocument();
    expect(document.querySelector("[data-enhance-pair]")).not.toHaveAttribute("data-split");
    emit({ type: "state.recording", recording: false });
    expect(screen.getByText("Enhancing…")).toBeInTheDocument();
    // Enhancement started: the surface splits, the shimmer fills the right column.
    expect(document.querySelector("[data-enhance-pair]")).toHaveAttribute("data-split", "true");
    expect(document.querySelector('[data-written-variant="pending"]')).toHaveAttribute("aria-busy", "true");

    emit({
      type: "transcription.result",
      text: "Let's meet tomorrow at 4:00.",
      raw_text: "Let's meet tomorrow at three, sorry, actually at four.",
      enhancement: "applied",
      duration_ms: 1000,
    });
    expect(await screen.findByText("Got it")).toBeInTheDocument();
    expect(screen.getByText("What you said")).toBeInTheDocument();
    expect(screen.getByText("After enhancement")).toBeInTheDocument();
    expect(screen.getByText("Let's meet tomorrow at 4:00.")).toBeInTheDocument();
    expect(document.querySelector('[data-written-variant="result"]')).not.toBeNull();
    expect(h.footer().primary).toMatchObject({ label: "Next example", disabled: false });

    h.click("primary");
    expect(await screen.findByText("Example 2 of 2")).toBeInTheDocument();
    expect(screen.getByText(examples.tryIt[1].input)).toBeInTheDocument();
    expect(h.footer().primary).toMatchObject({ label: "Finish", disabled: true });
  });

  it.each(["skipped", "failed", "off"] as const)(
    "labels a %s outcome as a plain transcript, even when texts differ",
    async (enhancement) => {
      getLlamaStatus.mockResolvedValue({ state: "ready" });
      const h = renderStep();
      await toTryIt(h);
      emit({
        type: "transcription.result",
        text: "Different text that is not enhanced.",
        raw_text: "Original raw text.",
        enhancement,
        duration_ms: 1000,
      });
      expect(
        await screen.findByText("Enhancement didn't run this time, so this is your plain transcript."),
      ).toBeInTheDocument();
      expect(document.querySelector('[data-written-variant="result"]')).toBeNull();
      expect(h.footer().primary.disabled).toBe(false);
      act(() => screen.getByRole("button", { name: "Try again" }).click());
      expect(screen.getByText(examples.tryIt[0].input)).toBeInTheDocument();
      expect(h.footer().primary.disabled).toBe(true);
    },
  );

  it("an applied result with unchanged text says nothing needed fixing", async () => {
    getLlamaStatus.mockResolvedValue({ state: "ready" });
    const h = renderStep();
    await toTryIt(h);
    emit({
      type: "transcription.result",
      text: "Already clean.",
      raw_text: "Already clean.",
      enhancement: "applied",
      duration_ms: 1000,
    });
    expect(await screen.findByText("Nothing needed fixing this time.")).toBeInTheDocument();
    expect(document.querySelector('[data-written-variant="result"]')).not.toBeNull();
  });

  it("shows Didn't catch that when a dictation ends without a result", async () => {
    getLlamaStatus.mockResolvedValue({ state: "ready" });
    const h = renderStep();
    await toTryIt(h);
    emit({ type: "state.recording", recording: true });
    emit({
      type: "dictation.state", version: 1, recording_id: 3, phase: "idle", origin: "push_to_talk",
      hud_enabled: true, preview_enabled: false, screen_reader_detected: false,
    });
    expect(await screen.findByText(/Didn't catch that/)).toBeInTheDocument();
    expect(h.footer().primary.disabled).toBe(true);
  });
});
