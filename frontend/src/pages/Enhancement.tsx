import { ProfilesSection } from "./enhancement/ProfilesSection";
import { useBenchmarkFlow } from "./enhancement/useBenchmarkFlow";
import { buildBenchmarkVerdictPatch } from "./enhancement/benchmarkVerdictPatch";
import { useRecipientPermission } from "./enhancement/useRecipientPermission";
import { useProviderCredential } from "./enhancement/useProviderCredential";
import { useSavedEnhancementConnection } from "./enhancement/useSavedEnhancementConnection";
import { isWorkspaceError, useAnthropicWorkspace, WORKSPACE_ERRORS } from "./enhancement/useAnthropicWorkspace";
import { type AppConfig, saveConfig } from "../lib/api/config";
import { type AppEvent } from "../lib/socket/events";
import { type LockStatus } from "../lib/useLockdown";
import { type DailyUseCapabilityReport, capabilityDecision, capabilityExplanation } from "../lib/dailyUseCapabilities";
import { type AdvancedCapabilityReport } from "../lib/advancedCapabilities";
import { useState, useRef, useEffect, useMemo } from "react";
import { type StatusState, mapLlamaStateToStatus, tooltipFor, ariaLabelFor, dotClassFor, labelClassFor, labelFor } from "./enhancement/connectionStatus";
import { useReducedMotion, motion } from "framer-motion";
import { PROVIDER_HELP, PROVIDERS, PROVIDER_DISPLAY, isLocalOllamaEndpoint } from "./enhancement/providerMetadata";
import { type EnhancementModel, getEnhancementModels, checkEnhancement, getLlamaStatus, getEnhancementDiskFree, deleteLocalModel, activateLocalCandidate, startLocalLLMDownload, cancelLocalLLMDownload } from "../lib/api/enhancement";
import { consentRequest, enhancementErrorMessage } from "../lib/enhancementConsent";
import { type LocalModel, formatModelSizeShort, localModelDownloadLabel, localModelCapabilities } from "./enhancement/localModels";
import { AUTO_BENCHMARK_MODEL_ID, getLocalGpuVerdict, LOCAL_MODEL_FIRST_PARTY, LOCAL_MODEL_DISPLAY_LABEL, LOCAL_MODEL_DESCRIPTION, LOCAL_MODEL_DESCRIPTION_HOVER } from "../lib/localModels";
import { errorType } from "../lib/logCapture";
import { getSystemInfo } from "../lib/api/system";
import { subscribeToDownloadEvents } from "../lib/socket/downloads";
import { type BenchmarkVerdict, BenchmarkFlow } from "../components/BenchmarkFlow";
import { type DropdownOption, Dropdown } from "../components/Dropdown";
import { HoverTooltip } from "../components/HoverTooltip";
import { ModelIdentityChip, MarginalDot, DownloadMiniIcon } from "./enhancement/Glyphs";
import { type PromptTesterGate } from "../components/PromptTester";
import { CloudModelPicker } from "../components/CloudModelPicker";
import { ApiKeyField } from "./enhancement/ApiKeyField";
import { ShieldSealGlyph } from "../components/SealPanel";
import { Toggle } from "../components/Toggle";
import { ConnectionSection } from "./enhancement/ConnectionSection";
import { ProviderDropdown } from "./enhancement/ProviderDropdown";
import { ModelRowActions } from "./enhancement/ModelRowActions";
import { SecondaryButton } from "../components/SecondaryButton";
import { AdvancedDisclosure } from "../components/AdvancedDisclosure";
import { SettingsGroup } from "../components/SettingsGroup";
import { DownloadDialog } from "../components/DownloadDialog";
import { LowDiskSpaceDialog } from "../components/LowDiskSpaceDialog";
import { LowRamDialog } from "../components/LowRamDialog";
import { DataLeavesDialog } from "../components/DataLeavesDialog";
import { SlowModelConsentDialog } from "../components/SlowModelConsentDialog";
import { LocalNudgeDialog } from "../components/LocalNudgeDialog";


interface EnhancementProps {
  config: AppConfig;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  /** Increments after App receives a successful config-save response. Connection
   *  probes use this instead of the optimistic config render as their trigger. */
  configSaveRevision?: number;
  onRequestPrivacyModal: (provider: string) => void;
  /** Historical provider list from GET /api/byok-consent. Changes invalidate
   *  the context lookup; this list never authorizes a recipient or model. */
  acknowledgedProviders: string[];
  /** Called after a consent record is written or revoked so the parent can
   *  refresh acknowledgedProviders from the server. */
  onConsentChanged: () => void;
  /** Raw subscribe function from useAppSocket(). Required for DownloadDialog
   *  and local-catalog refetch on download.complete events. */
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  /** WS send from useAppSocket(). Drives the per-profile Hotkey capture
   *  (shortcut.capture.start/stop) and the "Resume auto-routing" clear. */
  send: (msg: Record<string, unknown>) => boolean;
  /** WS connection state, forwarded to the per-profile ShortcutCapture. */
  connected: boolean;
  /** Bumped by the feature-spotlight Gemini offer's "Switch to 3.5 Flash" CTA.
   *  On each increment, switch to Gemini (consent-safe) and select 3.5 Flash. */
  geminiFlashSwitchSignal?: number;
  /** Live lock status from useLockdown (App.tsx). When `config.lockdown_mode`
   *  is true the page renders the Surface D policy banner and disables cloud /
   *  Ollama provider rows (Task 5.7). Optional so the page renders safely
   *  before App's hook fires. */
  lockStatus?: LockStatus;
  /** Navigate to the Home/General tab so the "Manage" link on the Surface D
   *  policy banner can jump to the Lockdown Mode control. */
  onNavigateHome?: () => void;
  capabilities: DailyUseCapabilityReport;
  advancedCapabilities: AdvancedCapabilityReport;
}

