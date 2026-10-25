// @vitest-environment node

import { describe, expect, it } from "vitest";
import {
  classifyLanguageTransition,
  getSttArtifactDisplayName,
  getDefaultBackendForLanguage,
  getModelsForLanguage,
  isLanguageSupportedByArtifact,
  normalizeSelectionMode,
  planKeepCurrentModel,
  planLanguageChange,
  planSwitchToRecommended,
  recommendedSttArtifact,
  resolveActiveSttArtifact,
  resolveSelectionModeChange,
  resolveSttExecution,
  sttArtifactBackend,
  sttArtifactForBackendChoice,
  sttArtifactFromRegistryId,
  sttArtifactRegistryId,
  sttRoutingPatch,
} from "./languages";

const V2 = "nemo-parakeet-tdt-0.6b-v2";
const V3 = "nemo-parakeet-tdt-0.6b-v3";
const QWEN = "qwen3-asr-0.6b-int8";
const COHERE = "cohere-transcribe-03-2026-int8";

describe("getModelsForLanguage", () => {
  it("returns all compatible models in stable picker order", () => {
    expect(getModelsForLanguage("de")).toEqual(["parakeet", "qwen", "cohere"]);
    expect(getModelsForLanguage("en")).toEqual(["parakeet", "qwen", "cohere"]);
  });
  it("returns only parakeet for Parakeet-only languages", () => {
    expect(getModelsForLanguage("bg")).toEqual(["parakeet"]);
    expect(getModelsForLanguage("uk")).toEqual(["parakeet"]);
  });
  it("adds Cohere only where its language set overlaps", () => {
    expect(getModelsForLanguage("ja")).toEqual(["qwen", "cohere"]);
    expect(getModelsForLanguage("hi")).toEqual(["qwen"]);
  });
  it("returns [] for unknown/empty", () => {
    expect(getModelsForLanguage("xx")).toEqual([]);
    expect(getModelsForLanguage("")).toEqual([]);
    expect(getModelsForLanguage(null)).toEqual([]);
  });
});

describe("getDefaultBackendForLanguage", () => {
  it("defaults dual-support and Parakeet-only languages to parakeet", () => {
    expect(getDefaultBackendForLanguage("de")).toBe("parakeet");
    expect(getDefaultBackendForLanguage("bg")).toBe("parakeet");
  });
  it("uses qwen for Qwen-only languages", () => {
    expect(getDefaultBackendForLanguage("ja")).toBe("qwen");
    expect(getDefaultBackendForLanguage("hi")).toBe("qwen");
  });
});

describe("STT artifact helpers", () => {
  it("resolves the exact active recognition artifact", () => {
    expect(resolveActiveSttArtifact("parakeet", V2)).toBe("parakeet-v2");
    expect(resolveActiveSttArtifact("parakeet", V3)).toBe("parakeet-v3");
    expect(resolveActiveSttArtifact("qwen", V2)).toBe("qwen3-asr");
    expect(resolveActiveSttArtifact("cohere", V2)).toBe("cohere-transcribe");
  });

  it("rejects stale or unknown artifact state", () => {
    expect(resolveActiveSttArtifact("parakeet", "stale-model-id")).toBeNull();
    expect(resolveActiveSttArtifact("unknown", V3)).toBeNull();
    expect(resolveActiveSttArtifact(null, V3)).toBeNull();
  });

  it("maps artifacts to the user-visible model family name", () => {
    expect(getSttArtifactDisplayName("parakeet-v2")).toBe("NVIDIA Parakeet");
    expect(getSttArtifactDisplayName("parakeet-v3")).toBe("NVIDIA Parakeet");
    expect(getSttArtifactDisplayName("qwen3-asr")).toBe("Qwen3 ASR");
    expect(getSttArtifactDisplayName("cohere-transcribe")).toBe("Cohere Transcribe");
  });

  it("checks language coverage for each exact artifact", () => {
    expect(isLanguageSupportedByArtifact("parakeet-v2", "en")).toBe(true);
    expect(isLanguageSupportedByArtifact("parakeet-v2", "de")).toBe(false);

    expect(isLanguageSupportedByArtifact("parakeet-v3", "en")).toBe(true);
    expect(isLanguageSupportedByArtifact("parakeet-v3", "uk")).toBe(true);
    expect(isLanguageSupportedByArtifact("parakeet-v3", "ja")).toBe(false);

    expect(isLanguageSupportedByArtifact("qwen3-asr", "en")).toBe(true);
    expect(isLanguageSupportedByArtifact("qwen3-asr", "ja")).toBe(true);
    expect(isLanguageSupportedByArtifact("qwen3-asr", "uk")).toBe(false);
    expect(isLanguageSupportedByArtifact("qwen3-asr", null)).toBe(false);
    expect(isLanguageSupportedByArtifact("cohere-transcribe", "en")).toBe(true);
    expect(isLanguageSupportedByArtifact("cohere-transcribe", "vi")).toBe(true);
    expect(isLanguageSupportedByArtifact("cohere-transcribe", "hi")).toBe(false);
  });
});

