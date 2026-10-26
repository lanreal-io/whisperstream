// Whole-HUD coverage driven through the socket event pipeline: dictation
// lifecycle, live preview graphemes, expansion geometry, and window
// reconciliation.
//
// Round 02 moved response queueing, dwell, and acknowledgement out of the
// client and onto the backend (`agent.response_queue.snapshot` replaced
// `agent.completion` / `agent.completions.snapshot` /
// `agent.completion.acknowledged`). The tests that pinned that retired wire
// protocol were removed here; their behaviour is now covered by
// tests/test_claude_interactions.py and agentCompletionQueue.test.ts. Nothing
// else was dropped. Component-level Claude interaction coverage lives in
// HUD.interactions.test.tsx.
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import { Profiler } from "react";

import type { AgentCompletion, AgentCompletionUpsertEvent, AgentReplyDraft, AgentReplyReceipt, AgentResponseCompletion, HudSession } from "../lib/socket/agentEvents";
import type { AppEvent, EnhancementTimeoutProvider } from "../lib/socket/events";
import { AGENT_MARK_VARIANTS, AgentMark, ClaudeIdentityRow } from "./hud/components/AgentIdentity";
import { AgentMarkComparisonSurface } from "./hud/HudAgentMarkComparison";
import { REPLY_RAIL_CARD_MAX_H, REPLY_RAIL_CARD_TEXT_MAX_H, REPLY_RAIL_TEXT_MAX_H, ReplyRail } from "./hud/components/ReplyRail";
import { HIDE_ANIMATION_MS } from "./hud/presentation";
import { CONSOLE_CONTRACT_FADE_MS, HUD, HUD_FINISH_PROCESSING_MIN_MS, HUD_OUTCOME_COLLAPSE_SETTLE_MS, SENT_REPLY_HOLD_MS, RECORDING_CONTROL_CARD_SETTLE_MS, RECORDING_CONTROL_CARD_SETTLE_REDUCED_MS, TERMINAL_QUESTION_DWELL_MS } from "./HUD";
import { ResponsePanel } from "./hud/views/ResponsePanel";
import { SESSION_TAB_CHROME_COST, SessionTabs, SessionTabStatusGlyph, sessionTabAccessibleName } from "./hud/views/ExpandedResponse";
import { CONSOLE_DEFAULT_H, CONSOLE_DEFAULT_W, CONSOLE_PAD_X, CONSOLE_PAD_Y, GLANCE_CARD_W, CLAUDE_CARD_RADIUS, CLAUDE_CARD_W, CLAUDE_PERMISSION_MAX_H, CLAUDE_PERMISSION_MIN_H, CLAUDE_PERMISSION_PILL_RADIUS, CLAUDE_PERMISSION_PILL_W, CLAUDE_PILL_H, CLAUDE_PILL_MIN_W, CLAUDE_PILL_RADIUS, CLAUDE_RAIL_MAX_H, CLAUDE_RAIL_PILL_RADIUS, CLAUDE_RAIL_PILL_W, HUD_ELEVATION_EXTENT, HUD_REGION_EDGE_BLEED, MODE_GEOMETRY, PANEL_HEIGHT, PANEL_WIDTH, PREVIEW_CARD_H, PREVIEW_CARD_RADIUS, PREVIEW_CARD_W, PREVIEW_PAD_X, PREVIEW_PAD_Y, PREVIEW_COMPACT_LINE_COUNT, PREVIEW_COMPACT_WELL_H, PREVIEW_EXPANDED_CARD_W, PREVIEW_EXPANDED_MIN_CARD_H, PREVIEW_EXPANDED_WIDTH, PREVIEW_HEIGHT, PREVIEW_WIDTH, PILL_WIDTH_PROFILE, QUESTION_MAX_H, QUESTION_MIN_H, QUESTION_PAD_X, QUESTION_PAD_Y, REPLY_SURFACE_BOTTOM_PAD, REPLY_SURFACE_WINDOW_STEP, RESPONSE_MAX_H, RESPONSE_MIN_H, claudeCardCeiling, hudModeGeometry, hudWindowRegionForMode, pillAnchorFromWindowPosition, pillRectFromAnchor, windowPositionFromPillAnchor } from "./hud/geometry";
import { HUD_DARK_DESKTOP_BORDER, HUD_DARK_DESKTOP_ELEVATION, HUD_DARK_PERSISTENT_CONTROL_HIGHLIGHT, HUD_DARK_DESKTOP_SURFACE, HUD_LIGHT_DESKTOP_BORDER, HUD_LIGHT_DESKTOP_ELEVATION, HUD_LIGHT_PERSISTENT_CONTROL_HIGHLIGHT, HUD_LIGHT_DESKTOP_SURFACE } from "./hud/material";
import { RECORDING_CONTROL_COLLAPSE_MS } from "./hud/recordingBridge";
import { HUD_FORCE_KEY } from "./hud/hudForceState";

const mocks = vi.hoisted(() => {
  const handlers = new Set<(event: unknown) => void>();
  const tauriEventHandlers = new Map<
    string,
    Set<(event: { payload: unknown }) => void>
  >();
  const windowCalls: string[] = [];
  const tauriEventTrace = { enabled: false };
  const glyphMotionRenderCounts = new Map<string, number>();
  const snapGuide = {
    setSize: vi.fn(async () => undefined),
    setPosition: vi.fn(async () => undefined),
    setIgnoreCursorEvents: vi.fn(async () => undefined),
    show: vi.fn(async () => undefined),
    hide: vi.fn(async () => undefined),
  };
  const tauriEmit = vi.fn(async (eventName: string, payload: unknown) => {
    if (tauriEventTrace.enabled) {
      windowCalls.push(`event:${eventName}:${String(payload)}`);
    }
    const handlers = tauriEventHandlers.get(eventName);
    handlers?.forEach((handler) => handler({ payload }));
  });

  const state: {
    atomicBoundsAvailable: boolean;
    showRegionAttempt: Promise<void> | null;
    hideRegionAttempt: Promise<void> | null;
    nativeHandoffAttempt: Promise<void> | null;
    tauriVisibleCache: boolean;
    physicallyVisible: boolean;
    nativeRegionInstalled: boolean;
  } = {
    atomicBoundsAvailable: true,
    showRegionAttempt: null,
    hideRegionAttempt: null,
    nativeHandoffAttempt: null,
    tauriVisibleCache: false,
    physicallyVisible: false,
    nativeRegionInstalled: false,
  };

  return {
    handlers,
    tauriEventHandlers,
    tauriEventTrace,
    tauriEmit,
    windowCalls,
    glyphMotionRenderCounts,
    snapGuide,
    getSnapGuide: vi.fn(async () => null as typeof snapGuide | null),
    // Flip to false to exercise the two-step setPosition/setSize fallback the
    // reconciler keeps for browser dev and jsdom (no Tauri command bridge).
    state,
    invoke: vi.fn(
      async (
        command: string,
        args: Record<string, number | boolean> = {},
      ) => {
        if (command === "hide_recording_control_window") {
          windowCalls.push("dedicated-control-hide");
          return;
        }
        if (command === "set_hud_window_borderless_visibility") {
          if (args.hide) {
            windowCalls.push("borderless-hide");
            state.physicallyVisible = false;
          } else if (args.show) {
            windowCalls.push("borderless-show");
            state.physicallyVisible = true;
          }
          // Native ShowWindow deliberately leaves tao's visibility cache
          // untouched. A later clean-family handoff routes through Tauri
          // hide/show so its cache again matches that owner's visible state.
          state.nativeRegionInstalled = false;
          return;
        }
        if (command === "hide_and_clear_hud_window_region") {
          windowCalls.push("native-hide+clear");
          state.physicallyVisible = false;
          state.nativeRegionInstalled = false;
          if (state.nativeHandoffAttempt !== null) {
            await state.nativeHandoffAttempt;
          }
          return;
        }
        if (command === "set_hud_window_region") {
          if (args.hide) {
            windowCalls.push("hide");
            state.physicallyVisible = false;
          }
          windowCalls.push(
            `${args.show ? "show+" : ""}${
              args.restricted
                ? `region:${args.x},${args.y},${args.width}x${args.height}`
                : "region:full"
            }`,
          );
          state.nativeRegionInstalled = true;
          if (args.show) state.physicallyVisible = true;
          if (args.show && state.showRegionAttempt !== null) {
            await state.showRegionAttempt;
          }
          if (args.hide && state.hideRegionAttempt !== null) {
            await state.hideRegionAttempt;
          }
          return;
        }
        if (command !== "set_hud_bounds") {
          throw new Error(`unexpected command: ${command}`);
        }
        if (!state.atomicBoundsAvailable) {
          throw new Error("no tauri command bridge");
        }
        windowCalls.push(
          `bounds:${args.x},${args.y},${args.width}x${args.height}`,
        );
      },
    ),
    connected: true,
    send: vi.fn((_message: Record<string, unknown>) => true),
    sendQuestionAnswers: vi.fn(),
    stopSpeech: vi.fn(async () => undefined),
    fetch: vi.fn(),
    dedicatedRecordingControlWindow: false,
    reducedMotion: false,
    naturalTextHeight: 0,
    // The painted footprint the one measured-card routine reports for whichever
    // Claude card surface is mounted (Task 3.1, design decision 17). jsdom has
    // no layout, so the offsetWidth/offsetHeight stubs installed in beforeEach
    // hand these numbers to the element carrying `data-claude-card-measure`.
    claudeCardSize: { width: 0, height: 0 },
    hudWindow: {
      setSize: vi.fn(async (size: { width: number; height: number }) => {
        windowCalls.push(`size:${size.width}x${size.height}`);
      }),
      setPosition: vi.fn(async (position: { x: number; y: number }) => {
        windowCalls.push(`position:${position.x},${position.y}`);
      }),
      show: vi.fn(async () => {
        windowCalls.push("show");
        state.tauriVisibleCache = true;
        state.physicallyVisible = true;
      }),
      hide: vi.fn(async () => {
        windowCalls.push("hide");
        if (state.tauriVisibleCache) state.physicallyVisible = false;
        state.tauriVisibleCache = false;
      }),
      outerPosition: vi.fn(async () => ({ x: 0, y: 0 })),
      outerSize: vi.fn(async () => ({ width: 160, height: 80 })),
      setFocusable: vi.fn(async (focusable: boolean) => {
        windowCalls.push(`focusable:${focusable}`);
      }),
    },
    currentMonitor: vi.fn(async () => ({
      scaleFactor: 1,
      position: { x: 0, y: 0 },
      size: { width: 1920, height: 1080 },
      workArea: {
        position: { x: 0, y: 0 },
        size: { width: 1920, height: 1080 },
      },
    })),
  };
});

vi.mock("../lib/socket/connection", async (importOriginal) => ({
    ...(await importOriginal<typeof import("../lib/socket/connection")>()),
    useAppSocket: () => ({
    connected: mocks.connected,
    send: mocks.send,
    subscribe: (handler: (event: unknown) => void) => {
      mocks.handlers.add(handler);
      return () => mocks.handlers.delete(handler);
    },
  }),
  }));
vi.mock("../lib/socket/agentEvents", async (importOriginal) => ({
    ...(await importOriginal<typeof import("../lib/socket/agentEvents")>()),
    sendAgentQuestionOpportunityAnswers: mocks.sendQuestionAnswers,
  }));

vi.mock("../lib/api/client", () => ({
    BASE: "http://localhost:38420",
  }));
vi.mock("../lib/recordingControl", () => ({
  BASE: "http://localhost:38420",
  usesDedicatedRecordingControlWindow: () =>
    mocks.dedicatedRecordingControlWindow,
  HUD_MAIN_VISIBILITY_EVENT: "hud-main-visibility-changed",
  RECORDING_CONTROL_START_PENDING_EVENT:
    "recording-control-start-pending",
  RECORDING_CONTROL_VISIBILITY_STORAGE_KEY:
    "ws-recording-control-visible",
  RECORDING_CONTROL_VISIBILITY_EVENT:
    "recording-control-visibility-changed",
  SCREEN_READER_STATUS_STORAGE_KEY: "ws-screen-reader-detected",
  SCREEN_READER_STATUS_EVENT: "screen-reader-status-changed",
  HUD_COMMAND_HEADERS: {
    "X-Whisperstream-Client": "hud-v1",
  },
  getScreenReaderStatus: async () => {
    const response = await mocks.fetch(
      "http://localhost:38420/api/accessibility/screen-reader",
    );
    if (!response.ok) throw new Error("screen_reader_probe_failed");
    return response.json();
  },
  stopSpeech: mocks.stopSpeech,
}));
vi.mock("../lib/accessibility", () => ({
  BASE: "http://localhost:38420",
  usesDedicatedRecordingControlWindow: () =>
    mocks.dedicatedRecordingControlWindow,
  HUD_MAIN_VISIBILITY_EVENT: "hud-main-visibility-changed",
  RECORDING_CONTROL_START_PENDING_EVENT:
    "recording-control-start-pending",
  RECORDING_CONTROL_VISIBILITY_STORAGE_KEY:
    "ws-recording-control-visible",
  RECORDING_CONTROL_VISIBILITY_EVENT:
    "recording-control-visibility-changed",
  SCREEN_READER_STATUS_STORAGE_KEY: "ws-screen-reader-detected",
  SCREEN_READER_STATUS_EVENT: "screen-reader-status-changed",
  HUD_COMMAND_HEADERS: {
    "X-Whisperstream-Client": "hud-v1",
  },
  getScreenReaderStatus: async () => {
    const response = await mocks.fetch(
      "http://localhost:38420/api/accessibility/screen-reader",
    );
    if (!response.ok) throw new Error("screen_reader_probe_failed");
    return response.json();
  },
  stopSpeech: mocks.stopSpeech,
}));
vi.mock("../lib/api/dictation", () => ({
    HUD_COMMAND_HEADERS: {
    "X-Whisperstream-Client": "hud-v1",
  },
  }));
vi.mock("../lib/api/system", () => ({
    getScreenReaderStatus: async () => {
    const response = await mocks.fetch(
      "http://localhost:38420/api/accessibility/screen-reader",
    );
    if (!response.ok) throw new Error("screen_reader_probe_failed");
    return response.json();
  },
  }));
vi.mock("../lib/api/speech", () => ({
    stopSpeech: mocks.stopSpeech,
  }));

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => false,
  invoke: mocks.invoke,
}));

vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => mocks.hudWindow,
  currentMonitor: mocks.currentMonitor,
  Window: {
    getByLabel: mocks.getSnapGuide,
  },
}));

vi.mock("@tauri-apps/api/dpi", () => ({
  LogicalPosition: class LogicalPosition {
    constructor(
      public x: number,
      public y: number,
    ) {}
  },
  LogicalSize: class LogicalSize {
    constructor(
      public width: number,
      public height: number,
    ) {}
  },
}));

vi.mock("@tauri-apps/api/event", () => ({
  emit: mocks.tauriEmit,
  listen: vi.fn(
    async (
      eventName: string,
      handler: (event: { payload: unknown }) => void,
    ) => {
      const handlers = mocks.tauriEventHandlers.get(eventName) ?? new Set();
      handlers.add(handler);
      mocks.tauriEventHandlers.set(eventName, handlers);
      return () => handlers.delete(handler);
    },
  ),
}));

vi.mock("framer-motion", async () => {
  const React = await import("react");
  const motionComponents = new Map<
    string,
    (props: Record<string, unknown>) => ReturnType<typeof React.createElement>
  >();
  const motionProps = new Set([
    "animate",
    "exit",
    "initial",
    "layout",
    "layoutId",
    "transition",
    "variants",
    "whileHover",
    "whileTap",
  ]);
  const motion = new Proxy(
    {},
    {
      get: (_target, tag: string) => {
        const cached = motionComponents.get(tag);
        if (cached) return cached;
        const component = ({ children, ...props }: Record<string, unknown>) => {
          const initial = props.initial as
            | Record<string, unknown>
            | false
            | undefined;
          const animate = props.animate as Record<string, unknown> | undefined;
          const exit = props.exit as Record<string, unknown> | undefined;
          const glyphId = props["data-grapheme-id"];
          if (typeof glyphId === "number") {
            const key = String(glyphId);
            mocks.glyphMotionRenderCounts.set(
              key,
              (mocks.glyphMotionRenderCounts.get(key) ?? 0) + 1,
            );
          }
          if (animate && typeof animate === "object") {
            if ("width" in animate) props["data-motion-width"] = String(animate.width);
            if ("height" in animate) props["data-motion-height"] = String(animate.height);
            if ("filter" in animate) {
              props["data-motion-animate-filter"] = String(animate.filter);
            }
            if ("opacity" in animate) {
              props["data-motion-animate-opacity"] = String(animate.opacity);
            }
            if ("scale" in animate) {
              props["data-motion-animate-scale"] = String(animate.scale);
            }
          }
          if (initial && typeof initial === "object" && "y" in initial) {
            props["data-motion-initial-y"] = String(initial.y);
          }
          if (initial && typeof initial === "object" && "filter" in initial) {
            props["data-motion-initial-filter"] = String(initial.filter);
          }
          if (initial && typeof initial === "object" && "opacity" in initial) {
            props["data-motion-initial-opacity"] = String(initial.opacity);
          }
          if (initial && typeof initial === "object" && "scale" in initial) {
            props["data-motion-initial-scale"] = String(initial.scale);
          }
          if (initial === false) {
            props["data-motion-initial"] = "false";
          }
          if (exit && typeof exit === "object" && "y" in exit) {
            props["data-motion-exit-y"] = String(exit.y);
          }
          const transition = props.transition as Record<string, unknown> | undefined;
          if (transition && "duration" in transition) {
            props["data-motion-duration"] = String(transition.duration);
          }
          if (transition && "delay" in transition) {
            props["data-motion-delay"] = String(transition.delay);
          }
          for (const key of motionProps) delete props[key];
          return React.createElement(tag, props, children as React.ReactNode);
        };
        motionComponents.set(tag, component);
        return component;
      },
    },
  );

  return {
    motion,
    AnimatePresence: ({ children }: { children: React.ReactNode }) => children,
    useReducedMotion: () => mocks.reducedMotion,
  };
});

function response(
  completionId: string,
  sequence: number,
): AgentResponseCompletion {
  return {
    completion_id: completionId,
    sequence,
    kind: "response",
    text: `Reply ${completionId}`,
    agent: "claude",
    source_label: "Claude · project",
    empty: false,
  };
}

let backendPendingCompletionIds: string[] = [];

function upsert(
  completion: AgentCompletion,
  showText = true,
  pendingCompletionIds?: string[],
): AgentCompletionUpsertEvent {
  if (pendingCompletionIds !== undefined) {
    backendPendingCompletionIds = [...pendingCompletionIds];
  } else if (
    showText &&
    !backendPendingCompletionIds.includes(completion.completion_id)
  ) {
    backendPendingCompletionIds = [
      ...backendPendingCompletionIds,
      completion.completion_id,
    ];
  }
  return {
    type: "agent.completion",
    version: 1,
    show_text: showText,
    completion,
    pending_completion_ids: [...backendPendingCompletionIds],
  };
}

function responseSnapshot(
  completion: AgentResponseCompletion,
  revision = 1,
  showSummaryText = false,
): Extract<AppEvent, { type: "agent.response_queue.snapshot" }> {
  return {
    type: "agent.response_queue.snapshot",
    version: 1,
    revision,
    phase: "active_grace",
    ordered_completion_ids: [completion.completion_id],
    completions: [completion],
    active: {
      completion_id: completion.completion_id,
      phase: "active_grace",
      request_id: null,
      session_handle: null,
      generation: revision,
      engaged: false,
      routable: false,
      passive_total_seconds: 10,
      passive_remaining_seconds: 10,
      hard_remaining_seconds: 120,
    },
    quiet_delay_remaining_seconds: 0,
    show_summary_text: showSummaryText,
  };
}

/** A response completion carrying a spoken summary, so the HUD can be driven
 *  into (and out of) the speaking state without changing anything else. */
function spokenResponse(
  completionId: string,
  sequence: number,
): AgentResponseCompletion {
  return { ...response(completionId, sequence), spoken_summary: "Spoken line." };
}

/** `responseSnapshot` with the two fields the footprint-invariance tests move:
 *  the queue phase (speaking on/off) and the active reply phase. */
function responseSnapshotWith(
  completion: AgentResponseCompletion,
  revision: number,
  overrides: {
    phase?: "active_speaking" | "active_grace";
    /** The conversation the card belongs to (its reply target is `conv:`). */
    sessionHandle?: string;
    showSummaryText?: boolean;
  } = {},
): Extract<AppEvent, { type: "agent.response_queue.snapshot" }> {
  const base = responseSnapshot(
    completion,
    revision,
    overrides.showSummaryText ?? false,
  );
  const phase = overrides.phase ?? "active_grace";
  return {
    ...base,
    phase,
    show_summary_text: overrides.showSummaryText ?? false,
    active: {
      ...base.active!,
      phase,
      // One response identity across revisions: a generation bump is a new
      // response and legitimately collapses the expanded card, which would
      // hide the footprint invariance these tests are about.
      generation: 1,
      ...(overrides.sessionHandle === undefined
        ? {}
        : { session_handle: overrides.sessionHandle }),
    },
  };
}

/** The reply owner's draft for one target (agent_reply_desk). */
function replyDraftFor(
  target: string,
  overrides: Partial<AgentReplyDraft> = {},
): AgentReplyDraft {
  return {
    draft_id: `draft-${target}`,
    target_id: target,
    kind: target.startsWith("question:") ? "question" : "reply",
    phase: "ready",
    text: "",
    text_revision: 1,
    compose_generation: null,
    capture_mode: null,
    held: false,
    delivery: "now",
    outcome: null,
    ...overrides,
  };
}

let replyRevision = 0;

function replySnapshot(
  drafts: AgentReplyDraft[] = [],
  receipts: AgentReplyReceipt[] = [],
): Extract<AppEvent, { type: "agent.reply.snapshot" }> {
  replyRevision += 1;
  return {
    type: "agent.reply.snapshot",
    version: 1,
    epoch: "test-epoch",
    revision: replyRevision,
    foreground: null,
    drafts,
    receipts,
    enter_owner: "none",
  };
}

/** A wrapped session whose console and hook cards share `conv:<handle>-conv`. */
function managedSession(handle: string, overrides: Partial<HudSession> = {}): HudSession {
  return {
    session_handle: handle,
    agent: "claude",
    source_name: "whisperstream",
    unread: false,
    needs_input: false,
    can_reply: true,
    accepts_reply: true,
    latest_response: null,
    conversation_handle: `${handle}-conv`,
    reply_target: `conv:${handle}-conv`,
    ...overrides,
  };
}

function sentCommands(type: string): Record<string, unknown>[] {
  return mocks.send.mock.calls
    .map(([message]) => message as Record<string, unknown>)
    .filter((message) => message.type === type);
}

function terminalResponseSnapshot(
  revision: number,
  phase: "idle" | "quiet_delay" = "quiet_delay",
): Extract<AppEvent, { type: "agent.response_queue.snapshot" }> {
  return {
    type: "agent.response_queue.snapshot",
    version: 1,
    revision,
    phase,
    ordered_completion_ids: [],
    completions: [],
    active: null,
    quiet_delay_remaining_seconds: phase === "quiet_delay" ? 2 : 0,
  };
}

function questionSnapshot(
  revision: number,
  overrides: {
    generation?: number;
    phase?: string;
    composeGeneration?: number | null;
  } = {},
): Extract<AppEvent, { type: "agent.question.snapshot" }> {
  return {
    type: "agent.question.snapshot",
    version: 1,
    revision,
    active: {
      session_handle: "handle",
      generation: overrides.generation ?? 1,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      phase: (overrides.phase ?? "surfaced") as any,
      questions: [
        {
          question: "Which environment should I deploy to?",
          header: "Deploy",
          multiSelect: false,
          options: [{ label: "Staging" }, { label: "Production" }],
        },
      ],
      current_index: 0,
      request_id: null,
      routable: false,
      capture_available: false,
      remaining_seconds: 6,
      answers: {},
    },
  };
}

/** One pending, content-free permission interaction: the only Claude state
 *  that reaches the HUD through `agent.interaction.state`. */
function pendingPermission(
  sequence: number,
): Extract<AppEvent, { type: "agent.interaction.state" }> {
  return {
    type: "agent.interaction.state",
    version: 1,
    interaction: {
      request_id: "req-permission",
      session_handle: "handle",
      kind: "permission",
      sequence,
      status: "pending",
      active: true,
      engaged: true,
      passive_seconds: 6,
      remaining_seconds: 120,
      hard_remaining_seconds: 120,
    },
  };
}

let permissionSnapshotRevision = 0;
function emit(event: AppEvent): void {
  if (event.type === "agent.interaction.state") event = { type: "agent.interactions.snapshot", version: 1, revision: ++permissionSnapshotRevision, interactions: [event] };
  act(() => {
    for (const handler of [...mocks.handlers]) handler(event);
  });
}

function lifecycle(
  recordingId: number,
  phase: "recording" | "processing" | "idle",
  overrides: Partial<Extract<AppEvent, { type: "dictation.state" }>> = {},
): Extract<AppEvent, { type: "dictation.state" }> {
  return {
    type: "dictation.state",
    version: 1,
    recording_id: recordingId,
    phase,
    origin: "push_to_talk",
    hud_enabled: true,
    preview_enabled: true,
    screen_reader_detected: false,
    ...overrides,
  };
}

function emitTauri(eventName: string, payload: unknown): void {
  act(() => {
    for (const handler of mocks.tauriEventHandlers.get(eventName) ?? []) {
      handler({ payload });
    }
  });
}

function preview(
  recordingId: number,
  revision: number,
  text: string,
  stableCharCount = 0,
  mutableStartCharCount = 0,
  freeze = false,
): Extract<AppEvent, { type: "transcription.preview" }> {
  return {
    type: "transcription.preview",
    recording_id: recordingId,
    revision,
    text,
    stable_char_count: stableCharCount,
    presentation_version: 1,
    mutable_start_char_count: mutableStartCharCount,
    freeze,
  };
}

function previewGraphemes(): HTMLElement[] {
  return screen.queryAllByTestId("preview-grapheme");
}

interface ScrollMetrics {
  scrollHeight: number;
  clientHeight: number;
  scrollTop: number;
}

function installScrollMetrics(
  element: HTMLElement,
  metrics: ScrollMetrics,
): void {
  Object.defineProperties(element, {
    scrollHeight: {
      configurable: true,
      get: () => metrics.scrollHeight,
    },
    clientHeight: {
      configurable: true,
      get: () => metrics.clientHeight,
    },
    scrollTop: {
      configurable: true,
      get: () => metrics.scrollTop,
      set: (value: number) => {
        metrics.scrollTop = value;
      },
    },
  });
}

function expectFuzzyReveal(
  grapheme: HTMLElement,
  expectedDelay: number,
): void {
  expect(grapheme).toHaveAttribute("data-motion-initial-filter", "blur(3px)");
  expect(grapheme).toHaveAttribute("data-motion-initial-opacity", "0.45");
  expect(grapheme).toHaveAttribute("data-motion-animate-filter", "blur(0px)");
  expect(grapheme).toHaveAttribute("data-motion-animate-opacity", "1");
  expect(grapheme).toHaveAttribute("data-motion-duration", "0.24");
  expect(grapheme).toHaveAttribute(
    "data-motion-delay",
    String(expectedDelay),
  );
  expect(grapheme).not.toHaveAttribute("data-motion-initial-y");
  expect(grapheme).not.toHaveAttribute("data-motion-exit-y");
}

function expectImmediateReplacement(grapheme: HTMLElement): void {
  expect(grapheme).not.toHaveAttribute("data-motion-initial-filter");
  expect(grapheme).not.toHaveAttribute("data-motion-initial-opacity");
  expect(grapheme).not.toHaveAttribute("data-motion-animate-filter");
  expect(grapheme).not.toHaveAttribute("data-motion-animate-opacity");
  expect(grapheme).not.toHaveAttribute("data-motion-duration");
  expect(grapheme).not.toHaveAttribute("data-motion-delay");
}

async function flushPromises(): Promise<void> {
  await act(async () => {
    for (let index = 0; index < 12; index += 1) {
      await Promise.resolve();
    }
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(ms);
    for (let index = 0; index < 12; index += 1) {
      await Promise.resolve();
    }
  });
}

function installAnimationFrameQueue() {
  let nextId = 1;
  let timestamp = 0;
  const callbacks = new Map<number, FrameRequestCallback>();
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn((callback: FrameRequestCallback) => {
      const id = nextId;
      nextId += 1;
      callbacks.set(id, callback);
      return id;
    }),
  );
  vi.stubGlobal(
    "cancelAnimationFrame",
    vi.fn((id: number) => {
      callbacks.delete(id);
    }),
  );
  return {
    advance: async () => {
      timestamp += 16;
      const frameCallbacks = [...callbacks.values()];
      callbacks.clear();
      await act(async () => {
        frameCallbacks.forEach((callback) => callback(timestamp));
        await Promise.resolve();
      });
    },
  };
}

async function mountHud() {
  const view = render(<HUD />);
  await advance(300);
  mocks.windowCalls.length = 0;
  mocks.hudWindow.setSize.mockClear();
  mocks.hudWindow.setPosition.mockClear();
  mocks.hudWindow.show.mockClear();
  mocks.hudWindow.hide.mockClear();
  mocks.invoke.mockClear();
  return view;
}

const hudContractIt = it;

