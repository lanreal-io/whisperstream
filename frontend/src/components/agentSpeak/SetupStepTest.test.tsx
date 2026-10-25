import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MotionGlobalConfig } from "framer-motion";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Run every animation in this file instantly.
 *
 * jsdom drives `requestAnimationFrame` off a plain timer, so a faked clock owns
 * framer-motion's frameloop: releasing the fake timers while a frame is pending
 * discards it, the loop never reschedules, and every later `AnimatePresence` in
 * the FILE stops mounting its incoming child. This step has two nested
 * `AnimatePresence mode="wait"` crossfades and several fake-timer tests, so
 * that artifact would decide which assertions can be written rather than the
 * product deciding. Skipping animation duration removes the dependency
 * entirely; the crossfade's own timing is a presentation concern proved by UI
 * QA, not by jsdom.
 */
MotionGlobalConfig.skipAnimations = true;

import * as api_speech from "../../lib/api/speech";
import * as api_agents from "../../lib/api/agents";
import * as api_config from "../../lib/api/config";
import type { AppConfig } from "../../lib/api/config";
import type { CodingAgentProvider } from "../../lib/api/agents";
import type { SpeechStatus } from "../../lib/api/speech";
import type { AppEvent } from "../../lib/socket/events";
import { SetupStepTest, type SetupStepTestProps } from "./SetupStepTest";

vi.mock("../../lib/api/agents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/agents")>();
  return {
    ...actual,
    armSetupDeliveryPreview: vi.fn(async () => ({ ok: true })),
    fireSetupQuestion: vi.fn(async () => ({ ok: true })),
    teardownSetup: vi.fn(async () => ({ ok: true })),
  };
});
vi.mock("../../lib/api/speech", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/speech")>();
  return {
    ...actual,
    getSpeechStatus: vi.fn(),
    startSpeechModelDownload: vi.fn(async () => undefined),
  };
});
vi.mock("../../lib/api/config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/config")>();
  return {
    ...actual,
    saveConfig: vi.fn(async (config: AppConfig) => config),
  };
});

const RUNG_1 = "Claude Code is no longer connected. Reconnect it in Configure.";
const RUNG_3 = "Speech is off, so nothing will be spoken. Turn it on in Configure.";
const RUNG_4 = "This test needs Claude's own summaries. Change it in Configure.";
const PREVIEW_ARM_PENDING = "Getting the test ready...";
const PREVIEW_ARM_FAILED =
  "Could not set up the test preview. Try reopening setup.";
/** `B13` / `B14` / `B15`: the three speaking download states. `B16`, state A's
 *  status line, is a locked ABSENCE and therefore has no constant. */
const STARTING_DOWNLOAD = "Starting your voice download...";
const QUEUED_CAPTION =
  "Your voice is queued behind another download. It starts on its own.";
const START_FAILED =
  "Could not start the voice download. Whisperstream keeps trying.";
/** `A11`, `DownloadCard`'s own default subtitle: the presence of this string is
 *  how these tests assert that a PROGRESS BAR is on screen, and its absence is
 *  how they assert states C and D draw none. */
const DOWNLOAD_CARD_SUBTITLE = "We'll continue automatically when it's ready.";
/** `B11` / `B12`: the elapsed-time hints that replaced post-fire rungs 2 and 6. */
const HINT_RELOAD = "Nothing yet. If you just connected, run /reload-plugins first.";
const HINT_HEARD =
  "If you have not heard anything, check your output device and volume.";
const QUESTION_FAILURE = "The question did not reach the HUD. Try it again.";

/** `B4` / `B5`: the checklist half's locked heading and lede. */
const HEADING = "Try it with Claude Code";
const LEDE =
  "Two quick checks. This first one follows a real message in your own Claude Code session.";
const CODEX_HEADING = "Try it with Codex";
const CODEX_LEDE = "Follow one real message in your own Codex session.";

function makeConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    claude_hud_enabled: true,
    claude_hud_show_text: true,
    agent_speak_setup_completed: false,
    agent_speech_enabled: true,
    agent_speech_summary_source: "agent",
    agent_speech_engine: "supertonic",
    claude_view_shortcut: "ctrl+alt+`",
    push_to_talk_shortcut: "right shift",
    toggle_shortcut: "",
    claude_reply_auto_submit: false,
    agent_summary_text_enabled: false,
    ...overrides,
  } as AppConfig;
}

function makeStatus(overrides: Partial<SpeechStatus> = {}): SpeechStatus {
  return {
    requested: "supertonic",
    preferred_engine: "supertonic",
    resolved_engine: "supertonic",
    active_engine: null,
    reason: "supertonic",
    memory: { total_mb: 16384, available_mb: 8192, eligible: true },
    kokoro_memory_tier: "recommended",
    engines: {
      kokoro: {
        key: "kokoro",
        label: "Kokoro",
        installed: false,
        downloading: false,
        download_bytes: 340_000_000,
        installed_bytes: 360_000_000,
      },
      supertonic: {
        key: "supertonic",
        label: "Supertonic",
        installed: true,
        downloading: false,
        download_bytes: 90_000_000,
        installed_bytes: 96_000_000,
      },
    },
    current_download: null,
    recommended_downloads: [],
    ...overrides,
  } as SpeechStatus;
}

/** Supertonic absent and IDLE: state B's exact precondition (design decision
 *  24). This used to fall through to the test variant and surface the deleted
 *  not-installed rung; it is now the state step 4 starts the download from. */
function absentStatus(): SpeechStatus {
  const status = makeStatus();
  status.engines.supertonic.installed = false;
  return status;
}

/** Supertonic absent AND actively transferring: state A. */
function downloadingStatus(): SpeechStatus {
  const status = makeStatus({ current_download: "supertonic" });
  status.engines.supertonic.installed = false;
  status.engines.supertonic.downloading = true;
  return status;
}

/**
 * Fake ONLY the timeout family. Vitest's default `toFake` set also replaces
 * `requestAnimationFrame` and `performance`, and framer-motion's frameloop is
 * module-level: a rAF scheduled under fake time is dropped by
 * `useRealTimers()`, the loop never restarts, and every later
 * `AnimatePresence` in the FILE stops swapping its children. That is a test
 * harness artifact, not a component bug, and this is the fix for it.
 */
function useTimeoutFakes() {
  vi.useFakeTimers({
    shouldAdvanceTime: true,
    toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
  });
}

let handlers: Array<(event: AppEvent) => void> = [];

function emit(event: AppEvent) {
  act(() => {
    for (const handler of [...handlers]) handler(event);
  });
}

function renderStep(overrides: Partial<SetupStepTestProps> = {}) {
  const props: SetupStepTestProps = {
    config: makeConfig(),
    onCanAdvanceChange: vi.fn(),
    onChildDialogOpenChange: vi.fn(),
    onGhostVisibleChange: vi.fn(),
    subscribe: (handler) => {
      handlers.push(handler);
      return () => {
        handlers = handlers.filter((h) => h !== handler);
      };
    },
    speechStatus: makeStatus(),
    provider: "claude",
    ...overrides,
  };
  return { ...render(<SetupStepTest {...props} />), props };
}

/** Act two's trigger. QUERY, not get: act two is absent from the DOM until the
 *  swap, so "there is no trigger yet" is a legitimate answer, not a failure. */
function triggers() {
  return screen.queryAllByRole("button", {
    name: "Try it",
  }) as HTMLButtonElement[];
}

function actOne() {
  return document.querySelector('[data-setup-act="1"]');
}

function actTwo() {
  return document.querySelector('[data-setup-act="2"]');
}

/** The swap delay, mirrored from the component (design decisions 31 and 32,
 *  revised in agent-onboarding-improvements: a plain delay, not a focused dwell). */
const SWAP_DELAY_MS = 1_600;

/**
 * Complete act one and let the swap delay elapse, so act two replaces it.
 * Requires `useTimeoutFakes()` before the render: the delay is a timeout.
 */
async function swapToActTwo() {
  await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
  completeChecklist();
  await act(async () => {
    vi.advanceTimersByTime(SWAP_DELAY_MS + 200);
  });
  await waitFor(() => expect(actTwo()).not.toBeNull());
}