describe("recommendedSttArtifact", () => {
  it("routes English to the English-only Parakeet artifact", () => {
    expect(recommendedSttArtifact("en")).toBe("parakeet-v2");
    expect(recommendedSttArtifact("EN")).toBe("parakeet-v2");
  });

  it("routes other Parakeet languages to multilingual Parakeet", () => {
    expect(recommendedSttArtifact("de")).toBe("parakeet-v3");
    expect(recommendedSttArtifact("uk")).toBe("parakeet-v3");
  });

  it("routes Qwen-only languages to Qwen", () => {
    expect(recommendedSttArtifact("ja")).toBe("qwen3-asr");
    expect(recommendedSttArtifact("hi")).toBe("qwen3-asr");
  });

  it("keeps the multilingual Parakeet fallback for unknown codes", () => {
    expect(recommendedSttArtifact("xx")).toBe("parakeet-v3");
    expect(recommendedSttArtifact(null)).toBe("parakeet-v3");
  });
});

describe("sttArtifactForBackendChoice", () => {
  it("resolves a Parakeet pick the way Automatic resolves the language", () => {
    expect(sttArtifactForBackendChoice("parakeet", "en")).toBe("parakeet-v2");
    expect(sttArtifactForBackendChoice("parakeet", "EN")).toBe("parakeet-v2");
    expect(sttArtifactForBackendChoice("parakeet", "de")).toBe("parakeet-v3");
  });

  it("resolves a Qwen pick to Qwen regardless of language", () => {
    expect(sttArtifactForBackendChoice("qwen", "en")).toBe("qwen3-asr");
    expect(sttArtifactForBackendChoice("qwen", "ja")).toBe("qwen3-asr");
  });

  it("resolves a Cohere pick to the Cohere artifact", () => {
    expect(sttArtifactForBackendChoice("cohere", "en")).toBe("cohere-transcribe");
    expect(sttArtifactForBackendChoice("cohere", "ja")).toBe("cohere-transcribe");
  });

  it("does not follow the recommendation away from the picked backend", () => {
    // `ja` is Qwen-only, so `recommendedSttArtifact` answers Qwen. Picking
    // Parakeet still means Parakeet; the picker only offers a backend that
    // serves the language, so the two questions must stay separate.
    expect(recommendedSttArtifact("ja")).toBe("qwen3-asr");
    expect(sttArtifactForBackendChoice("parakeet", "ja")).toBe("parakeet-v3");
  });

  it("treats a missing language as multilingual Parakeet", () => {
    expect(sttArtifactForBackendChoice("parakeet", null)).toBe("parakeet-v3");
    expect(sttArtifactForBackendChoice("parakeet", "")).toBe("parakeet-v3");
  });
});

