// Single source of truth for the UI language picker (onboarding chip grid +
// language settings). The codes here MUST stay in sync with
// `SUPPORTED_LANGUAGES` in `src/whisperstream/config_manager.py` — that
// frozenset is the canonical 39-language UNION (Parakeet TDT v3's 25 + Qwen3-ASR's
// extra languages), and any drift will cause the backend to drop a user's
// selection as unsupported.
//
// Two local STT backends support overlapping language sets:
//   - Parakeet TDT v3 — 25 European languages (en is its own v2 model).
//   - Qwen3-ASR 0.6B — 30 languages (East/SE Asian + Hindi + Arabic + Europe).
// The union is 39 languages. Each language carries a support descriptor naming
// which backend(s) can transcribe it (see `LANGUAGE_SUPPORT` below).
//
// Language leads routing, but it is not the only input. In Automatic mode the
// selected language resolves Whisperstream's recommended exact artifact
// (en->Parakeet v2, other Parakeet languages->v3, Qwen-only languages->Qwen).
// In Manual mode one exact artifact stays pinned for as long as it supports the
// selected language, which is what makes an organic "keep current model" choice
// truthful. Language becomes a per-call decode hint ONLY for Manual Qwen with
// `qwen_force_language` on; every other combination auto-detects at inference
// time. The picker never exposes Parakeet v2 vs v3; it offers backend families.
//
// See `getParakeetModel` / `getLanguageFamily` for the Parakeet variant mapping
// (keep in sync with `model_id_for_parakeet` in
// `src/whisperstream/model_downloader.py`), and `getDefaultBackendForLanguage` /
// `getModelsForLanguage` for backend routing (keep in sync with
// `src/whisperstream/language_routing.py`).

// Type-only, so this stays a dependency-free module at runtime (its unit tests
// run in the node environment). The registry-id union was already documented as
// something this file MUST mirror; importing it makes the compiler enforce it
// instead of a comment.
import type { SttArtifactId } from "./api/models";

export type SttBackend = "parakeet" | "qwen" | "cohere";

// Automatic follows Whisperstream's recommendation for the selected language.
// Manual pins one exact artifact and keeps it while it supports that language.
// Mirrors `SelectionMode` in `src/whisperstream/language_routing.py`.
export type SttSelectionMode = "automatic" | "manual";

export type ActiveSttArtifact =
  | "parakeet-v2"
  | "parakeet-v3"
  | "qwen3-asr"
  | "cohere-transcribe";

/** One complete persisted STT routing state.
 *
 *  Mirrors `_STT_ROUTING_FIELDS` in `src/whisperstream/main.py`. The backend
 *  refuses to install a prepared recognizer whose target routing no longer
 *  matches the saved config, so every switch persists all five fields together
 *  rather than patching them one at a time.
 */
export interface SttRoutingPatch {
  language: string;
  stt_model_selection_mode: SttSelectionMode;
  stt_backend: SttBackend;
  parakeet_model: string;
  qwen_force_language: boolean;
}

export interface LanguageTransitionInput {
  currentLanguage: string | null | undefined;
  targetLanguage: string | null | undefined;
  selectionMode: string | null | undefined;
  activeBackend: string | null | undefined;
  parakeetModel: string | null | undefined;
  /** Exact recognizer the backend reports as loaded. When supplied, including
   *  explicit `null`, this is authoritative over the configured routing
   *  fields. `undefined` preserves the pure-config behavior used by callers
   *  that do not have live process status. */
  loadedArtifact?: ActiveSttArtifact | null;
}

export type LanguageTransition =
  // Same language: nothing to persist, nothing to activate.
  | { type: "no-op" }
  // The exact artifact already serving the user also serves the target
  // language, so only `language` changes and no activation is required.
  | {
      type: "language-only";
      artifact: ActiveSttArtifact;
      mode: SttSelectionMode;
    }
  // Automatic with nothing worth keeping: activate the recommended artifact.
  | { type: "automatic-switch"; artifact: ActiveSttArtifact }
  // Automatic where the current artifact also covers the target language. The
  // user chooses between the recommendation (stay Automatic) and keeping the
  // current model (enter Manual, pinned to this exact artifact).
  | {
      type: "keep-or-switch";
      currentArtifact: ActiveSttArtifact;
      recommendedArtifact: ActiveSttArtifact;
    }
  // Manual pinned to an artifact that cannot serve the target language.
  // Continuing leaves Manual, clears Qwen forcing, and follows Automatic.
  | {
      type: "manual-incompatible";
      currentArtifact: ActiveSttArtifact;
      recommendedArtifact: ActiveSttArtifact;
    };

