import {
  agentRows, agentsEnabled as readAgentsEnabled, aggregateAgentsLight, rememberedProviders,
  IDLE_ACTIVITY, type AgentProviderActivityMap, type AgentProviderLastConnectMap,
} from "./agentSpeak/agentCompatibility";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { motion } from "framer-motion";
import { StatusLight } from "./StatusLight";
import { RangeInput } from "./RangeInput";
import { SliderField, SliderValue } from "./SliderField";
import { HoverTooltip } from "./HoverTooltip";
import { InfoGlyph } from "./InfoGlyph";
import { ModeSelector } from "./ModeSelector";
import { Dropdown } from "./Dropdown";
import { VOICE_MODEL_INFO, VOICE_MODEL_SELECTIONS } from "./agentSpeak/voiceModelCopy";
import { AdvancedDisclosure } from "./AdvancedDisclosure";
import { DurationSelect } from "./DurationSelect";
import { Toggle } from "./Toggle";
import { ShortcutCapture } from "./ShortcutCapture";
import { ModelDownloadConfirmDialog } from "./ModelDownloadConfirmDialog";
import { VoiceModelMemoryDialog } from "./VoiceModelMemoryDialog";
import { CardStatusLine, type CardStatus } from "./agentSpeak/SpeakSection";
import {
  AGENT_PROVIDER_LABEL,
  AGENT_PROVIDER_ORDER,
  AgentProviderInstallList,
  type AgentProviderResultMap,
} from "./agentSpeak/AgentProviderInstallList";
import { VoicePicker, type VoiceItem } from "./agentSpeak/VoicePicker";
import { useVoicePreview } from "./agentSpeak/useVoicePreview";
import { getSpeechVoices, type SpeechEngine, type SpeechSelection, type SpeechStatus } from "../lib/api/speech";
import { type AppConfig } from "../lib/api/config";
import { type CodingAgentProvider } from "../lib/api/agents";
import type { AppEvent } from "../lib/socket/events";
import { SecondsControl } from "./agentSpeak/SecondsControl";
import {
  automaticDownloadModel,
  missingAutomaticEngines,
} from "./agentSpeak/automaticVoiceDownload";
import {
  namedShortcutConflict,
  shortcutConflict,
} from "../lib/shortcutConflict";

const cardStyle: React.CSSProperties = {
  background: "var(--surface-modal)",
  backdropFilter: "blur(24px)",
  WebkitBackdropFilter: "blur(24px)",
  border: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass)",
  fontFamily: "var(--font-agent)",
};

const backdropStyle: React.CSSProperties = {
  background: "var(--backdrop-strong)",
  backdropFilter: "blur(40px)",
  WebkitBackdropFilter: "blur(40px)",
};

const XGlyph = () => (
  <svg
    width="12"
    height="12"
    viewBox="0 0 12 12"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    aria-hidden="true"
  >
    <path d="M3 3l6 6M9 3l-6 6" />
  </svg>
);

export interface HudConfigModalProps {
  open: boolean;
  embedded?: boolean;
  initialTab?: "General" | "Voice" | "Connections";
  onClose: () => void;
  config: AppConfig;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  connected: boolean;
  send: (msg: Record<string, unknown>) => boolean;
  onProviderToggle: (provider: CodingAgentProvider, checked: boolean) => void;
  onProviderRecheck?: () => void;
  providerActivity?: AgentProviderActivityMap;
  providerLastConnect?: AgentProviderLastConnectMap;
  providerResults: AgentProviderResultMap;
  /** Master Agents switch. Resolves whether a remembered provider then needs the user. */
  onAgentsToggle?: (checked: boolean) => Promise<{ needsAction: boolean }>;
  agentsActivity?: "idle" | "pausing" | "resuming";
  speechStatus: SpeechStatus | null;
  activeSpeechEngine?: SpeechEngine | null;
  speechPlaying: boolean;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  onSpeechEnabledChange: (checked: boolean) => void;
  onRefreshSpeechStatus: () => Promise<SpeechStatus | null>;
  onDownload: (engines: SpeechEngine[]) => Promise<void>;
  onCancelDownload: () => Promise<void>;
  /** Dev renderer marker for deterministic exact-state browser QA. */
  uiQaStateId?:
    | "settings.agent-speak-tooltip"
    | "settings.agent-speak-summaries"
    | "settings.agent-speak-voices"
    | "settings.agent-speak-voice-picker-open";
  /**
   * Dev renderer seam for deterministic exact-state browser QA, mirroring the
   * HUD's `uiQaFixtureId`. The QA runner has no backend, so `getSpeechVoices`
   * would always fail and the picker would only ever prove its error state;
   * and a *playing* row cannot be produced deterministically because it
   * depends on real audio. When present, this fixture supplies the roster and
   * the playing row directly. Product callers never pass it.
   */
  uiQaVoiceFixture?: {
    voices: VoiceItem[];
    playingKey: string | null;
  };
}


/**
 * The one genuinely undiscoverable thing in this modal, so it stays - as a
 * tooltip, not a permanent line.
 *
 * Locked copy 18 was ONE row-level hint carrying both definitions
 * ("When I ask: ... Every session: ..."). Two definitions concatenated behind a
 * single label is the tell that each half belongs on the option it describes,
 * so the owner (2026-08-13) split it: one tooltip per pill, same meaning, no
 * new behavior claims, and the reader no longer has to find their own clause.
 */
