import { isTauri } from "@tauri-apps/api/core";
import { StartupFailureCurtain } from "./components/StartupFailureCurtain";
import { LoadingCurtain } from "./components/LoadingCurtain";
import {
  type ComponentType,
  lazy,
  Suspense,
  useState,
  useEffect,
  useCallback,
  useRef,
} from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Sidebar, type TabId } from "./components/Sidebar";
import { EulaPopup, EULA_INLINE_BACKDROP_STYLE } from "./pages/EulaPopup";
import { Home } from "./pages/Home";
import { Audio } from "./pages/Audio";
import { Controls } from "./pages/Controls";
import { Agents } from "./pages/Agents";
import { Dictionary } from "./pages/Dictionary";
import { Transcripts } from "./pages/Transcripts";
import { Enhancement } from "./pages/Enhancement";
import { About } from "./pages/About";
import { officialUpdater } from "./lib/officialUpdater";
import { DownloadDialog } from "./components/DownloadDialog";
import { EnhancementRecoveryHost } from "./components/EnhancementRecoveryHost";
import { FeatureSpotlightHost } from "./components/FeatureSpotlightHost";
import { WhatsNewTourHost } from "./components/WhatsNewTourHost";
import { launchOverlayGates } from "./lib/whatsNew";
import type { SpotlightNav } from "./lib/spotlightOffers";
import { OnboardingOverlay } from "./components/onboarding/OnboardingOverlay";
import { LanguagePopup } from "./components/onboarding/LanguagePopup";
import { MultilingualLanguageSwitchDialog } from "./components/MultilingualLanguageSwitchDialog";
import { ModelDownloadConfirmDialog } from "./components/ModelDownloadConfirmDialog";
import { PrivacyModal } from "./components/PrivacyModal";
import { consentConfigPatch, consentRequest, enhancementErrorMessage } from "./lib/enhancementConsent";
import { createConfigSaveQueue } from "./lib/configSaveQueue";
import {
  LockdownScopeReviewDialog,
  needsLockdownScopeReview,
} from "./components/LockdownScopeReviewDialog";
import { LockdownDisableDialog } from "./components/LockdownDisableDialog";
import { LOCKDOWN_ACK_VERSION } from "./components/LockdownSetupModal";
import {
  ALL_LANGUAGES,
  getModelsForLanguage,
  getSttArtifactDisplayName,
  isLanguageSupportedByArtifact,
  planKeepCurrentModel,
  planLanguageChange,
  planSwitchToRecommended,
  resolveSelectionModeChange,
  resolveSttExecution,
  sttArtifactBackend,
  sttArtifactForBackendChoice,
  sttArtifactFromRegistryId,
  sttArtifactRegistryId,
  sttRoutingPatch,
  type ActiveSttArtifact,
  type SttBackend,
  type SttRoutingPatch,
} from "./lib/languages";
import {
  IDLE_STT_SWITCH,
  sttCancelDecision,
  sttDownloadFailureCopy,
  sttRoutingSaveFailureMessage,
  sttStartRejectionMessage,
  sttSwitchFailureMessage,
  sttSwitchReleased,
  sttSwitchStarted,
  sttTerminalDecision,
  type SttSwitchLifecycle,
  type SttTerminalEvent,
} from "./lib/sttSwitch";
import { useAppSocket } from "./lib/socket/connection";
import { sttFailureToErrorPayload } from "./lib/sttFailure";
import { getConfig, getDailyUseCapabilities, getAdvancedCapabilities, saveConfig, type AppConfig } from "./lib/api/config";
import { getEnhancementModels, type EnhancementModel } from "./lib/api/enhancement";
import { startDownload, cancelDownload, getSttStatus } from "./lib/api/models";
import { getHealth } from "./lib/api/system";
import { getBYOKConsent, recordBYOKConsent, getEulaStatus, resolveEulaRequirementState, type EulaStatus, type EulaEmitPayload } from "./lib/api/consent";
import { acknowledgeLockdown } from "./lib/api/lockdown";
import { getDevEnabled, getExperimentalEnabled } from "./lib/api/development";
import { RECORDING_CONTROL_WINDOW_LABEL, RECORDING_CONTROL_VISIBILITY_EVENT, RECORDING_CONTROL_VISIBILITY_STORAGE_KEY } from "./lib/recordingControl";
import { SCREEN_READER_STATUS_EVENT, SCREEN_READER_STATUS_STORAGE_KEY } from "./lib/accessibility";
import { type ErrorEmitPayload, type ErrorCode, type RetryAction } from "./lib/api/errors";
import {
  capabilityDecision,
  capabilityExplanation,
  parseDailyUseCapabilityReport,
  type DailyUseCapabilityReport,
} from "./lib/dailyUseCapabilities";
import {
  parseAdvancedCapabilityReport,
  type AdvancedCapabilityReport,
} from "./lib/advancedCapabilities";
import {
  getStartupState,
  applyStartupChange,
  type LaunchAtLoginStatus,
} from "./lib/autostart";
import { ExperimentalProvider, EXPERIMENTAL_BUILD } from "./lib/experimental";
import { errorType, flushLogSync } from "./lib/logCapture";
import { useLockdown } from "./lib/useLockdown";
import { useHistoryRecovery } from "./lib/useHistoryRecovery";
import { historyPlatform } from "./lib/historyRecoveryCopy";
import { TitleBar } from "./components/TitleBar";
import {
  DEDICATED_RECORDING_CONTROL_WINDOW_H,
  DEDICATED_RECORDING_CONTROL_WINDOW_W,
} from "./pages/hud/geometry";
import { WebviewWindow } from "@tauri-apps/api/webviewWindow";
import { publishPopupVisibility, applyWindowLifecycle, type WindowLifecycleRole } from "./lib/windowLifecycle";
import { emit, listen } from "@tauri-apps/api/event";

// The UI Test page is development tooling that source releases omit, so it
// registers only when its module exists (the glob is empty otherwise).
type UiTestPage = ComponentType<{ onShowPopup: typeof showPopup }>;
const loadUiTestPage = import.meta.env.DEV
  ? import.meta.glob<{ UiTest: UiTestPage }>("./pages/UiTest.tsx")["./pages/UiTest.tsx"]
  : undefined;
const UiTest = loadUiTestPage
  ? lazy(() => loadUiTestPage().then(({ UiTest }) => ({ default: UiTest })))
  : null;

const pageTransition = {
  type: "tween" as const,
  duration: 0.15,
  ease: [0.2, 0, 0, 1] as const,
};

const meshGradient = `
  radial-gradient(ellipse at 20% 50%, var(--frost-mesh-1) 0%, transparent 50%),
  radial-gradient(ellipse at 80% 20%, var(--frost-mesh-2) 0%, transparent 45%),
  radial-gradient(ellipse at 45% 80%, var(--frost-mesh-3) 0%, transparent 50%),
  var(--color-canvas)
`;

// A language change that needs the user to decide before anything is
// persisted. `keep-or-switch` is the Automatic case where the running model
// also covers the target language; `manual-incompatible` is the Manual case
// where the pinned model cannot serve it at all.
type PendingLanguageGuidance = {
  kind: "keep-or-switch" | "manual-incompatible";
  targetLanguage: string;
  currentArtifact: ActiveSttArtifact;
  recommendedArtifact: ActiveSttArtifact;
};

function getLanguageDisplayName(code: string): string {
  return (
    ALL_LANGUAGES.find((language) => language.code === code.toLowerCase())
      ?.label ?? code
  );
}

