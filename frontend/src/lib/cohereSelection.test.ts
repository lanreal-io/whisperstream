import { describe, expect, it } from "vitest";
import { ALL_LANGUAGES, isLanguageSupportedByArtifact, getPickerModelsForLanguage, sttArtifactForBackendChoice } from "./languages";

describe("Everyday speech model choices", () => {
  it.each(ALL_LANGUAGES)("curates $label without losing supported Cohere", ({ code }) => {
    const options = getPickerModelsForLanguage(code, false, null);
    expect(options.includes("cohere")).toBe(isLanguageSupportedByArtifact("cohere-transcribe", code));
    expect(options.includes("qwen")).toBe(!isLanguageSupportedByArtifact("parakeet-v3", code));
  });
  it("offers Parakeet English and Cohere for English", () => {
    expect(getPickerModelsForLanguage("en", false, "parakeet")).toEqual(["parakeet", "cohere"]);
    expect(sttArtifactForBackendChoice("parakeet", "en")).toBe("parakeet-v2");
  });
  it("retains a selected legacy model and exposes all choices in Advanced", () => {
    expect(getPickerModelsForLanguage("en", false, "qwen")).toEqual(["parakeet", "qwen", "cohere"]);
    expect(getPickerModelsForLanguage("en", true, "parakeet")).toEqual(["parakeet", "qwen", "cohere"]);
  });
});