describe("HUD multisession presentation", () => {
  const originalScrollHeight = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "scrollHeight",
  );
  const originalOffsetWidth = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "offsetWidth",
  );
  const originalOffsetHeight = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "offsetHeight",
  );

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    mocks.handlers.clear();
    mocks.tauriEventHandlers.clear();
    mocks.tauriEventTrace.enabled = false;
    mocks.tauriEmit.mockClear();
    mocks.windowCalls.length = 0;
    mocks.glyphMotionRenderCounts.clear();
    mocks.getSnapGuide.mockReset();
    mocks.getSnapGuide.mockResolvedValue(null);
    for (const method of Object.values(mocks.snapGuide)) method.mockClear();
    mocks.state.atomicBoundsAvailable = true;
    mocks.state.showRegionAttempt = null;
    mocks.state.hideRegionAttempt = null;
    mocks.state.nativeHandoffAttempt = null;
    mocks.state.tauriVisibleCache = false;
    mocks.state.physicallyVisible = false;
    mocks.state.nativeRegionInstalled = false;
    mocks.invoke.mockClear();
    mocks.connected = true;
    mocks.send.mockReset();
    mocks.send.mockReturnValue(true);
    mocks.sendQuestionAnswers.mockClear();
    mocks.stopSpeech.mockClear();
    mocks.fetch.mockReset();
    mocks.fetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, active: false }),
    });
    mocks.dedicatedRecordingControlWindow = false;
    vi.stubGlobal("fetch", mocks.fetch);
    mocks.reducedMotion = false;
    mocks.naturalTextHeight = 0;
    mocks.claudeCardSize = { width: 0, height: 0 };
    mocks.currentMonitor.mockReset();
    mocks.currentMonitor.mockResolvedValue({
      scaleFactor: 1,
      position: { x: 0, y: 0 },
      size: { width: 1920, height: 1080 },
      workArea: {
        position: { x: 0, y: 0 },
        size: { width: 1920, height: 1080 },
      },
    });
    Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
      configurable: true,
      get() {
        return this.dataset.previewNaturalMeasure === "true"
          ? mocks.naturalTextHeight
          : 0;
      },
    });
    // Only the element the shared measure routine watches reports a footprint;
    // everything else keeps jsdom's zero, so no other test changes behavior.
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
      configurable: true,
      get() {
        return this.dataset.claudeCardMeasure !== undefined
          ? mocks.claudeCardSize.width
          : 0;
      },
    });
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
      configurable: true,
      get() {
        return this.dataset.claudeCardMeasure !== undefined
          ? mocks.claudeCardSize.height
          : 0;
      },
    });
    backendPendingCompletionIds = [];
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    mocks.handlers.clear();
    mocks.tauriEventHandlers.clear();
    vi.unstubAllGlobals();
    if (originalScrollHeight) {
      Object.defineProperty(
        HTMLElement.prototype,
        "scrollHeight",
        originalScrollHeight,
      );
    } else {
      delete (HTMLElement.prototype as { scrollHeight?: number }).scrollHeight;
    }
    if (originalOffsetWidth) {
      Object.defineProperty(
        HTMLElement.prototype,
        "offsetWidth",
        originalOffsetWidth,
      );
    } else {
      delete (HTMLElement.prototype as { offsetWidth?: number }).offsetWidth;
    }
    if (originalOffsetHeight) {
      Object.defineProperty(
        HTMLElement.prototype,
        "offsetHeight",
        originalOffsetHeight,
      );
    } else {
      delete (HTMLElement.prototype as { offsetHeight?: number }).offsetHeight;
    }
    vi.useRealTimers();
  });

  it("holds a frozen response stable across clocks and live response snapshots", async () => {
    localStorage.setItem(HUD_FORCE_KEY, "response_expanded");
    await mountHud();

    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Refactored the config loader and the tests still pass.",
      ),
    ).toBeInTheDocument();

    await advance(60_000);
    emit(responseSnapshot(response("live-response", 99), 99));
    await flushPromises();

    expect(
      screen.getByText(
        "Refactored the config loader and the tests still pass.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Reply live-response")).toBeNull();
  });

  it("renders an exact-route fixture without mutating the cross-window fixture channel", async () => {
    document.documentElement.setAttribute("data-theme", "dark");
    localStorage.setItem("ws-theme", "light");
    const view = render(<HUD uiQaFixtureId="reply_draft_expanded" />);
    await advance(300);

    expect(localStorage.getItem(HUD_FORCE_KEY)).toBeNull();
    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Let's go with staging first, then production if the café check passes.",
      ),
    ).toBeInTheDocument();
    const root = screen.getByTestId("hud-root");
    expect(root).toHaveAttribute(
      "data-ui-qa-state",
      "hud.response-expanded-reply",
    );
    expect(root).toHaveAttribute("data-ui-qa-ready", "true");
    expect(root).toHaveAttribute("data-hud-palette", "desktop-dark");

    view.unmount();
    document.documentElement.setAttribute("data-theme", "light");
  });

  it.each([
    ["dictation_recording_builtin", "hud.dictation-recording-builtin", "mail"],
    ["dictation_processing_builtin", "hud.dictation-processing-builtin", "mail"],
    ["dictation_recording_letter", "hud.dictation-recording-letter", "letter"],
  ])(
    "paints the registered stamp fixture %s through the production reducer",
    async (fixtureId, qaStateId, expectedIcon) => {
      const view = render(<HUD uiQaFixtureId={fixtureId} />);
      await advance(300);

      const root = screen.getByTestId("hud-root");
      expect(root).toHaveAttribute("data-ui-qa-state", qaStateId);
      expect(root).toHaveAttribute("data-ui-qa-ready", "true");
      const stamp = screen.getByTestId("hud-profile-mark");
      expect(stamp).toHaveAttribute("data-profile-icon", expectedIcon);
      // The frozen fixture must not reach the cross-window channel, and no
      // Live Preview card belongs on the compact vessel.
      expect(localStorage.getItem(HUD_FORCE_KEY)).toBeNull();
      expect(screen.queryByTestId("live-preview-card")).toBeNull();

      view.unmount();
    },
  );

  it.each([
    ["enhance_timeout_local", "hud.enhance-timeout-local", "Local model"],
    ["enhance_timeout_anthropic", "hud.enhance-timeout-anthropic", "Anthropic"],
  ])(
    "paints the registered timeout fixture %s and holds it still",
    async (fixtureId, qaStateId, label) => {
      const view = render(<HUD uiQaFixtureId={fixtureId} />);
      await advance(300);

      expect(screen.getByTestId("hud-root")).toHaveAttribute(
        "data-ui-qa-state",
        qaStateId,
      );
      expect(screen.getByTestId("hud-enhance-timeout")).toBeInTheDocument();
      expect(screen.getByText(label)).toBeInTheDocument();
      expect(screen.getByText("timed out")).toBeInTheDocument();
      // No dictation is running, so no stamp shares the vessel.
      expect(screen.queryByTestId("hud-profile-mark")).toBeNull();

      // The real notice clears after 2,000ms; a frozen fixture must not, or a
      // reviewer and a screenshot both miss it.
      await advance(5_000);
      expect(screen.getByTestId("hud-enhance-timeout")).toBeInTheDocument();

      view.unmount();
    },
  );

  hudContractIt(
    "gives a selected frozen fixture priority over live dictation presentation",
    async () => {
      localStorage.setItem(HUD_FORCE_KEY, "response_expanded");
      await mountHud();

      emit(lifecycle(901, "recording"));
      emit(preview(901, 1, "Live dictation must not cover the fixture"));
      await flushPromises();

      expect(
        screen.getByText(
          "Refactored the config loader and the tests still pass.",
        ),
      ).toBeInTheDocument();
      expect(screen.queryByTestId("live-preview-card")).toBeNull();
    },
  );

  hudContractIt(
    "keeps frozen question choices from sending live answers",
    async () => {
      localStorage.setItem(HUD_FORCE_KEY, "question_single");
      await mountHud();
      mocks.send.mockClear();

      const staging = screen.getByRole("button", { name: /Staging/ });
      fireEvent.click(staging);

      expect(mocks.sendQuestionAnswers).not.toHaveBeenCalled();
    },
  );

  it("shows the resting seed in one fixed unshaped WebView", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");

    render(<HUD />);
    await advance(300);

    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Start dictation" }),
    ).toBeNull();
    expect(mocks.windowCalls.slice(-3)).toEqual([
      "size:132x52",
      "position:894,912",
      "borderless-show",
    ]);
    expect(mocks.invoke).toHaveBeenCalledWith(
      "set_hud_window_borderless_visibility",
      { show: true, hide: false },
    );
  });

  it.each([
    {
      theme: "light",
      surface: HUD_LIGHT_DESKTOP_SURFACE,
      elevation: HUD_LIGHT_DESKTOP_ELEVATION,
      controlHighlight: HUD_LIGHT_PERSISTENT_CONTROL_HIGHLIGHT,
      border: HUD_LIGHT_DESKTOP_BORDER,
      text: "#314657",
    },
    {
      theme: "dark",
      surface: HUD_DARK_DESKTOP_SURFACE,
      elevation: HUD_DARK_DESKTOP_ELEVATION,
      controlHighlight: HUD_DARK_PERSISTENT_CONTROL_HIGHLIGHT,
      border: HUD_DARK_DESKTOP_BORDER,
      text: "#E9F1F8",
    },
  ])(
    "uses the desktop-safe $theme HUD palette",
    async ({ theme, surface, elevation, controlHighlight, border, text }) => {
      document.documentElement.setAttribute("data-theme", theme);
      localStorage.setItem("ws-recording-control-visible", "true");

      render(<HUD />);
      await advance(300);

      const root = screen.getByTestId("hud-root");
      const vessel = screen.getByTestId("persistent-recording-vessel");
      expect(root).toHaveAttribute("data-hud-palette", `desktop-${theme}`);
      expect(root.style.getPropertyValue("--surface-overlay")).toBe(surface);
      expect(root.style.getPropertyValue("--color-slate")).toBe(text);
      expect(root.style.getPropertyValue("--hud-desktop-elevation")).toBe(
        elevation,
      );
      expect(vessel).toHaveStyle({
        background: "var(--surface-overlay)",
        border: `1px solid ${border}`,
        boxShadow: controlHighlight,
      });
    },
  );

  it("suppresses the seed until a live probe clears stale cached screen-reader state", async () => {
    let resolveProbe:
      | ((response: {
          ok: boolean;
          status: number;
          json: () => Promise<{ active: boolean }>;
        }) => void)
      | undefined;
    mocks.fetch.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveProbe = resolve;
        }),
    );
    mocks.connected = false;
    localStorage.setItem("ws-recording-control-visible", "true");
    localStorage.setItem("ws-screen-reader-detected", "false");

    render(<HUD />);
    await flushPromises();

    expect(screen.queryByTestId("recording-control-seed")).toBeNull();
    expect(mocks.windowCalls).not.toContain("show");

    await act(async () => {
      resolveProbe?.({
        ok: true,
        status: 200,
        json: async () => ({ active: false }),
      });
      await Promise.resolve();
    });
    await flushPromises();

    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
  });

  it("requests screen-reader status only while the recording control is visible", async () => {
    const screenReaderProbe = () =>
      mocks.fetch.mock.calls.filter(([input]) =>
        String(input).includes("/api/accessibility/screen-reader"),
      ).length;
    mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => ({
      ok: true,
      status: 200,
      json: async () =>
        String(input).includes("/api/accessibility/screen-reader")
          ? { active: false }
          : { ok: true },
    }));
    localStorage.setItem("ws-recording-control-visible", "false");
    await mountHud();
    await advance(10_000);
    expect(screenReaderProbe()).toBe(0);

    emitTauri("recording-control-visibility-changed", true);
    await flushPromises();
    expect(screenReaderProbe()).toBe(1);

    emitTauri("recording-control-visibility-changed", false);
    await flushPromises();
    await advance(10_000);
    expect(screenReaderProbe()).toBe(1);
  });

  it("follows the live cross-window visibility event without losing the stored contract", async () => {
    await mountHud();
    // The main window broadcasts its config's reading; with the control off
    // the HUD makes no request of its own.
    emitTauri("screen-reader-status-changed", false);
    await flushPromises();
    const parkedVessel = screen.getByTestId("persistent-recording-vessel");
    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();

    emitTauri("recording-control-visibility-changed", true);
    await flushPromises();
    expect(screen.getByTestId("persistent-recording-vessel")).toBe(
      parkedVessel,
    );
    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();

    mocks.windowCalls.length = 0;
    emitTauri("recording-control-visibility-changed", false);
    await flushPromises();
    expect(screen.getByTestId("persistent-recording-vessel")).toBe(
      parkedVessel,
    );
    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
    expect(mocks.windowCalls).toEqual(["borderless-hide"]);
  });

  it("parks a painted seed after borderless hide so re-enable has no collapse frame", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).toBeInTheDocument();
    const vessel = screen.getByTestId("persistent-recording-vessel");
    mocks.windowCalls.length = 0;

    emitTauri("recording-control-visibility-changed", false);
    await flushPromises();
    expect(screen.getByTestId("persistent-recording-vessel")).toBe(vessel);
    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Start dictation" }),
    ).toBeNull();
    expect(vessel).toHaveAttribute("data-motion-width", "36");
    expect(vessel).toHaveAttribute("data-parked", "true");
    expect(mocks.windowCalls).toEqual(["borderless-hide"]);

    mocks.windowCalls.length = 0;
    emitTauri("recording-control-visibility-changed", true);
    await flushPromises();

    expect(screen.getByTestId("persistent-recording-vessel")).toBe(vessel);
    expect(vessel).toHaveAttribute("data-parked", "false");
    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Start dictation" }),
    ).toBeNull();
    expect(
      screen.getByTestId("persistent-recording-vessel"),
    ).toHaveAttribute("data-motion-width", "36");
    expect(mocks.windowCalls).toEqual(["borderless-show"]);
  });

  it("expands around the canonical anchor and uses a collapse grace period", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    const root = screen.getByTestId("hud-root");

    fireEvent.pointerEnter(root);
    await flushPromises();
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).not.toHaveAttribute("title");
    // The shared 132x52 WebView is already laid out. Hover is DOM-only.
    expect(mocks.windowCalls).toEqual([]);

    fireEvent.pointerLeave(root);
    await advance(119);
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).toBeInTheDocument();
    fireEvent.pointerEnter(root);
    await advance(200);
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).toBeInTheDocument();

    fireEvent.pointerLeave(root);
    await advance(120);
    // The card leads the collapse inside the unchanged backing.
    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
    expect(mocks.windowCalls).toEqual([]);

    await advance(220);
    expect(mocks.windowCalls).toEqual([]);
  });

  it("animates the card without mutating the fixed native window", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    const root = screen.getByTestId("hud-root");

    fireEvent.pointerEnter(root);
    await flushPromises();
    expect(mocks.windowCalls).toEqual([]);
    expect(
      screen.getByTestId("recording-control-launcher"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("persistent-recording-vessel"),
    ).toHaveAttribute("data-motion-width", "80");

    mocks.windowCalls.length = 0;

    // Collapse remains a DOM-only state change.
    fireEvent.pointerLeave(root);
    await advance(RECORDING_CONTROL_COLLAPSE_MS);
    expect(screen.queryByTestId("recording-control-launcher")).toBeNull();
    expect(
      screen.getByTestId("persistent-recording-vessel"),
    ).toHaveAttribute("data-motion-width", "36");
    expect(mocks.windowCalls).toEqual([]);

    await advance(RECORDING_CONTROL_CARD_SETTLE_MS);
    expect(mocks.windowCalls).toEqual([]);
  });

  it("does nothing to the fixed borderless window on hover", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    const root = screen.getByTestId("hud-root");

    expect(mocks.invoke).not.toHaveBeenCalled();
    mocks.invoke.mockClear();
    mocks.hudWindow.setPosition.mockClear();
    mocks.hudWindow.setSize.mockClear();
    fireEvent.pointerEnter(root);
    await flushPromises();

    expect(mocks.invoke).not.toHaveBeenCalled();
    expect(mocks.hudWindow.setPosition).not.toHaveBeenCalled();
    expect(mocks.hudWindow.setSize).not.toHaveBeenCalled();
  });

  it("uses sequenced clean geometry with native borderless visibility", async () => {
    mocks.state.atomicBoundsAvailable = false;
    localStorage.setItem("ws-recording-control-visible", "true");
    render(<HUD />);
    await advance(300);
    const root = screen.getByTestId("hud-root");

    expect(mocks.windowCalls.slice(-3)).toEqual([
      "size:132x52",
      "position:894,912",
      "borderless-show",
    ]);

    mocks.windowCalls.length = 0;
    fireEvent.pointerEnter(root);
    await flushPromises();
    expect(mocks.windowCalls).toEqual([]);

    // Collapse also stays inside the unchanged viewport.
    mocks.windowCalls.length = 0;
    fireEvent.pointerLeave(root);
    await advance(RECORDING_CONTROL_COLLAPSE_MS);
    expect(mocks.windowCalls).toEqual([]);
    await advance(RECORDING_CONTROL_CARD_SETTLE_MS);
    expect(mocks.windowCalls).toEqual([]);
  });

  it("shortens the collapse settle window under reduced motion", async () => {
    mocks.reducedMotion = true;
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    const root = screen.getByTestId("hud-root");

    fireEvent.pointerEnter(root);
    await flushPromises();
    expect(
      screen.getByTestId("recording-control-launcher"),
    ).toBeInTheDocument();

    mocks.windowCalls.length = 0;
    fireEvent.pointerLeave(root);
    await advance(RECORDING_CONTROL_COLLAPSE_MS);
    expect(mocks.windowCalls).toEqual([]);
    await advance(RECORDING_CONTROL_CARD_SETTLE_REDUCED_MS);
    expect(mocks.windowCalls).toEqual([]);
  });

  it("keeps the launcher visible but disables start while disconnected", async () => {
    mocks.connected = false;
    localStorage.setItem("ws-recording-control-visible", "true");
    const view = await mountHud();

    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();
    const start = screen.getByRole("button", { name: "Start dictation" });
    expect(start).toBeDisabled();

    mocks.connected = true;
    view.rerender(<HUD />);
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).toBeEnabled();
  });

  it("posts start once and waits for authoritative HUD-origin recording state", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    const view = await mountHud();
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();

    const start = screen.getByRole("button", { name: "Start dictation" });
    fireEvent.click(start);
    fireEvent.click(start);
    await flushPromises();

    expect(
      mocks.fetch.mock.calls.filter(([input]) =>
        String(input).includes("/api/dictation/"),
      ),
    ).toHaveLength(1);
    expect(mocks.fetch).toHaveBeenCalledWith(
      "http://localhost:38420/api/dictation/start",
      {
        method: "POST",
        headers: { "X-Whisperstream-Client": "hud-v1" },
      },
    );
    // The click enters arming, so the launcher (and its microphone button) is
    // replaced by the non-committal vessel. Repeat activation is impossible
    // because the control is gone, not merely disabled.
    expect(
      screen.queryByRole("button", { name: "Start dictation" }),
    ).toBeNull();
    expect(screen.getByTestId("hud-arming-indicator")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Cancel dictation" }),
    ).toBeNull();

    mocks.connected = false;
    view.rerender(<HUD />);
    await flushPromises();
    mocks.connected = true;
    view.rerender(<HUD />);
    await flushPromises();
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).toBeEnabled();

    emit(
      lifecycle(101, "recording", {
        origin: "hud",
        preview_enabled: false,
      }),
    );
    await flushPromises();

    expect(
      screen.getByRole("button", { name: "Cancel dictation" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Finish and type" }),
    ).toBeEnabled();
    expect(screen.getByTestId("hud-recording-controls")).toBeInTheDocument();
  });

  it("expands to the active geometry on click, before any authoritative state", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();
    const vessel = screen.getByTestId("persistent-recording-vessel");
    mocks.windowCalls.length = 0;

    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flushPromises();

    // No dictation.state has arrived. The vessel is already at the active
    // width, and it got there with no native mutation at all: launcher and
    // arming share one window rect, so only the card width changed.
    expect(mocks.windowCalls).toEqual([]);
    expect(screen.getByTestId("hud-arming-indicator")).toBeInTheDocument();
    // Motion starts from the launcher width so the click reads as a continuous
    // expansion rather than a jump to the final size.
    const armingVessel = screen.getByTestId("hud-arming-indicator").parentElement
      ?.parentElement as HTMLElement;
    expect(armingVessel).toBe(vessel);
    expect(vessel).toHaveAttribute("data-motion-width", "112");
  });

  it("shows no recording affordances while arming", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();

    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flushPromises();

    const root = screen.getByTestId("hud-root");
    expect(screen.getByTestId("hud-arming-indicator")).toBeInTheDocument();
    // No VoiceBars (there is no audio yet) and no cancel/finish discs (there is
    // no session to discard or commit). The microphone glyph is the only mark.
    expect(screen.queryByTestId("hud-recording-controls")).toBeNull();
    expect(root.querySelectorAll("button")).toHaveLength(0);
    expect(root.querySelectorAll("svg")).toHaveLength(1);
  });

  it("crossfades arming to the recording controls without moving the window", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();

    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flushPromises();
    const vessel = screen.getByTestId("persistent-recording-vessel");
    mocks.windowCalls.length = 0;

    emit(
      lifecycle(140, "recording", {
        origin: "hud",
        preview_enabled: false,
      }),
    );
    await flushPromises();

    // Confirmation is a content swap only. Arming already occupies the active
    // width, so no further bounds/size/position mutation is issued.
    expect(mocks.windowCalls).toEqual([]);
    expect(screen.queryByTestId("hud-arming-indicator")).toBeNull();
    expect(screen.getByTestId("hud-recording-controls")).toBeInTheDocument();
    expect(screen.getByTestId("persistent-recording-vessel")).toBe(vessel);
    expect(
      screen.getByRole("button", { name: "Cancel dictation" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Finish and type" }),
    ).toBeEnabled();
  });

  it("returns arming to the resting seed when the start command is rejected", async () => {
    mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
      const isCommand = String(input).includes("/api/dictation/");
      return {
        ok: !isCommand,
        status: isCommand ? 409 : 200,
        text: async () => "recording_not_started",
        json: async () =>
          isCommand
            ? { ok: false, error: "recording_not_started" }
            : { active: false },
      };
    });
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();

    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flushPromises();
    expect(screen.queryByTestId("hud-arming-indicator")).toBeNull();

    fireEvent.pointerLeave(screen.getByTestId("hud-root"));
    await advance(RECORDING_CONTROL_COLLAPSE_MS + RECORDING_CONTROL_CARD_SETTLE_MS);
    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
  });

  // --- Fixed persistent-control geometry -------------------------------

  async function enterActiveRecording(): Promise<void> {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();
    emit(lifecycle(700, "recording", { origin: "hud", preview_enabled: false }));
    await flushPromises();
    mocks.windowCalls.length = 0;
  }

  it("uses a normal cursor and rejects dragging during HUD-origin dictation", async () => {
    await enterActiveRecording();
    const root = screen.getByTestId("hud-root");
    const setPointerCapture = vi.fn();
    Object.defineProperty(root, "setPointerCapture", {
      configurable: true,
      value: setPointerCapture,
    });
    mocks.hudWindow.outerPosition.mockClear();
    mocks.hudWindow.outerSize.mockClear();

    expect(root).toHaveClass("cursor-default");
    expect(root).not.toHaveClass("cursor-grab");
    fireEvent.pointerDown(root, {
      pointerId: 71,
      screenX: 60,
      screenY: 50,
    });
    await flushPromises();

    expect(setPointerCapture).not.toHaveBeenCalled();
    expect(mocks.hudWindow.outerPosition).not.toHaveBeenCalled();
    expect(mocks.hudWindow.outerSize).not.toHaveBeenCalled();
  });

  it("does not create the snap guide for a simple persistent-control press", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    mocks.getSnapGuide.mockResolvedValue(mocks.snapGuide);
    await mountHud();
    const root = screen.getByTestId("hud-root");
    Object.defineProperty(root, "setPointerCapture", {
      configurable: true,
      value: vi.fn(),
    });

    fireEvent.pointerDown(root, {
      pointerId: 72,
      screenX: 100,
      screenY: 80,
    });
    await flushPromises();
    fireEvent.pointerUp(root, {
      pointerId: 72,
      screenX: 100,
      screenY: 80,
    });
    await flushPromises();

    expect(mocks.getSnapGuide).not.toHaveBeenCalled();
    expect(mocks.snapGuide.setSize).not.toHaveBeenCalled();
    expect(mocks.snapGuide.setPosition).not.toHaveBeenCalled();
    expect(mocks.snapGuide.show).not.toHaveBeenCalled();
    expect(mocks.snapGuide.hide).not.toHaveBeenCalled();
  });

  it("creates and shows the snap guide only after crossing the drag threshold", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    mocks.getSnapGuide.mockResolvedValue(mocks.snapGuide);
    await mountHud();
    const root = screen.getByTestId("hud-root");
    Object.defineProperty(root, "setPointerCapture", {
      configurable: true,
      value: vi.fn(),
    });

    fireEvent.pointerDown(root, {
      pointerId: 73,
      screenX: 100,
      screenY: 80,
    });
    await flushPromises();
    fireEvent.pointerMove(root, {
      pointerId: 73,
      screenX: 103,
      screenY: 80,
    });
    await flushPromises();
    expect(mocks.getSnapGuide).not.toHaveBeenCalled();

    fireEvent.pointerMove(root, {
      pointerId: 73,
      screenX: 106,
      screenY: 80,
    });
    await flushPromises();
    expect(mocks.getSnapGuide).toHaveBeenCalledTimes(1);
    expect(mocks.snapGuide.setSize).toHaveBeenCalledWith({
      width: 1,
      height: 1080,
    });
    expect(mocks.snapGuide.setPosition).toHaveBeenCalledWith({
      x: 959.5,
      y: 0,
    });
    expect(mocks.snapGuide.show).toHaveBeenCalledTimes(1);

    fireEvent.pointerUp(root, {
      pointerId: 73,
      screenX: 106,
      screenY: 80,
    });
    await flushPromises();
    expect(mocks.snapGuide.hide).toHaveBeenCalledTimes(1);
  });

  it("keeps the pill rect anchored across every control geometry", () => {
    const anchors = [
      { x: 900, y: 920 },
      { x: 0, y: 0 },
      { x: 1788, y: 1044 },
      { x: -37, y: 13 },
    ];
    const expandedModes = ["launcher", "arming", "active"] as const;
    for (const anchor of anchors) {
      const launcher = pillRectFromAnchor("launcher", anchor);
      const active = pillRectFromAnchor("active", anchor);
      expect(pillRectFromAnchor("arming", anchor)).toEqual(active);

      // The vessel widens, but its bottom edge and center never move.
      for (const mode of expandedModes) {
        const rect = pillRectFromAnchor(mode, anchor);
        expect(rect.y + rect.height).toBe(launcher.y + launcher.height);
        expect(rect.x + rect.width / 2).toBe(
          launcher.x + launcher.width / 2,
        );
      }
    }
  });

  it("keeps persistent-control native regions rounded and inside the fixed WebView", () => {
    expect(hudWindowRegionForMode("seed")).toEqual({
      x: MODE_GEOMETRY.seed.padX - HUD_REGION_EDGE_BLEED,
      y: MODE_GEOMETRY.seed.padY - HUD_REGION_EDGE_BLEED,
      width: MODE_GEOMETRY.seed.cardW + HUD_REGION_EDGE_BLEED * 2,
      height: MODE_GEOMETRY.seed.cardH + HUD_REGION_EDGE_BLEED * 2,
      cornerRadius:
        (MODE_GEOMETRY.seed.cardH + HUD_REGION_EDGE_BLEED * 2) / 2,
    });
    const expandedRegion = {
      x: 2,
      y: 0,
      width: 128,
      height: 52,
      cornerRadius: 26,
    };
    expect(hudWindowRegionForMode("launcher")).toEqual(expandedRegion);
    expect(hudWindowRegionForMode("arming")).toEqual(expandedRegion);
    expect(hudWindowRegionForMode("active")).toEqual(expandedRegion);
    expect(hudWindowRegionForMode("pill")).toBeNull();
  });

  it("never passes permanent-control geometry to native region or bounds commands", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    mocks.invoke.mockClear();

    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();

    expect(
      mocks.invoke.mock.calls.some(
        ([command]) =>
          command === "set_hud_window_region" ||
          command === "set_hud_bounds",
      ),
    ).toBe(false);
  });

  it("keeps the microphone accessible name without a visual hover tooltip", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();
    mocks.windowCalls.length = 0;

    fireEvent.pointerEnter(
      screen.getByRole("button", { name: "Start dictation" }),
    );
    expect(screen.queryByTestId("hud-tip")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).toHaveAttribute("aria-label", "Start dictation");
    // Hovering the microphone remains a pure render interaction with no
    // tooltip rectangle and no native window mutation.
    expect(mocks.windowCalls).toEqual([]);
  });

  it("keeps outcome control names and performs no window mutation on hover", async () => {
    await enterActiveRecording();
    const cancel = screen.getByRole("button", { name: "Cancel dictation" });
    const finish = screen.getByRole("button", { name: "Finish and type" });

    fireEvent.pointerEnter(cancel);
    expect(mocks.windowCalls).toEqual([]);
    fireEvent.pointerLeave(cancel);

    fireEvent.pointerEnter(finish);
    expect(mocks.windowCalls).toEqual([]);
    fireEvent.pointerLeave(finish);

    expect(cancel).toHaveAttribute("aria-label", "Cancel dictation");
    expect(finish).toHaveAttribute("aria-label", "Finish and type");
  });

  it("performs no native mutation across launcher, arming, and active", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();
    expect(mocks.windowCalls).toEqual([]);
    mocks.windowCalls.length = 0;

    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flushPromises();
    expect(screen.getByTestId("hud-arming-indicator")).toBeInTheDocument();

    emit(
      lifecycle(760, "recording", { origin: "hud", preview_enabled: false }),
    );
    await flushPromises();
    expect(screen.getByTestId("hud-recording-controls")).toBeInTheDocument();

    expect(
      mocks.windowCalls.filter((call) => call.startsWith("bounds:")),
    ).toEqual([]);
  });

  it("keeps the root continuously painted across hover animation frames", async () => {
    const frames = installAnimationFrameQueue();
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    const root = screen.getByTestId("hud-root");
    expect(root.style.opacity).toBe("");

    // The WebView and its presentation owner remain unchanged.
    fireEvent.pointerEnter(root);
    await flushPromises();
    expect(mocks.windowCalls).toEqual([]);
    expect(root.style.opacity).toBe("");

    await frames.advance();
    await frames.advance();
    expect(root.style.opacity).toBe("");
  });

  it("does not blank the root for a position-only control mutation", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    const root = screen.getByTestId("hud-root");

    fireEvent.pointerEnter(root);
    await flushPromises();
    expect(root.style.opacity).toBe("");
    mocks.windowCalls.length = 0;

    // Same 132x52 window, moved: the WebView layout is still valid, so a blank
    // would be a visible cost with nothing to buy.
    localStorage.setItem("hud-position", JSON.stringify({ x: 700, y: 920 }));
    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "size:132x52",
      "position:694,912",
      "borderless-show",
    ]);
    expect(root.style.opacity).toBe("");
  });

  it("keeps no native title attribute on HUD controls", async () => {
    await enterActiveRecording();
    for (const name of ["Cancel dictation", "Finish and type"]) {
      expect(screen.getByRole("button", { name })).not.toHaveAttribute("title");
    }

    emit(lifecycle(700, "idle", { origin: "hud", preview_enabled: false }));
    await advance(RECORDING_CONTROL_COLLAPSE_MS);
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).not.toHaveAttribute("title");
  });

  it("still labels a disabled launcher control with the same locked string", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();
    mocks.connected = false;
    fireEvent.pointerEnter(
      screen.getByRole("button", { name: "Start dictation" }),
    );
    expect(screen.queryByTestId("hud-tip")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).toHaveAttribute("aria-label", "Start dictation");
  });

  it("re-enables start after an explicit command rejection", async () => {
    mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
      const isCommand = String(input).includes("/api/dictation/");
      return {
        ok: !isCommand,
        status: isCommand ? 409 : 200,
        text: async () => "recording_not_started",
        json: async () =>
          isCommand
            ? { ok: false, error: "recording_not_started" }
            : { active: false },
      };
    });
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();

    const start = screen.getByRole("button", { name: "Start dictation" });
    fireEvent.click(start);
    await flushPromises();

    expect(start).toBeEnabled();
    expect(screen.getByTestId("recording-control-launcher")).toBeInTheDocument();
  });

  it("releases command pending when an authoritative lifecycle is missed", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flushPromises();

    const start = screen.getByRole("button", { name: "Start dictation" });
    fireEvent.click(start);
    await flushPromises();
    expect(screen.getByTestId("hud-arming-indicator")).toBeInTheDocument();

    await advance(4999);
    expect(screen.getByTestId("hud-arming-indicator")).toBeInTheDocument();
    await advance(1);
    // The bounded missed-event timeout owns arming's release too, so a backend
    // that never confirms returns the vessel to the launcher instead of
    // stranding it at the active width.
    expect(screen.queryByTestId("hud-arming-indicator")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).toBeEnabled();
  });

  it("keeps cancel and finish pending until authoritative lifecycle advances", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    emit(
      lifecycle(110, "recording", {
        origin: "hud",
        preview_enabled: false,
      }),
    );
    await flushPromises();

    const cancel = screen.getByRole("button", { name: "Cancel dictation" });
    const finish = screen.getByRole("button", { name: "Finish and type" });
    fireEvent.click(cancel);
    await flushPromises();
    expect(mocks.fetch).toHaveBeenLastCalledWith(
      "http://localhost:38420/api/dictation/cancel",
      {
        method: "POST",
        headers: { "X-Whisperstream-Client": "hud-v1" },
      },
    );
    expect(cancel).toBeDisabled();
    expect(finish).toBeDisabled();

    emit(lifecycle(110, "idle", { origin: "hud" }));
    await flushPromises();
    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();

    emit(
      lifecycle(111, "recording", {
        origin: "hud",
        preview_enabled: false,
      }),
    );
    await flushPromises();
    fireEvent.click(
      screen.getByRole("button", { name: "Finish and type" }),
    );
    await flushPromises();
    expect(mocks.fetch).toHaveBeenLastCalledWith(
      "http://localhost:38420/api/dictation/complete",
      {
        method: "POST",
        headers: { "X-Whisperstream-Client": "hud-v1" },
      },
    );
    expect(
      screen.getByRole("button", { name: "Cancel dictation" }),
    ).toBeDisabled();

    emit(lifecycle(111, "processing", { origin: "hud" }));
    await flushPromises();
    expect(screen.getByTestId("dictation-processing-spinner")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Finish and type" }),
    ).toBeNull();
  });

  it("returns cancel through one active-to-seed spring without native clipping", async () => {
    await enterActiveRecording();
    fireEvent.click(
      screen.getByRole("button", { name: "Cancel dictation" }),
    );
    emit(lifecycle(700, "idle", { origin: "hud" }));
    await flushPromises();

    const vessel = screen.getByTestId("persistent-recording-vessel");
    expect(vessel).toHaveAttribute("data-returning-from-active", "true");
    expect(vessel).toHaveAttribute("data-motion-width", "36");
    expect(mocks.windowCalls).not.toContain("region:40,26,52x26");

    await advance(HUD_OUTCOME_COLLAPSE_SETTLE_MS - 1);
    expect(mocks.windowCalls).not.toContain("region:40,26,52x26");
    await advance(1);
    expect(mocks.windowCalls).toEqual([]);
  });

  it("holds finish processing in the active vessel before one smooth collapse", async () => {
    await enterActiveRecording();
    fireEvent.click(
      screen.getByRole("button", { name: "Finish and type" }),
    );
    emit(
      lifecycle(700, "processing", {
        origin: "hud",
        preview_enabled: false,
      }),
    );
    await flushPromises();

    expect(
      screen.getByTestId("dictation-processing-spinner"),
    ).toBeInTheDocument();
    expect(mocks.windowCalls).toEqual([]);

    emit(
      lifecycle(700, "idle", {
        origin: "hud",
        preview_enabled: false,
      }),
    );
    await flushPromises();
    await advance(HUD_FINISH_PROCESSING_MIN_MS - 1);
    expect(
      screen.getByTestId("dictation-processing-spinner"),
    ).toBeInTheDocument();
    expect(mocks.windowCalls).toEqual([]);

    await advance(1);
    expect(screen.queryByTestId("dictation-processing-spinner")).toBeNull();
    expect(
      screen.getByTestId("persistent-recording-vessel"),
    ).toHaveAttribute("data-returning-from-active", "true");

    await advance(HUD_OUTCOME_COLLAPSE_SETTLE_MS);
    expect(mocks.windowCalls).toEqual([]);
    expect(
      mocks.windowCalls.some((call) => call.startsWith("bounds:")),
    ).toBe(false);
  });

  it("adds outcome controls only to authoritative HUD-origin recordings", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();

    emit(
      lifecycle(120, "recording", {
        origin: "toggle",
        preview_enabled: false,
      }),
    );
    await flushPromises();
    expect(
      screen.queryByRole("button", { name: "Cancel dictation" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Finish and type" }),
    ).toBeNull();

    emit(
      lifecycle(121, "recording", {
        origin: "hud",
        preview_enabled: false,
      }),
    );
    await flushPromises();
    expect(
      screen.getByRole("button", { name: "Cancel dictation" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Finish and type" }),
    ).toBeInTheDocument();
  });

  it("separates Live Preview view controls from the bottom outcome rail", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();

    emit(lifecycle(130, "recording", { origin: "hud" }));
    await flushPromises();
    expect(screen.getByTestId("live-preview-card")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Expand preview" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("preview-text")).toBeEmptyDOMElement();

    emit(preview(130, 1, "Pointer-started preview"));
    await flushPromises();

    const controls = screen.getByTestId("preview-recording-controls");
    const viewControls = screen.getByTestId("preview-view-controls");
    const card = screen.getByTestId("live-preview-card");
    const cancel = screen.getByRole("button", { name: "Cancel dictation" });
    const expand = screen.getByRole("button", { name: "Expand preview" });
    const finish = screen.getByRole("button", { name: "Finish and type" });
    expect(
      cancel,
    ).toBeInTheDocument();
    expect(expand).toBeInTheDocument();
    expect(finish).toBeInTheDocument();
    expect(controls.querySelectorAll("button")).toHaveLength(2);
    expect(viewControls).toContainElement(expand);
    expect(controls).toContainElement(cancel);
    expect(controls).toContainElement(finish);
    expect(card.querySelectorAll("button")).toHaveLength(3);
    expect(cancel).toHaveStyle({ width: "24px", height: "24px" });
    expect(finish).toHaveStyle({ width: "24px", height: "24px" });

    fireEvent.click(
      screen.getByRole("button", { name: "Finish and type" }),
    );
    await flushPromises();
    expect(
      screen.getByRole("button", { name: "Cancel dictation" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Finish and type" }),
    ).toBeDisabled();

    emit(lifecycle(130, "processing", { origin: "hud" }));
    await flushPromises();
    expect(screen.getByTestId("dictation-processing-spinner")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Cancel dictation" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Finish and type" }),
    ).toBeNull();
  });

  it("lets screen-reader suppression win over the seed", async () => {
    const screenReaderProbe = () =>
      mocks.fetch.mock.calls.filter(([input]) =>
        String(input).includes("/api/accessibility/screen-reader"),
      ).length;
    mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => ({
      ok: true,
      status: 200,
      json: async () =>
        String(input).includes("/api/accessibility/screen-reader")
          ? { active: false }
          : { ok: true },
    }));
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();

    const probesBeforePush = screenReaderProbe();
    emit({ type: "accessibility.screen_reader", active: true });
    await flushPromises();
    expect(screen.queryByTestId("recording-control-seed")).toBeNull();
    await advance(300);
    expect(mocks.windowCalls.slice(-1)).toEqual(["borderless-hide"]);
    expect(
      mocks.windowCalls.some((call) => call === "region:40,26,52x26"),
    ).toBe(false);

    emit({ type: "accessibility.screen_reader", active: false });
    await flushPromises();
    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
    // Pushes replace the old 2-second poll.
    await advance(10_000);
    expect(screenReaderProbe()).toBe(probesBeforePush);
  });

  it("uses near-instant persistent-control geometry under reduced motion", async () => {
    mocks.reducedMotion = true;
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();

    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    expect(screen.getByTestId("persistent-recording-vessel")).toHaveAttribute(
      "data-motion-duration",
      "0.01",
    );
  });


  it.each(["light", "dark"] as const)(
    "keeps the resting compact pill on the desktop-safe %s material with no body clamp",
    (theme) => {
      // Round-05 (hud-presentation) retired the fixed PANEL_CARD_W/H response
      // card and its 4-line clamp (design.md decisions 19, 20): the compact
      // pill is identity-only at a content-measured width, and the expanded
      // card takes natural height with no clamp at all. Coverage for the
      // fixed geometry and clamp themselves moved to
      // HUD.interactions.test.tsx ("shows the resting compact pill with no
      // response body text", "renders the full expanded response with no
      // ReadingWell and no clamp"); this test keeps only what still applies
      // to this file's full-HUD, theme-driven harness: the pill's material.
      document.documentElement.setAttribute("data-theme", theme);
      const completion = response("geometry", 1);
      render(
        <ResponsePanel
          completion={completion}
          remainingCount={0}
          truncated
          replyState="idle"
          rms={0}
          peak={0}
          speaking={false}
        />,
      );

      const panel = screen.getByRole("status");
      expect(panel).toHaveStyle({ background: "var(--surface-overlay)" });
      expect(screen.queryByText("Reply geometry", { exact: false })).not.toBeInTheDocument();
    },
  );

  it("removes expanded-card content movement when reduced motion is preferred", () => {
    // Re-homed from "removes queued-card movement...": the old compact quote
    // body no longer exists to crossfade between queued completions (decision
    // 19), so this now covers the surviving y-motion in the new presentation:
    // the expanded card's content enters at y:6 -> 0, and reduced motion
    // drops the y term entirely (design decision 29).
    const completion = response("motion", 1);
    const { unmount } = render(
      <ResponsePanel
        completion={completion}
        remainingCount={0}
        truncated={false}
        replyState="idle"
        rms={0}
        peak={0}
        speaking={false}
        expanded
      />,
    );

    const content = screen.getByTestId("claude-expanded-card").firstElementChild;
    expect(content).toHaveAttribute("data-motion-initial-y", "6");

    unmount();
    mocks.reducedMotion = true;
    render(
      <ResponsePanel
        completion={completion}
        remainingCount={0}
        truncated={false}
        replyState="idle"
        rms={0}
        peak={0}
        speaking={false}
        expanded
      />,
    );

    const reducedContent = screen.getByTestId("claude-expanded-card").firstElementChild;
    expect(reducedContent).not.toHaveAttribute("data-motion-initial-y");
  });

  it("ignores a show_text false completion upsert", async () => {
    await mountHud();

    emit(upsert(response("hidden-v1", 1), false));
    await flushPromises();

    expect(screen.queryByText("Reply hidden-v1")).toBeNull();
    expect(mocks.windowCalls).toEqual([]);

    emit(upsert(response("visible", 2)));
    await flushPromises();
    await advance(3_000);
    await advance(3_000);

    expect(screen.queryByText("Reply visible")).toBeNull();
  });

  it("orders recording-to-seed geometry through the single window reconciler", async () => {
    await mountHud();

    emit({ type: "state.recording", recording: true });
    await flushPromises();
    expect(mocks.windowCalls).toEqual([
      "size:160x80",
      "position:880,898",
      "show",
    ]);

    mocks.windowCalls.length = 0;
    emit({ type: "state.recording", recording: false });
    emit({ type: "state.transcribing", active: false });
    await flushPromises();
    expect(mocks.windowCalls).toEqual([
      "hide",
      "size:132x52",
      "position:894,912",
    ]);
  });

  it("reuses the processing spinner only when microphone arming is delayed", async () => {
    await mountHud();

    emit({
      type: "state.arming",
      active: true,
      hud_enabled: true,
      screen_reader_detected: false,
    });
    await advance(149);
    expect(screen.queryByTestId("dictation-processing-spinner")).toBeNull();

    await advance(1);
    expect(
      screen.getByTestId("dictation-processing-spinner"),
    ).toBeInTheDocument();

    emit({ type: "state.recording", recording: true });
    await flushPromises();
    expect(screen.queryByTestId("dictation-processing-spinner")).toBeNull();
  });

  it("shows no arming spinner for a reply capture into the agent surface", async () => {
    await mountHud();

    emit({
      type: "state.arming",
      active: true,
      hud_enabled: true,
      screen_reader_detected: false,
      agent_compose: true,
    });
    await advance(400);

    expect(screen.queryByTestId("dictation-processing-spinner")).toBeNull();
  });

  it("cancels the delayed arming spinner when the microphone opens quickly", async () => {
    await mountHud();

    emit({
      type: "state.arming",
      active: true,
      hud_enabled: true,
      screen_reader_detected: false,
    });
    await advance(75);
    emit({ type: "state.arming", active: false });
    await advance(150);

    expect(screen.queryByTestId("dictation-processing-spinner")).toBeNull();
  });

  it("hands the permanent seed to the known-good ordinary recording lifecycle", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    expect(mocks.state.nativeRegionInstalled).toBe(false);
    const root = screen.getByTestId("hud-root");
    expect(root).not.toHaveStyle({ visibility: "hidden" });

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    emit(lifecycle(70, "recording", { preview_enabled: false }));
    expect(root).toHaveStyle({ visibility: "hidden" });
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "borderless-hide",
      "hide",
      "size:160x80",
      "position:880,898",
      "show",
    ]);
    expect(root).not.toHaveStyle({ visibility: "hidden" });
    expect(mocks.invoke.mock.calls.map(([command]) => command)).toEqual([
      "set_hud_window_borderless_visibility",
    ]);
    expect(mocks.state.nativeRegionInstalled).toBe(false);
    expect(mocks.state.tauriVisibleCache).toBe(true);
    expect(mocks.state.physicallyVisible).toBe(true);

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    emit(lifecycle(70, "processing", { preview_enabled: false }));
    await flushPromises();
    expect(mocks.windowCalls).toEqual([]);
  });

  it("prepares Preview while a visible permanent seed cannot paint its compact bounds", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    const root = screen.getByTestId("hud-root");
    expect(root).not.toHaveStyle({ visibility: "hidden" });

    mocks.windowCalls.length = 0;
    emit(lifecycle(71, "recording", { preview_enabled: true }));
    expect(root).toHaveStyle({ visibility: "hidden" });
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "borderless-hide",
      "hide",
      "size:360x192",
      "position:780,780",
      "show",
    ]);
    expect(root).not.toHaveStyle({ visibility: "hidden" });
    expect(mocks.state.tauriVisibleCache).toBe(true);
    expect(mocks.state.physicallyVisible).toBe(true);
  });

  it("parks the cold hidden main HUD at arming geometry before handback", async () => {
    mocks.dedicatedRecordingControlWindow = true;
    mocks.tauriEventTrace.enabled = true;
    localStorage.setItem("ws-recording-control-visible", "true");

    render(<HUD />);
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "hide",
      "size:132x52",
      "position:894,912",
      "event:hud-main-visibility-changed:false",
    ]);
    expect(mocks.invoke).not.toHaveBeenCalled();
    expect(mocks.state.physicallyVisible).toBe(false);
  });

  it("keeps dedicated-control Preview on the clean HUD bounds through its final hide", async () => {
    mocks.dedicatedRecordingControlWindow = true;
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();

    expect(screen.queryByTestId("persistent-recording-vessel")).toBeNull();
    emit(
      lifecycle(74, "recording", {
        origin: "hud",
        preview_enabled: true,
      }),
    );
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "dedicated-control-hide",
      "size:360x192",
      "position:780,780",
      "show",
    ]);
    expect(mocks.invoke.mock.calls.map(([command]) => command)).toEqual([
      "hide_recording_control_window",
    ]);

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    mocks.tauriEventTrace.enabled = true;
    emit(
      lifecycle(74, "idle", {
        origin: "hud",
        preview_enabled: true,
      }),
    );
    await flushPromises();
    await advance(299);
    expect(mocks.windowCalls).toEqual([]);

    await advance(1);
    expect(mocks.windowCalls).toEqual([
      "hide",
      "size:132x52",
      "position:894,912",
      "event:hud-main-visibility-changed:false",
    ]);
    expect(mocks.invoke).not.toHaveBeenCalled();

    mocks.windowCalls.length = 0;
    mocks.tauriEventTrace.enabled = false;
    emitTauri("recording-control-start-pending", true);
    await flushPromises();
    expect(mocks.windowCalls).toEqual(["dedicated-control-hide", "show"]);
    expect(mocks.hudWindow.setSize).toHaveBeenCalledTimes(2);
    expect(mocks.hudWindow.setPosition).toHaveBeenCalledTimes(2);
  });

  it("clears a shaped owner before parking the hidden main HUD", async () => {
    mocks.dedicatedRecordingControlWindow = true;
    // The permission pill keeps the shaped region owner this test hands off.
    mocks.claudeCardSize = {
      width: CLAUDE_PERMISSION_PILL_W,
      height: CLAUDE_PERMISSION_MIN_H,
    };
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();

    emit(pendingPermission(1));
    await flushPromises();
    expect(mocks.state.nativeRegionInstalled).toBe(true);
    expect(mocks.state.physicallyVisible).toBe(true);

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    mocks.tauriEventTrace.enabled = true;
    emit({ type: "agent.interactions.snapshot", version: 1, revision: ++permissionSnapshotRevision, interactions: [] });
    await flushPromises();
    await advance(299);
    expect(mocks.windowCalls).toEqual([]);

    await advance(1);
    expect(mocks.windowCalls).toEqual([
      "native-hide+clear",
      "hide",
      "size:132x52",
      "position:894,912",
      "event:hud-main-visibility-changed:false",
    ]);
    expect(mocks.invoke.mock.calls.map(([command]) => command)).toEqual([
      "hide_and_clear_hud_window_region",
    ]);
    expect(mocks.state.nativeRegionInstalled).toBe(false);
    expect(mocks.state.physicallyVisible).toBe(false);
  });

  it("keeps dedicated-control HUD-origin recording on the main clean owner", async () => {
    mocks.dedicatedRecordingControlWindow = true;
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();

    emit(
      lifecycle(75, "recording", {
        origin: "hud",
        preview_enabled: false,
      }),
    );
    await flushPromises();

    expect(screen.getByTestId("hud-recording-controls")).toBeInTheDocument();
    expect(mocks.windowCalls).toEqual(["dedicated-control-hide", "show"]);
    expect(mocks.invoke.mock.calls.map(([command]) => command)).toEqual([
      "hide_recording_control_window",
    ]);

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    emit(
      lifecycle(75, "idle", {
        origin: "hud",
        preview_enabled: false,
      }),
    );
    await flushPromises();
    await advance(299);
    expect(mocks.windowCalls).toEqual([]);

    await advance(1);
    expect(mocks.windowCalls).toEqual(["hide"]);
    expect(mocks.invoke).not.toHaveBeenCalled();
  });

  it("arms the clean main HUD immediately from the dedicated pending event", async () => {
    mocks.dedicatedRecordingControlWindow = true;
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();

    emitTauri("recording-control-start-pending", true);
    await flushPromises();

    expect(screen.getByTestId("hud-arming-indicator")).toBeInTheDocument();
    expect(screen.queryByTestId("recording-control-seed")).toBeNull();
    expect(screen.getByTestId("persistent-recording-vessel")).toHaveAttribute(
      "data-motion-width",
      "112",
    );
    const armingGlyph = screen.getByTestId("hud-arming-glyph");
    expect(armingGlyph).toHaveAttribute("data-motion-initial-opacity", "1");
    expect(armingGlyph).toHaveAttribute("data-motion-initial-scale", "1");
    expect(armingGlyph).toHaveAttribute("data-motion-animate-opacity", "0");
    expect(armingGlyph).toHaveAttribute("data-motion-animate-scale", "0.86");
    expect(armingGlyph).toHaveAttribute("data-motion-duration", "0.1");
    expect(mocks.windowCalls).toEqual(["dedicated-control-hide", "show"]);
    expect(mocks.invoke.mock.calls.map(([command]) => command)).toEqual([
      "hide_recording_control_window",
    ]);

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    mocks.hudWindow.setSize.mockClear();
    mocks.hudWindow.setPosition.mockClear();
    emit(
      lifecycle(76, "recording", {
        origin: "hud",
        preview_enabled: false,
      }),
    );
    await flushPromises();

    expect(screen.queryByTestId("hud-arming-indicator")).toBeNull();
    expect(screen.getByTestId("hud-recording-controls")).toBeInTheDocument();
    expect(mocks.windowCalls).toEqual([]);
    expect(mocks.hudWindow.setSize).not.toHaveBeenCalled();
    expect(mocks.hudWindow.setPosition).not.toHaveBeenCalled();
    expect(mocks.invoke).not.toHaveBeenCalled();
  });

  it("hides dedicated arming and hands visibility back on pending false", async () => {
    mocks.dedicatedRecordingControlWindow = true;
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();

    emitTauri("recording-control-start-pending", true);
    await flushPromises();
    expect(screen.getByTestId("hud-arming-indicator")).toBeInTheDocument();

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    mocks.tauriEmit.mockClear();
    emitTauri("recording-control-start-pending", false);
    await flushPromises();
    await advance(299);
    expect(mocks.windowCalls).toEqual([]);

    await advance(1);
    expect(screen.queryByTestId("hud-arming-indicator")).toBeNull();
    expect(mocks.windowCalls).toEqual(["hide"]);
    expect(mocks.invoke).not.toHaveBeenCalled();
    expect(mocks.tauriEmit).toHaveBeenCalledWith(
      "hud-main-visibility-changed",
      false,
    );
  });

  it("makes the dedicated arming glyph exit near-immediately under reduced motion", async () => {
    mocks.dedicatedRecordingControlWindow = true;
    mocks.reducedMotion = true;
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();

    emitTauri("recording-control-start-pending", true);
    await flushPromises();

    const armingGlyph = screen.getByTestId("hud-arming-glyph");
    expect(armingGlyph).toHaveAttribute("data-motion-initial-opacity", "1");
    expect(armingGlyph).toHaveAttribute("data-motion-animate-opacity", "0");
    expect(armingGlyph).toHaveAttribute("data-motion-duration", "0.01");
  });

  it("fails closed for stale dedicated pending events", async () => {
    mocks.dedicatedRecordingControlWindow = true;
    localStorage.setItem("ws-recording-control-visible", "true");
    localStorage.setItem("ws-screen-reader-detected", "true");
    mocks.fetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, active: true }),
    });
    await mountHud();

    emitTauri("recording-control-start-pending", true);
    await flushPromises();

    expect(screen.queryByTestId("hud-arming-indicator")).toBeNull();
    expect(mocks.windowCalls).toEqual([]);
    expect(mocks.invoke).not.toHaveBeenCalled();
  });

  it("hides ordinary recording before restoring the permanent borderless owner", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    const root = screen.getByTestId("hud-root");

    emit(lifecycle(72, "recording", { preview_enabled: false }));
    await flushPromises();
    expect(mocks.state.tauriVisibleCache).toBe(true);

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    emit(lifecycle(72, "idle", { preview_enabled: false }));
    expect(root).toHaveStyle({ visibility: "hidden" });
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "hide",
      "size:132x52",
      "position:894,912",
      "borderless-show",
    ]);
    expect(mocks.invoke.mock.calls.map(([command]) => command)).toEqual([
      "set_hud_window_borderless_visibility",
    ]);
    expect(root).not.toHaveStyle({ visibility: "hidden" });
    expect(mocks.state.nativeRegionInstalled).toBe(false);
    expect(mocks.state.tauriVisibleCache).toBe(false);
    expect(mocks.state.physicallyVisible).toBe(true);
  });

  it("hides Preview before restoring the permanent borderless owner", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    await mountHud();
    const root = screen.getByTestId("hud-root");

    emit(lifecycle(73, "recording", { preview_enabled: true }));
    await flushPromises();
    expect(mocks.state.tauriVisibleCache).toBe(true);

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    emit(lifecycle(73, "idle", { preview_enabled: true }));
    expect(root).toHaveStyle({ visibility: "hidden" });
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "hide",
      "size:132x52",
      "position:894,912",
      "borderless-show",
    ]);
    expect(mocks.invoke.mock.calls.map(([command]) => command)).toEqual([
      "set_hud_window_borderless_visibility",
    ]);
    expect(root).not.toHaveStyle({ visibility: "hidden" });
    expect(mocks.state.nativeRegionInstalled).toBe(false);
    expect(mocks.state.tauriVisibleCache).toBe(false);
    expect(mocks.state.physicallyVisible).toBe(true);
  });

  it("repairs a false tao visibility cache exactly once when a visible shaped surface yields to recording", async () => {
    // The permission pill keeps the shaped region owner this test hands off.
    mocks.claudeCardSize = {
      width: CLAUDE_PERMISSION_PILL_W,
      height: CLAUDE_PERMISSION_MIN_H,
    };
    await mountHud();

    emit(pendingPermission(1));
    await flushPromises();
    expect(mocks.state.nativeRegionInstalled).toBe(true);
    expect(mocks.state.physicallyVisible).toBe(true);
    expect(mocks.state.tauriVisibleCache).toBe(false);

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    emit(lifecycle(70, "recording", { preview_enabled: false }));
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "native-hide+clear",
      "hide",
      "size:160x80",
      "position:880,898",
      "show",
    ]);
    expect(mocks.invoke.mock.calls.map(([command]) => command)).toEqual([
      "hide_and_clear_hud_window_region",
    ]);
    expect(mocks.state.nativeRegionInstalled).toBe(false);
    expect(mocks.state.tauriVisibleCache).toBe(true);
    expect(mocks.state.physicallyVisible).toBe(true);

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    emit(lifecycle(70, "processing", { preview_enabled: false }));
    await flushPromises();
    expect(mocks.windowCalls).toEqual([]);
    expect(mocks.invoke).not.toHaveBeenCalled();

    emit(lifecycle(70, "idle", { preview_enabled: false }));
    await flushPromises();
    expect(mocks.invoke.mock.calls.map(([command]) => command)).toEqual([
      "set_hud_window_region",
      "set_hud_bounds",
    ]);
    expect(mocks.state.nativeRegionInstalled).toBe(true);
    expect(mocks.state.physicallyVisible).toBe(true);
  });

  it("records the native handoff before a stale generation yields to Preview", async () => {
    // The permission pill keeps the shaped region owner this test hands off.
    mocks.claudeCardSize = {
      width: CLAUDE_PERMISSION_PILL_W,
      height: CLAUDE_PERMISSION_MIN_H,
    };
    await mountHud();
    emit(pendingPermission(1));
    await flushPromises();

    let releaseHandoff: () => void = () => undefined;
    mocks.state.nativeHandoffAttempt = new Promise<void>((resolve) => {
      releaseHandoff = () => resolve();
    });
    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();

    emit(lifecycle(80, "recording", { preview_enabled: false }));
    await flushPromises();
    expect(mocks.windowCalls).toEqual(["native-hide+clear"]);

    emit(lifecycle(81, "recording", { preview_enabled: true }));
    await flushPromises();
    releaseHandoff();
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "native-hide+clear",
      "hide",
      "size:360x192",
      "position:780,780",
      "show",
    ]);
    expect(mocks.invoke.mock.calls.map(([command]) => command)).toEqual([
      "hide_and_clear_hud_window_region",
    ]);
    expect(mocks.state.nativeRegionInstalled).toBe(false);
    expect(mocks.state.tauriVisibleCache).toBe(true);
    expect(mocks.state.physicallyVisible).toBe(true);
  });

  it("round-trips one canonical pill anchor through every HUD geometry", () => {
    const anchor = { x: 734, y: 812 };
    for (const mode of [
      "seed",
      "launcher",
      "active",
      "pill",
      "preview",
      "panel",
    ] as const) {
      const position = windowPositionFromPillAnchor(mode, anchor);
      expect(pillAnchorFromWindowPosition(mode, position)).toEqual(anchor);
    }
  });

  it("anchors a measured compact Agent Speak pill to the recording pill", () => {
    const anchor = { x: 734, y: 812 };
    const measuredPanel = {
      surface: "panel" as const,
      width: CLAUDE_PILL_MIN_W,
      height: CLAUDE_PILL_H,
    };
    const panelGeometry = hudModeGeometry("panel", measuredPanel);

    expect(panelGeometry).toMatchObject({
      windowW: PANEL_WIDTH,
      windowH: PANEL_HEIGHT,
      cardW: CLAUDE_PILL_MIN_W,
      cardH: CLAUDE_PILL_H,
      padX: (PANEL_WIDTH - CLAUDE_PILL_MIN_W) / 2,
      // Bottom-pinned: padY is the card's top offset in the fixed window.
      padY: PANEL_HEIGHT - CLAUDE_PILL_H - REPLY_SURFACE_BOTTOM_PAD,
    });

    const recordingRect = pillRectFromAnchor("pill", anchor);
    const panelRect = pillRectFromAnchor("panel", anchor, measuredPanel);
    expect(panelRect.y).toBe(anchor.y + recordingRect.height - CLAUDE_PILL_H);
    expect(panelRect.height).toBe(CLAUDE_PILL_H);
    expect(panelRect.y + panelRect.height).toBe(
      recordingRect.y + recordingRect.height,
    );

    const panelPosition = windowPositionFromPillAnchor(
      "panel",
      anchor,
      measuredPanel,
    );
    expect(
      pillAnchorFromWindowPosition("panel", panelPosition, measuredPanel),
    ).toEqual(anchor);
  });

  it("keeps the panel fallback without a valid same-surface measurement", () => {
    const fallback = MODE_GEOMETRY.panel;
    const invalidMeasurements = [
      null,
      { surface: "response" as const, width: 108, height: 36 },
      { surface: "panel" as const, width: 0, height: 36 },
      { surface: "panel" as const, width: 108, height: Number.NaN },
      { surface: "panel" as const, width: PANEL_WIDTH + 1, height: 36 },
      { surface: "panel" as const, width: 108, height: RESPONSE_MAX_H + 1 },
    ];

    for (const measured of invalidMeasurements) {
      expect(hudModeGeometry("panel", measured)).toBe(fallback);
    }
  });

  it("opens one stable preview surface before the first batch appends text", async () => {
    await mountHud();

    emit(lifecycle(41, "recording"));
    await flushPromises();
    const card = screen.getByTestId("live-preview-card");
    expect(mocks.windowCalls).toEqual([
      "size:360x192",
      "position:780,780",
      "show",
    ]);

    mocks.windowCalls.length = 0;
    emit(preview(41, 1, "   "));
    await flushPromises();
    expect(screen.getByTestId("live-preview-card")).toBe(card);
    expect(mocks.windowCalls).toEqual([]);

    emit(preview(41, 2, "Hello changing world", 6));
    await flushPromises();

    expect(card).not.toHaveAttribute("aria-hidden");
    expect(card).toHaveAttribute("data-motion-initial", "false");
    expect(card).toHaveAttribute("data-motion-width", String(PREVIEW_CARD_W));
    expect(card).toHaveAttribute("data-motion-height", String(PREVIEW_CARD_H));
    expect(PREVIEW_WIDTH).toBe(360);
    expect(PREVIEW_HEIGHT).toBe(192);
    expect(PREVIEW_CARD_W).toBe(320);
    expect(PREVIEW_CARD_H).toBe(160);
    expect(screen.getByTestId("preview-text")).toHaveTextContent(
      "Hello changing world",
    );
    expect(screen.getByTestId("preview-text")).toHaveStyle({
      fontFamily: "var(--font-quote)",
      fontSize: "14px",
      fontWeight: "480",
      lineHeight: "1.46",
      letterSpacing: "-0.005em",
    });
    expect(screen.queryByTestId("preview-stable")).toBeNull();
    expect(screen.queryByTestId("preview-mutable")).toBeNull();
    expect(previewGraphemes()).toHaveLength("Hello changing world".length);
    const previewWell = screen.getByTestId("preview-well");
    expect(previewWell).toHaveAttribute("aria-hidden", "true");
    expect(previewWell).toHaveClass("overflow-y-auto", "overflow-x-hidden");
    expect(previewWell).not.toHaveAttribute("data-hud-interactive");
    expect(previewWell).toHaveStyle({
      height: `${PREVIEW_COMPACT_WELL_H}px`,
      overscrollBehavior: "contain",
      scrollBehavior: "auto",
      touchAction: "pan-y",
      // Nothing has scrolled under the top edge yet, so the first line stays
      // unfaded.
      WebkitMaskImage: "none",
      maskImage: "none",
    });
    expect(
      PREVIEW_COMPACT_WELL_H / (14 * 1.46),
    ).toBe(PREVIEW_COMPACT_LINE_COUNT);
    expect(PREVIEW_COMPACT_LINE_COUNT).toBe(3);
    expect(previewWell.childElementCount).toBe(1);
    expect(screen.queryByRole("status")).toBeNull();
    expect(mocks.windowCalls).toEqual([]);

    mocks.windowCalls.length = 0;
    emit(preview(41, 3, "Hello changing world again", 6));
    await flushPromises();
    expect(screen.getByTestId("preview-text")).toHaveTextContent(
      "Hello changing world again",
    );
    expect(mocks.windowCalls).toEqual([]);

    emit(preview(41, 4, ""));
    await flushPromises();
    expect(screen.getByTestId("live-preview-card")).toBeInTheDocument();
    expect(screen.getByTestId("preview-text").textContent).toBe("");
    expect(mocks.windowCalls).toEqual([]);
  });

  it("centers expanded geometry in a scaled negative work area", async () => {
    mocks.currentMonitor.mockResolvedValue({
      scaleFactor: 2,
      position: { x: -2560, y: 0 },
      size: { width: 3840, height: 2160 },
      workArea: {
        position: { x: -2400, y: 80 },
        size: { width: 1920, height: 1000 },
      },
    });
    await mountHud();
    emit(lifecycle(54, "recording"));
    emit(preview(54, 1, "Scaled monitor preview"));
    await flushPromises();

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    fireEvent.click(
      screen.getByRole("button", { name: "Expand preview" }),
    );
    await flushPromises();

    expect(PREVIEW_EXPANDED_WIDTH).toBe(560);
    expect(PREVIEW_EXPANDED_CARD_W).toBe(520);
    expect(PREVIEW_EXPANDED_MIN_CARD_H).toBe(360);
    expect(mocks.windowCalls).toEqual([
      "size:560x392",
      "position:-1000,94",
      "show",
    ]);
    expect(mocks.invoke).not.toHaveBeenCalled();
    expect(screen.getByTestId("live-preview-card")).toHaveAttribute(
      "data-motion-width",
      "520",
    );
  });

  it("falls back to full monitor bounds and a logical scale of one", async () => {
    mocks.currentMonitor.mockResolvedValue({
      scaleFactor: 0,
      position: { x: -600, y: 40 },
      size: { width: 1000, height: 700 },
    } as Awaited<ReturnType<typeof mocks.currentMonitor>>);
    await mountHud();
    emit(lifecycle(55, "recording"));
    emit(preview(55, 1, "Fallback geometry"));
    await flushPromises();

    mocks.windowCalls.length = 0;
    fireEvent.click(
      screen.getByRole("button", { name: "Expand preview" }),
    );
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "size:560x392",
      "position:-380,194",
      "show",
    ]);
  });

  it("caps expanded width and height inside a very small work area", async () => {
    mocks.currentMonitor.mockResolvedValue({
      scaleFactor: 1,
      position: { x: 0, y: 0 },
      size: { width: 800, height: 600 },
      workArea: {
        position: { x: 100, y: 200 },
        size: { width: 400, height: 180 },
      },
    });
    await mountHud();
    emit(lifecycle(56, "recording"));
    emit(preview(56, 1, "Small work area"));
    await flushPromises();

    mocks.windowCalls.length = 0;
    fireEvent.click(
      screen.getByRole("button", { name: "Expand preview" }),
    );
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "size:352x132",
      "position:124,224",
      "show",
    ]);
    const card = screen.getByTestId("live-preview-card");
    expect(card).toHaveAttribute("data-motion-width", "312");
    expect(card).toHaveAttribute("data-motion-height", "100");
    expect(224 + 132).toBe(200 + 180 - 24);
  });

  it("sizes once on expand and stays centered while later batches append", async () => {
    await mountHud();
    emit(lifecycle(57, "recording"));
    emit(preview(57, 1, "Natural height preview"));
    await flushPromises();
    mocks.naturalTextHeight = 200;
    mocks.windowCalls.length = 0;
    fireEvent.click(
      screen.getByRole("button", { name: "Expand preview" }),
    );
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "size:560x392",
      "position:680,344",
      "show",
    ]);
    expect(screen.getByTestId("live-preview-card")).toHaveAttribute(
      "data-motion-height",
      "360",
    );
    expect(screen.getByTestId("preview-well")).toHaveStyle({
      height: `${360 - (PREVIEW_CARD_H - PREVIEW_COMPACT_WELL_H)}px`,
    });
    expect(
      document.querySelector("[data-preview-natural-measure]"),
    ).toBeNull();

    mocks.windowCalls.length = 0;
    emit(preview(57, 2, "Natural height preview with another phrase"));
    await flushPromises();
    expect(mocks.windowCalls).toEqual([]);

    mocks.naturalTextHeight = 2000;
    emit(
      preview(
        57,
        3,
        "Natural height preview with another phrase and a much longer append at the height cap",
      ),
    );
    await flushPromises();
    expect(mocks.windowCalls).toEqual([]);
    expect(screen.getByTestId("live-preview-card")).toHaveAttribute(
      "data-motion-height",
      "360",
    );
  });

  it("keeps controls pointer-only, prevents expanded drag, and collapses to the canonical anchor", async () => {
    const canonicalAnchor = { x: 734, y: 812 };
    localStorage.setItem("hud-position", JSON.stringify(canonicalAnchor));
    await mountHud();
    emit(lifecycle(58, "recording"));
    emit(preview(58, 1, "Pointer-isolated controls"));
    await flushPromises();

    const root = screen.getByTestId("hud-root");
    const setPointerCapture = vi.fn();
    Object.defineProperty(root, "setPointerCapture", {
      configurable: true,
      value: setPointerCapture,
    });
    const expandButton = screen.getByRole("button", {
      name: "Expand preview",
    });
    expect(expandButton).toHaveAttribute("tabindex", "-1");
    expect(expandButton).toHaveAttribute("data-hud-interactive", "true");
    expect(screen.getByTestId("preview-well")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    expect(expandButton.closest('[aria-hidden="true"]')).toBeNull();

    fireEvent.pointerDown(expandButton, {
      pointerId: 11,
      screenX: 20,
      screenY: 20,
    });
    expect(setPointerCapture).not.toHaveBeenCalled();
    fireEvent.click(expandButton);
    await flushPromises();
    expect(screen.getByTestId("live-preview-card")).toHaveAttribute(
      "data-expanded",
      "true",
    );
    expect(screen.getByTestId("preview-well")).toHaveAttribute(
      "data-hud-interactive",
      "true",
    );

    mocks.hudWindow.outerPosition.mockClear();
    mocks.hudWindow.outerSize.mockClear();
    fireEvent.pointerDown(root, {
      pointerId: 12,
      screenX: 80,
      screenY: 60,
    });
    await flushPromises();
    expect(setPointerCapture).not.toHaveBeenCalled();
    expect(mocks.hudWindow.outerPosition).not.toHaveBeenCalled();
    expect(mocks.hudWindow.outerSize).not.toHaveBeenCalled();
    expect(localStorage.getItem("hud-position")).toBe(
      JSON.stringify(canonicalAnchor),
    );

    mocks.windowCalls.length = 0;
    const collapseButton = screen.getByRole("button", {
      name: "Collapse preview",
    });
    expect(collapseButton).toHaveAttribute("tabindex", "-1");
    fireEvent.click(collapseButton);
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "size:360x192",
      "position:614,672",
      "show",
    ]);
    expect(
      screen.getByRole("button", { name: "Expand preview" }),
    ).toBeInTheDocument();
    expect(localStorage.getItem("hud-position")).toBe(
      JSON.stringify(canonicalAnchor),
    );
  });

  it("preserves expanded mode through processing and resets it only for matching idle or a new recording", async () => {
    await mountHud();
    emit(lifecycle(59, "recording"));
    emit(preview(59, 1, "Lifecycle preview"));
    await flushPromises();
    fireEvent.click(
      screen.getByRole("button", { name: "Expand preview" }),
    );
    await flushPromises();

    mocks.windowCalls.length = 0;
    emit(lifecycle(59, "processing"));
    await flushPromises();
    expect(
      screen.getByRole("button", { name: "Collapse preview" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("dictation-processing-spinner")).toBeInTheDocument();
    expect(mocks.windowCalls).toEqual([]);

    emit(lifecycle(58, "idle"));
    await flushPromises();
    expect(
      screen.getByRole("button", { name: "Collapse preview" }),
    ).toBeInTheDocument();

    emit(lifecycle(59, "idle"));
    await flushPromises();
    expect(screen.queryByTestId("live-preview-card")).toBeNull();

    emit(lifecycle(60, "recording"));
    emit(preview(60, 1, "Next recording starts compact"));
    await flushPromises();
    expect(
      screen.getByRole("button", { name: "Expand preview" }),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Expand preview" }),
    );
    await flushPromises();
    emit(lifecycle(61, "recording"));
    emit(preview(61, 1, "Replacement recording also starts compact"));
    await flushPromises();
    expect(
      screen.getByRole("button", { name: "Expand preview" }),
    ).toBeInTheDocument();
  });

  it("serializes a collapse behind a stale pending expand without applying expanded geometry", async () => {
    await mountHud();
    emit(lifecycle(62, "recording"));
    emit(preview(62, 1, "Generation serialized preview"));
    await flushPromises();

    let resolveMonitor:
      | ((monitor: Awaited<ReturnType<typeof mocks.currentMonitor>>) => void)
      | undefined;
    const pendingMonitor = new Promise<
      Awaited<ReturnType<typeof mocks.currentMonitor>>
    >((resolve) => {
      resolveMonitor = resolve;
    });
    mocks.currentMonitor.mockImplementationOnce(() => pendingMonitor);
    mocks.windowCalls.length = 0;

    fireEvent.click(
      screen.getByRole("button", { name: "Expand preview" }),
    );
    await flushPromises();
    const inFlightCard = screen.getByTestId("live-preview-card");
    expect(inFlightCard).not.toHaveAttribute("data-geometry-transition");
    expect(inFlightCard).toHaveStyle({ opacity: "1" });
    expect(inFlightCard).toHaveAttribute("data-motion-width", "520");
    expect(inFlightCard).toHaveAttribute("data-motion-height", "160");
    expect(mocks.windowCalls).toEqual([]);
    fireEvent.click(
      screen.getByRole("button", { name: "Collapse preview" }),
    );
    resolveMonitor?.({
      scaleFactor: 1,
      position: { x: 0, y: 0 },
      size: { width: 1920, height: 1080 },
      workArea: {
        position: { x: 0, y: 0 },
        size: { width: 1920, height: 1080 },
      },
    });
    await flushPromises();

    expect(mocks.windowCalls).toEqual([]);
    const settledCard = screen.getByTestId("live-preview-card");
    expect(settledCard).not.toHaveAttribute("data-geometry-transition");
    expect(settledCard).toHaveAttribute("data-motion-width", "320");
    expect(settledCard).toHaveStyle({
      opacity: "1",
    });
    expect(
      screen.getByRole("button", { name: "Expand preview" }),
    ).toBeInTheDocument();
  });

  it("renders the complete accumulated preview beyond the old 120-character limit", async () => {
    await mountHud();
    const accumulatedPreview =
      `Opening thought ${"with every rolling preview word ".repeat(8)}final phrase`;
    expect(accumulatedPreview.length).toBeGreaterThan(120);

    emit(lifecycle(50, "recording"));
    emit(preview(50, 1, accumulatedPreview, 24));
    await flushPromises();

    expect(screen.getByTestId("preview-text")).toHaveTextContent(
      accumulatedPreview,
    );
    expect(previewGraphemes()).toHaveLength(accumulatedPreview.length);

    mocks.naturalTextHeight = 360;
    mocks.windowCalls.length = 0;
    fireEvent.click(
      screen.getByRole("button", { name: "Expand preview" }),
    );
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "size:560x491",
      "position:680,294.5",
      "show",
    ]);
    expect(screen.getByTestId("preview-text")).toHaveTextContent(
      accumulatedPreview,
    );
    const expandedWell = screen.getByTestId("preview-well");
    expect(expandedWell).toHaveStyle({
      height: `${459 - (PREVIEW_CARD_H - PREVIEW_COMPACT_WELL_H)}px`,
    });
    expect(expandedWell.style.webkitMaskImage).toBe("none");
    expect(expandedWell.style.maskImage).toBe("none");
  });

  // 100 batches of grapheme reveal genuinely take ~2s; the 5s default times
  // out under full-suite load (this flakes on main too).
  it("retains and scrolls a 100-batch transcript without resizing the expanded window", async () => {
    await mountHud();
    emit(lifecycle(64, "recording"));

    let transcript = "";
    for (let batch = 1; batch <= 100; batch += 1) {
      transcript += `${transcript ? " " : ""}preview batch ${batch} stays permanently visible`;
      emit(preview(64, batch, transcript, transcript.length));
    }
    await flushPromises();

    expect(screen.getByTestId("preview-text").textContent).toBe(transcript);
    expect(previewGraphemes()).toHaveLength(transcript.length);

    mocks.naturalTextHeight = 2400;
    mocks.windowCalls.length = 0;
    fireEvent.click(
      screen.getByRole("button", { name: "Expand preview" }),
    );
    await flushPromises();

    expect(mocks.windowCalls).toEqual([
      "size:560x1032",
      "position:680,24",
      "show",
    ]);
    expect(screen.getByTestId("live-preview-card")).toHaveAttribute(
      "data-motion-height",
      "1000",
    );

    const well = screen.getByTestId("preview-well");
    const metrics = {
      scrollHeight: 2400,
      clientHeight: 933,
      scrollTop: 1467,
    };
    installScrollMetrics(well, metrics);
    fireEvent.scroll(well);

    transcript += " preview batch 101 stays permanently visible";
    mocks.windowCalls.length = 0;
    metrics.scrollHeight = 2440;
    emit(preview(64, 101, transcript, transcript.length));
    await flushPromises();

    expect(screen.getByTestId("preview-text").textContent).toBe(transcript);
    expect(metrics.scrollTop).toBe(1507);
    expect(mocks.windowCalls).toEqual([]);
  }, 20_000);

  it("follows revisions only from within 4px of the bottom and preserves a manual scroll position", async () => {
    await mountHud();
    emit(lifecycle(51, "recording"));
    emit(preview(51, 1, "First rolling hypothesis with enough words to overflow"));
    await flushPromises();

    const well = screen.getByTestId("preview-well");
    const metrics = {
      scrollHeight: 200,
      clientHeight: 60,
      scrollTop: 0,
    };
    installScrollMetrics(well, metrics);
    expect(well.style.maskImage).toBe("none");

    emit(
      preview(
        51,
        2,
        "First rolling hypothesis with enough words to overflow and append",
      ),
    );
    await flushPromises();
    expect(metrics.scrollTop).toBe(140);
    // Text now sits under the top edge, so the compact well fades it.
    expect(well).toHaveStyle({
      maskImage: "linear-gradient(to bottom, transparent, black 10px)",
    });

    metrics.scrollTop = 80;
    fireEvent.scroll(well);
    metrics.scrollHeight = 240;
    emit(
      preview(
        51,
        3,
        "First rolling hypothesis with enough words to overflow and append while the reader stays at the manually chosen place",
      ),
    );
    await flushPromises();
    expect(metrics.scrollTop).toBe(80);

    metrics.scrollTop = 176;
    fireEvent.scroll(well);
    metrics.scrollHeight = 300;
    emit(
      preview(
        51,
        4,
        "First rolling hypothesis with enough words to overflow and append while the reader stays at the manually chosen place and now follows again",
      ),
    );
    await flushPromises();
    expect(metrics.scrollTop).toBe(240);
  });

  it("drags compact Preview from its text well but preserves the expanded scroller", async () => {
    await mountHud();
    emit(lifecycle(53, "recording"));
    emit(preview(53, 1, "Scrollable preview"));
    await flushPromises();

    const root = screen.getByTestId("hud-root");
    const setPointerCapture = vi.fn();
    Object.defineProperty(root, "setPointerCapture", {
      configurable: true,
      value: setPointerCapture,
    });
    mocks.hudWindow.outerPosition.mockClear();
    mocks.hudWindow.outerSize.mockClear();

    fireEvent.pointerDown(screen.getByTestId("preview-text"), {
      pointerId: 7,
      screenX: 40,
      screenY: 30,
    });
    await flushPromises();

    expect(setPointerCapture).toHaveBeenCalledTimes(1);
    expect(mocks.hudWindow.outerPosition).toHaveBeenCalledTimes(1);
    expect(mocks.hudWindow.outerSize).toHaveBeenCalledTimes(1);

    fireEvent.pointerUp(root, {
      pointerId: 7,
      screenX: 40,
      screenY: 30,
    });
    await flushPromises();

    fireEvent.click(
      screen.getByRole("button", { name: "Expand preview" }),
    );
    await flushPromises();
    expect(screen.getByTestId("preview-well")).toHaveAttribute(
      "data-hud-interactive",
      "true",
    );

    setPointerCapture.mockClear();
    mocks.hudWindow.outerPosition.mockClear();
    mocks.hudWindow.outerSize.mockClear();
    fireEvent.pointerDown(screen.getByTestId("preview-text"), {
      pointerId: 8,
      screenX: 80,
      screenY: 60,
    });
    await flushPromises();

    expect(setPointerCapture).not.toHaveBeenCalled();
    expect(mocks.hudWindow.outerPosition).not.toHaveBeenCalled();
    expect(mocks.hudWindow.outerSize).not.toHaveBeenCalled();
  });

  it("starts one-line preview text at the top and keeps that flow stable when it wraps", async () => {
    await mountHud();

    emit(lifecycle(48, "recording"));
    emit(preview(48, 1, "One line"));
    await flushPromises();

    const well = screen.getByTestId("preview-well");
    const text = screen.getByTestId("preview-text");
    expect(well).not.toHaveClass("items-end");
    expect(text.style.position).toBe("");
    expect(text.style.bottom).toBe("");

    emit(
      preview(
        48,
        2,
        "One line that grows naturally into enough words to wrap onto another line",
      ),
    );
    await flushPromises();

    expect(screen.getByTestId("preview-well")).toBe(well);
    expect(screen.getByTestId("preview-text")).toBe(text);
    expect(well).not.toHaveClass("items-end");
    expect(text.style.position).toBe("");
    expect(text.style.bottom).toBe("");
  });

  it("retains every existing grapheme element when text is appended", async () => {
    await mountHud();
    emit(lifecycle(42, "recording"));
    emit(preview(42, 1, "Hello"));
    await flushPromises();
    const before = previewGraphemes();
    const prefixRenderCounts = before.map(({ dataset }) =>
      mocks.glyphMotionRenderCounts.get(dataset.graphemeId ?? ""),
    );

    emit(preview(42, 2, "Hello world"));
    await flushPromises();
    const after = previewGraphemes();

    expect(after).toHaveLength(11);
    before.forEach((element, index) => {
      expect(after[index]).toBe(element);
      expect(
        mocks.glyphMotionRenderCounts.get(element.dataset.graphemeId ?? ""),
      ).toBe(prefixRenderCounts[index]);
    });
    after.slice(before.length).forEach((element, index) => {
      expectFuzzyReveal(element, index * 0.022);
    });
  });

  it("accepts bounded mutable corrections across successive revisions", async () => {
    await mountHud();
    emit(lifecycle(43, "recording"));
    emit(preview(43, 1, "Hello changing"));
    await flushPromises();
    const before = previewGraphemes();

    emit(preview(43, 2, "Hello calmer"));
    await flushPromises();
    const after = previewGraphemes();

    expect(screen.getByTestId("preview-text")).toHaveTextContent(
      "Hello calmer",
    );
    const firstCommonPrefixLength = "Hello c".length;
    before.slice(0, firstCommonPrefixLength).forEach((element, index) => {
      expect(after[index]).toBe(element);
    });
    before.slice(firstCommonPrefixLength).forEach((element) => {
      expect(after).not.toContain(element);
    });
    after.slice(firstCommonPrefixLength).forEach(expectImmediateReplacement);

    const corrected = [...after];
    emit(preview(43, 3, "Hello quieter now"));
    await flushPromises();
    const appended = previewGraphemes();
    expect(screen.getByTestId("preview-text")).toHaveTextContent(
      "Hello quieter now",
    );
    const secondCommonPrefixLength = "Hello ".length;
    corrected.slice(0, secondCommonPrefixLength).forEach((element, index) => {
      expect(appended[index]).toBe(element);
    });
    corrected.slice(secondCommonPrefixLength).forEach((element) => {
      expect(appended).not.toContain(element);
    });
    appended.slice(secondCommonPrefixLength).forEach(expectImmediateReplacement);
  });

  it("directly assigns every accepted cumulative revision, including freeze", async () => {
    await mountHud();
    emit(lifecycle(54, "recording"));
    emit(preview(54, 1, "one two three four five"));
    await flushPromises();
    const initialGraphemes = previewGraphemes();

    emit(preview(54, 2, "one two three four five six seven eight"));
    await flushPromises();
    expect(screen.getByTestId("preview-text").textContent).toBe(
      "one two three four five six seven eight",
    );
    initialGraphemes.forEach((element, index) => {
      expect(previewGraphemes()[index]).toBe(element);
    });

    emit(
      preview(
        54,
        3,
        "one two three four five six seven eight nine ten eleven",
      ),
    );
    await flushPromises();
    expect(screen.getByTestId("preview-text").textContent).toBe(
      "one two three four five six seven eight nine ten eleven",
    );
    initialGraphemes.forEach((element, index) => {
      expect(previewGraphemes()[index]).toBe(element);
    });

    const complete =
      "one two three four five six seven eight nine ten eleven";
    const collapsedFreeze = "seven eight nine ten eleven";
    emit(
      preview(
        54,
        4,
        collapsedFreeze,
        collapsedFreeze.length,
        collapsedFreeze.length,
        true,
      ),
    );
    await flushPromises();
    expect(screen.getByTestId("preview-text").textContent).toBe(
      collapsedFreeze,
    );

    const rewrittenFreeze =
      "wrong early words must not replace the retained preview prefix today";
    emit(
      preview(
        54,
        5,
        rewrittenFreeze,
        rewrittenFreeze.length,
        rewrittenFreeze.length,
        true,
      ),
    );
    await flushPromises();
    expect(screen.getByTestId("preview-text").textContent).toBe(
      rewrittenFreeze,
    );

    const longerRewrittenFreeze =
      "wrong early words must not replace this entirely unrelated and much longer retained preview presentation today";
    emit(
      preview(
        54,
        6,
        longerRewrittenFreeze,
        longerRewrittenFreeze.length,
        longerRewrittenFreeze.length,
        true,
      ),
    );
    await flushPromises();
    expect(screen.getByTestId("preview-text").textContent).toBe(
      longerRewrittenFreeze,
    );

    emit(
      preview(
        54,
        7,
        complete,
        complete.length,
        complete.length,
        true,
      ),
    );
    await flushPromises();
    expect(screen.getByTestId("preview-text").textContent).toBe(complete);
    expect(
      screen.getByTestId("preview-text").textContent?.match(/\bone\b/g),
    ).toHaveLength(1);
  });

  it("renders the recording-04 cumulative backend revisions verbatim", async () => {
    await mountHud();
    emit(lifecycle(68, "recording"));
    emit(preview(68, 1, "I'm testing out live pre"));
    emit(preview(68, 2, "I'm testing out live preview and I'm"));
    emit(
      preview(
        68,
        3,
        "I'm testing out live preview and I'm noticing that",
      ),
    );
    emit(
      preview(
        68,
        4,
        "I'm testing out live preview and I'm noticing that.",
      ),
    );
    emit(
      preview(
        68,
        5,
        "I'm testing out live preview and I'm noticing that. um",
      ),
    );
    await flushPromises();

    expect(screen.getByTestId("preview-text").textContent).toBe(
      "I'm testing out live preview and I'm noticing that. um",
    );
    expect(
      screen.getByTestId("preview-text").textContent?.match(/\bpreview\b/g),
    ).toHaveLength(1);
  });

  it("renders the recording-05 cumulative pause rebase verbatim", async () => {
    await mountHud();
    emit(lifecycle(69, "recording"));
    emit(preview(69, 1, "I am just talking."));
    emit(preview(69, 2, "Hey, I'm just talking um pa"));
    emit(
      preview(
        69,
        3,
        "Hey, I'm just talking, gonna pause.",
      ),
    );
    emit(
      preview(
        69,
        4,
        "Hey, I'm just talking, gonna pause and then I'm gonna s",
      ),
    );
    emit(
      preview(
        69,
        5,
        "Hey, I'm just talking, gonna pause and then when I see",
      ),
    );
    emit(
      preview(
        69,
        6,
        "Hey, I'm just talking, gonna pause and then I want to see",
      ),
    );
    emit(
      preview(
        69,
        7,
        "Hey, I'm just talking, gonna pause and then I wanna see to make sure",
      ),
    );
    await flushPromises();

    const visible = screen.getByTestId("preview-text").textContent;
    expect(
      visible?.match(/\bjust\b/g),
    ).toHaveLength(1);
    expect(
      visible?.match(/\btalking\b/g),
    ).toHaveLength(1);
    expect(visible).toContain("pause");
    expect(
      visible?.match(/\band\b/g),
    ).toHaveLength(1);
    expect(
      visible?.match(/\bthen\b/g),
    ).toHaveLength(1);
  });

  it("uses the backend cumulative correction when confirmation advances", async () => {
    await mountHud();
    emit(lifecycle(71, "recording"));
    const previous =
      "I'm testing out live preview and I'm noticing that um well it um it's hiding text like it check this last transcript like my earlier words are going away I only see like the last four words It's okay now I see all the words like as time. goes on, like. it's collapsed.";
    emit(preview(71, 27, previous));
    const next =
      "I'm testing out live preview and I'm noticing that Um well it um it's hiding text like it check this last transcript like my earlier words are going away. I only see like the last four collapsing a bunch of words.";
    emit(preview(71, 28, next, next.indexOf("collapsing")));
    await flushPromises();

    const visible = screen.getByTestId("preview-text").textContent;
    expect(visible).not.toContain("goes on, like. it's collapsed.");
    expect(visible).toContain("collapsing a bunch of words.");
    expect(visible).toBe(next);
  });

  it("leaves unrelated-revision presentation policy to the backend", async () => {
    await mountHud();
    emit(lifecycle(70, "recording"));
    emit(preview(70, 1, "one two three four five six seven"));
    emit(preview(70, 2, "six unrelated alpha beta gamma"));
    await flushPromises();

    expect(screen.getByTestId("preview-text").textContent).toBe(
      "six unrelated alpha beta gamma",
    );
  });

  it("ignores an unsupported backend presentation version", async () => {
    await mountHud();
    emit(lifecycle(72, "recording"));
    emit(preview(72, 1, "supported cumulative text"));
    emit({
      ...preview(72, 2, "unsupported replacement"),
      presentation_version: 2,
    } as unknown as AppEvent);
    await flushPromises();

    expect(screen.getByTestId("preview-text").textContent).toBe(
      "supported cumulative text",
    );
  });

  it("accepts a backend freeze that inserts a recovered interior span", async () => {
    await mountHud();
    emit(lifecycle(66, "recording"));
    emit(
      preview(
        66,
        3,
        "Hello testing. Is this working? Just want to make sure",
      ),
    );
    emit(
      preview(
        66,
        15,
        "Hello testing. Is this working? did it vanish from here?",
      ),
    );
    emit(
      preview(
        66,
        16,
        "Hello testing. Is this working? did vanish from here? So,",
      ),
    );
    emit(
      preview(
        66,
        17,
        "Hello testing. Is this working? did vanish from here? So, okay.",
      ),
    );
    await flushPromises();

    expect(screen.getByTestId("preview-text").textContent).toBe(
      "Hello testing. Is this working? did vanish from here? So, okay.",
    );

    const complete =
      "Hello testing. Is this working? Just want to make sure that all the text is showing up. And why did it vanish from here? So, okay.";
    emit(
      preview(
        66,
        18,
        complete,
        complete.length,
        complete.length,
        true,
      ),
    );
    await flushPromises();

    expect(screen.getByTestId("preview-text").textContent).toBe(complete);
  });

  it("accepts a longer backend freeze across ASR substitutions", async () => {
    await mountHud();
    emit(lifecycle(67, "recording"));
    emit(
      preview(
        67,
        1,
        "Hello testing is Um wanna that this is okay",
      ),
    );
    await flushPromises();

    const complete =
      "Hello testing is this working I want to make sure that this is okay";
    emit(
      preview(
        67,
        2,
        complete,
        complete.length,
        complete.length,
        true,
      ),
    );
    await flushPromises();

    expect(screen.getByTestId("preview-text").textContent).toBe(complete);
  });

  it("renders cumulative opening, rolling, and correction revisions", async () => {
    await mountHud();
    emit(lifecycle(65, "recording"));

    emit(preview(65, 1, "one two three"));
    emit(
      preview(
        65,
        2,
        "opening one two three four five six seven",
      ),
    );
    await flushPromises();
    expect(screen.getByTestId("preview-text").textContent).toBe(
      "opening one two three four five six seven",
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Expand preview" }),
    );
    await flushPromises();
    expect(screen.getByTestId("preview-text").textContent).toBe(
      "opening one two three four five six seven",
    );

    emit(
      preview(
        65,
        3,
        "opening one two three four five six seven eight",
      ),
    );
    await flushPromises();
    expect(screen.getByTestId("preview-text").textContent).toBe(
      "opening one two three four five six seven eight",
    );

    emit(
      preview(
        65,
        4,
        "opening one two three four five six seven ate",
      ),
    );
    await flushPromises();
    expect(screen.getByTestId("preview-text").textContent).toBe(
      "opening one two three four five six seven ate",
    );
  });

  it("preserves common glyphs when the backend corrects its cumulative tail", async () => {
    await mountHud();
    emit(lifecycle(55, "recording"));
    emit(preview(55, 1, "one two three four five"));
    emit(preview(55, 2, "one two three four five six seven eight"));
    await flushPromises();
    const before = previewGraphemes();
    const retainedLength = "one two three ".length;

    emit(preview(55, 3, "one two three four five six seven ate"));
    await flushPromises();
    const after = previewGraphemes();

    expect(screen.getByTestId("preview-text").textContent).toBe(
      "one two three four five six seven ate",
    );
    before.slice(0, retainedLength).forEach((element, index) => {
      expect(after[index]).toBe(element);
    });
  });

  it("does not duplicate words already deduplicated by the backend ledger", async () => {
    await mountHud();
    emit(lifecycle(56, "recording"));
    emit(preview(56, 1, "one two three four five"));
    emit(preview(56, 2, "one two three four five six seven eight"));
    emit(
      preview(
        56,
        3,
        "one two three four five six seven eight nine ten eleven",
      ),
    );
    await flushPromises();

    const stablePrefix = "one two three four five";
    emit(
      preview(
        56,
        4,
        `${stablePrefix} six seven eight nine twelve`,
        stablePrefix.length,
      ),
    );
    await flushPromises();

    const visible = screen.getByTestId("preview-text").textContent;
    expect(visible).toBe(
      "one two three four five six seven eight nine twelve",
    );
    expect(visible?.match(/\bsix\b/g)).toHaveLength(1);
  });

  it("replaces at most five mutable words while retaining the stable prefix", async () => {
    await mountHud();
    emit(lifecycle(46, "recording"));
    emit(
      preview(
        46,
        1,
        "Hello one two three four five",
        "Hello ".length,
      ),
    );
    await flushPromises();
    const before = previewGraphemes();
    const locked = before.slice(0, "Hello ".length);
    const mutable = before.slice("Hello ".length);
    const lockedRenderCounts = locked.map(({ dataset }) =>
      mocks.glyphMotionRenderCounts.get(dataset.graphemeId ?? ""),
    );

    emit(
      preview(
        46,
        2,
        "Hello alpha beta gamma delta epsilon",
        "Hello ".length,
      ),
    );
    await flushPromises();
    const after = previewGraphemes();

    expect(screen.getByTestId("preview-text")).toHaveTextContent(
      "Hello alpha beta gamma delta epsilon",
    );
    locked.forEach((element, index) => {
      expect(after[index]).toBe(element);
      expect(
        mocks.glyphMotionRenderCounts.get(element.dataset.graphemeId ?? ""),
      ).toBe(lockedRenderCounts[index]);
    });
    mutable.forEach((element) => {
      expect(after).not.toContain(element);
    });
    after.slice("Hello ".length).forEach(expectImmediateReplacement);

    emit(preview(46, 3, "Hello quieter", "Hello ".length));
    await flushPromises();
    expect(screen.getByTestId("preview-text")).toHaveTextContent(
      "Hello quieter",
    );
  });

  it("reveals a long append left to right without bunching its later glyphs", async () => {
    await mountHud();
    emit(lifecycle(49, "recording"));
    emit(preview(49, 1, "a"));
    await flushPromises();

    emit(preview(49, 2, "abcdefghijkl"));
    await flushPromises();
    const suffix = previewGraphemes().slice(1);

    expect(suffix).toHaveLength(11);
    suffix.forEach((element, index) => {
      expectFuzzyReveal(element, index * 0.022);
    });
    expect(suffix[8]).toHaveAttribute(
      "data-motion-delay",
      String(8 * 0.022),
    );
    expect(suffix[10]).toHaveAttribute(
      "data-motion-delay",
      String(10 * 0.022),
    );
    expect(suffix[10]).not.toHaveAttribute(
      "data-motion-delay",
      suffix[8].getAttribute("data-motion-delay"),
    );
  });

  it("retains every grapheme element when agreement promotes unchanged text", async () => {
    await mountHud();
    emit(lifecycle(44, "recording"));
    emit(preview(44, 1, "Hello settled text", 0));
    await flushPromises();
    const before = previewGraphemes();

    emit(preview(44, 2, "Hello settled text", 14));
    await flushPromises();
    const after = previewGraphemes();

    expect(after).toHaveLength(before.length);
    after.forEach((element, index) => {
      expect(element).toBe(before[index]);
    });
  });

  it("retains every grapheme element across audio-level updates", async () => {
    await mountHud();
    emit(lifecycle(45, "recording"));
    emit(preview(45, 1, "Steady transcript"));
    await flushPromises();
    const before = previewGraphemes();

    emit({ type: "audio.levels", rms: 0.8, peak: 0.95 });
    await flushPromises();
    const after = previewGraphemes();

    after.forEach((element, index) => {
      expect(element).toBe(before[index]);
      expect(element.dataset.graphemeId).toBe(
        before[index]?.dataset.graphemeId,
      );
    });
  });

  it("keeps complex Unicode graphemes intact and stable through an append", async () => {
    await mountHud();
    emit(lifecycle(46, "recording"));
    emit(preview(46, 1, "Go 👨‍👩‍👧‍👦 cafe\u0301"));
    await flushPromises();
    const before = previewGraphemes();

    expect(before.map((element) => element.textContent)).toContain("👨‍👩‍👧‍👦");
    expect(before.map((element) => element.textContent)).toContain("e\u0301");

    emit(preview(46, 2, "Go 👨‍👩‍👧‍👦 cafe\u0301!"));
    await flushPromises();
    const after = previewGraphemes();

    expect(after).toHaveLength(before.length + 1);
    before.forEach((element, index) => {
      expect(after[index]).toBe(element);
    });
  });

  it("accepts a backend-owned correction that changes an earlier prefix", async () => {
    await mountHud();
    emit(lifecycle(47, "recording"));
    emit(
      preview(
        47,
        1,
        "The older rolling window",
        "The older ".length,
      ),
    );
    await flushPromises();
    const before = previewGraphemes();

    emit(preview(47, 2, "The newest bounded window"));
    await flushPromises();
    const after = previewGraphemes();

    for (let index = 0; index < "The ".length; index += 1) {
      expect(after[index]).toBe(before[index]);
    }
    expect(screen.getByTestId("preview-text")).toHaveTextContent(
      "The newest bounded window",
    );
  });

  it("rejects stale generations and revisions without resizing the revealed card", async () => {
    await mountHud();
    emit(lifecycle(52, "recording"));
    emit(preview(52, 2, "Current hypothesis", 8));
    await flushPromises();
    mocks.windowCalls.length = 0;

    emit(preview(51, 99, "WRONG GENERATION"));
    emit(preview(52, 1, "WRONG REVISION"));
    emit(preview(52, 2, "WRONG DUPLICATE"));
    await flushPromises();

    expect(screen.getByTestId("live-preview-card")).toHaveTextContent(
      "Current hypothesis",
    );
    expect(document.body).not.toHaveTextContent(/WRONG/);
    expect(mocks.windowCalls).toEqual([]);
  });

  it("freezes preview through final result and enhancement until matching idle", async () => {
    await mountHud();
    emit(lifecycle(63, "recording"));
    emit(preview(63, 1, "Raw preview only"));
    await flushPromises();

    emit(lifecycle(63, "processing"));
    emit({
      type: "transcription.result",
      text: "Enhanced final text must not render",
      duration_ms: 100,
    });
    emit(preview(63, 2, "Late preview must not render"));
    await flushPromises();

    expect(screen.getByTestId("live-preview-card")).toHaveAttribute(
      "data-phase",
      "processing",
    );
    expect(screen.getByTestId("dictation-processing-spinner")).toBeInTheDocument();
    expect(screen.getByTestId("live-preview-card")).toHaveTextContent(
      "Raw preview only",
    );
    expect(document.body).not.toHaveTextContent(
      /Enhanced final text|Late preview/,
    );

    emit(lifecycle(62, "idle"));
    await flushPromises();
    expect(screen.getByTestId("live-preview-card")).toBeInTheDocument();

    emit(lifecycle(63, "idle"));
    await flushPromises();
    expect(screen.queryByTestId("live-preview-card")).toBeNull();
  });

  it("ignores stale legacy idle copies after the authoritative lifecycle appears", async () => {
    await mountHud();
    emit(lifecycle(71, "recording"));
    emit({ type: "state.recording", recording: false });
    emit({ type: "state.transcribing", active: false });
    emit(preview(71, 1, "Still recording"));
    await flushPromises();

    expect(screen.getByTestId("live-preview-card")).toHaveAttribute(
      "data-phase",
      "recording",
    );
    expect(screen.getByTestId("live-preview-card")).toHaveTextContent(
      "Still recording",
    );
  });

  it("keeps the non-verbal Live Preview spinner open during processing", async () => {
    await mountHud();
    emit(lifecycle(88, "recording"));
    emit(lifecycle(88, "processing"));
    await flushPromises();

    expect(screen.getByTestId("dictation-processing-spinner")).toBeInTheDocument();
    expect(screen.getByTestId("live-preview-card")).toHaveAttribute(
      "data-phase",
      "processing",
    );
    expect(screen.getByTestId("preview-text")).toBeEmptyDOMElement();
    expect(document.body).not.toHaveTextContent(/processing|enhancing/i);
  });

  it("mounts reduced-motion graphemes immediately sharp with no reveal animation", async () => {
    mocks.reducedMotion = true;
    await mountHud();
    emit(lifecycle(91, "recording"));
    emit(preview(91, 1, "Reduced motion hypothesis"));
    await flushPromises();

    for (const grapheme of previewGraphemes()) {
      expect(grapheme).not.toHaveAttribute("data-motion-initial-filter");
      expect(grapheme).not.toHaveAttribute("data-motion-initial-opacity");
      expect(grapheme).not.toHaveAttribute("data-motion-animate-filter");
      expect(grapheme).not.toHaveAttribute("data-motion-animate-opacity");
      expect(grapheme).not.toHaveAttribute("data-motion-duration");
      expect(grapheme).not.toHaveAttribute("data-motion-delay");
      expect(grapheme).not.toHaveAttribute("data-motion-initial-y");
      expect(grapheme).not.toHaveAttribute("data-motion-exit-y");
    }
  });

  // --- Task 3.1: one measured-card path feeding native bounds ---
  // Both Claude card surfaces are content-measured now, so `QUESTION_CARD_H`
  // (a fixed 320 serving both) is retired and one measurement routine feeds
  // one `measuredCardSize`, which feeds the CSS box and the native bounds
  // together (design decisions 13, 16, 17, 18).

  function lastBounds(): string | undefined {
    const bounds = mocks.windowCalls.filter((call) =>
      call.startsWith("bounds:"),
    );
    return bounds[bounds.length - 1];
  }

  async function expandResponse(
    completion: AgentResponseCompletion,
  ): Promise<void> {
    emit(responseSnapshotWith(completion, 1));
    await flushPromises();
    fireEvent.doubleClick(screen.getByTestId("hud-root"));
    await flushPromises();
  }

  it("fades a closing pill out at its own size instead of the recording pill", async () => {
    // Owner report 2026-10-24: during the hide delay the generic recording
    // pill stood in for the reply pill, so it shrank to a dot before fading.
    // The Windows app's separate control window; the shared-window fallback
    // parks the control seed in the hidden HUD instead.
    mocks.dedicatedRecordingControlWindow = true;
    mocks.claudeCardSize = { width: CLAUDE_PILL_MIN_W, height: CLAUDE_PILL_H };
    await mountHud();
    emit(responseSnapshotWith(response("bye", 1), 1));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
    emit(terminalResponseSnapshot(2, "idle"));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
    expect(screen.getByTestId("hud-root").className).toContain("justify-end");
  });

  it("drags the resting pill, and opens typing only on a click that did not move", async () => {
    mocks.claudeCardSize = { width: CLAUDE_PILL_MIN_W, height: CLAUDE_PILL_H };
    await mountHud();
    emit(responseSnapshotWith(response("drag-me", 1), 1, { sessionHandle: "drag-conv" }));
    await flushPromises();
    const root = screen.getByTestId("hud-root");
    const setPointerCapture = vi.fn();
    Object.defineProperty(root, "setPointerCapture", { configurable: true, value: setPointerCapture });
    const pill = () => screen.getByTestId("compact-claude-pill");

    // A drag starts from the pill and does not open typing.
    fireEvent.pointerDown(pill(), { pointerId: 81, screenX: 100, screenY: 80 });
    await flushPromises();
    expect(setPointerCapture).toHaveBeenCalledWith(81);
    fireEvent.pointerMove(pill(), { pointerId: 81, screenX: 160, screenY: 80 });
    fireEvent.pointerUp(pill(), { pointerId: 81, screenX: 160, screenY: 80 });
    await flushPromises();
    expect(screen.queryByRole("textbox", { name: "Edit your reply" })).toBeNull();

    // A press and release in place opens typing.
    fireEvent.pointerDown(pill(), { pointerId: 82, screenX: 100, screenY: 80 });
    await flushPromises();
    fireEvent.pointerUp(pill(), { pointerId: 82, screenX: 100, screenY: 80 });
    await flushPromises();
    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toBeInTheDocument();
  });

  it("keeps the Sent reply row on a closing pill until it is hidden", async () => {
    // Review 2026-10-25: the receipt expiring in the render that hides the
    // pill collapsed its reply row during the fade.
    mocks.dedicatedRecordingControlWindow = true;
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 68 };
    await mountHud();
    emit(responseSnapshotWith(response("sent-exit", 1), 1, { sessionHandle: "exit-conv" }));
    emit(replySnapshot([replyDraftFor("conv:exit-conv", { phase: "sending", text: "Ship it" })]));
    await flushPromises();
    emit(terminalResponseSnapshot(2));
    emit(replySnapshot([], [{ target_id: "conv:exit-conv", draft_id: "d1", result: "sent", text: "Ship it" }]));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toHaveTextContent("Sent");

    await advance(SENT_REPLY_HOLD_MS);
    const closing = screen.getByTestId("compact-claude-pill");
    expect(closing).toHaveTextContent("Ship it");
    expect(closing).toHaveTextContent("Sent");
  });

  it("does not replay the reply surface's entrance when its card remounts on screen", async () => {
    // Surface state (architecture step 4): the entrance runs while a surface
    // is appearing, never on a mount while it is steady.
    mocks.claudeCardSize = { width: CLAUDE_PILL_MIN_W, height: CLAUDE_PILL_H };
    await mountHud();
    emit(responseSnapshotWith(response("steady-card", 1), 1));
    await flushPromises();
    expect(screen.getByTestId("hud-surface-reveal")).toBeInTheDocument();

    // A question takes the same reply window as a compact pill, then leaves.
    emit(questionSnapshot(1));
    await flushPromises();
    expect(screen.queryByTestId("hud-surface-reveal")).toBeNull();
    emit({ ...questionSnapshot(2), active: null });
    await flushPromises();
    const reveal = screen.getByTestId("hud-surface-reveal");
    expect(reveal).toHaveAttribute("data-motion-initial", "false");
    expect(reveal).toHaveAttribute("data-motion-animate-opacity", "1");
  });

  it("keeps a hiding console's last frame instead of the generic pill", async () => {
    mocks.dedicatedRecordingControlWindow = true;
    mocks.claudeCardSize = { width: CLAUDE_PILL_MIN_W, height: CLAUDE_PILL_H };
    await mountHud();
    await expandResponse(response("console-exit", 1));
    expect(screen.getByTestId("claude-expanded-card")).toHaveAttribute("data-console", "true");

    emit(terminalResponseSnapshot(2, "idle"));
    await flushPromises();
    expect(screen.getByTestId("claude-expanded-card")).toHaveAttribute("data-console", "true");
    expect(screen.queryByTestId("hud-pill-vessel")).toBeNull();
    expect(screen.getByTestId("hud-surface-reveal")).toHaveAttribute("data-motion-animate-opacity", "0");
  });

  it("keeps text that cannot be sent with its reason and a Copy action", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    await mountHud();
    emit(responseSnapshotWith(response("c-unsendable", 1), 1, { sessionHandle: "closed-conv" }));
    emit(replySnapshot([
      replyDraftFor("conv:closed-conv", { phase: "editing", text: "Keep these words", delivery: "none" }),
    ]));
    await flushPromises();

    expect(screen.getByTestId("claude-reply-hint")).toHaveAttribute("data-state", "no_route");
    fireEvent.click(screen.getByTestId("claude-reply-copy"));
    expect(writeText).toHaveBeenCalledWith("Keep these words");
    expect(screen.getByTestId("claude-reply-copy")).toHaveAccessibleName("Copied");
  });

  it("brings back kept text as an open field, but an empty one at rest", async () => {
    await mountHud();
    emit(responseSnapshotWith(response("c-empty-edit", 1), 1, { sessionHandle: "empty-conv" }));
    // Typing was opened on an earlier card and nothing was typed: the next
    // card arrives resting, not typing.
    emit(replySnapshot([replyDraftFor("conv:empty-conv", { phase: "editing", text: "" })]));
    await flushPromises();
    expect(screen.queryByRole("textbox", { name: "Edit your reply" })).toBeNull();

    emit(replySnapshot([replyDraftFor("conv:empty-conv", { phase: "editing", text: "half a thought" })]));
    await flushPromises();
    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveValue("half a thought");
  });

  it("keeps the card while a wrapper collects the reply, then confirms the sent text", async () => {
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 68 };
    await mountHud();
    emit(responseSnapshotWith(response("wrapped", 1), 1, { sessionHandle: "wrap-conv" }));
    emit(replySnapshot([replyDraftFor("conv:wrap-conv", { phase: "sending", text: "On its way" })]));
    await flushPromises();
    // The hook resolved into the wrapper's slot: the response ends first.
    emit(terminalResponseSnapshot(2));
    await flushPromises();
    const pill = screen.getByTestId("compact-claude-pill");
    expect(pill).toHaveTextContent("On its way");
    expect(pill).toHaveTextContent("Sending");

    emit(replySnapshot([], [{ target_id: "conv:wrap-conv", draft_id: "d1", result: "sent", text: "On its way" }]));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toHaveTextContent("Sent");
    await advance(SENT_REPLY_HOLD_MS);
    expect(screen.queryByTestId("compact-claude-pill")).toBeNull();
  });

  it("says a held reply waits for the next Stop and offers Copy", async () => {
    await mountHud();
    emit(responseSnapshotWith(response("held", 1), 1, { sessionHandle: "held-conv" }));
    emit(replySnapshot([replyDraftFor("conv:held-conv", { phase: "sending", held: true, text: "Later please" })]));
    await flushPromises();
    expect(screen.getByTestId("claude-reply-hint")).toHaveAttribute("data-state", "held");
    expect(screen.getByTestId("claude-reply-copy")).toBeInTheDocument();
  });

  it("brings an uncollected send back on the card with Copy until Escape skips it", async () => {
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 68 };
    await mountHud();
    emit(responseSnapshotWith(response("lost", 1), 1, { sessionHandle: "lost-conv" }));
    emit(replySnapshot([replyDraftFor("conv:lost-conv", { phase: "sending", text: "Nobody took it" })]));
    await flushPromises();
    emit(terminalResponseSnapshot(2));
    emit(replySnapshot([
      replyDraftFor("conv:lost-conv", {
        phase: "editing",
        text: "Nobody took it",
        delivery: "none",
        outcome: { revision: 1, result: "refused", reason: "not_delivered" },
      }),
    ]));
    await flushPromises();
    expect(screen.getByTestId("claude-reply-hint")).toHaveAttribute("data-state", "not_delivered");
    expect(screen.getByTestId("claude-reply-copy")).toBeInTheDocument();

    emit({ type: "agent.reply.close", version: 1, target_id: "conv:lost-conv", surface: "card" });
    await flushPromises();
    await advance(400);
    expect(screen.queryByTestId("compact-claude-pill")).toBeNull();
  });

  it("drops typed buffers when the backend owner restarts", async () => {
    await mountHud();
    emit(replySnapshot());
    emit(responseSnapshotWith(spokenResponse("epoch", 1), 1, { showSummaryText: true, sessionHandle: "epoch-conv" }));
    await flushPromises();
    fireEvent.click(screen.getByRole("button", { name: "Reply to agent" }));
    await flushPromises();
    fireEvent.change(screen.getByRole("textbox", { name: "Edit your reply" }), { target: { value: "typed here" } });
    await flushPromises();
    emit({ ...replySnapshot([replyDraftFor("conv:epoch-conv", { phase: "editing", text: "server text", text_revision: 1 })]), epoch: "restarted" });
    await flushPromises();
    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveValue("server text");
  });

  // --- REPRO: the configured view shortcut (`agent-hud-view`) ---
  // Every expansion test above drives `doubleClick`. The shortcut arrives as a
  // socket event instead, and nothing covered that path.
  it("expands a response through the view shortcut", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();

    emit(responseSnapshotWith(response("c-1", 1), 1));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();

    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();

    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
  });

  it("ends typing with its card, so the conversation's next card arrives resting", async () => {
    await mountHud();
    emit(responseSnapshotWith(spokenResponse("typed-once", 1), 1, { showSummaryText: true, sessionHandle: "again-conv" }));
    await flushPromises();
    fireEvent.click(screen.getByRole("button", { name: "Reply to agent" }));
    await flushPromises();
    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toBeInTheDocument();

    // Nothing typed; the same conversation's next response replaces the card.
    emit(responseSnapshotWith(spokenResponse("typed-twice", 2), 2, { showSummaryText: true, sessionHandle: "again-conv" }));
    await flushPromises();
    expect(screen.queryByRole("textbox", { name: "Edit your reply" })).toBeNull();
    expect(screen.getByRole("button", { name: "Reply to agent" })).toBeInTheDocument();
  });

  it("types a hook reply before any backend acknowledgement and submits the shown text", async () => {
    await mountHud();
    const completion = spokenResponse("typed-first", 1);
    emit(responseSnapshotWith(completion, 1, { showSummaryText: true, sessionHandle: "typed-conv" }));
    await flushPromises();

    fireEvent.click(screen.getByRole("button", { name: "Reply to agent" }));
    await flushPromises();
    const editor = screen.getByRole("textbox", { name: "Edit your reply" });
    expect(editor).toHaveFocus();

    // No draft exists yet: the first update creates it.
    fireEvent.change(editor, { target: { value: "hi" } });
    fireEvent.change(editor, { target: { value: "hi there" } });
    expect(sentCommands("agent.draft.update").map(({ base_revision, text }) => [base_revision, text])).toEqual([
      [0, "hi"],
      [1, "hi there"],
    ]);
    fireEvent.keyDown(editor, { key: "Enter", shiftKey: true });
    expect(sentCommands("agent.draft.submit")).toHaveLength(0);
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(sentCommands("agent.draft.submit")).toEqual([
      expect.objectContaining({ target_id: "conv:typed-conv", revision: 2, text: "hi there" }),
    ]);
  });

  it("keeps the compact reply window fixed while the rail opens and grows, stepping only when outgrown", () => {
    const at = (height: number) =>
      hudModeGeometry("panel", { surface: "panel", width: CLAUDE_RAIL_PILL_W, height });
    const fits = PANEL_HEIGHT - REPLY_SURFACE_BOTTOM_PAD * 2;
    // Resting pill, rail opened, and a few lines of text share one window.
    expect([at(CLAUDE_PILL_H).windowH, at(118).windowH, at(fits).windowH]).toEqual([
      PANEL_HEIGHT, PANEL_HEIGHT, PANEL_HEIGHT,
    ]);
    // Outgrowing it steps by whole windows, never per line.
    expect(at(fits + 1).windowH).toBe(PANEL_HEIGHT + REPLY_SURFACE_WINDOW_STEP);
    expect(at(fits + REPLY_SURFACE_WINDOW_STEP).windowH).toBe(PANEL_HEIGHT + REPLY_SURFACE_WINDOW_STEP);
    const long = at(500);
    expect(long.windowH).toBe(PANEL_HEIGHT + REPLY_SURFACE_WINDOW_STEP * 2);
    expect(long.cardH).toBe(500);
    expect(long.padY).toBe(long.windowH - 500 - REPLY_SURFACE_BOTTOM_PAD);
  });

  it("sizes reply windows for a five-line reply up front, so typing never resizes them", () => {
    const fits = PANEL_HEIGHT - REPLY_SURFACE_BOTTOM_PAD * 2;
    // Five 13px lines at 1.45, then the field scrolls.
    expect(REPLY_RAIL_CARD_TEXT_MAX_H).toBe(97);
    // The pill at its tallest (identity row, border, divider, the rail row's
    // padding and the rail at its cap) still fits its fixed window.
    expect(CLAUDE_PILL_H + 2 + 1 + 6 + 10 + REPLY_RAIL_CARD_MAX_H).toBeLessThanOrEqual(fits);
    // The glance card reserves its rail's remaining growth: the window it
    // opens with is the one it has with the field full, card bottom-pinned.
    const resting = 150;
    const reserve = REPLY_RAIL_CARD_MAX_H - 16;
    const opened = hudModeGeometry("summary", {
      surface: "summary", width: GLANCE_CARD_W, height: resting, growthReserveH: reserve,
    });
    const full = hudModeGeometry("summary", {
      surface: "summary", width: GLANCE_CARD_W, height: resting + reserve, growthReserveH: 0,
    });
    expect(opened.windowH).toBe(full.windowH);
    expect(opened.windowH).toBeGreaterThan(PANEL_HEIGHT);
    expect(opened.padY).toBe(opened.windowH - resting - REPLY_SURFACE_BOTTOM_PAD);
    // A card that cannot take a reply reserves nothing.
    expect(
      hudModeGeometry("summary", { surface: "summary", width: GLANCE_CARD_W, height: resting }).windowH,
    ).toBe(PANEL_HEIGHT);
  });

  it("never shrinks a visible reply window when its card gets shorter", () => {
    const fits = PANEL_HEIGHT - REPLY_SURFACE_BOTTOM_PAD * 2;
    const grown = PANEL_HEIGHT + REPLY_SURFACE_WINDOW_STEP;
    const card = (height: number, surface: "panel" | "summary" = "panel") => ({
      surface, width: CLAUDE_RAIL_PILL_W, height, retainedWindowH: grown,
    });
    // The Sent rail or a shorter final: the window keeps its grown height and
    // the card stays pinned to its bottom edge.
    const shorter = hudModeGeometry("panel", card(fits - 40));
    expect(shorter.windowH).toBe(grown);
    expect(shorter.padY).toBe(grown - (fits - 40) - REPLY_SURFACE_BOTTOM_PAD);
    // A switch to the glance card keeps it too, before and after its first
    // measurement, at the same width.
    expect(hudModeGeometry("summary", card(fits - 40)).windowH).toBe(grown);
    expect(hudModeGeometry("summary", card(RESPONSE_MIN_H, "summary")).windowH).toBe(grown);
    // The console's measurement, still current while collapsing to the
    // glance card, never sizes the glance window.
    expect(
      hudModeGeometry("summary", { surface: "response", width: CLAUDE_CARD_W, height: 630 }).windowH,
    ).toBe(PANEL_HEIGHT);
    expect(MODE_GEOMETRY.summary.windowW).toBe(MODE_GEOMETRY.panel.windowW);
    expect(MODE_GEOMETRY.summary.windowH).toBe(MODE_GEOMETRY.panel.windowH);
  });

  it("never lets a stale draft echo overwrite newer keystrokes", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const session = managedSession("managed-echo", { latest_response: "Response" });
    emit({ type: "agent.sessions", sessions: [session] });
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();

    const editor = screen.getByRole("textbox", { name: "Edit your reply" });
    fireEvent.change(editor, { target: { value: "hello" } });
    fireEvent.change(editor, { target: { value: "hello world, still typing" } });
    // A late echo of the first update, and an unrelated incoming response.
    emit(replySnapshot([replyDraftFor(session.reply_target!, { phase: "editing", text: "hello", text_revision: 1 })]));
    emit({ type: "agent.sessions", sessions: [{ ...session, latest_response: "A new message" }] });
    await flushPromises();

    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveValue(
      "hello world, still typing",
    );
  });

  it("opens a CLI-managed session after the hook response has expired", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const session = managedSession("managed-one", {
      agent: "codex",
      latest_response: "Persistent response from the PTY session",
    });
    emit({ type: "agent.sessions", sessions: [session] });
    await flushPromises();
    expect(screen.queryByTestId("claude-expanded-card")).toBeNull();

    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();

    expect(screen.getByTestId("claude-expanded-card")).toHaveTextContent(
      "Persistent response from the PTY session",
    );
    expect(screen.getByTestId("claude-expanded-card")).toHaveTextContent("Codex");
    const editor = screen.getByRole("textbox", { name: "Edit your reply" });
    expect(editor).toHaveFocus();

    mocks.windowCalls.length = 0;
    mocks.invoke.mockClear();
    // Typing and Enter go straight out; nothing waits for a draft to exist.
    fireEvent.change(editor, { target: { value: "Already typing" } });
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(sentCommands("agent.draft.update")).toEqual([
      expect.objectContaining({ target_id: "conv:managed-one-conv", base_revision: 0, text: "Already typing" }),
    ]);
    expect(sentCommands("agent.draft.submit")).toEqual([
      expect.objectContaining({ target_id: "conv:managed-one-conv", revision: 1, text: "Already typing" }),
    ]);
    expect(
      mocks.invoke.mock.calls.some(([command]) => command === "set_hud_window_region"),
    ).toBe(false);
  });

  it("opens the editable managed console while its hook response is still in grace", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const session = managedSession("managed-grace", {
      latest_response: "A managed response that is still in grace",
    });
    emit({ type: "agent.sessions", sessions: [session] });
    // Hook items carry the conversation handle, not the session handle.
    emit(responseSnapshotWith(response("managed-grace-response", 1), 1, {
      sessionHandle: session.conversation_handle!,
    }));
    await flushPromises();

    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();

    expect(screen.getByTestId("claude-expanded-card")).toHaveTextContent(
      "A managed response that is still in grace",
    );
    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveFocus();
  });

  it("switches managed tabs from the focused composer and preserves each draft", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const first = managedSession("managed-one", { source_name: "one", latest_response: "First response" });
    const second = managedSession("managed-two", { source_name: "two", latest_response: "Second response" });
    emit({ type: "agent.sessions", sessions: [first, second] });
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    fireEvent.focus(window);
    fireEvent.change(screen.getByRole("textbox", { name: "Edit your reply" }), {
      target: { value: "Keep this draft" },
    });
    emit(replySnapshot([replyDraftFor(first.reply_target!, { phase: "editing", text: "Keep this draft" })]));
    await flushPromises();

    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: /^Claude, one\./ })).not.toHaveAttribute(
      "aria-disabled",
    );

    fireEvent.keyDown(document, { key: "2", ctrlKey: true });
    await flushPromises();

    expect(screen.getByRole("tab", { name: /^Claude, two\./ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveValue("");

    fireEvent.click(screen.getByRole("tab", { name: /^Claude, one\./ }));
    await flushPromises();
    expect(screen.getByRole("tab", { name: /^Claude, one\./ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveValue(
      "Keep this draft",
    );
  });

  it.each([
    { permanentIndicator: false, outcome: "hides the HUD" },
    { permanentIndicator: true, outcome: "restores the permanent indicator" },
  ])("releases a submitted managed console and $outcome", async ({ permanentIndicator }) => {
    if (permanentIndicator) {
      localStorage.setItem("ws-recording-control-visible", "true");
    }
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const session = managedSession("managed-submit");
    emit({ type: "agent.sessions", sessions: [session] });
    emit(replySnapshot());
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();

    const input = screen.getByRole("textbox", { name: "Edit your reply" });
    fireEvent.change(input, { target: { value: "send this" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(sentCommands("agent.draft.submit")).toEqual([
      expect.objectContaining({ target_id: session.reply_target, text: "send this" }),
    ]);

    // The backend's correlated receipt for this exact draft shows Sent for
    // the hold (owner decision 9), then releases the console.
    emit(replySnapshot([], [{ target_id: session.reply_target!, draft_id: "d9", result: "sent", text: "send this" }]));
    await flushPromises();
    expect(screen.getByTestId("claude-expanded-card")).toHaveTextContent("Sent");
    await advance(SENT_REPLY_HOLD_MS);
    // It fades out whole at its own size before it collapses.
    expect(screen.getByTestId("claude-expanded-card")).toHaveTextContent("Sent");
    expect(screen.getByTestId("hud-root")).toHaveStyle({ opacity: "0" });
    await advance(HIDE_ANIMATION_MS);

    expect(screen.queryByTestId("claude-expanded-card")).toBeNull();
    if (permanentIndicator) {
      expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
      expect(mocks.state.physicallyVisible).toBe(true);
    } else {
      expect(screen.queryByTestId("compact-claude-pill")).toBeNull();
      expect(mocks.state.physicallyVisible).toBe(false);
    }
  });

  it("keeps an ended session's console open with its text, the reason and Copy only", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const session = managedSession("managed-ended");
    emit({ type: "agent.sessions", sessions: [session] });
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    emit(replySnapshot([replyDraftFor(session.reply_target!, { phase: "editing", text: "unsent words" })]));
    await flushPromises();
    // The wrapper exits: its session leaves the list, the draft stays.
    emit({ type: "agent.sessions", sessions: [] });
    emit(replySnapshot([replyDraftFor(session.reply_target!, { phase: "editing", text: "unsent words", delivery: "none" })]));
    await flushPromises();

    const card = screen.getByTestId("claude-expanded-card");
    expect(card).toHaveTextContent("unsent words");
    expect(screen.getByTestId("claude-reply-hint")).toHaveAttribute("data-state", "ended");
    expect(screen.getByTestId("claude-reply-copy")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Edit your reply" })).toBeNull();
    // It routes nothing: push-to-talk falls through to ordinary dictation.
    expect(sentCommands("agent.hud.presented").at(-1)).toEqual(
      expect.objectContaining({ target_id: null }),
    );
  });

  it("keeps a console open while its draft and session change underneath it", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const session = managedSession("managed-cancel");
    emit({ type: "agent.sessions", sessions: [session] });
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    emit(replySnapshot([replyDraftFor(session.reply_target!, { phase: "editing", text: "keep me" })]));
    emit({ type: "agent.sessions", sessions: [{ ...session, latest_response: "Newer" }] });
    emit(replySnapshot([replyDraftFor(session.reply_target!, { phase: "recording", compose_generation: 3, text: "keep me" })]));
    await flushPromises();

    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
  });

  it("does not dismiss a managed console on double-click", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit({
      type: "agent.sessions",
      sessions: [
        {
          session_handle: "managed-double-click",
          agent: "claude",
          source_name: "whisperstream",
          unread: false,
          needs_input: false,
          can_reply: true,
          latest_response: null,
        },
      ],
    });
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();

    fireEvent.doubleClick(screen.getByTestId("hud-root"));
    await flushPromises();

    const card = screen.getByTestId("claude-expanded-card");
    expect(card).toBeInTheDocument();
    // Still the console: it fills its centered window.
    expect(card).toHaveAttribute("data-console", "true");
    expect(card.style.width).toBe(`calc(100vw - ${CONSOLE_PAD_X * 2}px)`);
  });

  it("keeps the console window still when switching to a shorter session", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 560 };
    await mountHud();
    const first = {
      session_handle: "managed-tall",
      agent: "claude" as const,
      source_name: "tall",
      unread: false,
      needs_input: false,
      can_reply: true,
      latest_response: "A long response",
    };
    const second = {
      ...first,
      session_handle: "managed-short",
      source_name: "short",
      latest_response: "Short",
    };
    emit({ type: "agent.sessions", sessions: [first, second] });
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    const consoleWindow = `${CONSOLE_DEFAULT_W + CONSOLE_PAD_X * 2}x${CONSOLE_DEFAULT_H + CONSOLE_PAD_Y * 2}`;
    expect(
      mocks.windowCalls.filter((call) => call.startsWith("bounds:")).pop(),
    ).toContain(consoleWindow);

    mocks.windowCalls.length = 0;
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 300 };
    fireEvent.click(screen.getByRole("tab", { name: /^Claude, short\./ }));
    await flushPromises();

    expect(screen.getByRole("tab", { name: /^Claude, short\./ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    // The console's size is the user's, not the content's: no native resize.
    expect(mocks.windowCalls.some((call) => call.startsWith("bounds:"))).toBe(false);
    expect(mocks.windowCalls.some((call) => call.startsWith("size:"))).toBe(false);
  });

  it("dismisses a managed console when the backend's Escape closes it", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const session = managedSession("managed-escape", { can_reply: false, accepts_reply: false });
    emit({ type: "agent.sessions", sessions: [session] });
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();

    // The HUD reports the console it actually painted; the backend owns Escape.
    expect(sentCommands("agent.hud.presented").at(-1)).toEqual(
      expect.objectContaining({ target_id: session.reply_target, surface: "console", painted: true }),
    );
    fireEvent.keyDown(window, { key: "Escape" });
    await flushPromises();
    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();

    emit({ type: "agent.reply.close", version: 1, target_id: session.reply_target!, surface: "console" });
    await flushPromises();
    // The dismissed console fades out at its own size; it takes no keys while
    // it does, and the native window stays put until the fade ends.
    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
    expect(screen.getByTestId("hud-root")).toHaveStyle({ opacity: "0" });
    expect(mocks.state.physicallyVisible).toBe(true);
    expect(sentCommands("agent.hud.presented").at(-1)).toEqual(
      expect.objectContaining({ target_id: null }),
    );
    await advance(HIDE_ANIMATION_MS);

    expect(screen.queryByTestId("claude-expanded-card")).not.toBeInTheDocument();
    expect(screen.queryByTestId("hud-pill-vessel")).not.toBeInTheDocument();
    expect(mocks.state.physicallyVisible).toBe(false);
    expect(sentCommands("agent.hud.presented").at(-1)).toEqual(
      expect.objectContaining({ target_id: null, surface: null, painted: false }),
    );
  });

  it("shows the draft a hook card started in the same session's console", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const session = managedSession("managed-shared", { latest_response: "Earlier response" });
    emit({ type: "agent.sessions", sessions: [session] });
    emit(responseSnapshotWith(response("shared-hook", 1), 1, { sessionHandle: session.conversation_handle! }));
    emit(replySnapshot([replyDraftFor(session.reply_target!, { phase: "editing", text: "carried over text" })]));
    await flushPromises();

    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();

    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
    expect(screen.getByDisplayValue("carried over text")).toBeInTheDocument();
  });

  it("renders Codex completion, speech, and Stop-reply processing with Codex identity", async () => {
    mocks.claudeCardSize = { width: CLAUDE_PILL_MIN_W, height: CLAUDE_PILL_H };
    await mountHud();
    const codex = {
      ...spokenResponse("codex-response", 1),
      agent: "codex" as const,
      source_label: "Codex project",
      source_name: "Codex",
    };

    const speaking = responseSnapshotWith(codex, 1, { phase: "active_speaking", sessionHandle: "codex-conv" });
    emit({ ...speaking, active: { ...speaking.active!, agent: "codex" } });
    await flushPromises();
    let pill = screen.getByTestId("compact-claude-pill");
    expect(pill).toHaveTextContent("Codex");
    expect(pill).not.toHaveTextContent("Claude");
    expect(pill.querySelector('[data-agent-mark="codex"]')).not.toBeNull();
    expect(pill.querySelector('[data-agent-mark="claude"]')).toBeNull();
    expect(document.querySelector('[data-agent-glyph="speaking"]')).not.toBeNull();

    const grace = responseSnapshotWith(codex, 2, { sessionHandle: "codex-conv" });
    emit({ ...grace, active: { ...grace.active!, agent: "codex" } });
    emit(replySnapshot([replyDraftFor("conv:codex-conv", { phase: "processing", compose_generation: 8 })]));
    await flushPromises();
    pill = screen.getByTestId("compact-claude-pill");
    expect(pill).toHaveTextContent("Codex");
    expect(
      pill.querySelector('[data-testid="dictation-processing-spinner"]'),
    ).not.toBeNull();

    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    const card = screen.getByTestId("claude-expanded-card");
    expect(card).toHaveTextContent("Codex");
    expect(card).not.toHaveTextContent("Claude");
    expect(card.querySelector('[data-agent-mark="codex"]')).not.toBeNull();
  });

  it("expands a question through the view shortcut", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 322 };
    await mountHud();

    emit(questionSnapshot(1));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();

    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();

    expect(screen.getByTestId("agent-question-panel")).toBeInTheDocument();
  });

  it("reports a question covering the console, and returns to the kept draft after it", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const session = managedSession("managed-covered");
    emit({ type: "agent.sessions", sessions: [session] });
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    fireEvent.change(screen.getByRole("textbox", { name: "Edit your reply" }), {
      target: { value: "half a thought" },
    });
    emit(replySnapshot([replyDraftFor(session.reply_target!, { phase: "editing", text: "half a thought" })]));
    await flushPromises();

    // A question paints on top: the HUD now reports the question, so the
    // covered console owns no key.
    emit(questionSnapshot(1, { phase: "grace" }));
    await flushPromises();
    expect(sentCommands("agent.hud.presented").at(-1)).toEqual(
      expect.objectContaining({ target_id: "question:handle:1", surface: "question" }),
    );

    emit({ ...questionSnapshot(2), active: null });
    await flushPromises();
    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveValue("half a thought");
  });

  it("reports native focus truth with what it painted", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(responseSnapshotWith(response("focus-card", 1), 1, { sessionHandle: "focus-conv" }));
    await flushPromises();
    fireEvent.blur(window);
    await flushPromises();
    expect(sentCommands("agent.hud.presented").at(-1)).toEqual(
      expect.objectContaining({ target_id: "conv:focus-conv", surface: "card", painted: true, focused: false }),
    );
    fireEvent.focus(window);
    await flushPromises();
    expect(sentCommands("agent.hud.presented").at(-1)).toEqual(
      expect.objectContaining({ target_id: "conv:focus-conv", focused: true }),
    );
  });

  it("opens the console on session N with Ctrl+N from the glance card", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const first = managedSession("glance-one", { source_name: "one", latest_response: "One" });
    const second = managedSession("glance-two", { source_name: "two", latest_response: "Two" });
    emit({ type: "agent.sessions", sessions: [first, second] });
    emit(responseSnapshotWith(response("glance-hook", 1), 1, { sessionHandle: first.conversation_handle! }));
    await flushPromises();
    expect(screen.queryByTestId("claude-expanded-card")).toBeNull();

    fireEvent.focus(window);
    fireEvent.keyDown(document, { key: "2", ctrlKey: true });
    await flushPromises();

    expect(screen.getByTestId("claude-expanded-card")).toHaveTextContent("Two");
    expect(screen.getByRole("tab", { name: /^Claude, two\./ })).toHaveAttribute("aria-selected", "true");
  });

  it("collapses the console, never retargets it, when its session ends", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const first = managedSession("ending-one", { source_name: "one", latest_response: "One" });
    const second = managedSession("ending-two", { source_name: "two", latest_response: "Two" });
    emit({ type: "agent.sessions", sessions: [first, second] });
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    expect(screen.getByTestId("claude-expanded-card")).toHaveTextContent("One");

    emit({ type: "agent.sessions", sessions: [second] });
    await flushPromises();

    expect(screen.queryByTestId("claude-expanded-card")).toBeNull();
    expect(sentCommands("agent.hud.presented").at(-1)).toEqual(
      expect.objectContaining({ target_id: null }),
    );
  });

  it("keeps the console on its own draft while another conversation's card has one", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const session = managedSession("managed-exact", { agent: "codex", latest_response: "Managed response" });
    emit({ type: "agent.sessions", sessions: [session] });
    emit(replySnapshot([
      replyDraftFor(session.reply_target!, { phase: "editing", text: "Managed draft" }),
      replyDraftFor("conv:unrelated-conv", { phase: "ready", text: "Hook-only draft" }),
    ]));
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    emit(responseSnapshotWith(response("unrelated-hook", 1), 1, { sessionHandle: "unrelated-conv" }));
    await flushPromises();

    const input = screen.getByRole("textbox", { name: "Edit your reply" });
    expect(input).toHaveValue("Managed draft");
    fireEvent.change(input, { target: { value: "Managed edit" } });
    expect(sentCommands("agent.draft.update").at(-1)).toEqual(
      expect.objectContaining({ target_id: session.reply_target, base_revision: 1, text: "Managed edit" }),
    );
    expect(screen.queryByText("Hook-only draft")).toBeNull();
  });

  it("holds a sent response receipt across the authoritative terminal snapshot", async () => {
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 68 };
    await mountHud();
    const completion = response("response-receipt", 1);

    emit(responseSnapshotWith(completion, 1, { sessionHandle: "receipt-conv" }));
    emit(replySnapshot([replyDraftFor("conv:receipt-conv", { text: "Please ship the reviewed build" })]));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toHaveTextContent(
      "Please ship the reviewed build",
    );

    mocks.windowCalls.length = 0;
    emit(replySnapshot([], [{ target_id: "conv:receipt-conv", draft_id: "d1", result: "sent", text: "Please ship the reviewed build" }]));
    emit(terminalResponseSnapshot(2));
    await flushPromises();

    const receipt = screen.getByTestId("compact-claude-pill");
    expect(receipt).toHaveTextContent("Please ship the reviewed build");
    expect(receipt).toHaveTextContent("Sent");
    expect(mocks.windowCalls).not.toContain("hide");
    expect(mocks.windowCalls).not.toContain("native-hide+clear");

    await advance(SENT_REPLY_HOLD_MS - 1);
    expect(screen.getByTestId("compact-claude-pill")).toHaveTextContent("Sent");

    await advance(1);
    expect(screen.queryByTestId("compact-claude-pill")).toBeNull();
  });

  it("keeps an expanded card expanded through its sent receipt, then removes it whole", async () => {
    await mountHud();
    const completion = response("response-expanded-receipt", 1);

    emit(responseSnapshotWith(completion, 1, { sessionHandle: "expanded-conv" }));
    emit(replySnapshot([replyDraftFor("conv:expanded-conv", { text: "Ship the reviewed build" })]));
    await flushPromises();
    fireEvent.doubleClick(screen.getByTestId("hud-root"));
    await flushPromises();
    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();

    emit(replySnapshot([], [{ target_id: "conv:expanded-conv", draft_id: "d1", result: "sent", text: "Ship the reviewed build" }]));
    emit(terminalResponseSnapshot(2));
    await flushPromises();

    // The owner-reported bug: Enter shrank the expanded card to the compact
    // pill while it still showed the sent text.
    expect(screen.queryByTestId("compact-claude-pill")).toBeNull();
    const card = screen.getByTestId("claude-expanded-card");
    expect(card).toHaveTextContent("Ship the reviewed build");
    expect(card).toHaveTextContent("Sent");

    await advance(SENT_REPLY_HOLD_MS);
    expect(screen.queryByTestId("claude-expanded-card")).toBeNull();
    expect(screen.queryByTestId("compact-claude-pill")).toBeNull();
  });

  it("edits a conversation again after an earlier reply to it was sent", async () => {
    await mountHud();
    emit(replySnapshot());
    // The backend keeps recent receipts in every snapshot. An old receipt for
    // this conversation must not end the new edit the moment it starts.
    emit(replySnapshot([], [{ target_id: "conv:again-conv", draft_id: "d1", result: "sent", text: "first reply" }]));
    emit(responseSnapshotWith(spokenResponse("again", 1), 1, { showSummaryText: true, sessionHandle: "again-conv" }));
    await flushPromises();

    fireEvent.click(screen.getByRole("button", { name: "Reply to agent" }));
    await flushPromises();
    const editor = screen.getByRole("textbox", { name: "Edit your reply" });
    fireEvent.change(editor, { target: { value: "second reply" } });
    emit(replySnapshot(
      [replyDraftFor("conv:again-conv", { draft_id: "d2", phase: "editing", text: "second reply", text_revision: 1 })],
      [{ target_id: "conv:again-conv", draft_id: "d1", result: "sent", text: "first reply" }],
    ));
    await flushPromises();
    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveValue("second reply");

    // The new draft's own receipt does end the edit.
    emit(replySnapshot([], [{ target_id: "conv:again-conv", draft_id: "d2", result: "sent", text: "second reply" }]));
    await flushPromises();
    expect(screen.queryByRole("textbox", { name: "Edit your reply" })).toBeNull();
  });

  it("keeps the caret in the reply textarea when the rail around it is pressed", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const session = managedSession("rail-press", { latest_response: "Response" });
    emit({ type: "agent.sessions", sessions: [session] });
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    const editor = screen.getByRole("textbox", { name: "Edit your reply" }) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: "typed" } });
    editor.blur();

    // A press on the field's padding or the hint row below it.
    const pressed = fireEvent.mouseDown(screen.getByTestId("claude-reply-field"));
    expect(pressed).toBe(false);
    expect(editor).toHaveFocus();
    expect(editor.selectionStart).toBe("typed".length);
    // The rail itself never takes focus while it is not a click-to-edit button.
    expect(screen.getByTestId("claude-reply-rail")).not.toHaveAttribute("tabindex");
  });

  it("keeps a glance card through a send whose response ends before its receipt", async () => {
    await mountHud();
    emit(replySnapshot());
    emit(responseSnapshotWith(spokenResponse("glance-send", 1), 1, {
      showSummaryText: true,
      sessionHandle: "glance-conv",
    }));
    await flushPromises();
    fireEvent.click(screen.getByRole("button", { name: "Reply to agent" }));
    await flushPromises();
    const editor = screen.getByRole("textbox", { name: "Edit your reply" });
    fireEvent.change(editor, { target: { value: "looks good" } });
    emit(replySnapshot([replyDraftFor("conv:glance-conv", { phase: "editing", text: "looks good", text_revision: 1 })]));
    await flushPromises();
    fireEvent.keyDown(editor, { key: "Enter" });

    // The response ends before any snapshot shows the draft as sending. The
    // card is held from the submit, so the receipt still has a card to confirm.
    emit(terminalResponseSnapshot(2, "idle"));
    await flushPromises();
    expect(screen.getByTestId("claude-expanded-card")).toHaveTextContent("looks good");

    emit(replySnapshot([], [{ target_id: "conv:glance-conv", draft_id: "draft-conv:glance-conv", result: "sent", text: "looks good" }]));
    await flushPromises();
    const card = screen.getByTestId("claude-expanded-card");
    expect(card).toHaveTextContent("looks good");
    expect(card).toHaveTextContent("Sent");
    expect(screen.queryByTestId("compact-claude-pill")).toBeNull();

    await advance(SENT_REPLY_HOLD_MS);
    expect(screen.queryByTestId("claude-expanded-card")).toBeNull();
    expect(screen.queryByTestId("compact-claude-pill")).toBeNull();
  });

  it("shows Sent in the console for the hold, then collapses it", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(replySnapshot());
    const session = managedSession("console-sent", { latest_response: "Response" });
    emit({ type: "agent.sessions", sessions: [session] });
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    const editor = screen.getByRole("textbox", { name: "Edit your reply" });
    fireEvent.change(editor, { target: { value: "ship it" } });
    fireEvent.keyDown(editor, { key: "Enter" });
    emit(replySnapshot([replyDraftFor(session.reply_target!, { phase: "sending", text: "ship it", text_revision: 1 })]));
    await flushPromises();

    emit(replySnapshot([], [{ target_id: session.reply_target!, draft_id: "d1", result: "sent", text: "ship it" }]));
    await flushPromises();
    // Owner decision 9: Sending, then Sent. The console stays open with the
    // receipt in its rail instead of closing on it.
    const consoleCard = screen.getByTestId("claude-expanded-card");
    expect(consoleCard).toHaveTextContent("ship it");
    expect(consoleCard).toHaveTextContent("Sent");

    await advance(SENT_REPLY_HOLD_MS - 1);
    expect(screen.getByTestId("claude-expanded-card")).toHaveTextContent("Sent");
    await advance(1);
    expect(screen.getByTestId("claude-expanded-card")).toHaveTextContent("Sent");
    await advance(HIDE_ANIMATION_MS);
    expect(screen.queryByTestId("claude-expanded-card")).toBeNull();
  });

  it("shows no sent receipt when the backend did not confirm the send", async () => {
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 68 };
    await mountHud();
    emit(replySnapshot());
    emit(responseSnapshotWith(response("response-unsent", 1), 1, { sessionHandle: "unsent-conv" }));
    emit(replySnapshot([replyDraftFor("conv:unsent-conv", { text: "This expired instead" })]));
    await flushPromises();

    // The card expired or was cancelled: terminal, but nothing was delivered.
    emit(terminalResponseSnapshot(2));
    emit(replySnapshot([replyDraftFor("conv:unsent-conv", { text: "This expired instead" })]));
    await flushPromises();

    expect(screen.queryByText("Sent")).toBeNull();
  });

  it("shows the sent receipt when the backend confirms the exact reply", async () => {
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 68 };
    await mountHud();
    emit(replySnapshot());
    emit(responseSnapshotWith(response("response-confirmed", 1), 1, { sessionHandle: "confirmed-conv" }));
    emit(replySnapshot([replyDraftFor("conv:confirmed-conv", { text: "Confirmed reply" })]));
    await flushPromises();
    emit(replySnapshot([], [{ target_id: "conv:confirmed-conv", draft_id: "d1", result: "sent", text: "Confirmed reply" }]));
    emit(terminalResponseSnapshot(2));
    await flushPromises();

    expect(screen.getByTestId("compact-claude-pill")).toHaveTextContent("Sent");
  });

  it("shows live speech after a kept draft's text while a capture appends to it", async () => {
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 68 };
    await mountHud();
    emit(responseSnapshotWith(response("append", 1), 1, { sessionHandle: "append-conv" }));
    emit(replySnapshot([replyDraftFor("conv:append-conv", { phase: "ready", text: "Ship it first." })]));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toHaveTextContent("Ship it first.");

    emit(lifecycle(5, "recording", { compose_generation: 3 }));
    emit(replySnapshot([replyDraftFor("conv:append-conv", { phase: "recording", compose_generation: 3, text: "Ship it first." })]));
    await flushPromises();
    // Nothing heard yet: the kept text stays instead of blanking the row.
    expect(screen.getByTestId("claude-reply-rail-text")).toHaveTextContent("Ship it first.");

    emit({
      type: "agent.compose.preview",
      version: 1,
      session_handle: "handle",
      generation: 3,
      recording_id: 5,
      revision: 1,
      text: "then the logs",
      stable_char_count: 13,
      presentation_version: 1,
      mutable_start_char_count: 13,
      freeze: false,
    } as AppEvent);
    await flushPromises();
    expect(screen.getByTestId("claude-reply-rail-text")).toHaveTextContent("Ship it first. then the logs");
    // The kept text is plain: it does not replay the live words' reveal.
    expect(screen.getByTestId("claude-reply-kept-text")).toHaveTextContent("Ship it first.");
  });

  it("holds an auto-submitted response receipt from processing to terminal", async () => {
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 68 };
    await mountHud();
    const completion = response("auto-response-receipt", 1);
    emit(replySnapshot());

    emit(lifecycle(9, "recording", { origin: "push_to_talk" }));
    emit({
      type: "agent.compose.state",
      version: 1,
      session_handle: "handle",
      generation: 4,
      recording_id: 9,
      state: "recording",
    } as AppEvent);
    emit(responseSnapshotWith(completion, 1, { sessionHandle: "auto-conv" }));
    emit(replySnapshot([replyDraftFor("conv:auto-conv", { phase: "recording", compose_generation: 4, text: "" })]));
    emit({
      type: "agent.compose.preview",
      version: 1,
      session_handle: "handle",
      generation: 4,
      recording_id: 9,
      revision: 1,
      text: "Use the automatic route",
      stable_char_count: 23,
      presentation_version: 1,
      mutable_start_char_count: 23,
      freeze: false,
    } as AppEvent);
    emit(lifecycle(9, "processing", { origin: "push_to_talk" }));
    emit(replySnapshot([replyDraftFor("conv:auto-conv", { phase: "processing", compose_generation: 4, text: "" })]));
    await flushPromises();

    emit(lifecycle(9, "idle", { origin: "push_to_talk" }));
    emit(replySnapshot([], [{ target_id: "conv:auto-conv", draft_id: "d4", result: "sent", text: "Use the automatic route" }]));
    emit(terminalResponseSnapshot(3, "idle"));
    await flushPromises();

    const receipt = screen.getByTestId("compact-claude-pill");
    expect(receipt).toHaveTextContent("Use the automatic route");
    expect(receipt).toHaveTextContent("Sent");

    await advance(SENT_REPLY_HOLD_MS);
    expect(screen.queryByTestId("compact-claude-pill")).toBeNull();
  });

  it("lets a newer active response supersede a stale sent receipt", async () => {
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 68 };
    await mountHud();
    emit(replySnapshot());
    emit(responseSnapshotWith(response("previous-response", 1), 1, { sessionHandle: "old-conv" }));
    emit(replySnapshot([replyDraftFor("conv:old-conv", { phase: "editing", text: "Old reply text" })]));
    await flushPromises();
    emit(replySnapshot([], [{ target_id: "conv:old-conv", draft_id: "d1", result: "sent", text: "Old reply text" }]));
    emit(terminalResponseSnapshot(2));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toHaveTextContent("Sent");

    const next = response("next-response", 2);
    emit(responseSnapshotWith(next, 3));
    await flushPromises();

    const nextPill = screen.getByTestId("compact-claude-pill");
    expect(nextPill).not.toHaveTextContent("Sent");
    expect(nextPill).not.toHaveTextContent("Old reply text");

    await advance(SENT_REPLY_HOLD_MS);
    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
  });

  // --- Speaking questions and the shared 16px trailing slot ---
  it("gives a speaking question the same stop control as a response", async () => {
    // The question pill used to hardcode `speaking={false}`, so a spoken
    // question had no way to be silenced at all.
    mocks.claudeCardSize = { width: CLAUDE_PILL_MIN_W, height: CLAUDE_PILL_H };
    await mountHud();

    emit(questionSnapshot(1, { phase: "surfaced" }));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
    expect(document.querySelector('[data-agent-glyph="speaking"]')).toBeNull();

    emit(questionSnapshot(2, { generation: 1, phase: "speaking" }));
    await flushPromises();

    expect(document.querySelector('[data-agent-glyph="speaking"]')).not.toBeNull();

    emit(questionSnapshot(3, { generation: 1, phase: "grace" }));
    await flushPromises();
    expect(document.querySelector('[data-agent-glyph="speaking"]')).toBeNull();
  });

  // --- Answering a question by voice is a compact, quiet path ---
  it("answers a question in the compact pill instead of expanding the card", async () => {
    // Routed capture used to force `expandedQuestionGeneration`, so the moment
    // the user started speaking the whole question card unfolded - and because
    // `hudEditing` follows expansion, the HUD also took native foreground out
    // of whatever they were typing in. Speaking a reply is now exactly what it
    // is for a response: the compact pill grows its rail and nothing else.
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 68 };
    await mountHud();

    emit(questionSnapshot(1, { phase: "grace", composeGeneration: 4 }));
    await flushPromises();
    emit(lifecycle(9, "recording", { origin: "push_to_talk" }));
    emit({
      type: "agent.compose.state",
      version: 1,
      session_handle: "handle",
      generation: 4,
      recording_id: 9,
      state: "recording",
    } as AppEvent);
    emit(questionSnapshot(2, { generation: 1, phase: "recording", composeGeneration: 4 }));
    emit(replySnapshot([replyDraftFor("question:handle:1", { phase: "recording", compose_generation: 4 })]));
    emit({
      type: "agent.compose.preview",
      version: 1,
      session_handle: "handle",
      generation: 4,
      recording_id: 9,
      revision: 1,
      text: "Staging please",
      stable_char_count: 14,
      presentation_version: 1,
      mutable_start_char_count: 14,
      freeze: false,
    } as AppEvent);
    await flushPromises();

    expect(screen.queryByTestId("agent-question-panel")).toBeNull();
    const pill = screen.getByTestId("compact-claude-pill");
    expect(pill).toBeInTheDocument();
    expect(pill.textContent).toContain("Staging please");
  });

  it("keeps an unsubmitted question draft compact and readable", async () => {
    // With auto-submit off the draft waits for a confirmation. It waits in the
    // rail, showing what would be sent, rather than reopening the card.
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 68 };
    await mountHud();

    emit(questionSnapshot(1, { phase: "draft_ready" }));
    emit(replySnapshot([replyDraftFor("question:handle:1", { text: "Staging please" })]));
    await flushPromises();

    expect(screen.queryByTestId("agent-question-panel")).toBeNull();
    expect(screen.getByTestId("compact-claude-pill").textContent).toContain(
      "Staging please",
    );
  });

  it("clears a successful answer faster than a failed one", async () => {
    // The dwells used to be inverted: expired and held_failed each lingered
    // TERMINAL_QUESTION_DWELL_MS so the failure could be read, while a
    // successful answer was released instantly with no confirmation at all.
    expect(SENT_REPLY_HOLD_MS).toBeLessThan(TERMINAL_QUESTION_DWELL_MS);
  });

  // --- The reply rail's text box is bounded and scrolls, never truncated ---
  it("allows long reply text to grow within the screen budget without clamping it", () => {
    // Live text used to grow without a bound, so a long dictated reply turned
    // the HUD into a tall moving object; draft and sent went the other way and
    // hard-clamped with -webkit-line-clamp, so the tail of your own reply was
    // unreachable. One bounded scrolling box replaces both.
    expect(REPLY_RAIL_TEXT_MAX_H).toBeLessThanOrEqual(CLAUDE_RAIL_MAX_H);
    expect(REPLY_RAIL_TEXT_MAX_H).toBeGreaterThan(300);

    for (const phase of ["live", "processing", "draft", "sent"] as const) {
      cleanup();
      render(
        <ReplyRail
          phase={phase}
          text="A reply long enough to run past three lines of the rail."
          stableCharCount={10}
        />,
      );
      const box = screen.getByTestId("claude-reply-rail-text");
      expect(box.style.maxHeight).toBe(`${REPLY_RAIL_TEXT_MAX_H}px`);
      expect(box.style.overflowY).toBe("auto");
      expect(box.style.webkitLineClamp ?? "").toBe("");
    }
  });

  it("leaves the editing textarea to own its own growth", () => {
    // The textarea auto-grows by measured scrollHeight; wrapping it in a second
    // bounded scroller would give the caret two competing scroll containers.
    render(<ReplyRail phase="editing" editingValue="typed" />);
    const box = screen.getByTestId("claude-reply-rail-text");
    const input = screen.getByRole("textbox", { name: "Edit your reply" });
    expect(box.style.maxHeight).toBe("");
    expect(box.style.overflowY).toBe("");
    expect(input).toHaveStyle({
      height: "21px",
      minHeight: "21px",
      outline: "none",
      boxShadow: "none",
    });
    // The field keeps one box and inset in every phase, framed only while
    // typing, so text never shifts when editing starts; multi-line text keeps
    // the key cap on the last line.
    expect(screen.getByTestId("claude-reply-field")).toHaveClass("items-end");
    expect(screen.getByTestId("claude-reply-field")).toHaveStyle({
      minHeight: "34px",
      padding: "6px 6px 6px 12px",
    });
    expect(screen.queryByText(/Enter to send/)).not.toBeInTheDocument();

    Object.defineProperty(input, "scrollHeight", {
      configurable: true,
      value: 57,
    });
    fireEvent.change(input, { target: { value: "a wrapped reply" } });
    expect(input).toHaveStyle({ height: "57px", overflowY: "hidden" });
  });

  it("holds the sent receipt for an auto-submitted answer", async () => {
    // Auto-submit is resolved by the backend, so this surface never runs
    // `submitQuestionAnswers` and the submitted-text ref stays empty. Reading
    // only that ref treated auto-submit as "answered somewhere else" and
    // released instantly - which killed the confirmation in the one mode whose
    // entire justification is that the user never sees a draft.
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 68 };
    await mountHud();

    emit(questionSnapshot(1, { phase: "draft_ready" }));
    emit(replySnapshot([replyDraftFor("question:handle:1", { text: "Staging" })]));
    await flushPromises();

    emit({
      ...questionSnapshot(2, { generation: 1, phase: "answered" }),
      active: {
        ...questionSnapshot(2, { generation: 1, phase: "answered" }).active!,
        answers: { "Which environment should I deploy to?": "Staging" },
      },
    });
    await flushPromises();

    const pill = screen.getByTestId("compact-claude-pill");
    expect(pill.textContent).toContain("Staging");
    expect(pill.textContent).toContain("Sent");

    act(() => {
      vi.advanceTimersByTime(SENT_REPLY_HOLD_MS + 50);
    });
    await flushPromises();
    expect(screen.queryByTestId("compact-claude-pill")).toBeNull();
    expect(mocks.send).toHaveBeenCalledWith({
      type: "agent.question.ack",
      version: 1,
      session_handle: "handle",
      generation: 1,
    });
  });

  it("holds no receipt for a question answered outside the HUD", async () => {
    // The confirmation shows what THIS surface sent. A question answered
    // directly in Claude Code reaches `answered` with nothing to echo, and must
    // release immediately rather than inventing a receipt.
    mocks.claudeCardSize = { width: CLAUDE_PILL_MIN_W, height: CLAUDE_PILL_H };
    await mountHud();

    emit(questionSnapshot(1));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();

    emit(questionSnapshot(2, { generation: 1, phase: "answered" }));
    await flushPromises();

    expect(screen.queryByTestId("compact-claude-pill")).toBeNull();
    expect(mocks.send).toHaveBeenCalledWith({
      type: "agent.question.ack",
      version: 1,
      session_handle: "handle",
      generation: 1,
    });
  });

  // --- The region-bleed invariant (jagged bottom corners) ---
  // The elevation is a downward-offset drop shadow. `CreateRoundRectRgn` is a
  // binary, unantialiased clip, so if the region boundary falls anywhere the
  // shadow is still painting, the cut shows up as a staircase - at the bottom
  // corners only, because that is the only side a downward shadow reaches far.
  it("keeps the region bleed wide enough for the elevation shadow", () => {
    expect(HUD_REGION_EDGE_BLEED).toBeGreaterThanOrEqual(HUD_ELEVATION_EXTENT);
  });

  it("keeps the region bleed inside every window's own pad", () => {
    // A bleed wider than the pad would push the region past its window, and
    // Windows would clip it back to the window rect - the same hard cut, just
    // relocated. The binding pad is the expanded-control family's 8px padY.
    const pads = Object.values(MODE_GEOMETRY).flatMap((geometry) => [
      geometry.padX,
      geometry.padY,
    ]);
    expect(HUD_REGION_EDGE_BLEED).toBeLessThanOrEqual(Math.min(...pads));
  });

  it("never asks the native validator for an impossible corner radius", () => {
    // lib.rs rejects `corner_radius > width.min(height) / 2`, and a rejected
    // command aborts region, bounds, and show together. A card measured
    // mid-transition can be shorter than its target radius implies.
    const shortRailPill = hudWindowRegionForMode(
      "panel",
      {
        surface: "panel",
        width: CLAUDE_RAIL_PILL_W,
        height: CLAUDE_PILL_H,
        radius: CLAUDE_RAIL_PILL_RADIUS,
      },
      null,
    );
    expect(shortRailPill).not.toBeNull();
    expect(shortRailPill!.cornerRadius).toBeLessThanOrEqual(
      Math.min(shortRailPill!.width, shortRailPill!.height) / 2,
    );
  });

  // --- REGRESSION: the multi-turn wedge (#346 tried and failed to close it) ---
  it("keeps showing the pill across three turns with an expand in between", async () => {
    mocks.claudeCardSize = { width: CLAUDE_PILL_MIN_W, height: CLAUDE_PILL_H };
    await mountHud();

    // Turn 1 arrives and shows.
    emit(responseSnapshotWith(response("turn-1", 1), 1));
    await flushPromises();
    // The compact pill is a clean reply surface: an ordinary Tauri show.
    expect(mocks.windowCalls).toContain("show");

    // The user expands it and collapses it again.
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();

    // Turns 2 and 3 must each still reach a visible window. Before the fix this
    // is where the HUD went quiet for the rest of the session.
    for (const [index, id] of ["turn-2", "turn-3"].entries()) {
      mocks.windowCalls.length = 0;
      emit(responseSnapshotWith(response(id, index + 2), index + 2));
      await advance(400);
      expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
      const hidden = mocks.windowCalls.lastIndexOf("hide");
      const shown = mocks.windowCalls.findIndex((call) =>
        call === "show" || call.startsWith("show+"),
      );
      expect(
        hidden === -1 || shown > hidden,
        `${id} left the window hidden: ${JSON.stringify(mocks.windowCalls)}`,
      ).toBe(true);
    }
  });

  it("clamps the one measured card between each surface's own bounds", () => {
    const questionCeiling = claudeCardCeiling(QUESTION_MIN_H, QUESTION_MAX_H);

    // The console's size never follows its content.
    expect(
      hudModeGeometry("response", {
        surface: "response",
        width: CLAUDE_CARD_W,
        height: 404,
      }).cardH,
    ).toBe(CONSOLE_DEFAULT_H);
    expect(
      hudModeGeometry("response", {
        surface: "response",
        width: CLAUDE_CARD_W,
        height: 40,
      }).cardH,
    ).toBe(CONSOLE_DEFAULT_H);
    expect(
      hudModeGeometry("summary", {
        surface: "summary",
        width: CLAUDE_CARD_W,
        height: 40,
      }).cardH,
    ).toBe(40);
    expect(
      hudModeGeometry("summary", {
        surface: "summary",
        width: GLANCE_CARD_W,
        height: 4000,
      }).cardH,
    ).toBe(claudeCardCeiling(0, RESPONSE_MAX_H));
    expect(
      hudModeGeometry("question", {
        surface: "question",
        width: CLAUDE_CARD_W,
        height: 4000,
      }).cardH,
    ).toBe(questionCeiling);

    // Both surfaces share one state, so a measurement belonging to the other
    // surface must never size this one.
    expect(
      hudModeGeometry("question", {
        surface: "response",
        width: CLAUDE_CARD_W,
        height: 500,
      }).cardH,
    ).toBe(QUESTION_MIN_H);

    // The shared width replaces the retired QUESTION_CARD_W (360).
    expect(hudModeGeometry("question", null).cardW).toBe(CLAUDE_CARD_W);
    expect(hudModeGeometry("response", null).cardW).toBe(CONSOLE_DEFAULT_W);
    expect(hudModeGeometry("summary", null).cardW).toBe(GLANCE_CARD_W);
  });

  it("opens the expanded response as a console centered on the display", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();

    await expandResponse(spokenResponse("measured", 1));

    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
    const width = CONSOLE_DEFAULT_W + CONSOLE_PAD_X * 2;
    const height = CONSOLE_DEFAULT_H + CONSOLE_PAD_Y * 2;
    // The 1920x1080 work area of the mocked monitor, centered.
    expect(lastBounds()).toBe(
      `bounds:${(1920 - width) / 2},${(1080 - height) / 2},${width}x${height}`,
    );
  });

  it("sizes the question window from the same measured-card path", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 322 };
    await mountHud();

    emit(questionSnapshot(1));
    await flushPromises();
    expect(screen.getByRole("status")).toHaveAccessibleName(
      expect.stringContaining("Claude asked"),
    );
    expect(screen.queryByTestId("agent-question-panel")).toBeNull();
    fireEvent.doubleClick(screen.getByTestId("hud-root"));
    await flushPromises();

    expect(screen.getByTestId("agent-question-panel")).toBeInTheDocument();
    expect(lastBounds()).toContain(
      `${CLAUDE_CARD_W + QUESTION_PAD_X * 2}x${322 + QUESTION_PAD_Y * 2}`,
    );
  });

  it("shows a compact question in the fixed clean reply window, with no region", async () => {
    mocks.claudeCardSize = {
      width: CLAUDE_PILL_MIN_W,
      height: CLAUDE_PILL_H,
    };
    await mountHud();

    emit(questionSnapshot(1));
    await flushPromises();

    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
    expect(lastBounds()).toContain(`${PANEL_WIDTH}x${PANEL_HEIGHT}`);
    expect(lastRegion()).toBeUndefined();
  });

  it("shows the next Claude surface after a hide that a newer state interrupted", async () => {
    // The dogfooding wedge: once a dwell ended while the next Claude event was
    // already arriving, NO later pill ever appeared again. The hide reached
    // the native window, then found a newer generation and returned before
    // recording that the window was now hidden. Every later reconcile read
    // the stale `visible: true` and skipped its show. The permission pill
    // carries this now: it keeps the shaped region owner whose hide this
    // guards (compact reply surfaces use ordinary Tauri visibility).
    mocks.claudeCardSize = {
      width: CLAUDE_PERMISSION_PILL_W,
      height: CLAUDE_PERMISSION_MIN_H,
    };
    await mountHud();

    emit(pendingPermission(1));
    await flushPromises();
    expect(lastRegion()).toContain("show+");

    // The ask resolves, so the HUD lets go and schedules its hide.
    let releaseHide: () => void = () => undefined;
    mocks.state.hideRegionAttempt = new Promise<void>((resolve) => {
      releaseHide = () => resolve();
    });
    emit({ type: "agent.interactions.snapshot", version: 1, revision: ++permissionSnapshotRevision, interactions: [] });
    await advance(400);

    // A new ask lands while that hide is still inside the native call.
    emit(pendingPermission(2));
    await flushPromises();
    releaseHide();
    await advance(400);

    expect(screen.getByTestId("claude-permission-pill")).toBeInTheDocument();
    // The superseded hide must have left the window recorded as hidden, so the
    // new ask's reconcile still owes a show.
    expect(lastRegion()).toContain("show+region:");
  });

  it("issues no native geometry event when speech starts and stops", async () => {
    // Design decision 15: a state that changes no footprint must issue no
    // native geometry event. The reconciler compares applied numbers, so the
    // measured value either side of a speaking toggle has to be identical.
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const completion = spokenResponse("speaking", 1);
    await expandResponse(completion);
    mocks.windowCalls.length = 0;

    emit(responseSnapshotWith(completion, 2, { phase: "active_speaking" }));
    await flushPromises();
    expect(document.querySelector('[data-agent-glyph="speaking"]')).not.toBeNull();
    expect(mocks.windowCalls).toEqual([]);

    emit(responseSnapshotWith(completion, 3, { phase: "active_grace" }));
    await flushPromises();
    expect(document.querySelector('[data-agent-glyph="speaking"]')).toBeNull();
    expect(mocks.windowCalls).toEqual([]);
  });

  it("issues no native window call when a capture opens the compact pill's reply row", async () => {
    // Owner report 2026-10-24: push-to-talk on the pill flashed and resized
    // the window on press and release. The reply row now opens in CSS inside
    // the fixed reply window, so neither the press nor the phases after it
    // reach the native window.
    mocks.claudeCardSize = { width: CLAUDE_PILL_MIN_W, height: CLAUDE_PILL_H };
    await mountHud();
    emit(responseSnapshotWith(spokenResponse("pill-reply", 1), 1, { sessionHandle: "pill-conv" }));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
    mocks.windowCalls.length = 0;

    // Press: the recording names its lease and the row opens taller.
    mocks.claudeCardSize = { width: CLAUDE_RAIL_PILL_W, height: 118 };
    emit(replySnapshot([replyDraftFor("conv:pill-conv", { phase: "recording", compose_generation: 6 })]));
    emit(lifecycle(90, "recording", { compose_generation: 6 }));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
    expect(mocks.windowCalls).toEqual([]);

    // Release, then the draft lands.
    emit(lifecycle(90, "processing", { compose_generation: 6 }));
    emit(replySnapshot([replyDraftFor("conv:pill-conv", { phase: "processing", compose_generation: 6 })]));
    await flushPromises();
    emit(replySnapshot([replyDraftFor("conv:pill-conv", { phase: "ready", text: "Ship it" })]));
    emit(lifecycle(90, "idle", { compose_generation: 6 }));
    await flushPromises();
    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
    expect(mocks.windowCalls).toEqual([]);
  });

  it("issues no native geometry event from live to processing", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const completion = spokenResponse("rail", 1);
    await expandResponse(completion);

    emit(responseSnapshotWith(completion, 2, { sessionHandle: "rail-conv" }));
    emit(replySnapshot([replyDraftFor("conv:rail-conv", { phase: "recording", compose_generation: 6 })]));
    await flushPromises();
    mocks.windowCalls.length = 0;

    emit(replySnapshot([replyDraftFor("conv:rail-conv", { phase: "processing", compose_generation: 6 })]));
    await flushPromises();
    expect(mocks.windowCalls).toEqual([]);
  });

  it("round-trips the canonical pill anchor through a measured card height", () => {
    const anchor = { x: 812, y: 964 };
    for (const measured of [
      { surface: "response" as const, width: CLAUDE_CARD_W, height: 404 },
      { surface: "question" as const, width: CLAUDE_CARD_W, height: 588 },
    ]) {
      const mode = measured.surface === "response" ? "response" : "question";
      const position = windowPositionFromPillAnchor(mode, anchor, measured);
      expect(pillAnchorFromWindowPosition(mode, position, measured)).toEqual(
        anchor,
      );
      // The painted card sits exactly centered in its window, which is what
      // removes the old 40px pad-versus-centering disagreement.
      const rect = pillRectFromAnchor(mode, anchor, measured);
      const geometry = hudModeGeometry(mode, measured);
      expect(rect.x - position.x).toBe((geometry.windowW - rect.width) / 2);
      expect(rect.y - position.y).toBe((geometry.windowH - rect.height) / 2);
    }
  });
  // --- Task 3.2: a shaped native region for every Claude surface ---
  // `hudWindowRegionForMode` used to return null for every mode outside the
  // control family, and a null region is applied as `restricted: false` - a
  // full, unrounded, window-sized rectangle. That is incident 7's mechanism: the OS clip described
  // the WebView, not the card, so any pixel WebView2 repainted inside it was a
  // legitimate, presentable, hit-testable pixel.

  function lastRegion(): string | undefined {
    const regions = mocks.windowCalls.filter((call) =>
      call.includes("region:"),
    );
    return regions[regions.length - 1];
  }

  it("clips every Claude surface to its measured painted footprint", () => {
    // Card surfaces: the window is card + 2 * pad, so the painted card is
    // exactly centered and the region is that rect plus `HUD_REGION_EDGE_BLEED`.
    // The console is unshaped: it lives in the clean Tauri family.
    expect(
      hudWindowRegionForMode("response", {
        surface: "response",
        width: CLAUDE_CARD_W,
        height: 404,
      }),
    ).toBeNull();
    expect(
      hudWindowRegionForMode("question", {
        surface: "question",
        width: CLAUDE_CARD_W,
        height: 322,
      }),
    ).toEqual({
      x: QUESTION_PAD_X - HUD_REGION_EDGE_BLEED,
      y: QUESTION_PAD_Y - HUD_REGION_EDGE_BLEED,
      width: CLAUDE_CARD_W + HUD_REGION_EDGE_BLEED * 2,
      height: 322 + HUD_REGION_EDGE_BLEED * 2,
      cornerRadius: CLAUDE_CARD_RADIUS + HUD_REGION_EDGE_BLEED,
    });

    // The compact pill keeps its fixed WebView, so its region is the measured
    // pill centered in that window at the pill's own painted radius.
    expect(
      hudWindowRegionForMode("panel", {
        surface: "panel",
        width: 132,
        height: CLAUDE_PILL_H,
        radius: CLAUDE_PILL_RADIUS,
      }),
    ).toEqual({
      x: (PANEL_WIDTH - 132) / 2 - HUD_REGION_EDGE_BLEED,
      y: (PANEL_HEIGHT - CLAUDE_PILL_H) / 2 - HUD_REGION_EDGE_BLEED,
      width: 132 + HUD_REGION_EDGE_BLEED * 2,
      height: CLAUDE_PILL_H + HUD_REGION_EDGE_BLEED * 2,
      cornerRadius: CLAUDE_PILL_RADIUS + HUD_REGION_EDGE_BLEED,
    });

    expect(
      hudWindowRegionForMode("permission", {
        surface: "permission",
        width: CLAUDE_PERMISSION_PILL_W,
        height: 92,
      }),
    ).toEqual({
      x: QUESTION_PAD_X - HUD_REGION_EDGE_BLEED,
      y: QUESTION_PAD_Y - HUD_REGION_EDGE_BLEED,
      width: CLAUDE_PERMISSION_PILL_W + HUD_REGION_EDGE_BLEED * 2,
      height: 92 + HUD_REGION_EDGE_BLEED * 2,
      cornerRadius: CLAUDE_PERMISSION_PILL_RADIUS + HUD_REGION_EDGE_BLEED,
    });

    // Nothing that paints a card falls back to `restricted: false`. A missing
    // measurement takes the previously applied region for the same mode, and
    // only with no previous region at all does it take the mode's resting
    // footprint - which is the same rect the bounds in that command describe,
    // so it can never clip more than the window already does.
    const previous = {
      x: QUESTION_PAD_X - HUD_REGION_EDGE_BLEED,
      y: QUESTION_PAD_Y - HUD_REGION_EDGE_BLEED,
      width: CLAUDE_CARD_W + HUD_REGION_EDGE_BLEED * 2,
      height: 404 + HUD_REGION_EDGE_BLEED * 2,
      cornerRadius: CLAUDE_CARD_RADIUS + HUD_REGION_EDGE_BLEED,
    };
    expect(hudWindowRegionForMode("summary", null, previous)).toEqual(
      previous,
    );
    expect(hudWindowRegionForMode("summary", null)).not.toBeNull();
    expect(hudWindowRegionForMode("question", null)).not.toBeNull();
    expect(hudWindowRegionForMode("panel", null)).not.toBeNull();
    expect(hudWindowRegionForMode("permission", null)).not.toBeNull();

    // A measurement belonging to another surface never shapes this one.
    expect(
      hudWindowRegionForMode("question", {
        surface: "response",
        width: CLAUDE_CARD_W,
        height: 500,
      }),
    ).toEqual(hudWindowRegionForMode("question", null));

    // Preview uses the same shaped-card rule, including work-area-capped
    // expanded geometry. The plain pill remains unrestricted.
    expect(hudWindowRegionForMode("pill")).toBeNull();
    expect(hudWindowRegionForMode("preview")).toEqual({
      x: PREVIEW_PAD_X - HUD_REGION_EDGE_BLEED,
      y: PREVIEW_PAD_Y - HUD_REGION_EDGE_BLEED,
      width: PREVIEW_CARD_W + HUD_REGION_EDGE_BLEED * 2,
      height: PREVIEW_CARD_H + HUD_REGION_EDGE_BLEED * 2,
      cornerRadius: PREVIEW_CARD_RADIUS + HUD_REGION_EDGE_BLEED,
    });
    expect(
      hudWindowRegionForMode("preview", null, null, {
        windowW: 352,
        windowH: 132,
        cardW: 312,
        cardH: 100,
      }),
    ).toEqual({
      x: 20 - HUD_REGION_EDGE_BLEED,
      y: 16 - HUD_REGION_EDGE_BLEED,
      width: 312 + HUD_REGION_EDGE_BLEED * 2,
      height: 100 + HUD_REGION_EDGE_BLEED * 2,
      cornerRadius: PREVIEW_CARD_RADIUS + HUD_REGION_EDGE_BLEED,
    });
    expect(hudWindowRegionForMode("seed")).toEqual({
      x: MODE_GEOMETRY.seed.padX - HUD_REGION_EDGE_BLEED,
      y: MODE_GEOMETRY.seed.padY - HUD_REGION_EDGE_BLEED,
      width: MODE_GEOMETRY.seed.cardW + HUD_REGION_EDGE_BLEED * 2,
      height: MODE_GEOMETRY.seed.cardH + HUD_REGION_EDGE_BLEED * 2,
      cornerRadius:
        (MODE_GEOMETRY.seed.cardH + HUD_REGION_EDGE_BLEED * 2) / 2,
    });
  });

  it("resizes the console from its corner, centered, and remembers the size", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    const width = CONSOLE_DEFAULT_W + CONSOLE_PAD_X * 2;
    const height = CONSOLE_DEFAULT_H + CONSOLE_PAD_Y * 2;
    const innerWidth = Object.getOwnPropertyDescriptor(window, "innerWidth");
    const innerHeight = Object.getOwnPropertyDescriptor(window, "innerHeight");
    Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: height });
    onTestFinished(() => {
      if (innerWidth) Object.defineProperty(window, "innerWidth", innerWidth);
      if (innerHeight) Object.defineProperty(window, "innerHeight", innerHeight);
    });
    await mountHud();
    await expandResponse(spokenResponse("resize", 1));
    expect(screen.getByTestId("console-resize-grip")).toBeInTheDocument();

    const corner = screen.getByTestId("console-resize-se");
    fireEvent.pointerDown(corner, { button: 0, buttons: 1, pointerId: 7, screenX: 500, screenY: 500 });
    await flushPromises();
    fireEvent.pointerMove(corner, { buttons: 1, pointerId: 7, screenX: 550, screenY: 520 });
    await flushPromises();

    // The dragged corner follows the pointer and the console stays centered.
    const grownW = width + 100;
    const grownH = height + 40;
    expect(lastBounds()).toBe(
      `bounds:${(1920 - grownW) / 2},${(1080 - grownH) / 2},${grownW}x${grownH}`,
    );

    fireEvent.pointerUp(corner, { pointerId: 7, screenX: 550, screenY: 520 });
    await flushPromises();
    expect(JSON.parse(localStorage.getItem("hud-console-size") ?? "{}")).toEqual({
      "display@0,0,1920x1080": {
        width: CONSOLE_DEFAULT_W + 100,
        height: CONSOLE_DEFAULT_H + 40,
      },
    });
    // Ending the drag resolves the remembered size: the window does not move.
    expect(lastBounds()).toBe(
      `bounds:${(1920 - grownW) / 2},${(1080 - grownH) / 2},${grownW}x${grownH}`,
    );
  });

  it("drags the console by its identity row only, without touching the pill anchor", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    await expandResponse(spokenResponse("console-drag", 1));
    const root = screen.getByTestId("hud-root");
    Object.defineProperty(root, "setPointerCapture", { configurable: true, value: vi.fn() });
    localStorage.removeItem("hud-position");
    mocks.hudWindow.outerPosition.mockClear();

    // The body (timeline and composer) is not a drag handle.
    fireEvent.pointerDown(screen.getByTestId("claude-card-body"), {
      button: 0, pointerId: 3, screenX: 10, screenY: 10,
    });
    await flushPromises();
    expect(mocks.hudWindow.outerPosition).not.toHaveBeenCalled();

    // The card's identity row is.
    fireEvent.pointerDown(screen.getByTestId("claude-expanded-card"), {
      button: 0, pointerId: 4, screenX: 10, screenY: 10,
    });
    await flushPromises();
    expect(mocks.hudWindow.outerPosition).toHaveBeenCalled();
    fireEvent.pointerUp(root, { pointerId: 4, screenX: 10, screenY: 10 });
    await flushPromises();
    // The console remembers its own offset; the pill's saved spot is untouched.
    expect(localStorage.getItem("hud-position")).toBeNull();
  });

  it("hands the expanded response from its shaped card to the unshaped console", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(responseSnapshotWith(spokenResponse("region", 1), 1));
    await flushPromises();
    mocks.windowCalls.length = 0;

    fireEvent.doubleClick(screen.getByTestId("hud-root"));
    await flushPromises();

    // The compact pill and the console are both clean surfaces: no region
    // handoff, no hide, just the console's bounds.
    expect(mocks.windowCalls).not.toContain("native-hide+clear");
    expect(mocks.windowCalls.some((call) => call.includes("region:"))).toBe(false);
    expect(lastBounds()).toContain(
      `${CONSOLE_DEFAULT_W + CONSOLE_PAD_X * 2}x${CONSOLE_DEFAULT_H + CONSOLE_PAD_Y * 2}`,
    );
  });

  it("hands the question card its measured region", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 322 };
    await mountHud();

    emit(questionSnapshot(1));
    await flushPromises();
    expect(screen.getByRole("status")).toHaveAccessibleName(
      expect.stringContaining("Claude asked"),
    );
    fireEvent.doubleClick(screen.getByTestId("hud-root"));
    await flushPromises();

    expect(screen.getByTestId("agent-question-panel")).toBeInTheDocument();
    // The compact question pill is in the clean reply family; the expanded
    // card is shaped. Crossing families hides first, so the card shows once
    // with its region (no restyle or resize of the visible window).
    expect(lastRegion()).toBe(
      `show+region:${QUESTION_PAD_X - HUD_REGION_EDGE_BLEED},${QUESTION_PAD_Y - HUD_REGION_EDGE_BLEED},${CLAUDE_CARD_W + HUD_REGION_EDGE_BLEED * 2}x${322 + HUD_REGION_EDGE_BLEED * 2}`,
    );
  });

  it("clips the permission pill to its own footprint", async () => {
    mocks.claudeCardSize = {
      width: CLAUDE_PERMISSION_PILL_W,
      height: CLAUDE_PERMISSION_MIN_H,
    };
    await mountHud();

    emit(pendingPermission(1));
    await flushPromises();

    expect(screen.getByTestId("claude-permission-pill")).toBeInTheDocument();
    expect(lastRegion()).toContain(
      `region:${QUESTION_PAD_X - HUD_REGION_EDGE_BLEED},${QUESTION_PAD_Y - HUD_REGION_EDGE_BLEED},${CLAUDE_PERMISSION_PILL_W + HUD_REGION_EDGE_BLEED * 2}x${CLAUDE_PERMISSION_MIN_H + HUD_REGION_EDGE_BLEED * 2}`,
    );
    expect(CLAUDE_PERMISSION_MIN_H).toBeLessThan(CLAUDE_PERMISSION_MAX_H);
  });

  it("renders a Codex permission as Codex and defers to Codex", async () => {
    mocks.claudeCardSize = {
      width: CLAUDE_PERMISSION_PILL_W,
      height: CLAUDE_PERMISSION_MIN_H,
    };
    await mountHud();
    const event = pendingPermission(1);
    emit({
      ...event,
      interaction: {
        ...event.interaction,
        agent: "codex",
        source_name: "Codex",
      },
    });
    await flushPromises();

    const pill = screen.getByTestId("claude-permission-pill");
    expect(pill).toHaveTextContent("Codex");
    expect(pill).not.toHaveTextContent("Claude");
    expect(
      screen.getByRole("button", { name: "Decide in Codex" }),
    ).toBeInTheDocument();
  });

  it("removes a terminal interaction instead of retaining it in HUD state", async () => {
    await mountHud();
    const pending = pendingPermission(1);
    emit(pending);
    await flushPromises();
    expect(screen.getByTestId("claude-permission-pill")).toBeInTheDocument();

    emit({
      ...pending,
      interaction: {
        ...pending.interaction,
        status: "resolved",
        remaining_seconds: 0,
        hard_remaining_seconds: 0,
      },
    });
    await flushPromises();

    expect(screen.queryByTestId("claude-permission-pill")).toBeNull();
  });

  it("replaces missed terminal state and rejects stale snapshots and legacy deltas", async () => {
    await mountHud();
    const pending = pendingPermission(1);
    emit({ type: "agent.interactions.snapshot", version: 1, revision: 10000, interactions: [pending] });
    await flushPromises();
    expect(screen.getByTestId("claude-permission-pill")).toBeInTheDocument();
    emit({ type: "agent.interactions.snapshot", version: 1, revision: 10001, interactions: [] });
    emit({ type: "agent.interactions.snapshot", version: 1, revision: 10000, interactions: [pending] });
    act(() => { for (const handler of [...mocks.handlers]) handler(pending); });
    await flushPromises();
    expect(screen.queryByTestId("claude-permission-pill")).toBeNull();
  });

  it("does not commit the HUD coordinator for an audio-only event burst", async () => {
    const commits = vi.fn();
    render(<Profiler id="hud" onRender={commits}><HUD /></Profiler>);
    await flushPromises();
    const before = commits.mock.calls.length;
    for (let i = 0; i < 100; i += 1) emit({ type: "audio.levels", rms: 0.5, peak: 0.8 });
    await flushPromises();
    expect(commits).toHaveBeenCalledTimes(before);
  });

  it("clears interaction authority on disconnect and accepts restarted backend revisions", async () => {
    const view = await mountHud();
    emit({ type: "agent.interactions.snapshot", version: 1, revision: 10000, interactions: [pendingPermission(1)] });
    await flushPromises();
    mocks.connected = false;
    view.rerender(<HUD />);
    await flushPromises();
    expect(screen.queryByTestId("claude-permission-pill")).toBeNull();
    mocks.connected = true;
    view.rerender(<HUD />);
    emit({ type: "agent.interactions.snapshot", version: 1, revision: 1, interactions: [pendingPermission(1)] });
    await flushPromises();
    expect(screen.getByTestId("claude-permission-pill")).toBeInTheDocument();
  });

  // Regression for Task 3.3's harness sweep: the outer pill vessel used to
  // animate to the legacy dictation PILL_WIDTH (120) for the permission
  // state, squeezing the wider ClaudePermissionPill child under the
  // browser's default flex-shrink and clipping the Allow button entirely. The native window
  // and region already followed MODE_GEOMETRY.permission's real
  // footprint (asserted above); the painted vessel now has to agree.
  it("paints the permission pill at its own footprint, not the legacy pill width", async () => {
    mocks.claudeCardSize = {
      width: CLAUDE_PERMISSION_PILL_W,
      height: CLAUDE_PERMISSION_MIN_H,
    };
    await mountHud();

    emit(pendingPermission(1));
    await flushPromises();

    const pill = screen.getByTestId("claude-permission-pill");
    expect(pill.parentElement).toHaveAttribute(
      "data-motion-width",
      String(CLAUDE_PERMISSION_PILL_W),
    );
  });

  it("keeps the compact pill footprint identical across a speaking toggle", async () => {
    // Design.md state 2: the stop-speech button replaces the queue chip, so
    // the footprint and radius are unchanged and speech start/stop must issue
    // no native geometry event at all - now that the region follows the
    // painted pill, a width that moved with `speaking` would flicker the clip.
    mocks.claudeCardSize = { width: 132, height: CLAUDE_PILL_H };
    await mountHud();
    const completion = spokenResponse("compact-speaking", 1);
    emit(responseSnapshotWith(completion, 1));
    await flushPromises();
    mocks.windowCalls.length = 0;

    emit(responseSnapshotWith(completion, 2, { phase: "active_speaking" }));
    await flushPromises();
    expect(document.querySelector('[data-agent-glyph="speaking"]')).not.toBeNull();
    expect(mocks.windowCalls).toEqual([]);

    emit(responseSnapshotWith(completion, 3, { phase: "active_grace" }));
    await flushPromises();
    expect(mocks.windowCalls).toEqual([]);
  });

  // --- Task 4.4: tabs share the identity header ---
  // Parallel-session navigation consumes horizontal room beside Claude, not a
  // second vertical row, so switching and session-count changes do not resize
  // the body well.

  function sessionsSnapshot(
    names: string[],
  ): Extract<AppEvent, { type: "agent.sessions" }> {
    return {
      type: "agent.sessions",
      sessions: names.map((name) => ({
        session_handle: `handle-${name}`,
        agent: "claude",
        source_name: name,
        unread: false,
        needs_input: false,
        can_reply: true,
      })),
    };
  }

  it("marks the speaking conversation's tab on the glance card, not the console's last pick", async () => {
    // Owner report 2026-10-25: the strip marked a remembered session instead
    // of the one speaking.
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    const snapshot = sessionsSnapshot(["sillytavern", "agent-speak"]);
    snapshot.sessions[0].conversation_handle = "conv-silly";
    snapshot.sessions[1].conversation_handle = "conv-agent";
    emit(snapshot);
    emit(responseSnapshotWith(spokenResponse("speaks", 1), 1, { showSummaryText: true, sessionHandle: "conv-agent" }));
    await flushPromises();

    expect(screen.getByRole("tab", { name: /^Claude, agent-speak\./ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /^Claude, sillytavern\./ })).toHaveAttribute("aria-selected", "false");
  });

  it("places tabs in the identity header without shrinking the body well", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(sessionsSnapshot(["whisperstream"]));
    await expandResponse(spokenResponse("strip-chrome", 1));

    // One session: no strip, and the console's own size is the baseline.
    expect(screen.queryByTestId("hud-session-tabs")).toBeNull();
    const height = screen.getByTestId("claude-expanded-card").style.height;
    // The console's well fills the card instead of carrying a ceiling.
    expect(screen.getByTestId("claude-well").style.maxHeight).toBe("");

    emit(sessionsSnapshot(["whisperstream", "site"]));
    await flushPromises();

    expect(screen.getByTestId("hud-session-tabs")).toBeInTheDocument();
    expect(screen.getByTestId("claude-card-header-trailing")).toContainElement(
      screen.getByTestId("hud-session-tabs"),
    );
    expect(SESSION_TAB_CHROME_COST).toBe(0);
    // The strip shares the header: the console keeps its size.
    expect(screen.getByTestId("claude-expanded-card").style.height).toBe(height);
    expect(screen.getByTestId("claude-well").style.maxHeight).toBe("");
  });

  it("suppresses the identity row's name suffix while the strip is present", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(sessionsSnapshot(["whisperstream"]));
    await expandResponse({
      ...spokenResponse("strip-suffix", 1),
      source_name: "whisperstream",
    });

    // Decision 2: the strip names every session, so the single-session suffix
    // would say it twice.
    const card = screen.getByTestId("claude-expanded-card");
    expect(card).toHaveTextContent(/·\s*whisperstream/);

    emit(sessionsSnapshot(["whisperstream", "site"]));
    await flushPromises();
    expect(card).not.toHaveTextContent(/·\s*whisperstream/);
    expect(card).toHaveTextContent("Claude");
    // The name did not vanish from the card, it moved into the strip.
    expect(
      screen.getByRole("tab", { name: /^Claude, whisperstream\./ }),
    ).toBeInTheDocument();
  });

  it("hands a selected managed tab to the clean Tauri presentation owner", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(sessionsSnapshot(["whisperstream", "site"]));
    emit(responseSnapshotWith(spokenResponse("strip-switch", 1), 1));
    await flushPromises();
    mocks.windowCalls.length = 0;
    fireEvent.doubleClick(screen.getByTestId("hud-root"));
    await flushPromises();
    // The compact pill and the console share the clean owner: no handoff.
    expect(mocks.windowCalls).not.toContain("native-hide+clear");
    mocks.windowCalls.length = 0;
    mocks.hudWindow.setFocusable.mockClear();

    const site = screen.getByRole("tab", { name: /^Claude, site\./ });
    fireEvent.click(site);
    await flushPromises();

    expect(site).toHaveAttribute("aria-selected", "true");
    // The managed console stays clean: no native region is installed.
    expect(
      mocks.windowCalls.some((call) => call.includes("region:")),
    ).toBe(false);
    expect(mocks.hudWindow.setFocusable).not.toHaveBeenCalled();
  });

  it("keeps exactly one reply rail in layout while tabs switch", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit({
      type: "agent.sessions",
      sessions: [
        {
          session_handle: "rail-one",
          agent: "claude",
          source_name: "one",
          unread: false,
          needs_input: false,
          can_reply: true,
          latest_response: "First response",
        },
        {
          session_handle: "rail-two",
          agent: "claude",
          source_name: "two",
          unread: false,
          needs_input: false,
          can_reply: true,
          latest_response: "Second response",
        },
      ],
    });
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();

    fireEvent.click(screen.getByRole("tab", { name: /^Claude, two\./ }));

    expect(screen.getAllByTestId("claude-reply-rail")).toHaveLength(1);
    expect(screen.getByTestId("claude-expanded-card")).toHaveTextContent(
      "Second response",
    );
  });

  // --- Task 4.5: transitions, and the scoped FIFO supersession ---

  it("does not move the tab selection when a new session arrives", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(sessionsSnapshot(["whisperstream", "site"]));
    await expandResponse(spokenResponse("selection-add", 1));

    fireEvent.click(screen.getByRole("tab", { name: /^Claude, site\./ }));
    await flushPromises();
    expect(screen.getByRole("tab", { name: /^Claude, site\./ })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    emit(sessionsSnapshot(["whisperstream", "site", "third"]));
    await flushPromises();

    expect(screen.getByRole("tab", { name: /^Claude, site\./ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("never moves an expanded card to another session when the selected session exits", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(sessionsSnapshot(["whisperstream", "site", "third"]));
    await expandResponse(spokenResponse("selection-remove", 1));

    fireEvent.click(screen.getByRole("tab", { name: /^Claude, site\./ }));
    await flushPromises();
    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();

    // The selected console's session ends: it collapses rather than moving
    // to a survivor whose draft Enter or push-to-talk could then reach.
    emit(sessionsSnapshot(["whisperstream", "third"]));
    await flushPromises();

    expect(screen.queryByRole("tab", { name: /^Claude, third\./, selected: true })).toBeNull();
    expect(screen.queryByRole("tab", { name: /^Claude, whisperstream\./, selected: true })).toBeNull();
  });

  it("keeps the card expanded when a background session is promoted while a strip is present (decision 10)", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(sessionsSnapshot(["whisperstream", "site"]));
    await expandResponse(spokenResponse("fifo-a", 1));
    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();

    // A FIFO promotion is a new completion identity. With a strip present,
    // it must light the background capsule's unread dot (backend-driven, via
    // the next `agent.sessions` snapshot) rather than reset this card.
    emit(responseSnapshotWith(spokenResponse("fifo-b", 1), 2));
    await flushPromises();

    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
    expect(screen.getByTestId("hud-session-tabs")).toBeInTheDocument();
  });

  it("still resets to compact on promotion with a single session (the guard is tight)", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    await expandResponse(spokenResponse("solo-a", 1));
    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();

    emit(responseSnapshotWith(spokenResponse("solo-b", 1), 2));
    await flushPromises();

    expect(screen.queryByTestId("claude-expanded-card")).toBeNull();
  });

  // --- Task 4.6: focus, keyboard switching, and the footer hint ---

  it("shows persistent numerals without adding shortcut hint copy", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(sessionsSnapshot(["whisperstream", "site"]));
    await expandResponse(spokenResponse("focus-rest", 1));

    const card = screen.getByTestId("claude-expanded-card");
    expect(card).not.toHaveTextContent("Double-click to collapse");
    expect(card).not.toHaveTextContent(/Ctrl\+1-9 switch/);
    const tabs = screen.getAllByRole("tab");
    expect(tabs[0]).toHaveTextContent("1");
    expect(tabs[1]).toHaveTextContent("2");
  });

  it("keeps persistent numerals after a plain card click", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(sessionsSnapshot(["whisperstream", "site"]));
    await expandResponse(spokenResponse("focus-click", 1));

    const card = screen.getByTestId("claude-expanded-card");
    fireEvent.click(card);
    await flushPromises();

    expect(card).not.toHaveTextContent("Ctrl+1-9 switch");
    expect(card).not.toHaveTextContent("Double-click to collapse");
    const tabs = screen.getAllByRole("tab");
    expect(tabs[0]).toHaveTextContent("1");
    expect(tabs[1]).toHaveTextContent("2");
  });

  it("switches tabs via Ctrl+1..9 and Ctrl+Tab only while focused", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(sessionsSnapshot(["whisperstream", "site", "third"]));
    await expandResponse(spokenResponse("focus-switch", 1));

    // Unfocused: Ctrl+2 does nothing. No tab owns this hook conversation and
    // none was picked, so none reads as current.
    fireEvent.blur(window);
    await flushPromises();
    fireEvent.keyDown(document, { key: "2", ctrlKey: true });
    await flushPromises();
    for (const tab of screen.getAllByRole("tab")) {
      expect(tab).toHaveAttribute("aria-selected", "false");
    }

    const card = screen.getByTestId("claude-expanded-card");
    fireEvent.click(card);
    fireEvent.focus(window);
    await flushPromises();

    fireEvent.keyDown(document, { key: "2", ctrlKey: true });
    await flushPromises();
    expect(screen.getByRole("tab", { name: /^Claude, site\./ })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    fireEvent.keyDown(document, { key: "Tab", ctrlKey: true });
    await flushPromises();
    expect(screen.getByRole("tab", { name: /^Claude, third\./ })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    fireEvent.keyDown(document, { key: "Tab", ctrlKey: true, shiftKey: true });
    await flushPromises();
    expect(screen.getByRole("tab", { name: /^Claude, site\./ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("never switches on a plain unmodified digit, even while focused", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(sessionsSnapshot(["whisperstream", "site"]));
    await expandResponse(spokenResponse("focus-plain-digit", 1));

    const card = screen.getByTestId("claude-expanded-card");
    fireEvent.click(card);
    await flushPromises();

    fireEvent.keyDown(document, { key: "2" });
    await flushPromises();
    for (const tab of screen.getAllByRole("tab")) {
      expect(tab).toHaveAttribute("aria-selected", "false");
    }
  });

  it("never makes the window focusable on mount or on an incoming completion by itself", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(sessionsSnapshot(["whisperstream", "site"]));
    mocks.hudWindow.setFocusable.mockClear();
    await expandResponse(spokenResponse("focus-mount", 1));

    expect(mocks.hudWindow.setFocusable).not.toHaveBeenCalledWith(true);
  });

  // --- Task 4.7: the blocked reply rail and the switch refusal ---

  it("refuses to switch tabs without adding shortcut copy while the rail is busy", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit(sessionsSnapshot(["whisperstream", "site"]));
    await expandResponse(spokenResponse("busy-rail", 1));

    const card = screen.getByTestId("claude-expanded-card");
    fireEvent.click(card);
    await flushPromises();

    // Drive the rail into "live": the reply owner is recording into it.
    fireEvent.focus(window);
    emit(responseSnapshotWith(spokenResponse("busy-rail", 1), 2, { sessionHandle: "busy-conv" }));
    emit(replySnapshot([replyDraftFor("conv:busy-conv", { phase: "recording", compose_generation: 5 })]));
    await flushPromises();

    expect(card).not.toHaveTextContent("Esc to cancel, then switch");
    expect(card).not.toHaveTextContent("Ctrl+1-9 switch");

    for (const tab of screen.getAllByRole("tab")) {
      expect(tab).toHaveAttribute("aria-disabled", "true");
    }

    // Ctrl+1 and a click are no-ops while busy: nothing gets selected.
    fireEvent.keyDown(document, { key: "1", ctrlKey: true });
    await flushPromises();
    for (const tab of screen.getAllByRole("tab")) {
      expect(tab).toHaveAttribute("aria-selected", "false");
    }

    fireEvent.click(screen.getByRole("tab", { name: /^Claude, site\./ }));
    await flushPromises();
    for (const tab of screen.getAllByRole("tab")) {
      expect(tab).toHaveAttribute("aria-selected", "false");
    }
  });

  it("shows the blocked resting line for a cannot-reply session before any user gesture", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 404 };
    await mountHud();
    emit({
      type: "agent.sessions",
      sessions: [
        {
          session_handle: "handle-whisperstream",
          agent: "claude",
          source_name: "whisperstream",
          unread: false,
          needs_input: false,
          can_reply: true,
        },
        {
          session_handle: "handle-site",
          agent: "claude",
          source_name: "site",
          unread: false,
          needs_input: false,
          can_reply: false,
        },
      ],
    });
    await expandResponse(spokenResponse("blocked-rest", 1));

    fireEvent.click(screen.getByRole("tab", { name: /^Claude, site\./ }));
    await flushPromises();

    expect(screen.getByTestId("claude-expanded-card")).toHaveTextContent(
      "Reply in your terminal",
    );
  });

  it("shows the approved content-free terms notice for an EULA-blocked hotkey", async () => {
    await mountHud();

    emit({
      type: "recording.blocked",
      reason: "eula_acceptance_required",
    });
    await flushPromises();

    expect(screen.getByText("REVIEW TERMS")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveAccessibleName(
      "Review and accept the updated terms before using dictation.",
    );
  });

  // --- Effective-profile stamp (transparent-indicators Task 2.1) ---------
  //
  // The stamp is identity, not status: it rides the ACTIVE dictation only, for
  // the recording its payload correlates to, and it never appears on a
  // transient, an idle pill, a HUD-origin control layout, or a later dictation.

  function profileEvent(
    recordingId: number,
    profileId: string,
    name: string,
    icon?: string,
  ): Extract<AppEvent, { type: "enhancement.profile" }> {
    return {
      type: "enhancement.profile",
      recording_id: recordingId,
      profile_id: profileId,
      name,
      ...(icon === undefined ? {} : { icon }),
    };
  }

  async function stampedRecording(
    recordingId: number,
    event: Extract<AppEvent, { type: "enhancement.profile" }>,
  ): Promise<void> {
    emit(lifecycle(recordingId, "recording", { preview_enabled: false }));
    await flushPromises();
    emit(event);
    await flushPromises();
  }

  it("stamps the active pill with the built-in glyph for its own recording", async () => {
    await mountHud();

    await stampedRecording(900, profileEvent(900, "chat", "Chat"));

    const mark = screen.getByTestId("hud-profile-mark");
    expect(mark).toHaveAttribute("data-profile-icon", "chat");
    // Identity is never announced: the HUD renders nothing under a detected
    // screen reader, so a labelled stamp would only ever be noise.
    expect(mark).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByTestId("hud-pill-vessel")).toHaveAttribute(
      "data-motion-width",
      String(PILL_WIDTH_PROFILE),
    );
    expect(PILL_WIDTH_PROFILE).toBeLessThan(120);
  });

  it("does not resize the vessel or the window across recording to processing", async () => {
    await mountHud();
    await stampedRecording(901, profileEvent(901, "code", "Code"));
    mocks.windowCalls.length = 0;

    emit(lifecycle(901, "processing", { preview_enabled: false }));
    await flushPromises();

    // Width follows STAMP PRESENCE, not dictation state, so the phase change
    // stays a pure content crossfade.
    expect(screen.getByTestId("hud-profile-mark")).toHaveAttribute(
      "data-profile-icon",
      "code",
    );
    expect(screen.getByTestId("hud-pill-vessel")).toHaveAttribute(
      "data-motion-width",
      String(PILL_WIDTH_PROFILE),
    );
    expect(mocks.windowCalls).toEqual([]);
  });

  it("renders a custom profile's curated glyph and falls back to its letter", async () => {
    await mountHud();
    await stampedRecording(902, profileEvent(902, "custom-1", "Slack", "chat"));
    expect(screen.getByTestId("hud-profile-mark")).toHaveAttribute(
      "data-profile-icon",
      "chat",
    );

    emit(lifecycle(902, "idle", { preview_enabled: false }));
    await flushPromises();
    await stampedRecording(903, profileEvent(903, "custom-2", "standup"));

    // No stored key: the letter is a content variant in the same tile, not an
    // error state.
    const mark = screen.getByTestId("hud-profile-mark");
    expect(mark).toHaveAttribute("data-profile-icon", "letter");
    expect(mark).toHaveTextContent("S");
  });

  it("degrades an unrecognized icon key to the letter rather than a wrong glyph", async () => {
    await mountHud();

    await stampedRecording(
      904,
      profileEvent(904, "custom-3", "Notes", "not-a-real-key"),
    );

    expect(screen.getByTestId("hud-profile-mark")).toHaveAttribute(
      "data-profile-icon",
      "letter",
    );
  });

  it("discards a stale-recording payload instead of stamping the next dictation", async () => {
    await mountHud();
    emit(lifecycle(905, "recording", { preview_enabled: false }));
    await flushPromises();
    emit(lifecycle(905, "idle", { preview_enabled: false }));
    await flushPromises();
    emit(lifecycle(906, "recording", { preview_enabled: false }));
    await flushPromises();

    // A finalizer for the previous dictation lands late. Correlation is
    // load-bearing: painting it would put the wrong identity on recording 906.
    emit(profileEvent(905, "email", "Email"));
    await flushPromises();

    expect(screen.queryByTestId("hud-profile-mark")).toBeNull();
    expect(screen.getByTestId("hud-pill-vessel")).toHaveAttribute(
      "data-motion-width",
      "80",
    );
  });

  it("clears the stamp when its dictation ends", async () => {
    await mountHud();
    await stampedRecording(907, profileEvent(907, "chat", "Chat"));
    expect(screen.getByTestId("hud-profile-mark")).toBeInTheDocument();

    emit(lifecycle(907, "idle", { preview_enabled: false }));
    await flushPromises();
    emit(lifecycle(908, "recording", { preview_enabled: false }));
    await flushPromises();

    expect(screen.queryByTestId("hud-profile-mark")).toBeNull();
  });

  it("leaves the raw no-enhancement dictation unstamped at the compact width", async () => {
    await mountHud();

    emit(lifecycle(909, "recording", { preview_enabled: false }));
    await flushPromises();

    expect(screen.queryByTestId("hud-profile-mark")).toBeNull();
    expect(screen.getByTestId("hud-pill-vessel")).toHaveAttribute(
      "data-motion-width",
      "80",
    );
  });

  it("keeps the HUD-origin control layout unstamped", async () => {
    await mountHud();
    emit(
      lifecycle(910, "recording", { origin: "hud", preview_enabled: false }),
    );
    await flushPromises();
    emit(profileEvent(910, "chat", "Chat"));
    await flushPromises();

    // The 112px control layout owns its content; a stamp would displace the
    // cancel / finish affordances.
    expect(screen.getByTestId("hud-recording-controls")).toBeInTheDocument();
    expect(screen.queryByTestId("hud-profile-mark")).toBeNull();
  });

  it("does not stamp a blocked transient", async () => {
    await mountHud();
    await stampedRecording(911, profileEvent(911, "chat", "Chat"));
    emit(lifecycle(911, "idle", { preview_enabled: false }));
    await flushPromises();

    emit({ type: "recording.blocked", reason: "eula_acceptance_required" });
    await flushPromises();

    expect(screen.getByText("REVIEW TERMS")).toBeInTheDocument();
    expect(screen.queryByTestId("hud-profile-mark")).toBeNull();
  });

  it("carries the same stamp on Live Preview's controls row", async () => {
    await mountHud();
    emit(lifecycle(912, "recording"));
    await flushPromises();
    emit(profileEvent(912, "email", "Email"));
    await flushPromises();

    const controls = screen.getByTestId("preview-view-controls");
    const mark = screen.getByTestId("hud-profile-mark");
    expect(controls).toContainElement(mark);
    // The built-in Email profile derives the shared `mail` glyph from its
    // immutable id; nothing about the icon is persisted for a built-in.
    expect(mark).toHaveAttribute("data-profile-icon", "mail");
    expect(mark).toHaveAttribute("aria-hidden", "true");
  });

  it("retires the manual-override companion chip", async () => {
    await mountHud();
    emit(lifecycle(913, "recording", { preview_enabled: false }));
    await flushPromises();

    // `active_profile_override` remains the Settings status line's signal and
    // is untouched by this work; the HUD simply no longer paints a second,
    // manual-only surface beside the vessel.
    emit({
      type: "active_profile_override",
      profile_id: "code",
      name: "Code",
    });
    await flushPromises();

    expect(screen.queryByText("Code")).toBeNull();
    expect(screen.queryByTestId("hud-profile-mark")).toBeNull();
  });
  // --- Provider timeout transient (transparent-indicators Task 2.2) --------
  //
  // Timeout and generic error outcomes share the raw-fallback transient family.

  function timeoutEvent(
    recordingId: number,
    provider: EnhancementTimeoutProvider,
  ): Extract<AppEvent, { type: "enhancement.timeout" }> {
    return { type: "enhancement.timeout", recording_id: recordingId, provider };
  }

  function errorEvent(
    recordingId: number,
    provider: EnhancementTimeoutProvider,
  ): Extract<AppEvent, { type: "enhancement.error" }> {
    return { type: "enhancement.error", recording_id: recordingId, provider };
  }

  const TIMEOUT_COPY: ReadonlyArray<
    readonly [EnhancementTimeoutProvider, string, string]
  > = [
    [
      "gemini",
      "Gemini",
      "Gemini timed out. Your text was pasted without enhancement.",
    ],
    [
      "openai",
      "OpenAI",
      "OpenAI timed out. Your text was pasted without enhancement.",
    ],
    [
      "anthropic",
      "Anthropic",
      "Anthropic timed out. Your text was pasted without enhancement.",
    ],
    [
      "ollama",
      "Ollama",
      "Ollama timed out. Your text was pasted without enhancement.",
    ],
    [
      "local",
      "Local model",
      "Local model timed out. Your text was pasted without enhancement.",
    ],
    [
      "custom",
      "AI provider",
      "The AI provider timed out. Your text was pasted without enhancement.",
    ],
  ];

  it.each(TIMEOUT_COPY)(
    "renders the locked visible and announced copy for %s",
    async (provider, label, announcement) => {
      await mountHud();

      emit(timeoutEvent(950, provider));
      await flushPromises();

      const status = screen.getByRole("status", { name: announcement });
      expect(status).toHaveAttribute("aria-live", "polite");
      expect(status).toHaveTextContent(label);
      // Line 2 is invariant and lowercase on purpose, so the two lines read as
      // one fragment rather than as a stacked warning label.
      expect(status).toHaveTextContent("timed out");
      expect(status.textContent ?? "").not.toContain("—");
    },
  );

  it("renders generic provider errors with rose treatment and raw-fallback copy", async () => {
    await mountHud();

    emit(errorEvent(949, "anthropic"));
    await flushPromises();

    const status = screen.getByRole("status", {
      name: "Anthropic returned an error. Your text was pasted without enhancement.",
    });
    expect(status).toHaveTextContent("Anthropic");
    expect(status).toHaveTextContent("returned an error");
    expect(status.parentElement).toHaveAttribute("data-motion-width", "120");
    expect(
      status.querySelector('[stroke="var(--color-rose)"]'),
    ).not.toBeNull();
  });

  it("paints a gold aria-hidden clock in the existing 120px vessel", async () => {
    await mountHud();

    emit(timeoutEvent(951, "gemini"));
    await flushPromises();

    const status = screen.getByTestId("hud-enhance-timeout");
    // Same width the blocked transient already uses, so
    // MODE_GEOMETRY.pill, the window, and the anchor math are untouched.
    expect(status.parentElement).toHaveAttribute("data-motion-width", "120");

    const icon = status.querySelector('svg[aria-hidden="true"]');
    expect(icon).not.toBeNull();
    // Gold is the caution token: the dictation succeeded, the cleanup did not.
    // Rose stays reserved for refusal-to-send.
    expect(
      icon?.querySelector('circle[stroke="var(--color-gold)"]'),
    ).not.toBeNull();
    expect(
      icon?.querySelector('path[stroke="var(--color-gold)"]'),
    ).not.toBeNull();
    expect(icon?.querySelector('[stroke="var(--color-rose)"]')).toBeNull();
  });

  it("keeps every provider label inside the measured 80px text column", async () => {
    await mountHud();

    emit(timeoutEvent(952, "local"));
    await flushPromises();

    const column = screen.getByTestId("hud-enhance-timeout-text");
    // 120 vessel - 20 padding - 12 glyph - 8 gap. jsdom cannot measure Inter,
    // so the budget is asserted structurally plus against the locked strings at
    // a fixed approximation of ~5px/char for 10px Inter 500 (`Local model` is
    // the binding label at ~55px). No ellipsis band-aid: a label that outgrows
    // this column is a copy bug, not a truncation problem.
    expect(column).toHaveStyle({ width: "80px" });
    expect(column.textContent ?? "").not.toContain("…");
    for (const [, label] of TIMEOUT_COPY) {
      expect(label.length * 5).toBeLessThanOrEqual(80);
    }
  });

  it("dwells for exactly two seconds", async () => {
    await mountHud();

    emit(timeoutEvent(953, "openai"));
    await flushPromises();

    await advance(1_999);
    expect(screen.getByTestId("hud-enhance-timeout")).toBeInTheDocument();
    await advance(1);
    expect(screen.queryByTestId("hud-enhance-timeout")).toBeNull();
  });

  it("never paints over an active dictation and queues behind processing", async () => {
    await mountHud();

    emit(lifecycle(954, "processing", { preview_enabled: false }));
    await flushPromises();
    emit(timeoutEvent(954, "ollama"));
    await flushPromises();

    // The dictation still owns the vessel; the notice waits for the raw paste
    // and the idle transition.
    expect(screen.queryByTestId("hud-enhance-timeout")).toBeNull();

    emit(lifecycle(954, "idle", { preview_enabled: false }));
    await flushPromises();

    expect(screen.getByTestId("hud-enhance-timeout")).toBeInTheDocument();
    await advance(1_999);
    expect(screen.getByTestId("hud-enhance-timeout")).toBeInTheDocument();
    await advance(1);
    expect(screen.queryByTestId("hud-enhance-timeout")).toBeNull();
  });

  it("pauses and resumes the dwell around a following recording", async () => {
    await mountHud();

    emit(timeoutEvent(955, "local"));
    await flushPromises();
    await advance(500);

    emit(lifecycle(956, "recording", { preview_enabled: false }));
    await flushPromises();
    expect(screen.queryByTestId("hud-enhance-timeout")).toBeNull();

    await advance(5_000);
    emit(lifecycle(956, "idle", { preview_enabled: false }));
    await flushPromises();

    expect(screen.getByTestId("hud-enhance-timeout")).toBeInTheDocument();
    await advance(1_499);
    expect(screen.getByTestId("hud-enhance-timeout")).toBeInTheDocument();
    await advance(1);
    expect(screen.queryByTestId("hud-enhance-timeout")).toBeNull();
  });

  it("mounts the clock hands in place under reduced motion", async () => {
    mocks.reducedMotion = true;
    await mountHud();

    emit(timeoutEvent(959, "gemini"));
    await flushPromises();

    const hands = screen.getByTestId("hud-enhance-timeout-hands");
    expect(hands).toHaveAttribute("data-motion-initial", "false");
    expect(hands).not.toHaveAttribute("data-motion-initial-scale");

    mocks.reducedMotion = false;
  });

  it("beats the clock hands in on the shared spring otherwise", async () => {
    await mountHud();

    emit(timeoutEvent(960, "gemini"));
    await flushPromises();

    const hands = screen.getByTestId("hud-enhance-timeout-hands");
    expect(hands).toHaveAttribute("data-motion-initial-scale", "0.7");
    expect(hands).toHaveAttribute("data-motion-delay", "0.18");
  });

  it("stays silent after a successful enhancement", async () => {
    await mountHud();

    // A dictation that enhanced normally emits a profile event and a result but
    // no outcome event, so neither transient may appear.
    await stampedRecording(961, profileEvent(961, "chat", "Chat"));
    emit(lifecycle(961, "idle", { preview_enabled: false }));
    await flushPromises();
    emit({ type: "transcription.result", text: "hello", duration_ms: 12 });
    await flushPromises();

    expect(screen.queryByTestId("hud-enhance-timeout")).toBeNull();
    expect(screen.queryByTestId("hud-enhance-error")).toBeNull();
  });

  it("carries no profile stamp on the timeout transient", async () => {
    await mountHud();
    await stampedRecording(962, profileEvent(962, "chat", "Chat"));
    emit(lifecycle(962, "idle", { preview_enabled: false }));
    await flushPromises();

    emit(timeoutEvent(962, "gemini"));
    await flushPromises();

    // Status, not identity: the stamp rides the active dictation only.
    expect(screen.getByTestId("hud-enhance-timeout")).toBeInTheDocument();
    expect(screen.queryByTestId("hud-profile-mark")).toBeNull();
  });

  it("renders automatic natural-height summary card when show_summary_text is true", async () => {
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 160 };
    await mountHud();

    emit(
      responseSnapshotWith(
        {
          ...response("c-summary", 1),
          spoken_summary: "Automated summary text for the response.",
        },
        1,
        { showSummaryText: true },
      ),
    );
    await flushPromises();

    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
    expect(
      screen.getByText("Automated summary text for the response."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Reply c-summary", { exact: false }),
    ).not.toBeInTheDocument();

    // The glance card: 440 wide in a fixed window. A 160px card fits the
    // resting window, so it is pinned to its bottom with no resize.
    expect(lastBounds()).toContain(
      `${MODE_GEOMETRY.summary.windowW}x${MODE_GEOMETRY.summary.windowH}`,
    );

    // Triggering the shortcut toggles from summary to full response
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();

    expect(
      screen.getByText("Reply c-summary", { exact: false }),
    ).toBeInTheDocument();

    // Triggering shortcut again contracts back to the summary card: the full
    // response fades out where it is first, then the glance card returns.
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    expect(screen.getByTestId("hud-root")).toHaveStyle({ opacity: "0" });
    expect(screen.getByText("Reply c-summary", { exact: false })).toBeInTheDocument();
    await advance(CONSOLE_CONTRACT_FADE_MS);

    expect(
      screen.getByText("Automated summary text for the response."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Reply c-summary", { exact: false }),
    ).not.toBeInTheDocument();
  });

  it("contracts the console by fading it in place, then moving the window once", async () => {
    // Owner report 2026-10-24: collapsing shrank the console in the middle
    // of the screen and then jumped it to the pill.
    mocks.claudeCardSize = { width: CLAUDE_CARD_W, height: 160 };
    await mountHud();
    emit(responseSnapshotWith({ ...response("c-contract", 1), spoken_summary: "Summary." }, 1, { showSummaryText: true }));
    await flushPromises();
    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    expect(lastBounds()).toContain(`${CONSOLE_DEFAULT_W + CONSOLE_PAD_X * 2}x`);
    mocks.windowCalls.length = 0;

    emit({ type: "agent-hud-view" } as AppEvent);
    await flushPromises();
    // Fading: the console stays put and its window does not move.
    expect(screen.getByTestId("hud-root")).toHaveStyle({ opacity: "0" });
    expect(mocks.windowCalls.filter((call) => call.startsWith("bounds:"))).toEqual([]);

    await advance(CONSOLE_CONTRACT_FADE_MS);
    await flushPromises();
    const moves = mocks.windowCalls.filter((call) => call.startsWith("bounds:"));
    expect(moves).toHaveLength(1);
    expect(moves[0]).toContain(`${MODE_GEOMETRY.summary.windowW}x`);
    expect(screen.getByText("Summary.")).toBeInTheDocument();
    expect(screen.getByTestId("hud-root")).not.toHaveStyle({ visibility: "hidden" });
  });

  it("remains compact when show_summary_text is true but summary is missing", async () => {
    await mountHud();

    emit(
      responseSnapshotWith(response("c-no-summary", 1), 1, {
        showSummaryText: true,
      }),
    );
    await flushPromises();

    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
    expect(screen.queryByTestId("claude-expanded-card")).not.toBeInTheDocument();
    expect(screen.getByTestId("compact-claude-pill")).toHaveAttribute(
      "aria-label",
      expect.stringContaining("No summary for this reply"),
    );
  });
});

