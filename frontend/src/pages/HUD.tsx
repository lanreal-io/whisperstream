import { useHudNativeReconciler } from "./hud/useHudNativeReconciler";
import { useConsoleResize } from "./hud/useConsoleResize";
import { Window, getCurrentWindow, currentMonitor } from "@tauri-apps/api/window";
import {
  type HudGeometryMode,
  type MeasuredClaudeCard,
  PREVIEW_EXPANDED_CARD_W,
  PREVIEW_CARD_H,
  isHudControlFamilyMode,
  PREVIEW_CARD_W,
  SNAP_THRESHOLD,
  pillAnchorFromWindowPosition,
  RECORDING_CONTROL_ACTIVE_W,
  PILL_WIDTH,
  PILL_WIDTH_COMPACT,
  CLAUDE_PERMISSION_PILL_W,
  PILL_WIDTH_PROFILE,
  PILL_PROFILE_GAP,
  type ClaudeCardSurface,
  hudModeGeometry,
  RECORDING_CONTROL_NATIVE_PAD,
  RECORDING_CONTROL_LAUNCHER_W,
  PILL_HEIGHT,
  REPLY_SURFACE_BOTTOM_PAD,
  PILL_SPRING,
  type HudWorkArea,
  currentHudDisplay,
  draggedConsolePlacement,
  saveConsoleOffset,
} from "./hud/geometry";
import { type AppEvent } from "../lib/socket/events";
import {
  type DictationModel,
  INITIAL_DICTATION_MODEL,
  dictationReducer,
} from "./hud/dictationState";
import {
  type TimedTransient,
  ambientReplyWindow,
  type AmbientReplyDraft,
  currentClaudeAttention,
  compactQuestionPillProps,
  type ReplyRailPhase,
  type ReplyNotice,
  type ReplyPhase,
  derivePresentation,
  HIDE_ANIMATION_MS,
} from "./hud/presentation";
import {
  hudSurfaceForMode,
  nextSurfaceFrame,
  surfaceAwaitingWindow,
  type HudSurfaceFrame,
} from "./hud/surfaceState";
import { useAppSocket } from "../lib/socket/connection";
import { useHudClickThrough } from "./hud/useHudClickThrough";
import {
  usesDedicatedRecordingControlWindow,
  RECORDING_CONTROL_VISIBILITY_STORAGE_KEY,
  RECORDING_CONTROL_VISIBILITY_EVENT,
  RECORDING_CONTROL_START_PENDING_EVENT,
} from "../lib/recordingControl";
import { useReducedMotion, AnimatePresence, motion } from "framer-motion";
import { useState, useEffect, useMemo, useReducer, useRef, useCallback, useLayoutEffect, type ReactElement } from "react";
import {
  type HudDesktopTheme,
  HUD_DARK_DESKTOP_BORDER,
  HUD_LIGHT_DESKTOP_BORDER,
  HUD_DARK_DESKTOP_ELEVATION,
  HUD_LIGHT_DESKTOP_ELEVATION,
  HUD_DARK_PERSISTENT_CONTROL_HIGHLIGHT,
  HUD_LIGHT_PERSISTENT_CONTROL_HIGHLIGHT,
  HUD_DARK_DESKTOP_PALETTE,
  HUD_LIGHT_DESKTOP_PALETTE,
} from "./hud/material";
import {
  type AgentConversation,
  type AgentInteraction,
  type AgentCompletion,
  type AgentResponseCompletion,
  sendAgentQuestionOpportunityAck,
  sendAgentQuestionOpportunityAnswers,
  type AgentQuestion,
  type HudSession,
  sendAgentQuestionOpportunityDefer,
  sendAgentHudPresented,
  sendAgentDraftUpdate,
  sendAgentDraftSubmit,
  replyTargetForConversation,
  replyTargetForQuestion,
  type ReplySurface,
} from "../lib/socket/agentEvents";
import {
  agentReplyReducer,
  bufferedRevision,
  bufferedText,
  createAgentReplyState,
  draftRefused,
  editBuffer,
  pruneBuffers,
  settleBuffer,
  type ReplyBuffers,
} from "../lib/agentReplyState";
import {
  agentCompletionQueueReducer,
  createAgentCompletionQueueState,
  currentCompletion,
  currentCompletionIsSpeaking,
  queuedCompletionCount,
} from "../lib/agentCompletionQueue";
import {
  agentQuestionReducer,
  createAgentQuestionState,
  questionCanAnswer,
  exactQuestionAnswers,
  toggleQuestionOption,
} from "../lib/agentQuestionState";
import {
  agentSessionsReducer,
  createAgentSessionsState,
  selectSessionOnExpand,
  stripTabFor,
  type StripSwitch,
  hasSessionStrip,
  sessionShortcutIndex,
} from "../lib/agentSessions";
import { useForcedHudState, forcedStateById, hudForcedStateQaId } from "./hud/hudForceState";
import {
  readRecordingControlVisibility,
  readScreenReaderStatus,
  type HudDictationCommand,
  postHudDictationCommand,
  RECORDING_CONTROL_COLLAPSE_MS,
} from "./hud/recordingBridge";
import { isTauri, invoke } from "@tauri-apps/api/core";
import {
  type FieldCarets,
  focusFieldAtEnd,
  rememberFieldCaret,
  restoreFieldCaret,
} from "./hud/fieldFocus";
import { listen } from "@tauri-apps/api/event";
import { LogicalSize, LogicalPosition } from "@tauri-apps/api/dpi";
import { applyWindowLifecycle } from "../lib/windowLifecycle";
import { SCREEN_READER_STATUS_STORAGE_KEY, SCREEN_READER_STATUS_EVENT } from "../lib/accessibility";
import { useScreenReaderStatusSync } from "../lib/screenReaderStatus";
import { getConfig } from "../lib/api/config";
import {
  ProcessingSpinner,
  ArmingIndicator,
  HudRecordingControls,
  PersistentRecordingControl,
} from "./hud/views/RecordingControls";
import {
  VoiceBars,
  AudioLevelsContext,
  type HUDState,
  EnhancementTimeoutIndicator,
  EnhancementErrorIndicator,
  BlockedIndicator,
} from "../components/Waveform";
import {
  ClaudePermissionPill,
  QuestionPanel,
} from "./hud/views/QuestionPanel";
import { agentProviderLabel } from "../lib/sessionName";
import { ProfileMark } from "../components/ProfileMark";
import {
  AGENT_MARK_COMPARISON_FIXTURE_ID,
  AgentMarkComparisonSurface,
} from "./hud/HudAgentMarkComparison";
import { CompactClaudePill } from "./hud/views/CompactResponse";
import { AgentConversationContext } from "../components/agentSpeak/AgentTimeline";
import { AgentVoiceCompletion } from "../components/agentSpeak/AgentVoiceGlyph";
import { ResponsePanel } from "./hud/views/ResponsePanel";
import { detectTruncation } from "./hud/views/ExpandedResponse";
import { PreviewCard } from "./hud/views/LivePreview";

// Bounded settle window between the card reaching seed size and the native
// window shrinking behind it. A plain timer (not onAnimationComplete) keeps the
// ordering deterministic in tests and in environments with no real compositor.
export const RECORDING_CONTROL_CARD_SETTLE_MS = 220;
export const RECORDING_CONTROL_CARD_SETTLE_REDUCED_MS = 20;
export const HUD_OUTCOME_COLLAPSE_SETTLE_MS = 320;
export const HUD_FINISH_PROCESSING_MIN_MS = 320;
const HUD_COMMAND_PENDING_TIMEOUT_MS = 5000;
/** How long an expired or held-failed question stays visible before the HUD
 *  releases it. Long enough to read the outcome, short enough that the pill
 *  does not become permanent furniture. */
export const TERMINAL_QUESTION_DWELL_MS = 4000;
/** How long a just-sent reply stays on the compact pill. Long enough to
 *  recognize what went out, not to read it (design.md state 6's math). Shared
 *  by the response reply and the answered question so the two confirmations
 *  cannot drift, and deliberately shorter than `TERMINAL_QUESTION_DWELL_MS` so
 *  a successful answer still clears faster than a failed one. */
function hasSpokenSummary(completion: AgentCompletion | null | undefined): boolean {
  return (
    completion?.kind === "response" &&
    Boolean(completion.spoken_summary && completion.spoken_summary.trim().length > 0)
  );
}

export const SENT_REPLY_HOLD_MS = 2200;
/** How long the console's last frame fades before it contracts to the pill. */
export const CONSOLE_CONTRACT_FADE_MS = 140;
/** How long a submitted card waits for the backend to show its draft as
 *  sending, refused or sent before an unconfirmed submit stops holding it. */
export const PENDING_SUBMIT_BRIDGE_MS = 3000;
export const LINGER_MS = 1500;

/** A leaving surface's last steady frame, kept in its own render slot. */
interface SurfaceSnapshot {
  kind: "question_pill" | "response" | "question";
  element: ReactElement;
}

interface DragState {
  startScreenX: number;
  startScreenY: number;
  startWinX: number;
  startWinY: number;
  hudW: number;
  guide: Window | null;
  dragging: boolean;
  scale: number;
  monitorX: number;
  monitorY: number;
  screenW: number;
  screenH: number;
  mode: HudGeometryMode;
  /** The measured card the drag STARTED with. The anchor is recovered through
   *  this exact value rather than whatever has been measured by the time the
   *  pointer comes up, so a card that grows mid-drag cannot shift the anchor. */
  measuredCard: MeasuredClaudeCard | null;
  /** The console only: its window height and display, for the two-axis snap
   *  and the per-display offset it saves instead of the pill anchor. */
  hudH: number;
  consoleDisplay: { displayKey: string; workArea: HudWorkArea } | null;
  lastScreenX: number;
  lastScreenY: number;
  totalMovement: number;
}

// DEFAULT_BAR_COLOR uses --frost-muted (decorative blue, per-theme: Light
// #5B7FA5, Dark #7FA3C9). Resolved at render time via a CSS custom property so
// the HUD pill re-reads the correct theme value.
const DEFAULT_BAR_COLOR_VAR = "var(--frost-muted)";
const MIC_ARMING_INDICATOR_DELAY_MS = 150;

interface PausedTimer {
  generation: number;
  remainingMs: number;
  startedAtMs: number | null;
}

/**
 * Fold a frozen fixture's wire events into a dictation model with the SAME
 * reducer production uses (dev-only; `HudForcedState.dictationEvents` is the
 * only caller). Hand-assembling a `DictationModel` here would let a fixture
 * paint a stamp on a non-authoritative or uncorrelated dictation, which is
 * exactly the state the reducer exists to refuse - the frozen state would
 * photograph a product that cannot happen.
 */
function forcedDictationModel(events: readonly AppEvent[] | undefined): DictationModel {
  if (events === undefined || events.length === 0) return INITIAL_DICTATION_MODEL;
  return events.reduce<DictationModel>((model, event) => {
    if (event.type === "dictation.state") {
      return dictationReducer(model, { type: "lifecycle", event });
    }
    if (event.type === "transcription.preview") {
      return dictationReducer(model, { type: "preview", event });
    }
    if (event.type === "enhancement.profile") {
      return dictationReducer(model, { type: "enhancement.profile", event });
    }
    return model;
  }, INITIAL_DICTATION_MODEL);
}

const BLOCKED_MS = 2000;

// The provider timeout dwells for the same beat as its sibling transients and
// inherits the whole machinery unchanged: generation counter, foreground
// pause/resume, queue-behind-dictation, and the shared 300ms hide tail.
export const ENHANCE_TIMEOUT_MS = 2000;

export interface HudProps {
  /**
   * Dev-only exact-route fixture. Unlike the UI Test cross-window channel,
   * this does not write localStorage and is owned by this mounted HUD.
   */
  uiQaFixtureId?: string;
}