describe("artifact identity helpers", () => {
  it("maps each artifact to its registry id and back", () => {
    expect(sttArtifactRegistryId("parakeet-v2")).toBe(V2);
    expect(sttArtifactRegistryId("parakeet-v3")).toBe(V3);
    expect(sttArtifactRegistryId("qwen3-asr")).toBe(QWEN);
    expect(sttArtifactRegistryId("cohere-transcribe")).toBe(COHERE);

    expect(sttArtifactFromRegistryId(V2)).toBe("parakeet-v2");
    expect(sttArtifactFromRegistryId(V3)).toBe("parakeet-v3");
    expect(sttArtifactFromRegistryId(QWEN)).toBe("qwen3-asr");
    expect(sttArtifactFromRegistryId(COHERE)).toBe("cohere-transcribe");
  });

  it("rejects unknown registry ids", () => {
    expect(sttArtifactFromRegistryId("gemma4-e2b-instruct")).toBeNull();
    expect(sttArtifactFromRegistryId(null)).toBeNull();
    expect(sttArtifactFromRegistryId("")).toBeNull();
  });

  it("resolves the backend that owns each artifact", () => {
    expect(sttArtifactBackend("parakeet-v2")).toBe("parakeet");
    expect(sttArtifactBackend("parakeet-v3")).toBe("parakeet");
    expect(sttArtifactBackend("qwen3-asr")).toBe("qwen");
    expect(sttArtifactBackend("cohere-transcribe")).toBe("cohere");
  });
});

describe("normalizeSelectionMode", () => {
  it("keeps an explicit manual selection", () => {
    expect(normalizeSelectionMode("manual")).toBe("manual");
    expect(normalizeSelectionMode(" Manual ")).toBe("manual");
  });

  it("falls back to automatic for anything else", () => {
    expect(normalizeSelectionMode("automatic")).toBe("automatic");
    expect(normalizeSelectionMode("bogus")).toBe("automatic");
    expect(normalizeSelectionMode(null)).toBe("automatic");
    expect(normalizeSelectionMode(undefined)).toBe("automatic");
  });
});

describe("sttRoutingPatch", () => {
  it("persists the full exact routing state for a Parakeet choice", () => {
    expect(
      sttRoutingPatch({
        artifact: "parakeet-v3",
        language: "FR",
        mode: "manual",
        qwenForceLanguage: true,
        currentParakeetModel: V2,
      }),
    ).toEqual({
      language: "fr",
      stt_model_selection_mode: "manual",
      stt_backend: "parakeet",
      parakeet_model: V3,
      // Qwen forcing can never survive on Parakeet, manual or not.
      qwen_force_language: false,
    });
  });

  it("retains the last known Parakeet variant while Qwen is selected", () => {
    expect(
      sttRoutingPatch({
        artifact: "qwen3-asr",
        language: "ja",
        mode: "manual",
        qwenForceLanguage: true,
        currentParakeetModel: V2,
      }),
    ).toEqual({
      language: "ja",
      stt_model_selection_mode: "manual",
      stt_backend: "qwen",
      parakeet_model: V2,
      qwen_force_language: true,
    });

    expect(
      sttRoutingPatch({
        artifact: "qwen3-asr",
        language: "ja",
        mode: "manual",
        qwenForceLanguage: false,
        currentParakeetModel: null,
      }).parakeet_model,
    ).toBe(V3);
  });

  it("pins Cohere without carrying Qwen's language hint", () => {
    expect(
      sttRoutingPatch({
        artifact: "cohere-transcribe",
        language: "DE",
        mode: "manual",
        qwenForceLanguage: true,
        currentParakeetModel: V2,
      }),
    ).toEqual({
      language: "de",
      stt_model_selection_mode: "manual",
      stt_backend: "cohere",
      parakeet_model: V2,
      qwen_force_language: false,
    });
  });

  it("clears Qwen forcing whenever the mode is not manual", () => {
    expect(
      sttRoutingPatch({
        artifact: "qwen3-asr",
        language: "ja",
        mode: "automatic",
        qwenForceLanguage: true,
        currentParakeetModel: V3,
      }).qwen_force_language,
    ).toBe(false);
  });
});