const SPEAK_IN_PER_SESSION_HINT =
  "Use /whisperstream:speak in Claude Code or $speak in Codex for that session.";
const SPEAK_IN_ALWAYS_ON_HINT = "Speaks in every session. No command needed.";

/**
 * Speed and Volume used to share one wide row, which stranded them at
 * opposite edges with a void between. They are now two adjacent rows in the
 * modal's own label-left / control-right grammar, with the SAME slider width
 * and value column as the Agent section's `Reply window`. Adjacent, aligned
 * and identically shaped, they read as one control cluster, and every control
 * in the modal now lands on one shared right edge (`ProfileEditorModal`'s
 * arrangement).
 */
function SliderRow({
  label,
  ariaLabel,
  min,
  max,
  step,
  value,
  display,
  onChange,
}: {
  label: string;
  ariaLabel: string;
  min: number;
  max: number;
  step: number;
  value: number;
  display: string;
  onChange: (value: number) => void;
}) {
  return (
    <SettingRow title={label}>
      <SliderField className="w-[240px]" value={<SliderValue>{display}</SliderValue>}>
        <RangeInput
          ariaLabel={ariaLabel}
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={onChange}
          className=""
        />
      </SliderField>
    </SettingRow>
  );
}

interface PendingSpeechDownload {
  /** The model the dialog tracks; the first of `engines`. */
  engine: SpeechEngine;
  /** Every model this confirmation downloads, in order. */
  engines: SpeechEngine[];
  /** What the selector saves once `engine` lands. */
  selection: SpeechSelection;
  model: ReturnType<typeof automaticDownloadModel>;
  enableSpeechOnComplete: boolean;
}

/**
 * The circled-i glyph that opens the on-brand HoverTooltip, matching
 * `ProfileEditorModal`'s `InfoDot` behavior. Renders the shared `InfoGlyph`
 * SVG instead of a literal "i" in a bordered circle (agent-onboarding-improvements
 * scope.md #6): the glyph is already a circle, so the button carries no
 * border/circle chrome of its own.
 */
function InfoDot({ label, content }: { label: string; content: string }) {
  const bubbleId = useId();
  return (
    <HoverTooltip
      content={content}
      side="auto"
      width="w-72"
      bubbleId={bubbleId}
      className="group/voice-info"
      focusVisibleClassName="group-focus-within/voice-info:opacity-100"
    >
      <button
        type="button"
        aria-label={label}
        aria-describedby={bubbleId}
        className="flex cursor-help select-none items-center justify-center text-steel/70 transition-colors hover:text-frost-deep"
      >
        <InfoGlyph />
      </button>
    </HoverTooltip>
  );
}

/**
 * One settings row: label left, control right, on a shared right edge. The
 * label is `ProfileEditorModal`'s `text-[13px] font-medium text-slate`, the
 * same type its own "Input Style" and "Hotkey" rows use.
 *
 * Rows carry their explanation as a visible `description` under the title,
 * the way Audio and Controls do (owner, 2026-10-25). The earlier rule kept
 * descriptions in label tooltips to make the modal read less busy, but on the
 * full Agents page the hidden help made the bare titles read as settings with
 * a missing control. `hint` remains for the few rows that keep a tooltip
 * (the Advanced idle-unload row).
 */
function SettingRow({
  title,
  description,
  descriptionId,
  hint,
  accessibleHint = false,
  children,
}: {
  title: string;
  /** Visible secondary line under the title. */
  description?: ReactNode;
  /** Id for the description, so the control can describe itself with it. */
  descriptionId?: string;
  hint?: string;
  /** Focusable help with viewport-aware placement for low advanced rows. */
  accessibleHint?: boolean;
  children: ReactNode;
}) {
  const hintId = useId();
  const label = (
    <span
      tabIndex={hint && accessibleHint ? 0 : undefined}
      aria-describedby={hint && accessibleHint ? hintId : undefined}
      className={`text-[13px] font-medium text-slate${
        hint ? " decoration-steel/40 underline-offset-4 group-hover:underline" : ""
      }`}
    >
      {title}
    </span>
  );
  return (
    <div className="flex min-h-[44px] items-center justify-between gap-6 py-1.5">
      <div className="min-w-0">
        <div className="flex items-center gap-1.5">
          {hint ? (
            <HoverTooltip
              content={hint}
              side={accessibleHint ? "auto" : "bottom-left"}
              width="w-64"
              bubbleId={accessibleHint ? hintId : undefined}
              focusVisibleClassName={accessibleHint ? "group-focus-within:opacity-100" : undefined}
            >
              {label}
            </HoverTooltip>
          ) : (
            label
          )}
        </div>
        {description && (
          <p id={descriptionId} className="mt-0.5 text-[11px] leading-relaxed text-steel">
            {description}
          </p>
        )}
      </div>
      <div className="flex shrink-0 items-center justify-end">{children}</div>
    </div>
  );
}