export function HUD({ uiQaFixtureId }: HudProps = {}) {
  const { connected, send, subscribe, connectionGeneration } = useAppSocket();
  const dedicatedRecordingControlWindow =
    usesDedicatedRecordingControlWindow();
  const hudReducedMotion = useReducedMotion() ?? false;
  const [hudTheme, setHudTheme] = useState<HudDesktopTheme>(() =>
    document.documentElement.getAttribute("data-theme") === "dark"
      ? "dark"
      : "light",
  );
  const [conversations, setConversations] = useState<AgentConversation[]>([]);
  useEffect(() => { if (!connected) setConversations([]); }, [connected]);
  const levelsRef = useRef({ rms: 0, peak: 0 });
  const levels = levelsRef.current;
  const interactionRevisionRef = useRef({ connection: -1, revision: -1 });
  useEffect(() => {
    if (connected) return;
    interactionRevisionRef.current = { connection: -1, revision: -1 };
    setHudInteractions({});
  }, [connected]);
  const [dictation, dispatchDictation] = useReducer(
    dictationReducer,
    INITIAL_DICTATION_MODEL,
  );
  useEffect(() => {
    if (!connected) dispatchDictation({ type: "reset" });
  }, [connected]);
  const [startupArmingVisible, setStartupArmingVisible] = useState(false);
  const [transient, setTransient] = useState<TimedTransient | null>(null);
  const [liveCompletionQueue, dispatchCompletion] = useReducer(
    agentCompletionQueueReducer,
    undefined,
    createAgentCompletionQueueState,
  );
  const [liveHudInteractions, setHudInteractions] = useState<
    Record<string, AgentInteraction>
  >({});
  const [liveQuestionState, dispatchQuestion] = useReducer(
    agentQuestionReducer,
    undefined,
    createAgentQuestionState,
  );
  // The managed-session list behind the expanded card's tab strip (Task 4.4).
  // Arrival order and names are the backend's; this holds only the list and
  // the user's selection.
  const [sessionsState, dispatchSessions] = useReducer(
    agentSessionsReducer,
    undefined,
    createAgentSessionsState,
  );
  // Dev-only forced state (UI Test tab -> localStorage -> this window). It
  // replaces the three values every Claude presentation is derived from, so a
  // state can be held still and looked at instead of expiring with its reply
  // window. Null in production, where the hook is dead code.
  const transportedForcedHudState = useForcedHudState();
  const exactRouteForcedHudState =
    import.meta.env.DEV && uiQaFixtureId !== undefined
      ? forcedStateById(uiQaFixtureId)
      : null;
  const forcedHudState =
    uiQaFixtureId === undefined
      ? transportedForcedHudState
      : exactRouteForcedHudState;
  const uiQaStateId =
    exactRouteForcedHudState === null
      ? null
      : hudForcedStateQaId(exactRouteForcedHudState.id);
  const completionQueue = forcedHudState
    ? (forcedHudState.completionQueue ?? createAgentCompletionQueueState())
    : liveCompletionQueue;
  const hudInteractions = forcedHudState
    ? (forcedHudState.interactions ?? {})
    : liveHudInteractions;
  const questionState = forcedHudState
    ? { revision: 1, active: forcedHudState.question ?? null }
    : liveQuestionState;
  const presentedSessionsState = forcedHudState?.sessions
    ? {
        sessions: forcedHudState.sessions,
        activeHandle:
          forcedHudState.activeSessionHandle ??
          forcedHudState.sessions[0]?.session_handle ??
          null,
      }
    : sessionsState;
  // What last moved a card's strip: a response starting to speak, or a tab
  // the user picked (`stripTabFor`). A new active completion is speech
  // starting; typing holds the voice queue, so it never moves the strip.
  const [stripSwitch, setStripSwitch] = useState<StripSwitch | null>(null);
  const speakingCompletionId = completionQueue.active?.completion_id ?? null;
  const speakingConversation = completionQueue.active?.session_handle ?? null;
  useEffect(() => {
    if (speakingCompletionId === null) return;
    setStripSwitch({ kind: "speech", conversationHandle: speakingConversation });
    // Only a new completion switches; its conversation does not change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [speakingCompletionId]);
  const [expandedQuestionGeneration, setExpandedQuestionGeneration] =
    useState<number | null>(null);
  const [expandedResponseIdentity, setExpandedResponseIdentity] =
    useState<string | null>(null);
  const [questionIndex, setQuestionIndex] = useState(0);
  const [questionSelections, setQuestionSelections] = useState<
    Record<string, string[]>
  >({});
  const [questionFreeform, setQuestionFreeform] = useState<
    Record<string, string>
  >({});
  const [expandedPreviewRecordingId, setExpandedPreviewRecordingId] =
    useState<number | null>(null);
  const [previewNaturalTextHeight, setPreviewNaturalTextHeight] = useState(0);
  // The ONE measured painted-card rect (design decision 17). Every Claude
  // surface reports into it through `useMeasuredClaudeCard`, and it feeds both
  // the native bounds and the native region, in the same command. Exactly one
  // surface is mounted at a time, so one value is enough; the surface tag is
  // what keeps a stale measurement from sizing a different surface.
  //
  // Design decision 15 (a state that changes no footprint issues no native
  // geometry event) holds structurally rather than by luck. The height comes
  // ONLY from the painted card, through one clamp; speaking on/off swaps a
  // 16px chip for a 16px button inside a min-height 22 identity row, and live
  // -> processing swaps a glyph inside the rail's own row. Neither changes the
  // painted height, so the measured number is byte-for-byte the same on both
  // sides and the reconciler's applied-number comparison short-circuits. Do
  // not derive this height from `speaking` or from a reply phase. The compact
  // pill holds the same invariant on its WIDTH by always reserving the 16px
  // trailing slot (see `measureCompactPillWidth`), because since Task 3.2 that
  // width is also the native region.
  const [measuredCardSize, setMeasuredCardSize] =
    useState<MeasuredClaudeCard | null>(null);
  const onClaudeCardMeasured = useCallback((next: MeasuredClaudeCard) => {
    setMeasuredCardSize((current) =>
      current !== null &&
      current.surface === next.surface &&
      current.width === next.width &&
      current.height === next.height
        ? current
        : next,
    );
  }, []);
  const [expandedPreviewCardSize, setExpandedPreviewCardSize] = useState({
    width: PREVIEW_EXPANDED_CARD_W,
    height: PREVIEW_CARD_H,
  });
  const [recordingControlVisible, setRecordingControlVisible] = useState(
    readRecordingControlVisibility,
  );
  // Three separate pieces of state, deliberately not one. `recordingControlExpanded`
  // is pointer intent. `controlWindowExpanded` drives presentation.state and
  // therefore the native geometry. `controlCardExpanded` drives the card's animated
  // size and which child it renders. The card follows applied geometry on expand and
  // leads it on collapse, so the two never animate against each other.
  const [recordingControlExpanded, setRecordingControlExpanded] =
    useState(false);
  const [controlWindowExpanded, setControlWindowExpanded] = useState(false);
  const [controlCardExpanded, setControlCardExpanded] = useState(false);
  const [appliedGeometryMode, setAppliedGeometryMode] =
    useState<HudGeometryMode | null>(null);
  const [idleScreenReaderDetected, setIdleScreenReaderDetected] = useState(
    readScreenReaderStatus,
  );
  const [dictationCommandPending, setDictationCommandPending] =
    useState<HudDictationCommand | null>(null);
  const effectivePersistentControlRef = useRef(false);
  effectivePersistentControlRef.current =
    recordingControlVisible && idleScreenReaderDetected === false;
  const dedicatedArmingParkingEnabled =
    dedicatedRecordingControlWindow &&
    recordingControlVisible &&
    idleScreenReaderDetected !== true;
  const [hudFinishVisualHold, setHudFinishVisualHold] = useState(false);
  // Replies: the backend reply owner's snapshot, plus one keystroke buffer
  // per target (agentReplyState). The HUD derives no route of its own; it
  // reports what it painted and whether it has focus.
  const [liveReplyState, dispatchReply] = useReducer(
    agentReplyReducer,
    undefined,
    createAgentReplyState,
  );
  const [replyBuffers, setReplyBuffersState] = useState<ReplyBuffers>({});
  const replyBuffersRef = useRef<ReplyBuffers>({});
  const setReplyBuffers = useCallback((next: ReplyBuffers) => {
    replyBuffersRef.current = next;
    setReplyBuffersState(next);
  }, []);
  const replyStateRef = useRef(liveReplyState);
  replyStateRef.current = liveReplyState;
  const replyCommandRef = useRef<{ next: number; targets: Map<number, string> }>({
    next: 1,
    targets: new Map(),
  });
  useEffect(() => {
    if (connected) return;
    // Nothing typed outlives the connection it was typed on.
    dispatchReply({ type: "reset" });
    setReplyBuffers({});
    replyCommandRef.current.targets.clear();
  }, [connected, setReplyBuffers]);
  // A rail the user clicked into. Typing before any backend acknowledgement
  // lands in the buffer; the first update creates the draft.
  const [editingTarget, setEditingTarget] = useState<string | null>(null);
  const [managedCardFocused, setManagedCardFocused] = useState(false);
  const [hudWindowFocused, setHudWindowFocused] = useState(
    () => typeof document !== "undefined" && document.hasFocus(),
  );
  const editingInputRef = useRef<HTMLTextAreaElement>(null);
  const questionInputRef = useRef<HTMLTextAreaElement>(null);

  const dragRef = useRef<DragState | null>(null);
  const hudRootRef = useRef<HTMLDivElement | null>(null);
  const recordingControlCollapseRef = useRef<number | null>(null);
  const recordingControlCardSettleRef = useRef<number | null>(null);
  const hudFinishVisualHoldRef = useRef<number | null>(null);
  const startupArmingDelayRef = useRef<number | null>(null);
  const hudSessionWasActiveRef = useRef(false);
  const hudOutcomeCollapseRef = useRef(false);
  const controlCardExpandedRef = useRef(false);
  const transientGenerationRef = useRef(0);
  const transientTimerRef = useRef<PausedTimer | null>(null);
  const resetPersistentControlBeforeHide = useCallback(() => {
    if (recordingControlCollapseRef.current !== null) {
      window.clearTimeout(recordingControlCollapseRef.current);
      recordingControlCollapseRef.current = null;
    }
    if (recordingControlCardSettleRef.current !== null) {
      window.clearTimeout(recordingControlCardSettleRef.current);
      recordingControlCardSettleRef.current = null;
    }
    hudOutcomeCollapseRef.current = false;
    controlCardExpandedRef.current = false;
    setRecordingControlExpanded(false);
    setControlWindowExpanded(false);
    setControlCardExpanded(false);
  }, []);
  // The just-sent reply's bounded receipt (see the sent confirmation below).
  // Declared here because an expanded card keeps its identity through it.
  const [sentReplyState, setSentReplyState] = useState<{
    completion: AgentResponseCompletion;
    generation: number;
    text: string;
    target: string;
  } | null>(null);
  // A card whose reply is still on its way (handed to a wrapper, or held for
  // the agent's next Stop) or came back undelivered stays after its response
  // ends, so the text and its Copy never vanish with the card.
  const [retainedCard, setRetainedCard] = useState<{
    completion: AgentResponseCompletion;
    generation: number;
    target: string;
  } | null>(null);
  // The receipt's identity, written synchronously when the receipt is taken
  // so the expanded-identity pruning effect in the same commit keeps the card
  // expanded instead of collapsing it to the compact pill mid-send.
  const sentReceiptIdentityRef = useRef<string | null>(null);
  const summaryShownRef = useRef<{ completionId: string; shown: boolean } | null>(null);
  // The one surface state (hud/surfaceState.ts): which surface is on screen
  // and whether it is appearing, steady or leaving, with the last steady
  // frame a leaving surface keeps until the native hide. Committed after
  // each render, so a double render reads the same committed frame.
  const committedSurfaceRef = useRef<HudSurfaceFrame<SurfaceSnapshot> | null>(null);
  // The console's Sent confirmation (owner decision 9: Sending, then Sent).
  // The console keeps its size and shows Sent in its rail for the same hold a
  // card uses, then collapses to the pill.
  const [consoleSentHold, setConsoleSentHold] = useState<{
    identity: string;
    text: string;
  } | null>(null);
  const consoleSentTimerRef = useRef<number | null>(null);
  // The console leaves by fading its last painted frame at its own size, then
  // collapses. Content never changes mid-fade (the dismissed draft is already
  // gone backend-side), and the native window is never resized while visible.
  const [consoleFadeIdentity, setConsoleFadeIdentity] = useState<string | null>(null);
  const [consoleFadeMs, setConsoleFadeMs] = useState(HIDE_ANIMATION_MS);
  const consoleFadeTimerRef = useRef<number | null>(null);
  const fadeOutConsole = useCallback((identity: string, durationMs: number = HIDE_ANIMATION_MS) => {
    if (consoleFadeTimerRef.current !== null) return;
    setConsoleFadeIdentity(identity);
    setConsoleFadeMs(durationMs);
    consoleFadeTimerRef.current = window.setTimeout(() => {
      consoleFadeTimerRef.current = null;
      setConsoleFadeIdentity(null);
      setExpandedResponseIdentity((current) => (current === identity ? null : current));
    }, durationMs);
  }, []);
  useEffect(
    () => () => {
      if (consoleFadeTimerRef.current !== null) window.clearTimeout(consoleFadeTimerRef.current);
    },
    [],
  );
  // Card targets with a submit on its way. The card is kept from the moment
  // of the submit, so a response that ends before the draft shows "sending"
  // still leaves a card for the receipt to confirm.
  const pendingSubmitTargetsRef = useRef(new Map<string, number>());
  const activeCompletion = currentCompletion(completionQueue);
  const responseIdentity =
    activeCompletion !== null && completionQueue.active !== null
      ? `${activeCompletion.completion_id}:${completionQueue.active.generation}`
      : sentReplyState !== null
        ? `${sentReplyState.completion.completion_id}:${sentReplyState.generation}`
        : retainedCard !== null
          ? `${retainedCard.completion.completion_id}:${retainedCard.generation}`
          : null;
  // hud-session-name design decision 5 removes the card suffix's old
  // session-count gate: it is now computed inside ClaudeIdentityRow from the
  // resolved `sourceName` alone, so the cross-completion "have we seen more
  // than one session" tracking that used to feed it is gone.
  //
  // Which tab reads as current (design decision 13): the user's own selection
  // while it is alive, otherwise the session the response FIFO is on. Derived,
  // not stored, so an arriving snapshot can never silently re-target the tab
  // the user is reading.
  const liveSelectedSession =
    presentedSessionsState.sessions.find(
      (session) => session.session_handle === presentedSessionsState.activeHandle,
    ) ?? null;
  // An open console whose session is gone stays on a ghost of it while its
  // reply still holds text: the text is shown with "session ended" and Copy.
  // It never moves to another session.
  const consoleGhostRef = useRef<HudSession | null>(null);
  const consoleGhost = consoleGhostRef.current;
  const ghostSession =
    liveSelectedSession === null &&
    consoleGhost !== null &&
    expandedResponseIdentity === `managed:${consoleGhost.session_handle}` &&
    !presentedSessionsState.sessions.some(
      (session) => session.session_handle === consoleGhost.session_handle,
    ) &&
    consoleGhost.reply_target !== undefined &&
    (liveReplyState.drafts[consoleGhost.reply_target]?.text.trim().length ?? 0) > 0
      ? { ...consoleGhost, ended: true, accepts_reply: false, can_reply: false }
      : null;
  const selectedManagedSession = ghostSession ?? liveSelectedSession;
  // Which tab reads as current on a card's strip (`stripTabFor`). The open
  // console keeps its own selection.
  const stripActiveHandle = forcedHudState?.sessions
    ? presentedSessionsState.activeHandle
    : stripTabFor(presentedSessionsState, stripSwitch);
  const managedResponseIdentity =
    selectedManagedSession === null
      ? null
      : `managed:${selectedManagedSession.session_handle}`;
  const responseExpanded = forcedHudState
    ? forcedHudState.expanded === true
    : expandedResponseIdentity !== null &&
      (expandedResponseIdentity === responseIdentity ||
        expandedResponseIdentity === managedResponseIdentity);
  const managedConsoleExpanded =
    responseExpanded &&
    managedResponseIdentity !== null &&
    expandedResponseIdentity === managedResponseIdentity;
  const managedConsoleExpandedRef = useRef(false);
  useLayoutEffect(() => {
    managedConsoleExpandedRef.current = managedConsoleExpanded;
  }, [managedConsoleExpanded]);
  useLayoutEffect(() => {
    if (!managedConsoleExpanded) consoleGhostRef.current = null;
    else if (liveSelectedSession !== null) consoleGhostRef.current = liveSelectedSession;
  }, [managedConsoleExpanded, liveSelectedSession]);
  // `claude_reply_auto_submit` decides whether a spoken reply waits as a
  // draft (and so whether the rail reserves the submit hint). Read on connect
  // and whenever a new draft generation lands, so a settings change applies
  // to the next reply. Unknown counts as off: a waiting draft implies it.
  const [replyAutoSubmit, setReplyAutoSubmit] = useState(false);
  // The recording shortcuts the resting reply hints name. `lastOrigin` breaks
  // the tie when both are bound: whichever started the latest recording this
  // session, memory only (owner, 2026-10-24).
  const [replyShortcutConfig, setReplyShortcutConfig] = useState<{ pushToTalk: string; toggle: string } | null>(null);
  const [lastRecordingOrigin, setLastRecordingOrigin] = useState<"push_to_talk" | "toggle" | null>(null);
  // Server-authored and persisted: after three dictated replies the pill and
  // glance card stop naming the shortcut (owner, 2026-10-24).
  const [replyHintRetired, setReplyHintRetired] = useState(false);
  useEffect(() => {
    if (dictation.origin === "push_to_talk" || dictation.origin === "toggle") {
      setLastRecordingOrigin(dictation.origin);
    }
  }, [dictation.origin]);
  const replyShortcuts = useMemo(
    () => (replyShortcutConfig === null ? null : { ...replyShortcutConfig, lastOrigin: lastRecordingOrigin }),
    [replyShortcutConfig, lastRecordingOrigin],
  );
  const replyCaptureGeneration = Object.values(liveReplyState.drafts).find(
    (draft) => draft.compose_generation !== null,
  )?.compose_generation ?? null;
  useEffect(() => {
    if (!connected || forcedHudState !== null) return;
    let active = true;
    void (async () => {
      try {
        const config = await getConfig();
        if (active) {
          setReplyAutoSubmit(config.claude_reply_auto_submit === true);
          setReplyShortcutConfig({
            pushToTalk: typeof config.push_to_talk_shortcut === "string" ? config.push_to_talk_shortcut : "",
            toggle: typeof config.toggle_shortcut === "string" ? config.toggle_shortcut : "",
          });
        }
      } catch {
        // Keep the last known value; the hint is advisory.
      }
    })();
    return () => {
      active = false;
    };
  }, [connected, forcedHudState, replyCaptureGeneration]);
  const managedCompletion: AgentCompletion | null =
    selectedManagedSession === null
      ? null
      : {
          completion_id: managedResponseIdentity ?? "managed-session",
          sequence: 0,
          kind: "response",
          text:
            selectedManagedSession.latest_response ??
            "No response from this session yet.",
          agent: selectedManagedSession.agent,
          source_label: selectedManagedSession.source_name,
          source_name: selectedManagedSession.source_name,
          empty: selectedManagedSession.latest_response === "",
        };
  const onSelectSession = useCallback((handle: string) => {
    setStripSwitch({ kind: "pick", sessionHandle: handle });
    dispatchSessions({ type: "select", handle });
    setExpandedResponseIdentity(`managed:${handle}`);
  }, []);

  useEffect(() => {
    if (forcedHudState?.sessions === undefined || managedResponseIdentity === null) return;
    setExpandedResponseIdentity(managedResponseIdentity);
  }, [forcedHudState?.sessions, managedResponseIdentity]);
  const speaking =
    activeCompletion?.kind === "response" &&
    currentCompletionIsSpeaking(completionQueue);
  const replyWindow = ambientReplyWindow(completionQueue.active);
  // The reply target each surface addresses. A hook card and its session's
  // console resolve to the same `conv:` target, so they share one draft.
  const cardTarget =
    completionQueue.active?.session_handle != null
      ? replyTargetForConversation(completionQueue.active.session_handle)
      : (sentReplyState?.target ?? retainedCard?.target ?? null);
  const consoleTarget = selectedManagedSession?.reply_target ?? null;
  const consoleTargetRef = useRef(consoleTarget);
  consoleTargetRef.current = consoleTarget;
  const expandedResponseIdentityRef = useRef(expandedResponseIdentity);
  expandedResponseIdentityRef.current = expandedResponseIdentity;
  const consoleFading =
    consoleFadeIdentity !== null && consoleFadeIdentity === expandedResponseIdentity;
  const replyTarget = managedConsoleExpanded ? consoleTarget : cardTarget;
  // A frozen QA fixture supplies one draft for whichever surface it shows.
  const forcedReplyDraft = forcedHudState?.replyDraft;
  const forcedReplyTarget =
    forcedHudState?.question !== undefined
      ? replyTargetForQuestion(
          forcedHudState.question.session_handle,
          forcedHudState.question.generation,
        )
      : replyTarget;
  const replyState =
    forcedHudState === null
      ? liveReplyState
      : {
          ...createAgentReplyState(),
          drafts:
            forcedReplyDraft === undefined || forcedReplyTarget === null
              ? {}
              : {
                  [forcedReplyTarget]: {
                    draft_id: "forced-draft",
                    target_id: forcedReplyTarget,
                    kind: forcedHudState.question !== undefined ? ("question" as const) : ("reply" as const),
                    phase: forcedReplyDraft.phase,
                    text: forcedReplyDraft.text ?? "",
                    text_revision: 1,
                    compose_generation:
                      forcedReplyDraft.phase === "recording" || forcedReplyDraft.phase === "processing"
                        ? 1
                        : null,
                    capture_mode: null,
                    held: forcedReplyDraft.held === true,
                    delivery: "now" as const,
                    outcome: forcedReplyDraft.refused
                      ? {
                          revision: 1,
                          result: "refused" as const,
                          reason: forcedReplyDraft.refusedReason ?? ("refused" as const),
                        }
                      : null,
                  },
                },
        };
  const replyServerDraft =
    replyTarget !== null ? replyState.drafts[replyTarget] : undefined;
  const replyText =
    replyTarget !== null
      ? bufferedText(replyBuffers, replyTarget, replyServerDraft)
      : "";
  const replyCapturing =
    replyServerDraft?.phase === "recording" ||
    replyServerDraft?.phase === "processing";
  const replySending = replyServerDraft?.phase === "sending";
  const consoleSessionEnded =
    managedConsoleExpanded && selectedManagedSession?.ended === true;
  // The server's editing phase outlives the card it was opened on (the draft
  // belongs to the conversation). Kept text returns as an open field, but an
  // empty field nobody clicked into here is just the resting card: reopening
  // it made the next card arrive typing and take focus (owner, 2026-10-25).
  const replyEditing =
    replyTarget !== null &&
    !replyCapturing &&
    !replySending &&
    (editingTarget === replyTarget ||
      (replyServerDraft?.phase === "editing" && replyText.trim().length > 0));
  const replyPhase: ReplyPhase =
    replyServerDraft?.phase === "recording"
      ? "recording"
      : replyServerDraft?.phase === "processing"
        ? "processing"
        : replySending
          ? "sending"
          : replyEditing
            ? "editing"
            : replyServerDraft?.phase === "ready" && replyText.trim().length > 0
              ? "draft_ready"
              : "passive";
  const replyDraft: AmbientReplyDraft | null =
    replyPhase === "draft_ready" || replyPhase === "editing" || replyPhase === "sending"
      ? {
          phase: replyPhase,
          generation: replyServerDraft?.text_revision ?? 0,
          text: replyText,
        }
      : null;
  // Text that cannot be sent from here keeps its place, says why, and offers
  // Copy: a refused submit, a send nobody collected, a conversation with no
  // route, or an ended session. A held reply says it waits for the next Stop.
  const replyNotice: ReplyNotice | null =
    replyTarget === null || replyText.trim().length === 0 || replyCapturing
      ? null
      : replySending
        ? replyServerDraft?.held === true
          ? "held"
          : null
        : consoleSessionEnded
          ? "ended"
          : draftRefused(replyServerDraft)
            ? replyServerDraft?.outcome?.reason === "not_delivered"
              ? "not_delivered"
              : "refused"
            : replyServerDraft?.delivery === "none"
              ? "no_route"
              : null;

  // --- Sent confirmation ---
  // Keyed on the backend's correlated receipt for this exact draft, never on a
  // phase edge an expiry or cancel could also produce. A card keeps its
  // completion through the receipt; a console closes on it.
  const sentReplyTimerRef = useRef<number | null>(null);
  const seenReceiptsRef = useRef<Set<string> | null>(null);
  // The card the live response shows, if it takes replies.
  const liveCard =
    activeCompletion?.kind === "response" &&
    completionQueue.active !== null &&
    completionQueue.active.session_handle != null
      ? {
          completion: activeCompletion,
          generation: completionQueue.active.generation,
          target: replyTargetForConversation(completionQueue.active.session_handle),
        }
      : null;

  const liveCardRef = useRef(liveCard);
  liveCardRef.current = liveCard;

  // Keep the card while its reply is out or came back. A newer live response
  // takes over; Escape (agent.reply.close) or a discarded draft releases it.
  useLayoutEffect(() => {
    const holds = (target: string) => {
      const submittedAt = pendingSubmitTargetsRef.current.get(target);
      if (submittedAt !== undefined && Date.now() - submittedAt < PENDING_SUBMIT_BRIDGE_MS) return true;
      const draft = replyState.drafts[target];
      return (
        draft !== undefined &&
        draft.text.trim().length > 0 &&
        (draft.phase === "sending" || draft.outcome !== null)
      );
    };
    if (liveCard !== null && holds(liveCard.target)) {
      setRetainedCard((current) =>
        current !== null &&
        current.target === liveCard.target &&
        current.generation === liveCard.generation &&
        current.completion.completion_id === liveCard.completion.completion_id
          ? current
          : liveCard,
      );
      return;
    }
    if (retainedCard === null) return;
    const sameAsLive =
      liveCard !== null &&
      liveCard.generation === retainedCard.generation &&
      liveCard.completion.completion_id === retainedCard.completion.completion_id;
    if ((activeCompletion !== null && !sameAsLive) || !holds(retainedCard.target)) {
      setRetainedCard(null);
    }
  });

  // Keyed on the backend's correlated receipt, which reaches the HUD before
  // the response's terminal state, so the card it confirms is still showing.
  useLayoutEffect(() => {
    const keys = replyState.receipts.map(
      (receipt) => `${replyState.epoch}:${receipt.draft_id}`,
    );
    if (seenReceiptsRef.current === null) {
      // The first snapshot after connecting replays old receipts silently.
      seenReceiptsRef.current = new Set(keys);
      return;
    }
    const seen = seenReceiptsRef.current;
    for (const receipt of replyState.receipts) {
      const key = `${replyState.epoch}:${receipt.draft_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      pendingSubmitTargetsRef.current.delete(receipt.target_id);
      if (
        managedConsoleExpanded &&
        receipt.target_id === consoleTarget &&
        managedResponseIdentity !== null
      ) {
        const identity = managedResponseIdentity;
        if (consoleSentTimerRef.current !== null) {
          window.clearTimeout(consoleSentTimerRef.current);
        }
        setConsoleSentHold({ identity, text: receipt.text });
        consoleSentTimerRef.current = window.setTimeout(() => {
          consoleSentTimerRef.current = null;
          // The fade shows the frozen Sent frame, so the hold can end now.
          setConsoleSentHold((current) => (current?.identity === identity ? null : current));
          fadeOutConsole(identity);
        }, SENT_REPLY_HOLD_MS);
        continue;
      }
      const card =
        liveCard?.target === receipt.target_id
          ? liveCard
          : retainedCard?.target === receipt.target_id
            ? retainedCard
            : null;
      if (card === null) continue;
      if (retainedCard?.target === receipt.target_id) setRetainedCard(null);
      if (sentReplyTimerRef.current !== null) {
        window.clearTimeout(sentReplyTimerRef.current);
      }
      const sent = {
        completion: card.completion,
        generation: card.generation,
        text: receipt.text,
        target: receipt.target_id,
      };
      const identity = `${sent.completion.completion_id}:${sent.generation}`;
      sentReceiptIdentityRef.current = identity;
      setSentReplyState(sent);
      sentReplyTimerRef.current = window.setTimeout(() => {
        if (sentReceiptIdentityRef.current === identity) {
          sentReceiptIdentityRef.current = null;
        }
        setSentReplyState((current) =>
          current?.completion.completion_id === sent.completion.completion_id &&
          current.generation === sent.generation
            ? null
            : current,
        );
        sentReplyTimerRef.current = null;
      }, SENT_REPLY_HOLD_MS);
    }
    // liveCard and retainedCard are read at the receipt's own commit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replyState.receipts, replyState.epoch, managedConsoleExpanded, consoleTarget]);

  useEffect(() => {
    if (!connected) seenReceiptsRef.current = null;
  }, [connected]);

  // A newer response replaces the receipt immediately.
  useLayoutEffect(() => {
    if (
      sentReplyState !== null &&
      activeCompletion?.kind === "response" &&
      (sentReplyState.completion.completion_id !== activeCompletion.completion_id ||
        sentReplyState.generation !== completionQueue.active?.generation)
    ) {
      if (sentReplyTimerRef.current !== null) {
        window.clearTimeout(sentReplyTimerRef.current);
        sentReplyTimerRef.current = null;
      }
      sentReceiptIdentityRef.current = null;
      setSentReplyState(null);
    }
  }, [activeCompletion, completionQueue.active?.generation, sentReplyState]);

  useEffect(
    () => () => {
      if (sentReplyTimerRef.current !== null) {
        window.clearTimeout(sentReplyTimerRef.current);
      }
      if (consoleSentTimerRef.current !== null) {
        window.clearTimeout(consoleSentTimerRef.current);
      }
    },
    [],
  );
  // Forced state can hold the sent confirmation indefinitely (no 2,200ms
  // timer), so the harness can review a state that otherwise self-clears
  // before a reviewer can look at it or screenshot it.
  const sentReply =
    forcedHudState?.sentReply !== undefined
      ? forcedHudState.sentReply
      : managedConsoleExpanded &&
          consoleSentHold !== null &&
          consoleSentHold.identity === managedResponseIdentity
        ? { completionId: consoleSentHold.identity, generation: 0, text: consoleSentHold.text }
        : sentReplyState === null
        ? null
        : {
            completionId: sentReplyState.completion.completion_id,
            generation: sentReplyState.generation,
            text: sentReplyState.text,
          };
  // The authoritative terminal snapshot intentionally carries no completion.
  // Keep the exact previous response only for the bounded sent receipt. A live
  // completion always wins, so queue promotion cannot be masked by stale UI.
  const completion =
    activeCompletion ?? sentReplyState?.completion ?? retainedCard?.completion ?? null;
  const displayedCompletion =
    managedConsoleExpanded && managedCompletion !== null
      ? managedCompletion
      : completion;

  // --- Answered-question sent confirmation ---
  // The same "recognize, not read" treatment the response pill has always had,
  // and for the same reason: under auto-submit the user never sees a draft, so
  // without this the pill simply vanishes and nothing ever confirms that what
  // they said is what went to Claude.
  //
  // It was inverted before this: `activeQuestion` was nulled the instant the
  // phase became `answered`, while the two FAILURE outcomes (expired,
  // held_failed) each dwelled `TERMINAL_QUESTION_DWELL_MS`. Failure lingered
  // 4s, success got nothing. Success now holds for the response pill's 2,200 ms
  // - deliberately shorter than the failure dwell, so a good answer still feels
  // quicker to clear than a bad one.
  const [answeredQuestionHold, setAnsweredQuestionHold] = useState<{
    sessionHandle: string;
    generation: number;
    text: string;
  } | null>(null);
  const answeredQuestionTimerRef = useRef<number | null>(null);
  const submittedAnswerTextRef = useRef<string>("");
  const questionPhaseTrackRef = useRef<{
    generation: number | null;
    phase: string | null;
  }>({ generation: null, phase: null });

  useEffect(() => {
    const generation = questionState.active?.generation ?? null;
    const sessionHandle = questionState.active?.session_handle ?? null;
    const phase = questionState.active?.phase ?? null;
    const previous = questionPhaseTrackRef.current;
    const justAnswered =
      phase === "answered" &&
      generation !== null &&
      !(previous.generation === generation && previous.phase === "answered");
    questionPhaseTrackRef.current = { generation, phase };
    if (!justAnswered) return;
    // Under AUTO-SUBMIT the backend resolves the draft itself, so this surface
    // never calls `submitQuestionAnswers` and the ref stays empty. Reading only
    // the ref therefore misclassified the auto-submit case as "answered
    // somewhere else" and released instantly - killing the confirmation in the
    // exact mode whose whole justification is that the user never sees a draft.
    // The snapshot's `answers` map is written by both HUD paths and by neither
    // native one, so it is the correct authority; the ref stays as a fallback.
    const text = (
      submittedAnswerTextRef.current.trim() ||
      Object.values(questionState.active?.answers ?? {})
        .join(", ")
        .trim()
    );
    if (sessionHandle === null) return;
    if (text.length === 0) {
      sendAgentQuestionOpportunityAck(send, sessionHandle, generation);
      return;
    }
    if (answeredQuestionTimerRef.current !== null) {
      window.clearTimeout(answeredQuestionTimerRef.current);
    }
    setAnsweredQuestionHold({ sessionHandle, generation, text });
    answeredQuestionTimerRef.current = window.setTimeout(() => {
      sendAgentQuestionOpportunityAck(send, sessionHandle, generation);
      setAnsweredQuestionHold(null);
      answeredQuestionTimerRef.current = null;
    }, SENT_REPLY_HOLD_MS);
  }, [questionState.active?.generation, questionState.active?.phase, send]);

  useEffect(
    () => () => {
      if (answeredQuestionTimerRef.current !== null) {
        window.clearTimeout(answeredQuestionTimerRef.current);
      }
    },
    [],
  );

  // AgentInteraction stays content-free on the wire, so currentClaudeAttention
  // always returns detail: null there. The dev harness needs to review the
  // detail line design (single-line vs two-line, the spine-at-2-lines cutoff)
  // before a backend round ever plumbs real text through, so a forced
  // scenario may supply `permissionDetail` and have it override the null here.
  // Production (forcedHudState === null) is untouched.
  const claudeAttentionBase = currentClaudeAttention(
    Object.values(hudInteractions),
  );
  const claudeAttention =
    claudeAttentionBase !== null && forcedHudState?.permissionDetail !== undefined
      ? { ...claudeAttentionBase, detail: forcedHudState.permissionDetail }
      : claudeAttentionBase;
  // Terminal phases are shown briefly so the outcome is legible, then the HUD
  // lets go. Locked decision 7: "Expiry closes the hook, dismisses the pill,
  // and frees dictation hotkeys." The backend keeps the expired question in its
  // snapshot indefinitely (correctly - it is the record of what happened), so
  // without this dwell the HUD sat on "Answer in Claude Code" forever.
  const questionPhase = questionState.active?.phase ?? null;
  const questionGeneration = questionState.active?.generation ?? null;
  const questionTerminal =
    questionPhase === "expired" || questionPhase === "held_failed";
  const [dismissedQuestionGeneration, setDismissedQuestionGeneration] =
    useState<number | null>(null);

  useEffect(() => {
    if (!questionTerminal || questionGeneration === null) return;
    if (dismissedQuestionGeneration === questionGeneration) return;
    const sessionHandle = questionState.active?.session_handle;
    if (sessionHandle === undefined) return;
    const timer = window.setTimeout(() => {
      sendAgentQuestionOpportunityAck(
        send,
        sessionHandle,
        questionGeneration,
      );
      setDismissedQuestionGeneration(questionGeneration);
    }, TERMINAL_QUESTION_DWELL_MS);
    return () => window.clearTimeout(timer);
  }, [
    questionTerminal,
    questionGeneration,
    questionState.active?.session_handle,
    dismissedQuestionGeneration,
    send,
  ]);

  useEffect(() => {
    const active = questionState.active;
    if (active?.phase !== "deferred") return;
    sendAgentQuestionOpportunityAck(
      send,
      active.session_handle,
      active.generation,
    );
  }, [
    questionState.active?.generation,
    questionState.active?.phase,
    questionState.active?.session_handle,
    send,
  ]);

  // An answered question survives for exactly its sent-confirmation hold, then
  // releases like any other. The hold is generation-scoped, so a newer question
  // arriving mid-confirmation is never masked by the previous one's receipt.
  const answeredHoldActive =
    answeredQuestionHold !== null &&
    questionState.active?.phase === "answered" &&
    questionState.active.generation === answeredQuestionHold.generation;
  const activeQuestion =
    (questionState.active?.phase === "answered" && !answeredHoldActive) ||
    questionState.active?.phase === "deferred" ||
    (questionTerminal &&
      questionGeneration !== null &&
      dismissedQuestionGeneration === questionGeneration)
      ? null
      : questionState.active;
  const compactQuestion =
    activeQuestion === null ? null : compactQuestionPillProps(activeQuestion);
  // The confirmation is a compact-pill state. An expanded card collapsing to a
  // receipt would be a second, louder transition on a question the user has
  // already finished with.
  const questionExpanded =
    activeQuestion !== null &&
    !answeredHoldActive &&
    (forcedHudState?.question !== undefined ||
      expandedQuestionGeneration === activeQuestion.generation);

  // --- The compact question's reply rail ---
  //
  // A question answered by voice is a reply like any other, so it uses the
  // response pill's rail vocabulary verbatim (decisions 2026-07-28: "the
  // compact pill grows a rail only when it has something to say" and "one
  // `ReplyRail` serves the compact pill, the expanded card, and the question
  // card"). Only the question *card* treats "editing" as its resting state and
  // keeps a textarea permanently open; compact must close back to identity-only
  // or the pill would never be one row again.
  //
  // The live/processing text comes from the exact compose lease, never from
  // whatever dictation happens to be running: a global recording that is not
  // routed to THIS question must not paint into its rail.
  // A question's answer draft lives in the reply owner under its own target.
  const questionTarget =
    activeQuestion !== null
      ? replyTargetForQuestion(activeQuestion.session_handle, activeQuestion.generation)
      : null;
  const questionServerDraft =
    questionTarget !== null ? replyState.drafts[questionTarget] : undefined;
  const questionDraftText =
    questionTarget !== null
      ? bufferedText(replyBuffers, questionTarget, questionServerDraft)
      : "";
  // The reply owner holds the one record of a running capture.
  const questionOwnsDictation =
    questionServerDraft?.compose_generation != null &&
    dictation.composeGeneration === questionServerDraft.compose_generation;
  const questionCapturePhase =
    questionServerDraft?.phase === "recording"
      ? "live"
      : questionServerDraft?.phase === "processing"
        ? "processing"
        : null;
  const questionRailPhase: Exclude<ReplyRailPhase, "resting"> | null =
    answeredHoldActive
      ? "sent"
      : activeQuestion === null
        ? null
        : questionCapturePhase !== null
          ? questionCapturePhase
          : questionDraftText.trim().length > 0
            ? "draft"
            : null;
  const questionRailText =
    questionRailPhase === "sent"
      ? (answeredQuestionHold?.text ?? "")
      : questionRailPhase === "live" || questionRailPhase === "processing"
        ? (forcedHudState?.replyPreview ??
            (questionOwnsDictation ? (dictation.preview?.text ?? "") : ""))
        : questionRailPhase === "draft"
          ? questionDraftText
          : "";
  // The current question's freeform answer is the reply owner's draft, so a
  // typed edit and a re-dictation always act on the same text. Other
  // questions in a batch keep their HUD-local freeform text.
  const currentQuestionKey =
    activeQuestion?.questions[activeQuestion.current_index]?.question ?? null;
  const presentedFreeform =
    currentQuestionKey !== null && questionDraftText.length > 0
      ? { ...questionFreeform, [currentQuestionKey]: questionDraftText }
      : questionFreeform;
  const questionRailStableCharCount =
    forcedHudState?.replyPreview !== undefined
      ? forcedHudState.replyPreview.length
      : (dictation.preview?.stableCharCount ?? 0);

  // A frozen fixture is an explicit presentation mode, not another live input
  // in the priority ladder. Continue collecting live state underneath it so
  // clearing the fixture returns to the current HUD.
  // A frozen dictation and a frozen timeout are the fixture's own inputs to the
  // production reducer and the production presentation function; nothing here
  // bypasses either. The live model keeps collecting underneath, so clearing
  // the fixture returns to the current HUD.
  const forcedDictation = forcedHudState
    ? forcedDictationModel(forcedHudState.dictationEvents)
    : INITIAL_DICTATION_MODEL;
  const forcedTransient: TimedTransient | null =
    forcedHudState?.errorProvider !== undefined
      ? {
          state: "enhance_error",
          generation: -1,
          provider: forcedHudState.errorProvider,
        }
      : forcedHudState?.timeoutProvider !== undefined
      ? {
          state: "enhance_timeout",
          generation: 1,
          provider: forcedHudState.timeoutProvider,
        }
      : null;
  // The stamp reads the dictation model directly, so the frozen model has to
  // reach it too, not just `derivePresentation`.
  const presentedDictation = forcedHudState ? forcedDictation : dictation;
  const presentation = forcedHudState
    ? derivePresentation({
        dictation: forcedDictation,
        transient: forcedTransient,
        claudeAttention,
        activeQuestion,
        questionExpanded,
        hasCompletion: completion !== null,
        persistentControlVisible: false,
        persistentControlExpanded: false,
        idleScreenReaderDetected: false,
        startCommandPending: false,
        finishVisualHold: false,
        dedicatedStartPendingEligible: false,
        startupArmingVisible: forcedHudState.startupArming === true,
      })
    : derivePresentation({
        dictation,
        transient,
        claudeAttention,
        activeQuestion,
        questionExpanded,
        hasCompletion: completion !== null || responseExpanded,
        persistentControlVisible: !dedicatedRecordingControlWindow && recordingControlVisible,
        persistentControlExpanded: controlWindowExpanded,
        idleScreenReaderDetected: idleScreenReaderDetected !== false,
        startCommandPending: dictationCommandPending === "start",
        finishVisualHold: hudFinishVisualHold,
        dedicatedStartPendingEligible: dedicatedRecordingControlWindow && recordingControlVisible,
        startupArmingVisible,
      });
  const visible = presentation.mode !== "hidden";
  // Hiding is deliberately delayed for the close animation. Keep the exact
  // Preview surface mounted during that delay so it fades at its current size
  // instead of being replaced by the generic compact pill.
  const closingPreview =
    presentation.mode === "hidden" && appliedGeometryMode === "preview";
  const previewSurfaceMounted =
    presentation.mode === "preview" || closingPreview;
  // When the user disables the persistent control, keep its resting seed in
  // the hidden WebView instead of replacing it with the generic pill. The
  // native hide runs first, then this seed becomes the dormant painted frame
  // that the next show can reveal without mounting or collapsing anything.
  const parkedRecordingControl =
    !dedicatedRecordingControlWindow &&
    presentation.mode === "hidden" &&
    !recordingControlVisible;
  // The vessel is expanded to the active width but has made no recording claim.
  const arming =
    presentation.mode === "control" && presentation.state === "arming";
  const transientForeground =
    transient !== null &&
    presentation.mode === "pill" &&
    presentation.state === transient.state;
  const previewExpanded =
    forcedHudState?.previewExpanded === true ||
    ((dictation.phase !== "idle" || closingPreview) &&
      dictation.recordingId !== null &&
      expandedPreviewRecordingId === dictation.recordingId);
  const hudOriginRecording =
    dictation.authoritative &&
    dictation.origin === "hud" &&
    dictation.phase === "recording";
  const hudOriginSessionVisual =
    (dictation.authoritative &&
      dictation.origin === "hud" &&
      dictation.phase !== "idle") ||
    hudFinishVisualHold;
  const returningToRestingControl =
    presentation.mode === "control" && hudSessionWasActiveRef.current;
  const returningControlVessel =
    returningToRestingControl || hudOutcomeCollapseRef.current;
  // A card held for its reply (sending, then Sent) after its response ended
  // keeps the shape it had: a glance card stays a glance card instead of
  // dropping to the compact pill when the queue's summary flag goes with it.
  const heldCardCompletion =
    activeCompletion === null
      ? (sentReplyState?.completion ?? retainedCard?.completion ?? null)
      : null;
  const liveSummaryShown =
    completionQueue.showSummaryText && hasSpokenSummary(activeCompletion);
  if (activeCompletion !== null) {
    summaryShownRef.current = {
      completionId: activeCompletion.completion_id,
      shown: liveSummaryShown,
    };
  }
  const heldSummaryShown =
    heldCardCompletion !== null &&
    summaryShownRef.current?.completionId === heldCardCompletion.completion_id &&
    summaryShownRef.current.shown;
  const effectiveShowSummaryText =
    completionQueue.showSummaryText || heldSummaryShown;
  const summaryActive =
    effectiveShowSummaryText &&
    hasSpokenSummary(activeCompletion ?? heldCardCompletion) &&
    !responseExpanded;
  const presentationGeometryMode: HudGeometryMode =
    parkedRecordingControl
      ? "seed"
      : presentation.mode === "control"
      ? presentation.state
      : presentation.mode === "panel"
        ? responseExpanded || summaryActive
          ? responseExpanded
            ? "response"
            : "summary"
          : "panel"
        : presentation.mode === "question_pill"
          ? "panel"
        : presentation.mode === "question"
          ? "question"
        : presentation.mode === "pill" && presentation.state === "permission"
          ? "permission"
        : presentation.mode === "preview"
          ? "preview"
          : presentation.mode === "pill" &&
              (presentation.state === "recording" ||
                presentation.state === "processing") &&
              hudOriginSessionVisual
            ? "active"
            : "pill";
  // The pill and glance card keep one native window while on screen: it may
  // grow when a card outgrows it, never shrink, until the surface hides. The
  // tallest window so far rides on the measurement every geometry reader
  // already gets, so anchor, drag and bounds math all agree.
  const replySurfaceGeometry =
    presentationGeometryMode === "panel" || presentationGeometryMode === "summary";
  const retainedReplyWindowHRef = useRef(0);
  if (!replySurfaceGeometry) retainedReplyWindowHRef.current = 0;
  const geometryMeasuredCard = useMemo<MeasuredClaudeCard | null>(() => {
    if (!replySurfaceGeometry) return measuredCardSize;
    const card: MeasuredClaudeCard = {
      ...(measuredCardSize ?? { surface: "panel", width: 0, height: 0 }),
      retainedWindowH: retainedReplyWindowHRef.current,
    };
    const windowH = hudModeGeometry(presentationGeometryMode, card).windowH;
    retainedReplyWindowHRef.current = Math.max(retainedReplyWindowHRef.current, windowH);
    return { ...card, retainedWindowH: retainedReplyWindowHRef.current };
  }, [measuredCardSize, presentationGeometryMode, replySurfaceGeometry]);
  const geometryMeasuredCardRef = useRef(geometryMeasuredCard);
  geometryMeasuredCardRef.current = geometryMeasuredCard;
  // Decision 10, the scoped FIFO supersession (Task 4.5): every new FIFO
  // promotion normally resets the card to compact, so a stale identity can
  // never stay "expanded" under a successor's content. But while the card is
  // open AND two or more sessions exist, a promotion in a BACKGROUND session
  // must not do that - the backend lights that capsule's unread dot in the
  // next `agent.sessions` snapshot instead, and the user's own selection
  // (agentSessionsReducer, Task 4.2) already keeps pointing at what they were
  // reading. Retarget to the new identity so the very next single-session
  // promotion still resets normally: the guard is tight, not permanent.
  const hasStrip = hasSessionStrip(presentedSessionsState);
  // The clean-geometry family is semantic rather than geometric. `pill` is also
  // used by permissions and transient statuses, all of which
  // retain the measured/native path. Ordinary dictation and Live Preview keep
  // the owner-confirmed Tauri visibility contract. The retired shared-window
  // fallback used the same unshaped geometry path with a native borderless
  // visibility owner; production instead isolates the permanent control in
  // its dedicated window.
  const permanentControlPresentation =
    !dedicatedRecordingControlWindow &&
    (parkedRecordingControl ||
      presentation.mode === "control" ||
      (presentation.mode === "pill" &&
        (presentation.state === "recording" ||
          presentation.state === "processing") &&
        dictation.origin === "hud"));
  const ordinaryRecordingPresentation =
    presentation.mode === "pill" &&
    (presentation.state === "recording" ||
      presentation.state === "processing") &&
    dictation.origin !== "hud";
  // The centered console (every expanded response, managed or hook-only) is
  // a persistent, resizable surface: unshaped, ordinary Tauri geometry.
  const consoleOpen =
    presentation.mode === "panel" && presentationGeometryMode === "response";
  // A fading console repeats its last painted frame, so the dismissed draft,
  // the Sent hold or a session update never changes it as it leaves; a
  // hiding card does the same until the native hide (HIDE_ANIMATION_MS).
  const surfaceFrame = forcedHudState !== null
    ? null
    : nextSurfaceFrame(committedSurfaceRef.current, {
        desired: visible ? hudSurfaceForMode(presentationGeometryMode) : null,
        appliedMode: appliedGeometryMode,
        // The rendered frame is recorded when it commits (below).
        snapshot: null,
        timedLeave: consoleFading ? "console" : null,
      });
  const leavingSnapshot =
    surfaceFrame?.phase === "leaving" ? surfaceFrame.snapshot : null;
  const closingReplySurface =
    !visible && leavingSnapshot !== null && surfaceFrame?.surface === "card";
  const panelCompletion =
    presentation.mode === "panel" ? displayedCompletion : null;
  // Expand and contract between the centered console and the pill-side
  // surfaces move the window, as does a handoff between families. Until the
  // window has its new place, the new card stays hidden, then springs in
  // there (owner, 2026-10-24: like Live Preview, never shrinking in place
  // and then moving).
  const surfaceSettling = surfaceAwaitingWindow(surfaceFrame);
  // A console that already faded with nothing after it paints nothing more
  // and hides at once.
  const managedConsoleDismissedToHidden =
    !visible && surfaceFrame?.surface === "console" && surfaceFrame.faded;
  const consoleResize = useConsoleResize(consoleOpen);
  // Once the HUD has hidden, nothing is expanded any more: a console hidden
  // without its own dismissal (the response withdrawn, a reset) must not
  // reopen as the console for the next response, which arrives as a card.
  useEffect(() => {
    if (forcedHudState !== null || visible || appliedGeometryMode !== null) return;
    if (consoleFadeIdentity !== null) return;
    setExpandedResponseIdentity((current) => (current === null ? current : null));
  }, [appliedGeometryMode, consoleFadeIdentity, forcedHudState, visible]);
  // The compact pill, compact question pill and glance card keep one fixed
  // window while on screen and animate inside it (geometry.ts,
  // `replySurfaceWindowHeight`). They use ordinary Tauri geometry with no
  // native region, like the console and Live Preview; the empty area above
  // the card is click-through instead.
  const replySurfacePresentation =
    (presentation.mode === "panel" || presentation.mode === "question_pill") &&
    (presentationGeometryMode === "panel" || presentationGeometryMode === "summary");
  useHudClickThrough(replySurfacePresentation && forcedHudState === null);
  // A closing reply surface keeps its layout and its clean-Tauri hide, so it
  // fades where it was instead of dropping to the window's centre.
  const replySurfaceLayout = replySurfacePresentation || closingReplySurface;
  const cleanGeometryPresentation =
    presentation.mode === "preview" ||
    consoleOpen ||
    replySurfaceLayout ||
    (dedicatedRecordingControlWindow && arming) ||
    permanentControlPresentation ||
    ordinaryRecordingPresentation ||
    (presentation.mode === "pill" &&
      (presentation.state === "recording" ||
        presentation.state === "processing") &&
      dictation.origin === "hud");
  // React commits the new card before the asynchronous native reconciler can
  // replace the visible permanent control's 132x52 bounds. Suppress only this
  // cross-family paint. The gate reopens from the reconciler's applied-mode
  // acknowledgement, after the final recording geometry has been shown.
  const preparingCleanRecordingFromPermanent =
    appliedGeometryMode !== null &&
    isHudControlFamilyMode(appliedGeometryMode) &&
    (presentation.mode === "preview" || ordinaryRecordingPresentation);
  const preparingPermanentControlFromOtherFamily =
    permanentControlPresentation &&
    appliedGeometryMode !== null &&
    !isHudControlFamilyMode(appliedGeometryMode);
  useEffect(() => {
    const receiptIdentity = sentReceiptIdentityRef.current;
    setExpandedResponseIdentity((current) => {
      if (current === null) return null;
      // An open console stays on its session, and collapses (never moves to
      // another session) when that session ends.
      if (current.startsWith("managed:")) {
        return current === managedResponseIdentity ? current : null;
      }
      // A sent reply keeps its expanded card through the receipt, then the
      // card leaves whole rather than shrinking to the pill first. With no
      // response showing there is nothing to collapse to; the next response
      // decides.
      if (
        current === receiptIdentity ||
        current === responseIdentity ||
        responseIdentity === null
      ) {
        return current;
      }
      return hasStrip ? responseIdentity : null;
    });
  }, [responseIdentity, managedResponseIdentity]);

  useEffect(() => {
    if (activeQuestion === null) {
      setExpandedQuestionGeneration(null);
      return;
    }
    if (!questionCanAnswer(activeQuestion)) {
      return;
    }
    setQuestionIndex(activeQuestion.current_index);
    setQuestionSelections((current) => {
      const next = { ...current };
      for (const [question, answer] of Object.entries(activeQuestion.answers)) {
        next[question] = [answer];
      }
      return next;
    });
    // Deliberately NOT expanding on recording/processing/draft_ready/editing.
    // Answering by voice is the quiet path: the compact pill grows the same
    // reply rail a response does and nothing else changes. Expansion is for
    // *reading* the options, so it stays an explicit double-click or view
    // shortcut. Auto-expanding also dragged native foreground with it (see
    // `hudEditing` below), which threw the caret out of whatever the user was
    // typing in the moment they started dictating.
  }, [
    activeQuestion?.generation,
    activeQuestion?.current_index,
    activeQuestion?.phase,
    activeQuestion?.answers,
  ]);

  /** Clicking a compact question draft opens that question's card, which is
   *  where every answering affordance already lives. */
  const onQuestionDraftEdit = useCallback(() => {
    if (activeQuestion === null) return;
    setExpandedQuestionGeneration(activeQuestion.generation);
  }, [activeQuestion]);

  /** Open the console on one session, ready to type: the console is the
   *  surface for writing a reply, so it opens with its field focused. */
  const openConsoleOn = useCallback(
    (handle: string) => {
      const session = presentedSessionsState.sessions.find(
        (candidate) => candidate.session_handle === handle,
      );
      setStripSwitch({ kind: "pick", sessionHandle: handle });
      dispatchSessions({ type: "select", handle });
      setExpandedResponseIdentity(`managed:${handle}`);
      if (session?.reply_target !== undefined) setEditingTarget(session.reply_target);
    },
    [presentedSessionsState.sessions],
  );

  const toggleHudExpansion = useCallback(() => {
    if (activeQuestion !== null) {
      if (expandedQuestionGeneration === activeQuestion.generation) {
        setExpandedQuestionGeneration(null);
      } else {
        setExpandedQuestionGeneration(activeQuestion.generation);
      }
      return;
    }
    if (expandedResponseIdentity !== null) {
      // Contract: the console's last frame fades out where it is, then the
      // card returns at the pill. It never shrinks in place and then jumps.
      fadeOutConsole(expandedResponseIdentity, CONSOLE_CONTRACT_FADE_MS);
      return;
    }
    // A wrapped session's own hook response opens its console (same draft);
    // a hook-only response opens its card. Hook items carry the conversation
    // handle, so compare against each session's conversation handle.
    const hookConversation = completionQueue.active?.session_handle ?? null;
    const owner =
      hookConversation === null
        ? null
        : (presentedSessionsState.sessions.find(
            (session) => session.conversation_handle === hookConversation,
          ) ?? null);
    if (owner !== null) {
      openConsoleOn(owner.session_handle);
      return;
    }
    if (responseIdentity !== null) {
      setExpandedResponseIdentity(responseIdentity);
      return;
    }
    const selection = selectSessionOnExpand(presentedSessionsState, null);
    if (selection.activeHandle !== null) openConsoleOn(selection.activeHandle);
  }, [
    activeQuestion,
    expandedQuestionGeneration,
    expandedResponseIdentity,
    responseIdentity,
    completionQueue.active?.session_handle,
    presentedSessionsState,
    openConsoleOn,
    fadeOutConsole,
  ]);

  // Focusability goes through the native command, NEVER `setFocusable`. Tauri's
  // version reaches tao's `WindowFlags::apply_diff`, which hides any window tao
  // still believes is invisible - and tao believes exactly that about this HUD
  // forever, because every show is the raw `ShowWindow` inside
  // `set_hud_window_region`. Expanding a question was the only path that flipped
  // FOCUSABLE false -> true, so it alone produced a non-empty diff, hid the live
  // window, and wedged the HUD for the rest of the session (a response's
  // `setFocusable(false)` was a no-op against an already-false flag, which is
  // why replies to responses always worked).
  //
  // The same command owns ACTIVATION. Tauri's `setFocus` is dead on this window
  // for the same reason: tao gates it on the stale cached VISIBLE flag and
  // silently returns. That is why an expanded question card never received a
  // keystroke - option digits, Enter-to-submit, Escape-to-defer and arrow
  // navigation are all implemented and correct, they just never reached a
  // focused window. The native side takes foreground when editing begins and
  // hands it back to the previous window when it ends.
  const hudEditing = replyEditing || questionExpanded || managedCardFocused;

  useEffect(() => {
    if (!managedConsoleExpanded) setManagedCardFocused(false);
  }, [managedConsoleExpanded]);

  // Keyed on the boolean ALONE. A question walks through several phases while
  // it is open (surfaced -> recording -> processing -> draft_ready), and any of
  // those in the dependency list would tear down and reapply focusability on
  // every transition - bouncing the foreground away to the user's editor and
  // back on each one. The desired state only ever changes when `hudEditing`
  // does.
  // Native focus truth: whether the HUD is the real foreground window. A
  // WebView focus event alone is not proof. WebView2 focuses the document on
  // a click into a window that never became foreground, and the keys then go
  // to the other app, so every focus signal is confirmed natively.
  const refreshHudWindowFocus = useCallback(() => {
    if (!isTauri()) {
      setHudWindowFocused(document.hasFocus());
      return;
    }
    void invoke<boolean>("hud_window_foreground_state")
      .then((foreground) => setHudWindowFocused(foreground === true))
      .catch(() => setHudWindowFocused(false));
  }, []);

  useEffect(() => {
    if (!isTauri()) return;
    const setNativeFocusable = (focusable: boolean) =>
      invoke("set_hud_window_focusable", { focusable });
    void setNativeFocusable(hudEditing)
      .then(() => refreshHudWindowFocus())
      .catch(() => {});
    return () => {
      if (hudEditing) void setNativeFocusable(false).catch(() => {});
    };
  }, [hudEditing, refreshHudWindowFocus]);

  // Which control inside the window gets the caret. Separate from the effect
  // above because this legitimately does need to re-run as the reply phase
  // moves, and it must not drag native focusability along with it. A field
  // focused here gets its caret after the text, never before it.
  useLayoutEffect(() => {
    if (!hudEditing) return;
    focusFieldAtEnd(replyEditing ? editingInputRef.current : questionInputRef.current);
  }, [hudEditing, replyEditing, replyTarget, activeQuestion?.phase]);

  // A click into any HUD text field makes the HUD the real foreground window,
  // every time, including a repeat click on a field that is already editing
  // (its editing state does not change, so the effect above would not run).
  // Activation runs when the press is released: activating moves native focus
  // off the WebView and back, which mid-press loses the release and with it
  // the click that starts editing. That move can also reset the caret, so the
  // field gets back the caret the user last placed.
  const fieldCaretRef = useRef<FieldCarets>(new WeakMap());
  useEffect(() => {
    const remember = (event: Event) => rememberFieldCaret(event, fieldCaretRef.current);
    const onPointerUp = (event: PointerEvent) => {
      if (!isTauri() || event.button !== 0) return;
      const hit =
        event.target instanceof Element
          ? event.target.closest("textarea, input, [data-hud-text-field]")
          : null;
      if (hit === null) return;
      if (
        (hit instanceof HTMLTextAreaElement || hit instanceof HTMLInputElement) &&
        (hit.readOnly || hit.disabled)
      ) {
        return;
      }
      void invoke("set_hud_window_focusable", { focusable: true })
        .then(() => {
          refreshHudWindowFocus();
          restoreFieldCaret(
            hit instanceof HTMLTextAreaElement || hit instanceof HTMLInputElement
              ? hit
              : document.activeElement,
            fieldCaretRef.current,
          );
        })
        .catch(() => {});
    };
    const events = ["input", "keyup", "mouseup", "select"] as const;
    for (const name of events) document.addEventListener(name, remember, true);
    document.addEventListener("pointerup", onPointerUp, true);
    return () => {
      for (const name of events) document.removeEventListener(name, remember, true);
      document.removeEventListener("pointerup", onPointerUp, true);
    };
  }, [refreshHudWindowFocus]);

  useEffect(() => {
    // Outside Tauri (browser development and tests) the WebView is the window.
    const onFocus = () => (isTauri() ? refreshHudWindowFocus() : setHudWindowFocused(true));
    const onBlur = () => setHudWindowFocused(false);
    window.addEventListener("focus", onFocus);
    window.addEventListener("blur", onBlur);
    refreshHudWindowFocus();
    let unlisten: (() => void) | undefined;
    let disposed = false;
    const hudWindow = isTauri() ? getCurrentWindow() : null;
    if (typeof hudWindow?.onFocusChanged === "function") {
      void hudWindow
        .onFocusChanged(({ payload }) => {
          if (payload) refreshHudWindowFocus();
          else setHudWindowFocused(false);
        })
        .then((fn) => {
          if (disposed) fn();
          else unlisten = fn;
        })
        .catch(() => {});
    }
    return () => {
      disposed = true;
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("blur", onBlur);
      unlisten?.();
    };
  }, [refreshHudWindowFocus]);

  // --- Presented acknowledgement ---
  // What the HUD actually painted: the reply target of the surface on screen,
  // painted only once native geometry for it is applied, and focused only
  // with real window focus. The backend routes push-to-talk, Enter and Escape
  // from this alone. Anything else on screen (dictation, a transient, a
  // permission pill, hidden) reports nothing.
  const presentedSurface: ReplySurface | null =
    presentation.mode === "question" || presentation.mode === "question_pill"
      ? "question"
      : presentation.mode === "panel" && displayedCompletion !== null
        ? managedConsoleExpanded
          ? "console"
          : "card"
        : null;
  // An ended session's console only offers Copy: it routes nothing. A fading
  // console is already dismissed: it takes no keys or dictation.
  const presentedTarget =
    presentedSurface === "question"
      ? questionTarget
      : presentedSurface !== null &&
          !consoleSessionEnded &&
          !(presentedSurface === "console" && consoleFading)
        ? replyTarget
        : null;
  const presentedPainted =
    presentedTarget !== null &&
    appliedGeometryMode !== null &&
    appliedGeometryMode === presentationGeometryMode;
  const presentedFocused = presentedPainted && hudWindowFocused;
  const presentedSeqRef = useRef(0);
  useEffect(() => {
    if (!connected || forcedHudState !== null) return;
    presentedSeqRef.current += 1;
    sendAgentHudPresented(send, {
      seq: presentedSeqRef.current,
      target_id: presentedTarget,
      surface: presentedTarget === null ? null : presentedSurface,
      painted: presentedPainted,
      focused: presentedFocused,
    });
  }, [
    connected,
    connectionGeneration,
    forcedHudState,
    send,
    presentedTarget,
    presentedSurface,
    presentedPainted,
    presentedFocused,
  ]);

  // --- Typing and submit ---
  const nextReplyCommand = useCallback((target: string) => {
    const commands = replyCommandRef.current;
    const id = commands.next;
    commands.next += 1;
    commands.targets.set(id, target);
    return id;
  }, []);

  const editReply = useCallback(
    (target: string, text: string) => {
      const draft = replyStateRef.current.drafts[target];
      const step = editBuffer(replyBuffersRef.current, target, draft, text);
      setReplyBuffers(step.buffers);
      if (forcedHudState !== null) return;
      sendAgentDraftUpdate(send, {
        command_id: nextReplyCommand(target),
        target_id: target,
        base_revision: step.baseRevision,
        text,
      });
    },
    [forcedHudState, nextReplyCommand, send, setReplyBuffers],
  );

  const submitReply = useCallback(
    (target: string) => {
      const draft = replyStateRef.current.drafts[target];
      const buffers = replyBuffersRef.current;
      const text = bufferedText(buffers, target, draft);
      if (text.trim().length === 0 || forcedHudState !== null) return;
      // Already on its way: the backend refuses a second submit anyway.
      if (draft?.phase === "sending") return;
      const card = liveCardRef.current;
      if (card !== null && card.target === target && !managedConsoleExpandedRef.current) {
        pendingSubmitTargetsRef.current.set(target, Date.now());
        setRetainedCard((current) =>
          current !== null &&
          current.target === card.target &&
          current.generation === card.generation &&
          current.completion.completion_id === card.completion.completion_id
            ? current
            : card,
        );
      }
      sendAgentDraftSubmit(send, {
        command_id: nextReplyCommand(target),
        target_id: target,
        revision: bufferedRevision(buffers, target, draft),
        text,
      });
    },
    [forcedHudState, nextReplyCommand, send],
  );

  const copyReply = useCallback(() => {
    if (replyText.length === 0) return;
    const fallback = () => {
      const area = document.createElement("textarea");
      area.value = replyText;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy");
      } finally {
        area.remove();
      }
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(replyText).catch(fallback);
    } else {
      fallback();
    }
  }, [replyText]);

  const replyReadOnly = replyCapturing || replySending || consoleSessionEnded;
  // The card typing was opened on. Typing ends with that card (skipped,
  // expired or replaced); kept text still returns on the conversation's next
  // card through the server draft, but an empty field does not.
  const activeCompletionId = completionQueue.active?.completion_id ?? null;
  const editingCompletionRef = useRef<string | null>(null);
  const onDraftEdit = useCallback(() => {
    if (replyTarget === null || replyReadOnly) return;
    editingCompletionRef.current = activeCompletionId;
    setEditingTarget(replyTarget);
  }, [activeCompletionId, replyReadOnly, replyTarget]);
  useEffect(() => {
    if (editingTarget === null || managedConsoleExpanded) return;
    if (activeCompletionId !== editingCompletionRef.current) setEditingTarget(null);
  }, [activeCompletionId, editingTarget, managedConsoleExpanded]);

  const onEditingDraftChange = useCallback(
    (text: string) => {
      if (replyTarget === null || replyReadOnly) return;
      editReply(replyTarget, text);
    },
    [editReply, replyReadOnly, replyTarget],
  );

  const onEditingDraftKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      // The HUD owns Enter while it has focus: plain Enter submits exactly the
      // text shown; Shift+Enter and IME composition are the text box's own.
      if (
        event.key !== "Enter" ||
        event.nativeEvent.isComposing ||
        event.shiftKey ||
        event.ctrlKey ||
        event.altKey ||
        event.metaKey ||
        replyTarget === null
      ) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (!replyReadOnly) submitReply(replyTarget);
    },
    [replyReadOnly, replyTarget, submitReply],
  );

  // Leave local edit mode once the draft is gone (sent, discarded) or the
  // surface moves to another target; forget settled buffers.
  useEffect(() => {
    if (editingTarget !== null && editingTarget !== replyTarget) {
      setEditingTarget(null);
    }
  }, [editingTarget, replyTarget]);
  // The backend keeps recent receipts in every snapshot, so a conversation
  // that already had a reply sent still carries that old receipt. Only a
  // receipt that arrived after editing began means this draft was sent.
  const editingBaselineReceiptRef = useRef<string | null>(null);
  useLayoutEffect(() => {
    editingBaselineReceiptRef.current =
      editingTarget === null
        ? null
        : (replyStateRef.current.receipts.find((receipt) => receipt.target_id === editingTarget)
            ?.draft_id ?? null);
  }, [editingTarget]);
  useEffect(() => {
    const next = pruneBuffers(replyBuffersRef.current, replyState.drafts, editingTarget);
    if (next !== replyBuffersRef.current) setReplyBuffers(next);
    const receipt =
      editingTarget === null
        ? undefined
        : replyState.receipts.find((candidate) => candidate.target_id === editingTarget);
    if (
      editingTarget !== null &&
      replyState.drafts[editingTarget] === undefined &&
      receipt !== undefined &&
      receipt.draft_id !== editingBaselineReceiptRef.current &&
      (replyBuffersRef.current[editingTarget]?.inflight ?? 0) === 0
    ) {
      setEditingTarget(null);
    }
  }, [replyState, editingTarget, setReplyBuffers]);

  // Session shortcuts while the HUD has keyboard focus: Ctrl+1-9 and Ctrl+Tab
  // on Windows, Cmd+1-9 on Mac. From the card they open the console on that
  // session; in the console they switch tabs.
  useEffect(() => {
    if (!hudWindowFocused || presentedSessionsState.sessions.length === 0) return;
    const onKeyDown = (event: KeyboardEvent) => {
      // Never move away from a reply while it is recording or processing.
      if (event.isComposing || replyCapturing) return;
      const index = sessionShortcutIndex(event, presentedSessionsState, {
        mac: navigator.platform.toLowerCase().includes("mac"),
      });
      if (index === null) return;
      const session = presentedSessionsState.sessions[index];
      if (session === undefined) return;
      event.preventDefault();
      openConsoleOn(session.session_handle);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [hudWindowFocused, presentedSessionsState, openConsoleOn, replyCapturing]);

  // A dictated draft while the HUD itself has focus. The backend's global
  // Enter normally takes it before the WebView sees the key; this covers an
  // Enter the global hook did not claim (an IME layout in the HUD).
  useEffect(() => {
    // Only for the painted reply itself; a question on top owns its own keys.
    if (
      !hudWindowFocused ||
      replyTarget === null ||
      replyPhase !== "draft_ready" ||
      presentedSurface === "question" ||
      presentedTarget !== replyTarget
    ) {
      return;
    }
    const target = replyTarget;
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.key !== "Enter" ||
        event.isComposing ||
        event.shiftKey ||
        event.ctrlKey ||
        event.altKey ||
        event.metaKey
      ) {
        return;
      }
      const element = event.target as HTMLElement | null;
      if (element?.tagName === "TEXTAREA" || element?.tagName === "INPUT") return;
      event.preventDefault();
      event.stopPropagation();
      submitReply(target);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [hudWindowFocused, replyPhase, replyTarget, presentedSurface, presentedTarget, submitReply]);

  const submitQuestionAnswers = useCallback(() => {
    if (activeQuestion === null || !questionCanAnswer(activeQuestion)) return;
    const answers = exactQuestionAnswers(
      activeQuestion.questions,
      questionSelections,
      presentedFreeform,
    );
    if (answers === null) {
      if (questionIndex + 1 < activeQuestion.questions.length) {
        setQuestionIndex((current) => current + 1);
      } else {
        questionInputRef.current?.focus();
      }
      return;
    }
    // Held for the sent confirmation below. Captured here because the wire
    // snapshot that reports `answered` carries no record of what was sent.
    submittedAnswerTextRef.current = Object.values(answers).join(", ");
    if (forcedHudState === null) {
      sendAgentQuestionOpportunityAnswers(
        send,
        activeQuestion.session_handle,
        activeQuestion.generation,
        answers,
      );
    }
  }, [
    activeQuestion,
    forcedHudState,
    presentedFreeform,
    questionIndex,
    questionSelections,
    send,
  ]);

  const chooseQuestionOption = useCallback(
    (question: AgentQuestion, label: string) => {
      if (activeQuestion === null) return;
      const previous = questionSelections[question.question] ?? [];
      const next = toggleQuestionOption(question, previous, label);
      const nextSelections = {
        ...questionSelections,
        [question.question]: next,
      };
      setQuestionSelections(nextSelections);
      setQuestionFreeform((current) => ({
        ...current,
        [question.question]: "",
      }));
      // One answer value: choosing an option clears the typed or spoken one.
      if (
        question.question === currentQuestionKey &&
        questionTarget !== null &&
        questionDraftText.length > 0
      ) {
        editReply(questionTarget, "");
      }
      if (!question.multiSelect) {
        const answers = exactQuestionAnswers(
          activeQuestion.questions,
          nextSelections,
          questionFreeform,
        );
        if (answers !== null) {
          if (forcedHudState === null) {
            sendAgentQuestionOpportunityAnswers(
              send,
              activeQuestion.session_handle,
              activeQuestion.generation,
              answers,
            );
          }
        } else if (questionIndex + 1 < activeQuestion.questions.length) {
          setQuestionIndex((current) => current + 1);
        }
      }
    },
    [
      activeQuestion,
      forcedHudState,
      questionFreeform,
      questionIndex,
      questionSelections,
      send,
    ],
  );

  const deferQuestion = useCallback(() => {
    if (forcedHudState !== null || activeQuestion === null) return;
    sendAgentQuestionOpportunityDefer(
      send,
      activeQuestion.session_handle,
      activeQuestion.generation,
    );
    setExpandedQuestionGeneration(null);
  }, [activeQuestion, forcedHudState, send]);

  // Permission decisions (design.md state 10). Distinct from the question
  // defer above: a permission interaction is identified by its opaque
  // request_id, not a session_handle/generation pair, so it takes the
  // generic agent.interaction.defer / agent.permission.* wire path (backend
  // `_handle_claude_interaction_ws_message`, server.py) rather than the
  // question coordinator's session-scoped defer. `claude_permission_decisions_enabled`
  // stays default-off server-side; sending while off is a harmless no-op.
  const allowPermission = useCallback(() => {
    if (
      forcedHudState !== null ||
      claudeAttention === null ||
      claudeAttention.state !== "permission"
    ) {
      return;
    }
    send({
      type: "agent.permission.allow",
      version: 1,
      request_id: claudeAttention.requestId,
    });
  }, [claudeAttention, forcedHudState, send]);

  const denyPermission = useCallback(() => {
    if (
      forcedHudState !== null ||
      claudeAttention === null ||
      claudeAttention.state !== "permission"
    ) {
      return;
    }
    send({
      type: "agent.permission.deny",
      version: 1,
      request_id: claudeAttention.requestId,
    });
  }, [claudeAttention, forcedHudState, send]);

  const deferPermission = useCallback(() => {
    if (
      forcedHudState !== null ||
      claudeAttention === null ||
      claudeAttention.state !== "permission"
    ) {
      return;
    }
    send({
      type: "agent.interaction.defer",
      version: 1,
      request_id: claudeAttention.requestId,
    });
  }, [claudeAttention, forcedHudState, send]);

  useEffect(() => {
    if (activeQuestion === null || !questionExpanded) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        deferQuestion();
        return;
      }
      const target = event.target as HTMLElement | null;
      const typing =
        target?.tagName === "TEXTAREA" ||
        target?.tagName === "INPUT" ||
        target?.isContentEditable;
      if (typing) return;
      const question =
        activeQuestion.questions[questionIndex] ??
        activeQuestion.questions[0];
      const digit = Number(event.key);
      if (
        Number.isInteger(digit) &&
        digit >= 1 &&
        digit <= question.options.length
      ) {
        event.preventDefault();
        chooseQuestionOption(question, question.options[digit - 1].label);
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const options = Array.from(
          document.querySelectorAll<HTMLElement>(
            "[data-question-option], [data-question-freeform]",
          ),
        );
        if (options.length === 0) return;
        const current = options.indexOf(document.activeElement as HTMLElement);
        const delta = event.key === "ArrowDown" ? 1 : -1;
        options[(current + delta + options.length) % options.length]?.focus();
        return;
      }
      if (event.key === " " && document.activeElement instanceof HTMLButtonElement) {
        event.preventDefault();
        document.activeElement.click();
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        submitQuestionAnswers();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    activeQuestion,
    chooseQuestionOption,
    deferQuestion,
    questionExpanded,
    questionIndex,
    submitQuestionAnswers,
  ]);

  useEffect(() => {
    document.documentElement.style.setProperty(
      "background",
      "transparent",
      "important",
    );
    document.body.style.setProperty("background", "transparent", "important");
  }, []);

  useEffect(() => {
    if (transient === null) {
      transientTimerRef.current = null;
      return;
    }
    const duration =
      transient.state === "enhance_timeout" ||
      transient.state === "enhance_error"
        ? ENHANCE_TIMEOUT_MS
        : BLOCKED_MS;
    if (transientTimerRef.current?.generation !== transient.generation) {
      transientTimerRef.current = {
        generation: transient.generation,
        remainingMs: duration,
        startedAtMs: null,
      };
    }
    if (!transientForeground) return;

    const timer = transientTimerRef.current;
    const startedAtMs = performance.now();
    timer.startedAtMs = startedAtMs;
    const timeout = window.setTimeout(() => {
      timer.remainingMs = 0;
      timer.startedAtMs = null;
      setTransient((current) =>
        current?.generation === transient.generation ? null : current,
      );
    }, timer.remainingMs);
    return () => {
      window.clearTimeout(timeout);
      if (
        transientTimerRef.current === timer &&
        timer.startedAtMs === startedAtMs
      ) {
        timer.remainingMs = Math.max(
          0,
          timer.remainingMs - (performance.now() - startedAtMs),
        );
        timer.startedAtMs = null;
      }
    };
  }, [transient, transientForeground]);

  useLayoutEffect(() => {
    hudSessionWasActiveRef.current = hudOriginSessionVisual;
  }, [hudOriginSessionVisual]);

  useEffect(() => {
    if (presentation.mode === "control" || hudOriginSessionVisual) return;
    if (recordingControlCollapseRef.current !== null) {
      window.clearTimeout(recordingControlCollapseRef.current);
      recordingControlCollapseRef.current = null;
    }
    setRecordingControlExpanded(false);
  }, [presentation.mode, hudOriginSessionVisual]);

  // A HUD-origin session returns through the already-expanded launcher, then
  // reuses the ordinary card-first collapse. This gives cancel and finish one
  // continuous spring instead of replacing the active pill with a tiny seed.
  useEffect(() => {
    if (!returningToRestingControl) return;
    hudOutcomeCollapseRef.current = true;
    setRecordingControlExpanded(false);
  }, [returningToRestingControl]);

  useEffect(() => {
    setDictationCommandPending((pending) => {
      if (dictation.origin !== "hud") return pending;
      if (
        pending === "start" &&
        dictation.recordingId !== null &&
        dictation.recordingId > 0
      ) {
        return null;
      }
      if (pending === "complete" && dictation.phase !== "recording") {
        return null;
      }
      if (pending === "cancel" && dictation.phase === "idle") return null;
      return pending;
    });
  }, [dictation.origin, dictation.phase, dictation.recordingId]);

  useEffect(() => {
    if (!connected) setDictationCommandPending(null);
  }, [connected]);

  useEffect(() => {
    if (dictationCommandPending === null) return;
    const timeout = window.setTimeout(() => {
      setDictationCommandPending(null);
    }, HUD_COMMAND_PENDING_TIMEOUT_MS);
    return () => window.clearTimeout(timeout);
  }, [dictationCommandPending]);

  useEffect(() => {
    if (
      !hudFinishVisualHold ||
      dictation.origin !== "hud" ||
      dictation.phase !== "processing"
    ) {
      return;
    }
    if (hudFinishVisualHoldRef.current !== null) {
      window.clearTimeout(hudFinishVisualHoldRef.current);
    }
    hudFinishVisualHoldRef.current = window.setTimeout(() => {
      hudFinishVisualHoldRef.current = null;
      setHudFinishVisualHold(false);
    }, HUD_FINISH_PROCESSING_MIN_MS);
  }, [dictation.origin, dictation.phase, hudFinishVisualHold]);

  useEffect(() => {
    if (dictation.authoritative && dictation.phase === "idle") {
      setIdleScreenReaderDetected(dictation.screenReaderDetected);
    }
  }, [
    dictation.authoritative,
    dictation.phase,
    dictation.recordingId,
    dictation.screenReaderDetected,
  ]);
  useHudNativeReconciler({
    presentation,
    managedConsoleDismissedToHidden,
    presentationGeometryMode,
    cleanGeometryPresentation,
    permanentControlPresentation,
    parkedRecordingControl,
    previewExpanded,
    previewNaturalTextHeight,
    measuredCardSize: geometryMeasuredCard,
    responseExpanded,
    consoleSizeRequest: consoleResize.request,
    dedicatedRecordingControlWindow,
    dedicatedArmingParkingEnabled,
    setExpandedPreviewCardSize,
    setAppliedGeometryMode,
  });

  // Live theme sync. The HUD is a persistent window created once at startup, so
  // the index.html inline script only sets data-theme at load. When the user
  // switches theme in the main window it emits "theme-changed"; apply it here so
  // the HUD follows. (Popups reload per-open, so they pick it up on their own.)
  useEffect(() => {
    let disposed = false;
    const apply = (theme: unknown) => {
      if (disposed) return;
      if (theme === "light" || theme === "dark") {
        document.documentElement.setAttribute("data-theme", theme);
        setHudTheme(theme);
      }
    };
    // The exact-state renderer applies its requested theme before mounting.
    // Do not replace it with the user's persisted app theme.
    if (uiQaFixtureId !== undefined) {
      apply(document.documentElement.getAttribute("data-theme"));
      return;
    }
    // Re-sync from localStorage on mount, in case it changed before this ran.
    try {
      apply(localStorage.getItem("ws-theme"));
    } catch {
      /* localStorage unavailable */
    }
    let unlisten: (() => void) | undefined;
    listen<string>("theme-changed", (e) => apply(e.payload)).then(
      (fn) => {
        if (disposed) fn();
        else unlisten = fn;
      },
      () => {
        /* not running under Tauri (browser dev) */
      },
    );
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [uiQaFixtureId]);

  useEffect(() => {
    let disposed = false;
    const apply = (visible: unknown) => {
      if (disposed) return;
      if (typeof visible !== "boolean") return;
      // Reset every expansion layer in the same event that removes the
      // control. Waiting for the ordinary spring-collapse timers leaves an
      // expanded launcher behind the hidden window, so a quick re-enable shows
      // that larger vessel for one frame before it collapses to the seed.
      if (!visible) resetPersistentControlBeforeHide();
      setRecordingControlVisible(visible);
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key !== RECORDING_CONTROL_VISIBILITY_STORAGE_KEY) return;
      apply(event.newValue === "true");
    };
    window.addEventListener("storage", onStorage);
    try {
      apply(
        localStorage.getItem(RECORDING_CONTROL_VISIBILITY_STORAGE_KEY) ===
          "true",
      );
    } catch {
      /* localStorage unavailable */
    }

    let unlisten: (() => void) | undefined;
    listen<boolean>(RECORDING_CONTROL_VISIBILITY_EVENT, (event) => {
      apply(event.payload);
    }).then(
      (fn) => {
        if (disposed) fn();
        else unlisten = fn;
      },
      () => {
        /* not running under Tauri (browser dev) */
      },
    );

    return () => {
      disposed = true;
      window.removeEventListener("storage", onStorage);
      unlisten?.();
    };
  }, [resetPersistentControlBeforeHide]);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    listen<boolean>(RECORDING_CONTROL_START_PENDING_EVENT, (event) => {
      if (disposed || typeof event.payload !== "boolean") return;
      if (!event.payload) {
        setDictationCommandPending((current) =>
          current === "start" ? null : current,
        );
        return;
      }
      // This event is valid only for the effective dedicated-control contract.
      // A disabled preference or detected screen reader must fail closed, and
      // an unrelated cancel/complete command retains ownership of its pending
      // state.
      if (
        !dedicatedRecordingControlWindow ||
        !effectivePersistentControlRef.current
      ) {
        setDictationCommandPending((current) =>
          current === "start" ? null : current,
        );
        return;
      }
      setDictationCommandPending((current) =>
        current === null || current === "start" ? "start" : current,
      );
    }).then(
      (dispose) => {
        if (disposed) {
          dispose();
        } else {
          unlisten = dispose;
        }
      },
      () => {
        /* browser development */
      },
    );
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [dedicatedRecordingControlWindow]);

  useEffect(() => {
    if (recordingControlVisible && idleScreenReaderDetected === false) return;
    setDictationCommandPending((current) =>
      current === "start" ? null : current,
    );
  }, [idleScreenReaderDetected, recordingControlVisible]);

  const applyIdleScreenReaderStatus = useCallback((active: unknown) => {
    if (typeof active !== "boolean") return;
    setIdleScreenReaderDetected(active);
    try {
      localStorage.setItem(
        SCREEN_READER_STATUS_STORAGE_KEY,
        String(active),
      );
    } catch {
      /* localStorage unavailable */
    }
  }, []);

  // Backend-owned status: one fetch, then WS pushes, refetched on reconnect.
  // Only the persistent recording control needs it; hidden means no requests.
  useScreenReaderStatusSync({
    enabled: recordingControlVisible,
    connected,
    subscribe,
    apply: applyIdleScreenReaderStatus,
  });

  useEffect(() => {
    let disposed = false;
    const apply = (active: unknown) => {
      if (!disposed) applyIdleScreenReaderStatus(active);
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key !== SCREEN_READER_STATUS_STORAGE_KEY) return;
      apply(event.newValue === "true");
    };
    window.addEventListener("storage", onStorage);
    apply(readScreenReaderStatus());

    let unlisten: (() => void) | undefined;
    listen<boolean>(SCREEN_READER_STATUS_EVENT, (event) => {
      apply(event.payload);
    }).then(
      (fn) => {
        if (disposed) {
          fn();
          return;
        }
        unlisten = fn;
      },
      () => {
        /* not running under Tauri (browser dev) */
      },
    );

    return () => {
      disposed = true;
      window.removeEventListener("storage", onStorage);
      unlisten?.();
    };
  }, [applyIdleScreenReaderStatus]);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    listen("agent-hud-view", () => { if (!disposed) toggleHudExpansion(); }).then(
      (fn) => {
        if (disposed) fn();
        else unlisten = fn;
      },
      () => {
        /* browser development */
      },
    );
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [toggleHudExpansion]);

  useEffect(() => {
    return subscribe((event) => {
      if (event.type === "audio.levels") {
        levelsRef.current.rms = event.rms;
        levelsRef.current.peak = event.peak;
      }

      // `active_profile_override` is deliberately NOT consumed here. It is the
      // Settings status line's signal for a manual hotkey switch only; the HUD
      // reads the recording-correlated effective profile instead, which covers
      // manual, site, app, and default routing alike.
      if (event.type === "enhancement.profile") {
        dispatchDictation({ type: "enhancement.profile", event });
      }

      if (event.type === "agent.conversation.snapshot") setConversations(event.sessions);

      if (event.type === "agent.response_queue.snapshot") {
        dispatchCompletion({
          type: "response_queue.snapshot",
          event,
          connection: connectionGeneration?.current ?? 0,
        });
      }

      if (event.type === "agent.question.snapshot") {
        dispatchQuestion({ type: "snapshot", event, connection: connectionGeneration?.current ?? 0 });
      }

      if (event.type === "agent.sessions") {
        dispatchSessions({ type: "agent.sessions", event });
      }

      if (event.type === "agent.reply.snapshot") {
        const previousEpoch = replyStateRef.current.epoch;
        if (previousEpoch !== null && event.epoch !== previousEpoch) {
          // A new backend owner: no buffer or command refers to it.
          setReplyBuffers({});
          replyCommandRef.current.targets.clear();
        }
        dispatchReply({ type: "snapshot", event });
        setReplyHintRetired(event.reply_hint_retired === true);
      }

      if (event.type === "agent.reply.result") {
        const commands = replyCommandRef.current;
        const target =
          event.command_id !== undefined ? commands.targets.get(event.command_id) : undefined;
        if (event.command_id !== undefined) commands.targets.delete(event.command_id);
        if (target !== undefined && event.command === "agent.draft.update") {
          setReplyBuffers(settleBuffer(replyBuffersRef.current, target, event.result));
        }
        if (
          target !== undefined &&
          event.command === "agent.draft.submit" &&
          event.result !== "applied"
        ) {
          // Not sent: the draft's refusal outcome holds the card, or nothing does.
          pendingSubmitTargetsRef.current.delete(target);
        }
      }

      if (event.type === "agent.reply.close") {
        if (event.surface === "console") {
          // Escape dismissed this console; the backend discarded its draft.
          const identity = expandedResponseIdentityRef.current;
          if (
            consoleTargetRef.current === event.target_id &&
            identity !== null &&
            identity.startsWith("managed:")
          ) {
            fadeOutConsole(identity);
          }
        } else {
          // Escape skipped this card; its draft stays for the next card.
          setRetainedCard((current) => (current?.target === event.target_id ? null : current));
        }
      }

      if (event.type === "agent-hud-view") {
        // Logged on EVERY press. `[WS] first message_type=` only reports a type
        // once per connection, so repeated view-shortcut presses left no trace
        // in an exported bundle - which is exactly what "I kept pressing it and
        // nothing happened" needed to be diagnosable.
        console.log("[HUD] view shortcut received");
        toggleHudExpansion();
      }

      if (event.type === "agent.interactions.snapshot") {
        const identity = connectionGeneration?.current ?? 0;
        if (interactionRevisionRef.current.connection !== identity) {
          interactionRevisionRef.current = { connection: identity, revision: -1 };
        }
        if (event.revision > interactionRevisionRef.current.revision) {
          interactionRevisionRef.current.revision = event.revision;
          const next: Record<string, AgentInteraction> = {};
          for (const row of event.interactions) {
            if (row.interaction.status === "pending") next[row.interaction.request_id] = row.interaction;
          }
          setHudInteractions(next);
        }
      }

      // Iteration 03: Claude activity is deliberately not presented. The event
      // is still emitted for backend lifecycle reasons; the HUD ignores it.
      if (event.type === "agent.hud.activity") return;

      if (event.type === "state.recording") {
        if (event.recording) {
          if (startupArmingDelayRef.current !== null) {
            window.clearTimeout(startupArmingDelayRef.current);
            startupArmingDelayRef.current = null;
          }
          setStartupArmingVisible(false);
        }
        dispatchDictation({
          type: "legacy.recording",
          recording: event.recording,
          screenReaderDetected: event.screen_reader_detected === true,
        });
      }

      if (event.type === "state.arming") {
        if (startupArmingDelayRef.current !== null) {
          window.clearTimeout(startupArmingDelayRef.current);
          startupArmingDelayRef.current = null;
        }
        setStartupArmingVisible(false);
        // The start carries the backend's current reading. With the
        // persistent control off nothing else fetches it, and the delayed
        // arming indicator is gated on it too.
        if (typeof event.screen_reader_detected === "boolean") {
          setIdleScreenReaderDetected(event.screen_reader_detected);
        }
        if (
          event.active &&
          event.agent_compose !== true &&
          event.hud_enabled !== false &&
          event.screen_reader_detected !== true
        ) {
          startupArmingDelayRef.current = window.setTimeout(() => {
            startupArmingDelayRef.current = null;
            setStartupArmingVisible(true);
          }, MIC_ARMING_INDICATOR_DELAY_MS);
        }
      }

      if (event.type === "state.transcribing" && !event.active) {
        dispatchDictation({ type: "legacy.transcribing.done" });
      }

      if (event.type === "dictation.state") {
        if (event.phase === "recording") {
          if (startupArmingDelayRef.current !== null) {
            window.clearTimeout(startupArmingDelayRef.current);
            startupArmingDelayRef.current = null;
          }
          setStartupArmingVisible(false);
        }
        dispatchDictation({ type: "lifecycle", event });
      }

      if (event.type === "transcription.preview") {
        dispatchDictation({ type: "preview", event });
      }

      if (event.type === "agent.compose.state") {
        dispatchDictation({ type: "compose.state", event });
      }

      if (event.type === "agent.compose.preview") {
        dispatchDictation({ type: "compose.preview", event });
      }

      if (event.type === "recording.blocked") {
        setTransient({
          state: "blocked",
          generation: ++transientGenerationRef.current,
          reason: event.reason,
        });
      }

      if (event.type === "enhancement.timeout") {
        // `blocked` and `enhance_timeout` are mutually exclusive in practice
        // (a blocked recording never reaches enhancement), so last-write-wins
        // is fine.
        //
        // The backend deduplicates per `recording_id`, so chunked cleanup that
        // times out on several chunks still produces at most one event. The
        // frontend deliberately does not dedupe or filter by recording: it
        // paints exactly what the dictation pipeline reported.
        const provider = event.provider;
        const generation = ++transientGenerationRef.current;
        setTransient({ state: "enhance_timeout", generation, provider });
      }

      if (event.type === "enhancement.error") {
        const provider = event.provider;
        const generation = ++transientGenerationRef.current;
        setTransient({ state: "enhance_error", generation, provider });
      }


    });
  }, [send, subscribe, toggleHudExpansion, setReplyBuffers, fadeOutConsole]);

  // Agent-speech stop chokepoint from the HUD button (Task 3.2). Fire-and-forget
  // and best-effort (the HUD window is focusable:false); the card stop-speaking button
  // hides only on the resulting speech.state: idle WS event, never
  // optimistically, so this handler intentionally does not touch `speaking`.
  const runHudDictationCommand = useCallback(
    (command: HudDictationCommand) => {
      if (!connected || dictationCommandPending !== null) return;
      setDictationCommandPending(command);
      void postHudDictationCommand(command).catch(() => {
        setDictationCommandPending((current) =>
          current === command ? null : current,
        );
      });
    },
    [connected, dictationCommandPending],
  );

  const onStartDictation = useCallback(() => {
    if (hudFinishVisualHoldRef.current !== null) {
      window.clearTimeout(hudFinishVisualHoldRef.current);
      hudFinishVisualHoldRef.current = null;
    }
    setHudFinishVisualHold(false);
    runHudDictationCommand("start");
  }, [runHudDictationCommand]);

  const onCancelDictation = useCallback(() => {
    if (
      dictation.authoritative &&
      dictation.origin === "hud" &&
      dictation.phase === "recording"
    ) {
      if (hudFinishVisualHoldRef.current !== null) {
        window.clearTimeout(hudFinishVisualHoldRef.current);
        hudFinishVisualHoldRef.current = null;
      }
      setHudFinishVisualHold(false);
      runHudDictationCommand("cancel");
    }
  }, [
    dictation.authoritative,
    dictation.origin,
    dictation.phase,
    runHudDictationCommand,
  ]);

  const onFinishDictation = useCallback(() => {
    if (
      dictation.authoritative &&
      dictation.origin === "hud" &&
      dictation.phase === "recording"
    ) {
      setHudFinishVisualHold(true);
      if (hudFinishVisualHoldRef.current !== null) {
        window.clearTimeout(hudFinishVisualHoldRef.current);
      }
      // Fallback for a missed processing event. A real processing lifecycle
      // restarts this dwell from its own arrival below.
      hudFinishVisualHoldRef.current = window.setTimeout(() => {
        hudFinishVisualHoldRef.current = null;
        setHudFinishVisualHold(false);
      }, HUD_FINISH_PROCESSING_MIN_MS);
      runHudDictationCommand("complete");
    }
  }, [
    dictation.authoritative,
    dictation.origin,
    dictation.phase,
    runHudDictationCommand,
  ]);

  const onRecordingControlPointerEnter = useCallback(() => {
    hudOutcomeCollapseRef.current = false;
    if (recordingControlCollapseRef.current !== null) {
      window.clearTimeout(recordingControlCollapseRef.current);
      recordingControlCollapseRef.current = null;
    }
    setRecordingControlExpanded(true);
  }, []);

  const onRecordingControlPointerLeave = useCallback(() => {
    if (dragRef.current !== null) return;
    if (recordingControlCollapseRef.current !== null) {
      window.clearTimeout(recordingControlCollapseRef.current);
    }
    recordingControlCollapseRef.current = window.setTimeout(() => {
      recordingControlCollapseRef.current = null;
      setRecordingControlExpanded(false);
    }, RECORDING_CONTROL_COLLAPSE_MS);
  }, []);

  useEffect(() => {
    return () => {
      if (recordingControlCollapseRef.current !== null) {
        window.clearTimeout(recordingControlCollapseRef.current);
      }
      if (recordingControlCardSettleRef.current !== null) {
        window.clearTimeout(recordingControlCardSettleRef.current);
      }
      if (hudFinishVisualHoldRef.current !== null) {
        window.clearTimeout(hudFinishVisualHoldRef.current);
      }
      if (startupArmingDelayRef.current !== null) {
        window.clearTimeout(startupArmingDelayRef.current);
      }
    };
  }, [resetPersistentControlBeforeHide]);

  const setControlCard = useCallback((next: boolean) => {
    controlCardExpandedRef.current = next;
    setControlCardExpanded(next);
  }, []);

  // Expand leg: intent clears the compact native region first. The card is
  // held at seed size until the reconciler reports the launcher geometry as
  // applied; the WebView itself stays 132x52 throughout.
  useEffect(() => {
    if (!recordingControlExpanded) return;
    if (recordingControlCardSettleRef.current !== null) {
      window.clearTimeout(recordingControlCardSettleRef.current);
      recordingControlCardSettleRef.current = null;
    }
    setControlWindowExpanded(true);
  }, [recordingControlExpanded]);

  useEffect(() => {
    if (!recordingControlExpanded) return;
    if (!controlWindowExpanded) return;
    if (appliedGeometryMode !== "launcher") return;
    setControlCard(true);
  }, [
    recordingControlExpanded,
    controlWindowExpanded,
    appliedGeometryMode,
    setControlCard,
  ]);

  // Collapse leg: the card shrinks first, then a bounded settle window passes,
  // then the native region clips to an already-collapsed card.
  useEffect(() => {
    if (recordingControlExpanded) return;
    if (!controlWindowExpanded && !controlCardExpandedRef.current) return;
    setControlCard(false);
    if (recordingControlCardSettleRef.current !== null) {
      window.clearTimeout(recordingControlCardSettleRef.current);
    }
    recordingControlCardSettleRef.current = window.setTimeout(
      () => {
        recordingControlCardSettleRef.current = null;
        hudOutcomeCollapseRef.current = false;
        setControlWindowExpanded(false);
      },
      hudReducedMotion
        ? RECORDING_CONTROL_CARD_SETTLE_REDUCED_MS
        : hudOutcomeCollapseRef.current
          ? HUD_OUTCOME_COLLAPSE_SETTLE_MS
          : RECORDING_CONTROL_CARD_SETTLE_MS,
    );
  }, [
    recordingControlExpanded,
    controlWindowExpanded,
    hudReducedMotion,
    setControlCard,
  ]);

  const onPreviewExpandedChange = useCallback((expanded: boolean) => {
    setExpandedPreviewRecordingId(
      expanded ? dictation.recordingId : null,
    );
    if (!expanded) setPreviewNaturalTextHeight(0);
    if (expanded) {
      setExpandedPreviewCardSize({
        width: PREVIEW_EXPANDED_CARD_W,
        height: PREVIEW_CARD_H,
      });
    }
  }, [dictation.recordingId]);

  const onPreviewNaturalTextHeightChange = useCallback((height: number) => {
    setPreviewNaturalTextHeight((current) =>
      current === height ? current : height,
    );
  }, []);

  // The resting pill a press started on; clicked if the release did not move.
  const restingClickRef = useRef<HTMLElement | null>(null);
  const onPointerDown = useCallback(async (e: React.PointerEvent) => {
    const pointerTarget = e.target as HTMLElement | null;
    if (pointerTarget?.closest("[data-hud-interactive]")) return;
    restingClickRef.current =
      pointerTarget?.closest<HTMLElement>("[data-hud-resting-click]") ?? null;
    if (arming || hudOriginSessionVisual) return;
    if (presentation.mode === "preview" && previewExpanded) return;
    // The console drags by its identity row (its body and resize edges are
    // interactive). It keeps its own per-display offset, never the pill anchor.
    const consoleDrag = presentationGeometryMode === "response";

    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    const startScreenX = e.screenX;
    const startScreenY = e.screenY;

    const win = getCurrentWindow();
    const [pos, size, monitor, consoleDisplay] = await Promise.all([
      win.outerPosition(),
      win.outerSize(),
      currentMonitor(),
      consoleDrag ? currentHudDisplay().catch(() => null) : Promise.resolve(null),
    ]);

    const scale = monitor?.scaleFactor ?? 1;
    const hudW = size.width / scale;
    const monitorX = (monitor?.position.x ?? 0) / scale;
    const monitorY = (monitor?.position.y ?? 0) / scale;
    const screenW = (monitor?.size.width ?? 1920) / scale;
    const screenH = (monitor?.size.height ?? 1080) / scale;

    dragRef.current = {
      startScreenX,
      startScreenY,
      startWinX: pos.x / scale,
      startWinY: pos.y / scale,
      hudW,
      guide: null,
      dragging: false,
      scale,
      monitorX,
      monitorY,
      screenW,
      screenH,
      mode: presentationGeometryMode,
      measuredCard: geometryMeasuredCardRef.current,
      hudH: size.height / scale,
      consoleDisplay,
      lastScreenX: startScreenX,
      lastScreenY: startScreenY,
      totalMovement: 0,
    };
  }, [
    arming,
    hudOriginSessionVisual,
    presentation.mode,
    presentationGeometryMode,
    previewExpanded,
  ]);

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    d.totalMovement += Math.hypot(
      e.screenX - d.lastScreenX,
      e.screenY - d.lastScreenY,
    );
    d.lastScreenX = e.screenX;
    d.lastScreenY = e.screenY;
    if (d.totalMovement < 5) return;
    if (!d.dragging) {
      d.dragging = true;
      void (async () => {
        try {
          const guide = await Window.getByLabel("snap-guide");
          if (!guide || dragRef.current !== d || !d.dragging) return;
          await guide.setSize(new LogicalSize(1, d.screenH));
          if (dragRef.current !== d || !d.dragging) return;
          await guide.setPosition(
            new LogicalPosition(
              d.monitorX + (d.screenW - 1) / 2,
              d.monitorY,
            ),
          );
          if (dragRef.current !== d || !d.dragging) return;
          await guide.setIgnoreCursorEvents(true);
          if (dragRef.current !== d || !d.dragging) return;
          d.guide = guide;
          if (!(await applyWindowLifecycle("snap", "show"))) {
            await guide.show();
          }
        } catch {
          /* snap-guide unavailable */
        }
      })();
    }
    let newX = d.startWinX + (e.screenX - d.startScreenX);
    const newY = d.startWinY + (e.screenY - d.startScreenY);

    if (d.mode === "response") {
      // The console snaps to the work area's center on both axes and stays on
      // screen. Saving the offset as it moves means a reconcile mid-drag
      // resolves to where the drag already is.
      if (d.consoleDisplay === null) return;
      const placed = draggedConsolePlacement(
        { x: newX, y: newY },
        { width: d.hudW, height: d.hudH },
        d.consoleDisplay.workArea,
      );
      saveConsoleOffset(d.consoleDisplay.displayKey, placed.offset);
      getCurrentWindow()
        .setPosition(new LogicalPosition(placed.x, placed.y))
        .catch(() => {});
      return;
    }

    // Snap to monitor center while dragging
    const screenCenterX = d.monitorX + d.screenW / 2;
    const hudCenterX = newX + d.hudW / 2;
    if (Math.abs(hudCenterX - screenCenterX) <= SNAP_THRESHOLD) {
      newX = screenCenterX - d.hudW / 2;
    }

    getCurrentWindow()
      .setPosition(new LogicalPosition(newX, newY))
      .catch(() => {});
  }, []);

  const onPointerUp = useCallback(async (e: React.PointerEvent) => {
    const restingClick = restingClickRef.current;
    restingClickRef.current = null;
    const d = dragRef.current;
    if (!d) {
      // Released before the drag finished starting: a click.
      restingClick?.click();
      return;
    }
    dragRef.current = null;
    d.totalMovement += Math.hypot(
      e.screenX - d.lastScreenX,
      e.screenY - d.lastScreenY,
    );
    if (d.totalMovement < 5) restingClick?.click();

    // Hide snap guide
    try {
      if (d.guide && !(await applyWindowLifecycle("snap", "hide"))) {
        await d.guide.hide();
      }
    } catch { /* ignore */ }

    // The console remembered its own offset while it moved.
    if (d.mode === "response") return;

    // Convert every presentation back to one canonical compact-pill anchor.
    try {
      const win = getCurrentWindow();
      const pos = await win.outerPosition();
      const anchor = pillAnchorFromWindowPosition(
        d.mode,
        { x: pos.x / d.scale, y: pos.y / d.scale },
        d.measuredCard,
      );
      localStorage.setItem(
        "hud-position",
        JSON.stringify(anchor),
      );
    } catch { /* ignore */ }
  }, []);

  const dictationCommandsDisabled =
    dictationCommandPending !== null || !connected;

  const pillState = presentation.mode === "pill" ? presentation.state : "idle";

  // The effective-profile stamp rides the ACTIVE dictation only, and only for
  // the recording its payload correlates to. The retired companion chip (a 5px
  // dot plus the profile name, manual overrides only) is gone: identity now
  // lives inside the vessel, and the name is taught beside the stamp on the
  // Enhancement page where there is room for it.
  const dictationProfile =
    presentedDictation.authoritative &&
    presentedDictation.recordingId !== null &&
    (presentedDictation.phase === "recording" ||
      presentedDictation.phase === "processing")
      ? presentedDictation.profile
      : null;
  // Excluded surfaces, all deliberate: arming (no dictation yet), HUD-origin
  // recordings (their 112px control layout owns its content), the blocked /
  // enhancement / idle / permission states (status, not identity),
  // and the dedicated recording-control window, which never reaches pill mode.
  const showPillProfileMark =
    dictationProfile !== null &&
    presentation.mode === "pill" &&
    !arming &&
    !hudOriginRecording &&
    !hudOriginSessionVisual &&
    (pillState === "recording" || pillState === "processing");
  // Live Preview replaces the pill for the same dictation, so omitting the
  // stamp there would hide the indicator from exactly the users who opted into
  // more visibility. Same rules, mirrored position in the controls row.
  const previewProfileMark =
    dictationProfile !== null &&
    presentation.mode === "preview" &&
    !hudOriginRecording &&
    !hudOriginSessionVisual &&
    !presentedDictation.composePreview
      ? dictationProfile
      : null;
  const pillWidth =
    hudOriginSessionVisual || arming
      ? RECORDING_CONTROL_ACTIVE_W
      // The permission pill paints its own 264px footprint
        // (CLAUDE_PERMISSION_PILL_W), not the legacy dictation pill widths.
        // MODE_GEOMETRY.permission already sizes the native window/region to
        // that footprint (Task 3.2); this vessel wrapper has to agree, or the
        // default flex-shrink squeezes the 264px-wide ClaudePermissionPill
        // child into whatever narrower width this ternary hands it.
        : pillState === "permission"
          ? CLAUDE_PERMISSION_PILL_W
          : pillState === "blocked" ||
              pillState === "enhance_timeout" ||
              pillState === "enhance_error"
            ? PILL_WIDTH
            : showPillProfileMark
              ? PILL_WIDTH_PROFILE
              : PILL_WIDTH_COMPACT;

  // The living element the pill paints for an ordinary dictation. Extracted so
  // the stamped and unstamped vessels share one definition and cannot drift.
  const dictationVisual =
    pillState === "processing" ? (
      <ProcessingSpinner />
    ) : (
      <VoiceBars
        state={
          pillState === "recording" ? (pillState as HUDState) : "idle"
        }
        rms={levels.rms}
        peak={levels.peak}
        color={DEFAULT_BAR_COLOR_VAR}
      />
    );

  const pillContent =
    arming ? (
      <ArmingIndicator />
    ) : hudOriginRecording ? (
      <HudRecordingControls
        rms={levels.rms}
        peak={levels.peak}
        barColor={DEFAULT_BAR_COLOR_VAR}
        disabled={dictationCommandsDisabled}
        onCancel={onCancelDictation}
        onFinish={onFinishDictation}
      />
    ) : pillState === "enhance_timeout" ? (
      <EnhancementTimeoutIndicator
        provider={
          (presentation.mode === "pill"
            ? presentation.timeoutProvider
            : undefined) ?? "custom"
        }
      />
    ) : pillState === "enhance_error" ? (
      <EnhancementErrorIndicator
        provider={
          (presentation.mode === "pill"
            ? presentation.errorProvider
            : undefined) ?? "custom"
        }
      />
    ) : pillState === "blocked" ? (
      <BlockedIndicator />
    ) : claudeAttention !== null &&
      pillState === claudeAttention.state ? (
      <ClaudePermissionPill
        // Resolved and sanitized at the hook seam (hud-session-name Task
        // 3.6); degrades to the literal fallback when the chain resolved
        // nothing.
        provider={claudeAttention.agent}
        sourceName={
          claudeAttention.sourceName ??
          agentProviderLabel(claudeAttention.agent)
        }
        detail={claudeAttention.detail}
        onMeasuredCardSizeChange={onClaudeCardMeasured}
        onAllow={allowPermission}
        onDeny={denyPermission}
        onDefer={deferPermission}
      />
    ) : showPillProfileMark && dictationProfile !== null ? (
      // [22px pad][16px stamp][8px gap][32px bars | 18px spinner][22px pad].
      // The vessel's justify-center does the padding; only the gap is declared.
      <div
        className="flex items-center justify-center"
        style={{ gap: PILL_PROFILE_GAP }}
      >
        <ProfileMark
          profileId={dictationProfile.profileId}
          name={dictationProfile.name}
          iconKey={dictationProfile.icon}
          size={16}
          testId="hud-profile-mark"
        />
        {dictationVisual}
      </div>
    ) : (
      dictationVisual
    );

  // Confirmation is a content crossfade and nothing else. Arming already
  // occupies the active width, so the vessel geometry is untouched here; only
  // the microphone -> X / VoiceBars / check swap is visible. Both layers are
  // absolutely positioned so the outgoing one cannot reflow the incoming one.
  const vesselBody =
    arming || hudOriginSessionVisual ? (
      <AnimatePresence initial={false}>
        <motion.div
          key={
            arming
              ? "arming"
              : hudOriginRecording
                ? "recording"
                : "processing"
          }
          className="absolute inset-0 flex items-center justify-center"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: hudReducedMotion ? 0.01 : 0.16 }}
        >
          {pillContent}
        </motion.div>
      </AnimatePresence>
    ) : (
      pillContent
    );
  const persistentVesselActive =
    arming ||
    (presentation.mode === "pill" && hudOriginSessionVisual);

  // The permission pill is a fully self-painting 74-128px card that overflows
  // this compact `rounded-full` vessel. Left painting, the vessel's own fill and
  // border show through as 1-2px corner nubs at the capsule's widest band, and
  // its elevation doubles up with the child's. For this one state the child
  // owns the vessel outright.
  const claudeOwnsVessel =
    pillState === "permission" && claudeAttention !== null;

  // Every control-family mode uses one 132x52 layout with an 8px bottom pad.
  // The seed rests on the pill's bottom edge and expands upward in-place.
  const controlFamilyMode =
    parkedRecordingControl ||
    isHudControlFamilyMode(presentationGeometryMode);
  const hudDesktopBorder =
    hudTheme === "dark"
      ? HUD_DARK_DESKTOP_BORDER
      : HUD_LIGHT_DESKTOP_BORDER;
  const hudDesktopElevation =
    hudTheme === "dark"
      ? HUD_DARK_DESKTOP_ELEVATION
      : HUD_LIGHT_DESKTOP_ELEVATION;
  const hudPersistentControlHighlight =
    hudTheme === "dark"
      ? HUD_DARK_PERSISTENT_CONTROL_HIGHLIGHT
      : HUD_LIGHT_PERSISTENT_CONTROL_HIGHLIGHT;
  const uiQaDynamicSurface: ClaudeCardSurface | null =
    presentationGeometryMode === "response"
      ? "response"
      : presentationGeometryMode === "summary"
        ? "summary"
      : presentationGeometryMode === "question"
        ? "question"
        : presentationGeometryMode === "permission"
          ? "permission"
          : presentationGeometryMode === "panel" ? "panel" : null;
  const uiQaMeasuredViewport =
    uiQaStateId !== null &&
    uiQaDynamicSurface !== null &&
    measuredCardSize?.surface === uiQaDynamicSurface
      ? hudModeGeometry(presentationGeometryMode, measuredCardSize)
      : null;
  const uiQaReady =
    uiQaStateId !== null &&
    (uiQaDynamicSurface === null || uiQaMeasuredViewport !== null);

  if (
    import.meta.env.DEV &&
    uiQaFixtureId === AGENT_MARK_COMPARISON_FIXTURE_ID
  ) {
    return <AgentMarkComparisonSurface />;
  }

  // The agent surfaces, built once so the rendered frame can be kept as the
  // leaving snapshot when it commits.
  const questionPillElement: ReactElement | null =
    leavingSnapshot === null && presentation.mode === "question_pill" && compactQuestion !== null ? (
      <CompactClaudePill
        sourceName={compactQuestion.sourceName}
        // A spoken question is a speaking pill like any other, so it gets
        // the same stop control as a response.
        speaking={activeQuestion?.phase === "speaking" && !answeredHoldActive}
        remainingCount={0}
        railPhase={questionRailPhase}
        accessibleLabel={compactQuestion.accessibleLabel}
        title={compactQuestion.title}
        railProps={{
          text: questionRailText,
          stableCharCount: questionRailStableCharCount,
          rms: levels.rms,
          peak: levels.peak,
          // A compact question draft opens the card instead of editing in
          // place. The card already owns the one freeform textarea, the
          // option digits, Enter-to-submit and native focus; a second
          // in-pill editor would be a second question input, which decision
          // 2026-07-28 ("one `ReplyRail`") exists to prevent.
          onEditStart: onQuestionDraftEdit,
        }}
        onMeasuredCardSizeChange={onClaudeCardMeasured}
      />
    ) : null;
  const responseElement: ReactElement | null =
    leavingSnapshot === null && questionPillElement === null && panelCompletion !== null ? (
      <AgentConversationContext.Provider value={forcedHudState?.conversation ?? conversations.find((item) => item.session_handle === (managedConsoleExpanded ? selectedManagedSession?.conversation_handle : completionQueue.active?.session_handle)) ?? null}>
      <AgentVoiceCompletion.Provider value={completionQueue.active?.speech_completed === true}>
      <ResponsePanel
        completion={panelCompletion}
        remainingCount={queuedCompletionCount(completionQueue)}
        truncated={detectTruncation(panelCompletion.text)}
        replyState="idle"
        rms={levels.rms}
        peak={levels.peak}
        speaking={speaking}
        spokenSummary={
          panelCompletion.kind === "response"
            ? panelCompletion.spoken_summary ?? null
            : null
        }
        markerKnown={
          panelCompletion.kind === "response" &&
          panelCompletion.spoken_summary !== undefined
        }
        replyWindow={replyWindow}
        replyDraft={replyDraft}
        canReply={replyTarget !== null}
        expanded={responseExpanded}
        showSummaryText={effectiveShowSummaryText}
        replyPhase={replyPhase}
        replyPreview={
          forcedHudState?.replyPreview ?? dictation.preview?.text ?? ""
        }
        replyKeptText={replyCapturing ? replyText : ""}
        replyStableCharCount={
          forcedHudState?.replyPreview !== undefined
            ? forcedHudState.replyPreview.length
            : (dictation.preview?.stableCharCount ?? 0)
        }
        sentReply={sentReply}
        sessions={presentedSessionsState.sessions}
        activeSessionHandle={
          managedConsoleExpanded ? presentedSessionsState.activeHandle : stripActiveHandle
        }
        onSelectSession={onSelectSession}
        onCardFocusChange={setManagedCardFocused}
        compactConsole={managedConsoleExpanded}
        consoleResize={consoleResize.handlers}
        onToggleExpanded={toggleHudExpansion}
        replyAutoSubmit={
          forcedHudState?.replyAutoSubmit ?? replyAutoSubmit
        }
        replyNotice={replyNotice}
        onCopyDraft={copyReply}
        editingText={replyText}
        onDraftEdit={onDraftEdit}
        onEditingTextChange={onEditingDraftChange}
        onEditingKeyDown={onEditingDraftKeyDown}
        editingInputRef={editingInputRef}
        onMeasuredCardSizeChange={onClaudeCardMeasured}
        replyShortcuts={replyShortcuts}
        replyHintRetired={replyHintRetired}
        glanceGrowFromPill={!surfaceSettling}
      />
      </AgentVoiceCompletion.Provider>
      </AgentConversationContext.Provider>
    ) : null;
  const questionElement: ReactElement | null =
    leavingSnapshot === null &&
    questionPillElement === null &&
    responseElement === null &&
    presentation.mode === "question" &&
    activeQuestion !== null ? (
      <QuestionPanel
        active={activeQuestion}
        onMeasuredCardSizeChange={onClaudeCardMeasured}
        questionIndex={questionIndex}
        selected={questionSelections}
        freeform={presentedFreeform}
        interimText={
          forcedHudState
            ? (forcedHudState.replyPreview ?? null)
            : questionOwnsDictation
              ? (dictation.preview?.text ?? null)
              : null
        }
        interimStableCharCount={
          forcedHudState?.replyPreview !== undefined
            ? forcedHudState.replyPreview.length
            : (dictation.preview?.stableCharCount ?? 0)
        }
        replyPhase={
          forcedHudState !== null
            ? questionServerDraft?.phase === "recording" ||
              questionServerDraft?.phase === "processing"
              ? questionServerDraft.phase
              : "idle"
            : questionOwnsDictation
              ? dictation.phase
              : "idle"
        }
        rms={levels.rms}
        peak={levels.peak}
        onOption={chooseQuestionOption}
        onFreeform={(question, text) => {
          if (question === currentQuestionKey && questionTarget !== null) {
            editReply(questionTarget, text);
          }
          setQuestionFreeform((current) => ({
            ...current,
            [question]: text,
          }));
          setQuestionSelections((current) => ({
            ...current,
            [question]: [],
          }));
        }}
        onSubmit={submitQuestionAnswers}
        inputRef={questionInputRef}
      />
    ) : null;
  const renderedSurfaceSnapshot: SurfaceSnapshot | null =
    questionPillElement !== null
      ? { kind: "question_pill", element: questionPillElement }
      : responseElement !== null
        ? { kind: "response", element: responseElement }
        : questionElement !== null
          ? { kind: "question", element: questionElement }
          : null;
  // A leaving surface keeps the frame it last committed.
  useLayoutEffect(() => {
    committedSurfaceRef.current =
      surfaceFrame === null || surfaceFrame.phase === "leaving"
        ? surfaceFrame
        : { ...surfaceFrame, snapshot: renderedSurfaceSnapshot };
  });
  // Response panel mode: the card grows from the pill spring. Keyed by surface
  // family, so expand and contract reveal the new card in its new window with
  // one spring, after `surfaceSettling` clears. The entrance runs only while
  // the surface is appearing: a card mounted while steady does not replay it.
  const leavingToHidden = !visible && leavingSnapshot !== null;
  const surfaceReveal = (content: ReactElement): ReactElement => (
    <motion.div
      key={(surfaceFrame?.surface ?? (consoleOpen ? "console" : "card")) === "console" ? "console" : "card"}
      data-testid="hud-surface-reveal"
      className="flex flex-col items-center"
      initial={
        hudReducedMotion || (surfaceFrame !== null && surfaceFrame.phase !== "appearing")
          ? false
          : { opacity: 0, scale: 0.97 }
      }
      animate={
        (surfaceSettling || leavingToHidden) && !hudReducedMotion
          ? // The console fades at its own size; the pill-side cards recede.
            { opacity: 0, scale: surfaceFrame?.surface === "console" ? 1 : 0.97 }
          : { opacity: 1, scale: 1 }
      }
      transition={
        hudReducedMotion
          ? { duration: 0 }
          : leavingToHidden
            ? { duration: HIDE_ANIMATION_MS / 1000 * 0.8, ease: "easeIn" }
            : PILL_SPRING
      }
    >
      {content}
    </motion.div>
  );

  return (
    <AudioLevelsContext.Provider value={forcedHudState ? null : levelsRef}>
    <div
      ref={hudRootRef}
      className={`relative flex h-full w-full flex-col items-center gap-1 ${
        controlFamilyMode || replySurfaceLayout
          ? "justify-end"
          : "justify-center"
      } ${
        arming ||
        hudOriginSessionVisual ||
        (previewSurfaceMounted && previewExpanded)
          ? "cursor-default"
          : "cursor-grab active:cursor-grabbing"
      }`}
      data-testid="hud-root"
      data-hud-palette={`desktop-${hudTheme}`}
      data-ui-qa-root={uiQaStateId ?? undefined}
      data-ui-qa-state={uiQaStateId ?? undefined}
      data-ui-qa-ready={uiQaStateId === null ? undefined : String(uiQaReady)}
      data-ui-qa-surface={uiQaStateId === null ? undefined : "hud"}
      data-ui-qa-viewport-mode={
        uiQaMeasuredViewport === null ? undefined : presentationGeometryMode
      }
      data-ui-qa-viewport-width={uiQaMeasuredViewport?.windowW}
      data-ui-qa-viewport-height={uiQaMeasuredViewport?.windowH}
      style={{
        ...(hudTheme === "dark"
          ? HUD_DARK_DESKTOP_PALETTE
          : HUD_LIGHT_DESKTOP_PALETTE),
        background: "transparent",
        ...(consoleFading
          ? {
              opacity: 0,
              transition: `opacity ${consoleFadeMs}ms ease`,
              pointerEvents: "none" as const,
            }
          : null),
        visibility:
          managedConsoleDismissedToHidden ||
          surfaceSettling ||
          preparingCleanRecordingFromPermanent ||
          preparingPermanentControlFromOtherFamily
            ? "hidden"
            : undefined,
        touchAction: "none",
        paddingBottom:
          controlFamilyMode
            ? RECORDING_CONTROL_NATIVE_PAD
            : replySurfaceLayout
              ? REPLY_SURFACE_BOTTOM_PAD
              : undefined,
      }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerEnter={
        presentation.mode === "control"
          ? onRecordingControlPointerEnter
          : undefined
      }
      onPointerLeave={
        presentation.mode === "control"
          ? onRecordingControlPointerLeave
          : undefined
      }
      onDoubleClick={managedConsoleExpanded ? undefined : toggleHudExpansion}
    >
      {managedConsoleDismissedToHidden ? null : presentation.mode === "control" ||
        parkedRecordingControl ||
        persistentVesselActive ? (
        <PersistentRecordingControl
          expanded={parkedRecordingControl ? false : controlCardExpanded}
          active={persistentVesselActive}
          activeContent={persistentVesselActive ? vesselBody : null}
          parked={parkedRecordingControl}
          returningFromActive={
            parkedRecordingControl ? false : returningControlVessel
          }
          desktopBorder={hudDesktopBorder}
          desktopHighlight={hudPersistentControlHighlight}
          connected={connected}
          pending={dictationCommandPending === "start"}
          onStart={onStartDictation}
        />
      ) : leavingSnapshot !== null ? (
        // A leaving surface keeps its last steady frame in its own slot (same
        // element types and key), so it fades where it is instead of the
        // generic pill or a successor standing in for it.
        leavingSnapshot.kind === "response" ? (
          surfaceReveal(leavingSnapshot.element)
        ) : (
          leavingSnapshot.element
        )
      ) : questionPillElement !== null ? (
        questionPillElement
      ) : responseElement !== null ? (
        surfaceReveal(responseElement)
      ) : questionElement !== null ? (
        questionElement
      ) : previewSurfaceMounted ? (
        <PreviewCard
          phase={
            presentation.mode === "preview"
              ? presentation.phase
              : "processing"
          }
          preview={presentedDictation.preview}
          rms={levels.rms}
          peak={levels.peak}
          barColor={DEFAULT_BAR_COLOR_VAR}
          expanded={previewExpanded}
          cardWidth={
            previewExpanded
              ? expandedPreviewCardSize.width
              : PREVIEW_CARD_W
          }
          cardHeight={
            previewExpanded
              ? expandedPreviewCardSize.height
              : PREVIEW_CARD_H
          }
          closing={closingPreview}
          hudOriginRecording={hudOriginRecording}
          commandsDisabled={dictationCommandsDisabled}
          onCancel={onCancelDictation}
          onFinish={onFinishDictation}
          onExpandedChange={onPreviewExpandedChange}
          onNaturalTextHeightChange={onPreviewNaturalTextHeightChange}
          agentCompose={dictation.composePreview}
          profileMark={previewProfileMark}
        />
      ) : (
        // Pill mode — dictation, Claude attention, or a transient status
        <motion.div
          data-testid="hud-pill-vessel"
          className={`relative flex items-center justify-center${claudeOwnsVessel ? "" : " rounded-full"}`}
          // Arming mounts this vessel in place of the launcher card, so it has
          // to start at the launcher width and spring out to the active width.
          // Without it framer would mount straight at the target and the click
          // would still read as an instant jump rather than an expansion.
          initial={
            arming
              ? {
                  scale: 1,
                  opacity: 1,
                  width: RECORDING_CONTROL_LAUNCHER_W,
                }
              : undefined
          }
          animate={{
            scale: visible ? 1 : 0.9,
            opacity: visible ? 1 : 0,
            width: pillWidth,
          }}
          transition={{ type: "spring", damping: 20, stiffness: 300 }}
          style={{
            height: claudeOwnsVessel ? "auto" : PILL_HEIGHT,
            // Desktop-independent charcoal. App theme cannot predict the
            // destination window under this transparent, always-on-top HUD.
            background: claudeOwnsVessel
              ? "transparent"
              : "var(--surface-overlay)",
            border: claudeOwnsVessel
              ? "none"
              : `1px solid ${hudDesktopBorder}`,
            boxShadow: claudeOwnsVessel ? "none" : hudDesktopElevation,
          }}
        >
          {vesselBody}
        </motion.div>
      )}
    </div>
    </AudioLevelsContext.Provider>
  );
}