describe("resolveSelectionModeChange", () => {
  it("pins the exact compatible artifact when manual is enabled", () => {
    expect(
      resolveSelectionModeChange({
        manual: true,
        language: "en",
        activeBackend: "parakeet",
        parakeetModel: V3,
      }),
    ).toEqual({ mode: "manual", artifact: "parakeet-v3" });
  });

  it("pins the authoritative loaded artifact when config disagrees", () => {
    expect(
      resolveSelectionModeChange({
        manual: true,
        language: "en",
        activeBackend: "qwen",
        parakeetModel: V2,
        loadedArtifact: "parakeet-v2",
      }),
    ).toEqual({ mode: "manual", artifact: "parakeet-v2" });
  });

  it("does not invent a manual pin when no artifact is loaded", () => {
    expect(
      resolveSelectionModeChange({
        manual: true,
        language: "ja",
        activeBackend: "qwen",
        parakeetModel: V3,
        loadedArtifact: null,
      }),
    ).toEqual({ mode: "automatic", artifact: "qwen3-asr" });
  });

  it("returns to the recommended artifact when manual is disabled", () => {
    expect(
      resolveSelectionModeChange({
        manual: false,
        language: "en",
        activeBackend: "parakeet",
        parakeetModel: V3,
      }),
    ).toEqual({ mode: "automatic", artifact: "parakeet-v2" });
  });

  it("refuses to pin an incompatible or stale artifact", () => {
    expect(
      resolveSelectionModeChange({
        manual: true,
        language: "ja",
        activeBackend: "parakeet",
        parakeetModel: V3,
      }),
    ).toEqual({ mode: "automatic", artifact: "qwen3-asr" });

    expect(
      resolveSelectionModeChange({
        manual: true,
        language: "de",
        activeBackend: "parakeet",
        parakeetModel: "stale-model-id",
      }),
    ).toEqual({ mode: "automatic", artifact: "parakeet-v3" });
  });
});

