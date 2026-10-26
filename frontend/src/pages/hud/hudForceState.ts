/**
 * Dev-only forced HUD states.
 *
 * The backend simulator fires real hook payloads, which is right for proving
 * the transport works but wrong for looking at a design: every state rides the
 * real lifecycle, so a response pill lives six seconds, a reply route has to be
 * opened inside that window, and an expanded card disappears while you are
 * still reading it. Reviewing a surface that way is brittle by construction.
 *
 * This is the other half: each state is a frozen fixture injected straight into
 * the values `HUD.tsx` derives its presentation from. No timers, no expiry, no
 * backend. It stays on screen until you pick another one or clear it, which is
 * what a design review and a screenshot both need.
 *
 * Transport is `localStorage` plus the `storage` event, the same channel the
 * theme switch already uses to reach the HUD window. The UI Test tab writes the
 * key; the HUD window, which is a separate webview, receives it.
 *
 * Never reachable in production: every consumer is behind `import.meta.env.DEV`.
 */

import { useEffect, useState } from "react";
import type { AgentActiveQuestion, AgentConversation, AgentInteraction, AgentResponseCompletion, HudSession, ReplyDraftPhase } from "../../lib/socket/agentEvents";
import type { AppEvent, EnhancementTimeoutProvider } from "../../lib/socket/events";
import type { AgentCompletionQueueState } from "../../lib/agentCompletionQueue";

export const HUD_FORCE_KEY = "ws-hud-force";

/** Same-window notification; `storage` only fires in OTHER windows. */
export const HUD_FORCE_EVENT = "ws-hud-force-change";

export interface HudForcedState {
  id: string;
  group: string;
  label: string;
  note: string;
  /** Response FIFO snapshot. Absent means no response is showing. */
  completionQueue?: AgentCompletionQueueState;
  /** Expanded (`View full response`) rather than the compact pill. */
  expanded?: boolean;
  conversation?: AgentConversation;
  /** Live reply text, as recognition would deliver it. */
  replyPreview?: string;
  question?: AgentActiveQuestion;
  interactions?: Record<string, AgentInteraction>;
  /** Frozen managed-session strip. Uses the production card and routing model. */
  sessions?: HudSession[];
  activeSessionHandle?: string;
  /** Holds the compact pill's 2,200ms sent confirmation (design.md state 6)
   *  indefinitely, bypassing the real edge-detected timer so a reviewer or a
   *  screenshot has time to look at it before it clears. `undefined` (the
   *  default) leaves the real dynamic value alone; `null` explicitly forces
   *  "no sent confirmation" even if a real one happens to be in flight. */
  sentReply?: { completionId: string; generation: number; text: string } | null;
  /** Overrides the permission pill's detail line. AgentInteraction is
   *  content-free on the wire (no detail text crosses it yet), so this is the
   *  only way to review the single-line vs two-line/spine split ahead of a
   *  future round wiring real text through. */
  permissionDetail?: string | null;
  /** Real backend events for ONE dictation, folded through the production
   *  `dictationReducer` in `HUD.tsx` rather than hand-assembled into a model.
   *  That is the whole point: an effective-profile stamp only paints for an
   *  authoritative, recording-correlated dictation, so a fixture that skipped
   *  the reducer could show a stamp the product can never reach. Serializable
   *  by construction, because these are the wire payloads. */
  dictationEvents?: AppEvent[];
  /** Freezes the `enhance_timeout` transient on its bounded provider. Held
   *  rather than timed: the real notice clears after two seconds, which is not
   *  long enough to review or screenshot. */
  timeoutProvider?: EnhancementTimeoutProvider;
  /** Freezes the matching generic error transient for visual review. */
  errorProvider?: EnhancementTimeoutProvider;
  /** Holds the debounced microphone-open presentation for visual QA. */
  startupArming?: boolean;
  /** Live Preview fixtures only: hold the preview at its expanded size. */
  previewExpanded?: boolean;
  /** Frozen `claude_reply_auto_submit`; absent means off. */
  replyAutoSubmit?: boolean;
  /** The reply owner's draft for whichever surface is shown (the question
   *  when one is active, otherwise the card or console). `refused` holds the
   *  undeliverable state with its Copy action; `refusedReason` picks which
   *  refusal (default "refused"). `held` marks a sending draft that waits for
   *  the agent's next Stop. */
  replyDraft?: {
    phase: ReplyDraftPhase;
    text?: string;
    refused?: boolean;
    refusedReason?: "refused" | "not_delivered";
    held?: boolean;
  };
}

// ---------------------------------------------------------------------------
// Fixture builders
// ---------------------------------------------------------------------------