/**
 * The switch that governs a whole tab (Agents on General, Voice on Voice)
 * sits in a quiet raised panel so it reads as the tab's master rather than
 * one more row (owner-accepted direction, 2026-10-25). The reserved status
 * line stays directly under the panel.
 */
function MasterPanel({
  title,
  headingId,
  description,
  descriptionId,
  action,
  status,
  statusId,
}: {
  title: string;
  /** Labels the caller's tab section, so the whole tab is one region. */
  headingId: string;
  description: string;
  descriptionId: string;
  action: ReactNode;
  status?: CardStatus | null;
  statusId?: string;
}) {
  return (
    <>
      <div className="flex items-center justify-between gap-6 rounded-[var(--radius-row)] border border-[var(--border-hairline-steel)] bg-[var(--surface-raised)] px-4 py-3.5">
        <div className="min-w-0">
          <h3 id={headingId} className="text-[13px] font-medium text-slate">{title}</h3>
          <p id={descriptionId} className="mt-0.5 text-[11px] leading-relaxed text-steel">{description}</p>
        </div>
        {action}
      </div>
      <div className="px-1 pt-1">
        <CardStatusLine status={status} id={statusId} />
      </div>
    </>
  );
}

export function HudConfigModal({
  open,
  embedded = false,
  initialTab = "General",
  onClose,
  config,
  onConfigChange,
  connected,
  send,
  onProviderToggle,
  onProviderRecheck,
  providerActivity = IDLE_ACTIVITY,
  providerLastConnect = {},
  providerResults,
  onAgentsToggle,
  agentsActivity = "idle",
  speechStatus,
  activeSpeechEngine,
  speechPlaying,
  subscribe,
  onSpeechEnabledChange,
  onRefreshSpeechStatus,
  onDownload,
  onCancelDownload,
  uiQaStateId,
  uiQaVoiceFixture,
}: HudConfigModalProps) {
  const [tab, setTab] = useState<"General" | "Voice" | "Connections">(initialTab);
  const tabRef = useRef(tab);
  tabRef.current = tab;
  const tabs = ["General", "Voice", "Connections"] as const;
  // Moving focus after the tab commits keeps it off the control that unmounts.
  const focusConnectionsTabRef = useRef(false);
  useEffect(() => {
    if (tab !== "Connections" || !focusConnectionsTabRef.current) return;
    focusConnectionsTabRef.current = false;
    document.getElementById("agents-tab-Connections")?.focus();
  }, [tab]);
  const masterHintId = useId();
  const masterLineId = useId();
  const voiceHintId = useId();
  const generalHeadingId = useId();
  const voiceHeadingId = useId();
  const [voiceItems, setVoiceItems] = useState<VoiceItem[]>([]);
  const [voicesLoading, setVoicesLoading] = useState(true);
  const [voicesError, setVoicesError] = useState(false);
  const [voiceAdvancedOpen, setVoiceAdvancedOpen] = useState(false);
  const [viewCaptureConflict, setViewCaptureConflict] = useState<string | null>(
    null,
  );
  const [pendingSpeechDownload, setPendingSpeechDownload] =
    useState<PendingSpeechDownload | null>(null);
  const [memoryDialogStatus, setMemoryDialogStatus] =
    useState<SpeechStatus | null>(null);
  const [speechChoicePending, setSpeechChoicePending] = useState(false);
  const [unavailableSpeechEngine, setUnavailableSpeechEngine] =
    useState<SpeechEngine | null>(null);

  // Bundled pre-rendered clips, played in the frontend: no backend call, no
  // engine load, and both sliders demonstrate themselves live through the
  // shared audio element.
  const {
    playingKey,
    error: sampleError,
    toggle: togglePreview,
    stopAll: stopPreview,
  } = useVoicePreview(config.agent_speech_speed, config.agent_speech_volume);

  const speechSelection = config.agent_speech_engine ?? "auto";
  // What the selector shows: the saved choice, Automatic included, or the
  // engine whose download is waiting for confirmation.
  const displayedSelection = pendingSpeechDownload?.selection ?? speechSelection;
  const playingEngine =
    activeSpeechEngine === undefined
      ? speechStatus?.active_engine
      : activeSpeechEngine;
  const visibleEngine: SpeechEngine =
    speechSelection === "auto"
      ? ((speechPlaying ? playingEngine : null) ??
        speechStatus?.resolved_engine ??
        speechStatus?.preferred_engine ??
        "supertonic")
      : speechSelection;

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setViewCaptureConflict(null);
    if (uiQaVoiceFixture) {
      // Deterministic dev QA: no network, no engine, one frozen roster.
      setVoiceItems(uiQaVoiceFixture.voices);
      setVoicesError(false);
      setVoicesLoading(false);
      return;
    }
    setVoiceItems([]);
    setVoicesError(false);
    setVoicesLoading(true);
    // Switching the voice model pauses and resets the preview before the new
    // roster loads: the old clip must not outlive the list it belonged to.
    stopPreview();
    getSpeechVoices(visibleEngine)
      .then((data) => {
        if (cancelled) return;
        setVoiceItems(
          data.voices.map((voice) => ({
            id: voice.id ?? voice.sid,
            label: voice.label,
            group: voice.group,
            reference_label: voice.reference_label,
          })),
        );
        setVoicesLoading(false);
      })
      .catch(() => {
        // The settings remain usable if the experimental endpoint is absent;
        // the failure surfaces as the list's own error state instead of an
        // indistinguishable empty list.
        if (cancelled) return;
        setVoicesError(true);
        setVoicesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, stopPreview, uiQaVoiceFixture, visibleEngine]);

  useEffect(() => {
    if (!open || embedded) return;
    const onKey = (event: KeyboardEvent) => {
      if (
        event.key === "Escape" &&
        pendingSpeechDownload === null &&
        memoryDialogStatus === null
      ) {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [embedded, memoryDialogStatus, open, onClose, pendingSpeechDownload]);

  if (!open) return null;

  const speechEnabled =
    config.agent_speech_enabled ?? config.agent_speech_mode !== "off";
  const summaryTextEnabled = config.agent_summary_text_enabled ?? false;
  const activation =
    config.agent_speech_activation === "always_on"
      ? "always_on"
      : "per_session";
  const perSessionHint = speechStatus?.plugin_variant === "dev"
    ? "Use /whisperstream-dev:speak in Claude Code or $speak-dev in Codex for that session."
    : SPEAK_IN_PER_SESSION_HINT;
  const replyGraceSeconds = config.claude_reply_grace_seconds ?? 6;
  const replyAutoSubmit = config.claude_reply_auto_submit ?? false;
  const viewShortcut = config.claude_view_shortcut ?? "ctrl+alt+`";
  const viewConflictCandidates = [
    { shortcut: config.primary_shortcut, action: "the primary shortcut" },
    { shortcut: config.push_to_talk_shortcut, action: "Push to Talk" },
    { shortcut: config.toggle_shortcut, action: "Toggle" },
    { shortcut: config.paste_last_shortcut, action: "Paste Last Transcript" },
    {
      shortcut: config.paste_last_original_shortcut,
      action: "Paste Last Transcript (Original)",
    },
    ...Object.values(config.profile_hotkeys ?? {}).map((shortcut) => ({
      shortcut,
      action: "a profile shortcut",
    })),
    { shortcut: "enter", action: "Enter" },
    { shortcut: "esc", action: "Escape" },
  ];
  const viewConflict =
    shortcutConflict(viewShortcut) ||
    namedShortcutConflict(viewShortcut, viewConflictCandidates);
  const agentsEnabled = readAgentsEnabled(config);
  const rows = agentRows(config, providerResults, providerActivity, providerLastConnect);
  const providerConnected = {
    codex: rows.codex.state === "connected",
    claude: rows.claude.state === "connected",
  };
  const anyProviderConnected =
    providerConnected.codex || providerConnected.claude;
  // While resuming, the switch already reads on, so the light reports the
  // resume in progress rather than the paused state being left.
  const light = aggregateAgentsLight(agentsEnabled || agentsActivity === "resuming", rows);
  const remembered = rememberedProviders(config);
  const rememberedNames = AGENT_PROVIDER_ORDER.filter((provider) => remembered[provider])
    .map((provider) => AGENT_PROVIDER_LABEL[provider]).join(" and ");
  const masterChecked = agentsActivity === "pausing" ? false : agentsActivity === "resuming" ? true : agentsEnabled;
  const masterStatus: CardStatus | null = agentsActivity === "resuming"
    ? { message: rememberedNames ? `Resuming ${rememberedNames}...` : "", tone: "normal" }
    : !agentsEnabled && agentsActivity === "idle"
      ? {
          message: rememberedNames
            ? `Agents is paused. Turn it on to resume ${rememberedNames}.`
            : "Agents is paused. Your settings are kept.",
          tone: "normal",
        }
      : null;
  const showGeneralLight = agentsEnabled && agentsActivity !== "pausing";
  // The shortcut-conflict line is no longer a floating paragraph between rows;
  // it lives in the Agent section's reserved status slot (locked copy 40 is
  // whatever the shared helpers return, unchanged).
  const replyConflict = viewCaptureConflict ?? viewConflict;
  const conflictStatus: CardStatus | null = replyConflict
    ? { message: replyConflict, tone: "error" }
    : null;
  const visibleEngineStatus = speechStatus?.engines[visibleEngine];
  const visibleEngineAvailable = Boolean(
    visibleEngineStatus &&
      (visibleEngineStatus.available ?? visibleEngineStatus.installed),
  );
  const visibleVoice: string | number =
    visibleEngine === "kokoro"
      ? (config.agent_speech_kokoro_voice ?? "af_heart")
      : config.agent_speech_voice;
  const explicitKokoroCaution =
    speechSelection === "kokoro" && speechStatus?.memory.eligible === false;
  // `slow_kokoro` is Automatic's performance fallback, not a startup failure.
  // Explicit Kokoro remains available as the direct-retry path, so only a
  // structural failure should occupy the Voice section's hard-error rung.
  const explicitKokoroFailure =
    speechSelection === "kokoro" &&
    speechStatus?.reason === "unhealthy_kokoro";
  const runtimeUnavailable =
    unavailableSpeechEngine !== null ||
    visibleEngineStatus?.supported === false ||
    visibleEngineStatus?.unavailable_reason === "runtime_unavailable";
  // One priority-ordered reserved line replaces the four floating paragraphs
  // that used to appear and disappear between the Voice section's rows. Order:
  // unavailable runtime > failure > memory caution > Claude not connected >
  // sample playback failure > empty.
  const voiceCardStatus: CardStatus | null = !speechEnabled
    ? null
    : runtimeUnavailable
      ? {
          message: `${(unavailableSpeechEngine ?? visibleEngine) === "kokoro" ? "Higher quality" : "Lower memory"} isn't available on this platform.`,
          tone: "error",
        }
      : explicitKokoroFailure
      ? {
          message:
            speechStatus?.engines.supertonic.supported === false
              ? "Higher quality could not start. Try again."
              : "Higher quality could not start. Try Lower memory instead.",
          tone: "error",
        }
      : explicitKokoroCaution
        ? {
            message:
              "Higher quality uses more memory than this computer has spare.",
            tone: "attention",
          }
        : !agentsEnabled
          ? {
              message: "Agents is paused. Turn it on in General to hear replies.",
              tone: "normal",
            }
        : !anyProviderConnected
          ? {
              message:
                "Connect Codex or Claude Code in Connections so Agent Speak can listen.",
              tone: "normal",
            }
          : sampleError
            ? {
                message: "That sample could not play.",
                tone: "normal",
              }
            : null;
  const selectionLight =
    speechStatus?.current_download ||
    explicitKokoroCaution ||
    speechChoicePending
      ? "attention"
      : visibleEngineAvailable
        ? "ready"
        : "attention";
  const pendingEngineStatus = pendingSpeechDownload?.model ?? null;

  const requestSpeechEngine = (
    engine: SpeechEngine,
    status: SpeechStatus,
    enableSpeechOnComplete: boolean,
  ) => {
    setMemoryDialogStatus(null);
    const engineStatus = status.engines[engine];
    if (
      engineStatus.supported === false ||
      engineStatus.unavailable_reason === "runtime_unavailable"
    ) {
      setPendingSpeechDownload(null);
      setUnavailableSpeechEngine(engine);
      return;
    }
    setUnavailableSpeechEngine(null);
    if (engineStatus.installed) {
      setPendingSpeechDownload(null);
      onConfigChange({ agent_speech_engine: engine });
      if (enableSpeechOnComplete) onSpeechEnabledChange(true);
      return;
    }
    setPendingSpeechDownload({
      engine,
      engines: [engine],
      selection: engine,
      model: automaticDownloadModel(status, [engine]),
      enableSpeechOnComplete,
    });
  };

  /** Automatic stays Automatic: it confirms every missing model at once and
   *  saves "auto" (with speech on, when enabling) once the first one lands;
   *  Kokoro keeps downloading after Supertonic is ready. */
  const requestAutomatic = (
    status: SpeechStatus,
    enableSpeechOnComplete: boolean,
  ) => {
    setMemoryDialogStatus(null);
    setUnavailableSpeechEngine(null);
    const engines = missingAutomaticEngines(status);
    if (engines.length === 0) {
      setPendingSpeechDownload(null);
      onConfigChange({ agent_speech_engine: "auto" });
      if (enableSpeechOnComplete) onSpeechEnabledChange(true);
      return;
    }
    setPendingSpeechDownload({
      engine: engines[0],
      engines,
      selection: "auto",
      model: automaticDownloadModel(status, engines),
      enableSpeechOnComplete,
    });
  };

  const handleSummaryTextToggle = (checked: boolean) => {
    if (checked) {
      if (replyGraceSeconds === 6) {
        onConfigChange({
          agent_summary_text_enabled: true,
          claude_reply_grace_seconds: 20,
        });
      } else {
        onConfigChange({
          agent_summary_text_enabled: true,
        });
      }
    } else {
      if (replyGraceSeconds === 20) {
        onConfigChange({
          agent_summary_text_enabled: false,
          claude_reply_grace_seconds: 6,
        });
      } else {
        onConfigChange({
          agent_summary_text_enabled: false,
        });
      }
    }
  };

  const handleSpeechToggle = async (checked: boolean) => {
    if (!checked) {
      setPendingSpeechDownload(null);
      setMemoryDialogStatus(null);
      onSpeechEnabledChange(false);
      return;
    }
    setSpeechChoicePending(true);
    const status = (await onRefreshSpeechStatus()) ?? speechStatus;
    setSpeechChoicePending(false);
    if (!status) return;
    if (status.requested === "auto") {
      requestAutomatic(status, true);
      return;
    }
    requestSpeechEngine(status.preferred_engine, status, true);
  };

  const handleVoiceModelChange = async (selection: SpeechSelection) => {
    if (selection === "auto") {
      // Automatic downloads both models. With speech off it only saves the
      // choice; turning speech on confirms the downloads.
      if (!speechEnabled || !speechStatus) {
        setMemoryDialogStatus(null);
        setPendingSpeechDownload(null);
        setUnavailableSpeechEngine(null);
        onConfigChange({ agent_speech_engine: "auto" });
        return;
      }
      requestAutomatic(speechStatus, false);
      return;
    }
    const engine = selection;
    if (engine === "supertonic") {
      if (speechStatus) requestSpeechEngine(engine, speechStatus, false);
      return;
    }

    setSpeechChoicePending(true);
    const status = (await onRefreshSpeechStatus()) ?? speechStatus;
    setSpeechChoicePending(false);
    if (!status) return;
    if (status.kokoro_memory_tier !== "recommended") {
      setMemoryDialogStatus(status);
      return;
    }
    requestSpeechEngine("kokoro", status, false);
  };

  function goToConnections() {
    stopPreview();
    focusConnectionsTabRef.current = true;
    setTab("Connections");
  }

  // Only a toggle-driven resume may move the user, and only from General, so
  // the app never overrides navigation the user started themselves.
  async function toggleAgents(checked: boolean) {
    if (!onAgentsToggle) return;
    const { needsAction } = await onAgentsToggle(checked);
    if (embedded && checked && needsAction && tabRef.current === "General") goToConnections();
  }

  const content = (
    <>
      <div className={embedded ? "agents-settings" : "fixed inset-0 z-50 flex items-center justify-center"}>
      {!embedded && <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        onClick={onClose}
      />}

      {/* No `overflow-y-auto` and no `overflow-hidden` on this root: with the
          voice well gone this modal has NO scroller at all, and a CSS-only
          HoverTooltip would be clipped by an overflow ancestor anyway. The
          width is back to 600: 720 only ever existed to seat the always-open
          voice list. */}
      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className={embedded ? "relative w-full max-w-[760px] pb-8" : "relative flex max-h-[calc(100vh-40px)] w-[600px] max-w-[calc(100vw-40px)] flex-col rounded-[var(--radius-card)] p-7"}
        style={embedded ? { fontFamily: "var(--font-agent)" } : cardStyle}
        role={embedded ? undefined : "dialog"}
        aria-modal={embedded ? undefined : true}
        aria-labelledby={embedded ? undefined : "hud-config-title"}
        data-ui-qa-surface={
          uiQaStateId === undefined ? undefined : "settings-agent-speak"
        }
        data-ui-qa-floating={
          uiQaStateId === undefined || embedded ? undefined : "modal"
        }
      >
        {!embedded && <><button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="absolute right-4 top-4 flex h-6 w-6 cursor-pointer items-center justify-center rounded-full text-steel/60 transition-colors hover:text-slate"
        >
          <XGlyph />
        </button>

        <h2
          id="hud-config-title"
          className="pr-8 text-[15px] font-semibold text-slate"
        >
          Agents
        </h2></>}

        {/* The strip's base line uses `--tab-rule`, a steel etch that holds
            on the light card; `--divider` (white) all but vanished there. */}
        {embedded && <div role="tablist" aria-label="Agents settings" className="flex gap-6 border-b border-[var(--tab-rule)] mb-6">
          {tabs.map((name, index) => <button key={name} id={`agents-tab-${name}`} role="tab"
            aria-selected={tab === name} aria-controls={`agents-panel-${name}`} tabIndex={tab === name ? 0 : -1}
            className={`cursor-pointer pb-3 text-[13px] border-b-2 -mb-px ${tab === name ? "border-frost-deep text-slate font-medium" : "border-transparent text-steel"}`}
            onClick={() => { stopPreview(); setTab(name); }}
            onKeyDown={(event) => {
              const next = event.key === "ArrowRight" ? (index + 1) % 3 : event.key === "ArrowLeft" ? (index + 2) % 3 : event.key === "Home" ? 0 : event.key === "End" ? 2 : -1;
              if (next < 0) return;
              event.preventDefault(); stopPreview(); setTab(tabs[next]);
              document.getElementById(`agents-tab-${tabs[next]}`)?.focus();
            }}>{name}</button>)}
        </div>}
        {/* Owner direction (2026-10-25, accepted prototype): General and Voice
            lead with a master switch panel, then plain rows whose descriptions
            sit under their titles, separated by spacing alone. The old
            Overlay and Replies headings were bare 13px titles that read as
            settings whose control had gone missing, so they are gone. */}
        <div className="mt-3 flex flex-col gap-6" role={embedded ? "tabpanel" : undefined} id={embedded ? `agents-panel-${tab}` : undefined} aria-labelledby={embedded ? `agents-tab-${tab}` : undefined}>
          {(!embedded || tab === "General") && <section aria-labelledby={generalHeadingId}>
            <MasterPanel
              title="Agents"
              headingId={generalHeadingId}
              description="Pause all coding agents without disconnecting them."
              descriptionId={masterHintId}
              action={
                <div className="flex shrink-0 items-center gap-2.5">
                  {showGeneralLight && (
                    <StatusLight
                      state={light.state}
                      ariaLabel={light.ariaLabel}
                      onActivate={embedded && light.actionable ? goToConnections : undefined}
                      activateLabel="Agents needs attention, open Connections"
                    />
                  )}
                  <Toggle
                    checked={masterChecked}
                    disabled={agentsActivity !== "idle" || !onAgentsToggle}
                    onChange={(checked) => void toggleAgents(checked)}
                    ariaLabel="Agents"
                    ariaDescribedBy={masterStatus?.message ? masterLineId : masterHintId}
                  />
                </div>
              }
              status={masterStatus}
              statusId={masterLineId}
            />
            {/* Pausing keeps every setting, so these stay editable while off. */}
            <div>
              <SettingRow
                title="View full response"
                description="Opens full replies, questions, and sessions started with the Whisperstream CLI."
              >
                <ShortcutCapture
                  value={viewShortcut}
                  onChange={(keys) => {
                    const conflict =
                      shortcutConflict(keys) ||
                      namedShortcutConflict(keys, viewConflictCandidates);
                    setViewCaptureConflict(conflict);
                    if (!conflict) onConfigChange({ claude_view_shortcut: keys });
                  }}
                  send={send}
                  subscribe={subscribe}
                  connected={connected}
                  captureTarget="claude_view"
                  clearAriaLabel="Clear View full response shortcut"
                />
              </SettingRow>
              <SettingRow
                title="Show summaries"
                description="Shows your coding agent’s short summary in the overlay when it replies."
              >
                <Toggle
                  checked={summaryTextEnabled}
                  onChange={handleSummaryTextToggle}
                  ariaLabel="Show written summaries for coding agent replies"
                />
              </SettingRow>
              <SettingRow title="Reply window" description="Time to reply after the voice stops. Escape skips to the next response.">
                <SecondsControl label="Reply window" value={replyGraceSeconds} tiered onChange={(value) => onConfigChange({ claude_reply_grace_seconds: value })} />
              </SettingRow>
              <SettingRow
                title="Send replies automatically"
                description="Sends your reply the moment dictation stops, with no Enter press."
              >
                <Toggle
                  checked={replyAutoSubmit}
                  onChange={(checked) =>
                    onConfigChange({
                      claude_reply_auto_submit: checked,
                    })
                  }
                  ariaLabel="Send replies automatically"
                />
              </SettingRow>
              <CardStatusLine status={conflictStatus} />
            </div>
          </section>}

          {/* The tab names the subject, so Connections has no title of its
              own: the intro sentence leads, with the summary light beside it. */}
          {(!embedded || tab === "Connections") && <section aria-label="Connections">
            <div className="mb-1.5 flex items-center justify-between gap-4">
              <p className="text-[12px] leading-relaxed text-steel">
                {agentsEnabled
                  ? "One connection covers the desktop app and the CLI."
                  : "Agents is paused. Turn it on in General, or resume here."}
              </p>
              <StatusLight state={light.state} ariaLabel={light.ariaLabel} />
            </div>
            <AgentProviderInstallList
              rows={rows}
              activity={providerActivity}
              connected={providerConnected}
              onToggle={onProviderToggle}
              onResume={() => void toggleAgents(true)}
              onRecheck={onProviderRecheck}
              density="configure"
            />
          </section>}

          {(!embedded || tab === "Voice") && <section aria-labelledby={voiceHeadingId}>
            {/* The selection light renders only while speech is on, so an off
                tab says "off" once, through the switch. */}
            <MasterPanel
              title="Voice"
              headingId={voiceHeadingId}
              description="Reads your coding agent’s replies aloud."
              descriptionId={voiceHintId}
              action={
                <div className="flex shrink-0 items-center gap-2.5">
                  {speechEnabled && <StatusLight state={selectionLight} />}
                  <Toggle
                    checked={speechEnabled}
                    onChange={(checked) => void handleSpeechToggle(checked)}
                    ariaLabel="Read coding agent replies aloud"
                    ariaDescribedBy={voiceHintId}
                  />
                </div>
              }
              status={voiceCardStatus}
            />
            {/* The "Summary source" row is gone. Its only enabled value was
                "Agent summary" (the V1 contract, and the sole supported
                source); the other was a permanently-disabled "Coming later"
                placeholder. A control with one selectable option is an
                advertisement, not a setting. agent_speech_summary_source keeps
                its "agent" default. */}
            {!speechEnabled ? (
              <p className="px-1 text-[11px] text-steel">
                Turn this on to hear coding agent replies.
              </p>
            ) : (
              <div>
                <SettingRow title="What to read" description="Code blocks and raw links are skipped.">
                  <Dropdown value={config.agent_speech_content ?? "summary"}
                    options={[{ value: "summary", label: "Spoken summary" }, { value: "short", label: "Full response when short" }, { value: "full", label: "Full response" }]}
                    onChange={(agent_speech_content) => onConfigChange({ agent_speech_content })} ariaLabel="What to read" triggerClassName="w-[220px]" panelWidth={250} />
                </SettingRow>
                {config.agent_speech_content === "short" && (
                  <SettingRow title="Short response limit" description="Estimated speaking time at your selected speed.">
                    <SecondsControl label="Short response limit" value={config.agent_speech_short_seconds ?? 20} min={5} max={120} onChange={(agent_speech_short_seconds) => onConfigChange({ agent_speech_short_seconds })} />
                  </SettingRow>
                )}
                {/* The selected option's hint is the row's description, so it
                    reads without hovering; each pill keeps its own tooltip. */}
                <SettingRow title="Speak in" description={activation === "always_on" ? SPEAK_IN_ALWAYS_ON_HINT : perSessionHint}>
                  <ModeSelector
                    ariaLabel="Speak in"
                    value={activation}
                    onChange={(agent_speech_activation) =>
                      onConfigChange({ agent_speech_activation })
                    }
                    className="shrink-0 flex-nowrap justify-end"
                    options={[
                      {
                        value: "per_session",
                        label: "When I ask",
                        hint: perSessionHint,
                        uiQaHoverTarget:
                          uiQaStateId === "settings.agent-speak-tooltip",
                      },
                      {
                        value: "always_on",
                        label: "Every session",
                        hint: SPEAK_IN_ALWAYS_ON_HINT,
                      },
                    ]}
                  />
                </SettingRow>

                {/* The "i" lives inside the picker (owner, 2026-10-25): on the
                    closed trigger it explains the current choice, and in the
                    open list each option carries its own. */}
                <SettingRow title="Voice model">
                  <Dropdown
                    value={displayedSelection}
                    options={VOICE_MODEL_SELECTIONS}
                    onChange={(selection) => void handleVoiceModelChange(selection)}
                    ariaLabel="Voice model"
                    triggerClassName="w-[220px]"
                    panelWidth={250}
                    triggerAccessory={
                      <InfoDot
                        label="Voice model information"
                        content={VOICE_MODEL_INFO[displayedSelection]}
                      />
                    }
                    renderOptionAction={(option) => (
                      <InfoDot
                        label={`${option.label} information`}
                        content={VOICE_MODEL_INFO[option.value]}
                      />
                    )}
                    optionActionsFocusable
                    panelOverflow="visible"
                  />
                </SettingRow>

                {/* Previews are bundled clips, so every option row can be
                    auditioned before the voice model is installed. The
                    int/string split between the two persisted voice slots is
                    resolved here and nowhere else. */}
                <SettingRow title="Voice">
                  <VoicePicker
                    engine={visibleEngine}
                    voices={voiceItems}
                    loading={voicesLoading}
                    error={voicesError}
                    selectedId={visibleVoice}
                    onSelect={(voice) =>
                      onConfigChange(
                        visibleEngine === "kokoro"
                          ? { agent_speech_kokoro_voice: String(voice) }
                          : { agent_speech_voice: Number(voice) },
                      )
                    }
                    playingKey={
                      uiQaVoiceFixture
                        ? uiQaVoiceFixture.playingKey
                        : playingKey
                    }
                    onTogglePlay={togglePreview}
                  />
                </SettingRow>

                <SliderRow
                  label="Speed"
                  ariaLabel="Speech speed"
                  min={0.5}
                  max={2}
                  step={0.1}
                  value={config.agent_speech_speed}
                  display={`${config.agent_speech_speed.toFixed(1)}x`}
                  onChange={(agent_speech_speed) =>
                    onConfigChange({ agent_speech_speed })
                  }
                />
                <SliderRow
                  label="Volume"
                  ariaLabel="Speech volume"
                  min={0}
                  max={1}
                  step={0.05}
                  value={config.agent_speech_volume}
                  display={`${Math.round(config.agent_speech_volume * 100)}%`}
                  onChange={(agent_speech_volume) =>
                    onConfigChange({ agent_speech_volume })
                  }
                />
                <AdvancedDisclosure
                  open={voiceAdvancedOpen}
                  allowContentOverflow
                  onToggle={() => setVoiceAdvancedOpen((previous) => !previous)}
                >
                  <SettingRow
                    title="Unload voice model when idle"
                    hint="Free memory after speech is inactive. The next reply may take a moment to start."
                    accessibleHint
                  >
                    <DurationSelect
                      value={config.agent_speech_idle_timeout_seconds ?? 1800}
                      onChange={(agent_speech_idle_timeout_seconds) =>
                        onConfigChange({ agent_speech_idle_timeout_seconds })
                      }
                      ariaLabel="Unload voice model when idle"
                    />
                  </SettingRow>
                </AdvancedDisclosure>
              </div>
            )}
          </section>}

        </div>
      </motion.div>
      </div>
      {pendingSpeechDownload && pendingEngineStatus && (
        <ModelDownloadConfirmDialog
          targetModel={pendingEngineStatus}
          eventKind="tts"
          targetEngine={pendingSpeechDownload.engine}
          subscribe={subscribe}
          onConfirm={() => onDownload(pendingSpeechDownload.engines)}
          onComplete={() => {
            onConfigChange({
              agent_speech_engine: pendingSpeechDownload.selection,
            });
            if (pendingSpeechDownload.enableSpeechOnComplete) {
              onSpeechEnabledChange(true);
            }
            setPendingSpeechDownload(null);
          }}
          onCancel={() => setPendingSpeechDownload(null)}
          onCancelDownload={async () => {
            await onCancelDownload();
            setPendingSpeechDownload(null);
          }}
        />
      )}
      {memoryDialogStatus &&
        memoryDialogStatus.kokoro_memory_tier !== "recommended" && (
          <VoiceModelMemoryDialog
            open={true}
            tier={memoryDialogStatus.kokoro_memory_tier}
            memory={memoryDialogStatus.memory}
            onContinue={() =>
              requestSpeechEngine("kokoro", memoryDialogStatus, false)
            }
            onUseLowerMemory={() =>
              requestSpeechEngine("supertonic", memoryDialogStatus, false)
            }
            onDismiss={() => setMemoryDialogStatus(null)}
          />
        )}
    </>
  );
  return embedded ? content : createPortal(content, document.body);
}