function statusLines(): string[] {
  return Array.from(
    document.querySelectorAll<HTMLElement>("p.min-h-\\[14px\\]"),
  ).map((line) => line.textContent ?? "");
}

/** Act two owns the first reserved line (it renders inside the step's body) and
 *  only exists after the swap; the step frame's own reserved line renders after
 *  its children, so it is always the LAST one, in both acts. It carries the
 *  preconditions, the hints, `B9`, and then `B10`. */
function questionStatusEl(): HTMLElement {
  return document.querySelectorAll<HTMLElement>("p.min-h-\\[14px\\]")[0];
}

function stepStatusEl(): HTMLElement {
  const lines = document.querySelectorAll<HTMLElement>("p.min-h-\\[14px\\]");
  return lines[lines.length - 1];
}

function lineStates(): string[] {
  return Array.from(
    document.querySelectorAll<HTMLElement>("[data-setup-checklist-line]"),
  ).map((line) => line.getAttribute("data-state") ?? "");
}

/** The reply owner's snapshot. Line 4's detector reads only receipt ids. */
function replySnapshot(revision: number, receiptIds: string[] = []): AppEvent {
  return {
    type: "agent.reply.snapshot",
    version: 1,
    epoch: "e1",
    revision,
    foreground: null,
    drafts: [],
    receipts: receiptIds.map((draft_id) => ({ target_id: "conv:s", draft_id, result: "sent" as const, text: "reply" })),
    enter_owner: "none",
  };
}

/** One backend-authoritative response-queue snapshot. */
function queueSnapshot(
  revision: number,
  active: {
    completion_id: string;
    generation: number;
    agent?: CodingAgentProvider;
  } | null,
  phase: "idle" | "active_speaking" | "quiet_delay" = "active_speaking",
  show_summary_text?: boolean,
): AppEvent {
  return {
    type: "agent.response_queue.snapshot",
    version: 1,
    revision,
    phase,
    ordered_completion_ids: active ? [active.completion_id] : [],
    completions: [],
    show_summary_text,
    active: active
      ? {
          completion_id: active.completion_id,
          phase: "active_speaking",
          request_id: null,
          session_handle: "s",
          generation: active.generation,
          engaged: true,
          routable: true,
          passive_total_seconds: 60,
          passive_remaining_seconds: 60,
          hard_remaining_seconds: 120,
          agent: active.agent ?? "claude",
        }
      : null,
    quiet_delay_remaining_seconds: 0,
  };
}

function completionEvent(provider: CodingAgentProvider): AppEvent {
  return {
    type: "agent.completion",
    version: 1,
    show_text: false,
    completion: {
      completion_id: `${provider}-completion`,
      sequence: 1,
      kind: "response",
      text: "",
      agent: provider,
      source_label: provider,
      empty: false,
      spoken_summary: null,
    },
    pending_completion_ids: [],
  };
}

/** The whole observed chain, in order, as a REAL Claude turn would produce it.
 *  There is nothing to click: this half fires nothing. */
function completeChecklist(provider: CodingAgentProvider = "claude") {
  emit(completionEvent(provider));
  emit({ type: "speech.state", state: "speaking" });
  emit({
    type: "agent.compose.final",
    version: 1,
    session_handle: "s",
    generation: 1,
    recording_id: 1,
    text: "a private reply the modal must never show",
  });
  emit(replySnapshot(1));
  emit(replySnapshot(2, ["d1"]));
}

beforeEach(() => {
  handlers = [];
  vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(makeStatus());
  vi.mocked(api_agents.fireSetupQuestion).mockResolvedValue({ ok: true });
  vi.mocked(api_agents.teardownSetup).mockResolvedValue({ ok: true });
  vi.mocked(api_speech.startSpeechModelDownload).mockResolvedValue(undefined);
  vi.mocked(api_config.saveConfig).mockImplementation(async (config) => config as AppConfig);
});

afterEach(() => {
  // Real timers BEFORE `cleanup()`: unmounting under fake time leaves
  // framer-motion's module-level frameloop with a scheduled frame that
  // `useRealTimers()` then throws away, and every later `AnimatePresence` in
  // the file stops swapping its children.
  vi.useRealTimers();
  cleanup();
  vi.clearAllMocks();
});