const SHORT_TEXT = "Refactored the config loader and the tests still pass.";

const LONG_TEXT = Array.from(
  { length: 7 },
  (_unused, index) =>
    `Paragraph ${index + 1}. This response is deliberately long, so the ` +
    "expanded card has to decide what to do with a body that does not fit. " +
    "It also carries an em dash, curly quotes and accents so an encoding " +
    "regression shows on sight: café, 東京, and a dash to finish.",
).join("\n\n");

/** Long enough to clear `RESPONSE_MAX_H` (720) on any reasonable monitor, so
 *  the well is guaranteed scrollable and the 24px bottom fade paints. */
const CEILING_TEXT = Array.from(
  { length: 24 },
  (_unused, index) =>
    `Paragraph ${index + 1} of a response well past the expanded card's ` +
    "natural-height ceiling. This is the state that proves the well scrolls " +
    "internally instead of the card growing past the work area, and that the " +
    "24px bottom fade paints only while there is more to see below the fold.",
).join("\n\n");

const DRAFT_TEXT =
  "Let's go with staging first, then production if the café check passes.";

function completion(
  text: string,
  spokenSummary: string | null,
  sourceLabel = "Claude",
  sourceName?: string,
): AgentResponseCompletion {
  return {
    completion_id: "forced-completion",
    sequence: 1,
    kind: "response",
    text,
    agent: "claude",
    source_label: sourceLabel,
    source_name: sourceName,
    empty: text === "",
    spoken_summary: spokenSummary,
  };
}

function queue(
  item: AgentResponseCompletion,
  options: {
    /** A reply is in progress, so the Stop is engaged. */
    replying?: boolean;
    speaking?: boolean;
    engaged?: boolean;
    showSummaryText?: boolean;
    passiveTotalSeconds?: number;
    passiveRemainingSeconds?: number;
    /** Extra queued-behind-active completion ids, so `queuedCompletionCount`
     *  is non-zero and the compact pill's queue chip renders. Content of the
     *  extra ids never displays; only the count does. */
    queuedExtra?: number;
  } = {},
): AgentCompletionQueueState {
  const engaged = options.engaged ?? options.replying === true;
  const extraIds = Array.from(
    { length: options.queuedExtra ?? 0 },
    (_unused, index) => `forced-queued-${index + 1}`,
  );
  const totalSeconds = options.passiveTotalSeconds ?? 6;
  const remainingSeconds =
    options.passiveRemainingSeconds ?? (engaged ? 0 : 4);
  return {
    queue: [item],
    orderedCompletionIds: [item.completion_id, ...extraIds],
    phase: options.speaking ? "active_speaking" : "active_grace",
    active: {
      completion_id: item.completion_id,
      phase: options.speaking ? "active_speaking" : "active_grace",
      request_id: "forced-request",
      session_handle: "forced-session",
      generation: 1,
      engaged,
      routable: true,
      passive_total_seconds: totalSeconds,
      passive_remaining_seconds: remainingSeconds,
      hard_remaining_seconds: engaged ? 113 : totalSeconds,
    },
    quietDelayRemainingSeconds: 0,
    revision: 1,
    showSummaryText: options.showSummaryText ?? false,
  };
}

/** Agent text exercising the Markdown renderer: heading, bold, inline code,
 *  a list, a fenced block wider than the card and deliberate single line
 *  breaks. */
const MARKDOWN_TURNS: AgentConversation["turns"] = [
  {
    id: "md-1",
    role: "user",
    text: "Why does push-to-talk stop working after I close the console?",
  },
  {
    id: "md-2",
    role: "assistant",
    text:
      "## Cause\n\nThe router kept the **agent window** open after the console " +
      "closed, so the hotkey was consumed without starting a recording.\n\n" +
      "- `start_routed_reply` returned *handled* with no route\n" +
      "- the toggle latch never reset\n\n" +
      "```python\nif route is None:\n    return None  # fall through to ordinary dictation, never consume the press\n```\n\n" +
      "Push-to-talk now records\ninto the focused app whenever no agent window is open.",
  },
  {
    id: "md-3",
    role: "user",
    text: "Great. Does Enter still submit while the console is open?",
  },
  {
    id: "md-4",
    role: "assistant",
    text:
      "Yes. **Enter** is claimed only while a draft is visible, and released " +
      "as soon as it sends, fails or closes.\n\n" +
      "1. Draft visible: Enter submits\n2. No draft: Enter goes to your app",
  },
];