describe("classifyLanguageTransition", () => {
  const classify = (
    currentLanguage: string | null,
    targetLanguage: string | null,
    {
      selectionMode = "automatic",
      activeBackend = "parakeet",
      parakeetModel = V3,
    }: {
      selectionMode?: string | null;
      activeBackend?: string | null;
      parakeetModel?: string | null;
    } = {},
  ) =>
    classifyLanguageTransition({
      currentLanguage,
      targetLanguage,
      selectionMode,
      activeBackend,
      parakeetModel,
    });

  it("returns no-op when the selected language is unchanged", () => {
    expect(classify("es", "es")).toEqual({ type: "no-op" });
    expect(
      classify("ja", "ja", { selectionMode: "manual", activeBackend: "qwen" }),
    ).toEqual({ type: "no-op" });
  });

  describe("automatic", () => {
    it("changes only the language when the recommendation is already active", () => {
      expect(classify("es", "fr")).toEqual({
        type: "language-only",
        artifact: "parakeet-v3",
        mode: "automatic",
      });
      expect(classify("ja", "ko", { activeBackend: "qwen" })).toEqual({
        type: "language-only",
        artifact: "qwen3-asr",
        mode: "automatic",
      });
    });

    it("switches straight to the recommendation when nothing can be kept", () => {
      expect(classify("es", "ja")).toEqual({
        type: "automatic-switch",
        artifact: "qwen3-asr",
      });
      expect(classify("ja", "uk", { activeBackend: "qwen" })).toEqual({
        type: "automatic-switch",
        artifact: "parakeet-v3",
      });
      expect(classify("en", "de", { parakeetModel: V2 })).toEqual({
        type: "automatic-switch",
        artifact: "parakeet-v3",
      });
    });

    it("offers keep-or-switch when the current artifact also covers the target", () => {
      // Multilingual Parakeet supports English, but Automatic recommends the
      // English-only artifact.
      expect(classify("es", "en")).toEqual({
        type: "keep-or-switch",
        currentArtifact: "parakeet-v3",
        recommendedArtifact: "parakeet-v2",
      });
      // Qwen supports German, but Parakeet is the recommendation for it.
      expect(classify("ja", "de", { activeBackend: "qwen" })).toEqual({
        type: "keep-or-switch",
        currentArtifact: "qwen3-asr",
        recommendedArtifact: "parakeet-v3",
      });
    });

    it("follows automatic routing for stale artifact state", () => {
      expect(classify("es", "en", { parakeetModel: "stale-model-id" })).toEqual({
        type: "automatic-switch",
        artifact: "parakeet-v2",
      });
      expect(classify("es", "en", { activeBackend: "unknown" })).toEqual({
        type: "automatic-switch",
        artifact: "parakeet-v2",
      });
    });
  });

  describe("manual", () => {
    const manual = { selectionMode: "manual" };

    it("preserves the exact pinned artifact across a compatible language", () => {
      // The organic keep-current case: multilingual Parakeet stays pinned for
      // English instead of resolving to the English-only artifact.
      expect(classify("es", "en", manual)).toEqual({
        type: "language-only",
        artifact: "parakeet-v3",
        mode: "manual",
      });
      expect(classify("ja", "de", { ...manual, activeBackend: "qwen" })).toEqual({
        type: "language-only",
        artifact: "qwen3-asr",
        mode: "manual",
      });
    });

    it("reports incompatibility instead of silently re-routing", () => {
      expect(classify("es", "ja", manual)).toEqual({
        type: "manual-incompatible",
        currentArtifact: "parakeet-v3",
        recommendedArtifact: "qwen3-asr",
      });
      expect(classify("ja", "uk", { ...manual, activeBackend: "qwen" })).toEqual({
        type: "manual-incompatible",
        currentArtifact: "qwen3-asr",
        recommendedArtifact: "parakeet-v3",
      });
      // A pinned English-only Parakeet cannot serve any other language.
      expect(classify("en", "fr", { ...manual, parakeetModel: V2 })).toEqual({
        type: "manual-incompatible",
        currentArtifact: "parakeet-v2",
        recommendedArtifact: "parakeet-v3",
      });
    });

    it("treats an unrecognized mode value as automatic", () => {
      expect(classify("es", "en", { selectionMode: "bogus" })).toEqual({
        type: "keep-or-switch",
        currentArtifact: "parakeet-v3",
        recommendedArtifact: "parakeet-v2",
      });
    });
  });

  it("handles an unset current language and an unknown target", () => {
    expect(classify(null, "de")).toEqual({
      type: "language-only",
      artifact: "parakeet-v3",
      mode: "automatic",
    });
    // No language has ever been selected, so there is no deliberate model
    // choice to offer keeping: Automatic just follows its recommendation.
    expect(classify(null, "en")).toEqual({
      type: "automatic-switch",
      artifact: "parakeet-v2",
    });
    expect(classify(null, "de", { activeBackend: "qwen" })).toEqual({
      type: "automatic-switch",
      artifact: "parakeet-v3",
    });
    // An unknown code keeps the multilingual Parakeet fallback, so nothing is
    // activated; the backend drops the unsupported language on save.
    expect(classify("es", "xx")).toEqual({
      type: "language-only",
      artifact: "parakeet-v3",
      mode: "automatic",
    });
    expect(classify("ja", "xx", { activeBackend: "qwen" })).toEqual({
      type: "automatic-switch",
      artifact: "parakeet-v3",
    });
  });
});