describe("SetupStepTest — the live checklist", () => {
  it("bounds waiting status reads, aborts timed-out requests, and aborts on dismissal", async () => {
    useTimeoutFakes();
    const missing = makeStatus();
    missing.engines.supertonic.installed = false;
    const signals: AbortSignal[] = [];
    vi.mocked(api_speech.getSpeechStatus).mockImplementation((signal) => {
      signals.push(signal!);
      return new Promise(() => {});
    });
    const { unmount } = renderStep({ speechStatus: missing });
    await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
    expect(signals).toHaveLength(1);
    expect(signals[0].aborted).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(signals[0].aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(signals).toHaveLength(2);
    unmount();
    expect(signals[1].aborted).toBe(true);
  });

  it("renders ONE step heading over both acts, and no trigger on the checklist", async () => {
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    // `B4` is the step's own `h2` and the focus target on every step change.
    expect(screen.getByRole("heading", { name: HEADING, level: 2 })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(1);
    // `B5`, whose COUNT clause is load-bearing.
    expect(screen.getByText(LEDE)).toBeInTheDocument();
    // Act two is HIDDEN until the checklist completes (design decision 32), so
    // neither its heading nor its trigger exists yet. The half is a pure
    // observer: `fireSetupResponse` has no caller here any more, and the only
    // `Try it` in the flow arrives with act two.
    expect(actOne()).not.toBeNull();
    expect(actTwo()).toBeNull();
    expect(
      screen.queryByRole("heading", { name: "Answer a question", level: 3 }),
    ).toBeNull();
    expect(triggers()).toHaveLength(0);
  });

  it("renders four lines with their locked titles and static sub-text", async () => {
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    await waitFor(() => expect(lineStates()).toEqual(["active", "pending", "pending", "pending"]));
    expect(
      screen.getByRole("heading", { name: "Send a message in Claude Code" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Press Enter to send your reply" }),
    ).toBeInTheDocument();
    // Line 1's sub-text must name `/reload-plugins`, in every state.
    expect(
      screen.getByText(
        "Ask your own Claude Code session anything. If you just connected, run /reload-plugins there first.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps Continue enabled and owns no child dialog", async () => {
    const { props } = renderStep();
    await waitFor(() => expect(props.onCanAdvanceChange).toHaveBeenCalledWith(true));
    expect(props.onCanAdvanceChange).not.toHaveBeenCalledWith(false);
    expect(props.onChildDialogOpenChange).toHaveBeenCalledWith(false);
  });

  it("lights each line from its own real event and never mirrors the reply text", async () => {
    const { container } = renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    emit({ type: "agent.completion" } as unknown as AppEvent);
    expect(lineStates()).toEqual(["done", "active", "pending", "pending"]);

    emit({ type: "speech.state", state: "speaking" });
    expect(lineStates()).toEqual(["done", "done", "active", "pending"]);

    emit({
      type: "agent.compose.final",
      version: 1,
      session_handle: "s",
      generation: 1,
      recording_id: 3,
      text: "a private reply the modal must never show",
    });
    expect(lineStates()).toEqual(["done", "done", "done", "active"]);
    expect(container.textContent).not.toContain("a private reply");
  });

  it("back-fills: a later proof completes every earlier line, and no line un-completes", async () => {
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    // Lines 1 and 2's events never arrive (a dropped `speech.state`, a
    // completion that landed before the step mounted). The compose final still
    // proves all three.
    emit({
      type: "agent.compose.final",
      version: 1,
      session_handle: "s",
      generation: 1,
      recording_id: 3,
      text: "unused",
    });
    expect(lineStates()).toEqual(["done", "done", "done", "active"]);

    // A late, out-of-order earlier proof must not walk the checklist backwards.
    emit({ type: "agent.completion" } as unknown as AppEvent);
    emit({ type: "speech.state", state: "idle" });
    expect(lineStates()).toEqual(["done", "done", "done", "active"]);
  });

  it("completes line 4 only on a fresh delivery receipt", async () => {
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    // A receipt already present when the step opened is not this user's send.
    emit(replySnapshot(1, ["old"]));
    emit({
      type: "agent.compose.final",
      version: 1,
      session_handle: "s",
      generation: 1,
      recording_id: 3,
      text: "unused",
    });
    expect(lineStates()[3]).toBe("active");
    emit(replySnapshot(2, ["old"]));
    // A queue going idle without a receipt proves nothing.
    emit(queueSnapshot(3, null, "idle"));
    expect(lineStates()[3]).toBe("active");

    emit(replySnapshot(4, ["old", "new"]));
    expect(lineStates()).toEqual(["done", "done", "done", "done"]);
  });

  it("omits line 4 entirely when auto-submit is on, rather than rendering a dead line", async () => {
    renderStep({ config: makeConfig({ claude_reply_auto_submit: true }) });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    expect(lineStates()).toHaveLength(3);
    expect(
      screen.queryByRole("heading", { name: "Press Enter to send your reply" }),
    ).toBeNull();

    emit({
      type: "agent.compose.final",
      version: 1,
      session_handle: "s",
      generation: 1,
      recording_id: 3,
      text: "unused",
    });
    expect(lineStates()).toEqual(["done", "done", "done"]);
  });

  it("answers act two by digit and reaches Answered on the answered snapshot", async () => {
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    await swapToActTwo();
    await waitFor(() => expect(triggers()[0]).toBeEnabled());

    fireEvent.click(triggers()[0]);
    await waitFor(() => expect(api_agents.fireSetupQuestion).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Waiting for your answer")).toBeInTheDocument();

    const active = {
      session_handle: "s",
      generation: 1,
      phase: "surfaced" as const,
      questions: [],
      current_index: 0,
      request_id: "r",
      routable: true,
      capture_available: true,
      remaining_seconds: 60,
      compose_generation: null,
      capture_mode: null,
      answers: {},
    };
    emit({ type: "agent.question.snapshot", version: 1, revision: 1, active });
    expect(screen.getByText("Waiting for your answer")).toBeInTheDocument();

    emit({
      type: "agent.question.snapshot",
      version: 1,
      revision: 2,
      active: {
        ...active,
        phase: "answered",
        answers: { "Which sounds better?": "A quieter voice" },
      },
    });
    expect(screen.getByText("Answered")).toBeInTheDocument();
    // The verbatim answers map is read for its PHASE only; its content is
    // never rendered.
    expect(document.body.textContent).not.toContain("Which sounds better?:");
  });

  it("keeps act two's locked copy and its trigger", async () => {
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    await swapToActTwo();

    expect(
      screen.getByText("This is a practice question. Which sounds better?"),
    ).toBeInTheDocument();
    expect(screen.getByText("A quieter voice")).toBeInTheDocument();
    expect(screen.getByText("A louder voice")).toBeInTheDocument();
    // Locked copy 22, the highest trust-stakes string in the flow.
    expect(
      screen.getByText(
        "Whisperstream asks this one. Nothing is sent to Claude Code.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Not started")).toBeInTheDocument();
  });
});

describe("SetupStepTest — agent-agnostic copy while unresolved", () => {
  it("shows the agnostic heading, lede, checklist, and destination with no responder yet", async () => {
    renderStep({
      provider: null,
      config: makeConfig({ codex_hud_enabled: true, claude_hud_enabled: true }),
    });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    expect(
      screen.getByRole("heading", {
        name: "Try it with your coding agent",
        level: 2,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Follow one real message from your coding agent."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        name: "Send a message in your coding agent",
        level: 3,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Ask your coding agent anything. If you just connected it, reload it first.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Your words land in the HUD reply box. Press Enter to send them to your coding agent.",
      ),
    ).toBeInTheDocument();
    // No named provider before a responder is known.
    expect(document.body.textContent).not.toContain("Try it with Codex");
    expect(document.body.textContent).not.toContain("Try it with Claude Code");
    expect(document.body.textContent).not.toContain("Two quick checks");
  });

  it("resolves to the responder on the first real completion and switches to its named copy", async () => {
    renderStep({
      provider: null,
      config: makeConfig({ codex_hud_enabled: true, claude_hud_enabled: true }),
    });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    emit(completionEvent("claude"));
    expect(
      screen.getByRole("heading", { name: HEADING, level: 2 }),
    ).toBeInTheDocument();
    expect(screen.getByText(LEDE)).toBeInTheDocument();
    // The resolving completion also advances line 1.
    expect(lineStates()[0]).toBe("done");
    expect(document.body.textContent).not.toContain("your coding agent");
  });
});

describe("SetupStepTest — persists the per-agent verified flag on completion", () => {
  it("writes claude_verified once when the Claude checklist completes", async () => {
    renderStep({ provider: "claude" });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    completeChecklist("claude");
    await waitFor(() => expect(api_config.saveConfig).toHaveBeenCalledTimes(1));
    expect(api_config.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ claude_verified: true }),
    );
  });

  it("writes codex_verified when the Codex checklist completes", async () => {
    renderStep({
      provider: "codex",
      config: makeConfig({ codex_hud_enabled: true, claude_hud_enabled: true }),
    });
    await waitFor(() =>
      expect(lineStates()).toEqual(["active", "pending", "pending", "pending"]),
    );

    completeChecklist("codex");
    await waitFor(() =>
      expect(api_config.saveConfig).toHaveBeenCalledWith(
        expect.objectContaining({ codex_verified: true }),
      ),
    );
  });

  it("skips the write when the flag is already set", async () => {
    renderStep({ config: makeConfig({ claude_verified: true }) });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    completeChecklist("claude");
    // A short settle window: the effect would have fired synchronously after the
    // last event if it were going to write at all.
    await act(async () => {});
    expect(api_config.saveConfig).not.toHaveBeenCalled();
  });
});

describe("SetupStepTest - Codex provider path", () => {
  it("uses Codex's exact one-check copy and never claims structured questions", async () => {
    useTimeoutFakes();
    renderStep({
      provider: "codex",
      config: makeConfig({ codex_hud_enabled: true, claude_hud_enabled: true }),
    });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    expect(
      screen.getByRole("heading", { name: CODEX_HEADING, level: 2 }),
    ).toBeInTheDocument();
    expect(screen.getByText(CODEX_LEDE)).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Send a message in Codex", level: 3 }),
    ).toBeInTheDocument();
    expect(document.body.textContent).toContain(
      "Ask your own Codex session anything. If you just connected, run /hooks there first.",
    );
    expect(document.body.textContent).not.toContain("Two quick checks");
    expect(document.body.textContent).not.toContain("Answer a question");
    expect(document.body.textContent).not.toContain("practice question");

    completeChecklist("codex");
    expect(lineStates()).toEqual(["done", "done", "done", "done"]);
    await act(async () => {
      vi.advanceTimersByTime(SWAP_DELAY_MS + 200);
    });

    expect(actOne()).not.toBeNull();
    expect(actTwo()).toBeNull();
    expect(document.querySelector("#setup-trigger-question")).toBeNull();
    expect(api_agents.fireSetupQuestion).not.toHaveBeenCalled();
  });

  it("starts the proof only from the selected provider's completion", async () => {
    renderStep({
      provider: "codex",
      config: makeConfig({ codex_hud_enabled: true, claude_hud_enabled: true }),
    });
    await waitFor(() =>
      expect(lineStates()).toEqual(["active", "pending", "pending", "pending"]),
    );

    emit(completionEvent("claude"));
    emit({ type: "speech.state", state: "speaking" });
    emit({
      type: "agent.compose.final",
      version: 1,
      session_handle: "other",
      generation: 1,
      recording_id: 1,
      text: "not the selected provider",
    });
    expect(lineStates()).toEqual(["active", "pending", "pending", "pending"]);

    emit(completionEvent("codex"));
    expect(lineStates()).toEqual(["done", "active", "pending", "pending"]);
    emit({ type: "speech.state", state: "speaking" });
    emit({
      type: "agent.compose.final",
      version: 1,
      session_handle: "codex-session",
      generation: 1,
      recording_id: 2,
      text: "private",
    });
    emit(replySnapshot(1));
    emit(replySnapshot(2, ["d1"]));
    expect(lineStates()).toEqual(["done", "done", "done", "done"]);
    expect(document.body.textContent).not.toContain("not the selected provider");
    expect(document.body.textContent).not.toContain("private");
  });

  it("names Codex in the disconnected precondition", async () => {
    renderStep({
      provider: "codex",
      config: makeConfig({ codex_hud_enabled: false, claude_hud_enabled: true }),
    });
    await waitFor(() =>
      expect(stepStatusEl()).toHaveTextContent(
        "Codex is no longer connected. Reconnect it in Configure.",
      ),
    );
    expect(lineStates()).toEqual(["pending", "pending", "pending", "pending"]);
  });
});

describe("SetupStepTest — the preconditions", () => {
  it("rung 1: not connected, as an error that blocks the whole checklist", async () => {
    renderStep({ config: makeConfig({ claude_hud_enabled: false }) });
    await waitFor(() => expect(stepStatusEl()).toHaveTextContent(RUNG_1));
    expect(stepStatusEl().getAttribute("role")).toBe("alert");
    // Blocked renders every line pending: nothing is proved, and a lit line
    // would contradict the line beside it. The user is not sent away.
    expect(lineStates()).toEqual(["pending", "pending", "pending", "pending"]);
    // A live precondition keeps the user on act one, so act two (and its
    // trigger) is not even in the DOM to be pressed.
    expect(actTwo()).toBeNull();
    expect(triggers()).toHaveLength(0);
  });

  it("rung 3: speech off, as attention on a disabled trigger", async () => {
    renderStep({ config: makeConfig({ agent_speech_enabled: false }) });
    await waitFor(() => expect(stepStatusEl()).toHaveTextContent(RUNG_3));
    expect(stepStatusEl().getAttribute("role")).toBe("status");
    // A live precondition keeps the user on act one, so act two (and its
    // trigger) is not even in the DOM to be pressed.
    expect(actTwo()).toBeNull();
    expect(triggers()).toHaveLength(0);
  });

  it("rung 4: summary source is not the agent, as an error on a disabled trigger", async () => {
    renderStep({
      config: makeConfig({ agent_speech_summary_source: "whisperstream" }),
    });
    await waitFor(() => expect(stepStatusEl()).toHaveTextContent(RUNG_4));
    expect(stepStatusEl().getAttribute("role")).toBe("alert");
    // A live precondition keeps the user on act one, so act two (and its
    // trigger) is not even in the DOM to be pressed.
    expect(actTwo()).toBeNull();
    expect(triggers()).toHaveLength(0);
  });

  it("healthy leaves the reserved lines empty and never unmounts them", async () => {
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    // Act one: the step frame's reserved line, alone and empty.
    expect(statusLines()).toEqual([""]);

    await swapToActTwo();
    // Act two: the step frame's line (now carrying `B10`) plus act two's own,
    // both reserved and pre-mounted.
    expect(statusLines()).toEqual([
      "",
      "Second check ready. Answer a question.",
    ]);
  });

  it("does not collapse the rungs: each condition keeps its own string", async () => {
    const seen = new Set<string>();
    for (const [config, expected] of [
      [makeConfig({ claude_hud_enabled: false }), RUNG_1],
      [makeConfig({ agent_speech_enabled: false }), RUNG_3],
      [makeConfig({ agent_speech_summary_source: "whisperstream" }), RUNG_4],
    ] as Array<[AppConfig, string]>) {
      const { unmount } = renderStep({ config });
      await waitFor(() => expect(stepStatusEl()).toHaveTextContent(expected));
      seen.add(expected);
      unmount();
    }
    expect(seen.size).toBe(3);
  });
});

describe("SetupStepTest — the download-waiting variant", () => {
  // THE OWNER'S FIRST COMPLAINT. The shipped bug rendered a progress bar
  // titled `Downloading the voice model` INSIDE a section titled `Hear a
  // reply`: two titles on one waiting screen. The variant now replaces the
  // whole step, heading included.
  it("replaces the WHOLE step including the heading", async () => {
    const waiting = downloadingStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(waiting);
    const { props } = renderStep({ speechStatus: waiting });

    // `A9` is the step's own `h2`, and it is the only step heading here.
    expect(
      screen.getByRole("heading", { name: "Downloading your voice", level: 2 }),
    ).toBeInTheDocument();
    expect(screen.queryByText(HEADING)).toBeNull();
    expect(screen.queryByText("Answer a question")).toBeNull();
    expect(lineStates()).toEqual([]);
    // `A10`: no lede. `A11`: an empty `DownloadCard` title, so only its own
    // default subtitle speaks.
    expect(screen.queryByText("Downloading the voice model")).toBeNull();
    expect(
      screen.getByText("We'll continue automatically when it's ready."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try it" })).toBeNull();
    await waitFor(() =>
      expect(props.onGhostVisibleChange).toHaveBeenCalledWith(true),
    );
  });

  it("flips to the test variant only on a getSpeechStatus re-check, not on the event alone", async () => {
    const waiting = downloadingStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(waiting);
    renderStep({ speechStatus: waiting });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(1));

    // The event alone is not the gate: the re-check still reports absent
    // (a cancelled or failed transfer), so the variant holds.
    emit({ type: "download.complete", kind: "tts", engine: "supertonic" });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("button", { name: "Try it" })).toBeNull();

    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(makeStatus());
    emit({ type: "download.complete", kind: "tts", engine: "supertonic" });
    // The test variant opens on ACT ONE, the live checklist. Act two is not on
    // screen yet, so the checklist is what proves the flip.
    await waitFor(() => expect(actOne()).not.toBeNull());
  });

  it("pins the variant once and never transitions test back to waiting", async () => {
    renderStep({ speechStatus: makeStatus() });
    await waitFor(() => expect(actOne()).not.toBeNull());

    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(downloadingStatus());
    emit({ type: "download.complete", kind: "tts", engine: "supertonic" });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(2));
    expect(actOne()).not.toBeNull();
    expect(
      screen.queryByRole("heading", { name: "Downloading your voice" }),
    ).toBeNull();
  });

  it("ignores a Parakeet download: it neither re-checks nor moves the bar", async () => {
    const waiting = downloadingStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(waiting);
    renderStep({ speechStatus: waiting });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(1));

    emit({
      type: "download.progress",
      downloaded: 50,
      total: 100,
      speed_bps: 10,
      kind: "parakeet",
    });
    emit({ type: "download.complete", kind: "parakeet" });

    // No re-check, and the card is still in its indeterminate phase because
    // the untagged progress never reached it.
    expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("50%")).toBeNull();
    expect(screen.getByText("Connecting…")).toBeInTheDocument();

    emit({
      type: "download.progress",
      downloaded: 50,
      total: 100,
      speed_bps: 10,
      kind: "tts",
      engine: "supertonic",
    });
    await waitFor(() => expect(screen.getByText("50%")).toBeInTheDocument());
  });
});

/**
 * THE OWNER'S THIRD COMPLAINT, on seeing the retired rung 5: *"that should
 * never happen, we should show the voice downloading window until its ready."*
 *
 * The gate is now `installed`, and an absent voice can only ever resolve to one
 * of the four download states of design decision 24.
 */
describe("SetupStepTest — an absent voice is always a download screen", () => {
  it("never renders the deleted not-installed rung, and shows the download screen instead", async () => {
    const absent = absentStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(absent);
    renderStep({ speechStatus: absent });

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Downloading your voice", level: 2 }),
      ).toBeInTheDocument(),
    );
    // The string itself is deliberately not spelled anywhere in the tree, so it
    // is matched by shape rather than quoted back into `frontend/src`.
    expect(screen.queryByText(/not installed/i)).toBeNull();
    expect(screen.queryByText(HEADING)).toBeNull();
    expect(triggers()).toHaveLength(0);
  });

  it("state B: starts the download itself, once, and says so", async () => {
    const absent = absentStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(absent);
    renderStep({ speechStatus: absent });

    await waitFor(() =>
      expect(api_speech.startSpeechModelDownload).toHaveBeenCalledTimes(1),
    );
    expect(api_speech.startSpeechModelDownload).toHaveBeenCalledWith("supertonic");
    // `B13` is honest here and ONLY here: the start has already been posted.
    await waitFor(() =>
      expect(stepStatusEl()).toHaveTextContent(STARTING_DOWNLOAD),
    );
    // State B renders state A's bar; the marquee covers the gap before the
    // first progress event.
    expect(screen.getByText(DOWNLOAD_CARD_SUBTITLE)).toBeInTheDocument();
  });

  it("state B: `B13` retires the moment the first tts progress event lands", async () => {
    const absent = absentStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(absent);
    renderStep({ speechStatus: absent });
    await waitFor(() =>
      expect(stepStatusEl()).toHaveTextContent(STARTING_DOWNLOAD),
    );

    emit({
      type: "download.progress",
      downloaded: 10,
      total: 100,
      speed_bps: 10,
      kind: "tts",
      engine: "supertonic",
    });

    // `B16`: a locked absence. `A9` and `A11` already carry this state.
    await waitFor(() => expect(stepStatusEl()).toHaveTextContent(""));
    expect(screen.queryByText(STARTING_DOWNLOAD)).toBeNull();
  });

  it("does not start a download that is already running", async () => {
    const waiting = downloadingStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(waiting);
    renderStep({ speechStatus: waiting });

    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    // A second start against a live one is a 409 against yourself.
    expect(api_speech.startSpeechModelDownload).not.toHaveBeenCalled();
    // State A's status line is empty, not `B13`: this step did not start it.
    expect(stepStatusEl()).toHaveTextContent("");
  });

  it("does not start anything when the engine is already installed", async () => {
    renderStep({ speechStatus: makeStatus() });
    await waitFor(() => expect(actOne()).not.toBeNull());
    expect(api_speech.startSpeechModelDownload).not.toHaveBeenCalled();
  });

  it("posts the start exactly once across repeated authoritative re-checks", async () => {
    const absent = absentStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(absent);
    renderStep({ speechStatus: absent });
    await waitFor(() =>
      expect(api_speech.startSpeechModelDownload).toHaveBeenCalledTimes(1),
    );

    // Each of these drives a fresh `getSpeechStatus()` and therefore a fresh
    // `status` object into the effect's deps. The one-shot guard is what stops
    // that becoming one start per re-check.
    emit({ type: "download.complete", kind: "tts", engine: "supertonic" });
    emit({ type: "download.complete", kind: "tts", engine: "supertonic" });
    // Concurrent completion notices share one authoritative re-check.
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(2));
    expect(api_speech.startSpeechModelDownload).toHaveBeenCalledTimes(1);
  });

  it("state C: a 409 renders the halo and the queued caption, with NO progress bar", async () => {
    const absent = absentStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(absent);
    vi.mocked(api_speech.startSpeechModelDownload).mockRejectedValue(
      new Error("409: another download is already in flight"),
    );
    renderStep({ speechStatus: absent });

    await waitFor(() =>
      expect(screen.getByText(QUEUED_CAPTION)).toBeInTheDocument(),
    );
    // THE POINT OF THIS STATE: no bar. There is no progress to show, and a bar
    // that never moves is the defect the original narrow gate existed to stop.
    expect(screen.queryByText(DOWNLOAD_CARD_SUBTITLE)).toBeNull();
    expect(screen.queryByText("Connecting…")).toBeNull();
    expect(
      document.querySelector('[data-setup-download-stalled="queued"]'),
    ).not.toBeNull();
    // The heading is still `A9`, and the reserved status line stays empty: the
    // caption carries the whole explanation in this state.
    expect(
      screen.getByRole("heading", { name: "Downloading your voice", level: 2 }),
    ).toBeInTheDocument();
    expect(stepStatusEl()).toHaveTextContent("");
    expect(screen.queryByText(STARTING_DOWNLOAD)).toBeNull();
  });

  /**
   * REGRESSION, found by the round 02 UI QA sweep (issue 7).
   *
   * The start effect's deps move on every authoritative re-check and on every
   * 4s status poll, and its outcome used to be discarded by a `cancelled` flag
   * scoped to the effect RUN. So a poll landing between the POST and its
   * response threw the answer away: the one-shot guard refused to ask again,
   * `startOutcome` stayed `none`, no retry was scheduled, and the step sat in
   * state B forever, showing an indeterminate marquee for a download nothing
   * had accepted. React's StrictMode double-mount reproduced it every time,
   * which is how the queued state's UI QA fixture caught it.
   */
  it("keeps the start outcome when the effect re-runs before the POST settles", async () => {
    const absent = absentStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(absent);
    let refuse: (error: Error) => void = () => {};
    vi.mocked(api_speech.startSpeechModelDownload).mockReturnValue(
      new Promise<void>((_resolve, reject) => {
        refuse = reject;
      }),
    );
    renderStep({ speechStatus: absent });
    await waitFor(() =>
      expect(api_speech.startSpeechModelDownload).toHaveBeenCalledTimes(1),
    );

    // A re-check lands while the start is still in flight: a fresh `status`
    // object moves the effect's deps and re-runs it.
    emit({ type: "download.complete", kind: "tts", engine: "supertonic" });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(2));

    await act(async () => {
      refuse(new Error("409: another download is already in flight"));
    });

    await waitFor(() =>
      expect(screen.getByText(QUEUED_CAPTION)).toBeInTheDocument(),
    );
    expect(screen.queryByText(DOWNLOAD_CARD_SUBTITLE)).toBeNull();
  });

  it("state D: any other failure is `B15` at error tone, also with no bar", async () => {
    const absent = absentStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(absent);
    vi.mocked(api_speech.startSpeechModelDownload).mockRejectedValue(
      new Error("500: internal server error"),
    );
    renderStep({ speechStatus: absent });

    await waitFor(() => expect(stepStatusEl()).toHaveTextContent(START_FAILED));
    expect(stepStatusEl().getAttribute("role")).toBe("alert");
    expect(screen.queryByText(DOWNLOAD_CARD_SUBTITLE)).toBeNull();
    // It is NOT the queued state: no caption, and it must not borrow `B14`'s
    // "it starts on its own" promise.
    expect(screen.queryByText(QUEUED_CAPTION)).toBeNull();
    expect(
      document.querySelector('[data-setup-download-stalled="failed"]'),
    ).not.toBeNull();
  });

  it("crosses from the queued state into the download screen once installed", async () => {
    const absent = absentStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(absent);
    vi.mocked(api_speech.startSpeechModelDownload).mockRejectedValue(
      new Error("409: busy"),
    );
    renderStep({ speechStatus: absent });
    await waitFor(() =>
      expect(screen.getByText(QUEUED_CAPTION)).toBeInTheDocument(),
    );

    // The authoritative re-check, not the event alone, is the install gate.
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(makeStatus());
    emit({ type: "download.complete", kind: "tts", engine: "supertonic" });
    await waitFor(() => expect(actOne()).not.toBeNull());
    expect(screen.queryByText(QUEUED_CAPTION)).toBeNull();
  });

  it("keeps pin-once: an absent re-check never drags a pinned test variant back", async () => {
    renderStep({ speechStatus: makeStatus() });
    await waitFor(() => expect(actOne()).not.toBeNull());

    // Absent AND idle, which is now a download state rather than a rung. The
    // pin is what keeps it off screen.
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(absentStatus());
    emit({ type: "download.complete", kind: "tts", engine: "supertonic" });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalledTimes(2));
    expect(actOne()).not.toBeNull();
    expect(
      screen.queryByRole("heading", { name: "Downloading your voice" }),
    ).toBeNull();
    expect(screen.queryByText(QUEUED_CAPTION)).toBeNull();
    expect(api_speech.startSpeechModelDownload).not.toHaveBeenCalled();
  });
});