describe("neutral coding-agent marks", () => {
  afterEach(() => {
    cleanup();
  });

  it("uses one neutral default for Claude and Codex while text and data retain identity", () => {
    render(
      <div>
        <div data-testid="claude-identity">
          <ClaudeIdentityRow
            variant="compact"
            provider="claude"
            sourceName="Claude"
          />
        </div>
        <div data-testid="codex-identity">
          <ClaudeIdentityRow
            variant="compact"
            provider="codex"
            sourceName="Codex"
          />
        </div>
      </div>,
    );

    const claudeRow = screen.getByTestId("claude-identity");
    const codexRow = screen.getByTestId("codex-identity");
    const claudeMark = claudeRow.querySelector("svg")!;
    const codexMark = codexRow.querySelector("svg")!;

    expect(claudeRow).toHaveTextContent("Claude");
    expect(claudeRow).not.toHaveTextContent("Codex");
    expect(codexRow).toHaveTextContent("Codex");
    expect(codexRow).not.toHaveTextContent("Claude");
    expect(claudeMark).toHaveAttribute("data-agent-mark", "claude");
    expect(codexMark).toHaveAttribute("data-agent-mark", "codex");
    expect(claudeMark).toHaveAttribute("data-agent-glyph", "rest");
    expect(codexMark).toHaveAttribute("data-agent-glyph", "rest");
    expect(claudeMark.querySelector("image")?.getAttribute("href")).toBe(codexMark.querySelector("image")?.getAttribute("href"));
    expect(claudeMark.querySelector("circle, polygon")).toBeNull();
  });

  it("renders every typed variant deterministically and keeps them distinct", () => {
    const { container, rerender } = render(
      <div>
        {AGENT_MARK_VARIANTS.map((variant) => (
          <AgentMark key={variant} provider="claude" variant={variant} />
        ))}
      </div>,
    );
    const first = Array.from(container.querySelectorAll("svg")).map(
      (mark) => `${mark.getAttribute("data-agent-mark-variant")}:${mark.innerHTML}`,
    );

    rerender(
      <div>
        {AGENT_MARK_VARIANTS.map((variant) => (
          <AgentMark key={variant} provider="claude" variant={variant} />
        ))}
      </div>,
    );
    const second = Array.from(container.querySelectorAll("svg")).map(
      (mark) => `${mark.getAttribute("data-agent-mark-variant")}:${mark.innerHTML}`,
    );

    expect(first).toEqual(second);
    expect(first).toHaveLength(4);
    expect(new Set(first).size).toBe(4);
  });

  it("exposes one deterministic comparison sheet with every variant and provider", () => {
    render(<AgentMarkComparisonSurface />);

    expect(screen.getByTestId("agent-mark-comparison")).toHaveAttribute(
      "data-ui-qa-ready",
      "true",
    );
    expect(screen.getByText("Recommended")).toBeInTheDocument();
    for (const variant of AGENT_MARK_VARIANTS) {
      const column = document.querySelector(
        `[data-agent-mark-comparison-variant="${variant}"]`,
      )!;
      expect(column).toBeInTheDocument();
      expect(
        column.querySelectorAll(`[data-agent-mark-variant="${variant}"]`),
      ).toHaveLength(2);
      expect(
        column.querySelector('[data-agent-mark="claude"]'),
      ).not.toBeNull();
      expect(
        column.querySelector('[data-agent-mark="codex"]'),
      ).not.toBeNull();
    }
  });
});