describe("planLanguageChange", () => {
  const plan = (
    currentLanguage: string | null,
    targetLanguage: string,
    overrides: {
      selectionMode?: string;
      activeBackend?: string;
      parakeetModel?: string;
      loadedArtifact?: "parakeet-v2" | "parakeet-v3" | "qwen3-asr" | null;
      qwenForceLanguage?: boolean;
    } = {},
  ) =>
    planLanguageChange({
      currentLanguage,
      targetLanguage,
      selectionMode: overrides.selectionMode ?? "automatic",
      activeBackend: overrides.activeBackend ?? "parakeet",
      parakeetModel: overrides.parakeetModel ?? V3,
      loadedArtifact: overrides.loadedArtifact,
      qwenForceLanguage: overrides.qwenForceLanguage ?? false,
    });

  it("does nothing when the language is unchanged", () => {
    expect(plan("de", "de")).toEqual({ action: "none" });
  });

  it("commits routing without activation when the loaded artifact still fits", () => {
    expect(plan("es", "fr")).toEqual({
      action: "commit",
      artifact: "parakeet-v3",
      patch: {
        language: "fr",
        stt_model_selection_mode: "automatic",
        stt_backend: "parakeet",
        parakeet_model: V3,
        qwen_force_language: false,
      },
    });
  });

  it("activates the recommendation when nothing can be kept", () => {
    expect(plan("es", "ja")).toEqual({
      action: "activate",
      artifact: "qwen3-asr",
      patch: {
        language: "ja",
        stt_model_selection_mode: "automatic",
        stt_backend: "qwen",
        parakeet_model: V3,
        qwen_force_language: false,
      },
    });
  });

  it("asks before persisting anything when both models are viable", () => {
    expect(plan("ja", "de", { activeBackend: "qwen" })).toEqual({
      action: "ask",
      kind: "keep-or-switch",
      currentArtifact: "qwen3-asr",
      recommendedArtifact: "parakeet-v3",
    });
  });

  it("uses the loaded artifact for dialog planning when config disagrees", () => {
    expect(
      plan("ja", "de", {
        // Config still names Parakeet, but the process is actually running
        // Qwen. The dialog must offer to keep Qwen, never the configured target.
        activeBackend: "parakeet",
        parakeetModel: V3,
        loadedArtifact: "qwen3-asr",
      }),
    ).toEqual({
      action: "ask",
      kind: "keep-or-switch",
      currentArtifact: "qwen3-asr",
      recommendedArtifact: "parakeet-v3",
    });
  });

  it("does not invent a keep-current choice when nothing is loaded", () => {
    expect(
      plan("ja", "de", {
        activeBackend: "qwen",
        loadedArtifact: null,
      }),
    ).toEqual({
      action: "activate",
      artifact: "parakeet-v3",
      patch: {
        language: "de",
        stt_model_selection_mode: "automatic",
        stt_backend: "parakeet",
        parakeet_model: V3,
        qwen_force_language: false,
      },
    });
  });

  it("asks before dropping an incompatible manual pin", () => {
    expect(
      plan("de", "ja", { selectionMode: "manual", qwenForceLanguage: false }),
    ).toEqual({
      action: "ask",
      kind: "manual-incompatible",
      currentArtifact: "parakeet-v3",
      recommendedArtifact: "qwen3-asr",
    });
  });

  it("keeps the manual pin and the Qwen hint across a compatible language", () => {
    expect(
      plan("ja", "de", {
        selectionMode: "manual",
        activeBackend: "qwen",
        qwenForceLanguage: true,
      }),
    ).toEqual({
      action: "commit",
      artifact: "qwen3-asr",
      patch: {
        language: "de",
        stt_model_selection_mode: "manual",
        stt_backend: "qwen",
        parakeet_model: V3,
        // The hint follows the newly selected language; it stays effective
        // because this is still Manual Qwen.
        qwen_force_language: true,
      },
    });
  });

  it("drops a stale Qwen hint when automatic routing lands on Parakeet", () => {
    expect(
      plan("ja", "uk", { activeBackend: "qwen", qwenForceLanguage: true }),
    ).toEqual({
      action: "activate",
      artifact: "parakeet-v3",
      patch: {
        language: "uk",
        stt_model_selection_mode: "automatic",
        stt_backend: "parakeet",
        parakeet_model: V3,
        qwen_force_language: false,
      },
    });
  });
});