describe("SetupStepTest — teardown", () => {
  it("tears down on unmount", async () => {
    const { unmount } = renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    expect(api_agents.teardownSetup).not.toHaveBeenCalled();
    unmount();
    await waitFor(() => expect(api_agents.teardownSetup).toHaveBeenCalledTimes(1));
  });
});

/**
 * FAKE-TIMER TESTS RUN LAST, DELIBERATELY.
 *
 * jsdom's `requestAnimationFrame` is driven by a timer, so faking the timer
 * family kills it for the rest of the FILE: framer-motion never gets another
 * frame, an `AnimatePresence` exit never completes, and `mode="wait"` stops
 * swapping children. That is a harness artifact rather than a component
 * defect, and keeping every timeout-driven test after the presentation tests
 * is the fix. Do not interleave these back into the blocks above.
 */
describe("SetupStepTest — the elapsed-time hints (fake timers, run last)", () => {
  it("`B11` at 45s with line 1 still incomplete, at attention tone and never as a verdict", async () => {
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    expect(stepStatusEl().textContent).toBe("");

    await act(async () => {
      vi.advanceTimersByTime(46_000);
    });
    expect(stepStatusEl()).toHaveTextContent(HINT_RELOAD);
    // Attention, NOT error: nothing has been declared broken, and there is no
    // failure state on this half.
    expect(stepStatusEl().getAttribute("role")).toBe("status");
    // The line still waits; it is a hint, not a verdict.
    expect(lineStates()[0]).toBe("active");
  });

  it("names BOTH commands in the reconnect hint while unresolved (both connected)", async () => {
    useTimeoutFakes();
    renderStep({
      provider: null,
      config: makeConfig({ codex_hud_enabled: true, claude_hud_enabled: true }),
    });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    await act(async () => {
      vi.advanceTimersByTime(46_000);
    });
    expect(stepStatusEl()).toHaveTextContent(
      "Nothing yet. If you just connected, run /reload-plugins in Claude Code or /hooks in Codex.",
    );
  });

  it("retires `B11` the moment line 1 completes", async () => {
    useTimeoutFakes();
    renderStep();
    await act(async () => {
      vi.advanceTimersByTime(46_000);
    });
    expect(stepStatusEl()).toHaveTextContent(HINT_RELOAD);

    emit({ type: "agent.completion" } as unknown as AppEvent);
    expect(stepStatusEl().textContent).toBe("");
  });

  it("`B12` at 20s with line 1 done and line 2 not", async () => {
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    emit({ type: "agent.completion" } as unknown as AppEvent);
    await act(async () => {
      vi.advanceTimersByTime(21_000);
    });
    expect(stepStatusEl()).toHaveTextContent(HINT_HEARD);
    expect(stepStatusEl().getAttribute("role")).toBe("status");

    emit({ type: "speech.state", state: "speaking" });
    expect(stepStatusEl().textContent).toBe("");
  });

  it("keeps ONE reserved slot: a live precondition outranks a due hint", async () => {
    useTimeoutFakes();
    renderStep({ config: makeConfig({ claude_hud_enabled: false }) });
    await act(async () => {
      vi.advanceTimersByTime(46_000);
    });
    expect(stepStatusEl()).toHaveTextContent(RUNG_1);
    // ONE reserved slot on act one, carrying preconditions and hints in
    // priority order. Act two is not on screen to own a second.
    expect(statusLines()).toHaveLength(1);
  });

  it("surfaces act two's own failure, independently of the checklist", async () => {
    useTimeoutFakes();
    renderStep();
    await swapToActTwo();
    await waitFor(() => expect(triggers()[0]).toBeEnabled());

    fireEvent.click(triggers()[0]);
    await waitFor(() => expect(api_agents.fireSetupQuestion).toHaveBeenCalled());
    await act(async () => {
      vi.advanceTimersByTime(13_000);
    });
    expect(questionStatusEl()).toHaveTextContent(QUESTION_FAILURE);
    expect(questionStatusEl().getAttribute("role")).toBe("alert");
  });

  // design decision 9, widened this round: the escape also appears once the
  // step has been open 60 seconds with the checklist incomplete. This half has
  // no timeout to fail, so waiting is not evidence of anything.
  it("offers the ghost escape at 60s with the checklist incomplete", async () => {
    useTimeoutFakes();
    const { props } = renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    expect(props.onGhostVisibleChange).not.toHaveBeenCalledWith(true);

    await act(async () => {
      vi.advanceTimersByTime(61_000);
    });
    expect(props.onGhostVisibleChange).toHaveBeenLastCalledWith(true);
  });

  it("retracts the ghost escape once the checklist completes", async () => {
    useTimeoutFakes();
    const { props } = renderStep();
    await act(async () => {
      vi.advanceTimersByTime(61_000);
    });
    expect(props.onGhostVisibleChange).toHaveBeenLastCalledWith(true);

    completeChecklist();
    await waitFor(() =>
      expect(props.onGhostVisibleChange).toHaveBeenLastCalledWith(false),
    );
  });
});

