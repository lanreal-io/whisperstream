import { useCallback, useState, useEffect, useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import { motion, useReducedMotion } from "framer-motion";
import { Toggle } from "../components/Toggle";
import { Dropdown } from "../components/Dropdown";
import { AdvancedDisclosure } from "../components/AdvancedDisclosure";
import { DurationSelect } from "../components/DurationSelect";
import { Badge } from "../components/Badge";
import { HoverTooltip } from "../components/HoverTooltip";
import { SecondaryButton } from "../components/SecondaryButton";
import { PermissionsSummary } from "../components/PermissionsSummary";
import {
  SttModelPicker,
  STT_MODEL_LABELS,
  type SttModelPickerOption,
} from "../components/SttModelPicker";
import { DeleteSttModelDialog } from "../components/DeleteSttModelDialog";
import { DeleteUnusedSttModelsDialog } from "../components/DeleteUnusedSttModelsDialog";
import { LockdownSetupModal, LOCKDOWN_ACK_VERSION } from "../components/LockdownSetupModal";
import { LockdownDisableDialog } from "../components/LockdownDisableDialog";
import { LockdownManageDialog } from "../components/LockdownManageDialog";
import { ChangePasswordDialog, ViewRecoveryCodeDialog } from "../components/ChangePasswordDialog";
import { RelocateModelsDialog } from "../components/RelocateModelsDialog";
import { BASE } from "../lib/api/client";
import { startRegisteredRelocate, deleteSttModel, deleteUnusedSttModels, getSttStatus } from "../lib/api/models";
import { chooseMacosModelStorage, releaseMacosNativeSelections } from "../lib/nativeFileWorkflows";
import type { AppConfig } from "../lib/api/config";
import type { ExactSttStatus, SttArtifactId } from "../lib/api/models";
import { formatModelBytes } from "../lib/format";
import type { AppEvent } from "../lib/socket/events";
import { errorType } from "../lib/logCapture";
import type { LockStatus } from "../lib/useLockdown";
import type { LaunchAtLoginStatus } from "../lib/autostart";
import {
  capabilityDecision,
  capabilityExplanation,
  type DailyUseCapabilityReport,
} from "../lib/dailyUseCapabilities";
import {
  ALL_LANGUAGES,
  getFamilyFromModel,
  getPickerModelsForLanguage,
  normalizeSelectionMode,
  resolveActiveSttArtifact,
  sttArtifactBackend,
  sttArtifactForBackendChoice,
  sttArtifactFromRegistryId,
  sttArtifactRegistryId,
  type ModelFamily,
  type SttBackend,
} from "../lib/languages";

interface Stats {
  total_transcriptions: number;
  total_words: number;
  total_recording_ms: number;
}

const THEME_OPTIONS: { value: string; label: string }[] = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

// Locked deletion-failure copy (copy-review.md -> Targeted deletion). The
// backend's refusal reason rides in the thrown Error's message; only the two
// named refusals get their own recovery step, and everything else falls back to
// the general form rather than guessing at a cause.
function sttDeleteFailureMessage(detail: string, modelName: string): string {
  if (detail.includes("dictation_active")) {
    return "Finish your current dictation, then delete the model.";
  }
  if (detail.includes("operation_busy")) {
    return "Whisperstream is already updating a model. Try again when it finishes.";
  }
  return `Could not delete ${modelName}. Try again.`;
}

// Locked bulk-deletion failure copy (copy-review.md -> Bulk unused-model
// deletion). Same two named refusals as the targeted path, but the general form
// says "all" because a bulk call can legitimately remove some artifacts and
// fail on another.
function sttBulkDeleteFailureMessage(detail: string): string {
  if (detail.includes("dictation_active")) {
    return "Finish your current dictation, then delete unused models.";
  }
  if (detail.includes("operation_busy")) {
    return "Whisperstream is already updating a model. Try again when it finishes.";
  }
  return "Could not delete all unused models. Try again.";
}

/** Friendly storage names for the EXACT artifacts, used only by the bulk
 *  deletion confirmation.
 *
 *  Everywhere else Parakeet is one user-facing model (`STT_MODEL_LABELS`) and
 *  the v2/v3 split is an implementation detail. A bulk delete is the one place
 *  that has to name what is on disk, because both Parakeet artifacts can be
 *  stored at once and the user has never been shown a difference between them.
 *  The qualifiers describe what each one is FOR, not which version it is. */
const STT_ARTIFACT_STORAGE_LABELS: Record<SttArtifactId, string> = {
  "nemo-parakeet-tdt-0.6b-v2": "NVIDIA Parakeet (English)",
  "nemo-parakeet-tdt-0.6b-v3": "NVIDIA Parakeet (Multilingual)",
  "qwen3-asr-0.6b-int8": "Qwen3 ASR",
  "cohere-transcribe-03-2026-int8": "Cohere Transcribe",
};

function formatDuration(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

interface HomeProps {
  config: AppConfig;
  startupStatus: LaunchAtLoginStatus;
  onStartupChange: (enabled: boolean) => void;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  /** Open the language picker. App owns the popup so it escapes the
   *  <main> backdrop-blur containing block. */
  onRequestOpenLanguagePopup: () => void;
  /** Reserved hook point for direct language changes from Home (currently
   *  unused — picker selections route through LanguagePopup -> App). */
  onRequestLanguageChange: (code: string) => void;
  /** True while a family-crossover download is mid-flight. The badge keeps
   *  showing the OLD family until this returns to false (design.md §5). It is
   *  also the "a model operation owns the recognizer" signal that disables the
   *  language, mode, and model controls for the length of a transition. */
  downloadInFlight: boolean;
  /** User-facing name of the model being activated, while one is being
   *  activated. Present-artifact activations have no transfer to show, so this
   *  is the only place the user learns what is happening. */
  switchingToModel?: string | null;
  /** Bumped by App whenever a model operation settles, so the authoritative
   *  artifact inventory (which artifacts exist, and which one is LOADED) is
   *  re-read even when the routing fields end up back where they started, as
   *  they do after a rollback. */
  sttInventoryRevision?: number;
  /** The Advanced `Choose speech model manually` toggle changed. App resolves
   *  the mode + exact artifact and runs the one activation path; Home never
   *  writes routing fields itself. */
  onSelectionModeChange: (manual: boolean) => void;
  /** A Manual picker choice. App resolves which exact artifact the choice
   *  lands on and activates it. */
  onSttBackendChange: (backend: SttBackend) => void;
  /** Transient status toast (Sidebar). Used for deletion outcomes, which have
   *  no other surface once the confirmation closes. */
  onShowStatus: (message: string, isError: boolean) => void;
  /** Live lock status from the backend (useLockdown hook in App.tsx). Used by
   *  the Privacy & Security section to show/hide lockdown sub-controls and
   *  wire the action buttons. Optional so Home renders without it during
   *  early-mount before the hook fires. */
  lockStatus?: LockStatus;
  /** Re-fetch /api/config after a relocate persists a new model_storage_dir
   *  out-of-band, so the storage row shows the new path and the next config
   *  save doesn't PUT a stale (empty) value over it. */
  onConfigReload: () => void;
  /** REPLACE the whole in-memory config with a server-authoritative one. The
   *  lockdown enable/disable endpoints return the sanitized config; the client
   *  adopts it verbatim (no follow-up PUT) so provider/enhancement coercion
   *  can never be clobbered by a stale echo. */
  onConfigReplace: (config: AppConfig) => void;
  /** Bumped by App when another page (Transcripts, F5) needs the user to set a
   *  transcript password under a no-envelope lockdown. Each increment
   *  auto-opens the Lockdown setup modal (the same flow "Start saving
   *  transcripts" in Manage opens) so the user lands here with it already up.
   *  0/undefined = no pending request. */
  openLockdownSetupSignal?: number;
  capabilities: DailyUseCapabilityReport;
  connected?: boolean;
}

export function Home({
  config,
  startupStatus,
  onStartupChange,
  onConfigChange,
  subscribe,
  onRequestOpenLanguagePopup,
  downloadInFlight,
  switchingToModel,
  sttInventoryRevision = 0,
  onSelectionModeChange,
  onSttBackendChange,
  onShowStatus,
  lockStatus,
  onConfigReload,
  onConfigReplace,
  openLockdownSetupSignal,
  capabilities,
  connected = true,
}: HomeProps) {
  // Whether an administrator machine policy owns Lockdown Mode on this device.
  // Managed authority removes only the user's ability to turn Lockdown OFF; it
  // never changes what Lockdown itself explains on the dependent settings.
  const isManagedLockdown = lockStatus?.managed ?? false;
  // Derived lock state for the Lockdown row (Surface A). Managed policy is the
  // higher authority, so the control reads ON even if a config echo lags.
  const isLockdownEnabled = (config.lockdown_mode ?? false) || isManagedLockdown;
  // Whether the setup modal opens at Step 1 (fresh enable via the master toggle)
  // or deep-links to the transcripts step ("start saving" while already locked
  // down). Set by whichever trigger opens the modal, read by the modal.
  const [setupStartStep, setSetupStartStep] = useState<"ack" | "transcripts">(
    "ack",
  );
  // Whether Step 1 was already accepted for the current acknowledgment copy.
  const ackAccepted =
    (lockStatus?.acknowledgedVersion ?? null) === LOCKDOWN_ACK_VERSION;
  // onLockRefresh re-fetches backend lock status after dialog actions.
  const onLockRefresh = lockStatus?.refresh;
  // Envelope presence picks the full (keep-history) vs slim (no-envelope /
  // history-off) Manage + disable variant. Sourced from the backend status.
  const hasEnvelope = lockStatus?.hasEnvelope ?? false;

  // Dialog open state. The master Toggle does NOT write config directly
  // (Decision 5): ON opens the Setup modal, OFF opens the Disable dialog.
  const [setupOpen, setSetupOpen] = useState(false);
  const [disableOpen, setDisableOpen] = useState(false);
  const [manageOpen, setManageOpen] = useState(false);
  const [changePwOpen, setChangePwOpen] = useState(false);
  const [viewCodeOpen, setViewCodeOpen] = useState(false);

  // F5: an increment from Transcripts (no-envelope lockdown, "Save
  // transcripts" blocked) auto-opens the same setup modal "Start saving
  // transcripts" uses. Skip the initial falsy value so mounting here never
  // opens it uninvited.
  useEffect(() => {
    if (openLockdownSetupSignal) {
      // "Start saving transcripts" under a no-envelope lockdown: deep-link to the
      // transcripts step with Keep pre-selected instead of Step 1.
      setSetupStartStep("transcripts");
      setSetupOpen(true);
    }
  }, [openLockdownSetupSignal]);

  const modelLifecycle = capabilityDecision(capabilities, "model-lifecycle");
  const qwenStt = capabilityDecision(capabilities, "qwen-stt");
  const launchAtLogin = capabilityDecision(capabilities, "launch-at-login");
  const startupEnabled = startupStatus.state === "enabled";
  const startupDisabled =
    !launchAtLogin.enabled ||
    !(
      ["enabled", "disabled"].includes(startupStatus.state) ||
      (capabilities.platform === "Darwin" && startupStatus.state === "not-found")
    );
  const startupDescription = (() => {
    if (!launchAtLogin.enabled) return capabilityExplanation(launchAtLogin);
    switch (startupStatus.state) {
      case "requires-approval":
        return "Approval is required in System Settings before startup can be enabled.";
      case "not-found":
        return capabilities.platform === "Darwin"
          ? "Turn on to register Whisperstream for startup."
          : "The login item is not available in this app installation.";
      case "unsupported":
        return "Run on Startup is not supported on this system.";
      case "unavailable":
        return "Startup status is unavailable right now.";
      default:
        return "Start Whisperstream automatically when you log in";
    }
  })();
  const privateMacStorage = capabilities.platform === "Darwin";
  const [stats, setStats] = useState<Stats | null>(null);
  const inventoryRequestEpoch = useRef(0);
  const inventoryMounted = useRef(true);
  useEffect(() => {
    inventoryMounted.current = true;
    return () => {
      inventoryMounted.current = false;
      inventoryRequestEpoch.current += 1;
    };
  }, []);
  const [liveInventoryRevision, setLiveInventoryRevision] = useState(0);
  const [statsRevision, setStatsRevision] = useState(0);

  useEffect(() => subscribe((event) => {
    // A recognizer swap after a background download (onboarding, a language
    // change) reports only download.complete, not model.loaded.
    if (
      event.type === "model.loaded" ||
      event.type === "model.load_failed" ||
      event.type === "stt.failure" ||
      event.type === "download.complete"
    ) {
      setLiveInventoryRevision((revision) => revision + 1);
    }
    if (event.type === "transcription.result") {
      setStatsRevision((revision) => revision + 1);
    }
  }), [subscribe]);

  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [displayedFamily, setDisplayedFamily] = useState<ModelFamily>(
    getFamilyFromModel(config.parakeet_model),
  );

  // ── Model storage location ──────────────────────────────────────────────────
  // Destination drive chosen via Browse or Reset to default. Non-null = dialog
  // is open. Null = dialog is closed / not yet triggered.
  const [relocateDest, setRelocateDest] = useState<string | null>(null);
  const [nativeStoragePickerError, setNativeStoragePickerError] = useState(false);

  // True when the user has set a custom storage directory.
  const isCustomDir = !!config.model_storage_dir;

  // Path shown in the row field: custom dir when set, else the default data dir
  // (%LOCALAPPDATA%\Whisperstream) returned by the backend in config.data_dir.
  const displayPath = privateMacStorage
    ? "Managed by Whisperstream"
    : config.model_storage_dir || config.data_dir || "";

  // The default data dir — destination for "Reset to default" (itself a move).
  const defaultDir = config.data_dir || "";

  const browseForStorageFolder = async () => {
    if (privateMacStorage) {
      setNativeStoragePickerError(false);
      const result = await chooseMacosModelStorage();
      if (!result || result.status === "cancelled") return;
      if (result.status === "failed" || !result.destination_token) {
        setNativeStoragePickerError(true);
        return;
      }
      try {
        await startRegisteredRelocate(result.destination_token);
      } catch (error) {
        await releaseMacosNativeSelections([result.destination_token]).catch(
          () => undefined,
        );
        throw error;
      }
      return;
    }
    const { open } = await import("@tauri-apps/plugin-dialog");
    const selected = await open({
      directory: true,
      multiple: false,
      title: "Choose where to store models",
    });
    if (!selected || typeof selected !== "string") return; // cancel = silent no-op
    setRelocateDest(selected);
  };

  const resetToDefault = () => {
    if (!defaultDir) return;
    setRelocateDest(defaultDir);
  };
  // ───────────────────────────────────────────────────────────────────────────

  const activeLabel = useMemo(() => {
    const code = config.language ?? "en";
    return ALL_LANGUAGES.find((l) => l.code === code)?.label ?? "English";
  }, [config.language]);

  // Authoritative exact-artifact inventory. The picker renders backend state
  // rather than reconstructing disk state, so retained Parakeet variants and a
  // configured-but-not-yet-loaded artifact stay truthful.
  // `undefined` means the authoritative status is not available yet, so the
  // configured target is the only useful fallback. Once a response arrives,
  // its `loaded_artifact_id` is authoritative even when it is explicitly null.
  const [sttStatus, setSttStatus] = useState<ExactSttStatus | undefined>();

  // WHAT IS RUNNING, not what is configured.
  //
  // `stt_backend` / `parakeet_model` describe the routing TARGET. They are the
  // right answer for "what will load next" and the wrong answer for "what is
  // loaded now", and they disagree exactly when it matters: mid-transition,
  // after a failed activation whose rollback could not be written, and before
  // the first recognizer is constructed. Showing the configured target as the
  // current model there would tell the user a model is running that is not, and
  // would make re-picking it a no-op that leaves them stranded.
  //
  // `loaded_artifact_id` is the only authoritative answer. An explicit null
  // means no recognizer is loaded and must never fall through to the configured
  // target, which would mislabel a future target as the current model.
  const loadedArtifact =
    sttStatus === undefined
      ? null
      : sttArtifactFromRegistryId(sttStatus.loaded_artifact_id);
  const configuredArtifact = resolveActiveSttArtifact(
    config.stt_backend,
    config.parakeet_model,
  );
  const currentArtifact =
    sttStatus === undefined ? configuredArtifact : loadedArtifact;
  const activeBackend: SttBackend | null = currentArtifact
    ? sttArtifactBackend(currentArtifact)
    : null;
  const currentFamily: ModelFamily | null =
    currentArtifact === null
      ? null
      : currentArtifact === "parakeet-v2"
        ? "english"
        : currentArtifact === "parakeet-v3"
          ? "multilingual"
          : null;

  // Lag the badge during an in-flight family-crossover download so the
  // user sees a single clean transition (old -> new) when the dialog
  // dismisses on download.complete, not a flicker on confirm-click.
  // Same-family / silent swaps update immediately because downloadInFlight
  // is false in those cases. Sourced from the artifact actually loaded, so the
  // badge cannot describe a model the process never brought up.
  useEffect(() => {
    if (downloadInFlight || currentFamily === null) return;
    setDisplayedFamily(currentFamily);
  }, [currentFamily, downloadInFlight]);

  // Everyday model choices expose speed/quality without opening Advanced.
  // Advanced also exposes the remaining compatible backends.
  const selectionMode = normalizeSelectionMode(config.stt_model_selection_mode);
  const compatibleBackends = getPickerModelsForLanguage(config.language, advancedOpen, activeBackend).filter(
    (backend) => backend === "parakeet" || ((backend === "qwen" || backend === "cohere") && qwenStt.enabled),
  );
  const showModelPicker =
    activeBackend !== null &&
    compatibleBackends.length > 1;
  // Qwen's decode hint is only ever effective on Manual + Qwen (scope.md), so
  // it is not shown anywhere else rather than shown inert.
  const showQwenLanguageHint =
    selectionMode === "manual" && activeBackend === "qwen";
  // Every model control is frozen for the length of a transition: the backend
  // owns exactly one recognizer swap at a time and rejects the rest.
  const modelControlsLocked = downloadInFlight;

  const refreshSttStatus = useCallback(async () => {
    const epoch = ++inventoryRequestEpoch.current;
    try {
      const status = await getSttStatus();
      if (!inventoryMounted.current || epoch !== inventoryRequestEpoch.current) return null;
      setSttStatus(status);
      return status;
    } catch (err) {
      console.warn(`[Stt] inventory load failed error_type=${errorType(err)}`);
      return null;
    }
  }, []);

  // Re-read after anything that can change which artifacts exist or which one
  // is loaded: mount, a settled switch, and a language change (which moves the
  // Parakeet choice between its English-only and multilingual artifacts).
  useEffect(() => {
    void refreshSttStatus();
    return () => { inventoryRequestEpoch.current += 1; };
  }, [
    refreshSttStatus,
    config.language,
    config.stt_backend,
    config.parakeet_model,
    config.stt_model_selection_mode,
    downloadInFlight,
    // A settled operation can leave every routing field exactly where it
    // started (that is what a rollback IS), so the config deps above cannot be
    // relied on to notice it.
    sttInventoryRevision,
    liveInventoryRevision,
    connected,
  ]);

  const pickerOptions: SttModelPickerOption[] = useMemo(
    () =>
      compatibleBackends.map((backend) => {
        if (backend === activeBackend) return { backend, state: "selected" };
        const registryId = sttArtifactRegistryId(
          sttArtifactForBackendChoice(backend, config.language),
        );
        const entry = sttStatus?.artifacts.find(
          (artifact) => artifact.id === registryId,
        );
        if (!entry?.downloaded) return { backend, state: "absent" };
        // Configured and loaded ids differ during a transition and BOTH are
        // protected, so a downloaded artifact is not automatically removable.
        const protectedIds = [
          sttStatus?.configured_artifact_id,
          sttStatus?.loaded_artifact_id,
        ];
        return {
          backend,
          state: "downloaded",
          deletable: !protectedIds.includes(registryId),
        };
      }),
    [activeBackend, compatibleBackends, config.language, sttStatus],
  );

  // Re-selecting the model that is already running is a no-op. The picker's
  // unit is the user-facing model, not the exact artifact behind it, so
  // clicking the checked row must never silently trade one Parakeet artifact
  // for the other. Turning Manual off is the documented way to land back on the
  // recommended artifact for the current language.
  const handleModelPicked = useCallback(
    (backend: SttBackend) => {
      if (modelControlsLocked || backend === activeBackend) return;
      onSttBackendChange(backend);
    },
    [activeBackend, modelControlsLocked, onSttBackendChange],
  );

  // Non-null while the deletion confirmation is up. The trash is only rendered
  // for a removable artifact, so this never targets the running model.
  const [pendingDelete, setPendingDelete] = useState<{
    backend: SttBackend;
    modelName: string;
  } | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const confirmDeleteModel = useCallback(async () => {
    if (!pendingDelete || deleteBusy) return;
    const { backend, modelName } = pendingDelete;
    const registryId = sttArtifactRegistryId(
      sttArtifactForBackendChoice(backend, config.language),
    );
    setDeleteBusy(true);
    try {
      await deleteSttModel(registryId);
      setPendingDelete(null);
      onShowStatus(`${modelName} deleted`, false);
    } catch (err) {
      setPendingDelete(null);
      const detail = err instanceof Error ? err.message : String(err);
      console.warn(`[Stt] delete failed error_type=${errorType(err)}`);
      onShowStatus(sttDeleteFailureMessage(detail, modelName), true);
    } finally {
      setDeleteBusy(false);
      // Never claim an outcome from an optimistic mutation: whatever the call
      // did, the next render reflects what the backend now reports.
      await refreshSttStatus();
    }
  }, [
    config.language,
    deleteBusy,
    onShowStatus,
    pendingDelete,
    refreshSttStatus,
  ]);

  // ── Unused-model storage cleanup ───────────────────────────────────────────
  // The exact artifacts the bulk endpoint would remove, in registry order.
  // `size_bytes` rather than `downloaded` is the test on purpose: an abandoned
  // partial download is not "downloaded", but it still occupies the disk and
  // the backend still removes it, so it belongs in the list the user approves.
  const unusedArtifacts = useMemo(() => {
    if (!sttStatus) return [];
    return sttStatus.artifacts.filter(
      (artifact) =>
        artifact.size_bytes > 0 &&
        artifact.id !== sttStatus.configured_artifact_id &&
        artifact.id !== sttStatus.loaded_artifact_id,
    );
  }, [sttStatus]);

  // Offered only when there is genuinely something to reclaim, per the
  // authoritative total. An action that would free nothing is noise.
  const showUnusedCleanup =
    sttStatus !== undefined &&
    sttStatus.unused_size_bytes > 0 &&
    unusedArtifacts.length > 0;

  const [cleanupOpen, setCleanupOpen] = useState(false);
  const [cleanupBusy, setCleanupBusy] = useState(false);

  const confirmDeleteUnusedModels = useCallback(async () => {
    if (cleanupBusy) return;
    setCleanupBusy(true);
    try {
      const result = await deleteUnusedSttModels();
      setCleanupOpen(false);
      // The refreshed inventory decides what happened, not the click. A bulk
      // removal can partially succeed (one locked directory), so "deleted" is
      // only claimed once the backend reports nothing left to reclaim.
      const refreshed = await refreshSttStatus();
      if (refreshed !== null && refreshed.unused_size_bytes > 0) {
        onShowStatus("Could not delete all unused models. Try again.", true);
      } else if ((result.removed_model_ids?.length ?? 0) === 0) {
        onShowStatus("There are no unused models to delete.", false);
      } else {
        onShowStatus("Unused models deleted", false);
      }
    } catch (err) {
      setCleanupOpen(false);
      const detail = err instanceof Error ? err.message : String(err);
      console.warn(`[Stt] bulk delete failed error_type=${errorType(err)}`);
      await refreshSttStatus();
      onShowStatus(sttBulkDeleteFailureMessage(detail), true);
    } finally {
      setCleanupBusy(false);
    }
  }, [cleanupBusy, onShowStatus, refreshSttStatus]);
  // ───────────────────────────────────────────────────────────────────────────

  useEffect(() => {
    let cancelled = false;
    fetch(`${BASE}/api/stats`)
      .then((r) => {
        if (!r.ok) throw new Error("Stats request failed");
        return r.json();
      })
      .then((value) => { if (!cancelled) setStats(value); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [statsRevision, connected]);

  return (
    <div className="absolute inset-0 flex flex-col pt-10">
      {/* Stats row */}
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30 }}
        className="flex rounded-[var(--radius-card)] mb-5 shrink-0 mx-10"
        style={{
          background: "var(--surface-control)",
          border: "1px solid var(--border-hairline)",
          boxShadow: "var(--shadow-glass-sm)",
        }}
      >
        {[
          { label: "Words", value: stats ? stats.total_words.toLocaleString() : "\u2014" },
          { label: "Time", value: stats ? formatDuration(stats.total_recording_ms) : "\u2014" },
          { label: "Transcriptions", value: stats ? stats.total_transcriptions.toLocaleString() : "\u2014" },
        ].map((stat, i) => (
          <div key={stat.label} className="flex-1 py-5 text-center">
            <motion.div
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                delay: i * 0.03,
                type: "spring",
                stiffness: 400,
                damping: 30,
              }}
              className="font-display text-[30px] font-light text-slate"
            >
              {stat.value}
            </motion.div>
            <div className="text-[11px] text-steel mt-1 font-medium uppercase tracking-wider">
              {stat.label}
            </div>
          </div>
        ))}
      </motion.div>

      {/* Settings — full-bleed inside the rounded pane: scrollbar hugs the
          right edge, pl-10 keeps content off the left edge, pr-5 keeps
          controls off the scroll track. */}
      <div className="flex-1 min-h-0 overflow-y-auto pl-10 pr-5 pb-6">
        <SettingRow index={0} title="Appearance">
          <Dropdown
            value={config.theme ?? "light"}
            options={THEME_OPTIONS}
            onChange={(theme) => onConfigChange({ theme })}
            ariaLabel="Appearance"
            triggerClassName="w-[100px]"
          />
        </SettingRow>

        <SettingRow index={1} title="Run on Startup" description={startupDescription}>
          <Toggle
            checked={startupEnabled}
            onChange={onStartupChange}
            disabled={startupDisabled}
            ariaLabel="Run on Startup"
          />
        </SettingRow>

        <SettingRow
          index={2}
          title="Restore clipboard after dictation"
          description="Save your clipboard before pasting and restore it afterwards"
        >
          <Toggle
            checked={config.clipboard_behavior ?? true}
            onChange={(next) => onConfigChange({ clipboard_behavior: next })}
          />
        </SettingRow>

        <SettingRow index={3} title="Dictation Language" description="Configure multiple languages by setting them in enhancement profiles.">
          <button
            onClick={() => onRequestOpenLanguagePopup()}
            // Frozen while a switch runs: a second language pick would race the
            // recognizer swap the first one started (design.md).
            disabled={modelControlsLocked}
            aria-label={`Dictation language: ${activeLabel}`}
            // Matches the shared Dropdown trigger (Appearance, speech model);
            // it opens the language popup instead of a list.
            className={`min-w-[100px] px-3 py-1.5 rounded-[var(--radius-input)] text-[13px] text-left text-slate flex items-center justify-between transition-all duration-200 ${
              modelControlsLocked
                ? "opacity-40 cursor-not-allowed"
                : "cursor-pointer"
            }`}
            style={{
              background: "var(--surface-control)",
              border: "1px solid var(--border-hairline-steel)",
            }}
          >
            <span className="truncate">{activeLabel}</span>
            <svg
              width="12"
              height="12"
              viewBox="0 0 12 12"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              className="text-steel shrink-0 ml-2"
            >
              <path d="M3 4.5l3 3 3-3" />
            </svg>
          </button>
        </SettingRow>

        <SettingRow
          index={4}
          title="Voice Recognition Model"
          align="start"
          description="Models vary in speed and transcription quality."
        >
          {/* Keep a plain value when there is no alternative. The inline
              switching status preserves the row height during activation. */}
          <div className="flex flex-col items-end gap-1">
            <div className="flex items-center gap-2">
              {switchingToModel && (
                <span
                  role="status"
                  className="inline-flex items-center gap-1.5 text-[11px] text-steel"
                >
                  <SwitchingSpinner />
                  Switching to {switchingToModel}
                </span>
              )}
              {showModelPicker && activeBackend !== null ? (
                <SttModelPicker
                  value={activeBackend}
                  options={pickerOptions}
                  onChange={handleModelPicked}
                  onRequestDelete={(backend) =>
                    setPendingDelete({
                      backend,
                      modelName: STT_MODEL_LABELS[backend],
                    })
                  }
                  disabled={modelControlsLocked}
                />
              ) : (
                <span className="text-[13px] text-steel leading-none">
                  {activeBackend === null
                    ? "Loading model..."
                    : STT_MODEL_LABELS[activeBackend]}
                </span>
              )}
              {activeBackend !== null && (
                <ModelInfoTooltip
                  backend={activeBackend}
                  family={displayedFamily}
                />
              )}
            </div>
            {activeBackend === "parakeet" && displayedFamily === "multilingual" && (
              <Badge label="Multilingual" tone="neutral" size="sm" />
            )}
          </div>
        </SettingRow>

        {/* Lockdown Mode — single control row (Surface A, round 01). NO
            "Privacy & Security" section header; the when-ON controls live in
            LockdownManageDialog, not inline. Matches the SettingRow rhythm
            (py-3.5 + divider). */}
        <motion.div
          initial={{ y: 6 }}
          animate={{ y: 0 }}
          transition={{ delay: 4 * 0.03, type: "spring", stiffness: 400, damping: 30 }}
        >
          <div className="flex items-center justify-between py-3.5 gap-4">
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <span className="text-[13px] font-medium text-slate">Lockdown Mode</span>
                <HoverTooltip
                  content="On-device transcription and cleanup. Saved history locked. Designed to support HIPAA-conscious workflows."
                  side="bottom-left"
                  width="w-64"
                >
                  {/* Info glyph — inline mini-icon (matches the ShieldMiniIcon/info
                      recipe in Enhancement.tsx: 14px, stroke 2, round joins). */}
                  <svg
                    width="14"
                    height="14"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="text-steel/50 cursor-help shrink-0"
                    aria-hidden="true"
                  >
                    <circle cx="12" cy="12" r="10" />
                    <line x1="12" y1="16" x2="12" y2="12" />
                    <line x1="12" y1="8" x2="12.01" y2="8" />
                  </svg>
                </HoverTooltip>
              </div>
              {/* Two sentences on their own lines (same prominence, pure line
                  break) so the row subtitle stays concise. */}
              <div className="text-[11px] text-steel mt-0.5 leading-snug">
                <span className="block">On-device transcription and cleanup. Saved history locked.</span>
                <span className="block">Designed to support HIPAA-conscious workflows.</span>
              </div>
              {/* Authority line — why the control can't be changed, kept apart
                  from the feature description above (design.md: two different
                  causes, two separate explanations). Slate at the same 11px
                  size distinguishes policy from description without a new
                  color, badge, or icon. It is the disabled toggle's accessible
                  description, so it never depends on hover. */}
              {isManagedLockdown && (
                <div
                  id="lockdown-managed-note"
                  className="text-[11px] font-medium text-slate mt-1.5 leading-snug"
                >
                  Managed by your organization.
                </div>
              )}
            </div>
            {/* Right: Manage (when ON) + the master toggle. ON -> setup modal,
                OFF (when on) -> disable dialog (deferred-write, unchanged). */}
            <div className="shrink-0 flex items-center gap-2.5">
              {isLockdownEnabled && (
                <SecondaryButton
                  onClick={() => setManageOpen(true)}
                  className="h-8 px-3.5 rounded-full text-[12px] font-medium text-slate"
                >
                  Manage
                </SecondaryButton>
              )}
              <Toggle
                checked={isLockdownEnabled}
                ariaLabel="Lockdown Mode"
                // Managed policy disables ONLY this master authority control.
                // Manage, password/recovery, transcript-history setup, and idle
                // lock all stay available.
                disabled={isManagedLockdown}
                ariaDescribedBy={
                  isManagedLockdown ? "lockdown-managed-note" : undefined
                }
                onChange={(next) => {
                  if (isManagedLockdown) return;
                  if (next) {
                    // Fresh enable from the master toggle: full flow from Step 1.
                    setSetupStartStep("ack");
                    setSetupOpen(true);
                  } else {
                    setDisableOpen(true);
                  }
                }}
              />
            </div>
          </div>
          <div className="h-px" style={{ background: "var(--divider)" }} />
        </motion.div>

        {capabilities.platform === "Darwin" && (
          <PermissionsSummary connected={connected} />
        )}

        <AdvancedDisclosure
          open={advancedOpen}
          onToggle={() => setAdvancedOpen((prev) => !prev)}
          entranceDelay={6 * 0.03}
        >
              {/* The escape hatch, and the ONLY place Automatic is left
                  deliberately. Turning it on pins the exact model already
                  running; turning it off resolves the recommended one for the
                  selected language again. App owns both transitions. */}
              <SettingRow
                index={0}
                title="Choose speech model manually"
                description="Keep a compatible speech model when you change languages."
              >
                <Toggle
                  checked={selectionMode === "manual"}
                  ariaLabel="Choose speech model manually"
                  disabled={modelControlsLocked}
                  onChange={onSelectionModeChange}
                />
              </SettingRow>

              <SettingRow
                index={1}
                title="Unload model when idle"
                description="Free memory after this long with no use. Never keeps it loaded."
              >
                <DurationSelect
                  value={config.model_timeout}
                  onChange={(seconds) => onConfigChange({ model_timeout: seconds })}
                  ariaLabel="Unload model when idle"
                />
              </SettingRow>

              <SettingRow
                index={2}
                title="Unload cleanup model when idle"
                description="Free memory after this long with no use. Never keeps it loaded."
              >
                <DurationSelect
                  value={config.enhancement_local_idle_timeout}
                  onChange={(seconds) => onConfigChange({ enhancement_local_idle_timeout: seconds })}
                  ariaLabel="Unload cleanup model when idle"
                />
              </SettingRow>

              <SettingRow
                index={3}
                title="Max Recording Duration"
                description="Auto-stop after this duration (0 for no limit)"
              >
                <div className="flex items-center gap-2">
                  <input
                    type="number"
                    min={0}
                    step={1}
                    value={config.max_recording_duration}
                    onChange={(e) =>
                      onConfigChange({
                        max_recording_duration: parseFloat(e.target.value) || 0,
                      })
                    }
                    className="w-16 px-2 py-1.5 rounded-[var(--radius-input)] text-[13px] text-center text-slate outline-none"
                    style={{
                      background: "var(--surface-control)",
                      border: "1px solid var(--border-hairline)",
                    }}
                  />
                  <span className="text-[12px] text-steel">seconds</span>
                </div>
              </SettingRow>

              {/* Qwen's decode hint, shown ONLY while Qwen is manually
                  selected. Automatic never needs it, and a routing exit clears
                  the stored value, so a disabled-but-present row here would
                  advertise a setting that cannot apply. */}
              {showQwenLanguageHint && (
                <SettingRow
                  index={4}
                  title="Force selected language"
                  description="Tell Qwen which language to listen for instead of detecting it."
                >
                  <Toggle
                    checked={config.qwen_force_language ?? false}
                    ariaLabel="Force selected language"
                    disabled={modelControlsLocked}
                    onChange={(next) =>
                      onConfigChange({ qwen_force_language: next })
                    }
                  />
                </SettingRow>
              )}

              {/* Model storage location — block-layout row (design.md decision #7).
                  Stacks header / description / path field + Browse so a long Windows
                  path fits full-width. Index=5 continues the stagger sequence. */}
              <motion.div
                initial={{ y: 6 }}
                animate={{ y: 0 }}
                transition={{ delay: 5 * 0.03, type: "spring", stiffness: 400, damping: 30 }}
              >
                <div className="py-3">
                  {/* Header line: title + Reset to default (only when custom dir is set) */}
                  <div className="flex items-center justify-between gap-4">
                    <div className="text-[13px] font-medium text-slate">Model storage location</div>
                    {isCustomDir && !privateMacStorage && (
                      <button
                        onClick={resetToDefault}
                        className="text-[12px] text-steel hover:text-slate cursor-pointer transition-colors"
                      >
                        Reset to default
                      </button>
                    )}
                  </div>
                  {/* Description */}
                  <div className="text-[11px] text-steel mt-0.5 mb-2.5">
                    {privateMacStorage
                      ? "Move the Parakeet Multilingual model. Other speech and cleanup models stay in app storage."
                      : modelLifecycle.enabled
                        ? "Where the speech and cleanup models are kept. Changing this moves about 3.5 GB to the new folder."
                        : capabilityExplanation(modelLifecycle)}
                  </div>
                  {/* Path field + Browse button */}
                  <div className="flex items-center gap-2">
                    <HoverTooltip
                      content={displayPath}
                      width="w-72"
                      side="bottom-left"
                      className="flex-1 min-w-0"
                    >
                      {/* full path exposed to assistive tech via title + aria-label
                          (not hover-only per accessibility note in copy-review.md) */}
                      <div
                        className="flex items-center gap-2 h-9 px-3 rounded-[var(--radius-row)] min-w-0 w-full"
                        style={{
                          background: "var(--surface-control)",
                          border: "1px solid var(--border-hairline)",
                        }}
                        title={privateMacStorage ? undefined : displayPath}
                        aria-label={displayPath || "Default model storage location"}
                      >
                        {/* Folder glyph */}
                        <svg
                          width="13"
                          height="13"
                          viewBox="0 0 16 16"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          className="text-steel/60 shrink-0"
                          aria-hidden="true"
                        >
                          <path d="M1 4.5A1.5 1.5 0 012.5 3h3.764a1 1 0 01.894.553L8 5H13.5A1.5 1.5 0 0115 6.5v6A1.5 1.5 0 0113.5 14h-11A1.5 1.5 0 011 12.5v-8z" />
                        </svg>
                        <span className="text-[12px] text-slate truncate flex-1">
                          {displayPath || "Default location"}
                        </span>
                        {!isCustomDir && !privateMacStorage && (
                          <span className="text-[10px] uppercase tracking-wider text-steel/70 shrink-0">
                            Default
                          </span>
                        )}
                      </div>
                    </HoverTooltip>
                    <SecondaryButton
                      onClick={browseForStorageFolder}
                      disabled={!modelLifecycle.enabled}
                      aria-label="Browse for model storage folder"
                      className="h-9 px-3.5 rounded-2xl text-[13px] text-slate shrink-0"
                    >
                      Browse
                    </SecondaryButton>
                    {privateMacStorage && nativeStoragePickerError && (
                      <span role="alert" className="text-[11px] text-rose">
                        Couldn't open the folder picker. Try again.
                      </span>
                    )}
                  </div>
                  {/* Bulk unused-model cleanup — a low-emphasis frost text
                      action, not a button: reclaiming disk is a rare
                      housekeeping errand, not something the storage row is
                      asking the user to do. It appears only when the backend
                      reports reclaimable bytes, and is frozen alongside the
                      other model controls while a switch owns the recognizer
                      (the endpoint refuses that overlap anyway). */}
                  {showUnusedCleanup && (
                    <div className="mt-2.5">
                      <button
                        type="button"
                        onClick={() => setCleanupOpen(true)}
                        disabled={modelControlsLocked}
                        className="text-[12px] font-medium text-frost-deep enabled:hover:underline cursor-pointer disabled:cursor-default disabled:opacity-50"
                      >
                        Delete unused models
                      </button>
                    </div>
                  )}
                </div>
                <div className="h-px" style={{ background: "var(--divider)" }} />
              </motion.div>
        </AdvancedDisclosure>
      </div>

      {/* Targeted speech-model deletion. Portalled to document.body because
          <main> is a backdrop-blur containing block, which would otherwise
          resolve the dialog's `position: fixed` against the content pane
          instead of the window. */}
      {pendingDelete !== null &&
        createPortal(
          <DeleteSttModelDialog
            open
            modelName={pendingDelete.modelName}
            busy={deleteBusy}
            onConfirm={confirmDeleteModel}
            onCancel={() => {
              if (!deleteBusy) setPendingDelete(null);
            }}
          />,
          document.body,
        )}

      {/* Bulk unused-model deletion. Portalled for the same containing-block
          reason as the targeted confirm above. */}
      {cleanupOpen &&
        createPortal(
          <DeleteUnusedSttModelsDialog
            open
            modelNames={unusedArtifacts.map(
              (artifact) => STT_ARTIFACT_STORAGE_LABELS[artifact.id],
            )}
            totalSize={formatModelBytes(sttStatus?.unused_size_bytes ?? 0)}
            busy={cleanupBusy}
            onConfirm={confirmDeleteUnusedModels}
            onCancel={() => {
              if (!cleanupBusy) setCleanupOpen(false);
            }}
          />,
          document.body,
        )}

      {/* Enable-Lockdown setup flow (Surface B, Task 5.4). Opened by the master
          Toggle (fresh, from Step 1) or the "start saving" deep-link (transcripts
          step). The enable call is server-authoritative: the modal adopts the
          returned config via onConfigReplace instead of PUTting a stale patch. */}
      <LockdownSetupModal
        historyDisabled={lockStatus?.historyDisabled === true}
        requiresKeychainConsent={capabilities.platform === "Darwin" && config.mac_history_opt_in !== true}
        open={setupOpen}
        idleLockSeconds={config.lockdown_idle_lock_seconds ?? 300}
        currentSaveAudio={config.save_audio}
        currentTranscriptRetentionDays={config.transcript_retention_days}
        initialStep={setupStartStep}
        ackAccepted={ackAccepted}
        onClose={() => setSetupOpen(false)}
        onConfigSynced={onConfigReplace}
        onLockRefresh={onLockRefresh}
      />

      {/* Disable-Lockdown dialog (Surface E, Task 5.6). Opens when the master
          Toggle is flipped OFF. Requires the current password before disabling.
          On confirm the backend clears lockdown_mode and returns the sanitized
          config, which the client adopts. NOTE: disable deliberately does NOT
          restore a prior cloud provider or re-enable AI cleanup — silently
          resuming off-device uploads would be a privacy violation; the user must
          opt back into cloud themselves. */}
      <LockdownDisableDialog
        // Managed installs have no route here (the master toggle is disabled and
        // Manage drops its turn-off action). The guard keeps it that way if a
        // policy arrives while some other surface holds this open.
        open={disableOpen && !isManagedLockdown}
        hasEnvelope={hasEnvelope}
        onCancel={() => setDisableOpen(false)}
        onDisabled={(syncedConfig) => {
          setDisableOpen(false);
          if (syncedConfig) onConfigReplace(syncedConfig);
          onLockRefresh?.();
        }}
      />

      {/* Manage dialog (Surface A, round 01). Opens from the Manage button on
          the Lockdown row when ON. Full (envelope) vs slim (no-envelope)
          variant keyed off lock status. Its actions close Manage and hand off
          to the existing child dialogs / the setup modal. */}
      <LockdownManageDialog
        open={manageOpen}
        managed={isManagedLockdown}
        historyDisabled={lockStatus?.historyDisabled === true}
        hasEnvelope={hasEnvelope}
        idleLockSeconds={config.lockdown_idle_lock_seconds ?? 300}
        onIdleLockChange={(secs) =>
          onConfigChange({ lockdown_idle_lock_seconds: secs })
        }
        onChangePassword={() => {
          setManageOpen(false);
          setChangePwOpen(true);
        }}
        onViewRecovery={() => {
          setManageOpen(false);
          setViewCodeOpen(true);
        }}
        onStartSaving={() => {
          setManageOpen(false);
          // "Start saving" from Manage under a no-envelope lockdown deep-links to
          // the transcripts step with Keep pre-selected, same as the Transcripts
          // page signal.
          setSetupStartStep("transcripts");
          setSetupOpen(true);
        }}
        onDisable={() => {
          setManageOpen(false);
          setDisableOpen(true);
        }}
        onClose={() => setManageOpen(false)}
      />

      {/* Change-password dialog (Surface A action, Task 5.6). */}
      <ChangePasswordDialog
        open={changePwOpen}
        onCancel={() => setChangePwOpen(false)}
        onChanged={() => {
          setChangePwOpen(false);
          onLockRefresh?.();
        }}
      />

      {/* View/regenerate recovery code dialog (Surface A action, Task 5.6).
          Viewing mints a fresh code and invalidates the old one
          (regenerate-on-view decision). */}
      <ViewRecoveryCodeDialog
        open={viewCodeOpen}
        onCancel={() => setViewCodeOpen(false)}
        onRegenerated={() => {
          onLockRefresh?.();
        }}
      />

      {/* RelocateModelsDialog — mounts when the user picks a folder (Browse) or
          clicks "Reset to default". Unmounts on close/cancel/success/error. */}
      {relocateDest !== null && (
        <RelocateModelsDialog
          destination={relocateDest}
          currentPath={displayPath}
          subscribe={subscribe}
          onRelocated={onConfigReload}
          onClose={() => setRelocateDest(null)}
          onChooseAnother={() => {
            setRelocateDest(null);
            // Re-open the picker after state clears (blocked -> choose another).
            // Small timeout lets React flush the unmount before the async open.
            setTimeout(browseForStorageFolder, 50);
          }}
        />
      )}
    </div>
  );
}

function SettingRow({
  index,
  title,
  description,
  children,
  last,
  align = "center",
}: {
  index: number;
  title: string;
  description?: React.ReactNode;
  children: React.ReactNode;
  last?: boolean;
  /** Vertical alignment of the title/children row. Default "center"; pass
   *  "start" when the left column has a multi-line description that should sit
   *  under the title while the control stays top-aligned with it. */
  align?: "center" | "start";
}) {
  return (
    <motion.div
      initial={{ y: 6 }}
      animate={{ y: 0 }}
      transition={{
        delay: index * 0.03,
        type: "spring",
        stiffness: 400,
        damping: 30,
      }}
    >
      <div
        className={`flex ${align === "start" ? "items-start" : "items-center"} justify-between py-3 gap-4`}
      >
        <div className="min-w-0">
          <div className="text-[13px] font-medium text-slate">{title}</div>
          {description && (
            <div className="text-[11px] text-steel mt-0.5">{description}</div>
          )}
        </div>
        <div className="shrink-0">{children}</div>
      </div>
      {!last && <div className="h-px" style={{ background: "var(--divider)" }} />}
    </motion.div>
  );
}

// The app's sweeping-arc spinner (the third production copy — see frontend
// backlog #8; it is not extracted because the HUD's copy owns Tauri
// presentation semantics the others must not inherit). Decorative: the
// switching status text beside it is what is announced.
function SwitchingSpinner() {
  const reduceMotion = useReducedMotion();
  return (
    <motion.svg
      width="11"
      height="11"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      aria-hidden="true"
      className="shrink-0"
      animate={reduceMotion ? undefined : { rotate: 360 }}
      transition={
        reduceMotion
          ? undefined
          : { duration: 0.9, repeat: Infinity, ease: "linear" }
      }
    >
      <path d="M7 1.5a5.5 5.5 0 015.5 5.5" />
    </motion.svg>
  );
}

// Locked tooltip copy (copy-review.md → Model row). For Parakeet, the blurb
// follows the active v2/v3 family (english vs multilingual); Qwen and Cohere
// each have one blurb. `family` is only read for Parakeet.
function modelTooltipCopy(backend: SttBackend, family: ModelFamily): string {
  if (backend === "qwen") {
    return "Alibaba's Qwen3-ASR 0.6B speech-to-text model. Covers 30 languages with automatic detection.";
  }
  if (backend === "cohere") {
    return "Cohere's speech-to-text model. Supports 14 languages, uses your selected language, and requires more memory.";
  }
  return family === "english"
    ? "NVIDIA's 600M parameter speech-to-text model, optimized for English speech."
    : "NVIDIA's 600M parameter speech-to-text model. Covers 25 languages with automatic detection.";
}

// The bare info icon + hover bubble used by the single-model label.
function ModelInfoTooltip({
  backend,
  family,
}: {
  backend: SttBackend;
  family: ModelFamily;
}) {
  return (
    <HoverTooltip side="bottom-right" content={modelTooltipCopy(backend, family)}>
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="opacity-40 group-hover:opacity-70 transition-opacity cursor-help"
      >
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="16" x2="12" y2="12" />
        <line x1="12" y1="8" x2="12.01" y2="8" />
      </svg>
    </HoverTooltip>
  );
}