describe("dialog outcomes", () => {
  it("keep-current enters manual pinned to the EXACT loaded artifact", () => {
    // Multilingual Parakeet stays pinned for English rather than resolving to
    // the English-only artifact, which is what makes the choice truthful.
    expect(
      planKeepCurrentModel({
        currentArtifact: "parakeet-v3",
        targetLanguage: "en",
        parakeetModel: V3,
        qwenForceLanguage: false,
      }),
    ).toEqual({
      action: "commit",
      artifact: "parakeet-v3",
      patch: {
        language: "en",
        stt_model_selection_mode: "manual",
        stt_backend: "parakeet",
        parakeet_model: V3,
        qwen_force_language: false,
      },
    });
  });

  it("keep-current on Qwen retains the hint and the last Parakeet variant", () => {
    expect(
      planKeepCurrentModel({
        currentArtifact: "qwen3-asr",
        targetLanguage: "de",
        parakeetModel: V2,
        qwenForceLanguage: true,
      }).patch,
    ).toEqual({
      language: "de",
      stt_model_selection_mode: "manual",
      stt_backend: "qwen",
      parakeet_model: V2,
      qwen_force_language: true,
    });
  });

  it("switch-to-recommended returns to automatic and clears the Qwen hint", () => {
    expect(
      planSwitchToRecommended({
        recommendedArtifact: "parakeet-v2",
        targetLanguage: "en",
        parakeetModel: V3,
      }),
    ).toEqual({
      action: "activate",
      artifact: "parakeet-v2",
      patch: {
        language: "en",
        stt_model_selection_mode: "automatic",
        stt_backend: "parakeet",
        parakeet_model: V2,
        qwen_force_language: false,
      },
    });
  });

  it("the incompatible-manual outcome exits manual onto the recommendation", () => {
    expect(
      planSwitchToRecommended({
        recommendedArtifact: "qwen3-asr",
        targetLanguage: "ja",
        parakeetModel: V3,
      }),
    ).toEqual({
      action: "activate",
      artifact: "qwen3-asr",
      patch: {
        language: "ja",
        stt_model_selection_mode: "automatic",
        stt_backend: "qwen",
        parakeet_model: V3,
        qwen_force_language: false,
      },
    });
  });
});

describe("resolveSttExecution", () => {
  // The authoritative loaded artifact is the ONLY thing that can shorten a
  // switch to a routing-only commit. `stt_backend`/`parakeet_model` name the
  // configured target, which disagrees with the running recognizer exactly
  // when it matters.
  it("commits when the target is already the loaded artifact", () => {
    expect(
      resolveSttExecution({
        target: "parakeet-v3",
        loadedArtifactId: V3,
        downloaded: true,
      }),
    ).toBe("commit");
  });

  it("activates when a downloaded target is not the loaded artifact", () => {
    expect(
      resolveSttExecution({
        target: "qwen3-asr",
        loadedArtifactId: V3,
        downloaded: true,
      }),
    ).toBe("activate");
  });

  it("activates a configured/loaded mismatch even when config already names the target", () => {
    // Config says Qwen (a previous switch persisted it) but the process is
    // still holding Parakeet because activation failed. Committing here would
    // leave the recognizer disagreeing with the UI forever.
    expect(
      resolveSttExecution({
        target: "qwen3-asr",
        loadedArtifactId: V2,
        downloaded: true,
      }),
    ).toBe("activate");
  });

  it("activates when nothing is loaded yet", () => {
    expect(
      resolveSttExecution({
        target: "parakeet-v2",
        loadedArtifactId: null,
        downloaded: true,
      }),
    ).toBe("activate");
  });

  it("never mistakes one Parakeet artifact for the other", () => {
    expect(
      resolveSttExecution({
        target: "parakeet-v2",
        loadedArtifactId: V3,
        downloaded: true,
      }),
    ).toBe("activate");
  });

  it("asks for a download when the target is absent", () => {
    expect(
      resolveSttExecution({
        target: "qwen3-asr",
        loadedArtifactId: QWEN,
        downloaded: false,
      }),
    ).toBe("download");
  });

  it("fails toward the confirm dialog when disk state is unknown", () => {
    expect(
      resolveSttExecution({
        target: "qwen3-asr",
        loadedArtifactId: undefined,
        downloaded: null,
      }),
    ).toBe("download");
  });

  it("ignores an unrecognized loaded id rather than trusting it", () => {
    expect(
      resolveSttExecution({
        target: "qwen3-asr",
        loadedArtifactId: "some-future-artifact",
        downloaded: true,
      }),
    ).toBe("activate");
  });
});