export interface Language {
  code: string;
  label: string;
}

// The 6 curated chips shown on the collapsed grid (scope.md).
// Order matters: `en` is slot 0 and carries the "Default" badge.
export const CURATED_LANGUAGES: Language[] = [
  { code: "en", label: "English" },
  { code: "es", label: "Spanish" },
  { code: "fr", label: "French" },
  { code: "de", label: "German" },
  { code: "it", label: "Italian" },
  { code: "pt", label: "Portuguese" },
];

// The 33 other languages in the union, alphabetical by label. Includes every
// Parakeet TDT v3 language outside the curated 6 plus the Qwen-only additions
// (East/SE Asian, Hindi, Arabic, Persian, Macedonian).
export const EXTENDED_LANGUAGES: Language[] = [
  { code: "ar", label: "Arabic" },
  { code: "bg", label: "Bulgarian" },
  { code: "yue", label: "Cantonese" },
  { code: "zh", label: "Chinese" },
  { code: "hr", label: "Croatian" },
  { code: "cs", label: "Czech" },
  { code: "da", label: "Danish" },
  { code: "nl", label: "Dutch" },
  { code: "et", label: "Estonian" },
  { code: "fil", label: "Filipino" },
  { code: "fi", label: "Finnish" },
  { code: "el", label: "Greek" },
  { code: "hi", label: "Hindi" },
  { code: "hu", label: "Hungarian" },
  { code: "id", label: "Indonesian" },
  { code: "ja", label: "Japanese" },
  { code: "ko", label: "Korean" },
  { code: "lv", label: "Latvian" },
  { code: "lt", label: "Lithuanian" },
  { code: "mk", label: "Macedonian" },
  { code: "ms", label: "Malay" },
  { code: "mt", label: "Maltese" },
  { code: "fa", label: "Persian" },
  { code: "pl", label: "Polish" },
  { code: "ro", label: "Romanian" },
  { code: "ru", label: "Russian" },
  { code: "sk", label: "Slovak" },
  { code: "sl", label: "Slovenian" },
  { code: "sv", label: "Swedish" },
  { code: "th", label: "Thai" },
  { code: "tr", label: "Turkish" },
  { code: "uk", label: "Ukrainian" },
  { code: "vi", label: "Vietnamese" },
];

export const ALL_LANGUAGES: Language[] = [
  ...CURATED_LANGUAGES,
  ...EXTENDED_LANGUAGES,
];

// Per-language backend support descriptor (source of truth on the frontend).
// MUST mirror PARAKEET_LANGUAGES / QWEN_LANGUAGES / COHERE_LANGUAGES in
// `src/whisperstream/language_routing.py`. Derived from:
//   - Parakeet TDT v3: 25 European languages.
//   - Qwen3-ASR 0.6B: 30 languages (research.md confirmed set).
// Classification: 16 dual-support, 9 Parakeet-only, 14 Qwen-only.

// 25 Parakeet TDT v3 languages.
const PARAKEET_LANGUAGES: ReadonlySet<string> = new Set([
  "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de",
  "el", "hu", "it", "lv", "lt", "mt", "pl", "pt", "ro", "sk",
  "sl", "es", "sv", "ru", "uk",
]);

// 30 Qwen3-ASR 0.6B languages (research.md confirmed set).
const QWEN_LANGUAGES: ReadonlySet<string> = new Set([
  "zh", "en", "yue", "ar", "de", "fr", "es", "pt", "id", "it",
  "ko", "ru", "th", "vi", "ja", "tr", "hi", "ms", "nl", "sv",
  "da", "fi", "pl", "cs", "fil", "fa", "el", "hu", "mk", "ro",
]);

// 14 Cohere Transcribe languages. Cohere requires the selected language on
// each stream, so it is a Manual choice and never an Automatic recommendation.
const COHERE_LANGUAGES: ReadonlySet<string> = new Set([
  "ar", "de", "el", "en", "es", "fr", "it", "ja", "ko", "nl",
  "pl", "pt", "vi", "zh",
]);

const PARAKEET_V2_MODEL = "nemo-parakeet-tdt-0.6b-v2";
const PARAKEET_V3_MODEL = "nemo-parakeet-tdt-0.6b-v3";
const QWEN_ARTIFACT_ID = "qwen3-asr-0.6b-int8";
const COHERE_ARTIFACT_ID = "cohere-transcribe-03-2026-int8";