// Task 4.7: the resting rail's blocked-session line, rendered directly.
describe("ReplyRail resting copy (Task 4.7)", () => {
  afterEach(() => {
    cleanup();
  });

  it("renders the locked blocked line with no user gesture when can_reply is false", () => {
    render(<ReplyRail phase="resting" canReply={false} />);
    expect(screen.getByTestId("claude-reply-rail")).toHaveTextContent(
      "Reply in your terminal",
    );
    expect(screen.queryByText("Speak or type")).toBeNull();
  });

  it("keeps the ordinary placeholder when can_reply is true", () => {
    render(<ReplyRail phase="resting" canReply />);
    expect(screen.getByTestId("claude-reply-rail")).toHaveTextContent(
      "Speak or type",
    );
    expect(
      screen.queryByText("Reply in your terminal"),
    ).toBeNull();
  });

  it("never adds a second textarea for the blocked line", () => {
    const { container } = render(
      <ReplyRail phase="resting" canReply={false} />,
    );
    expect(container.querySelectorAll("textarea")).toHaveLength(0);
  });
});

// Task 4.3: SessionTabs and the status glyph, rendered directly rather than
// through mountHud - unit tests of the tab strip in isolation. The strip's
// place in the card's measured chrome is covered by the Task 4.4 tests above.
describe("SessionTabs (Task 4.3)", () => {
  afterEach(() => {
    cleanup();
  });

  function makeSession(overrides: Partial<HudSession> = {}): HudSession {
    return {
      session_handle: "handle-a",
      agent: "claude",
      source_name: "whisperstream",
      unread: false,
      needs_input: false,
      can_reply: true,
      ...overrides,
    };
  }

  it("renders no strip for a single session (decision 3)", () => {
    render(
      <SessionTabs
        sessions={[makeSession()]}
        activeHandle="handle-a"
        onSelect={() => {}}
      />,
    );
    expect(screen.queryByTestId("hud-session-tabs")).not.toBeInTheDocument();
  });

  it("renders the strip for two or more sessions", () => {
    render(
      <SessionTabs
        sessions={[
          makeSession({ session_handle: "a" }),
          makeSession({ session_handle: "b", source_name: "site" }),
        ]}
        activeHandle="a"
        onSelect={() => {}}
      />,
    );
    expect(screen.getByTestId("hud-session-tabs")).toBeInTheDocument();
    expect(screen.getAllByRole("tab")).toHaveLength(2);
  });

  it("disambiguates mixed-provider duplicate names and scrolls selection into view", () => {
    const scrollIntoView = vi.fn();
    const original = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    const claude = makeSession({ session_handle: "claude", source_name: "site" });
    const codex = makeSession({
      session_handle: "codex",
      agent: "codex",
      source_name: "site",
    });
    const { rerender } = render(
      <SessionTabs
        sessions={[claude, codex]}
        activeHandle="claude"
        onSelect={() => {}}
      />,
    );

    expect(screen.getByRole("tablist", { name: "Coding agent sessions" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /^Claude, site\./ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /^Codex, site\./ })).toBeInTheDocument();
    expect(document.querySelectorAll('[data-agent-mark="claude"]')).toHaveLength(1);
    expect(document.querySelectorAll('[data-agent-mark="codex"]')).toHaveLength(1);

    rerender(
      <SessionTabs
        sessions={[claude, codex]}
        activeHandle="codex"
        onSelect={() => {}}
      />,
    );
    expect(scrollIntoView).toHaveBeenCalledWith({
      block: "nearest",
      inline: "nearest",
    });
    HTMLElement.prototype.scrollIntoView = original;
  });

  it("matches the locked accessible-name copy byte for byte", () => {
    const idle = makeSession({ session_handle: "a", source_name: "whisperstream" });
    const unread = makeSession({
      session_handle: "b",
      source_name: "site",
      unread: true,
    });
    const needsInput = makeSession({
      session_handle: "c",
      source_name: "docs",
      needs_input: true,
    });
    const cannotReply = makeSession({
      session_handle: "d",
      source_name: "api",
      can_reply: false,
    });

    expect(sessionTabAccessibleName(idle, true)).toBe(
      "Claude, whisperstream. Nothing new in this session. Current session.",
    );
    expect(sessionTabAccessibleName(idle, false)).toBe(
      "Claude, whisperstream. Nothing new in this session.",
    );
    expect(sessionTabAccessibleName(unread, false)).toBe(
      "Claude, site. New response you have not read.",
    );
    expect(sessionTabAccessibleName(needsInput, false)).toBe(
      "Claude, docs. Waiting for your answer.",
    );
    expect(sessionTabAccessibleName(cannotReply, false)).toBe(
      "Claude, api. Read only. Reply in your terminal.",
    );

    render(
      <SessionTabs
        sessions={[idle, unread, needsInput, cannotReply]}
        activeHandle="a"
        onSelect={() => {}}
      />,
    );
    expect(
      screen.getByRole("tab", {
        name: "Claude, whisperstream. Nothing new in this session. Current session.",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "Claude, site. New response you have not read." }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "Claude, docs. Waiting for your answer." }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", {
        name: "Claude, api. Read only. Reply in your terminal.",
      }),
    ).toBeInTheDocument();
  });

  it("never shares a state word between needs-input and cannot-reply", () => {
    // Accessibility note (copy-review.md): a screen reader user cycling tabs
    // must never hear the same word ("blocked") for two different states.
    const needsInput = sessionTabAccessibleName(
      makeSession({ needs_input: true }),
      false,
    );
    const cannotReply = sessionTabAccessibleName(
      makeSession({ can_reply: false }),
      false,
    );
    expect(needsInput).not.toMatch(/blocked/i);
    expect(cannotReply).not.toMatch(/blocked/i);
  });

  it("calls onSelect with the clicked session's handle", () => {
    const onSelect = vi.fn();
    render(
      <SessionTabs
        sessions={[
          makeSession({ session_handle: "a" }),
          makeSession({ session_handle: "b", source_name: "site" }),
        ]}
        activeHandle="a"
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: /site/ }));
    expect(onSelect).toHaveBeenCalledWith("b");
  });

  it("never selects while locked", () => {
    const onSelect = vi.fn();
    render(
      <SessionTabs
        sessions={[
          makeSession({ session_handle: "a" }),
          makeSession({ session_handle: "b", source_name: "site" }),
        ]}
        activeHandle="a"
        onSelect={onSelect}
        locked
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: /site/ }));
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByRole("tab", { name: /site/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("reserves the 8px status slot even when nothing is painted (decision 5)", () => {
    render(
      <SessionTabs
        sessions={[
          makeSession({ session_handle: "a" }),
          makeSession({ session_handle: "b", source_name: "site" }),
        ]}
        activeHandle="a"
        onSelect={() => {}}
      />,
    );
    const idleGlyphSlot = screen
      .getByRole("tab", { name: /whisperstream/ })
      .querySelector('[aria-hidden="true"]');
    expect(idleGlyphSlot).toBeInTheDocument();
  });

  it("SessionTabStatusGlyph renders nothing painted for an idle session", () => {
    const { container } = render(
      <SessionTabStatusGlyph session={makeSession()} />,
    );
    expect(container.querySelector("svg")).not.toBeInTheDocument();
  });

  it("SessionTabStatusGlyph paints the needs-input bubble over unread/blocked (priority order)", () => {
    const { container } = render(
      <SessionTabStatusGlyph
        session={makeSession({
          needs_input: true,
          unread: true,
          can_reply: false,
        })}
      />,
    );
    // Needs-input renders as a rect+path bubble; unread/blocked render a bare
    // circle. Only one glyph occupies the slot (design.md section D).
    expect(container.querySelectorAll("svg")).toHaveLength(1);
    expect(container.querySelector("rect")).toBeInTheDocument();
  });
});