/** Two managed sessions so the console carries its tab strip. */
function consoleSessions(active: Partial<HudSession> = {}): HudSession[] {
  const base = {
    unread: false,
    needs_input: false,
    can_reply: true,
    accepts_reply: true,
    reply_held: false,
  };
  return [
    {
      ...base,
      session_handle: "console-claude-whisperstream",
      agent: "claude",
      source_name: "whisperstream",
      conversation_handle: "forced-session",
      reply_target: "conv:forced-session",
      latest_response: "Enter is claimed only while a draft is visible.",
      ...active,
    },
    {
      ...base,
      session_handle: "console-codex-site",
      agent: "codex",
      source_name: "site",
      unread: true,
      latest_response: "Codex finished the site task.",
    },
  ];
}

function question(
  overrides: Partial<AgentActiveQuestion> = {},
): AgentActiveQuestion {
  return {
    session_handle: "forced-session",
    generation: 1,
    phase: "grace",
    questions: [
      {
        question: "Which environment should I deploy to?",
        header: "Deploy",
        multiSelect: false,
        options: [
          { label: "Staging", description: "Use the internal test environment" },
          { label: "Production", description: "Publish to customers" },
        ],
      },
    ],
    current_index: 0,
    request_id: "forced-request",
    routable: true,
    capture_available: true,
    remaining_seconds: 6,
    answers: {},
    ...overrides,
  };
}

function permission(): Record<string, AgentInteraction> {
  return {
    "forced-permission": {
      request_id: "forced-permission",
      session_handle: "forced-session",
      kind: "permission",
      sequence: 1,
      status: "pending",
      active: true,
      engaged: true,
      passive_seconds: 0,
      remaining_seconds: 118,
      hard_remaining_seconds: 118,
    },
  };
}

/** One dictation's lifecycle event, exactly as the backend publishes it.
 *  `preview_enabled: false` keeps these fixtures on the compact pill, which is
 *  the vessel the stamp was designed into; Live Preview's mirrored stamp is
 *  covered by the HUD suite. */
function dictationLifecycle(
  phase: "recording" | "processing" | "idle",
  recordingId = 41,
  previewEnabled = false,
): AppEvent {
  return {
    type: "dictation.state",
    version: 1,
    recording_id: recordingId,
    phase,
    origin: "push_to_talk",
    hud_enabled: true,
    preview_enabled: previewEnabled,
    screen_reader_detected: false,
  };
}

/** One Live Preview revision for that same dictation. */
function dictationPreview(text: string, recordingId = 41): AppEvent {
  return {
    type: "transcription.preview",
    recording_id: recordingId,
    revision: 1,
    text,
    stable_char_count: text.length,
    presentation_version: 1,
    mutable_start_char_count: text.length,
    freeze: false,
  };
}

const PREVIEW_TEXT =
  "Okay so the export bug is fixed, and next I want to look at why the " +
  "console clips long code lines when the reply is wider than the card.";

/** A managed-session strip of any size, for the tab layout rules. */
function namedSessions(
  names: readonly string[],
  mixed: boolean,
): HudSession[] {
  return names.map((name, index) => ({
    session_handle: `console-${index}-${name}`,
    agent: mixed && index % 3 === 1 ? "codex" : "claude",
    source_name: name,
    conversation_handle: index === 0 ? "forced-session" : undefined,
    reply_target: index === 0 ? "conv:forced-session" : undefined,
    unread: index % 4 === 2,
    needs_input: index === 3,
    can_reply: true,
    accepts_reply: true,
    reply_held: false,
    latest_response: "Finished the selected managed-session task.",
  }));
}

const FIVE_SESSION_NAMES = [
  "whisperstream",
  "site",
  "docs-sync",
  "fixture-studio",
  "release-notes",
] as const;

const TEN_SESSION_NAMES = [
  "whisperstream",
  "site",
  "docs-sync",
  "fixture-studio",
  "release-notes",
  "macos-port",
  "tts-bench",
  "local-llm",
  "onboarding",
  "marketing",
] as const;

/** The effective profile for that same dictation. `recording_id` is the
 *  correlation the reducer enforces, so it is repeated here rather than
 *  defaulted apart from the lifecycle event. */
function dictationProfile(
  profileId: string,
  name: string,
  recordingId = 41,
): AppEvent {
  return {
    type: "enhancement.profile",
    recording_id: recordingId,
    profile_id: profileId,
    name,
  };
}

// ---------------------------------------------------------------------------
// The catalogue
// ---------------------------------------------------------------------------