// Exact artifact -> download-registry model id. Mirrors `SttArtifactId` in
// `lib/api.ts` and `DOWNLOAD_REGISTRY` in `src/whisperstream/model_downloader.py`;
// registry ids are the only identifiers that cross the API boundary.
const ARTIFACT_REGISTRY_IDS: Record<ActiveSttArtifact, SttArtifactId> = {
  "parakeet-v2": PARAKEET_V2_MODEL,
  "parakeet-v3": PARAKEET_V3_MODEL,
  "qwen3-asr": QWEN_ARTIFACT_ID,
  "cohere-transcribe": COHERE_ARTIFACT_ID,
};

// Which backend(s) can transcribe the given language. Returns Parakeet first
// when both support it (Parakeet is the default for dual-support languages).
// Unknown/empty codes return an empty array.
export function getModelsForLanguage(
  code: string | null | undefined,
): SttBackend[] {
  if (!code) return [];
  const lower = code.toLowerCase();
  const models: SttBackend[] = [];
  if (PARAKEET_LANGUAGES.has(lower)) models.push("parakeet");
  if (QWEN_LANGUAGES.has(lower)) models.push("qwen");
  if (COHERE_LANGUAGES.has(lower)) models.push("cohere");
  return models;
}

// The default backend for a language: Parakeet when it supports the language,
// otherwise Qwen. (The model dropdown can later override a dual-support language
// to Qwen; there is no per-language memory — it resets to this default on a
// language change.) Defaults to Parakeet for unknown/empty codes.
export function getDefaultBackendForLanguage(
  code: string | null | undefined,
): SttBackend {
  if (!code) return "parakeet";
  const lower = code.toLowerCase();
  return QWEN_LANGUAGES.has(lower) && !PARAKEET_LANGUAGES.has(lower)
    ? "qwen"
    : "parakeet";
}

// Resolve configured backend state to the exact recognition artifact that is
// active. An unknown Parakeet model is stale state rather than an implicit v3.
export function resolveActiveSttArtifact(
  activeBackend: string | null | undefined,
  parakeetModel: string | null | undefined,
): ActiveSttArtifact | null {
  if (activeBackend === "qwen") return "qwen3-asr";
  if (activeBackend === "cohere") return "cohere-transcribe";
  if (activeBackend !== "parakeet") return null;
  if (parakeetModel === PARAKEET_V2_MODEL) return "parakeet-v2";
  if (parakeetModel === PARAKEET_V3_MODEL) return "parakeet-v3";
  return null;
}

export function getSttArtifactDisplayName(
  artifact: ActiveSttArtifact,
): "NVIDIA Parakeet" | "Qwen3 ASR" | "Cohere Transcribe" {
  if (artifact === "qwen3-asr") return "Qwen3 ASR";
  if (artifact === "cohere-transcribe") return "Cohere Transcribe";
  return "NVIDIA Parakeet";
}

export function isLanguageSupportedByArtifact(
  artifact: ActiveSttArtifact,
  language: string | null | undefined,
): boolean {
  if (!language) return false;
  const normalizedLanguage = language.toLowerCase();

  if (artifact === "parakeet-v2") return normalizedLanguage === "en";
  if (artifact === "parakeet-v3") {
    return PARAKEET_LANGUAGES.has(normalizedLanguage);
  }
  if (artifact === "qwen3-asr") {
    return QWEN_LANGUAGES.has(normalizedLanguage);
  }
  return COHERE_LANGUAGES.has(normalizedLanguage);
}

/** Registry model id for one exact artifact. */
export function sttArtifactRegistryId(artifact: ActiveSttArtifact): SttArtifactId {
  return ARTIFACT_REGISTRY_IDS[artifact];
}

/** Inverse of `sttArtifactRegistryId`. Unknown ids resolve to null so an
 *  unrecognized registry entry can never be treated as a known model. */
export function sttArtifactFromRegistryId(
  id: string | null | undefined,
): ActiveSttArtifact | null {
  if (!id) return null;
  const match = (
    Object.keys(ARTIFACT_REGISTRY_IDS) as ActiveSttArtifact[]
  ).find((artifact) => ARTIFACT_REGISTRY_IDS[artifact] === id);
  return match ?? null;
}

export function sttArtifactBackend(artifact: ActiveSttArtifact): SttBackend {
  if (artifact === "qwen3-asr") return "qwen";
  if (artifact === "cohere-transcribe") return "cohere";
  return "parakeet";
}