/**
 * The swap (design decisions 31, 32 and 33, revised in
 * agent-onboarding-improvements). The load-bearing FIX is that the swap fires
 * on checklist completion WITHOUT requiring focus: the old design gated it on
 * 1.6s of continuous `document.hasFocus()` on the modal, which the user in their
 * own coding-agent window almost never gives, so exercise 2 rarely appeared. A
 * short plain delay remains only so a user still glancing at the finished
 * checklist is not swapped mid-glance.
 */
describe("SetupStepTest — the swap to act two", () => {
  it("holds the finished checklist for a short delay before swapping", async () => {
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    completeChecklist();
    expect(lineStates()).toEqual(["done", "done", "done", "done"]);
    // `B9`, polite and count-free, on the step's own reserved line.
    expect(stepStatusEl()).toHaveTextContent("Checklist complete.");
    expect(stepStatusEl().getAttribute("role")).toBe("status");

    // Just short of the delay: the checklist is still the whole body.
    await act(async () => {
      vi.advanceTimersByTime(SWAP_DELAY_MS - 200);
    });
    expect(actOne()).not.toBeNull();
    expect(actTwo()).toBeNull();

    await act(async () => {
      vi.advanceTimersByTime(400);
    });
    await waitFor(() => expect(actTwo()).not.toBeNull());
    expect(actOne()).toBeNull();
  });

  // THE FIX (research finding 7). The old focused-dwell gate required 1.6s of
  // continuous focus on the modal; a user who alt-tabbed to their agent window
  // to send the message never accrued it, so exercise 2 never appeared. The swap
  // now fires on the plain delay regardless of focus.
  it("swaps even when the document is blurred the whole time", async () => {
    useTimeoutFakes();
    const hasFocus = vi.spyOn(document, "hasFocus").mockReturnValue(false);
    try {
      renderStep();
      await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
      completeChecklist();

      await act(async () => {
        vi.advanceTimersByTime(SWAP_DELAY_MS + 200);
      });
      await waitFor(() => expect(actTwo()).not.toBeNull());
      expect(actOne()).toBeNull();
    } finally {
      hasFocus.mockRestore();
    }
  });

  it("swaps even if the document is never focused and no focus event fires", async () => {
    useTimeoutFakes();
    const hasFocus = vi.spyOn(document, "hasFocus").mockReturnValue(false);
    try {
      renderStep();
      await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
      completeChecklist();
      // No `focus` event is dispatched at all: the user stayed in their agent
      // window. The swap must still fire on the delay alone.
      await act(async () => {
        vi.advanceTimersByTime(SWAP_DELAY_MS * 2);
      });
      await waitFor(() => expect(actTwo()).not.toBeNull());
    } finally {
      hasFocus.mockRestore();
    }
  });

  it("does NOT move focus on the swap", async () => {
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    const before = document.activeElement;
    await swapToActTwo();
    // Decision 8 confines focus movement to step changes. A timer-driven focus
    // jump would yank a screen-reader user out of what they were reading.
    expect(document.activeElement).toBe(before);
  });

  it("changes the heading and the lede with the body, on the same `h2`", async () => {
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    const headingBefore = screen.getByRole("heading", { level: 2 });

    await swapToActTwo();

    const headingAfter = screen.getByRole("heading", { level: 2 });
    expect(headingAfter).toBe(headingBefore);
    expect(headingAfter).toHaveTextContent("Answer with a number");
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(1);
    // Copy-review Element 6b: the anti-voice cue is restored.
    expect(
      screen.getByText(
        "Last check. Press 1 or 2 to answer, not by speaking.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(LEDE)).toBeNull();
  });

  it("keeps the completed summary strip as the trace act one leaves behind", async () => {
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    await swapToActTwo();

    // `B8`, true in both the four-line and the three-line variants.
    expect(
      screen.getByText("First check done. Your reply reached Claude Code."),
    ).toBeInTheDocument();
    expect(document.querySelector("[data-setup-summary-strip]")).not.toBeNull();
    // The checklist itself is gone: replaced, not stacked.
    expect(document.querySelector("[data-setup-checklist]")).toBeNull();
  });

  it("announces the swap politely on the surviving step-level region", async () => {
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    await swapToActTwo();

    expect(stepStatusEl()).toHaveTextContent(
      "Second check ready. Answer a question.",
    );
    expect(stepStatusEl().getAttribute("role")).toBe("status");
  });

  it("is one-way: a later event never returns the user to the checklist", async () => {
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());
    await swapToActTwo();

    emit({ type: "agent.completion" } as unknown as AppEvent);
    emit({ type: "speech.state", state: "speaking" });
    expect(actTwo()).not.toBeNull();
    expect(document.querySelector("[data-setup-checklist]")).toBeNull();
  });

  it("keeps the swap gated behind a live precondition", async () => {
    useTimeoutFakes();
    renderStep({ config: makeConfig({ agent_speech_enabled: false }) });
    await waitFor(() => expect(stepStatusEl()).toHaveTextContent(RUNG_3));

    completeChecklist();
    await act(async () => {
      vi.advanceTimersByTime(SWAP_DELAY_MS * 4);
    });
    // Blocked renders every disc pending, so swapping to a "first check done"
    // summary would contradict the line beside it.
    expect(actTwo()).toBeNull();
    expect(lineStates()).toEqual(["pending", "pending", "pending", "pending"]);
  });
});

/**
 * The download retries, which are the reason `B14` and `B15` are allowed to
 * promise that the user has nothing to do. Fake-timer tests, so they live in
 * the file's fake-timer section by the convention above.
 */
describe("SetupStepTest — the download start retries", () => {
  it("state C retries every 5s and crosses into the download screen on success", async () => {
    useTimeoutFakes();
    const absent = absentStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(absent);
    vi.mocked(api_speech.startSpeechModelDownload).mockRejectedValueOnce(
      new Error("409: busy"),
    );
    renderStep({ speechStatus: absent });

    await waitFor(() =>
      expect(screen.getByText(QUEUED_CAPTION)).toBeInTheDocument(),
    );
    expect(api_speech.startSpeechModelDownload).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_100);
    });
    await waitFor(() =>
      expect(api_speech.startSpeechModelDownload).toHaveBeenCalledTimes(2),
    );
    // The retry was accepted, so the step is in state B again: bar plus `B13`.
    await waitFor(() =>
      expect(screen.getByText(DOWNLOAD_CARD_SUBTITLE)).toBeInTheDocument(),
    );
    expect(screen.queryByText(QUEUED_CAPTION)).toBeNull();
  });

  it("state D waits 15s before retrying, so a broken backend is not hammered", async () => {
    useTimeoutFakes();
    const absent = absentStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(absent);
    vi.mocked(api_speech.startSpeechModelDownload).mockRejectedValue(
      new Error("500: internal server error"),
    );
    renderStep({ speechStatus: absent });

    await waitFor(() => expect(stepStatusEl()).toHaveTextContent(START_FAILED));
    expect(api_speech.startSpeechModelDownload).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_100);
    });
    expect(api_speech.startSpeechModelDownload).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(11_000);
    });
    await waitFor(() =>
      expect(api_speech.startSpeechModelDownload).toHaveBeenCalledTimes(2),
    );
  });

  it("stops retrying once the download is actually in flight", async () => {
    useTimeoutFakes();
    const absent = absentStatus();
    vi.mocked(api_speech.getSpeechStatus).mockResolvedValue(absent);
    vi.mocked(api_speech.startSpeechModelDownload).mockRejectedValue(
      new Error("409: busy"),
    );
    renderStep({ speechStatus: absent });
    await waitFor(() =>
      expect(screen.getByText(QUEUED_CAPTION)).toBeInTheDocument(),
    );

    emit({
      type: "download.progress",
      downloaded: 10,
      total: 100,
      speed_bps: 10,
      kind: "tts",
      engine: "supertonic",
    });
    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });
    expect(api_speech.startSpeechModelDownload).toHaveBeenCalledTimes(1);
  });
});