// Locked failure copy and the switch lifecycle live in lib/sttSwitch, so both
// are unit-tested directly and the confirm-then-download dialog can render the
// same recovery strings this file shows in the status toast.
function failureDetail(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// Emits a show event the popup listens for to reset its state, then shows the window.
// Decoupled from tauri://focus so re-focusing an already-visible popup doesn't replay
// the entry animation.
//
// For `popup-eula`, an optional `payload` is emitted on a separate `"eula-data"`
// channel BEFORE the window is shown, so the popup's listener has the payload
// in hand by the time it hydrates. Mirrors the `popup-update` emit-then-show
// pattern used by the `?forceUpdate=1` dev hook below.
//
// The ready-handshake waits for `<label>:ready` (emitted by the popup's React
// tree via usePopupReady after first paint) before calling WebviewWindow.show().
// This eliminates the Tauri v2 + WebView2 transparent-window paint race on
// Windows. Falls back after 1500ms so a partial-rollout install (popup doesn't
// emit :ready) still shows the window.
interface ReviewInitPayload {
  initialStep: "question" | "star" | "issue";
}
interface UpdateInfoPayload {
  version: string;
  body: string;
  source?: "automatic" | "manual";
}
async function showPopup(
  label: "popup-review" | "popup-eula" | "popup-update" | "popup-error",
  payload?: EulaEmitPayload | ReviewInitPayload | UpdateInfoPayload | ErrorEmitPayload,
  canShow: () => boolean = () => true,
) {
  if (!canShow()) return;
  try {
    let unlisten: (() => void) | undefined;
    const start = performance.now();

    // Register the ready listener BEFORE emitting any payload or show event so
    // a fast popup that paints during the emit doesn't race past us.
    const ready = new Promise<"ready" | "timeout">((resolve) => {
      const timer = setTimeout(() => {
        resolve("timeout");
      }, 1500);
      listen<void>(`${label}:ready`, () => {
        clearTimeout(timer);
        resolve("ready");
      }).then((u) => { unlisten = u; });
    });

    console.log(`[Popup] show waiting label=${label}`);

    if (label === "popup-eula" && payload) {
      await emit("eula-data", payload as EulaEmitPayload);
    }
    if (label === "popup-review" && payload) {
      await emit("popup-review:init", payload as ReviewInitPayload);
    }
    if (label === "popup-update" && payload) {
      lastUpdatePayload = payload as UpdateInfoPayload;
      await emit("update-available", payload as UpdateInfoPayload);
    }
    await emit(`${label}:show`, label === "popup-update" ? payload : undefined);

    const outcome = await ready;

    // popup-error: store the payload so a `popup-error:fetch` request from
    // the popup window can recover from a missed initial emit. Emit data
    // AFTER :ready as a best-effort first try — the popup's :fetch round-trip
    // is the durable fallback if this races past its listener.
    if (label === "popup-error" && payload) {
      lastErrorPayload = payload as ErrorEmitPayload;
      await emit("popup-error:data", payload as ErrorEmitPayload);
    }

    if (unlisten) {
      try { unlisten(); } catch { /* ignore */ }
    }

    const waitMs = Math.round(performance.now() - start);
    if (outcome === "timeout") {
      console.warn(`[Popup] ready timeout label=${label} wait_ms=${waitMs}`);
    } else {
      console.log(`[Popup] show resolved label=${label} wait_ms=${waitMs}`);
    }

    if (!canShow()) return;
    const contractRole: WindowLifecycleRole = label === "popup-eula"
      ? "eula"
      : label === "popup-update"
        ? "update"
        : label === "popup-review"
          ? "review"
          : "error";
    if (!(await applyWindowLifecycle(contractRole, "show"))) {
      const w = await WebviewWindow.getByLabel(label);
      await w?.show();
      if (w) await publishPopupVisibility(label, true);
    }
    if (!canShow()) {
      const w = await WebviewWindow.getByLabel(label);
      await w?.hide();
      if (w) await publishPopupVisibility(label, false);
    }
  } catch {
    /* not in Tauri context */
  }
}

// Module-level guards for the dev `?force*=1` hooks. React StrictMode in dev
// double-fires `useEffect` with [] deps, which would otherwise show each forced
// popup twice (and re-show it after the user dismisses, since the second async
// show() can complete after dismiss). Module-level vars survive StrictMode's
// simulated unmount/remount, so the action runs exactly once per page load.
const devForcedShown = {
  review: false,
  update: false,
  eula: false,
  amendment: false,
  errorDialog: false,
};

// Module-level latch so the first-run EULA popup is only shown once per page
// load even if React StrictMode re-runs the render-time dispatcher. Resetting
// it back to false when the gate releases (needs_first_run becomes false) is
// not needed because this is a single-shot gate per launch.
let eulaFirstRunPopupShown = false;

// Module-level latch for the inline amendment flow. Prevents the production
// trigger useEffect (which watches eulaStatus) from setting amendmentInlinePayload
// more than once per page load — matches the same StrictMode-guard pattern
// used by eulaFirstRunPopupShown above.
let amendmentInlineShown = false;

// Module-level latch for the inline re-acknowledgement flow (EULA Section 20).
// Same StrictMode-guard pattern as amendmentInlineShown above.
let reacknowledgeInlineShown = false;

let recordingControlWindowCreation:
  | Promise<WebviewWindow | null>
  | null = null;

async function ensureRecordingControlWindow(): Promise<WebviewWindow | null> {
  if (!isTauri()) return null;
  if (recordingControlWindowCreation !== null) {
    return recordingControlWindowCreation;
  }
  recordingControlWindowCreation = (async () => {
    try {
      const existing = await WebviewWindow.getByLabel(
        RECORDING_CONTROL_WINDOW_LABEL,
      );
      if (existing) return existing;
      return new WebviewWindow(RECORDING_CONTROL_WINDOW_LABEL, {
        url: "/recording-control",
        title: "",
        width: DEDICATED_RECORDING_CONTROL_WINDOW_W,
        height: DEDICATED_RECORDING_CONTROL_WINDOW_H,
        resizable: false,
        decorations: false,
        transparent: true,
        shadow: false,
        alwaysOnTop: true,
        visible: false,
        skipTaskbar: true,
        focusable: false,
      });
    } catch (error) {
      recordingControlWindowCreation = null;
      console.error(
        `[HUD] dedicated control create failed reason=${
          error instanceof Error ? error.message : String(error)
        }`,
      );
      return null;
    }
  })();
  return recordingControlWindowCreation;
}

// Module-level latch for the backend-disconnect popup. Fires once per page load
// when the disconnect threshold is reached; resets when the connection recovers.
let backendDisconnectPopupShown = false;

// Module-level snapshot of the most recently dispatched error.dialog payload.
// The popup-error window asks for this via `popup-error:fetch` after its data
// listener is active, so an early :data emit that raced past the listener can
// be recovered. Mirrors UpdatePopup's check() / EulaPopup's getEulaStatus()
// fallback patterns — every popup needs a fallback because Tauri's listen()
// is async.
let lastErrorPayload: ErrorEmitPayload | null = null;

// The latest update offer sent to popup-update. On the first show after launch
// the popup's webview may not be listening yet, so `popup-update:show` is lost;
// the popup sends `popup-update:fetch` once its listener is live and gets the
// offer re-sent. Re-sending only rebinds the popup's content; it never shows
// the window.
let lastUpdatePayload: UpdateInfoPayload | null = null;

function CapabilityErrorCurtain({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      data-testid="capability-error"
      className="w-full min-h-[100dvh] flex items-center justify-center"
      style={{ background: meshGradient, backgroundSize: "200% 200%" }}
    >
      <div className="text-center max-w-sm px-8">
        <div className="font-display text-lg font-light tracking-[0.12em] text-steel mb-3">
          whisperstream
        </div>
        <div className="text-[13px] font-medium text-slate">Couldn't confirm platform support</div>
        <div className="text-[12px] text-steel mt-1.5 mb-4">
          Settings stay unavailable until the local engine reports what this build supports.
        </div>
        <button
          type="button"
          onClick={onRetry}
          className="h-9 px-4 rounded-full text-[12px] font-medium text-slate cursor-pointer"
          style={{
            background: "var(--surface-control)",
            border: "1px solid var(--border-emphasis)",
          }}
        >
          Try again
        </button>
      </div>
    </div>
  );
}

interface ReadyAppProps {
  initialEulaStatus: EulaStatus;
  onConsentRequired: (status: EulaStatus) => void;
}

function ReadyApp({ initialEulaStatus, onConsentRequired }: ReadyAppProps) {
  const { connected, send, subscribe, everConnected, reconnectAttempt, lastDisconnectAt } = useAppSocket();
  // Lockdown lock status: polls on mount, stays live via the transcripts.locked
  // WS event. Passed down to Transcripts (lock screen), Enhancement (disabled
  // cloud providers), and Home (sub-controls visibility). The hook must be called
  // at the App level so all three pages share the same live state.
  const lockStatus = useLockdown(subscribe);
  // Standard-history recovery status: gates Transcripts, drives the one-time
  // launch prompt and the sidebar dot. Lockdown owns recovery while it is on.
  const historyRecovery = useHistoryRecovery(
    subscribe,
    !lockStatus.enabled && lockStatus.historyDisabled !== true,
  );
  const [recoverySetupRequest, setRecoverySetupRequest] = useState(0);
  const [tab, setTab] = useState<TabId>("home");
  // Set when the backend flagged a launch-time GPU re-check; routes to the
  // Enhancement tab and tells it to auto-open the benchmark on startup.
  // Bumped by Transcripts when the user tries to save transcripts under a
  // no-envelope lockdown (F5): navigates to Home AND tells it to auto-open the
  // Lockdown setup modal (the same one "Start saving transcripts" opens), so
  // the user reaches password setup in one hop instead of hunting for Manage.
  // 0 = no pending request; Home's effect ignores the initial falsy value and
  // fires only on each increment.
  const [lockdownSetupRequest, setLockdownSetupRequest] = useState(0);
  // Feature-spotlight / upgrade-offer overlay: bumped when the Gemini offer's
  // CTA fires, so the Enhancement tab performs the consent-safe provider switch
  // + model auto-select. 0 = idle; Enhancement's effect fires only on increment.
  const [geminiFlashSwitch, setGeminiFlashSwitch] = useState(0);
  // Lowest-priority launch gate for the spotlight overlay: opens a beat after
  // launch so the update / review popups (which fire near startup) win
  // the moment. The host still requires setup_completed + no lockdown.
  const [spotlightGateOpen, setSpotlightGateOpen] = useState(false);
  // Sticky per session: once the What's new tour appears, spotlight offers wait
  // for the next launch so two launch overlays never chain back to back.
  const [whatsNewShownThisSession, setWhatsNewShownThisSession] = useState(false);
  const markWhatsNewShown = useCallback(() => setWhatsNewShownThisSession(true), []);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [dailyCapabilities, setDailyCapabilities] = useState<DailyUseCapabilityReport | null>(null);
  const [advancedCapabilities, setAdvancedCapabilities] = useState<AdvancedCapabilityReport | null>(null);
  const [capabilitiesError, setCapabilitiesError] = useState(false);
  // Monotonic signal for children whose side effects must observe backend-
  // persisted config rather than App's optimistic config snapshot.
  const [configSaveRevision, setConfigSaveRevision] = useState(0);
  const [startupStatus, setStartupStatus] = useState<LaunchAtLoginStatus>({
    state: "unavailable",
    failure: null,
  });
  const [status, setStatus] = useState<{
    message: string;
    isError: boolean;
  } | null>(null);
  const statusTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [downloadModel, setDownloadModel] = useState<string | null>(null);
  const [enhancementRecoveryOpen, setEnhancementRecoveryOpen] = useState(false);
  const [modelLoaded, setModelLoaded] = useState(false);
  const [dictationActivity, setDictationActivity] = useState({ arming: false, recording: false, transcribing: false });
  const [eulaStatus, setEulaStatus] = useState<EulaStatus>(initialEulaStatus);
  // Inline amendment payload. Non-null when the amendment EULA should be rendered
  // as a modal overlay over the main UI. Cleared when the user accepts (via the
  // eula-accepted listener) or dismisses (via EulaPopup's onClose callback).
  const [amendmentInlinePayload, setAmendmentInlinePayload] = useState<EulaEmitPayload | null>(null);
  // Inline re-acknowledgement payload (EULA Section 20). Non-null when the
  // accept-or-exit re-acknowledgement prompt should render over the main UI.
  // Cleared by the eula-accepted listener on accept; Decline exits the app.
  const [reacknowledgeInlinePayload, setReacknowledgeInlinePayload] = useState<EulaEmitPayload | null>(null);
  // Existing Lockdown users receive one compact review when the disclosure
  // version changes. The authenticated disable dialog is the only alternative path.
  const [lockdownScopeReviewResolved, setLockdownScopeReviewResolved] = useState(false);
  const [lockdownScopeReviewDisableOpen, setLockdownScopeReviewDisableOpen] = useState(false);
  // Track latest config inside the WS subscribe closure without re-subscribing.
  const configRef = useRef<AppConfig | null>(null);
  // Language picker + confirm-then-download state. Owned here so the popup and
  // dialog mount at the App root, escaping the <main> backdrop-blur containing
  // block (design.md §3, §4). `pendingDownload` carries the resolved target
  // (the config patch to apply on confirm + which backend's model is being
  // fetched, for the dialog copy); non-null while the confirm dialog is up.
  const [languagePopupOpen, setLanguagePopupOpen] = useState(false);
  const [pendingLanguageGuidance, setPendingLanguageGuidance] =
    useState<PendingLanguageGuidance | null>(null);
  const [pendingDownload, setPendingDownload] = useState<{
    backend: SttBackend;
    artifact: ActiveSttArtifact;
    patch: SttRoutingPatch;
  } | null>(null);
  const [languageDownloadInFlight, setLanguageDownloadInFlight] = useState(false);
  // Non-null while an artifact that is ALREADY on disk is being activated in
  // place. It still runs through the one backend activation operation (saving
  // config alone leaves the previous recognizer live in the process), but there
  // is no transfer to show, so it reports through the in-flight flag Home
  // already consumes instead of opening a download modal.
  const [sttActivation, setSttActivation] = useState<{
    artifact: ActiveSttArtifact;
    modelName: string;
  } | null>(null);
  // Read from the WebSocket handler, which must not re-subscribe per change.
  const sttActivationRef = useRef<{
    artifact: ActiveSttArtifact;
    modelName: string;
  } | null>(null);
  // Rollback tracking for the one in-flight backend switch. Deliberately NOT
  // derived from the dialog/activation state: a cancel that answers
  // `cancelling` releases the UI long before the worker finishes rolling the
  // routing back, and the terminal event still has to be reconciled when it
  // finally lands (see lib/sttSwitch).
  const sttSwitchRef = useRef<SttSwitchLifecycle>(IDLE_STT_SWITCH);
  // Bumped whenever the set of stored artifacts, or which one is loaded, can
  // have changed. Home re-reads the authoritative inventory off this.
  const [sttInventoryRevision, setSttInventoryRevision] = useState(0);
  // Privacy modal state (for AI Enhancement). Provider name when visible, null when hidden.
  const [privacyModalProvider, setPrivacyModalProvider] = useState<string | null>(
    import.meta.env.DEV
      ? (new URLSearchParams(window.location.search).get("forcePrivacyModal") ?? null)
      : null
  );
  // Acknowledged providers, loaded from byok-consent.json via /api/byok-consent.
  // Replaces the legacy enhancement_privacy_acknowledged_providers array in
  // config.toml.
  const [acknowledgedProviders, setAcknowledgedProviders] = useState<string[]>([]);
  // Dev-tools (UI Test tab) runtime gate. Backend tells us via GET
  // /api/dev/enabled whether WHISPERSTREAM_DEV_TOOLS=1 is set; false by
  // default (also short-circuits via import.meta.env.DEV at build time).
  const [devEnabled, setDevEnabled] = useState(false);
  // Resolved catalog row for the active local model. Undefined means the
  // dev-only lookup is still in flight; null means the configured model is not
  // a catalog candidate. Transcripts receives this exact snapshot so it can
  // disable unavailable candidates without duplicating the model picker.
  const [activeDevModel, setActiveDevModel] = useState<
    EnhancementModel | null | undefined
  >(undefined);
  // Agent-speech experimental gate. Backend tells us via GET
  // /api/experimental/enabled whether WHISPERSTREAM_EXPERIMENTAL=1 AND the build
  // kept the modules. Folded together with the compile-time EXPERIMENTAL_BUILD
  // gate by ExperimentalProvider below; units 0b/0c read it via useExperimental().
  const [experimentalEnabled, setExperimentalEnabled] = useState(false);
  // Belt-and-braces dev gate: import.meta.env.DEV is dead-code-eliminated
  // by Vite in production, so devTabVisible is constant-false in prod
  // builds and the Sidebar's tab filter strips the tab entirely.
  const devTabVisible = import.meta.env.DEV && devEnabled;

  useEffect(() => {
    if (!devTabVisible) {
      setActiveDevModel(undefined);
      return;
    }
    const modelId = config?.enhancement_local_model;
    if (!modelId) {
      setActiveDevModel(null);
      return;
    }

    let cancelled = false;
    setActiveDevModel(undefined);
    getEnhancementModels("local")
      .then((catalog) => {
        if (cancelled) return;
        setActiveDevModel(
          catalog.bundled.find((model) => model.id === modelId) ?? null,
        );
      })
      .catch(() => {
        if (!cancelled) setActiveDevModel(null);
      });
    return () => {
      cancelled = true;
    };
  }, [devTabVisible, config?.enhancement_local_model]);

  const refreshAcknowledgedProviders = useCallback(async () => {
    try {
      const state = await getBYOKConsent();
      setAcknowledgedProviders(state.acknowledged_providers ?? []);
    } catch (err) {
      console.warn(`[BYOK] consent state load failed error_type=${errorType(err)}`);
    }
  }, []);

  useEffect(() => {
    refreshAcknowledgedProviders();
  }, [refreshAcknowledgedProviders]);

  // Rollback is owned by the backend activation worker: it restores the
  // complete prior routing state (language, selection mode, backend, Parakeet
  // variant, Qwen hint) before it reports a cancel or a failure, so the
  // frontend re-reads that state instead of replaying a partial snapshot.
  //
  // Keep configRef in sync so the WS subscribe closure can read the latest.
  useEffect(() => {
    configRef.current = config;
  }, [config]);

  // Dev-only hook for Playwright verification of the review prompt popup flow.
  // Loading the page with ?forceReviewPrompt=1 immediately shows the review
  // popup without needing 20+ real sessions or a backend review.prompt event.
  // Guarded by import.meta.env.DEV so it never ships in production bundles.
  const forceReviewPromptRef = useRef<boolean>(
    import.meta.env.DEV &&
      typeof window !== "undefined" &&
      new URLSearchParams(window.location.search).get("forceReviewPrompt") === "1"
  );
  useEffect(() => {
    if (forceReviewPromptRef.current && !devForcedShown.review) {
      devForcedShown.review = true;
      showPopup("popup-review");
    }
  }, []);

  // Dev-only hook for visually verifying the update popup without a real release.
  // Loading the page with ?forceUpdate=1 emits a fake update-available event
  // (with a sample version + body) and shows the popup. Guarded by
  // import.meta.env.DEV so it never ships in production bundles.
  const forceUpdateRef = useRef<boolean>(
    import.meta.env.DEV &&
      typeof window !== "undefined" &&
      new URLSearchParams(window.location.search).get("forceUpdate") === "1"
  );
  useEffect(() => {
    if (forceUpdateRef.current && !devForcedShown.update) {
      devForcedShown.update = true;
      (async () => {
        try {
          await showPopup("popup-update", {
            version: "0.2.0",
            body: "Bug fixes and improvements.",
          });
        } catch {
          /* not in Tauri context */
        }
      })();
    }
  }, []);

  // Dev-only hook for Playwright verification of the EULA first-run popup flow.
  // Loading the page with ?forceEula=1 fetches real EULA data from the backend
  // and shows the popup even when acceptance.json already exists on disk.
  // Guarded by import.meta.env.DEV so it never ships in production bundles.
  const forceEulaRef = useRef<boolean>(
    import.meta.env.DEV &&
      typeof window !== "undefined" &&
      new URLSearchParams(window.location.search).get("forceEula") === "1"
  );
  useEffect(() => {
    if (forceEulaRef.current && !devForcedShown.eula) {
      devForcedShown.eula = true;
      (async () => {
        try {
          const s = await getEulaStatus();
          setEulaStatus(s);
          console.log(`[EULA] forceEula status fetched needs_first_run=${s.needs_first_run}`);
          const payload: EulaEmitPayload = {
            mode: "first-run",
            eulaText: s.bundled_text ?? "",
            eulaVersion: s.bundled_version ?? "",
            eulaSha256: s.bundled_sha256 ?? "",
            softwareVersion: s.software_version,
          };
          await showPopup("popup-eula", payload);
        } catch (e) {
          console.error(`[EULA] forceEula error error_type=${errorType(e)}`);
        }
      })();
    }
  }, []);

  // Dev-only hook for Playwright verification of the EULA amendment inline flow.
  // Loading the page with ?forceAmendment=1 fetches real EULA data from the
  // backend and sets a synthetic amendment payload (with fixed v2.0/v1.0
  // versions and sample change bullets) to render inline over the main UI.
  // Exercises the full production path through App.tsx's gate rather than the
  // popup-level shortcut in EulaPopup.tsx (?forceAmendment=1 on /popup/eula).
  // Guarded by import.meta.env.DEV so it never ships in production bundles.
  const forceAmendmentRef = useRef<boolean>(
    import.meta.env.DEV &&
      typeof window !== "undefined" &&
      new URLSearchParams(window.location.search).get("forceAmendment") === "1"
  );
  useEffect(() => {
    if (forceAmendmentRef.current && !devForcedShown.amendment) {
      devForcedShown.amendment = true;
      (async () => {
        try {
          const s = await getEulaStatus();
          const payload: EulaEmitPayload = {
            mode: "amendment",
            eulaVersion: "2.0",
            pinnedVersion: "1.0",
            eulaText: s.bundled_text ?? "",
            eulaSha256: s.bundled_sha256 ?? "",
            softwareVersion: s.software_version,
            changeBullets: ["Sample change A", "Sample change B"],
          };
          console.log(`[EULA] forceAmendment inline eula_version=${payload.eulaVersion} pinned_version=${payload.pinnedVersion}`);
          setAmendmentInlinePayload(payload);
        } catch (e) {
          console.error(`[EULA] forceAmendment error error_type=${errorType(e)}`);
        }
      })();
    }
  }, []);

  // Dev-only hook for Playwright verification of the EULA re-acknowledgement
  // inline flow. Loading the page with ?forceReacknowledge=1 fetches real EULA
  // data and sets a synthetic re-acknowledge payload rendered inline over the
  // main UI. Guarded by import.meta.env.DEV so it never ships in production.
  const forceReacknowledgeRef = useRef<boolean>(
    import.meta.env.DEV &&
      typeof window !== "undefined" &&
      new URLSearchParams(window.location.search).get("forceReacknowledge") === "1"
  );
  useEffect(() => {
    if (forceReacknowledgeRef.current && !reacknowledgeInlineShown) {
      reacknowledgeInlineShown = true;
      (async () => {
        try {
          const s = await getEulaStatus();
          const payload: EulaEmitPayload = {
            mode: "reacknowledge",
            eulaText: s.bundled_text ?? "",
            eulaVersion: s.bundled_version ?? "",
            eulaSha256: s.bundled_sha256 ?? "",
            softwareVersion: s.software_version,
            changeBullets: s.summary ?? [],
          };
          console.log(`[EULA] forceReacknowledge inline eula_version=${payload.eulaVersion}`);
          setReacknowledgeInlinePayload(payload);
        } catch (e) {
          console.error(`[EULA] forceReacknowledge error error_type=${errorType(e)}`);
        }
      })();
    }
  }, []);

  // Dev-only hook for Playwright verification of the error dialog popup.
  // Loading the page with ?forceErrorDialog=<code> (one of the ErrorCode
  // values) immediately shows the popup with locked copy for that code, without
  // needing a real backend error event. Guarded by import.meta.env.DEV so it
  // never ships in production bundles.
  const devErrorDialogCopy: Record<string, { title: string; message: string; retryAction: string }> = {
    no_input_device:      { title: "No microphone found",                   message: "Plug in a microphone to start recording.",                                                                    retryAction: "rescan_devices"   },
    backend_disconnect:   { title: "Whisperstream stopped responding",       message: "Restart Whisperstream to recover. Antivirus software can sometimes interrupt it.",                            retryAction: "recheck_health"   },
    model_load_failed:    { title: "Couldn't load the speech model",         message: "Restart Whisperstream to try again. You can also re-download the model from settings.",                      retryAction: "reload_model"     },
    stt_engine_failed:    { title: "Speech model stopped",                  message: "Whisperstream is still running, but the speech model couldn't recover. Reload the model to try again.",     retryAction: "reload_model"     },
    audio_capture_failed: { title: "Couldn't start recording",              message: "Check that your microphone is connected, then try again.",                                                    retryAction: "restart_capture"  },
  };
  const forceErrorDialogRef = useRef<string | null>(
    import.meta.env.DEV &&
      typeof window !== "undefined"
      ? new URLSearchParams(window.location.search).get("forceErrorDialog")
      : null
  );
  useEffect(() => {
    const code = forceErrorDialogRef.current;
    if (code && devErrorDialogCopy[code] && !devForcedShown.errorDialog) {
      devForcedShown.errorDialog = true;
      const copy = devErrorDialogCopy[code];
      console.log("[Popup] dev force-trigger code=" + code);
      (async () => {
        try {
          await showPopup("popup-error", {
            code: code as ErrorCode,
            title: copy.title,
            message: copy.message,
            retryAction: copy.retryAction as RetryAction,
          });
        } catch {
          /* not in Tauri context */
        }
      })();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Production trigger: when eulaStatus indicates a pending amendment (and first-run
  // is already cleared), render the amendment EULA inline over the main UI.
  // The amendmentInlineShown latch prevents this from firing more than once per
  // page load under React StrictMode double-invocation.
  useEffect(() => {
    if (
      eulaStatus?.needs_amendment === true &&
      eulaStatus.needs_first_run === false &&
      !amendmentInlineShown
    ) {
      amendmentInlineShown = true;
      const payload: EulaEmitPayload = {
        mode: "amendment",
        eulaText: eulaStatus.bundled_text ?? "",
        eulaVersion: eulaStatus.bundled_version ?? "",
        eulaSha256: eulaStatus.bundled_sha256 ?? "",
        softwareVersion: eulaStatus.software_version,
        pinnedVersion: eulaStatus.pinned_version ?? undefined,
        // changeBullets are not shipped by the backend yet; AmendmentView
        // gracefully renders without them (hasBullets guard).
      };
      console.log(`[EULA] amendment inline trigger eula_version=${payload.eulaVersion} pinned_version=${payload.pinnedVersion}`);
      setAmendmentInlinePayload(payload);
    }
  }, [eulaStatus]);

  // Production trigger: when eulaStatus indicates a pending re-acknowledgement
  // (and neither first-run nor a material amendment is pending), render the
  // accept-or-exit re-acknowledgement prompt inline over the main UI. The
  // material amendment flow takes precedence: the backend returns
  // needs_reacknowledgement=false during a major bump, so the two never fire
  // together. The reacknowledgeInlineShown latch guards StrictMode double-fire.
  useEffect(() => {
    if (
      eulaStatus?.needs_reacknowledgement === true &&
      eulaStatus.needs_first_run === false &&
      eulaStatus.needs_amendment === false &&
      !reacknowledgeInlineShown
    ) {
      reacknowledgeInlineShown = true;
      const payload: EulaEmitPayload = {
        mode: "reacknowledge",
        eulaText: eulaStatus.bundled_text ?? "",
        eulaVersion: eulaStatus.bundled_version ?? "",
        eulaSha256: eulaStatus.bundled_sha256 ?? "",
        softwareVersion: eulaStatus.software_version,
        changeBullets: eulaStatus.summary ?? [],
      };
      console.log(`[EULA] reacknowledge inline trigger eula_version=${payload.eulaVersion}`);
      setReacknowledgeInlinePayload(payload);
    }
  }, [eulaStatus]);

  // Production trigger: show the backend-disconnect error popup once the
  // disconnect threshold is reached (5s elapsed AND 2+ reconnect attempts).
  // The module-level latch prevents double-fire under StrictMode and resets
  // when the connection recovers so a subsequent disconnect can re-trigger.
  useEffect(() => {
    if (
      !connected &&
      everConnected &&
      lastDisconnectAt !== null &&
      Date.now() - lastDisconnectAt > 5000 &&
      reconnectAttempt >= 2 &&
      !backendDisconnectPopupShown
    ) {
      backendDisconnectPopupShown = true;
      console.warn(`[App] backend disconnect threshold reached attempt=${reconnectAttempt}`);
      showPopup("popup-error", {
        code: "backend_disconnect",
        title: "Whisperstream stopped responding",
        message: "Restart Whisperstream to recover. Antivirus software can sometimes interrupt it.",
        retryAction: "recheck_health",
      });
    }
    if (connected) {
      backendDisconnectPopupShown = false;
    }
  }, [connected, everConnected, reconnectAttempt, lastDisconnectAt]);

  // popup-update:fetch responder; see lastUpdatePayload.
  useEffect(() => {
    let unlistenPromise: Promise<() => void> | null = null;
    try {
      unlistenPromise = listen("popup-update:fetch", () => {
        if (lastUpdatePayload) {
          console.log(`[App] popup-update fetch responding version=${lastUpdatePayload.version}`);
          emit("popup-update:show", lastUpdatePayload);
        }
      });
    } catch {
      /* not in Tauri context */
    }
    return () => {
      unlistenPromise?.then((fn) => fn()).catch(() => {});
    };
  }, []);

  // popup-error:fetch responder. The popup window asks for the latest error
  // payload after its data listener is active, recovering from the IPC race
  // where the original :data emit landed before the popup's listener was
  // registered. Mirrors EulaPopup's getEulaStatus() fallback pattern.
  useEffect(() => {
    // Tauri-only IPC listener. In a plain browser (Playwright / Vite dev
    // outside the Tauri shell) listen() throws synchronously — guard so
    // dev screenshots don't trip the error boundary.
    let unlistenPromise: Promise<() => void> | null = null;
    try {
      unlistenPromise = listen("popup-error:fetch", () => {
        if (lastErrorPayload) {
          console.log(`[App] popup-error fetch responding code=${lastErrorPayload.code}`);
          emit("popup-error:data", lastErrorPayload);
        } else {
          console.warn("[App] popup-error fetch received but no payload stored");
        }
      });
    } catch {
      /* not in Tauri context */
    }
    return () => {
      unlistenPromise?.then((fn) => fn()).catch(() => {});
    };
  }, []);

  useEffect(() => {
    return subscribe((event) => {
      if (event.type === "state.arming") setDictationActivity((s) => ({ ...s, arming: event.active }));
      if (event.type === "state.recording") setDictationActivity((s) => ({ ...s, recording: event.recording }));
      if (event.type === "state.transcribing") setDictationActivity((s) => ({ ...s, transcribing: event.active }));
      if (event.type === "dictation.state") setDictationActivity({ arming: false, recording: event.phase === "recording", transcribing: event.phase === "processing" });
      if (event.type === "model.missing") {
        console.warn(`[App] model missing model_name=${event.model_name}`);
        // Onboarding handles model download itself; don't pop the legacy
        // DownloadDialog on top of it. Note that `model.missing` can arrive
        // BEFORE the initial GET /api/config resolves, so configRef.current
        // may be null here. Strict positive match (loaded config AND setup
        // genuinely complete) treats both null and false as "do not show".
        if (configRef.current !== null && configRef.current.setup_completed === true) {
          setDownloadModel(event.model_name);
        }
      }
      if (event.type === "download.complete" && event.kind === undefined) {
        setModelLoaded(true);
      }
      if (event.type === "model.loaded") {
        setModelLoaded(true);
      }
      if (event.type === "model.load_failed") {
        console.error("[App] model load failed error_type=RemoteError");
        setModelLoaded(false);
      }
      if (event.type === "stt.failure") {
        console.warn(
          `[STT] failure code=${event.code} backend=${event.backend} operation=${event.operation} recovery_attempted=${event.recovery_attempted} suppressed=${event.suppressed}`,
        );
        setModelLoaded(false);
        showPopup("popup-error", sttFailureToErrorPayload(event));
      }
      if (event.type === "recording.blocked") {
        if (event.reason === "eula_acceptance_required") {
          console.warn("[EULA] content blocked action=refresh_status");
          getEulaStatus()
            .then(onConsentRequired)
            .catch((err) => console.error(`[EULA] blocked status refresh failed error_type=${errorType(err)}`));
        }
      }
      if (event.type === "review.prompt") {
        console.log("[Review] prompt triggered");
        showPopup("popup-review");
      }
      if (event.type === "error.dialog") {
        console.warn(`[App] error dialog received code=${event.code}`);
        showPopup("popup-error", {
          code: event.code as ErrorCode,
          title: event.title,
          message: event.message,
          retryAction: event.retry_action as RetryAction | undefined,
        });
      }
    });
  }, [subscribe, onConsentRequired]);

  useEffect(() => {
    getConfig()
      .then((c) => { console.log("[App] config loaded"); setConfig(c); })
      .catch((e) => { console.error(`[App] config load failed error_type=${errorType(e)}`); });
    Promise.all([getDailyUseCapabilities(), getAdvancedCapabilities()])
      .then(([dailyReport, advancedReport]) => {
        setDailyCapabilities(parseDailyUseCapabilityReport(dailyReport));
        setAdvancedCapabilities(parseAdvancedCapabilityReport(advancedReport));
        setCapabilitiesError(false);
        console.log("[App] capability reports loaded daily_schema=1 advanced_schema=1");
      })
      .catch((e) => {
        setCapabilitiesError(true);
        console.error(`[App] capability reports failed error_type=${errorType(e)}`);
      });
    getStartupState()
      .then(setStartupStatus)
      .catch(() => {});
    getHealth()
      .then((h) => setModelLoaded(h.model_loaded))
      .catch(() => {});
    // Dev-tools probe: only fetch in dev Vite builds. Production builds
    // dead-code-eliminate this entire branch via import.meta.env.DEV so
    // the tab can never be offered. getDevEnabled is already resilient
    // (returns { enabled: false } on failure) so no try/catch needed.
    if (import.meta.env.DEV) {
      getDevEnabled().then((r) => setDevEnabled(r.enabled));
    }
    // Experimental probe: only fetch in experimental builds (dev, or a
    // --experimental dogfood build). Production bundles dead-code-eliminate this
    // branch via EXPERIMENTAL_BUILD. getExperimentalEnabled is resilient.
    if (EXPERIMENTAL_BUILD) {
      getExperimentalEnabled().then((r) => setExperimentalEnabled(r.enabled));
    }
  }, []);

  // Recovery probe: if onboarding already completed but the active STT model is
  // absent on disk (never re-downloaded, deleted, moved, or a stale symlink
  // whose target is gone), deterministically surface the download flow. The
  // backend's `model.missing` WS event is sent once on connect and races the
  // initial getConfig() — on a cold start it lands ~1ms BEFORE config resolves
  // and is dropped while configRef is still null (see lib/missingModelRace).
  // StrictMode's WS reconnect masks this in dev but not in a production build.
  // Re-checking disk presence here, AFTER config is known, closes that gap using
  // the SAME source of truth the backend uses (stt_model_status -> is_present,
  // which reads a dangling symlink as missing). Onboarding owns the fresh-install
  // case (setup_completed === false); the live `model.missing` handler covers a
  // model deleted while the app is already running.
  //
  // Depend on the derived `setupComplete` boolean, NOT on `config` itself: the
  // effect must fire exactly once on the false->true transition and must NOT
  // re-run on later config refetches (the WS-connect refetch would otherwise
  // re-run this while getSttStatus is in flight, and the re-run's cleanup would
  // set cancelled=true and swallow the only probe — the same class of race this
  // is meant to fix). The model name is read from configRef so config need not
  // be a dependency.
  const setupComplete = config?.setup_completed === true;
  useEffect(() => {
    if (!setupComplete) return;
    let cancelled = false;
    getSttStatus()
      .then((status) => {
        if (cancelled) return;
        // Authoritative exact-artifact status. Retained inactive artifacts are
        // irrelevant here: the only question is whether the artifact this
        // config will load is actually on disk.
        const configured = status.artifacts.find(
          (artifact) => artifact.id === status.configured_artifact_id,
        );
        if (configured !== undefined && !configured.downloaded) {
          console.warn(`[App] configured STT model missing on disk artifact=${configured.id}; surfacing download`);
          // DownloadDialog auto-starts the download on mount; this value only
          // gates the modal (the backend resolves the target itself). Guard
          // with prev ?? so a model.missing event that already opened the
          // dialog is never clobbered.
          setDownloadModel((prev) => prev ?? configured.id);
        }
      })
      .catch((err) => {
        console.error(`[App] stt status probe failed error_type=${errorType(err)}`);
      });
    return () => {
      cancelled = true;
    };
  }, [setupComplete]);

  // Apply the loaded/changed theme to the document root and persist it to
  // localStorage so the next launch's inline script (index.html) reads it
  // without a flash. Popups reload per-open so they pick this up from the
  // shared localStorage origin; the HUD is a persistent window created once at
  // startup, so it needs the "theme-changed" event below to update live.
  useEffect(() => {
    if (config?.theme) {
      document.documentElement.setAttribute('data-theme', config.theme);
      localStorage.setItem('ws-theme', config.theme);
      emit('theme-changed', config.theme).catch(() => {});
    }
  }, [config?.theme]);

  // The HUD is a persistent webview, so mirror the effective, master-gated
  // preference through shared localStorage for startup and a Tauri event for
  // immediate updates. The raw child preference remains untouched in config.
  useEffect(() => {
    if (!config) return;
    const visible =
      config.show_recording_indicator && config.show_recording_control;
    localStorage.setItem(
      RECORDING_CONTROL_VISIBILITY_STORAGE_KEY,
      String(visible),
    );
    void (async () => {
      if (visible) {
        await ensureRecordingControlWindow();
      }
      await emit(RECORDING_CONTROL_VISIBILITY_EVENT, visible);
    })().catch(() => {});
  }, [config?.show_recording_indicator, config?.show_recording_control]);

  // Seed the persistent HUD with the accessibility state discovered while
  // loading config. The HUD also probes the lightweight runtime endpoint so a
  // screen reader opening or closing while idle is reflected without requiring
  // a recording or a main-window refresh.
  useEffect(() => {
    if (typeof config?.screen_reader_detected !== "boolean") return;
    localStorage.setItem(
      SCREEN_READER_STATUS_STORAGE_KEY,
      String(config.screen_reader_detected),
    );
    emit(SCREEN_READER_STATUS_EVENT, config.screen_reader_detected).catch(
      () => {},
    );
  }, [config?.screen_reader_detected]);

  // AnimatePresence fallback: if the dev tab disappears at runtime (env var
  // unset between sessions, hot-reload race) while it's the active tab,
  // route back to home so AnimatePresence keyed on `tab` doesn't crash.
  useEffect(() => {
    if (tab === "ui-test" && !devTabVisible) {
      setTab("home");
    }
  }, [tab, devTabVisible]);

  useEffect(() => {
    if (
      !config ||
      eulaStatus?.needs_first_run !== true ||
      eulaFirstRunPopupShown
    ) {
      return;
    }
    eulaFirstRunPopupShown = true;
    console.log("[EULA] showing first_run popup mode=inline");
    // Run after React commits the inline EULA surface. This keeps the packaged
    // readiness proof independent of background WebKit timer scheduling.
    void flushLogSync();
  }, [config, eulaStatus]);

  // Listen for the popup's "eula-accepted" handoff. When received, re-fetch
  // status so `needs_first_run` flips to false and the gate releases, and
  // clear any inline amendment overlay (for the amendment accept path).
  // This is the single post-accept hydration signal — no polling.
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    (async () => {
      try {
        unlisten = await listen("eula-accepted", async () => {
          console.log("[EULA] accepted event received action=refetch_status");
          setAmendmentInlinePayload(null);
          setReacknowledgeInlinePayload(null);
          try {
            const next = await getEulaStatus();
            setEulaStatus(next);
          } catch (e) {
            console.error(`[EULA] post_accept status refetch failed error_type=${errorType(e)}`);
          }
        });
      } catch {
        /* not in Tauri context */
      }
    })();
    return () => {
      if (unlisten) unlisten();
    };
  }, []);

  useEffect(() => {
    if (connected && (!config || !dailyCapabilities || !advancedCapabilities)) {
      getConfig()
        .then((c) => { console.log("[App] config loaded"); setConfig(c); })
        .catch(() => {});
      if (!dailyCapabilities || !advancedCapabilities) {
        Promise.all([getDailyUseCapabilities(), getAdvancedCapabilities()])
          .then(([dailyReport, advancedReport]) => {
            setDailyCapabilities(parseDailyUseCapabilityReport(dailyReport));
            setAdvancedCapabilities(parseAdvancedCapabilityReport(advancedReport));
            setCapabilitiesError(false);
          })
          .catch(() => setCapabilitiesError(true));
      }
      getStartupState()
        .then(setStartupStatus)
        .catch(() => {});
    }
  }, [connected, config, dailyCapabilities, advancedCapabilities, eulaStatus]);

  // The official installer's updater schedules its own checks; source builds have none.
  useEffect(() => {
    if (!officialUpdater || !capabilityDecision(dailyCapabilities, "auto-update").enabled) return;
    if (resolveEulaRequirementState(eulaStatus) !== "ready" || config?.setup_completed !== true || lockStatus.enabled) return;
    return officialUpdater.startAutoCheck((update) =>
      showPopup("popup-update", { version: update.version, body: update.body ?? "", source: "automatic" }));
  }, [config?.setup_completed, dailyCapabilities, eulaStatus, lockStatus.enabled]);

  const showStatus = useCallback((message: string, isError: boolean) => {
    setStatus({ message, isError });
    if (statusTimer.current) clearTimeout(statusTimer.current);
    statusTimer.current = setTimeout(() => setStatus(null), 4000);
  }, []);

  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingConfigPatch = useRef<Partial<AppConfig>>({});
  const configEditRevision = useRef(0);
  // Serialize writes so a slower prior settings save cannot overwrite a
  // recipient the user has just reviewed and enabled.
  const configSaveQueue = useRef(createConfigSaveQueue(saveConfig));

  // Open the lowest-priority spotlight gate a beat after config is ready, giving
  // the launch-time update / review popups the first moment.
  const configLoaded = config != null;
  useEffect(() => {
    if (!configLoaded) return;
    const t = setTimeout(() => setSpotlightGateOpen(true), 1800);
    return () => clearTimeout(t);
  }, [configLoaded]);

  const patchConfig = useCallback(
    (patch: Partial<AppConfig>) => {
      setConfig((prev) => {
        if (!prev) return prev;
        const next = { ...prev, ...patch };
        pendingConfigPatch.current = { ...pendingConfigPatch.current, ...patch };
        configEditRevision.current += 1;
        const isDeviceChange = "audio_input_device" in patch;
        if (saveTimer.current) clearTimeout(saveTimer.current);
        const doSave = async () => {
          const patchToSave = pendingConfigPatch.current;
          pendingConfigPatch.current = {};
          const savedRevision = configEditRevision.current;
          try {
            const saved = await configSaveQueue.current.save(patchToSave);
            if (configEditRevision.current === savedRevision) setConfig(saved);
            setConfigSaveRevision((revision) => revision + 1);
            console.log(`[Config] save result=success keys_changed=${Object.keys(patch).join(",")}`);
            flushLogSync().catch(() => undefined);
          } catch (err) {
            pendingConfigPatch.current = {
              ...patchToSave,
              ...pendingConfigPatch.current,
            };
            console.error(`[Config] save failed error_type=${errorType(err)}`);
            flushLogSync().catch(() => undefined);
            showStatus(
              enhancementErrorMessage(err, "Could not save settings. Check the values and try again."),
              true
            );
          }
        };
        if (isDeviceChange) {
          doSave();
        } else {
          saveTimer.current = setTimeout(doSave, 500);
        }
        return next;
      });
    },
    [showStatus]
  );

  // Agent installation must observe its selected surface before enabling.
  // Drain earlier debounced edits through the same queue rather than racing them.
  const commitAgentConfig = useCallback(async (patch: Partial<AppConfig>): Promise<void> => {
    if (!configRef.current) throw new Error("config_unavailable");
    if (saveTimer.current) { clearTimeout(saveTimer.current); saveTimer.current = null; }
    const pending = pendingConfigPatch.current;
    pendingConfigPatch.current = {};
    const savedRevision = ++configEditRevision.current;
    try {
      const saved = await configSaveQueue.current.save({ ...pending, ...patch });
      if (configEditRevision.current === savedRevision) setConfig(saved);
      else setConfig((current) => current ? { ...current, ...patch } : current);
      setConfigSaveRevision((revision) => revision + 1);
    } catch (error) {
      pendingConfigPatch.current = { ...pending, ...pendingConfigPatch.current };
      throw error;
    }
  }, []);

  const importDictionary = useCallback(
    async (buildPatch: (latest: AppConfig) => Partial<AppConfig>): Promise<void> => {
      const base = configRef.current;
      if (!base) throw new Error("config_unavailable");
      const pending = pendingConfigPatch.current;
      const importPatch = buildPatch({ ...base, ...pending });
      if (saveTimer.current) {
        clearTimeout(saveTimer.current);
        saveTimer.current = null;
      }
      const patchToSave = { ...pending, ...importPatch };
      pendingConfigPatch.current = {};
      const savedRevision = ++configEditRevision.current;
      try {
        const saved = await configSaveQueue.current.save(patchToSave);
        if (configEditRevision.current === savedRevision) setConfig(saved);
        setConfigSaveRevision((revision) => revision + 1);
      } catch (error) {
        // Failed imports stay staged in the modal. Restore unrelated pending
        // settings, but never silently retry the import after cancellation.
        pendingConfigPatch.current = { ...pending, ...pendingConfigPatch.current };
        throw error;
      }
    }, [],
  );

  // Re-fetch the full config from the backend. Used after a relocate persists a
  // new model_storage_dir out-of-band (the worker writes config.toml directly),
  // so our in-memory config matches the backend. Without this the storage row
  // shows a stale "Default location" and the next patchConfig PUT would clobber
  // model_storage_dir back to its stale value.
  const reloadConfig = useCallback(() => {
    getConfig()
      .then(setConfig)
      .catch((err) => {
        console.error(
          `[Config] reload failed error_type=${errorType(err)}`,
        );
      });
  }, []);

  const handleStartupChange = useCallback(
    async (enabled: boolean) => {
      const previous = startupStatus;
      setStartupStatus({ state: enabled ? "enabled" : "disabled", failure: null });
      const outcome = await applyStartupChange(previous, enabled);
      setStartupStatus(outcome.status);
      if (!outcome.failed) {
        if (outcome.status.state === "requires-approval") {
          showStatus("Approve Whisperstream in System Settings to finish enabling startup.", true);
        }
      } else {
        showStatus(
          `Failed to update startup: ${outcome.error instanceof Error ? outcome.error.message : outcome.error}`,
          true
        );
      }
    },
    [showStatus, startupStatus]
  );

  const qwenStt = capabilityDecision(dailyCapabilities, "qwen-stt");
  const isLanguageAvailable = useCallback(
    (code: string) => qwenStt.enabled || getModelsForLanguage(code).includes("parakeet"),
    [qwenStt.enabled],
  );

  // ----- Language transitions, model activation, and confirm-then-download ---

  // Persist one COMPLETE routing state immediately.
  //
  // Routing never rides patchConfig's 500ms debounce. The backend refuses to
  // install a prepared recognizer whose target routing no longer matches the
  // saved config, so a late debounced PUT could invalidate an activation that
  // is already running. Saving all five fields together is what makes the
  // backend's staleness check meaningful.
  const commitRouting = useCallback(
    async (patch: SttRoutingPatch): Promise<AppConfig | null> => {
      const base = configRef.current;
      if (!base) return null;
      if (saveTimer.current) {
        clearTimeout(saveTimer.current);
        saveTimer.current = null;
      }
      const patchToSave = { ...pendingConfigPatch.current, ...patch };
      pendingConfigPatch.current = {};
      const savedRevision = ++configEditRevision.current;
      const saved = await configSaveQueue.current.save(patchToSave);
      if (configEditRevision.current === savedRevision) setConfig(saved);
      setConfigSaveRevision((revision) => revision + 1);
      return saved;
    },
    [dailyCapabilities?.platform],
  );

  // Undo a routing save the backend never took ownership of.
  //
  // The activation worker restores the complete prior state itself once it
  // starts, but a REJECTED start (dictation live, another model operation in
  // flight) never reaches it. Without this, config would name a model the
  // process is not running and the recognizer would silently disagree with the
  // UI until the next successful switch.
  const restoreRoutingAfterRejectedStart = useCallback(
    async (previous: AppConfig | null) => {
      const current = configRef.current;
      if (!previous || !current) {
        reloadConfig();
        return;
      }
      try {
        // Only the five routing fields are rolled back, over whatever the
        // config is now, so an unrelated setting saved in the same window is
        // not clobbered by a stale whole-config write.
        const savedRevision = ++configEditRevision.current;
        const saved = await configSaveQueue.current.save({
            language: previous.language,
            stt_model_selection_mode: previous.stt_model_selection_mode,
            stt_backend: previous.stt_backend,
            parakeet_model: previous.parakeet_model,
            qwen_force_language: previous.qwen_force_language,
          });
        if (configEditRevision.current === savedRevision) setConfig(saved);
        setConfigSaveRevision((revision) => revision + 1);
      } catch (err) {
        console.error(`[Stt] routing restore failed error_type=${errorType(err)}`);
        reloadConfig();
      }
    },
    [reloadConfig],
  );

  const reportRoutingSaveFailure = useCallback(
    (err: unknown, modelName: string) => {
      console.error(`[Stt] routing save failed error_type=${errorType(err)}`);
      showStatus(sttRoutingSaveFailureMessage(modelName), true);
      reloadConfig();
    },
    [reloadConfig, showStatus],
  );

  // The single entry point for every model change.
  //
  // The decision is made against AUTHORITATIVE backend state, never against
  // config. `stt_backend` / `parakeet_model` name the configured target; only
  // `loaded_artifact_id` answers "what is the process actually running". They
  // disagree mid-transition, after a failed activation whose rollback could not
  // be written, and before the first recognizer exists, which is exactly when
  // shortening a switch to a routing-only save would strand the user on a model
  // the UI says was replaced.
  //
  // Absent targets go through the confirm-then-download dialog; present targets
  // are activated in place. Both end in the same backend
  // prepare/construct/atomic-swap operation, because saving config on its own
  // leaves the old recognizer loaded.
  const applySttRouting = useCallback(
    async (
      artifact: ActiveSttArtifact,
      patch: SttRoutingPatch,
      logKey: string,
    ) => {
      const registryId = sttArtifactRegistryId(artifact);
      let loadedArtifactId: string | null | undefined;
      let downloaded: boolean | null = null;
      try {
        const status = await getSttStatus();
        loadedArtifactId = status.loaded_artifact_id;
        downloaded =
          status.artifacts.find((entry) => entry.id === registryId)
            ?.downloaded ?? false;
      } catch (err) {
        // Unknown state fails toward the confirm dialog so a genuinely missing
        // model is never silently skipped. Preparation is idempotent for files
        // that are already there.
        console.warn(`[Stt] status probe failed error_type=${errorType(err)}`);
      }

      const action = resolveSttExecution({
        target: artifact,
        loadedArtifactId,
        downloaded,
      });

      if (action === "download") {
        console.log(`[Stt] ${logKey} artifact=${artifact} action=confirm_download`);
        setPendingDownload({
          backend: sttArtifactBackend(artifact),
          artifact,
          patch,
        });
        return;
      }

      if (action === "commit") {
        // The recognizer in memory IS the target, so only routing is saved.
        console.log(`[Stt] ${logKey} artifact=${artifact} action=commit_routing`);
        try {
          await commitRouting(patch);
        } catch (err) {
          reportRoutingSaveFailure(err, getSttArtifactDisplayName(artifact));
        }
        return;
      }

      const modelName = getSttArtifactDisplayName(artifact);
      console.log(`[Stt] ${logKey} artifact=${artifact} action=activate_present`);
      const activation = { artifact, modelName };
      sttActivationRef.current = activation;
      setSttActivation(activation);
      const previous = configRef.current;
      try {
        await commitRouting(patch);
        // Armed BEFORE the POST: the worker can finish and broadcast its
        // terminal event before the awaited response even resolves.
        sttSwitchRef.current = sttSwitchStarted();
        await startDownload();
      } catch (err) {
        console.error(`[Stt] activation start failed error_type=${errorType(err)}`);
        sttActivationRef.current = null;
        setSttActivation(null);
        sttSwitchRef.current = sttSwitchReleased();
        await restoreRoutingAfterRejectedStart(previous);
        showStatus(sttStartRejectionMessage(failureDetail(err), modelName), true);
      }
    },
    [
      commitRouting,
      reportRoutingSaveFailure,
      restoreRoutingAfterRejectedStart,
      showStatus,
    ],
  );

  // Settle an STT switch from the authoritative backend lifecycle events.
  // Untagged download events belong to the STT operation; local-LLM and TTS
  // downloads carry a `kind`.
  //
  // This is the ONLY place a switch stops being owed a rollback. A failure or
  // cancel means the worker already restored the previous routing state before
  // reporting, so the frontend re-reads config and the artifact inventory
  // instead of trusting its own optimistic copy. Tracking lives in a ref rather
  // than in the dialog state precisely so a cancel that answered `cancelling`
  // (worker still running) is still reconciled when its event finally lands.
  useEffect(() => {
    return subscribe((event) => {
      if (
        event.type !== "download.complete" &&
        event.type !== "download.error" &&
        event.type !== "download.cancelled"
      ) {
        return;
      }
      if (event.kind !== undefined) return;

      const activation = sttActivationRef.current;
      const decision = sttTerminalDecision(
        sttSwitchRef.current,
        event.type as SttTerminalEvent,
      );
      sttSwitchRef.current = decision.next;
      if (decision.reloadConfig) reloadConfig();
      if (decision.refreshInventory) setSttInventoryRevision((n) => n + 1);

      if (event.type === "download.error" && activation !== null) {
        showStatus(
          sttSwitchFailureMessage(
            activation.modelName,
            event.reason,
            event.restored,
          ),
          true,
        );
      }

      if (activation !== null) {
        sttActivationRef.current = null;
        setSttActivation(null);
      }
    });
  }, [reloadConfig, showStatus, subscribe]);

  const handleLanguagePicked = useCallback(
    async (code: string) => {
      setLanguagePopupOpen(false);
      if (!config) return;
      if (sttActivation !== null || pendingDownload !== null) {
        console.log("[Stt] language pick ignored reason=operation_in_flight");
        return;
      }

      // A language dialog may say "current" only when the backend can name
      // the recognizer actually loaded in this process. Config names a target,
      // not necessarily the running artifact, and can disagree after a failed
      // or still-settling switch. A failed probe or authoritative `null`
      // therefore supplies no current artifact and routes quietly to the
      // recommendation instead of inventing a keep-current choice.
      let loadedArtifact: ActiveSttArtifact | null = null;
      try {
        const status = await getSttStatus();
        loadedArtifact = sttArtifactFromRegistryId(status.loaded_artifact_id);
      } catch (err) {
        console.warn(
          `[Stt] language status probe failed error_type=${errorType(err)}`,
        );
      }

      const plan = planLanguageChange({
        currentLanguage: config.language,
        targetLanguage: code,
        selectionMode: config.stt_model_selection_mode,
        activeBackend: config.stt_backend,
        parakeetModel: config.parakeet_model,
        loadedArtifact,
        qwenForceLanguage: config.qwen_force_language,
      });

      if (plan.action === "none") return;

      if (plan.action === "ask") {
        // Nothing is persisted and no model is probed until the user answers.
        setPendingLanguageGuidance({
          kind: plan.kind,
          targetLanguage: code,
          currentArtifact: plan.currentArtifact,
          recommendedArtifact: plan.recommendedArtifact,
        });
        return;
      }

      // Both `commit` and `activate` go through the one applier. The planner
      // has already used the loaded artifact for its user-facing decision; the
      // applier probes again immediately before execution so a recognizer that
      // changed in between cannot be left behind by a routing-only save.
      await applySttRouting(
        plan.artifact,
        plan.patch,
        `language picked code=${code}`,
      );
    },
    [applySttRouting, config, pendingDownload, sttActivation],
  );

  // The Advanced `Choose speech model manually` toggle. Turning it on pins the
  // exact artifact already serving the user; turning it off returns to
  // Automatic, which clears Qwen forcing and resolves the recommended artifact
  // for the selected language.
  const handleSelectionModeChange = useCallback(
    async (manual: boolean) => {
      const current = configRef.current;
      if (!current) return;
      if (sttActivation !== null || pendingDownload !== null) {
        console.log("[Stt] selection mode change ignored reason=operation_in_flight");
        return;
      }
      let loadedArtifact: ActiveSttArtifact | null | undefined;
      if (manual) {
        try {
          const status = await getSttStatus();
          loadedArtifact = sttArtifactFromRegistryId(status.loaded_artifact_id);
        } catch (err) {
          console.warn(
            `[Stt] selection mode status probe failed error_type=${errorType(err)}`,
          );
          reportRoutingSaveFailure(err, "your current model");
          return;
        }
      }
      const resolved = resolveSelectionModeChange({
        manual,
        language: current.language,
        activeBackend: current.stt_backend,
        parakeetModel: current.parakeet_model,
        loadedArtifact,
      });
      await applySttRouting(
        resolved.artifact,
        sttRoutingPatch({
          artifact: resolved.artifact,
          language: current.language ?? "",
          mode: resolved.mode,
          qwenForceLanguage: current.qwen_force_language ?? false,
          currentParakeetModel: current.parakeet_model,
        }),
        `selection mode manual=${manual} mode=${resolved.mode}`,
      );
    },
    [applySttRouting, pendingDownload, reportRoutingSaveFailure, sttActivation],
  );

  // A Manual picker choice. The picker groups Parakeet's two artifacts under
  // one name, so the choice resolves the same way Automatic would for this
  // language (English -> English-only Parakeet, otherwise multilingual).
  const handleSttBackendChange = useCallback(
    async (backend: SttBackend) => {
      const current = configRef.current;
      if (!current) return;
      if (sttActivation !== null || pendingDownload !== null) {
        console.log("[Stt] model pick ignored reason=operation_in_flight");
        return;
      }
      const artifact = sttArtifactForBackendChoice(backend, current.language);
      await applySttRouting(
        artifact,
        sttRoutingPatch({
          artifact,
          language: current.language ?? "",
          mode: "manual",
          qwenForceLanguage: current.qwen_force_language ?? false,
          currentParakeetModel: current.parakeet_model,
        }),
        `model picked backend=${backend}`,
      );
    },
    [applySttRouting, pendingDownload, sttActivation],
  );

  // Organic entry into Manual: change the language, pin the EXACT artifact that
  // is already loaded, and make that state visible in Advanced settings and the
  // Home model row. Ordinarily nothing is activated, because the recognizer in
  // memory is already the pinned one; the applier confirms that against
  // `loaded_artifact_id` rather than assuming it, so a pin taken while the
  // process is running something else still reaches the recognizer.
  const handleKeepCurrentModel = useCallback(async () => {
    const guidance = pendingLanguageGuidance;
    if (guidance?.kind !== "keep-or-switch") return;
    setPendingLanguageGuidance(null);
    // Re-read at the moment the user confirms. The dialog was opened from an
    // authoritative snapshot, but an external lifecycle event can still
    // change the process while it is visible. Pin only what is actually loaded
    // now; if there is no compatible recognizer to keep, follow the already
    // presented recommendation instead of persisting the stale dialog value.
    let loadedArtifact: ActiveSttArtifact | null = null;
    try {
      const status = await getSttStatus();
      loadedArtifact = sttArtifactFromRegistryId(status.loaded_artifact_id);
    } catch (err) {
      console.warn(
        `[Stt] keep-current status probe failed error_type=${errorType(err)}`,
      );
    }
    const plan =
      loadedArtifact !== null &&
      isLanguageSupportedByArtifact(loadedArtifact, guidance.targetLanguage)
        ? planKeepCurrentModel({
            currentArtifact: loadedArtifact,
            targetLanguage: guidance.targetLanguage,
            parakeetModel: configRef.current?.parakeet_model ?? null,
            qwenForceLanguage: configRef.current?.qwen_force_language ?? false,
          })
        : planSwitchToRecommended({
            recommendedArtifact: guidance.recommendedArtifact,
            targetLanguage: guidance.targetLanguage,
            parakeetModel: configRef.current?.parakeet_model ?? null,
          });
    await applySttRouting(
      plan.artifact,
      plan.patch,
      `language keep_current code=${guidance.targetLanguage} mode=manual`,
    );
  }, [applySttRouting, pendingLanguageGuidance]);

  // Both dialog variants land on Automatic and the recommendation for the
  // target language. For an incompatible Manual pin that is also the exit from
  // Manual, which clears the Qwen hint with it.
  const handleSwitchToRecommendedModel = useCallback(async () => {
    const guidance = pendingLanguageGuidance;
    if (guidance === null) return;
    setPendingLanguageGuidance(null);
    const plan = planSwitchToRecommended({
      recommendedArtifact: guidance.recommendedArtifact,
      targetLanguage: guidance.targetLanguage,
      parakeetModel: configRef.current?.parakeet_model ?? null,
    });
    await applySttRouting(
      plan.artifact,
      plan.patch,
      `language ${guidance.kind} code=${guidance.targetLanguage}`,
    );
  }, [applySttRouting, pendingLanguageGuidance]);

  // Escape, backdrop, and `Keep current language`: abandon the language change
  // entirely. Nothing was persisted, so there is nothing to restore.
  const handleDismissLanguageGuidance = useCallback(() => {
    setPendingLanguageGuidance(null);
  }, []);

  const handleConfirmDownload = useCallback(async () => {
    if (!pendingDownload) return;
    setLanguageDownloadInFlight(true);
    // Persist the complete target routing BEFORE starting so the worker
    // prepares and activates the exact artifact the user confirmed.
    const previous = configRef.current;
    try {
      await commitRouting(pendingDownload.patch);
      // Armed BEFORE the POST, for the same reason as the in-place path.
      sttSwitchRef.current = sttSwitchStarted();
      await startDownload();
    } catch (err) {
      console.error(`[Stt] save/start failed error_type=${errorType(err)}`);
      setLanguageDownloadInFlight(false);
      // A refused start never reached the worker, so nothing is owed.
      sttSwitchRef.current = sttSwitchReleased();
      // Rethrow after undoing the save: the dialog owns the error presentation
      // and its Retry re-commits the target from whatever state we leave here.
      await restoreRoutingAfterRejectedStart(previous);
      throw err;
    }
  }, [commitRouting, pendingDownload, restoreRoutingAfterRejectedStart]);

  const handleDownloadComplete = useCallback(() => {
    setPendingDownload(null);
    setLanguageDownloadInFlight(false);
  }, []);

  const handleDownloadCancel = useCallback(() => {
    setPendingDownload(null);
    setLanguageDownloadInFlight(false);
  }, []);

  // Cancel-during-download.
  //
  // The POST joins the worker thread so the frontend's awaited call resolves
  // after cleanup, but that join has a 10 second ceiling: past it the backend
  // answers `cancelling` and the worker is STILL RUNNING, still deleting its
  // `.tmp` files and still restoring the prior routing state. Releasing the
  // pending operation there would drop that rollback, because the terminal
  // event that carries it would find nothing to reconcile. So a `cancelling`
  // answer leaves the dialog in its cancelling state and leaves rollback
  // tracking armed; the WebSocket terminal handler settles it.
  const handleCancelDownload = useCallback(async () => {
    let status: string | undefined;
    let failed = false;
    try {
      status = (await cancelDownload())?.status;
    } catch (err) {
      console.error(`[Download] cancel error error_type=${errorType(err)}`);
      failed = true;
    }

    const decision = sttCancelDecision(sttSwitchRef.current, status, { failed });
    sttSwitchRef.current = decision.next;

    if (decision.reconcileNow) {
      reloadConfig();
      setSttInventoryRevision((n) => n + 1);
    }
    if (!decision.releaseSwitchUi) {
      console.log("[Download] cancel pending reason=worker_running");
      return;
    }
    setPendingDownload(null);
    setLanguageDownloadInFlight(false);
  }, [reloadConfig]);


  // Pre-hydrate gate. Two conditions keep the main UI (Sidebar, WebSocket,
  // tabs, settings) out of the DOM:
  //   1. Config or EULA status is still loading.
  //   2. EULA status says a first-run acceptance is required.
  //
  // Per research.md Legal Constraints #1 (write-before-hide) and #3
  // (focus-bypass guard), the main UI must not mount until acceptance.json
  // is durably written — otherwise a user could Alt+Tab or race around
  // the modal and interact with the app before accepting.
  if (capabilitiesError && (!dailyCapabilities || !advancedCapabilities)) {
    return (
      <CapabilityErrorCurtain
        onRetry={() => {
          setCapabilitiesError(false);
          Promise.all([getDailyUseCapabilities(), getAdvancedCapabilities()])
            .then(([dailyReport, advancedReport]) => {
              setDailyCapabilities(parseDailyUseCapabilityReport(dailyReport));
              setAdvancedCapabilities(parseAdvancedCapabilityReport(advancedReport));
            })
            .catch(() => setCapabilitiesError(true));
        }}
      />
    );
  }

  if (!config || !dailyCapabilities || !advancedCapabilities) {
    return <LoadingCurtain label={connected ? "Loading settings..." : "Loading..."} />;
  }

  if (eulaStatus.needs_first_run) {
    // Render the EULA inline inside the main window. The previous
    // implementation spawned the `popup-eula` Tauri webview, but that window
    // has been intermittently invisible on some systems (transparent +
    // decorations:false + skipTaskbar leaves nothing reachable for the user
    // when paint fails). Inline rendering sidesteps the popup-visibility
    // bug entirely while preserving the gate semantics: the main UI shell
    // (Sidebar, role=main, WebSocket consumers) does not mount until
    // acceptance.json is written and `eula-accepted` is emitted, which the
    // listener effect above catches to refetch status and release the gate.
    const payload: EulaEmitPayload = {
      mode: "first-run",
      eulaText: eulaStatus.bundled_text ?? "",
      eulaVersion: eulaStatus.bundled_version ?? "",
      eulaSha256: eulaStatus.bundled_sha256 ?? "",
      softwareVersion: eulaStatus.software_version,
    };
    return (
      <div
        data-testid="loading-curtain"
        className="w-full h-screen"
        style={{
          background: meshGradient,
          backgroundSize: "200% 200%",
          backgroundPosition: "30% 30%",
        }}
      >
        <EulaPopup inline initialPayload={payload} />
      </div>
    );
  }

  // Onboarding overlay short-circuits the normal sidebar/tabs shell on first
  // install. It lives outside the main render so the regular app doesn't
  // flash behind it while the overlay is mounted.
  if (config.setup_completed === false) {
    return (
      <OnboardingOverlay
        config={config}
        modelLoaded={modelLoaded}
        send={send}
        subscribe={subscribe}
        connected={connected}
        onConfigChange={patchConfig}
        onFinish={() => setConfig((prev) => (prev ? { ...prev, setup_completed: true } : prev))}
        capabilities={dailyCapabilities}
      />
    );
  }

  // One dialog carries every keep-or-switch and incompatible-pin decision. It
  // adapts its own copy when both sides share a user-facing name (a
  // Parakeet-to-Parakeet transition), because the locked scope forbids the
  // English-only / multilingual qualifiers that would otherwise disambiguate
  // them, and naming one model on both sides of the decision is nonsense.
  const switchDialogGuidance = pendingLanguageGuidance;

  // What the Home model row says while a switch runs. A present-artifact
  // activation has no transfer and therefore no download dialog, so this is the
  // only place that names the model being brought up.
  const sttSwitchTargetName =
    sttActivation?.modelName ??
    (languageDownloadInFlight && pendingDownload !== null
      ? getSttArtifactDisplayName(pendingDownload.artifact)
      : null);

  const renderPage = () => {
    switch (tab) {
      case "home":
        return (
          <Home
            config={config}
            startupStatus={startupStatus}
            onStartupChange={handleStartupChange}
            onConfigChange={patchConfig}
            subscribe={subscribe}
            onRequestOpenLanguagePopup={() => setLanguagePopupOpen(true)}
            onRequestLanguageChange={() => {
              /* unused — picker selections route through LanguagePopup -> handleLanguagePicked */
            }}
            downloadInFlight={languageDownloadInFlight || sttActivation !== null}
            switchingToModel={sttSwitchTargetName}
            sttInventoryRevision={sttInventoryRevision}
            onSelectionModeChange={handleSelectionModeChange}
            onSttBackendChange={handleSttBackendChange}
            onShowStatus={showStatus}
            lockStatus={lockStatus}
            capabilities={dailyCapabilities}
            connected={connected}
            onConfigReload={reloadConfig}
            onConfigReplace={setConfig}
            openLockdownSetupSignal={lockdownSetupRequest}
          />
        );
      case "audio":
        return (
          <Audio
            config={config}
            onConfigChange={patchConfig}
            capabilities={dailyCapabilities}
          />
        );
      case "controls":
        return (
          <Controls
            config={config}
            onConfigChange={patchConfig}
            send={send}
            subscribe={subscribe}
            connected={connected}
            onShowStatus={showStatus}
          />
        );
      case "agents":
        return (
          <Agents
            config={config}
            onConfigChange={patchConfig}
            onCommitConfig={commitAgentConfig}
            send={send}
            subscribe={subscribe}
            connected={connected}
            onShowStatus={showStatus}
          />
        );
      case "dictionary":
        return <Dictionary config={config} onConfigChange={patchConfig} onImportConfig={importDictionary} onOpenEnhancement={() => setTab("enhancement")} />;
      case "transcripts":
        return (
          <Transcripts
            config={config}
            onConfigChange={patchConfig}
            subscribe={subscribe}
            devEnabled={devTabVisible}
            activeModelId={config.enhancement_local_model}
            activeModel={activeDevModel}
            lockStatus={lockStatus}
            capabilities={dailyCapabilities}
            recovery={historyRecovery}
            recoverySetupRequest={recoverySetupRequest}
            onRequestLockdownSetup={() => {
              setTab("home");
              setLockdownSetupRequest((n) => n + 1);
            }}
          />
        );
      case "enhancement":
        return (
          <Enhancement
            config={config}
            onConfigChange={patchConfig}
            configSaveRevision={configSaveRevision}
            onRequestPrivacyModal={(provider) => setPrivacyModalProvider(provider)}
            acknowledgedProviders={acknowledgedProviders}
            onConsentChanged={refreshAcknowledgedProviders}
            subscribe={subscribe}
            send={send}
            connected={connected}
            geminiFlashSwitchSignal={geminiFlashSwitch}
            lockStatus={lockStatus}
            onNavigateHome={() => setTab("home")}
            capabilities={dailyCapabilities}
            advancedCapabilities={advancedCapabilities}
          />
        );
      case "about":
        return (
          <About capabilities={dailyCapabilities} lockdownEnabled={lockStatus.enabled} onUpdateAvailable={(update) => showPopup("popup-update", { version: update.version, body: update.body ?? "", source: "manual" })} />
        );
      case "ui-test":
        if (!UiTest) return null;
        return (
          <Suspense fallback={null}>
            <UiTest onShowPopup={showPopup} />
          </Suspense>
        );
    }
  };

  // Navigation surface handed to a spotlight offer's deep-link CTA. The Gemini
  // offer switches provider through Enhancement's consent-safe path (bumping the
  // signal below), never a raw provider write.
  const spotlightNav: SpotlightNav = {
    goToTab: (t) => setTab(t as TabId),
    switchToGeminiFlash: () => {
      setTab("enhancement");
      setGeminiFlashSwitch((n) => n + 1);
    },
    openHistoryRecoverySetup: () => {
      setTab("transcripts");
      setRecoverySetupRequest((n) => n + 1);
    },
  };
  const historyNeedsRecoverySetup =
    !lockStatus.enabled && historyRecovery.status?.needs_setup === true;
  // Plain function (NOT a hook) so it can live below ReadyApp's `if (!config)`
  // early return without changing hook order.
  const handleResolveOffer = (offerId: string) => {
    const current = configRef.current?.dismissed_offer_ids ?? [];
    if (current.includes(offerId)) return;
    patchConfig({ dismissed_offer_ids: [...current, offerId] });
  };
  const launchGates = launchOverlayGates({
    overlaysClear: (config.setup_completed as boolean) === true && spotlightGateOpen && connected
      && !dictationActivity.arming && !dictationActivity.recording && !dictationActivity.transcribing
      && !amendmentInlinePayload && !reacknowledgeInlinePayload && !downloadModel
      && !enhancementRecoveryOpen && !switchDialogGuidance && !pendingDownload && !pendingLanguageGuidance && !languagePopupOpen,
    lockdown: lockStatus.enabled,
    dismissedOfferIds: config.dismissed_offer_ids ?? [],
    tourShownThisSession: whatsNewShownThisSession,
  });

  return (
    <ExperimentalProvider enabled={experimentalEnabled}>
    <div
      className="w-full h-screen relative"
      style={{
        background: meshGradient,
        backgroundSize: "200% 200%",
        backgroundPosition: "30% 30%",
        filter: connected ? "none" : "saturate(0.3) brightness(0.95)",
        transition: "filter 0.8s ease",
      }}
    >
      <div data-tauri-drag-region className="absolute top-0 left-0 h-10 w-44 z-10" />
      <div className="flex h-full p-2 gap-2.5">
        <Sidebar
          activeTab={tab}
          onTabChange={setTab}
          status={status}
          devEnabled={devTabVisible}
          attention={historyNeedsRecoverySetup ? { transcripts: "Recovery setup needed" } : undefined}
        />

        <main
          className="relative flex-1 h-full overflow-hidden backdrop-blur-xl shadow-glass"
          style={{
            borderRadius: "var(--radius-card)",
            background: "var(--surface-card)",
            border: "1px solid var(--border-emphasis)",
          }}
        >
          {/* DevMark: 1px gold hairline at the top edge of <main> when the
              dev-only UI Test tab is active. Peripheral / unmistakable
              cue that the user is off the production rails. */}
          {tab === "ui-test" && (
            <div
              aria-hidden
              className="absolute top-0 left-0 right-0 h-px pointer-events-none z-20"
              style={{ background: "rgba(212,175,55,0.4)" }}
            />
          )}
          <TitleBar />
          <div className="h-full overflow-y-auto px-8 py-8">
            <AnimatePresence mode="popLayout">
              <motion.div
                key={tab}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={pageTransition}
                className="h-full"
              >
                {renderPage()}
              </motion.div>
            </AnimatePresence>
          </div>
        </main>
      </div>

      {/* Disconnect toast */}
      <AnimatePresence>
        {!connected && config && (
          <motion.div
            initial={{ opacity: 0, y: -20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -20 }}
            transition={{ type: "spring", stiffness: 400, damping: 30 }}
            className="fixed top-4 left-1/2 -translate-x-1/2 z-40 px-5 py-2.5 rounded-full text-[12px] font-medium text-steel"
            style={{
              background: "var(--surface-overlay)",
              backdropFilter: "blur(16px)",
              border: "1px solid var(--border-emphasis)",
              boxShadow: "var(--shadow-glass-sm)",
            }}
          >
            Loading...
          </motion.div>
        )}
      </AnimatePresence>


      {/* Amendment EULA overlay. Renders as a fixed full-screen dim+blur
          backdrop over the main UI when a pending amendment is detected.
          Uses inline mode so EulaPopup does not try to hide the main window
          on accept/dismiss; the parent clears the state instead. */}
      <AnimatePresence>
        {amendmentInlinePayload && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            style={EULA_INLINE_BACKDROP_STYLE}
          >
            <EulaPopup
              inline
              initialPayload={amendmentInlinePayload}
              onClose={() => setAmendmentInlinePayload(null)}
            />
          </motion.div>
        )}
      </AnimatePresence>

      {/* Re-acknowledgement overlay (EULA Section 20). Same fixed dim+blur
          backdrop as the amendment overlay, but accept-or-exit: no onClose
          dismiss path (ReacknowledgeView swallows Escape). Cleared by the
          eula-accepted listener on accept; Decline exits the app. */}
      <AnimatePresence>
        {reacknowledgeInlinePayload && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            style={EULA_INLINE_BACKDROP_STYLE}
          >
            <EulaPopup inline initialPayload={reacknowledgeInlinePayload} />
          </motion.div>
        )}
      </AnimatePresence>

      <LockdownScopeReviewDialog
        open={
          !lockdownScopeReviewResolved &&
          !lockdownScopeReviewDisableOpen &&
          !amendmentInlinePayload &&
          !reacknowledgeInlinePayload &&
          needsLockdownScopeReview(
            lockStatus.enabled,
            lockStatus.acknowledgedVersion,
          )
        }
        onContinue={async () => {
          await acknowledgeLockdown(LOCKDOWN_ACK_VERSION);
          setLockdownScopeReviewResolved(true);
          lockStatus.refresh();
        }}
        onTurnOff={() => setLockdownScopeReviewDisableOpen(true)}
        managed={lockStatus.managed}
      />

      <LockdownDisableDialog
        // Managed policy removes the review's turn-off action, so this can only
        // be reached on an unmanaged install. Gate it anyway so a policy that
        // arrives mid-session can't leave a dead disable path open.
        open={lockdownScopeReviewDisableOpen && !lockStatus.managed}
        hasEnvelope={lockStatus.hasEnvelope}
        onCancel={() => setLockdownScopeReviewDisableOpen(false)}
        onDisabled={(syncedConfig) => {
          setLockdownScopeReviewDisableOpen(false);
          setLockdownScopeReviewResolved(true);
          if (syncedConfig) setConfig(syncedConfig);
          lockStatus.refresh();
        }}
      />

      {/* Onboarding handles model download itself; don't pop the legacy
          DownloadDialog on top of it. The early-return above already guards
          this, but keep the explicit check as defense in depth in case the
          gate ever drifts. */}
      {downloadModel && (config.setup_completed as boolean) !== false && (
        <DownloadDialog
          title="Downloading the speech model"
          subtitle="We'll continue automatically when it's ready."
          subscribe={subscribe}
          onComplete={() => setDownloadModel(null)}
          onCancel={async () => {
            try {
              await cancelDownload();
            } catch (err) {
              console.error(`[Download] cancel error error_type=${errorType(err)}`);
            }
            setDownloadModel(null);
          }}
        />
      )}

      <EnhancementRecoveryHost
        key={JSON.stringify([config.enhancement_provider, config.enhancement_local_model, config.enhancement_local_endpoint, config.lockdown_mode, config.enhancement_enabled])}
        enabled={config.setup_completed === true && config.enhancement_enabled}
        connected={connected}
        blocked={!!downloadModel}
        subscribe={subscribe}
        onOpenChange={setEnhancementRecoveryOpen}
      />

      {/* What's new in 2.0 tour, then feature-spotlight / upgrade-offer
          overlays. Both wait for setup, connection, the post-launch beat, idle
          dictation and no other dialog. The tour also shows under Lockdown;
          offers keep the Lockdown gate and wait for the tour ID to resolve.
          Dev preview: ?forceSpotlight=<id> (whats-new-2-0 for the tour). */}
      <WhatsNewTourHost
        config={config}
        gatesClear={launchGates.tour}
        onResolveOffer={handleResolveOffer}
        onStartDictating={() => setTab("home")}
        onShow={markWhatsNewShown}
      />
      <FeatureSpotlightHost
        config={config}
        lockdownEnabled={lockStatus.enabled}
        hardwareCapable={true}
        nav={spotlightNav}
        onResolveOffer={handleResolveOffer}
        gatesClear={launchGates.spotlight}
        historyRecoveryNeedsSetup={historyNeedsRecoverySetup}
        platform={historyPlatform(dailyCapabilities?.platform)}
      />

      {/* Language picker popup, owned here so it escapes the <main>
          backdrop-blur containing block. */}
      <LanguagePopup
        open={languagePopupOpen}
        selectedCode={config.language}
        onSelect={handleLanguagePicked}
        onClose={() => setLanguagePopupOpen(false)}
        isLanguageAvailable={isLanguageAvailable}
        unavailableReason={capabilityExplanation(qwenStt)}
      />

      <MultilingualLanguageSwitchDialog
        open={switchDialogGuidance !== null}
        variant={
          switchDialogGuidance?.kind === "manual-incompatible"
            ? "incompatible"
            : "keep-or-switch"
        }
        currentModelName={
          switchDialogGuidance !== null
            ? getSttArtifactDisplayName(switchDialogGuidance.currentArtifact)
            : ""
        }
        recommendedModelName={
          switchDialogGuidance !== null
            ? getSttArtifactDisplayName(switchDialogGuidance.recommendedArtifact)
            : ""
        }
        targetLanguageName={
          switchDialogGuidance !== null
            ? getLanguageDisplayName(switchDialogGuidance.targetLanguage)
            : ""
        }
        onKeepCurrentModel={handleKeepCurrentModel}
        onSwitchToRecommended={handleSwitchToRecommendedModel}
        onDismiss={handleDismissLanguageGuidance}
      />

      {/* Confirm-then-download for an auto-selected backend whose model isn't
          on disk yet. Mounted at the App root (not inside Home) so the fixed
          backdrop/card resolve against the viewport, not <main>. */}
      {pendingDownload && (
        <ModelDownloadConfirmDialog
          targetBackend={pendingDownload.backend}
          subscribe={subscribe}
          onConfirm={handleConfirmDownload}
          onComplete={handleDownloadComplete}
          onCancel={handleDownloadCancel}
          onCancelDownload={handleCancelDownload}
          // The backend's own strings are generic (it does not know which model
          // the user asked for) and its refusals arrive as HTTP payloads. The
          // frontend knows the model, so it composes the locked recovery copy
          // and never renders the raw text.
          mapFailure={(failure) =>
            sttDownloadFailureCopy(
              getSttArtifactDisplayName(pendingDownload.artifact),
              failure,
            )
          }
        />
      )}

      {/* Privacy confirmation modal for AI Enhancement. Mounted at the App
          root so its position: fixed resolves against the viewport, not the
          <main> backdrop-blur containing block. */}
      {privacyModalProvider !== null && (
        <PrivacyModal
          provider={privacyModalProvider}
          ollamaEndpoint={config?.enhancement_ollama_endpoint}
          customBaseUrl={config?.enhancement_custom_base_url}
          model={config ? consentRequest(config, privacyModalProvider).model : undefined}
          onConfirm={async (context) => {
            await recordBYOKConsent(context);
            // Drain older writes before collecting drafts they might have
            // restored on failure. The reviewed fields always win this save.
            await configSaveQueue.current.idle();
            if (saveTimer.current) clearTimeout(saveTimer.current);
            const pending = pendingConfigPatch.current;
            pendingConfigPatch.current = {};
            const patch = { ...pending, ...consentConfigPatch(context) };
            const savedRevision = ++configEditRevision.current;
            const operation = configSaveQueue.current.save(patch);
            try {
              const saved = await operation;
              if (configEditRevision.current === savedRevision) setConfig(saved);
              setConfigSaveRevision((revision) => revision + 1);
              await refreshAcknowledgedProviders();
              setPrivacyModalProvider(null);
            } catch (error) {
              pendingConfigPatch.current = { ...pending, ...pendingConfigPatch.current };
              throw error;
            }
          }}
          onCancel={() => {
            setPrivacyModalProvider(null);
          }}
        />
      )}


    </div>
    </ExperimentalProvider>
  );
}

function payloadForEulaStatus(status: EulaStatus | null): EulaEmitPayload {
  const state = status ? resolveEulaRequirementState(status) : "configuration_error";
  return {
    mode:
      state === "substantive_update_required"
        ? "substantive-update"
        : state === "reacknowledgement_required"
          ? "reacknowledge"
          : state === "configuration_error"
            ? "configuration-error"
            : "first-run",
    eulaText: status?.bundled_text ?? "",
    eulaVersion: status?.bundled_version ?? "",
    eulaSha256: status?.bundled_sha256 ?? "",
    softwareVersion: status?.software_version ?? "",
    pinnedVersion: status?.pinned_version ?? undefined,
    changeBullets: status?.summary ?? [],
  };
}

export function EulaConsentGate() {
  const [status, setStatus] = useState<EulaStatus | null>(null);
  const [statusFailed, setStatusFailed] = useState(false);
  const [statusFailures, setStatusFailures] = useState(0);

  const refresh = useCallback(async () => {
    setStatusFailed(false);
    try {
      const next = await getEulaStatus();
      console.log(`[EULA] status loaded state=${next.state}`);
      setStatus(next);
      setStatusFailures(0);
    } catch (err) {
      console.error(`[EULA] status load failed error_type=${errorType(err)}`);
      setStatus(null);
      setStatusFailed(true);
      setStatusFailures((count) => count + 1);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!statusFailed || statusFailures >= 3) return;
    const retryTimer = window.setTimeout(refresh, 500);
    return () => window.clearTimeout(retryTimer);
  }, [refresh, statusFailed, statusFailures]);

  if (status && resolveEulaRequirementState(status) === "ready") {
    return (
      <ReadyApp
        key={`${status.bundled_version}:${status.bundled_sha256}`}
        initialEulaStatus={status}
        onConsentRequired={setStatus}
      />
    );
  }

  if (status === null) {
    if (statusFailed && statusFailures >= 3) {
      return <StartupFailureCurtain onRetry={() => {
        setStatusFailures(0);
        void refresh();
      }} />;
    }
    return <LoadingCurtain label="Loading..." />;
  }

  return (
    <div
      data-testid="eula-consent-gate"
      className="w-full h-screen"
      style={{
        background: meshGradient,
        backgroundSize: "200% 200%",
        backgroundPosition: "30% 30%",
      }}
    >
      <EulaPopup
        inline
        initialPayload={payloadForEulaStatus(status)}
        onAccepted={refresh}
      />
    </div>
  );
}

export default EulaConsentGate;