// Whisperstream's Automatic recommendation for a language. Keep in sync with
// `recommended_artifact` in `src/whisperstream/language_routing.py`: English
// takes the English-only Parakeet artifact, every other Parakeet language takes
// multilingual Parakeet, and Qwen-only languages take Qwen. Unknown codes keep
// the historical Parakeet-multilingual fallback.
export function recommendedSttArtifact(
  code: string | null | undefined,
): ActiveSttArtifact {
  const lower = code?.trim().toLowerCase() ?? "";
  if (PARAKEET_LANGUAGES.has(lower)) {
    return lower === "en" ? "parakeet-v2" : "parakeet-v3";
  }
  return QWEN_LANGUAGES.has(lower) ? "qwen3-asr" : "parakeet-v3";
}

/** The exact artifact an explicit picker choice resolves to.
 *
 *  The Manual picker groups both Parakeet artifacts under the single
 *  user-facing `NVIDIA Parakeet` choice, so a Parakeet pick has to be resolved
 *  the same way Automatic resolves it: English takes the English-only artifact
 *  and every other Parakeet language takes multilingual Parakeet. Only an
 *  organic keep-current action can deliberately hold multilingual Parakeet for
 *  English (scope.md), and it never comes through here.
 *
 *  This is deliberately NOT `recommendedSttArtifact`: that answers "what should
 *  Whisperstream choose for this language", which returns Qwen for a
 *  Qwen-only language. This answers "what does picking THIS backend mean",
 *  which is only ever asked while the backend serves the language.
 */
export function sttArtifactForBackendChoice(
  backend: SttBackend,
  language: string | null | undefined,
): ActiveSttArtifact {
  if (backend === "qwen") return "qwen3-asr";
  if (backend === "cohere") return "cohere-transcribe";
  return (language ?? "").trim().toLowerCase() === "en"
    ? "parakeet-v2"
    : "parakeet-v3";
}

/** Normalize a persisted selection mode. Anything that is not exactly
 *  `manual` is Automatic, matching the backend's fail-safe normalization. */
export function normalizeSelectionMode(
  mode: string | null | undefined,
): SttSelectionMode {
  return (mode ?? "").trim().toLowerCase() === "manual" ? "manual" : "automatic";
}

/** Build the complete routing state a selection resolves to.
 *
 *  `qwen_force_language` is only ever effective on Manual Qwen, so the
 *  invariant is enforced here instead of at each call site: leaving Manual or
 *  landing on Parakeet clears the hint rather than leaving it hidden and stale.
 */
export function sttRoutingPatch({
  artifact,
  language,
  mode,
  qwenForceLanguage,
  currentParakeetModel,
}: {
  artifact: ActiveSttArtifact;
  language: string;
  mode: SttSelectionMode;
  qwenForceLanguage: boolean;
  currentParakeetModel: string | null | undefined;
}): SttRoutingPatch {
  const backend = sttArtifactBackend(artifact);
  return {
    language: language.trim().toLowerCase(),
    stt_model_selection_mode: mode,
    stt_backend: backend,
    // While Qwen is selected the last known Parakeet variant is retained, so
    // switching back does not have to re-derive it (mirrors
    // `normalize_stt_selection`).
    parakeet_model:
      backend === "parakeet"
        ? sttArtifactRegistryId(artifact)
        : currentParakeetModel === PARAKEET_V2_MODEL
          ? PARAKEET_V2_MODEL
          : PARAKEET_V3_MODEL,
    qwen_force_language:
      mode === "manual" && backend === "qwen" && qwenForceLanguage,
  };
}

/** Resolve the mode and exact artifact an Advanced toggle change lands on.
 *
 *  Turning manual selection on pins the artifact already serving the user when
 *  it supports the selected language. Turning it off (or a pin that would be
 *  incompatible or stale) returns to Automatic and the recommended artifact.
 */
export function resolveSelectionModeChange({
  manual,
  language,
  activeBackend,
  parakeetModel,
  loadedArtifact,
}: {
  manual: boolean;
  language: string | null | undefined;
  activeBackend: string | null | undefined;
  parakeetModel: string | null | undefined;
  loadedArtifact?: ActiveSttArtifact | null;
}): { mode: SttSelectionMode; artifact: ActiveSttArtifact } {
  const current =
    loadedArtifact === undefined
      ? resolveActiveSttArtifact(activeBackend, parakeetModel)
      : loadedArtifact;
  if (manual && current !== null && isLanguageSupportedByArtifact(current, language)) {
    return { mode: "manual", artifact: current };
  }
  return { mode: "automatic", artifact: recommendedSttArtifact(language) };
}