describe("SetupStepTest — text-only mode", () => {
  it("arms the delivery preview on mount with local mode selections", async () => {
    useTimeoutFakes();
    renderStep({
      config: makeConfig({
        agent_speech_enabled: false,
        agent_summary_text_enabled: true,
      }),
    });
    await waitFor(() =>
      expect(api_agents.armSetupDeliveryPreview).toHaveBeenCalledWith({
        show_summary_text: true,
        agent_speech_enabled: false,
      }),
    );
  });

  it("renders truthful checklist copy and advances line 2 only on authoritative queue snapshot with show_summary_text true", async () => {
    useTimeoutFakes();
    renderStep({
      config: makeConfig({
        agent_speech_enabled: false,
        agent_summary_text_enabled: true,
      }),
    });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    // Precondition check: not blocked by RUNG_3
    expect(stepStatusEl().textContent).toBe("");
    expect(
      screen.getByRole("heading", {
        name: "See the reply in Whisperstream.",
        level: 3,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Read the summary in the overlay."),
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(
      "Hear the reply in Whisperstream",
    );

    // Completion advances ONLY line 1, not line 2
    emit(completionEvent("claude"));
    expect(lineStates()).toEqual(["done", "active", "pending", "pending"]);

    // Queue snapshot with show_summary_text=false does NOT advance line 2
    emit(
      queueSnapshot(
        1,
        {
          completion_id: "claude-completion",
          generation: 1,
          agent: "claude",
        },
        "active_speaking",
        false,
      ),
    );
    expect(lineStates()).toEqual(["done", "active", "pending", "pending"]);

    // Queue snapshot with show_summary_text=true advances line 2 to done
    emit(
      queueSnapshot(
        2,
        {
          completion_id: "claude-completion",
          generation: 1,
          agent: "claude",
        },
        "active_speaking",
        true,
      ),
    );
    expect(lineStates()).toEqual(["done", "done", "active", "pending"]);

    // Hold dictation key and answer
    emit({
      type: "agent.compose.final",
      version: 1,
      session_handle: "s",
      generation: 1,
      recording_id: 1,
      text: "hello world",
    });
    expect(lineStates()).toEqual(["done", "done", "done", "active"]);

    // Press Enter to send (idle edge)
    emit(replySnapshot(3));
    emit(replySnapshot(4, ["d1"]));
    expect(lineStates()).toEqual(["done", "done", "done", "done"]);
  });

  it("speech mode still requires speaking event and ignores snapshot show_summary_text for line 2", async () => {
    useTimeoutFakes();
    renderStep({
      config: makeConfig({
        agent_speech_enabled: true,
        agent_summary_text_enabled: true,
      }),
    });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    emit(completionEvent("claude"));
    expect(lineStates()).toEqual(["done", "active", "pending", "pending"]);

    // Snapshot with show_summary_text does NOT advance speech mode's line 2
    emit(
      queueSnapshot(
        1,
        {
          completion_id: "claude-completion",
          generation: 1,
          agent: "claude",
        },
        "active_speaking",
        true,
      ),
    );
    expect(lineStates()).toEqual(["done", "active", "pending", "pending"]);

    // speech.state speaking advances line 2
    emit({ type: "speech.state", state: "speaking" });
    expect(lineStates()).toEqual(["done", "done", "active", "pending"]);
  });

  it("displays error status and blocks checklist if arming delivery preview fails", async () => {
    vi.mocked(api_agents.armSetupDeliveryPreview).mockResolvedValueOnce({
      ok: false,
      detail: "Preview arm failed",
    });
    useTimeoutFakes();
    renderStep({
      config: makeConfig({
        agent_speech_enabled: false,
        agent_summary_text_enabled: true,
      }),
    });
    await waitFor(() =>
      expect(stepStatusEl().textContent).toBe(PREVIEW_ARM_FAILED),
    );
    expect(lineStates()).toEqual(["pending", "pending", "pending", "pending"]);
  });

  it("does not enter the waiting variant for absent models when speech is disabled", async () => {
    const absent = absentStatus();
    renderStep({
      config: makeConfig({
        agent_speech_enabled: false,
        agent_summary_text_enabled: true,
      }),
      speechStatus: absent,
    });
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    expect(actOne()).not.toBeNull();
    expect(
      screen.queryByText(DOWNLOAD_CARD_SUBTITLE),
    ).toBeNull();
  });
});

describe("SetupStepTest — preview arming lifecycle", () => {
  it("starts pending, displays calm status, and blocks checklist progress until armed", async () => {
    let resolveArm!: (value: { ok: boolean }) => void;
    vi.mocked(api_agents.armSetupDeliveryPreview).mockReturnValue(
      new Promise<{ ok: boolean }>((resolve) => {
        resolveArm = resolve;
      }),
    );
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    // Status line shows calm pending status
    expect(stepStatusEl()).toHaveTextContent(PREVIEW_ARM_PENDING);
    expect(stepStatusEl().getAttribute("role")).toBe("status");

    // All checklist lines are pending ("Not yet")
    expect(lineStates()).toEqual(["pending", "pending", "pending", "pending"]);

    // Events emitted while pending MUST NOT advance progress
    emit(completionEvent("claude"));
    emit({ type: "speech.state", state: "speaking" });
    emit({
      type: "agent.compose.final",
      version: 1,
      session_handle: "s",
      generation: 1,
      recording_id: 1,
      text: "hello",
    });
    expect(lineStates()).toEqual(["pending", "pending", "pending", "pending"]);

    // Resolving arming enables progress
    await act(async () => {
      resolveArm({ ok: true });
    });

    await waitFor(() => expect(stepStatusEl().textContent).toBe(""));
    expect(lineStates()).toEqual(["active", "pending", "pending", "pending"]);

    // Now events advance the checklist normally
    emit(completionEvent("claude"));
    expect(lineStates()).toEqual(["done", "active", "pending", "pending"]);
  });

  it("drops fast completions that arrive while arming is pending and does not retroactively advance", async () => {
    let resolveArm!: (value: { ok: boolean }) => void;
    vi.mocked(api_agents.armSetupDeliveryPreview).mockReturnValue(
      new Promise<{ ok: boolean }>((resolve) => {
        resolveArm = resolve;
      }),
    );
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    // Very fast completion arrives before preview arming settles
    emit(completionEvent("claude"));
    expect(lineStates()).toEqual(["pending", "pending", "pending", "pending"]);

    // Preview arming settles ready
    await act(async () => {
      resolveArm({ ok: true });
    });

    await waitFor(() => expect(stepStatusEl().textContent).toBe(""));
    // The previous completion was dropped, so line 1 is active (not done)
    expect(lineStates()).toEqual(["active", "pending", "pending", "pending"]);

    // Real completion after ready advances line 1
    emit(completionEvent("claude"));
    expect(lineStates()).toEqual(["done", "active", "pending", "pending"]);
  });

  it("blocks checklist progress and displays error when arming fails with ok false or rejected promise", async () => {
    let rejectArm!: (error: Error) => void;
    vi.mocked(api_agents.armSetupDeliveryPreview).mockReturnValue(
      new Promise<{ ok: boolean }>((_, reject) => {
        rejectArm = reject;
      }),
    );
    useTimeoutFakes();
    renderStep();
    await waitFor(() => expect(api_speech.getSpeechStatus).toHaveBeenCalled());

    expect(stepStatusEl()).toHaveTextContent(PREVIEW_ARM_PENDING);

    await act(async () => {
      rejectArm(new Error("Network failed"));
    });

    await waitFor(() =>
      expect(stepStatusEl()).toHaveTextContent(PREVIEW_ARM_FAILED),
    );
    expect(stepStatusEl().getAttribute("role")).toBe("alert");
    expect(lineStates()).toEqual(["pending", "pending", "pending", "pending"]);

    // Events emitted while failed do not advance progress
    emit(completionEvent("claude"));
    emit({ type: "speech.state", state: "speaking" });
    expect(lineStates()).toEqual(["pending", "pending", "pending", "pending"]);
  });

  it("ignores stale in-flight arming responses when mode selection changes", async () => {
    let resolveReq1!: (value: { ok: boolean }) => void;
    let resolveReq2!: (value: { ok: boolean }) => void;

    let armCallCount = 0;
    vi.mocked(api_agents.armSetupDeliveryPreview).mockImplementation(() => {
      armCallCount++;
      if (armCallCount === 1) {
        return new Promise<{ ok: boolean }>((resolve) => {
          resolveReq1 = resolve;
        });
      }
      return new Promise<{ ok: boolean }>((resolve) => {
        resolveReq2 = resolve;
      });
    });

    useTimeoutFakes();
    const initialConfig = makeConfig({
      agent_speech_enabled: true,
      agent_summary_text_enabled: false,
    });
    const { rerender } = renderStep({ config: initialConfig });
    await waitFor(() => expect(api_agents.armSetupDeliveryPreview).toHaveBeenCalledTimes(1));

    expect(stepStatusEl()).toHaveTextContent(PREVIEW_ARM_PENDING);

    // Switch mode to text-only (triggers second arming request)
    const textOnlyConfig = makeConfig({
      agent_speech_enabled: false,
      agent_summary_text_enabled: true,
    });
    rerender(
      <SetupStepTest
        config={textOnlyConfig}
        onCanAdvanceChange={vi.fn()}
        onChildDialogOpenChange={vi.fn()}
        onGhostVisibleChange={vi.fn()}
        subscribe={(handler) => {
          handlers.push(handler);
          return () => {
            handlers = handlers.filter((h) => h !== handler);
          };
        }}
        speechStatus={makeStatus()}
        provider="claude"
      />,
    );
    await waitFor(() => expect(api_agents.armSetupDeliveryPreview).toHaveBeenCalledTimes(2));

    // Stale Request 1 resolves with ok: true
    await act(async () => {
      resolveReq1({ ok: true });
    });

    // Still pending because Request 1 was stale for previous selection
    expect(stepStatusEl()).toHaveTextContent(PREVIEW_ARM_PENDING);
    emit(completionEvent("claude"));
    expect(lineStates()).toEqual(["pending", "pending", "pending", "pending"]);

    // Request 2 (current selection) resolves
    await act(async () => {
      resolveReq2({ ok: true });
    });

    await waitFor(() => expect(stepStatusEl().textContent).toBe(""));
    expect(lineStates()).toEqual(["active", "pending", "pending", "pending"]);
  });

  it("out-of-order arming: stale earlier failure cannot override newer successful request", async () => {
    let resolveReq1!: (value: { ok: boolean }) => void;
    let resolveReq2!: (value: { ok: boolean }) => void;

    let armCallCount = 0;
    vi.mocked(api_agents.armSetupDeliveryPreview).mockImplementation(() => {
      armCallCount++;
      if (armCallCount === 1) {
        return new Promise<{ ok: boolean }>((resolve) => {
          resolveReq1 = resolve;
        });
      }
      return new Promise<{ ok: boolean }>((resolve) => {
        resolveReq2 = resolve;
      });
    });

    useTimeoutFakes();
    const { rerender } = renderStep({
      config: makeConfig({
        agent_speech_enabled: true,
        agent_summary_text_enabled: false,
      }),
    });
    await waitFor(() => expect(api_agents.armSetupDeliveryPreview).toHaveBeenCalledTimes(1));

    // Change selection
    rerender(
      <SetupStepTest
        config={makeConfig({
          agent_speech_enabled: false,
          agent_summary_text_enabled: true,
        })}
        onCanAdvanceChange={vi.fn()}
        onChildDialogOpenChange={vi.fn()}
        onGhostVisibleChange={vi.fn()}
        subscribe={(handler) => {
          handlers.push(handler);
          return () => {
            handlers = handlers.filter((h) => h !== handler);
          };
        }}
        speechStatus={makeStatus()}
        provider="claude"
      />,
    );
    await waitFor(() => expect(api_agents.armSetupDeliveryPreview).toHaveBeenCalledTimes(2));

    // Request 2 (newer) finishes first and succeeds
    await act(async () => {
      resolveReq2({ ok: true });
    });
    await waitFor(() => expect(stepStatusEl().textContent).toBe(""));

    // Request 1 (stale earlier) finishes later and reports failure
    await act(async () => {
      resolveReq1({ ok: false });
    });

    // Stale failure must not break the ready state
    expect(stepStatusEl().textContent).toBe("");
    expect(lineStates()).toEqual(["active", "pending", "pending", "pending"]);
  });
});



