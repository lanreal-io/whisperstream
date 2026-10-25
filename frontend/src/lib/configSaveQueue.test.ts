// @vitest-environment node
import { expect, it, vi } from "vitest";
import { createConfigSaveQueue } from "./configSaveQueue";
import { consentConfigPatch } from "./enhancementConsent";
import type { AppConfig } from "./api/config";
import type { BYOKConsentContext } from "./api/consent";

it("finishes prior writes before saving the exact reviewed recipient", async () => {
  let finish!: (value: AppConfig) => void;
  const save = vi.fn().mockReturnValueOnce(new Promise<AppConfig>((resolve) => { finish = resolve; }))
    .mockResolvedValue({ enhancement_custom_base_url: "https://reviewed.example/v1" });
  const queue = createConfigSaveQueue(save);
  const earlier = queue.save({ enhancement_custom_base_url: "https://old.example/v1" });
  const context = { provider: "custom", endpoint: "https://reviewed.example/v1", model: "reviewed-model" } as BYOKConsentContext;
  const approvedPatch = { enhancement_custom_base_url: "https://draft.example/v1", enhancement_custom_model: "draft-model", ...consentConfigPatch(context) };
  const accepted = queue.save(approvedPatch);
  await Promise.resolve();
  expect(save).toHaveBeenCalledTimes(1);
  finish({} as AppConfig);
  await earlier;
  await accepted;
  expect(save.mock.calls[1][0]).toMatchObject({ enhancement_custom_base_url: context.endpoint, enhancement_custom_model: context.model, enhancement_enabled: true });
  await queue.idle();
});

it("continues immediate saves after a rejected write without replaying its draft", async () => {
  const save = vi.fn().mockRejectedValueOnce(new Error("secure_storage_failed")).mockResolvedValue({ language: "en" });
  const queue = createConfigSaveQueue(save);
  const failed = queue.save({ enhancement_custom_api_key: "synthetic-key" });
  const immediate = queue.save({ language: "en" });
  await expect(failed).rejects.toThrow("secure_storage_failed");
  await expect(immediate).resolves.toEqual({ language: "en" });
  expect(save.mock.calls[1][0]).toEqual({ language: "en" });
});