export const HUD_FORCED_STATES: HudForcedState[] = [
  {
    id: "agent_summary_text",
    group: "Response",
    label: "Written summary, resting",
    note: "Text-only resting summary with 20s total and current window.",
    completionQueue: queue(
      completion(
        SHORT_TEXT,
        "Refactored the config loader and the tests still pass.",
        "Claude session",
        "whisperstream",
      ),
      {
        showSummaryText: true,
        passiveTotalSeconds: 20,
        passiveRemainingSeconds: 20,
      },
    ),
  },
  {
    id: "agent_summary_markdown",
    group: "Response",
    label: "Written summary, Markdown",
    note: "A summary carrying bold and inline code renders them, not raw markers.",
    completionQueue: queue(
      completion(
        SHORT_TEXT,
        "**Hotkeys fixed.** Push-to-talk records whenever no agent window is " +
          "open, and `Enter` is claimed only while a draft is visible.",
        "Claude session",
        "whisperstream",
      ),
      {
        showSummaryText: true,
        passiveTotalSeconds: 20,
        passiveRemainingSeconds: 20,
      },
    ),
  },
  {
    id: "agent_summary_reply",
    group: "Response",
    label: "Written summary, live reply",
    note: "Written summary with a live reply transcript beneath it.",
    completionQueue: queue(
      completion(
        SHORT_TEXT,
        "Refactored the config loader and the tests still pass.",
        "Claude session",
        "whisperstream",
      ),
      {
        showSummaryText: true,
        replying: true,
        passiveTotalSeconds: 20,
        passiveRemainingSeconds: 0,
      },
    ),
    replyDraft: { phase: "recording" },
    replyPreview: "run the full verification suite next",
  },
  {
    id: "response_compact",
    group: "Response",
    label: "Compact pill",
    note: "What arrives with a message. Owner wants logo plus session name only.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
  },
  {
    id: "response_speaking",
    group: "Response",
    label: "Compact, speaking",
    note: "The speaking cluster while the summary is read aloud.",
    completionQueue: queue(
      completion(SHORT_TEXT, "Refactored the config loader."),
      { speaking: true, queuedExtra: 2 },
    ),
  },
  {
    id: "agent_summary_speaking",
    group: "Response",
    label: "Written summary, speaking",
    note: "The card identity row while the summary is read aloud.",
    completionQueue: queue(
      completion(
        SHORT_TEXT,
        "Refactored the config loader and the tests still pass.",
        "Claude session",
        "whisperstream",
      ),
      { speaking: true, showSummaryText: true },
    ),
  },
  {
    id: "response_compact_queued",
    group: "Response",
    label: "Compact, with queue chip",
    note: "Resting, one row, but a second completion is waiting behind this one.",
    completionQueue: queue(
      completion(SHORT_TEXT, "Refactored the config loader."),
      { queuedExtra: 2 },
    ),
  },
  {
    id: "response_expanded",
    group: "Response",
    label: "Expanded, short",
    note: "View full response on a body that fits.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
  },
  {
    id: "conversation_expanded",
    group: "Response",
    label: "Expanded, short",
    note: "View full response on a body that fits.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    conversation: { session_handle: "forced-session", activity: "working", turns: [
      { id: "turn-one", role: "user", text: "Review the queue and reply behavior." },
      { id: "turn-two", role: "assistant", text: "# Queue updated\n\nEscape now skips the current response and starts the next waiting message. The count stays visible while speech plays." },
      { id: "turn-three", role: "user", text: "Also make the reply field grow with longer messages." },
      { id: "turn-four", role: "assistant", text: "The composer now grows with your message. Long replies scroll only when they reach the screen limit." },
    ] },
    expanded: true,
  },
  {
    id: "console_markdown",
    group: "Response",
    label: "Console, Markdown turns",
    note: "Managed console history with headings, lists, inline and fenced code.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
    activeSessionHandle: "console-claude-whisperstream",
    sessions: consoleSessions(),
    conversation: {
      session_handle: "forced-session",
      activity: "finished",
      turns: MARKDOWN_TURNS,
    },
  },
  {
    id: "response_expanded_long",
    group: "Response",
    label: "Expanded, long",
    note: "The scroll the owner does not want. Body must be shown whole.",
    completionQueue: queue(completion(LONG_TEXT, "Finished the long write-up.")),
    expanded: true,
  },
  {
    id: "response_no_marker",
    group: "Response",
    label: "Expanded, no spoken summary",
    note: "No marker, so nothing speaks and there is no summary strip.",
    completionQueue: queue(completion(SHORT_TEXT, null)),
    expanded: true,
  },
  {
    id: "response_expanded_named",
    group: "Response",
    label: "Expanded, named session",
    note: "The subordinate session-name suffix beside Claude, decision 9.",
    completionQueue: queue(
      completion(
        SHORT_TEXT,
        "Refactored the config loader.",
        "Claude session",
        "whisperstream",
      ),
    ),
    expanded: true,
  },
  {
    id: "response_expanded_ceiling",
    group: "Response",
    label: "Expanded, at ceiling",
    note: "Past the natural-height ceiling: the well scrolls and the 24px fade paints.",
    completionQueue: queue(completion(CEILING_TEXT, "Finished a very long write-up.")),
    expanded: true,
  },
  {
    id: "managed_sessions_mixed",
    group: "Response",
    label: "Mixed coding agent sessions",
    note: "Claude and Codex sessions with duplicate names and horizontal overflow.",
    completionQueue: queue(completion(SHORT_TEXT, "Finished the selected task.")),
    expanded: true,
    activeSessionHandle: "managed-codex-whisperstream",
    sessions: [
      ["managed-claude-whisperstream", "claude", "whisperstream", false],
      ["managed-codex-whisperstream", "codex", "whisperstream", false],
      ["managed-claude-site", "claude", "site", true],
      ["managed-codex-site", "codex", "site", false],
      ["managed-claude-docs", "claude", "docs", false],
      ["managed-codex-docs", "codex", "docs", true],
    ].map(([session_handle, agent, source_name, unread]) => ({
      session_handle: session_handle as string,
      agent: agent as "claude" | "codex",
      source_name: source_name as string,
      unread: unread as boolean,
      needs_input: false,
      can_reply: true,
      latest_response:
        agent === "codex"
          ? "Codex finished the selected managed-session task."
          : "Claude finished the selected managed-session task.",
    })),
  },
  {
    id: "console_sessions_five",
    group: "Response",
    label: "Console, five sessions",
    note: "Five tabs: full names while they fit, then even shrinking.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
    activeSessionHandle: "console-0-whisperstream",
    sessions: namedSessions(FIVE_SESSION_NAMES, false),
    conversation: {
      session_handle: "forced-session",
      activity: "finished",
      turns: MARKDOWN_TURNS,
    },
  },
  {
    id: "console_sessions_ten",
    group: "Response",
    label: "Console, ten sessions",
    note: "Ten tabs: minimum width reached, the strip scrolls with fade edges.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
    activeSessionHandle: "console-6-tts-bench",
    sessions: namedSessions(TEN_SESSION_NAMES, true),
    conversation: {
      session_handle: "forced-session",
      activity: "finished",
      turns: MARKDOWN_TURNS,
    },
  },
  {
    id: "response_empty",
    group: "Response",
    label: "Empty turn",
    note: "Pure-tool turn. Renders the Claude finished state.",
    completionQueue: queue(completion("", null)),
  },

  {
    id: "reply_listening",
    group: "Reply",
    label: "Listening, compact",
    note: "Speech arriving on the compact pill. Today it shows nothing.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader."), {
      replying: true,
    }),
    replyDraft: { phase: "recording" },
    replyPreview: "Let's go with staging first, then",
  },
  {
    id: "reply_listening_expanded",
    group: "Reply",
    label: "Listening, expanded",
    note: "Same speech with the full response above it.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader."), {
      replying: true,
    }),
    replyDraft: { phase: "recording" },
    replyPreview: "Let's go with staging first, then",
    expanded: true,
  },
  {
    id: "reply_processing",
    group: "Reply",
    label: "Processing",
    note: "Capture finished, transcript not yet authoritative.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader."), {
      replying: true,
    }),
    replyDraft: { phase: "processing" },
    expanded: true,
  },
  {
    id: "reply_draft",
    group: "Reply",
    label: "Draft ready, compact",
    note: "Auto-submit off: this is what Enter would send, and it is invisible.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader."), {
      replying: true,
    }),
    replyDraft: { phase: "ready", text: (DRAFT_TEXT + "\n\n").repeat(8) },
  },
  {
    id: "reply_draft_expanded",
    group: "Reply",
    label: "Draft ready, expanded",
    note: "The reply region under the full response.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader."), {
      replying: true,
    }),
    replyDraft: { phase: "ready", text: DRAFT_TEXT },
    expanded: true,
  },
  {
    id: "reply_editing",
    group: "Reply",
    label: "Editing the draft",
    note: "The input field. Owner wants one line that grows past one line.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader."), {
      replying: true,
    }),
    replyDraft: { phase: "editing", text: DRAFT_TEXT },
    expanded: true,
  },
  {
    id: "reply_sent",
    group: "Reply",
    label: "Sent confirmation, compact",
    note:
      "The 2,200ms state. Held here (not timed out) so it can be reviewed " +
      "and screenshotted; the real HUD clears it after 2,200ms.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    sentReply: {
      completionId: "forced-completion",
      generation: 1,
      text: (DRAFT_TEXT + "\n\n").repeat(5),
    },
  },
  {
    id: "reply_refused",
    group: "Reply",
    label: "Submit refused, compact",
    note: "The glance card keeps a refused reply with its reason and Copy.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader."), {
      replying: true,
    }),
    replyDraft: { phase: "editing", text: DRAFT_TEXT, refused: true },
  },
  {
    id: "reply_not_delivered",
    group: "Reply",
    label: "Send not collected, compact",
    note: "A send nobody collected comes back on the glance card with Copy.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader."), {
      replying: true,
    }),
    replyDraft: {
      phase: "editing",
      text: DRAFT_TEXT,
      refused: true,
      refusedReason: "not_delivered",
    },
  },
  {
    id: "reply_held",
    group: "Reply",
    label: "Reply held, compact",
    note: "A reply sent mid-turn waits on the glance card for the next Stop.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader."), {
      replying: true,
    }),
    replyDraft: { phase: "sending", text: DRAFT_TEXT, held: true },
  },
  {
    id: "console_typing",
    group: "Reply",
    label: "Console, typing a reply",
    note: "Typed text in the boxed field; the key cap lights and no hint shows.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
    activeSessionHandle: "console-claude-whisperstream",
    sessions: consoleSessions(),
    replyDraft: { phase: "editing", text: "Also check the Mac Escape path please" },
    conversation: {
      session_handle: "forced-session",
      activity: "finished",
      turns: MARKDOWN_TURNS,
    },
  },
  {
    id: "console_spoken_draft",
    group: "Reply",
    label: "Console, spoken draft waiting",
    note: "Auto-submit off: a dictated reply waits with the submit hint.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
    activeSessionHandle: "console-claude-whisperstream",
    sessions: consoleSessions(),
    replyDraft: { phase: "ready", text: "Merge it once CI passes, then tag the release." },
    conversation: {
      session_handle: "forced-session",
      activity: "finished",
      turns: MARKDOWN_TURNS,
    },
    replyAutoSubmit: false,
  },
  {
    id: "console_refused",
    group: "Reply",
    label: "Console, submit refused",
    note: "The backend refused the submit; the draft stays with the error line.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
    activeSessionHandle: "console-claude-whisperstream",
    sessions: consoleSessions(),
    replyDraft: {
      phase: "editing",
      text: "Merge it once CI passes, then tag the release.",
      refused: true,
    },
    conversation: {
      session_handle: "forced-session",
      activity: "finished",
      turns: MARKDOWN_TURNS,
    },
  },
  {
    id: "console_not_delivered",
    group: "Reply",
    label: "Console, send not collected",
    note: "A send nobody collected comes back with its reason and Copy.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
    activeSessionHandle: "console-claude-whisperstream",
    sessions: consoleSessions(),
    replyDraft: {
      phase: "editing",
      text: "Merge it once CI passes, then tag the release.",
      refused: true,
      refusedReason: "not_delivered",
    },
    conversation: {
      session_handle: "forced-session",
      activity: "finished",
      turns: MARKDOWN_TURNS,
    },
  },
  {
    id: "console_ended",
    group: "Reply",
    label: "Console, session ended",
    note: "The session ended with text in the field; it stays read-only with Copy.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
    activeSessionHandle: "console-claude-whisperstream",
    sessions: consoleSessions({ ended: true, can_reply: false, accepts_reply: false }),
    replyDraft: { phase: "editing", text: "Merge it once CI passes, then tag the release." },
    conversation: {
      session_handle: "forced-session",
      activity: "finished",
      turns: MARKDOWN_TURNS,
    },
  },
  {
    id: "console_held_draft",
    group: "Reply",
    label: "Console, held draft in the field",
    note: "A reply sent mid-turn waits in the field for the agent's next Stop.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
    activeSessionHandle: "console-claude-whisperstream",
    sessions: consoleSessions({ can_reply: false, accepts_reply: true }),
    replyDraft: {
      phase: "sending",
      text: "Merge it once CI passes, then tag the release.",
      held: true,
    },
    conversation: {
      session_handle: "forced-session",
      activity: "working",
      turns: MARKDOWN_TURNS,
    },
  },
  {
    id: "console_working",
    group: "Reply",
    label: "Console, agent working",
    note: "The working row closes the timeline; the field stays open to type ahead.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
    activeSessionHandle: "console-claude-whisperstream",
    sessions: consoleSessions({ can_reply: false, accepts_reply: true }),
    conversation: {
      session_handle: "forced-session",
      activity: "working",
      turns: MARKDOWN_TURNS,
    },
  },
  {
    id: "console_sent",
    group: "Reply",
    label: "Console, reply sent",
    note: "The reply is the newest pane with its sent line, while the agent works.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
    activeSessionHandle: "console-claude-whisperstream",
    sessions: consoleSessions({ can_reply: false, accepts_reply: true }),
    conversation: {
      session_handle: "forced-session",
      activity: "working",
      turns: [
        ...MARKDOWN_TURNS,
        { id: "md-5", role: "user", text: "Great. Merge it once CI passes." },
      ],
    },
  },
  {
    id: "console_held",
    group: "Reply",
    label: "Console, reply held",
    note: "Sent mid-turn to an agent that takes it at its next Stop.",
    completionQueue: queue(completion(SHORT_TEXT, "Refactored the config loader.")),
    expanded: true,
    activeSessionHandle: "console-claude-whisperstream",
    sessions: consoleSessions({
      can_reply: false,
      accepts_reply: true,
      reply_held: true,
    }),
    conversation: {
      session_handle: "forced-session",
      activity: "working",
      turns: [
        ...MARKDOWN_TURNS,
        { id: "md-5", role: "user", text: "Great. Merge it once CI passes." },
      ],
    },
  },

  {
    id: "question_single",
    group: "Question",
    label: "Single select",
    note: "The card is the question presentation. First click answers.",
    question: question(),
  },
  {
    id: "question_multi",
    group: "Question",
    label: "Multi select",
    note: "Selections persist; Enter sends.",
    question: question({
      questions: [
        {
          question: "Which checks should I run before merging?",
          header: "Checks",
          multiSelect: true,
          options: [
            { label: "Backend", description: "Run the pytest suite" },
            { label: "Frontend", description: "Run vitest and tsc" },
            { label: "Packaged build", description: "Rebuild the installer" },
          ],
        },
      ],
    }),
  },
  {
    id: "question_listening",
    group: "Question",
    label: "Listening for a reply",
    note: "Speech into a question, the state that showed nothing in round 03.",
    question: question({ phase: "grace" }),
    replyDraft: { phase: "recording" },
    replyPreview: "Staging first please",
  },
  {
    id: "question_draft",
    group: "Question",
    label: "Draft ready",
    note: "A spoken answer waiting for Enter.",
    question: question({ phase: "grace" }),
    replyDraft: { phase: "ready", text: "Staging first please" },
  },
  {
    id: "question_long",
    group: "Question",
    label: "Long labels",
    note: "Wrapping and truncation inside the card.",
    question: question({
      questions: [
        {
          question:
            "This question is itself quite long, because Claude sometimes " +
            "asks something that does not fit on one line at all. Which of " +
            "these should I do?",
          header: "Plan",
          multiSelect: false,
          options: [
            {
              label:
                "Rewrite the whole module from scratch, which is slower but " +
                "leaves a cleaner result",
              description: "Roughly two hours, and it touches every call site",
            },
            {
              label: "Patch the specific failure only",
              description: "Fast, but the design problem remains",
            },
          ],
        },
      ],
    }),
  },

  {
    id: "permission_pending",
    group: "Permission",
    label: "Needs approval",
    note: "Live for 120s in the backend; today the pill paints for about one.",
    interactions: permission(),
  },
  {
    id: "permission_single_line",
    group: "Permission",
    label: "Single-line detail",
    note:
      "No spine beside one short line, per design.md state 10: a 2px rule " +
      "beside one line is noise, not structure. (AgentInteraction carries no " +
      "detail text on the wire yet; this is forced for review.)",
    interactions: permission(),
    permissionDetail: "Run `npm run build` in whisperstream",
  },
  {
    id: "permission_two_line",
    group: "Permission",
    label: "Two-line detail",
    note:
      "The spine appears at two lines or more. (Same forced-detail caveat as " +
      "the single-line scenario.)",
    interactions: permission(),
    permissionDetail:
      "Run a recursive delete across the workspace build output and the " +
      "node_modules cache directories, which touches files outside git tracking.",
  },

  {
    id: "startup_arming",
    group: "Dictation",
    label: "Microphone arming",
    note: "The existing compact processing spinner reused during a slow microphone open.",
    startupArming: true,
  },
  {
    id: "dictation_recording_builtin",
    group: "Dictation",
    label: "Recording, built-in profile",
    note:
      "The stamped 100px vessel while dictation runs. Email's glyph derives " +
      "from its immutable id, so no icon key crosses the wire.",
    dictationEvents: [
      dictationLifecycle("recording"),
      dictationProfile("email", "Email"),
    ],
  },
  {
    id: "dictation_processing_builtin",
    group: "Dictation",
    label: "Processing, built-in profile",
    note:
      "The SAME dictation one phase later. Compared with the recording state " +
      "this is the proof that the vessel does not resize mid-dictation.",
    dictationEvents: [
      dictationLifecycle("recording"),
      dictationProfile("email", "Email"),
      dictationLifecycle("processing"),
    ],
  },
  {
    id: "dictation_recording_letter",
    group: "Dictation",
    label: "Recording, letter fallback",
    note:
      "A custom profile that stored no curated icon: the stamp falls back to " +
      "the name's first grapheme, at the 16px HUD tile size.",
    dictationEvents: [
      dictationLifecycle("recording"),
      dictationProfile("profile-release-notes", "Release notes"),
    ],
  },

  {
    id: "live_preview",
    group: "Dictation",
    label: "Live Preview, compact",
    note: "Raw dictation text with the expand control at rest.",
    dictationEvents: [
      dictationLifecycle("recording", 41, true),
      dictationPreview(PREVIEW_TEXT),
    ],
  },
  {
    id: "live_preview_first_line",
    group: "Dictation",
    label: "Live Preview, first line",
    note: "Text that has not scrolled yet: the top line sits unfaded.",
    dictationEvents: [
      dictationLifecycle("recording", 41, true),
      dictationPreview("Okay so the export bug is fixed."),
    ],
  },
  {
    id: "live_preview_expanded",
    group: "Dictation",
    label: "Live Preview, expanded",
    note: "The same preview opened to its reading size, with the collapse control.",
    dictationEvents: [
      dictationLifecycle("recording", 41, true),
      dictationPreview(PREVIEW_TEXT),
    ],
    previewExpanded: true,
  },

  {
    id: "enhance_timeout_local",
    group: "Enhancement",
    label: "Timeout, local model",
    note:
      "The widest label in the set (`Local model`) inside the unchanged " +
      "120px envelope. Held still; the real transient clears after 2,000ms.",
    timeoutProvider: "local",
  },
  {
    id: "enhance_timeout_anthropic",
    group: "Enhancement",
    label: "Timeout, Anthropic",
    note:
      "The widest cloud-provider label, for the same 80px text budget the " +
      "design measured rather than ellipsized.",
    timeoutProvider: "anthropic",
  },
  {
    id: "enhance_error_anthropic",
    group: "Enhancement",
    label: "Error, Anthropic",
    note:
      "The generic provider fallback in the unchanged 120px envelope, with " +
      "rose error treatment and the longest cloud-provider label.",
    errorProvider: "anthropic",
  },
];

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