// Classify a language change before App mutates config or enters the shared
// activation flow. The result is presentation-agnostic on purpose: it names the
// exact artifacts involved and leaves dialog choice, copy, and persistence to
// the caller. Stale or unrecognized artifact state deliberately falls back to
// Automatic routing rather than offering to keep a model we cannot identify.
export function classifyLanguageTransition({
  currentLanguage,
  targetLanguage,
  selectionMode,
  activeBackend,
  parakeetModel,
  loadedArtifact,
}: LanguageTransitionInput): LanguageTransition {
  const current = currentLanguage?.trim().toLowerCase() || null;
  const target = targetLanguage?.trim().toLowerCase() || null;

  if (current === target) return { type: "no-op" };

  const recommended = recommendedSttArtifact(target);
  const artifact =
    loadedArtifact === undefined
      ? resolveActiveSttArtifact(activeBackend, parakeetModel)
      : loadedArtifact;
  if (artifact === null || target === null) {
    return { type: "automatic-switch", artifact: recommended };
  }

  const mode = normalizeSelectionMode(selectionMode);
  const keepsWorking = isLanguageSupportedByArtifact(artifact, target);

  if (mode === "manual") {
    // Manual preserves the EXACT artifact, not merely the backend, so keeping
    // multilingual Parakeet for English stays truthful until Manual is off.
    return keepsWorking
      ? { type: "language-only", artifact, mode }
      : {
          type: "manual-incompatible",
          currentArtifact: artifact,
          recommendedArtifact: recommended,
        };
  }

  if (artifact === recommended) return { type: "language-only", artifact, mode };

  // Nothing to keep, or nothing the user ever chose to keep: a config with no
  // language selected has never expressed a preference, so Automatic follows
  // its recommendation quietly instead of asking about a model the user did
  // not pick.
  if (!keepsWorking || current === null) {
    return { type: "automatic-switch", artifact: recommended };
  }

  return {
    type: "keep-or-switch",
    currentArtifact: artifact,
    recommendedArtifact: recommended,
  };
}

/** What a language change resolves to once the persisted state is known.
 *
 *  `commit` saves routing and nothing else: the exact artifact already loaded
 *  serves the target language, so the recognizer in memory stays untouched.
 *  `activate` must run the backend prepare/construct/atomic-swap operation,
 *  because saving config on its own leaves the previous recognizer live.
 *  `ask` needs a user decision before anything is written.
 */
export type SttRoutingPlan =
  | { action: "none" }
  | { action: "commit"; artifact: ActiveSttArtifact; patch: SttRoutingPatch }
  | { action: "activate"; artifact: ActiveSttArtifact; patch: SttRoutingPatch }
  | {
      action: "ask";
      kind: "keep-or-switch" | "manual-incompatible";
      currentArtifact: ActiveSttArtifact;
      recommendedArtifact: ActiveSttArtifact;
    };

export interface SttRoutingPlanInput extends LanguageTransitionInput {
  qwenForceLanguage: boolean;
}

/** Resolve a language pick into the exact routing state to persist and whether
 *  the recognizer has to be replaced to honor it. */
export function planLanguageChange({
  currentLanguage,
  targetLanguage,
  selectionMode,
  activeBackend,
  parakeetModel,
  loadedArtifact,
  qwenForceLanguage,
}: SttRoutingPlanInput): SttRoutingPlan {
  const transition = classifyLanguageTransition({
    currentLanguage,
    targetLanguage,
    selectionMode,
    activeBackend,
    parakeetModel,
    loadedArtifact,
  });
  const language = targetLanguage ?? "";

  if (transition.type === "no-op") return { action: "none" };

  if (transition.type === "language-only") {
    return {
      action: "commit",
      artifact: transition.artifact,
      patch: sttRoutingPatch({
        artifact: transition.artifact,
        language,
        mode: transition.mode,
        qwenForceLanguage,
        currentParakeetModel: parakeetModel,
      }),
    };
  }

  if (transition.type === "automatic-switch") {
    return {
      action: "activate",
      artifact: transition.artifact,
      patch: sttRoutingPatch({
        artifact: transition.artifact,
        language,
        mode: "automatic",
        qwenForceLanguage,
        currentParakeetModel: parakeetModel,
      }),
    };
  }

  return {
    action: "ask",
    kind: transition.type,
    currentArtifact: transition.currentArtifact,
    recommendedArtifact: transition.recommendedArtifact,
  };
}