export function Enhancement({
  config,
  onConfigChange,
  configSaveRevision = 0,
  onRequestPrivacyModal,
  acknowledgedProviders,
  onConsentChanged,
  subscribe,
  send,
  connected,
  geminiFlashSwitchSignal,
  lockStatus,
  onNavigateHome,
  capabilities,
  advancedCapabilities,
}: EnhancementProps) {
  // Derived lockdown state. Task 5.7 uses these to render the Surface D policy
  // banner and disable cloud/Ollama provider rows. Task 5.2 wires the prop so
  // the banner can be added without changing the signature again.
  // lockStatus is retained for future idle-lock timer display (locked state).
  void lockStatus;
  const isLockdown = config.lockdown_mode ?? false;
  const managedLocalCapability = capabilityDecision(capabilities, "managed-local-enhancement");
  const cloudCapability = capabilityDecision(capabilities, "cloud-enhancement");
  const ollamaCapability = capabilityDecision(capabilities, "ollama-enhancement");
  const capabilityForProvider = (provider: string) =>
    provider === "local"
      ? managedLocalCapability
      : provider === "ollama"
        ? ollamaCapability
        : cloudCapability;

  const [providerDropdownOpen, setProviderDropdownOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const [rawStatus, setStatus] = useState<StatusState>({ kind: "idle" });
  const reduceMotion = useReducedMotion();

  // Connection block expand/collapse (issue 1). The block has two resting
  // shapes: a one-line summary when connected (collapsed), and the full
  // controls (expanded). `connectionExpanded` is the user's manual override;
  // the actual rendered state also forces expanded when the active cloud
  // provider has no stored key (first-run setup card). See `connectionState`
  // and `ConnectionSection` below.
  const [connectionExpanded, setConnectionExpanded] = useState(false);

  // Key storage is per-provider so users can switch providers without
  // re-entering credentials. ``activeKeyField`` is the dataclass field that
  // backs the input; ``storedKey`` is its current value (empty for ollama /
  // local where no key is needed). Under lockdown the key field still resolves
  // against the STORED provider (not the effective local) so that the stored
  // API key is preserved and can be restored when lockdown turns off.
  const activeProvider = config.enhancement_provider || "local";
  const { storedKey, storedKeyFingerprint, localApiKey, keyDraftPending, handleRemoveKey, handleApiKeyChange, handleApiKeyBlur } = useProviderCredential({
    config, activeProvider, onConfigChange, onConsentChanged, setStatus,
  });
  const savedConnection = useSavedEnhancementConnection(config, configSaveRevision);
  const connectionSignature = savedConnection.signature;
  const workspace = useAnthropicWorkspace({
    config,
    credentialContext: JSON.stringify([activeProvider, storedKey, storedKeyFingerprint]),
    active: activeProvider === "anthropic" && !isLockdown,
    onConfigChange,
  });
  const [workspaceFailure, setWorkspaceFailure] = useState<{ signature: string; message: string } | null>(null);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const currentConnectionSignatureRef = useRef(connectionSignature);
  currentConnectionSignatureRef.current = connectionSignature;
  const lastCheckedConnectionSignatureRef = useRef<string | null>(null);
  const connectionProbeRevisionRef = useRef(0);

  // Model picker state (cloud providers only; Ollama uses its own free-text field).
  const [providerModels, setProviderModels] = useState<EnhancementModel[]>([]);

  // Advanced disclosure open/closed. Houses niche cleanup controls that don't
  // deserve top-of-page real estate. Collapsed by default.
  const [advancedOpen, setAdvancedOpen] = useState(false);

  // Ollama endpoint + model.
  const [localOllamaEndpoint, setLocalOllamaEndpoint] = useState(
    config.enhancement_ollama_endpoint || "http://localhost:11434"
  );
  const [localOllamaModel, setLocalOllamaModel] = useState(
    config.enhancement_ollama_model || "llama3.2"
  );
  const ollamaDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Custom provider base URL + model (free-text, mirrors the Ollama pattern).
  const [localCustomBaseUrl, setLocalCustomBaseUrl] = useState(
    config.enhancement_custom_base_url || ""
  );
  const customDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Include uncommitted fields: the old recipient stops being ready as soon
  // as a user types, before the debounced config save reaches the backend.
  const recipientRequest = {
    ...consentRequest(config, activeProvider),
    ...(activeProvider === "custom" ? { endpoint: localCustomBaseUrl } : {}),
    ...(activeProvider === "ollama" ? { endpoint: localOllamaEndpoint, model: localOllamaModel } : {}),
  };
  const recipientKey = JSON.stringify(recipientRequest);
  const { hasRecipientConsent, recipientError, setRecipientPermission } = useRecipientPermission({
    recipientKey, isLockdown, activeProvider, configSaveRevision, acknowledgedProviders, setStatus,
  });
  const draftsPending = keyDraftPending || workspace.pending
    || (activeProvider === "custom" && localCustomBaseUrl !== (config.enhancement_custom_base_url || ""))
    || (activeProvider === "ollama" && (
      localOllamaEndpoint !== (config.enhancement_ollama_endpoint || "http://localhost:11434")
      || localOllamaModel !== (config.enhancement_ollama_model || "llama3.2")
    ));
  const probeSignature = JSON.stringify([
    connectionSignature, recipientKey, localApiKey,
    activeProvider === "anthropic" ? workspace.draft : "", draftsPending,
  ]);
  currentConnectionSignatureRef.current = probeSignature;
  const connectionAdmitted = savedConnection.confirmed && !draftsPending
    && hasRecipientConsent && config.enhancement_enabled && !isLockdown;
  // Invalidated evidence disappears on the first draft render, before effects
  // or a debounced save. Local readiness remains owned by its supervisor.
  const status: StatusState = isLockdown && activeProvider !== "local"
    ? { kind: "idle" }
    : activeProvider !== "local" && !connectionAdmitted ? { kind: "not-checked" } : rawStatus;
  const workspaceError = workspaceFailure?.signature === probeSignature ? workspaceFailure.message : undefined;

  useEffect(() => () => {
    if (customDebounceRef.current) clearTimeout(customDebounceRef.current);
    if (ollamaDebounceRef.current) clearTimeout(ollamaDebounceRef.current);
  }, []);

  const reviewCurrentRecipient = () => {
    // Flush local debounces into the parent draft before it mounts the notice.
    if (customDebounceRef.current) clearTimeout(customDebounceRef.current);
    if (ollamaDebounceRef.current) clearTimeout(ollamaDebounceRef.current);
    if (activeProvider === "custom") onConfigChange({ enhancement_custom_base_url: localCustomBaseUrl });
    if (activeProvider === "ollama") onConfigChange({ enhancement_ollama_endpoint: localOllamaEndpoint, enhancement_ollama_model: localOllamaModel });
    onRequestPrivacyModal(activeProvider);
  };

  // ---- Local model state ----
  const [localBundledModels, setLocalBundledModels] = useState<LocalModel[]>([]);
  const [localModelCatalogLoaded, setLocalModelCatalogLoaded] = useState(false);
  const [installedLocalModelIds, setInstalledLocalModelIds] = useState<string[]>([]);
  // Ids the picker may SURFACE. A model without a pinned download source is
  // omitted by the backend so the picker hides it rather than offering a
  // download that would fail; installed models always appear. Older backends omit the
  // field -> we fall back to every bundled id (show all), so this is a no-op
  // there. Populated together with localBundledModels in fetchLocalModels.
  const [availableLocalModelIds, setAvailableLocalModelIds] = useState<string[]>([]);
  // Backend-authoritative active id for the explicit "Active model" marker.
  // Older backends omit it, so derived values fall back to config below.
  const [catalogActiveLocalModelId, setCatalogActiveLocalModelId] = useState<string | null>(null);
  const priorConfigLocalModelRef = useRef(config.enhancement_local_model);
  // Candidate activation is a backend-first operation. Prevent overlapping
  // requests so a slower response cannot overwrite a newer visible choice.
  const candidateActivationInFlightRef = useRef(false);
  // The model being downloaded (null = no download dialog open).
  const [downloadingModel, setDownloadingModel] = useState<LocalModel | null>(null);
  // Byte progress of that download, mirrored from the local_llm download
  // events so the picker trigger/helper line can show it (design.md "Trigger").
  const [localDownloadProgress, setLocalDownloadProgress] = useState<
    { downloaded: number; total: number } | null
  >(null);
  const localPickerRef = useRef<HTMLDivElement>(null);
  // Profile editor "Change model": expand the connection card, bring the local
  // model picker into view, then open it (Dropdown `openRequest`). It never
  // switches models or starts a download by itself.
  const [localPickerOpenRequest, setLocalPickerOpenRequest] = useState(0);
  const [localPickerFocusTick, setLocalPickerFocusTick] = useState(0);
  const openLocalModelPicker = () => {
    setConnectionExpanded(true);
    setLocalPickerFocusTick((tick) => tick + 1);
  };
  useEffect(() => {
    if (localPickerFocusTick === 0) return;
    localPickerRef.current?.scrollIntoView?.({ block: "nearest" });
    // Open after the scroll settles: the open panel closes on outside scroll.
    const timer = window.setTimeout(() => setLocalPickerOpenRequest((n) => n + 1), 120);
    return () => window.clearTimeout(timer);
  }, [localPickerFocusTick]);

  // ---- Low-RAM dialog state ----
  // total_ram_mb cached per-mount. null = detection unavailable (fail open:
  // skip the warning entirely). Physical RAM doesn't change at runtime, so
  // one fetch per Enhancement-tab mount is sufficient.
  const [systemRamMb, setSystemRamMb] = useState<number | null>(null);
  // When non-null, LowRamDialog is mounted for this candidate model. Set by
  // requestLocalDownload() when system RAM < model.ram_mb.
  const [lowRamCandidate, setLowRamCandidate] = useState<LocalModel | null>(null);

  // ---- Low-disk dialog state ----
  // When non-null, LowDiskSpaceDialog is mounted for this candidate. Set by
  // requestLocalDownload() when free disk space < required (model disk_mb +
  // 200 MB buffer). Disk check is a HARD block (the download would
  // physically fail), unlike the RAM check which is a soft warning.
  const [lowDiskCandidate, setLowDiskCandidate] = useState<
    { model: LocalModel; freeMb: number; requiredMb: number } | null
  >(null);

  // ---- Data-leaves dialog state ----
  // When non-null, DataLeavesDialog is mounted for this provider pick.
  // previousProvider captured at dialog open so Cancel can revert the
  // dropdown to the prior selection (the dropdown's displayed value is
  // derived from config.enhancement_provider, not internal state, so a
  // "no-op write" still requires we keep the prior value to compare against).
  const [dataLeavesCandidate, setDataLeavesCandidate] = useState<
    | { provider: "gemini" | "openai" | "anthropic" | "ollama" | "custom"; previousProvider: string }
    | null
  >(null);
  // Per-session "user already saw Dialog B for this provider this run."
  // Not persisted to disk: Dialog B is informational, not legal consent, so
  // the durable record lives on PrivacyModal's onConfirm path (byok-consent).
  // Re-using `acknowledgedProviders` would conflate "saw friendly heads-up"
  // with "signed binding consent record," so we track separately.
  const dataLeavesAckedRef = useRef<Set<string>>(new Set());

  // Current local model: use config value, falling back to first bundled model.
  const currentLocalModelId = config.enhancement_local_model
    || (localBundledModels[0]?.id ?? AUTO_BENCHMARK_MODEL_ID);
  const { benchmarkFlow, setBenchmarkFlow, openBenchmarkFlow } = useBenchmarkFlow({
    localBundledModels, currentLocalModelId, availableLocalModelIds,
    verdicts: config.enhancement_local_gpu_verdicts,
    isLockdown, provider: config.enhancement_provider,
  });

  // Manual local-model switch consent (round 01, Issue 3). Set when the user
  // picks a fine-tune whose speed here warrants a heads-up:
  //  - variant:"cached" — its cached verdict is already `marginal` (no re-bench).
  //  - variant:"fresh"  — never benched: SlowModelConsentDialog runs a single-
  //    model bench, then shows consent (marginal) / applies (viable) / blocks.
  const [slowConsent, setSlowConsent] = useState<
    null | { modelId: string; variant: "cached" | "fresh" }
  >(null);

  // First-visit nudge (Feature #2): shown once to a pre-existing cloud user.
  const [showLocalNudge, setShowLocalNudge] = useState(false);
  const nudgeEvaluatedRef = useRef(false);
  useEffect(() => {
    // config is guaranteed loaded here (App gates the UI on a non-null config).
    if (nudgeEvaluatedRef.current) return;
    nudgeEvaluatedRef.current = true;
    const provider = config.enhancement_provider || "local";
    if (
      getLocalGpuVerdict(config.enhancement_local_gpu_verdicts, currentLocalModelId) === "not_run" &&
      provider !== "local" &&
      !config.enhancement_local_nudge_seen
    ) {
      setShowLocalNudge(true);
    }
    // Run once on mount; config is loaded by the time Enhancement renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const dismissLocalNudge = () => {
    setShowLocalNudge(false);
    onConfigChange({ enhancement_local_nudge_seen: true });
  };
  const acceptLocalNudge = () => {
    if (!openBenchmarkFlow("intro")) {
      void fetchLocalModels();
      return;
    }
    setShowLocalNudge(false);
    onConfigChange({ enhancement_local_nudge_seen: true });
  };

  // Feature-spotlight Gemini offer: App bumps this signal when "Switch to 3.5
  // Flash" fires. Set the target model (safe regardless of consent) then switch
  // provider through the consent-safe path. Different keys merge in patchConfig's
  // debounce so neither write is dropped; provider is already gemini for a
  // targeted user, so requestProviderPick is a direct write.
  const geminiFlashSwitchRef = useRef(0);
  useEffect(() => {
    const sig = geminiFlashSwitchSignal ?? 0;
    if (sig === 0 || sig === geminiFlashSwitchRef.current) return;
    geminiFlashSwitchRef.current = sig;
    onConfigChange({ enhancement_gemini_model: "gemini-3.5-flash" });
    requestProviderPick("gemini");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [geminiFlashSwitchSignal]);
  // Pending flag: when true, the next handleLocalDownloadComplete issues a
  // combined { enhancement_local_model, enhancement_enabled: true } patch
  // instead of the bare model-id patch. Ref (not state) so that StrictMode's
  // double-mount of subscribers can't double-fire the auto-enable write
  // against the App.tsx 500ms debounce. Cleared in every terminal path
  // (complete, cancel, error -> Cancel).
  const pendingEnableAfterDownloadRef = useRef(false);

  // Fetch the local model catalog (bundled models + installed ids).
  const fetchLocalModels = async () => {
    setLocalModelCatalogLoaded(false);
    try {
      const r = await getEnhancementModels("local");
      const bundled: LocalModel[] = (r.bundled ?? []).map((m) => ({
        id: m.id,
        label: m.label,
        disk_mb: m.disk_mb ?? 0,
        ram_mb: m.ram_mb ?? 0,
        min_system_ram_mb: m.min_system_ram_mb,
        recommended: m.recommended,
        prompt_key: m.prompt_key,
        capabilities: m.capabilities,
        source: m.source,
        experiment_id: m.experiment_id,
        parameter_size: m.parameter_size,
        role: m.role,
        status: m.status,
        serving_contract: m.serving_contract,
        trained_modes: m.trained_modes,
        artifact_available: m.artifact_available,
        selectable: m.selectable,
        downloadable: m.downloadable,
        active: m.active,
      }));
      setLocalBundledModels(bundled);
      setInstalledLocalModelIds(r.installed_ids ?? []);
      // Older backends omit available_ids -> show every bundled id (no gate).
      setAvailableLocalModelIds(r.available_ids ?? bundled.map((m) => m.id));
      setCatalogActiveLocalModelId(
        r.active_id ?? bundled.find((m) => m.active)?.id ?? null,
      );
    } catch (err) {
      console.warn(`[Enhancement] local model fetch failed error_type=${errorType(err)}`);
    } finally {
      setLocalModelCatalogLoaded(true);
    }
  };

  // Fetch local catalog on mount.
  useEffect(() => {
    fetchLocalModels();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Once the catalog has seeded its backend-authoritative marker, keep it in
  // step with later config selections without overwriting it on first mount.
  useEffect(() => {
    if (priorConfigLocalModelRef.current === config.enhancement_local_model) return;
    priorConfigLocalModelRef.current = config.enhancement_local_model;
    setCatalogActiveLocalModelId(config.enhancement_local_model || null);
  }, [config.enhancement_local_model]);

  // Fetch total physical RAM on mount. Detection failure (null) is the
  // explicit fail-open contract from dialog-design.md: skip the warning
  // rather than block a legitimate download.
  useEffect(() => {
    let cancelled = false;
    getSystemInfo()
      .then((info) => {
        if (cancelled) return;
        setSystemRamMb(info.total_ram_mb);
      })
      .catch((err) => {
        if (cancelled) return;
        // Network blip or endpoint missing on an older backend: fail open.
        console.warn(`[Enhancement] system info fetch failed error_type=${errorType(err)} action=fail_open`);
        setSystemRamMb(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Refetch local catalog whenever a local_llm download completes.
  useEffect(() => {
    return subscribeToDownloadEvents(
      subscribe,
      (event) => {
        if (event.type === "download.progress") {
          setLocalDownloadProgress({ downloaded: event.downloaded, total: event.total });
          return;
        }
        setLocalDownloadProgress(null);
        if (event.type === "download.complete") {
          fetchLocalModels();
        }
      },
      "local_llm",
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subscribe]);

  // Close dropdown on outside click (same pattern as Audio.tsx).
  useEffect(() => {
    if (!providerDropdownOpen) return;
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setProviderDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [providerDropdownOpen]);

  // Check once on mount, then only after App confirms that a changed connection
  // signature reached the backend. App renders config optimistically, so probing
  // on those renders can otherwise race the debounced save and ask the backend
  // about the previous provider/model. Each result is also bound to the exact
  // signature that started it so a superseded model cannot overwrite the UI.
  useEffect(() => {
    const signature = currentConnectionSignatureRef.current;
    if (!connectionAdmitted) {
      connectionProbeRevisionRef.current += 1;
      lastCheckedConnectionSignatureRef.current = null;
      if (activeProvider !== "local") setStatus({ kind: "idle" });
      return;
    }
    if (lastCheckedConnectionSignatureRef.current === signature) return;
    lastCheckedConnectionSignatureRef.current = signature;
    const probeRevision = ++connectionProbeRevisionRef.current;
    const provider = config.enhancement_provider || "local";
    if (provider === "local") return; // local provider needs no connection check; local-badge effect below owns the badge
    const isOllamaProvider = provider === "ollama";
    const hasKey = !!storedKey;
    if (!config.enhancement_enabled) {
      setStatus({ kind: "idle" });
      return;
    }
    if (!isOllamaProvider && !hasKey) {
      setStatus({ kind: "idle" });
      return;
    }

    setStatus({ kind: isOllamaProvider ? "ollama-loading" : "checking" });
    setWorkspaceFailure(null);

    const resultIsCurrent = () =>
      connectionProbeRevisionRef.current === probeRevision
      && currentConnectionSignatureRef.current === signature;

    checkEnhancement()
      .then((result) => {
        if (!resultIsCurrent()) return;
        if (result.connected) {
          setStatus({ kind: "connected" });
        } else if (result.error_code === "consent_required") {
          setRecipientPermission({ key: recipientKey, allowed: false });
          setStatus({ kind: "idle" });
        } else if (provider === "anthropic" && isWorkspaceError(result.error_code)) {
          const message = WORKSPACE_ERRORS[result.error_code];
          setWorkspaceFailure({ signature, message });
          workspace.reveal();
          setConnectionExpanded(true);
          setStatus({ kind: "error", message });
        } else {
          setStatus({ kind: "error", message: enhancementErrorMessage({ errorCode: result.error_code }, "Could not connect. Check your connection settings and try again.") });
        }
      })
      .catch(() => {
        if (!resultIsCurrent()) return;
        setStatus({ kind: "error", message: "Could not connect. Check your connection settings and try again." });
      });
    return () => {
      connectionProbeRevisionRef.current += 1;
      lastCheckedConnectionSignatureRef.current = null;
    };
  }, [connectionAdmitted, probeSignature]);

  useEffect(() => {
    if (workspaceError && workspace.visible) {
      workspaceRef.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
    }
  }, [workspaceError, workspace.visible]);

  // Seed the local badge from REST on mount when local provider is active.
  // Early-returns when provider is not local; sets idle when enhancement
  // is disabled. The WS subscription below refines on every supervisor
  // transition after this initial snapshot.
  useEffect(() => {
    const provider = config.enhancement_provider || "local";
    if (provider !== "local") return;
    if (!config.enhancement_enabled) {
      setStatus({ kind: "idle" });
      return;
    }

    let cancelled = false;
    getLlamaStatus()
      .then((s) => {
        if (cancelled) return;
        setStatus(mapLlamaStateToStatus(s));
      })
      .catch(() => {
        if (cancelled) return;
        // Seed failure: leave the badge idle. The WS will refine on
        // the next supervisor transition.
      });
    return () => {
      cancelled = true;
    };
  }, [config.enhancement_provider, config.enhancement_enabled]);

  // Subscribe to llama.status WS events when local provider is active.
  // Early-returns when provider is not local or enhancement is disabled.
  // Dep on subscribe mirrors the existing subscribeToDownloadEvents pattern.
  useEffect(() => {
    if (config.enhancement_provider !== "local") return;
    if (!config.enhancement_enabled) return;
    return subscribe((event) => {
      if (event.type !== "llama.status") return;
      setStatus(
        mapLlamaStateToStatus({
          state: event.state,
          model: event.model,
          port: event.port ?? undefined,
          message: event.message,
        }),
      );
    });
  }, [config.enhancement_provider, config.enhancement_enabled, subscribe]);

  useEffect(() => {
    setLocalOllamaEndpoint(config.enhancement_ollama_endpoint || "http://localhost:11434");
    setLocalOllamaModel(config.enhancement_ollama_model || "llama3.2");
  }, [config.enhancement_ollama_endpoint, config.enhancement_ollama_model]);

  useEffect(() => {
    setLocalCustomBaseUrl(config.enhancement_custom_base_url || "");
  }, [config.enhancement_custom_base_url, config.enhancement_custom_model]);

  const handleEnableToggle = (enabled: boolean) => {
    if (!capabilityForProvider(config.enhancement_provider || "local").enabled) return;
    if (enabled) {
      const provider = currentProvider;
      if (provider === "local") {
        // local: no consent needed, no API key required.
        // local-gpu-benchmark Task 3.3: when the model is not installed OR the
        // GPU verdict has never been run, open the BenchmarkFlow at "intro"
        // and bail. The toggle stays visually off — we never write
        // enhancement_enabled here. The flow's onVerdict handler issues the
        // single combined patch (success -> keep local + enable; failure ->
        // route to Gemini through the consent chain).
        // tiered-benchmark Task 3.1 (scope.md decision 4, loosened gating):
        // an already-benched model — viable OR marginal — is freely
        // selectable and skips the flow entirely (the marginal gold note
        // under the picker is the only warning). Unbenched (not_run) still
        // opens the flow; unviable also falls through here and stays
        // blocked by the flow's existing failure card.
        if (!localModelInstalled || !localModelBenched) {
          openBenchmarkFlow("intro");
          return;
        }
        // verdict already viable/marginal + model installed: straight through, no modal
      } else {
        // existing cloud path — unchanged
        const isOllamaProvider = provider === "ollama";
        const hasKey = !!storedKey.trim();
        // Consent-gate ordering per
        // PRIVACY.md §6.5: "explicitly select a provider AND supply a
        // valid credential" before the blocking dialog.
        // Issue 1: enabling while unconfigured routes into the expanded setup
        // card rather than surfacing a silent "Add API key first" error. The
        // Connection block already force-expands when an active cloud provider
        // has no stored key (`connectionState === "unconfigured"`); we never
        // write enhancement_enabled, so the toggle stays visually off until a
        // valid key checks out.
        if (!isOllamaProvider && !hasKey) {
          setConnectionExpanded(true);
          return;
        }
        if (!hasRecipientConsent) {
          reviewCurrentRecipient();
          return;
        }
      }
    }
    onConfigChange({ enhancement_enabled: enabled });
    if (!enabled) setStatus({ kind: "idle" });
  };

  const handleOllamaEndpointChange = (value: string) => {
    setLocalOllamaEndpoint(value);
    if (ollamaDebounceRef.current) clearTimeout(ollamaDebounceRef.current);
    ollamaDebounceRef.current = setTimeout(() => {
      onConfigChange({ enhancement_ollama_endpoint: value });
    }, 1000);
  };

  const handleOllamaModelChange = (value: string) => {
    setLocalOllamaModel(value);
    if (ollamaDebounceRef.current) clearTimeout(ollamaDebounceRef.current);
    ollamaDebounceRef.current = setTimeout(() => {
      onConfigChange({ enhancement_ollama_model: value });
    }, 1000);
  };

  const handleCustomBaseUrlChange = (value: string) => {
    setLocalCustomBaseUrl(value);
    if (customDebounceRef.current) clearTimeout(customDebounceRef.current);
    customDebounceRef.current = setTimeout(() => {
      onConfigChange({ enhancement_custom_base_url: value });
    }, 1000);
  };

  // ---- Local model handlers ----

  const openDownloadDialog = (model: LocalModel) => {
    // The DownloadDialog will call startFn() on mount — no eager call here.
    setLocalDownloadProgress(null);
    setDownloadingModel(model);
  };

  // Low-disk + low-RAM gates per dialog-design.md §"Dialog A § Triggers".
  // Order of checks: disk first (HARD block, dialog cannot be bypassed
  // because download would physically fail), then RAM (soft warning, user
  // can override with "Download anyway"). The RAM threshold lives on a
  // dedicated catalog field (``min_system_ram_mb``) decoupled from
  // ``ram_mb`` (the model's runtime cost); falls back to ``ram_mb`` when
  // absent. Both checks fail open: detection failures (free_mb == -1 from
  // OS-denied disk.usage; systemRamMb == null) proceed to download so a
  // detection bug doesn't block a perfectly fine machine.
  //   disk free < required   -> mount LowDiskSpaceDialog (hard block)
  //   else, low RAM          -> mount LowRamDialog (soft warning)
  //   else                   -> open download directly
  const requestLocalDownload = async (model: LocalModel) => {
    // Development candidates are developer-supplied artifacts. They never
    // enter the managed production download, disk, or RAM flows.
    if (model.source === "candidate" || model.downloadable === false) return;
    try {
      const { free_mb, required_mb } = await getEnhancementDiskFree(model.id);
      if (
        free_mb >= 0
        && typeof required_mb === "number"
        && free_mb < required_mb
      ) {
        setLowDiskCandidate({
          model,
          freeMb: free_mb,
          requiredMb: required_mb,
        });
        return;
      }
    } catch (err) {
      // Fail open: if the disk check itself errors, fall through to the
      // RAM check and download path. The backend's defensive 412 check in
      // POST /api/enhancement/local-download/start still guards against
      // an actual insufficient-space download attempt.
      console.warn(`[Enhancement] disk check failed error_type=${errorType(err)}`);
    }

    const threshold = model.min_system_ram_mb ?? model.ram_mb;
    if (systemRamMb == null || threshold <= 0 || systemRamMb >= threshold) {
      openDownloadDialog(model);
      return;
    }
    setLowRamCandidate(model);
  };

  // Delete the local model GGUF from disk. Wired into the Decision 3 inline
  // confirm flow on the Local row of the provider dropdown (only reachable
  // when provider != "local" AND the model is installed). On success we
  // refetch the catalog so installedLocalModelIds reflects reality. On
  // failure we surface the fault via the existing rose status pill — no
  // toast (per Task 2.9: the status pill is the existing surface).
  const handleDeleteLocalModel = async (modelId: string) => {
    const model = localBundledModels.find((entry) => entry.id === modelId);
    // Candidate files live outside Whisperstream's managed model directory.
    // Guard the handler as well as every affordance so stale UI cannot delete
    // or relocate a developer-owned artifact.
    if (!model || model.source === "candidate" || model.downloadable === false) return;
    try {
      await deleteLocalModel(modelId);
      await fetchLocalModels();
    } catch (err) {
      console.warn(`[Enhancement] local model delete failed error_type=${errorType(err)}`);
      setStatus({ kind: "error", message: "Delete failed" });
    }
  };

  const handleLowDiskSwitchToCloud = () => {
    setLowDiskCandidate(null);
  };

  const handleLowDiskDismiss = () => {
    setLowDiskCandidate(null);
  };

  // RE-TARGET flow (low-disk dialog "Choose a different folder"):
  // Nothing has been downloaded yet, so there is nothing to move.
  // We simply save the new model_storage_dir, dismiss the dialog, and
  // re-run the download gate (disk check + RAM check + openDownloadDialog).
  // The backend re-bases get_local_llm_dir() onto the new dir, so the
  // subsequent getEnhancementDiskFree call probes the new location.
  const handleLowDiskChooseFolder = async () => {
    const candidate = lowDiskCandidate;
    if (!candidate) return;
    // Close the dialog before opening the picker so the two don't overlap.
    setLowDiskCandidate(null);
    const { open } = await import("@tauri-apps/plugin-dialog");
    const selected = await open({
      directory: true,
      multiple: false,
      title: "Choose where to store models",
    });
    if (!selected || typeof selected !== "string") return; // cancel = silent no-op
    try {
      // Persist synchronously so the immediate re-download reads the new dir.
      await saveConfig({ model_storage_dir: selected });
      // Mirror into App's in-memory config so a later config save doesn't PUT a
      // stale (empty) model_storage_dir over it.
      onConfigChange({ model_storage_dir: selected });
    } catch (err) {
      console.warn(`[Enhancement] model storage save failed error_type=${errorType(err)}`);
      return;
    }
    // Re-run the full disk + RAM gate so the flow continues from the new dir.
    await requestLocalDownload(candidate.model);
  };

  const handleLowRamSwitchToCloud = () => {
    setLowRamCandidate(null);
  };

  const handleLowRamContinue = () => {
    // Take the original download path. Mirrors the pre-Dialog-A click flow.
    const model = lowRamCandidate;
    setLowRamCandidate(null);
    if (model) openDownloadDialog(model);
  };

  const handleLowRamDismiss = () => {
    // No side effect: user stays on Enhancement tab, local row unchanged.
    setLowRamCandidate(null);
  };

  // Data-leaves gate for any non-local provider pick. Fires BEFORE
  // PrivacyModal. Per dialog-design.md §"Dialog B § Triggers":
  //   provider == "local"                              -> no dialog
  //   dataLeavesAckedRef.current.has(provider)         -> skip B, go to PrivacyModal
  //   else                                             -> mount DataLeavesDialog
  // Ollama triggers Dialog B too, regardless of endpoint: we cannot
  // introspect the endpoint at selection time.
  const requestProviderPick = (
    newProvider: "local" | "gemini" | "openai" | "anthropic" | "ollama" | "custom",
  ) => {
    // Local is the cleanup-on-device path: no DataLeavesDialog and no
    // PrivacyModal because cleanup runs on this computer. But (Bug #3) don't
    // silently switch: if the model isn't installed or the GPU verdict isn't
    // already benched viable OR marginal (tiered-benchmark Task 3.1, scope.md
    // decision 4 — loosened gating), run the benchmark flow (intro) and let
    // its verdict do the provider write. A proven-viable OR marginal +
    // installed local goes straight through; unviable falls through to the
    // flow and stays blocked there. failureFallback="current" (snapshotted in
    // openBenchmarkFlow) keeps the user on their existing cloud provider if
    // the benchmark fails.
    if (newProvider === "local") {
      if (!localModelInstalled || !localModelBenched) {
        openBenchmarkFlow("intro");
        return;
      }
      onConfigChange({ enhancement_provider: "local" });
      return;
    }
    if (dataLeavesAckedRef.current.has(newProvider)) {
      // Already saw Dialog B this session: skip B, go straight to PrivacyModal.
      if (newProvider === "custom" || newProvider === "ollama") {
        onConfigChange({ enhancement_provider: newProvider, enhancement_enabled: false });
      } else onRequestPrivacyModal(newProvider);
      return;
    }
    setDataLeavesCandidate({
      provider: newProvider,
      previousProvider: config.enhancement_provider || "local",
    });
  };

  const handleDataLeavesContinue = () => {
    const candidate = dataLeavesCandidate;
    setDataLeavesCandidate(null);
    if (!candidate) return;
    // Mark seen-this-session so re-picking the same provider skips B.
    dataLeavesAckedRef.current.add(candidate.provider);
    // Chain into PrivacyModal — the existing binding-consent flow.
    if (candidate.provider === "custom" || candidate.provider === "ollama") {
      onConfigChange({ enhancement_provider: candidate.provider, enhancement_enabled: false });
    } else onRequestPrivacyModal(candidate.provider);
  };

  const handleDataLeavesCancel = () => {
    // Revert the dropdown: the dropdown's value is derived from
    // config.enhancement_provider, so writing the previous value restores
    // it. If previousProvider matches the current value we skip the write
    // (no-op) — the dropdown was never updated yet because the consent
    // gate fires BEFORE the write.
    setDataLeavesCandidate(null);
    // No config write needed: we never advanced config in the first place,
    // so the dropdown still shows the previous selection. Matches the
    // existing PrivacyModal Cancel semantics added by the UX simplification.
  };

  // BenchmarkFlow terminal verdict. Every measured terminal outcome
  // each issues a SINGLE combined config patch (App.tsx debounces patchConfig
  // by 500ms, so two back-to-back writes race the merge and one field can be
  // silently dropped — never write sequentially here).
  //
  //  - viable:   keep provider=local, enable, set the measured model active, and
  //              merge its real verdict into the cache. The backend
  //              already persisted enhancement_local_gpu_device when it wrote the
  //              verdict; we echo the verdicts into the same write so the page
  //              re-renders immediately without waiting for a config GET.
  //  - unviable: auto-delete the model THIS flow freshly downloaded
  //              (v.freshlyDownloaded), then follow the snapshotted fallback. An ordinary Local flow routes
  //              to Gemini THROUGH requestProviderPick("gemini") so the
  //              DataLeavesDialog -> PrivacyModal consent chain is not bypassed;
  //              Lockdown and existing cloud providers stay where they are.
  //              The measured model's enhancement_local_gpu_verdicts entry +
  //              enhancement_enabled ride the combined write. The existing
  //              currentProvider !== "local" gate auto-reveals the cloud model
  //              picker — no reveal flag.
  const handleBenchmarkVerdict = (v: BenchmarkVerdict) => {
    // Snapshot the failure route (decided by the provider at flow-open) before
    // clearing the flow. "gemini" == ordinary local; "current" == an existing
    // cloud provider; "local" == Lockdown.
    const failureFallback = benchmarkFlow?.failureFallback ?? "gemini";
    const benchmarkModelId = benchmarkFlow?.benchModelId ?? currentLocalModelId;
    setBenchmarkFlow(null);
    // The config mapping lives in buildBenchmarkVerdictPatch (shared with
    // onboarding); this handler performs the side effects it asks for.
    const plan = buildBenchmarkVerdictPatch(v, config, {
      failureFallback,
      benchmarkModelId,
      currentLocalModelId,
      installedLocalModelIds,
    });
    // Option B: auto-delete a freshly downloaded model only on give-up,
    // deferred so the benchmark's llama-server releases the GGUF handle first
    // (avoids a file-lock error). "Try again" never reaches here.
    if (plan.deleteModelIds.length > 0) {
      const toDelete = plan.deleteModelIds;
      window.setTimeout(() => {
        toDelete.forEach((id) => deleteLocalModel(id).catch(() => undefined));
      }, 400);
    }
    if (plan.patch) onConfigChange(plan.patch);
    // Refresh the picker so the newly benched model shows as present.
    if (plan.refreshLocalModels) fetchLocalModels();
    // Defensive: an applied model that is not on disk goes through the SAME
    // download gate the picker uses rather than being left unusable.
    if (plan.downloadModelId) {
      const model = localBundledModels.find((m) => m.id === plan.downloadModelId);
      if (model) requestLocalDownload(model);
    }
    // Unviable on ordinary local routes to Gemini through the consent chain.
    // The currentProvider !== "local" gate then reveals the cloud model picker.
    if (plan.providerPick) requestProviderPick(plan.providerPick);
  };

  // Local model picker onChange gate (round 01, Issue 3). Switching UP to a
  // fine-tune that runs slowly here should warn, not silently apply. Gemma
  // (third-party, never auto-benched) gets the full manual BenchmarkFlow the
  // first time; a viable/already-resolved pick just switches.
  const handleLocalModelSelect = async (id: string) => {
    if (id === currentLocalModelId) return; // no-op
    const model = localBundledModels.find((entry) => entry.id === id);
    if (model?.source === "candidate") {
      // Missing candidates remain visible for registry context, but both the
      // dropdown and this handler refuse activation unless the backend marked
      // the artifact selectable and present.
      if (model.artifact_available !== true || model.selectable === false) return;
      if (candidateActivationInFlightRef.current) return;
      candidateActivationInFlightRef.current = true;
      try {
        const activated = await activateLocalCandidate(model.id);
        // The endpoint persists and repoints the backend first. Only after it
        // succeeds do we update the visible config and authoritative marker.
        setCatalogActiveLocalModelId(activated.model_id);
        onConfigChange({ enhancement_local_model: activated.model_id });
      } catch (err) {
        console.warn(`[Enhancement] candidate activation failed error_type=${errorType(err)}`);
        setStatus({ kind: "error", message: "Activation failed" });
      } finally {
        candidateActivationInFlightRef.current = false;
      }
      return;
    }
    const verdict = getLocalGpuVerdict(config.enhancement_local_gpu_verdicts, id);
    const isFineTune = LOCAL_MODEL_FIRST_PARTY[id] === true; // Stream-1 Mini
    // A fine-tune with a cached `marginal` verdict -> consent from the cache
    // (no re-bench).
    if (verdict === "marginal") {
      setSlowConsent({ modelId: id, variant: "cached" });
      return;
    }
    // A never-benched fine-tune -> run a single-model bench first, then consent.
    if (isFineTune && verdict === "not_run") {
      setSlowConsent({ modelId: id, variant: "fresh" });
      return;
    }
    // An un-benched Gemma -> the manual single-model BenchmarkFlow (its only
    // bench trigger; a marginal Gemma shows the C(a) card).
    if (id === "gemma4-e2b-instruct" && verdict === "not_run") {
      const failureFallback = isLockdown
        ? "local"
        : (config.enhancement_provider || "local") === "local"
          ? "gemini"
          : "current";
      setBenchmarkFlow({
        start: "prefilter",
        mode: "setup",
        failureFallback,
        benchMode: "manual",
        benchModelId: id,
      });
      return;
    }
    if (verdict === "unviable") return;
    // Viable or a previously consented model can switch.
    onConfigChange({ enhancement_local_model: id });
  };

  // The user accepted a slow fine-tune from SlowModelConsentDialog. ONE combined
  // patch: set it active + cache its verdict. Do NOT touch enhancement_enabled /
  // provider — this is a model switch, not an enable (preserves the single-
  // combined-patch invariant).
  const handleSlowConsentUse = (
    modelId: string,
    verdict: "viable" | "marginal",
  ) => {
    setSlowConsent(null);
    onConfigChange({
      enhancement_local_model: modelId,
      enhancement_local_gpu_verdicts: {
        ...config.enhancement_local_gpu_verdicts,
        [modelId]: verdict,
      },
    });
  };

  const handleLocalDownloadComplete = () => {
    const modelId = downloadingModel?.id;
    setDownloadingModel(null);
    if (modelId) {
      // When the auto-enable flow set the pending flag, the model write
      // AND the enable write MUST land as a SINGLE onConfigChange patch.
      // App.tsx debounces patchConfig() by 500ms; two back-to-back patches
      // race the merge and one of the fields can be silently dropped. The
      // combined patch is the canonical fix per research.md.
      if (pendingEnableAfterDownloadRef.current) {
        pendingEnableAfterDownloadRef.current = false;
        onConfigChange({
          enhancement_local_model: modelId,
          enhancement_enabled: true,
        });
      } else {
        onConfigChange({ enhancement_local_model: modelId });
      }
    }
    fetchLocalModels();
  };

  const handleLocalDownloadCancel = async () => {
    // User cancelled mid-download (or hit Cancel on the dialog error
    // surface). Clear the pending flag without writing enhancement_enabled:
    // the toggle was never visually on, so leaving config untouched keeps
    // the UI consistent with what the user sees.
    pendingEnableAfterDownloadRef.current = false;
    setDownloadingModel(null);
  };

  // ---- Derived values ----

  // Under lockdown the effective provider is always "local" (Task 5.7 / design D).
  // The STORED enhancement_provider is preserved (never mutated here) so it
  // restores exactly when lockdown is toggled off (restore-on-off).
  const storedProvider = config.enhancement_provider || "local";
  const currentProvider = isLockdown ? "local" : storedProvider;
  const isOllama = currentProvider === "ollama";
  const isCustom = currentProvider === "custom";
  // Hostname extracted from the custom base URL for host-aware data-handling copy.
  // Empty string when the field is unset or the URL is unparseable.
  const customHost = (() => {
    const raw = (config.enhancement_custom_base_url || "").trim();
    if (!raw) return "";
    try { return new URL(raw).hostname; } catch { return ""; }
  })();
  const ollamaEndpoint = localOllamaEndpoint.trim();
  const providerHelp = PROVIDER_HELP[currentProvider];
  const currentProviderLabel =
    PROVIDERS.find((p) => p.value === currentProvider)?.label ?? currentProvider;

  const currentLocalModel: LocalModel = localBundledModels.find(
    (m) => m.id === currentLocalModelId
  ) ?? {
    id: currentLocalModelId,
    label: currentLocalModelId,
    disk_mb: 0,
    ram_mb: 0,
  };

  const activeModelCapabilities = localModelCapabilities(currentLocalModel);
  // Per-model UI state, keyed by catalog id (local-model-picker Task 3.3).
  // Generalizes the old single-model installed/downloading checks (which
  // assumed exactly one bundled local model) into a lookup any picker row —
  // or the detail line below the trigger — can read by its OWN id,
  // independent of which model happens to be selected.
  const localModelStateById: Record<string, { installed: boolean; downloading: boolean }> =
    useMemo(() => {
      const map: Record<string, { installed: boolean; downloading: boolean }> = {};
      for (const m of localBundledModels) {
        map[m.id] = {
          installed:
            m.source === "candidate"
              ? m.artifact_available === true
              : installedLocalModelIds.includes(m.id),
          downloading: downloadingModel?.id === m.id,
        };
      }
      return map;
    }, [localBundledModels, installedLocalModelIds, downloadingModel]);

  const localModelInstalled = localModelStateById[currentLocalModelId]?.installed ?? false;
  const currentLocalModelIsCandidate = currentLocalModel.source === "candidate";
  const currentLocalModelManaged =
    currentLocalModel.downloadable !== false
    && (
      currentLocalModel.source === "production"
      || (
        currentLocalModel.source === undefined
        && localBundledModels.some((model) => model.id === currentLocalModelId)
      )
    );
  const activeLocalModelId =
    catalogActiveLocalModelId
    ?? config.enhancement_local_model
    ?? currentLocalModelId;
  // Whether the SELECTED model is the one actively downloading — drives the
  // detail line's transient "Downloading…" state (copy-review.md Locked Copy
  // Decision #6). The DownloadDialog modal owns progress; this only mirrors
  // the same in-flight state under the trigger.
  const localModelDownloading = localModelStateById[currentLocalModelId]?.downloading ?? false;
  // Tri-state cached verdict for the SELECTED model + the loosened-gating
  // check (scope.md decision 4): viable OR marginal is an already-benched
  // model, freely selectable without reopening BenchmarkFlow. not_run
  // (unbenched) and unviable both fall through to the flow below — unviable
  // stays a hard block there (using the active fallback policy), while
  // not_run runs the benchmark for the first time. Read once here so
  // handleEnableToggle and requestProviderPick share one definition instead
  // of re-deriving the tri-state check twice.
  const localModelVerdict = getLocalGpuVerdict(config.enhancement_local_gpu_verdicts, currentLocalModelId);
  const localModelBenched =
    (currentLocalModelIsCandidate && localModelInstalled)
    || localModelVerdict === "viable"
    || localModelVerdict === "marginal";

  // Managed rows preserve their production naming/actions and publication
  // gate. Candidate rows follow in registry order with registry-owned labels,
  // including missing artifacts, with explicit metadata and state text.
  // Whole-percent progress of the in-flight local download, or null before the
  // first progress event (unknown size).
  const downloadPercent =
    localDownloadProgress && localDownloadProgress.total > 0
      ? Math.min(100, Math.floor((localDownloadProgress.downloaded / localDownloadProgress.total) * 100))
      : null;

  const localModelOptions: DropdownOption<string>[] = useMemo(() => {
    const visibleProduction = localBundledModels
      .filter((m) => m.source !== "candidate")
      // Hide-until-published gate: only surface an available model. The
      // currently-selected id is always kept so the trigger never renders a
      // model it can't name (defensive — the default, Gemma, is always
      // available). Installed models are reported available by the backend.
      .filter(
        (m) =>
          availableLocalModelIds.includes(m.id) || m.id === currentLocalModelId,
      );

    // DISPLAY-ONLY order: first-party Stream-1 Mini first, then Customizable,
    // with no divider or group heading between the two rows (design.md "Open
    // panel rows"). Stable sort by first-party rank preserves the
    // catalog's within-group order, so this NEVER touches `localBundledModels`
    // (the config-default == catalog-position-0 invariant, read as
    // `localBundledModels[0]?.id` above, is unaffected — this is a derived,
    // render-only array).
    const orderedProduction = [...visibleProduction].sort(
      (a, b) =>
        (LOCAL_MODEL_FIRST_PARTY[a.id] ? 0 : 1) -
        (LOCAL_MODEL_FIRST_PARTY[b.id] ? 0 : 1),
    );
    const candidates = localBundledModels.filter((m) => m.source === "candidate");
    const ordered = [...orderedProduction, ...candidates];

    return ordered.map((m, i) => {
      if (m.source === "candidate") {
        const artifactAvailable =
          m.artifact_available === true && m.selectable !== false;
        const isActive = m.id === activeLocalModelId;
        const isFirstCandidate = i === orderedProduction.length;
        const metadata = [m.parameter_size, m.role, m.status]
          .filter((value): value is string => Boolean(value))
          .join(" · ");
        return {
          value: m.id,
          label: m.label ?? m.experiment_id ?? m.id,
          description: metadata,
          disabled: !artifactAvailable,
          dividerBefore: isFirstCandidate && orderedProduction.length > 0,
          adornment: (
            <span
              className={`inline-flex flex-col items-end gap-0.5 text-[10px] leading-3 font-medium ${
                artifactAvailable ? "text-steel" : "text-steel/60"
              }`}
            >
              {isFirstCandidate && <span>Development candidates</span>}
              <span>
                {artifactAvailable ? "Artifact available" : "Artifact unavailable"}
              </span>
              {isActive && <span>Active model</span>}
            </span>
          ),
        };
      }

      const state = localModelStateById[m.id];
      const installed = state?.installed ?? false;
      const downloading = state?.downloading ?? false;
      const verdict = getLocalGpuVerdict(config.enhancement_local_gpu_verdicts, m.id);
      const unviable = verdict === "unviable";
      const marginal = verdict === "marginal";
      const label = LOCAL_MODEL_DISPLAY_LABEL[m.id] ?? m.label;
      const size = formatModelSizeShort(m.disk_mb);
      const percent = downloading ? downloadPercent : null;
      const baseDescription = LOCAL_MODEL_DESCRIPTION[m.id];
      const descriptionHover = LOCAL_MODEL_DESCRIPTION_HOVER[m.id];
      // Panel line 2 switches to the row's state when one applies.
      const stateDescription = downloading
        ? "Downloading"
        : unviable
          ? "Too slow on this computer"
          : marginal
            ? "Runs slowly on this computer"
            : undefined;
      const description = stateDescription ?? baseDescription;
      return {
        value: m.id,
        label,
        description,
        descriptionInOptionsOnly: true,
        // Customizable names its backing model only here, behind a hover on
        // "Gemma 4" (owner review). Plain text everywhere else.
        descriptionContent:
          !stateDescription && descriptionHover && baseDescription?.includes(descriptionHover.term)
            ? (
              <DescriptionWithHover
                text={baseDescription}
                term={descriptionHover.term}
                hover={descriptionHover.hover}
              />
            )
            : undefined,
        disabled: unviable || !availableLocalModelIds.includes(m.id),
        // A too-slow model stays deletable: the speed check downloads before
        // measuring, so it would otherwise be stuck on disk.
        actionWhenDisabled: unviable && installed,
        ...(m.recommended
          ? {
              badge: { label: "Recommended", tone: "frost" as const },
              badgeInOptionsOnly: true,
              badgeInline: true,
            }
          : {}),
        leadingAdornment: (
          <ModelIdentityChip
            kind={LOCAL_MODEL_FIRST_PARTY[m.id] ? "mark" : "pen"}
            dimmed={unviable}
          />
        ),
        // ONE trailing meta per row, by priority: progress, size when a
        // download is needed, the slow dot, else nothing.
        adornment: downloading ? (
          <span className="text-[11px] text-steel tabular-nums">
            {percent != null ? `${percent}%` : null}
          </span>
        ) : !installed ? (
          <span className="text-[11px] text-steel/70 tabular-nums">{size}</span>
        ) : marginal ? (
          <span className="inline-flex items-center"><MarginalDot /></span>
        ) : undefined,
        triggerAdornment: downloading ? (
          <>
            <span className="text-[11px] text-steel tabular-nums shrink-0">
              {percent != null ? `${percent}%` : null}
            </span>
            <span
              aria-hidden="true"
              data-local-model-progress
              className="absolute left-0 bottom-0 h-[2px] transition-[width] duration-300"
              style={{
                width: `${percent ?? 0}%`,
                background: "linear-gradient(90deg, var(--color-frost), var(--color-frost-deep))",
                borderRadius: "0 2px 2px 0",
              }}
            />
          </>
        ) : !installed ? (
          <DownloadMiniIcon className="text-steel/70 shrink-0" aria-hidden="true" />
        ) : marginal ? (
          <span className="inline-flex items-center shrink-0"><MarginalDot /></span>
        ) : undefined,
        accessibleDescription: [
          installed ? "Downloaded" : `Not downloaded, ${size}`,
          descriptionHover && !stateDescription ? descriptionHover.hover : null,
        ].filter(Boolean).join(". "),
      };
    });
  }, [
    localBundledModels,
    localModelStateById,
    downloadPercent,
    availableLocalModelIds,
    currentLocalModelId,
    activeLocalModelId,
    config.enhancement_local_gpu_verdicts,
  ]);

  // A too-slow model still counts as a choice: its disabled row explains the
  // verdict and keeps its delete action reachable (design.md "Open panel rows").
  const showLocalModelSelector =
    localModelOptions.filter(
      (option) =>
        !option.disabled
        || getLocalGpuVerdict(config.enhancement_local_gpu_verdicts, option.value) === "unviable",
    ).length > 1;

  // Which config field holds the model name for the current cloud provider.
  const modelFieldForProvider: Record<string, keyof AppConfig | undefined> = {
    gemini: "enhancement_gemini_model",
    openai: "enhancement_openai_model",
    anthropic: "enhancement_anthropic_model",
    custom: "enhancement_custom_model",
  };
  const currentModelField = modelFieldForProvider[currentProvider];
  const currentModelValue = currentModelField
    ? ((config[currentModelField] as string | undefined) || "")
    : "";

  // ---- Connection block state (issue 1) ----
  // The Connection block resolves to one of three shapes:
  //   "connected"     — active cloud provider has a stored key, OR Local is
  //                      ready. Collapses to a one-line summary; click expands.
  //   "unconfigured"  — active cloud provider has NO stored key (first run).
  //                      Renders the guided setup card and FORCES expanded
  //                      until a key checks out (status.kind === "connected").
  //   "failed"        — provider has a key but the last check failed. Summary
  //                      reads "Connection failed" with a rose dot; clicking
  //                      expands to the key field.
  // Ollama is treated as "configured" (no key field); Local leans on the
  // benchmark/download affordance rather than a key.
  const connectionState: "connected" | "unconfigured" | "failed" = (() => {
    if (currentProvider === "local") return "connected";
    if (isOllama) return status.kind === "error" ? "failed" : "connected";
    if (!storedKey) return "unconfigured";
    if (status.kind === "error") return "failed";
    return "connected";
  })();

  // The model name shown to the right of the collapsed summary. The local model
  // name is never exposed to the user (they don't pick it), so local returns an
  // empty label — only the provider + top-right status describe local. Cloud
  // providers show their selected model (the user chose it), falling back to the
  // first curated model's label so the cloud summary is never blank.
  //
  // Under lockdown: if the stored provider is cloud, the collapsed summary shows
  // Local as active with the D3 "Local (Lockdown Mode)" qualifier instead of the
  // stored cloud provider, so the page never claims a cloud provider is live.
  const summaryModelLabel = (() => {
    // Surface D (Task 5.7): D3 qualifier when lockdown is on and stored provider
    // is a cloud one. currentProvider is already forced to "local" by the
    // isLockdown branch above; this adds the parenthetical so the summary reads
    // "Local (Lockdown Mode)" rather than just "Local".
    if (isLockdown && storedProvider !== "local") {
      return "(Lockdown Mode)";
    }
    const rawLabel = (() => {
      if (currentProvider === "local") {
        return "";
      }
      if (isCustom) return config.enhancement_custom_model;
      if (isOllama) return localOllamaModel;
      const match = providerModels.find((m) => m.id === currentModelValue);
      if (match) return match.label;
      if (currentModelValue) return currentModelValue;
      return "Choose a model";
    })();
    return rawLabel.split(" (")[0];
  })();

  // Curated local catalog rows; CloudModelPicker adds the saved custom IDs.
  const cloudModelOptions: DropdownOption<string>[] = [
    ...providerModels.map((m) => ({
      value: m.id,
      label: m.label,
      badges: m.recommended
        ? [{ label: "Recommended", tone: "frost" as const }]
        : [],
    })),
  ];

  // The Connection block renders expanded when the user opened it OR when the
  // active cloud provider is unconfigured (first-run setup card stays open).
  const connectionRenderedExpanded =
    connectionState === "unconfigured" || connectionExpanded || !hasRecipientConsent;

  // ---- Prompt-tester gate (issue 4) ----
  // The per-profile "Try it" tester runs the configured provider against a
  // sample. Its Run button is gated on the SAME connection signals the summary
  // row reads:
  //   "ready"        — connected cloud provider, OR Local is warm (status
  //                    local-ready). Run is enabled.
  //   "warming"      — Local provider whose model isn't ready yet (spawning /
  //                    standby / not-yet-seeded). Run disabled, "warming" hint.
  //   "disconnected" — any cloud provider that isn't connected (no key, failed,
  //                    or enhancement disabled). Run disabled, "connect" hint.
  // Ollama is treated like a connected cloud provider (no key, on-prem); it
  // only drops to "disconnected" when the last check errored.
  const testerGate: PromptTesterGate = (() => {
    if (!config.enhancement_enabled) return "disabled";
    if (!hasRecipientConsent) return "consent";
    if (currentProvider === "local") {
      return status.kind === "local-ready" ? "ready" : "warming";
    }
    if (isOllama && status.kind === "ollama-loading") return "warming";
    return connectionAdmitted && status.kind === "connected" ? "ready" : "disconnected";
  })();

  // Round 03: when the card is force-expanded (unconfigured) and a key is just
  // entered, KEEP it open instead of collapsing out from under the user mid-setup.
  // The connection check fires on key save and resolves the status pill while the
  // card stays open; the user collapses it manually when done. Fires for both
  // connected AND failed (any exit from "unconfigured"); guarded on the manual
  // override so a deliberate re-open isn't disturbed.
  const prevConnectionStateRef = useRef(connectionState);
  useEffect(() => {
    const prev = prevConnectionStateRef.current;
    prevConnectionStateRef.current = connectionState;
    if (
      prev === "unconfigured" &&
      connectionState !== "unconfigured" &&
      !connectionExpanded
    ) {
      setConnectionExpanded(true);
    }
  }, [connectionState, connectionExpanded]);

  // Read the local catalog whenever the cloud provider changes.
  useEffect(() => {
    if (currentProvider === "local" || isOllama || isCustom) {
      setProviderModels([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await getEnhancementModels(currentProvider as "gemini" | "openai" | "anthropic");
        if (cancelled) return;
        setProviderModels(r.bundled);
      } catch (err) {
        console.warn(`[Enhancement] provider model fetch failed error_type=${errorType(err)}`);
        if (!cancelled) {
          setProviderModels([]);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProvider, isOllama, isCustom]);

  const cloudPicker = (
    <CloudModelPicker
      key={currentProvider}
      value={currentModelValue}
      catalog={isCustom ? [] : cloudModelOptions}
      saved={config.enhancement_saved_models?.[currentProvider] ?? []}
      onChange={(model, saved) => {
        if (!currentModelField) return;
        onConfigChange({
          [currentModelField]: model,
          enhancement_saved_models: {
            ...config.enhancement_saved_models,
            [currentProvider]: saved,
          },
        } as Partial<AppConfig>);
      }}
    />
  );

  const inputBase =
    "w-full h-11 rounded-[var(--radius-input)] px-5 text-[13px] font-body text-slate placeholder:text-steel/40 outline-none transition-all duration-300";
  const thinking = savedConnection.thinking;
  const thinkingLabel = (level: string) => level.charAt(0).toUpperCase() + level.slice(1);
  const thinkingAdjustable = thinking?.kind === "configurable" || thinking?.kind === "mandatory";
  const preferredThinking = thinking ? config.enhancement_thinking_levels?.[thinking.preference_key] : undefined;
  const thinkingValue = thinking && preferredThinking && thinking.options.includes(preferredThinking)
    ? preferredThinking : thinking?.effective || "default";
  const thinkingHelp = thinking?.kind === "mandatory"
    ? `This model requires thinking. ${thinkingLabel(thinking.default)} is its lowest setting.`
    : thinking?.kind === "configurable"
      ? `Let the model spend extra time before answering. ${thinking.options.includes("on") ? "This may take longer." : "Higher settings may take longer."}`
      : thinking?.kind === "fixed"
        ? currentProvider === "local" ? "Fixed for this local model." : "Fixed for this model."
        : "Thinking settings are not available for this connection. The provider's default applies.";
  const inputBaseStyle: React.CSSProperties = {
    background: "var(--surface-control)",
    border: "1px solid var(--border-emphasis)",
    boxShadow: "inset 0 2px 4px rgba(0, 0, 0, 0.02)",
  };
  const onInputFocus = (e: React.FocusEvent<HTMLElement>) => {
    // Option 2: focus is the single global :focus-visible outline only. The
    // fill/shadow are NOT changed on focus -- that bg lift (0.5 -> 0.85) was the
    // "dark then lighten" flash on click. outline="" just clears any stale value.
    e.currentTarget.style.outline = "";
  };
  const onInputBlur = (e: React.FocusEvent<HTMLElement>) => {
    e.currentTarget.style.background = "var(--surface-control)";
    e.currentTarget.style.boxShadow = "inset 0 2px 4px rgba(0, 0, 0, 0.02)";
    e.currentTarget.style.borderColor = "var(--border-emphasis)";
  };

  // The API-key label + field + its "Remove key" action, bundled so removal sits
  // WITH the field wherever it lands (round 03): cloud puts the key in the top
  // row, custom in the full-width stack below — the action follows the field
  // rather than floating at the card bottom. Used by both; rendered for neither
  // local (no key) nor ollama (no key).
  const apiKeyBlock = (
    <div>
      <div className="text-[11px] text-steel font-medium mb-1">API key</div>
      <ApiKeyField
        value={localApiKey}
        storedKey={storedKey}
        fingerprint={storedKeyFingerprint}
        provider={activeProvider}
        inputBase={inputBase}
        inputBaseStyle={inputBaseStyle}
        onChange={(value) => { workspace.reset(); handleApiKeyChange(value); }}
        onBlur={handleApiKeyBlur}
        onFocus={onInputFocus}
        onBlurCapture={onInputBlur}
      />
      {localApiKey && (
        <div className="flex justify-end mt-1.5">
          <button
            onClick={() => { workspace.reset(); void handleRemoveKey(); }}
            className="text-[11px] text-steel/70 hover:text-rose transition-colors cursor-pointer"
          >
            Remove key
          </button>
        </div>
      )}
    </div>
  );

  // ONE helper line under the local model trigger, at most one action, shown
  // only when something needs the user (design.md "Helper line"). Ready or
  // viable models show nothing; launch-time hardware rechecks stay automatic.
  const localHelperLinkClass = "text-[11px] hover:underline cursor-pointer";
  const localModelAuxiliary = currentProvider === "local" ? (
    currentLocalModelIsCandidate ? (
      localModelInstalled ? (
        <p className="text-[11px] text-steel mt-1.5">
          Development candidate · Artifact available
        </p>
      ) : (
        <p className="text-[11px] text-steel mt-1.5">
          Artifact unavailable in the candidate model directory.
        </p>
      )
    ) : localModelDownloading ? (
      <p className="text-[11px] leading-4 text-steel mt-1.5 tabular-nums">
        {localDownloadProgress && localDownloadProgress.total > 0
          ? `Downloading · ${formatModelSizeShort(localDownloadProgress.downloaded / (1024 * 1024))} of ${formatModelSizeShort(currentLocalModel.disk_mb)}`
          : "Downloading…"}
      </p>
    ) : !localModelInstalled ? (
      <p className="text-[11px] leading-4 mt-1.5">
        <button
          type="button"
          onClick={() => requestLocalDownload(currentLocalModel)}
          className={localHelperLinkClass}
          style={{ color: "var(--color-frost-deep)" }}
        >
          Download to use · {formatModelSizeShort(currentLocalModel.disk_mb)}
        </button>
      </p>
    ) : localModelVerdict === "marginal" ? (
      <p className="text-[11px] leading-4 text-steel mt-1.5 flex flex-wrap items-baseline gap-x-1.5">
        <span>Runs slowly on this computer.</span>
        <button
          type="button"
          onClick={() => openBenchmarkFlow("prefilter", "recheck")}
          className={localHelperLinkClass}
          style={{ color: "var(--color-frost-deep)" }}
        >
          Check again
        </button>
      </p>
    ) : null
  ) : null;

  return (
    // pb-6 gives the last description breathing room above the scroll edge
    // (overflow-y: auto in the parent main card otherwise clips it tight).
    <div className="space-y-4 pb-6">
      {/* Page heading + top-right connection status pill. The pill lives here
          (not in the Connection block's collapsed summary) so the live
          connection state is visible without expanding the card. The summary
          row below shows only the provider + glyph + model. */}
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0, type: "spring", stiffness: 400, damping: 30 }}
      >
        <div className="flex items-center justify-between gap-3">
          <div className="text-[18px] font-medium text-slate">Enhancement</div>
          {/* Keep status text in one live region so invalidation replaces
              connection evidence immediately, without an exit-animation delay. */}
          <HoverTooltip
            content={
              status.kind !== "idle"
                ? tooltipFor(status, PROVIDER_DISPLAY[currentProvider]) ?? ""
                : ""
            }
            side="bottom-right"
          >
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: 0.15 }}
                role="status"
                aria-live="polite"
                aria-label={ariaLabelFor(status)}
                className="flex items-center gap-1.5 shrink-0 max-w-[180px]"
              >
                {status.kind !== "idle" && <motion.div
                  aria-hidden="true"
                  className={`w-1.5 h-1.5 rounded-full shrink-0 ${dotClassFor(status)}`}
                  style={
                    status.kind === "local-loading" || status.kind === "ollama-loading"
                      ? { boxShadow: "0 0 6px rgba(212, 175, 55, 0.45)" }
                      : undefined
                  }
                  animate={
                    (status.kind === "local-loading" || status.kind === "ollama-loading") && !reduceMotion
                      ? { opacity: [1, 0.4, 1] }
                      : { opacity: 1 }
                  }
                  transition={
                    (status.kind === "local-loading" || status.kind === "ollama-loading") && !reduceMotion
                      ? { duration: 1.6, repeat: Infinity, ease: "easeInOut" }
                      : { duration: 0 }
                  }
                />}
                <span
                  className={`text-[10px] font-medium uppercase tracking-wider truncate ${labelClassFor(status)}`}
                >
                  {status.kind === "error" ? "Connection failed" : labelFor(status)}
                </span>
              </motion.div>
          </HoverTooltip>
        </div>
      </motion.div>

      {/* Surface D policy banner (Task 5.7). Rendered directly under the page
          heading when lockdown is on. Calm informational state (frost, steel
          text) — not rose, not gold. Copy is a NEUTRAL, FUNCTIONAL status line
          (one line): it describes what Lockdown does to this setting — restricts
          AI cleanup to the on-device model — rather than making a data-privacy /
          HIPAA claim. That deliberately keeps it out of vetted-claim territory
          (it under-claims, which is the safe direction); the legally
          load-bearing disclaimers live elsewhere (the Home row subtitle, the 'i'
          popover's "not a HIPAA certification", the HIPAA-summary PDF) and are
          unchanged. The action label is navigation, not a claim: it reads
          "Manage" now that the "Privacy & Security" section header is gone. */}
      {isLockdown && (
        <motion.div
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.5 * 0.03, type: "spring", stiffness: 400, damping: 30 }}
        >
          <div
            className="flex items-center gap-2.5 rounded-[var(--radius-row)] px-4 py-2.5"
            style={{
              // Mirrors this file's themed cardStyle: control surface + standard
              // border, with the caught-light top rim left literal.
              background: "var(--surface-control)",
              border: "1px solid var(--border-standard)",
              boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.8 * var(--rim-scale)))",
            }}
          >
            <ShieldSealGlyph size={14} className="text-steel shrink-0" />
            <span className="text-[11px] text-steel leading-snug flex-1">
              Lockdown limits AI cleanup to the on-device model.
            </span>
            <button
              type="button"
              onClick={onNavigateHome}
              className="ml-auto text-[11px] shrink-0 hover:underline cursor-pointer"
              style={{ color: "var(--color-frost-deep)" }}
              aria-label="Manage Lockdown Mode"
            >
              Manage
            </button>
          </div>
        </motion.div>
      )}

      {!capabilityForProvider(currentProvider).enabled && (
        <div
          role="status"
          className="rounded-[var(--radius-row)] px-4 py-2.5 text-[11px] text-steel"
          style={{
            background: "var(--surface-control)",
            border: "1px solid var(--border-standard)",
          }}
        >
          {capabilityExplanation(capabilityForProvider(currentProvider))}
        </div>
      )}

      {/* (1) Enable toggle — manual inline row instead of `<Section inline>`
          because the shared inline variant uses py-3.5 (calibrated for the
          stacked-toggle layout on Audio/Controls pages). Here the toggle is
          a single header above a non-inline Provider section, so the extra
          padding read as a gap between the two. Manual row uses py-1 to
          let the toggle sit close to the provider section it gates. */}
      <motion.div
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 1 * 0.03, type: "spring", stiffness: 400, damping: 30 }}
        className="flex items-center justify-between gap-4 py-1"
      >
        <div className="min-w-0">
          <div className="text-[13px] font-medium text-slate">AI Enhancement</div>
          <div className="text-[11px] text-steel mt-0.5">Clean up dictation with an AI model.</div>
        </div>
        <div className="shrink-0">
          <Toggle
            checked={config.enhancement_enabled ?? false}
            onChange={handleEnableToggle}
            disabled={!capabilityForProvider(currentProvider).enabled}
            ariaLabel="AI Enhancement"
          />
        </div>
      </motion.div>

      {/* (2 + 3) Connection block (issue 1). A stateful collapsible card that
           replaces the always-expanded Provider / API key / Model rows:
             - connected  -> one-line summary, click to expand inline
             - unconfigured (cloud, no key) -> guided setup card, forced open
             - local -> benchmark/download affordance via the provider dropdown
           The expanded body is the existing Provider dropdown + key/Ollama
           fields + Model picker, threaded through unchanged. */}
      <ConnectionSection
        index={2}
        state={connectionState}
        provider={currentProvider}
        providerLabel={currentProviderLabel}
        summaryModelLabel={summaryModelLabel}
        reduceMotion={!!reduceMotion}
        expanded={connectionRenderedExpanded}
        onToggleExpanded={() => setConnectionExpanded((v) => !v)}
        customHost={customHost}
        ollamaEndpoint={ollamaEndpoint}
        controls={(
        <>
        <div className="grid grid-cols-[150px_1fr] gap-3 items-start">
          <div>
            <div className="text-[11px] text-steel font-medium mb-1">Provider</div>
            <ProviderDropdown
              ref={dropdownRef}
              open={providerDropdownOpen}
              setOpen={setProviderDropdownOpen}
              currentLabel={currentProviderLabel}
              currentValue={currentProvider}
              customHost={customHost}
              ollamaEndpoint={ollamaEndpoint}
              onProviderPicked={requestProviderPick}
              setStatus={setStatus}
              localModelInstalled={localModelInstalled}
              localModelManaged={currentLocalModelManaged}
              currentLocalModelId={currentLocalModelId}
              onDeleteLocalModel={handleDeleteLocalModel}
              lockdown={isLockdown}
              onDownloadLocal={() => {
                setProviderDropdownOpen(false);
                requestLocalDownload(currentLocalModel);
              }}
              disabledProviders={{
                ...(managedLocalCapability.enabled
                  ? {}
                  : { local: capabilityExplanation(managedLocalCapability) }),
                ...(cloudCapability.enabled
                  ? {}
                  : {
                      gemini: capabilityExplanation(cloudCapability),
                      openai: capabilityExplanation(cloudCapability),
                      anthropic: capabilityExplanation(cloudCapability),
                      custom: capabilityExplanation(cloudCapability),
                    }),
              }}
              providerExplanations={{
                ...(ollamaCapability.explained
                  ? { ollama: capabilityExplanation(ollamaCapability) }
                  : {}),
              }}
            />
            {/* Subtext slot under the dropdown: Cloud/Ollama show their
                PROVIDER_HELP link here. Custom's OpenAI-compatible caption is
                rendered full-width below the grid instead (too long for the
                150px Provider column). Local shows its helper here only when
                there is no model picker. */}
            {providerHelp?.url && providerHelp?.label ? (
              <p className="text-[11px] leading-relaxed mt-1.5">
                <a
                  href={providerHelp.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hover:underline"
                  style={{ color: "var(--color-frost-deep)" }}
                >
                  {providerHelp.label} →
                </a>
              </p>
            ) : null}
            {!showLocalModelSelector && localModelAuxiliary}
          </div>
          {/* Right column: the provider's PRIMARY connection field. The 1fr cell
              is always reserved so the dropdown width never shifts across
              providers. Cloud's API key here keeps cloud visually unchanged. */}
          <div>
            {isCustom && (
              <div>
                <div className="text-[11px] text-steel font-medium mb-1">Base URL</div>
                <input
                  type="text"
                  value={localCustomBaseUrl}
                  onChange={(e) => handleCustomBaseUrlChange(e.target.value)}
                  onBlur={() => {
                    if (customDebounceRef.current) clearTimeout(customDebounceRef.current);
                    onConfigChange({ enhancement_custom_base_url: localCustomBaseUrl });
                  }}
                  placeholder="https://openrouter.ai/api/v1"
                  aria-label="Base URL"
                  className={inputBase}
                  style={inputBaseStyle}
                  onFocus={onInputFocus}
                  onBlurCapture={onInputBlur}
                />
              </div>
            )}
            {isOllama && (
              <div>
                <div className="text-[11px] text-steel font-medium mb-1">Endpoint</div>
                <input
                  type="text"
                  value={localOllamaEndpoint}
                  onChange={(e) => handleOllamaEndpointChange(e.target.value)}
                  onBlur={() => {
                    if (ollamaDebounceRef.current) clearTimeout(ollamaDebounceRef.current);
                    onConfigChange({ enhancement_ollama_endpoint: localOllamaEndpoint });
                  }}
                  placeholder="http://localhost:11434"
                  aria-label="Endpoint"
                  className={inputBase}
                  style={inputBaseStyle}
                  onFocus={onInputFocus}
                  onBlurCapture={onInputBlur}
                />
              </div>
            )}
            {!isCustom && !isOllama && currentProvider !== "local" && apiKeyBlock}
            {/* Local: the model picker is the primary field (design.md,
                Stream-1 Mini), built on the shared `Dropdown`: a one-line
                trigger, two-line rows in the panel only, and one
                keyboard-reachable download/delete action per row. With a
                single choice the cell stays empty and the helper line sits
                under Provider instead. */}
            {currentProvider === "local" && showLocalModelSelector && (
              <div ref={localPickerRef}>
                <div className="text-[11px] text-steel font-medium mb-1">Model</div>
                <Dropdown
                  value={currentLocalModelId}
                  options={localModelOptions}
                  onChange={handleLocalModelSelect}
                  ariaLabel="Local model"
                  triggerClassName="w-full relative overflow-hidden"
                  optionActionsFocusable
                  openRequest={localPickerOpenRequest}
                  // Two public rows need room for the Gemma and delete hover bubbles,
                  // so the short list may overflow its panel. Developer candidate
                  // registries can grow, so they keep internal scrolling.
                  panelOverflow={localBundledModels.some(model => model.source === "candidate") ? "auto" : "visible"}
                  renderOptionAction={(opt) => {
                    const model = localBundledModels.find((m) => m.id === opt.value);
                    if (
                      !model
                      || model.source === "candidate"
                      || model.downloadable === false
                    ) return null;
                    return (
                      <ModelRowActions
                        modelId={opt.value}
                        modelLabel={localModelDownloadLabel(opt.value)}
                        sizeLabel={formatModelSizeShort(model.disk_mb)}
                        installed={localModelStateById[opt.value]?.installed ?? false}
                        downloading={localModelStateById[opt.value]?.downloading ?? false}
                        onDownload={() => requestLocalDownload(model)}
                        onDelete={handleDeleteLocalModel}
                      />
                    );
                  }}
                />
                {localModelAuxiliary}
              </div>
            )}
          </div>
        </div>

        {(isCustom || isOllama) && <p className="text-[11px] text-steel leading-relaxed mt-1.5">
          Use HTTPS for remote servers. HTTP is allowed only for a server on this device.
        </p>}
        {isOllama && <p className="text-[11px] text-steel leading-relaxed mt-1.5">
          {isLocalOllamaEndpoint(localOllamaEndpoint) ? "Server on this device; may use cloud models" : "Processing depends on the server and model"}
        </p>}

        {/* Custom: the OpenAI-compatible helper spans the FULL width under the
            Provider + Base URL row, so it fits the longer caption (it was being
            trimmed in the narrow 150px Provider column). */}
        {isCustom && (
          <p className="text-[11px] text-steel leading-relaxed mt-1.5">
            <a
              href="https://platform.openai.com/docs/api-reference/chat"
              target="_blank"
              rel="noopener noreferrer"
              className="hover:underline"
              style={{ color: "var(--color-frost-deep)" }}
            >
              OpenAI-compatible Chat Completions
            </a>
          </p>
        )}

        {/* Full-width fields below the grid (round 03). Ollama and Custom shed
            their old cramped narrow-column stacks: every field beyond the primary
            one stretches the full content width (matching cloud's Model picker).
            Cloud is unchanged (its API key stays in the grid; its Model <select>
            is already the full-width block below). */}
        {isOllama && (
          <div className="mt-2">
            <div className="text-[11px] text-steel font-medium mb-1">Model</div>
            <input
              type="text"
              value={localOllamaModel}
              onChange={(e) => handleOllamaModelChange(e.target.value)}
              onBlur={() => {
                if (ollamaDebounceRef.current) clearTimeout(ollamaDebounceRef.current);
                onConfigChange({ enhancement_ollama_model: localOllamaModel });
              }}
              placeholder="llama3.2"
              aria-label="Model"
              className={inputBase}
              style={inputBaseStyle}
              onFocus={onInputFocus}
              onBlurCapture={onInputBlur}
            />
          </div>
        )}
        {isCustom && (
          <div className="space-y-2 mt-2">
            <div>
              <div className="text-[11px] text-steel font-medium mb-1">Model</div>
              {cloudPicker}
            </div>
            {apiKeyBlock}
          </div>
        )}

        {/* Round 03: the per-provider help link now lives under the dropdown (the
            standardized subtext slot above); "Remove key" is its own action row at
            the very bottom of the controls (below the Model picker). */}

      {/* Cloud model picker — conditionally rendered for non-Ollama, non-local.
           Now part of the Connection block's expanded body (issue 1). */}
      {!isOllama && !isCustom && currentProvider !== "local" && (
        <div className="mt-3">
          <div className="text-[11px] text-steel font-medium mb-2">Model</div>
          {cloudPicker}
          {/* Privacy note moved to the provider-selector icon tooltip (round 04). */}
        </div>
      )}

      {workspace.visible && (
        <div className="mt-3" ref={workspaceRef}>
          <label htmlFor="enhancement-workspace" className="block text-[11px] text-steel font-medium mb-1">Workspace ID</label>
          <input
            id="enhancement-workspace"
            type="text"
            value={workspace.draft}
            onChange={(event) => workspace.change(event.target.value)}
            onBlur={() => workspace.flush()}
            onKeyDown={(event) => { if (event.key === "Enter") workspace.flush(); }}
            onFocus={onInputFocus}
            onBlurCapture={onInputBlur}
            placeholder="Paste Workspace ID"
            autoComplete="off"
            spellCheck={false}
            aria-describedby={`enhancement-workspace-help${workspaceError ? " enhancement-workspace-error" : ""}`}
            aria-invalid={workspaceError ? true : undefined}
            className={inputBase}
            style={inputBaseStyle}
          />
          <div className="flex items-start justify-between gap-3 mt-1.5">
            <p id="enhancement-workspace-help" className="text-[11px] text-steel leading-relaxed">
              Find it in Anthropic Console. A workspace-scoped API key does not need this field.
            </p>
            <button type="button" aria-label="Clear Workspace ID" onClick={workspace.clear}
              disabled={!workspace.draft && !config.enhancement_anthropic_workspace_id}
              className="text-[11px] text-steel/70 hover:text-rose transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-default shrink-0">
              Clear
            </button>
          </div>
          {workspaceError && <p id="enhancement-workspace-error" className="text-[11px] text-rose leading-relaxed mt-1.5">{workspaceError}</p>}
        </div>
      )}

        {/* "Remove key" is co-located with the API key field (round 03): it lives
            inside `apiKeyBlock`, directly under the field, for both cloud and
            custom. No floating bottom row. */}
        </>
        )}
      />

      {currentProvider !== "local" && <div className="space-y-2">
        {!hasRecipientConsent && <div className="flex items-center justify-between gap-3">
          <p className={`text-[12px] leading-relaxed ${recipientError ? "text-rose" : "text-steel"}`} role={recipientError ? "alert" : "status"}>
            {recipientError ?? "Connection settings changed. Review the privacy notice before sending text."}
          </p>
          <SecondaryButton onClick={reviewCurrentRecipient} className="shrink-0 text-[12px] px-4 py-2">Review privacy notice</SecondaryButton>
        </div>}
        <p className="text-[11px] text-steel">Checks use fixed test text, not your transcripts or profile instructions.</p>
        {!config.enhancement_enabled && <p className="text-[11px] text-steel">Turn on Enhancement to test prompts or check the connection.</p>}
      </div>}

      <Section index={4} title="">
        <ProfilesSection
          config={config}
          onConfigChange={onConfigChange}
          subscribe={subscribe}
          send={send}
          connected={connected}
          advancedCapabilities={advancedCapabilities}
          testerGate={testerGate}
          activeModelCapabilities={activeModelCapabilities}
          activeModelLabel={LOCAL_MODEL_DISPLAY_LABEL[currentLocalModel.id] ?? currentLocalModel.label}
          activeProvider={activeProvider}
          reduceMotion={!!reduceMotion}
          onChangeModel={openLocalModelPicker}
        />
      </Section>

      {/* (7) Advanced disclosure. Safeguards, timeout and chunking apply
           to every provider. */}
      <AdvancedDisclosure open={advancedOpen} onToggle={() => setAdvancedOpen((v) => !v)}>
              <div className="mt-3">
                <Section
                  index={0}
                  title="Thinking"
                  description={<>{thinkingHelp}{thinkingAdjustable && <span className="block mt-1">Saved for this model across all profiles.</span>}</>}
                  descriptionId="enhancement-thinking-help"
                  inline
                >
                  {thinkingAdjustable && thinking ? (
                    <Dropdown
                      value={thinkingValue}
                      options={thinking.options.map((value) => ({
                        value,
                        label: thinkingLabel(value),
                        ...(value === thinking.default ? { badge: { label: "Default" }, badgeInOptionsOnly: true } : {}),
                      }))}
                      ariaLabel="Thinking"
                      ariaDescribedBy="enhancement-thinking-help"
                      triggerClassName="w-[112px]"
                      panelWidth={168}
                      onChange={(value) => onConfigChange({ enhancement_thinking_levels: {
                        ...config.enhancement_thinking_levels,
                        [thinking.preference_key]: value,
                      } })}
                    />
                  ) : (
                    <span aria-describedby="enhancement-thinking-help" className="text-[12px] text-steel">
                      {thinking?.kind === "fixed" ? thinkingLabel(thinking.effective) : "Provider default"}
                    </span>
                  )}
                </Section>
                <Section
                  index={1}
                  title="Enhancement timeout"
                  description="How long Whisperstream waits for each enhancement request before keeping the original text. Increase this for slower models."
                  descriptionId="enhancement-timeout-help"
                  inline
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={1}
                      max={300}
                      step={1}
                      value={config.enhancement_timeout_seconds ?? 5}
                      aria-label="Enhancement timeout in seconds"
                      aria-describedby="enhancement-timeout-help"
                      onChange={(event) => {
                        const value = event.currentTarget.valueAsNumber;
                        if (Number.isFinite(value)) {
                          onConfigChange({ enhancement_timeout_seconds: value });
                        }
                      }}
                      onFocus={onInputFocus}
                      onBlur={onInputBlur}
                      className={`${inputBase} w-20 px-3 text-right`}
                      style={inputBaseStyle}
                    />
                    <span className="text-[11px] text-steel">seconds</span>
                  </div>
                </Section>

                <Section
                  index={1}
                  title="Output safeguards"
                  description={currentProvider === "local"
                    ? "Repair unambiguous digit errors and use the raw transcript when output checks fail. Turn off to use the model response without these checks."
                    : "If the model returns a refusal, repetition loop, or runaway response, fall back to the raw transcript."}
                  descriptionId="enhancement-safeguards-help"
                  inline
                  last
                >
                  <Toggle
                    ariaLabel="Output safeguards"
                    ariaDescribedBy="enhancement-safeguards-help"
                    checked={config.enhancement_safeguards_enabled ?? true}
                    onChange={(next) => onConfigChange({ enhancement_safeguards_enabled: next })}
                  />
                </Section>

                {/* Chunked cleanup (round 04) — default on; applies to any
                    enhancement provider, so it's shown regardless of provider. */}
                <SettingsGroup title="Chunking" entranceDelay={3 * 0.03}>
                  <Section
                    index={3}
                    title="Clean in chunks"
                    description="Split longer dictations so cleaned text starts appearing sooner."
                    inline
                    last
                  >
                    <Toggle
                      checked={config.enhancement_chunked_cleanup ?? true}
                      onChange={(next) => onConfigChange({ enhancement_chunked_cleanup: next })}
                      ariaLabel="Clean in chunks"
                    />
                  </Section>

                  <Section
                    index={4}
                    title="Words per chunk"
                    description="Dictations longer than this are split. Later chunks may vary slightly to keep sentences together."
                    descriptionId="enhancement-words-per-chunk-help"
                    inline
                    last
                    disabled={!(config.enhancement_chunked_cleanup ?? true)}
                  >
                    <input
                      type="number"
                      min={20}
                      max={500}
                      step={1}
                      value={config.enhancement_words_per_chunk ?? 120}
                      aria-label="Words per chunk"
                      aria-describedby="enhancement-words-per-chunk-help"
                      disabled={!(config.enhancement_chunked_cleanup ?? true)}
                      onChange={(event) => {
                        const value = event.currentTarget.valueAsNumber;
                        if (Number.isFinite(value)) {
                          onConfigChange({ enhancement_words_per_chunk: value });
                        }
                      }}
                      onFocus={onInputFocus}
                      onBlur={onInputBlur}
                      className={`${inputBase} w-20 px-3 text-right disabled:cursor-not-allowed`}
                      style={inputBaseStyle}
                    />
                  </Section>

                  <Section
                    index={5}
                    title="Limit first chunk to 60 words"
                    description="Helps the first cleaned text appear sooner when later chunks are larger."
                    inline
                    last
                    disabled={!(config.enhancement_chunked_cleanup ?? true)}
                  >
                    <Toggle
                      checked={config.enhancement_limit_first_chunk ?? true}
                      onChange={(next) => onConfigChange({ enhancement_limit_first_chunk: next })}
                      disabled={!(config.enhancement_chunked_cleanup ?? true)}
                      ariaLabel="Limit first chunk to 60 words"
                    />
                  </Section>
                </SettingsGroup>
              </div>
      </AdvancedDisclosure>

      {/* Download dialog for local GGUF model — conditionally rendered */}
      {downloadingModel && (
        <DownloadDialog
          title="Downloading your on-device AI"
          subtitle={`Cleanup runs on this computer. About ${downloadingModel.disk_mb} MB.`}
          subscribe={(handler) =>
            subscribeToDownloadEvents(subscribe, handler as Parameters<typeof subscribeToDownloadEvents>[1], "local_llm")
          }
          startFn={() => startLocalLLMDownload(downloadingModel.id)}
          cancelFn={cancelLocalLLMDownload}
          onComplete={handleLocalDownloadComplete}
          onCancel={handleLocalDownloadCancel}
        />
      )}

      {/* Low-disk hard block — gates the Download button when the local-LLM
          cache directory has insufficient free space for the model GGUF.
          Different from LowRamDialog: this is NOT a soft warning ("Download
          anyway" doesn't exist) because the download would physically fail
          mid-stream. The user clears space and re-clicks Download. */}
      <LowDiskSpaceDialog
        open={lowDiskCandidate !== null}
        freeDiskGb={
          lowDiskCandidate
            ? Math.round((lowDiskCandidate.freeMb / 1024) * 10) / 10
            : 0
        }
        requiredDiskGb={
          lowDiskCandidate
            ? Math.ceil(lowDiskCandidate.requiredMb / 1024)
            : 3
        }
        onChooseFolder={handleLowDiskChooseFolder}
        onSwitchToCloud={handleLowDiskSwitchToCloud}
        onDismiss={handleLowDiskDismiss}
        showCloudOption={true}
      />

      {/* Low-RAM warning — gates the Download button when system RAM is
          insufficient. Only the user's own system RAM appears in the body
          (rounded to 1 dp); the model's RAM requirement is intentionally
          omitted so the copy stays vague. */}
      <LowRamDialog
        open={lowRamCandidate !== null}
        systemRamGb={Math.round(((systemRamMb ?? 0) / 1024) * 10) / 10}
        onSwitchToCloud={handleLowRamSwitchToCloud}
        onContinue={handleLowRamContinue}
        onDismiss={handleLowRamDismiss}
      />

      {/* Data-leaves warning — friendly heads-up that precedes the binding
          PrivacyModal whenever the user picks a non-local provider for the
          first time this session. */}
      <DataLeavesDialog
        open={dataLeavesCandidate !== null}
        provider={dataLeavesCandidate?.provider ?? "gemini"}
        customHost={customHost}
        onContinue={handleDataLeavesContinue}
        onCancel={handleDataLeavesCancel}
      />

      {/* GPU benchmark flow — local-gpu-benchmark Task 3.3. The single
          evolving modal (intro -> prefilter -> download -> benchmark ->
          success | failure) that replaces the old EnableLocalConfirmDialog
          seat. Mounted when the user enables AI Enhancement while
          provider=local and the verdict has not yet run (opens at "intro"),
          or from the re-check link (opens at "prefilter"). onCancel writes no
          config; onVerdict issues the single combined patch. Success keeps
          local; measured failure follows the snapshotted fallback policy. */}
      {benchmarkFlow && (
        <BenchmarkFlow
          currentCleanupEnabled={config.enhancement_enabled}
          startPhase={benchmarkFlow.start}
          mode={benchmarkFlow.mode}
          benchMode={benchmarkFlow.benchMode}
          benchModelId={benchmarkFlow.benchModelId}
          benchModelSize={(() => {
            const model = localBundledModels.find((m) => m.id === benchmarkFlow.benchModelId);
            return model && model.disk_mb > 0 ? formatModelSizeShort(model.disk_mb) : undefined;
          })()}
          failureFallback={benchmarkFlow.failureFallback}
          applyModelOnSuccess={benchmarkFlow.applyModelOnSuccess}
          currentModelId={currentLocalModelId}
          subscribe={subscribe}
          onCancel={() => setBenchmarkFlow(null)}
          onVerdict={handleBenchmarkVerdict}
        />
      )}

      {/* Manual "switch up to a slow fine-tune" consent (round 01, Issue 3).
          Keyed on modelId+variant so each open is a fresh instance (fresh phase
          state + bench-fired guard). cached = static caution receipt; fresh =
          runs a single-model bench, then consent / apply / block. */}
      {slowConsent && (
        <SlowModelConsentDialog
          key={`${slowConsent.modelId}-${slowConsent.variant}`}
          open
          modelId={slowConsent.modelId}
          variant={slowConsent.variant}
          subscribe={subscribe}
          onUse={handleSlowConsentUse}
          onKeep={() => setSlowConsent(null)}
        />
      )}

      {/* First-visit local-cleanup nudge (Feature #2) — one-time in-page modal
          for a pre-existing cloud user. "Check my hardware" opens the same
          BenchmarkFlow at "intro". */}
      {showLocalNudge && (
        <LocalNudgeDialog
          onDismiss={dismissLocalNudge}
          onCheck={acceptLocalNudge}
          loading={!localModelCatalogLoaded}
          unavailable={localModelCatalogLoaded && !localBundledModels.some((model) =>
            model.source !== "candidate" && model.selectable !== false
            && model.downloadable !== false && availableLocalModelIds.includes(model.id)
          )}
        />
      )}

    </div>
  );
}

/* ---------- Per-profile "Try it" prompt tester (issue 4) ----------
 * Extracted to components/PromptTester.tsx (round 03) so ProfileEditorModal can
 * render it without a page <-> component circular import. Re-exported here so
 * the existing `from "./Enhancement"` test import keeps working. Behavior
 * unchanged. */
export { PromptTester } from "../components/PromptTester";

/* ---------- Local picker description with one hover phrase ----------
 * Customizable's panel line ("Write your own prompts. Built on Gemma 4.")
 * carries the backing-model identity behind a dotted hover on "Gemma 4"
 * (owner review, October 19, 2026). The same sentence also rides on the row's
 * `accessibleDescription`, so keyboard and screen-reader users get it without
 * a focus stop inside the option button. */
function DescriptionWithHover({ text, term, hover }: { text: string; term: string; hover: string }) {
  const at = text.indexOf(term);
  // The bubble lives inside the option button, so it is aria-hidden: the same
  // sentence is the row's accessible description, never part of its name.
  return (
    <>
      {text.slice(0, at)}
      <span className="group/hover relative inline">
        <span
          className="cursor-help"
          style={{ borderBottom: "1px dotted currentColor" }}
          data-description-hover
        >
          {term}
        </span>
        <span
          aria-hidden="true"
          className="pointer-events-none absolute left-0 top-full mt-1.5 w-56 rounded-xl px-3 py-2 text-[11px] leading-relaxed font-normal text-slate opacity-0 group-hover/hover:opacity-100 transition-opacity duration-150 z-30"
          style={{
            background: "var(--surface-overlay)",
            backdropFilter: "blur(24px)",
            WebkitBackdropFilter: "blur(24px)",
            border: "1px solid var(--border-emphasis)",
            boxShadow: "var(--shadow-glass)",
          }}
        >
          {hover}
        </span>
      </span>
      {text.slice(at + term.length)}
    </>
  );
}

/* ---------- Section component (identical pattern to Audio.tsx) ---------- */
function Section({
  index,
  title,
  description,
  children,
  inline,
  last,
  disabled,
  descriptionId,
}: {
  index: number;
  title: string;
  description?: React.ReactNode;
  children?: React.ReactNode;
  inline?: boolean;
  last?: boolean;
  disabled?: boolean;
  descriptionId?: string;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: inline ? 6 : 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        delay: index * 0.03,
        type: "spring",
        stiffness: 400,
        damping: 30,
      }}
      aria-disabled={disabled || undefined}
      className={disabled ? "opacity-50 transition-opacity" : undefined}
    >
      {inline ? (
        <>
          <div className="flex items-center justify-between py-3 gap-4">
            <div className="min-w-0">
              <div className="text-[13px] font-medium text-slate">{title}</div>
              {description && <div id={descriptionId} className="text-[11px] text-steel mt-0.5">{description}</div>}
            </div>
            <div className="shrink-0">{children}</div>
          </div>
          {!last && <div className="h-px" style={{ background: "var(--divider)" }} />}
        </>
      ) : (
        <>
          {title && (
            <div className="text-[13px] font-medium text-slate mb-2">{title}</div>
          )}
          {description && (
            <div id={descriptionId} className="text-[11px] text-steel mb-3">{description}</div>
          )}
          {children}
        </>
      )}
    </motion.div>
  );
}