export function forcedStateById(id: string | null): HudForcedState | null {
  if (!id) return null;
  return HUD_FORCED_STATES.find((state) => state.id === id) ?? null;
}

/** Stable external state ID used by the exact-route QA manifest. */
export function hudForcedStateQaId(fixtureId: string): string {
  if (fixtureId === "reply_draft_expanded") {
    return "hud.response-expanded-reply";
  }
  return `hud.${fixtureId.replace(/_/g, "-")}`;
}

/** Write the selection. Reaches other windows through the `storage` event. */
export function setForcedHudState(id: string | null): void {
  if (!import.meta.env.DEV) return;
  if (id === null) window.localStorage.removeItem(HUD_FORCE_KEY);
  else window.localStorage.setItem(HUD_FORCE_KEY, id);
  window.dispatchEvent(new Event(HUD_FORCE_EVENT));
}

export function readForcedHudStateId(): string | null {
  if (!import.meta.env.DEV) return null;
  try {
    return window.localStorage.getItem(HUD_FORCE_KEY);
  } catch {
    return null;
  }
}

/**
 * The forced state, or null. Dev-only: in a production build the whole body
 * is dead code behind `import.meta.env.DEV` and always returns null.
 */
export function useForcedHudState(): HudForcedState | null {
  const [id, setId] = useState<string | null>(() =>
    import.meta.env.DEV ? readForcedHudStateId() : null,
  );

  useEffect(() => {
    if (!import.meta.env.DEV) return;
    const sync = () => setId(readForcedHudStateId());
    // `storage` for the other window (the UI Test tab writes it), the custom
    // event for this one.
    window.addEventListener("storage", sync);
    window.addEventListener(HUD_FORCE_EVENT, sync);
    return () => {
      window.removeEventListener("storage", sync);
      window.removeEventListener(HUD_FORCE_EVENT, sync);
    };
  }, []);

  if (!import.meta.env.DEV) return null;
  return forcedStateById(id);
}