/** The dialog's keep-current outcome: change the language, enter Manual, and
 *  pin the exact artifact that is already loaded. No activation is needed
 *  precisely because the pinned artifact is the running one. */
export function planKeepCurrentModel({
  currentArtifact,
  targetLanguage,
  parakeetModel,
  qwenForceLanguage,
}: {
  currentArtifact: ActiveSttArtifact;
  targetLanguage: string;
  parakeetModel: string | null | undefined;
  qwenForceLanguage: boolean;
}): { action: "commit"; artifact: ActiveSttArtifact; patch: SttRoutingPatch } {
  return {
    action: "commit",
    artifact: currentArtifact,
    patch: sttRoutingPatch({
      artifact: currentArtifact,
      language: targetLanguage,
      mode: "manual",
      qwenForceLanguage,
      currentParakeetModel: parakeetModel,
    }),
  };
}

/** The dialog's recommended outcome, shared by both variants: Automatic plus
 *  the recommended artifact for the target language. For an incompatible
 *  Manual pin this is also the exit from Manual, which clears the Qwen hint. */
export function planSwitchToRecommended({
  recommendedArtifact,
  targetLanguage,
  parakeetModel,
}: {
  recommendedArtifact: ActiveSttArtifact;
  targetLanguage: string;
  parakeetModel: string | null | undefined;
}): { action: "activate"; artifact: ActiveSttArtifact; patch: SttRoutingPatch } {
  return {
    action: "activate",
    artifact: recommendedArtifact,
    patch: sttRoutingPatch({
      artifact: recommendedArtifact,
      language: targetLanguage,
      mode: "automatic",
      qwenForceLanguage: false,
      currentParakeetModel: parakeetModel,
    }),
  };
}

/** How a resolved routing target has to be honored against the RUNNING
 *  process, once authoritative backend state is known.
 *
 *  `commit` writes routing and nothing else, `activate` runs the backend
 *  prepare/construct/atomic-swap operation, and `download` needs the
 *  confirm-then-download dialog first. */
export type SttExecutionAction = "commit" | "activate" | "download";

/** Reconcile a routing plan with what the backend actually has and holds.
 *
 *  `stt_backend` / `parakeet_model` describe the CONFIGURED target, never the
 *  artifact the recognizer really loaded. They agree almost always, and
 *  disagree in exactly the cases that matter: mid-transition, after a failed
 *  activation whose rollback could not be written, and before the first
 *  recognizer is constructed. Shortening a switch to a routing-only commit off
 *  the configured fields therefore leaves the process running a model the UI
 *  says it replaced, which is the failure this exists to prevent.
 *
 *  `loaded_artifact_id` is the only authoritative answer to "what is running",
 *  so only it can license a commit. Unknown state (`downloaded: null`, the
 *  status probe failed) fails toward the confirm dialog rather than silently
 *  skipping a genuinely missing model; preparation is idempotent for files that
 *  are already there. */
export function resolveSttExecution({
  target,
  loadedArtifactId,
  downloaded,
}: {
  target: ActiveSttArtifact;
  loadedArtifactId: string | null | undefined;
  downloaded: boolean | null;
}): SttExecutionAction {
  if (downloaded !== true) return "download";
  return sttArtifactFromRegistryId(loadedArtifactId) === target
    ? "commit"
    : "activate";
}

export type ModelFamily = "english" | "multilingual";

// IMPORTANT: keep this in sync with `model_id_for_parakeet` in
// src/whisperstream/model_downloader.py.
// en -> v2 (English-only), everything else -> v3 (multilingual).
export function getLanguageFamily(code: string | null | undefined): ModelFamily {
  return code === "en" ? "english" : "multilingual";
}

export function getParakeetModel(code: string | null | undefined): string {
  return code === "en"
    ? PARAKEET_V2_MODEL
    : PARAKEET_V3_MODEL;
}

export function getFamilyFromModel(
  model: string | null | undefined,
): ModelFamily {
  return model === PARAKEET_V2_MODEL ? "english" : "multilingual";
}

/** Everyday choices prioritize Parakeet speed and Cohere quality. Advanced
 * retains all compatible backends; a legacy selected backend stays visible. */
export function getPickerModelsForLanguage(code: string | null | undefined, advanced: boolean, selected: SttBackend | null): SttBackend[] {
  const compatible = getModelsForLanguage(code);
  return compatible.filter(backend => advanced || backend !== "qwen" || !compatible.includes("parakeet") || backend === selected);
}
