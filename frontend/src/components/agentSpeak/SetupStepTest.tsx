import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";

import { DownloadCard } from "../onboarding/DownloadCard";
import type { CardStatus } from "./SpeakSection";
import { SetupStep } from "./SetupStep";
import { AGENT_PROVIDER_ORDER } from "./AgentProviderInstallList";
import { SetupChecklist, type SetupChecklistLineState } from "./SetupChecklist";
import { SetupExercise } from "./SetupExercise";
import { SetupKeyChip } from "./SetupKeyChip";
import { SetupStatusDot, type SetupDotState } from "./SetupStatusDot";
import { armSetupDeliveryPreview, fireSetupQuestion, teardownSetup, type CodingAgentProvider } from "../../lib/api/agents";
import { getSpeechStatus, startSpeechModelDownload, type SpeechEngine, type SpeechStatus } from "../../lib/api/speech";
import { saveConfig } from "../../lib/api/config";
import type { AppEvent } from "../../lib/socket/events";
import type { SetupStepTestProps as SetupStepTestShellProps } from "./AgentSpeakSetupModal";

/**
 * Step 4 of the Agents setup flow: the hands-on proof.
 *
 * Provider-aware proof, one act at a time. Both providers use act one. Claude
 * Code alone adds act two after act one completes; Codex stays on the completed
 * checklist because it has no structured-question hook to prove honestly.
 * The acts exercise different mechanisms and fail independently, so each owns
 * its own reserved status line:
 *
 *   - **Act one, the live checklist.** Round 02 (`design.md` decisions 29, 30
 *     and 27) replaced the old synthetic-fire exercise with a PURE OBSERVER.
 *     There is no `Try it` on this half and nothing for the app to fire: the
 *     user goes to their selected coding agent, and Whisperstream lights three
 *     or four lines as each is proved by a real event.
 *   - **Act two, Claude's keyboard loop.** A locally-fired synthetic question
 *     through the real HUD/coordinator path, expanded with the just-bound view
 *     shortcut and answered BY PRESSING THE OPTION'S DIGIT, never by voice.
 *     The question is authored WITH numbered options precisely because digit
 *     selection is scoped to the focused expanded surface. Its machinery is
 *     UNCHANGED and is on decision 34's do-not-break list.
 *
 * **The four checklist lines and their proofs** (decision 29, and
 * `mechanism-facts.md` for what the wire can actually prove):
 *
 *   1. the user sent a message  ->  selected-provider `agent.completion`
 *   2. Whisperstream read it out  ->  `speech.state` `speaking`
 *   3. the user answered by voice ->  `agent.compose.final`
 *   4. the user pressed Enter     ->  the was-in-flight-now-idle edge
 *
 * Claude retains the legacy provider-optional completion fallback. Codex needs
 * a positively labelled completion before provider-neutral speech and compose
 * events can advance its chain, so unrelated Claude activity cannot start it.
 *
 * **Completion is monotonic and back-filling.** A line never un-completes, and
 * a later proof implies the earlier ones: an `agent.compose.final` marks lines
 * 1 and 2 done too. Event loss must not strand the user on a line the system
 * already passed, which is why `progress` is a high-water mark rather than four
 * independent booleans.
 *
 * **There is no clock and no failure state on this half** (decision 27). With
 * no fire there is nothing to time out, and silence means "the user has not
 * gone and done it yet" far more often than it means "it is broken". The
 * preconditions still gate BEFORE the user is sent away, and the two retired
 * post-fire rungs come back as elapsed-time HINTS at `attention` tone.
 *
 * **The waiting variant replaces the WHOLE step, heading included** (`A9`),
 * exactly as onboarding's `StepTest` cross-fades its entire card body. The
 * shipped defect this fixes: a progress bar titled `Downloading the voice
 * model` rendered inside a section titled `Hear a reply`, two titles on one
 * waiting screen.
 *
 * **An absent voice is ALWAYS a download screen** (design decision 24, round
 * 02). The owner, on seeing the retired rung 5: *"that should never happen, we
 * should show the voice downloading window until its ready."* So the variant
 * gate is `installed`, this step POSTS the start itself when nothing is in
 * flight, and there are four states rather than one: A downloading, B started
 * by this step, C refused with 409 because the global download mutex is busy,
 * and D start-failed. C and D draw no progress bar, because a bar with nothing
 * behind it never moves; that was the real reason the gate used to be narrow,
 * and it survives as their halo presentation.
 *
 * **The reply text is never mirrored into this modal.** State only. Compose
 * owned content belongs to its exact lease and lives in the HUD; duplicating
 * it here would create a second content surface. Nothing in this file logs the question, an
 * answer, or a reply: there is no `console.*` here at all.
 *
 * **Claude's swap fires on checklist completion, after a short delay** (design
 * decisions 31 and 32, revised in agent-onboarding-improvements). The previous
 * design gated it on 1.6 seconds of continuous FOCUSED DWELL on the modal, and
 * that was the ROOT CAUSE of exercise 2 rarely appearing: the user has to leave
 * this window to perform act one, so the modal is blurred when the checklist
 * completes and the dwell almost never accrued. Focus is no longer read at all;
 * the swap fires a fixed short delay after completion so a user still glancing
 * at the finished checklist is not swapped mid-glance, but the delay NEVER
 * requires focus. The swap is one-way (the flow is forward-only and there is no
 * Back control), it changes the heading and the lede with the body, it leaves
 * the `B8` summary strip behind as the trace of act one, and **it never moves
 * focus**: focus moves only on a step change, to the step's `h2`, so a
 * timer-driven focus jump would yank a screen-reader user out of what they were
 * reading. Claude's step still reads `Step 4 of 4`; this is one step with two
 * acts, not a fifth step.
 *
 * **Sizing.** The replacement is the THIRD named in-step exception to the
 * never-reflow-within-a-step rule, alongside step 1's trust disclosure and this
 * file's own waiting-to-test crossfade (design decision 33). It is admissible at
 * exactly the moment the sizing rule's rationale says reflow is cheap: the user
 * has just arrived at a new context. It rides the shell's 220ms height tween,
 * and it may NEVER be paid for with an `overflow` property on the card root:
 * `HoverTooltip` is portal-less CSS and an `overflow` ancestor clips it.
 *
 * Continue is ALWAYS enabled on this step and it finishes (design decision 25).
 * The test is proof, not a gate: it depends on a microphone, an audio device,
 * and the user going to another application, and stranding a user behind any of
 * those is how a setup flow becomes a trap. The footer ghost link is the
 * explicit escape, and it now also appears at 60 seconds.
 */

/** Copy `B4`, which supersedes `A4` `Try it out`. The instruction changed
 *  direction: the modal no longer fires anything for the user to watch, so the
 *  heading names where the user has to go. Focus target on every step change. */
const PROVIDER_LABEL: Record<CodingAgentProvider, string> = {
  codex: "Codex",
  claude: "Claude Code",
};

function headingFor(provider: CodingAgentProvider): string {
  return `Try it with ${PROVIDER_LABEL[provider]}`;
}

/** Claude retains the two-act sentence. Codex has one real Stop-continuation
 * proof and must not imply that a structured-question act follows. */
function ledeFor(provider: CodingAgentProvider): string {
  return provider === "codex"
    ? "Follow one real message in your own Codex session."
    : "Two quick checks. This first one follows a real message in your own Claude Code session.";
}

/** Agent-agnostic heading + lede, shown ONLY while both agents are connected and
 *  neither has answered yet (`resolvedProvider === null`). The moment a real
 *  completion resolves the responder they switch to the named `headingFor` /
 *  `ledeFor`. The lede is count-neutral on purpose: it must stay true whether
 *  the responder turns out to be Codex (one check) or Claude (which adds a
 *  second). When only one agent is connected the step resolves at entry, so this
 *  agnostic phase never renders. */
const UNRESOLVED_HEADING = "Try it with your coding agent";
const UNRESOLVED_LEDE = "Follow one real message from your coding agent.";

/** Copy `B6`, the heading AFTER the swap, on the same `h2` element. Deliberately
 *  NOT `Locked 19` `Answer a question`, which renders as the exercise's own
 *  13px label about 40px below: a 20px display heading immediately above an
 *  identical label is the "card title under a section title" defect. `B6` goes
 *  one rung up and states what is genuinely new about this half. */
const SWAPPED_HEADING = "Answer with a number";

/** Copy `B7`. `Last check` carries the remaining count, the same job `B5`'s count
 *  clause did before the swap. `not by speaking` is load-bearing and RESTORED
 *  here (copy-review Element 6b): the user has just spent four lines answering
 *  with their voice, and without it the natural move is to hold the dictation
 *  key again and get nothing. */
const SWAPPED_LEDE = "Last check. Press 1 or 2 to answer, not by speaking.";

/** Copy `B8`, the completed summary strip. It is the evidence the swap must not
 *  delete (design decision 32, research anti-pattern 3), and it is written to be
 *  true in BOTH checklist variants: with line 4 present the reply reached Claude
 *  because the user pressed Enter; with auto-submit on and line 4 omitted it
 *  reached Claude because auto-submit sent it. */
function summaryStripFor(provider: CodingAgentProvider): string {
  return `First check done. Your reply reached ${PROVIDER_LABEL[provider]}.`;
}

/** Copy `B9`. Deliberately count-free, so it is correct in both the four-line
 *  and the three-line (auto-submit) variants. Polite, never `alert`: a detection
 *  success is not an emergency. */
const CHECKLIST_COMPLETE: CardStatus = {
  message: "Checklist complete.",
  tone: "normal",
};

/** Copy `B10`. WCAG 4.1.3: the body has been replaced with no change of context
 *  and no focus move, so this sentence is the ONLY way a screen-reader user
 *  learns what is now on screen. */
const SWAP_ANNOUNCEMENT: CardStatus = {
  message: "Second check ready. Answer a question.",
  tone: "normal",
};

/** Copy `Locked 19`: act two's title, a verb phrase because it is a thing you
 *  do rather than a thing you configure. */
const QUESTION_TITLE = "Answer a question";

/** Copy `Locked 20`. Do not rename this trigger. */
const TRY_IT = "Try it";

/** Copy `Locked 21`. The prompt labels itself as practice in its own first
 *  sentence, which is the cheapest defense against "did that go to Claude".
 *  Exactly two options: enough to prove digit selection, and it keeps the step
 *  inside budget. */
const QUESTION_PROMPT = "This is a practice question. Which sounds better?";
const QUESTION_OPTIONS = ["A quieter voice", "A louder voice"] as const;

/** Copy `Locked 22`, a scope requirement rather than a style choice. Rendered
 *  at 12px `text-steel`, NOT 11px and NOT `text-steel/60`: this is the flow's
 *  highest trust-stakes string, and setting it at the smallest size and weakest
 *  opacity on the surface changes whether it is read, which is the same failure
 *  as rewording it away. */
function attributionFor(provider: CodingAgentProvider): string {
  return `Whisperstream asks this one. Nothing is sent to ${PROVIDER_LABEL[provider]}.`;
}

/** Copy `A9`. Replaces the whole step while the voice model is still landing.
 *  The possessive stays distinct from first-run onboarding's `Downloading the
 *  speech model` (an entirely different file, Parakeet) and does not imply that
 *  each voice downloads separately. `A10`: this variant has NO lede, because
 *  the heading and `DownloadCard`'s own default subtitle (`A11`) already carry
 *  the state and the promise. */
const WAITING_HEADING = "Downloading your voice";

/** Copy `B13`, the status line for the state this step CREATED by posting
 *  `/api/speech/download/start` itself (design decision 24, state B).
 *
 *  It is the only string in this flow allowed to promise that something starts
 *  by itself, because by the time it renders the start has already been posted.
 *  **Do not reuse it for any state where no start has been made**: in state C
 *  the mutex refused the start, and in state D the start failed outright, so
 *  the promise would be a guess. It is replaced by `B16`'s deliberately empty
 *  slot as soon as the first `kind:"tts"` progress event arrives. */
const STARTING_DOWNLOAD: CardStatus = {
  message: "Starting your voice download...",
  tone: "normal",
};

/** Copy `B14`, the mutex-busy caption (design decision 24, state C). It is a
 *  CAPTION under the halo glyph rather than a status line, because in this one
 *  state there is no progress bar for a status line to sit under: the caption is
 *  the whole explanation. It deliberately does not name what holds the lock (the
 *  step cannot know) and its second clause is what stops the user hunting for a
 *  button that does not exist. */
const QUEUED_CAPTION =
  "Your voice is queued behind another download. It starts on its own.";

/** Copy `B15`, the only `error` tone in the whole download variant (state D).
 *  It still names what happens next, because the retry below is real. */
const START_FAILED: CardStatus = {
  message: "Could not start the voice download. Whisperstream keeps trying.",
  tone: "error",
};

/** Copy `A14`. It fixes a real defect: the machine used to route `failed` back
 *  to `Not started`, so the label contradicted the rose line beside it, and a
 *  screen-reader user heard the two in sequence. */
const DID_NOT_FINISH = "Did not finish";

/** Copy `Locked 29`: act two's own post-fire failure. It names the destination
 *  that failed, which is what distinguishes it from a hook problem. */
const QUESTION_FAILURE: CardStatus = {
  message: "The question did not reach the HUD. Try it again.",
  tone: "error",
};

const PREVIEW_ARM_PENDING: CardStatus = {
  message: "Getting the test ready...",
  tone: "normal",
};

const PREVIEW_ARM_FAILED: CardStatus = {
  message: "Could not set up the test preview. Try reopening setup.",
  tone: "error",
};

type PreviewArmState = "pending" | "ready" | "failed";

/**
 * The PRECONDITIONS, locked copy 28.1 / 28.3 / 28.4 verbatim and in this
 * priority order (design decision 27). They are config and status derived, not
 * fire derived, so they survive the change to an observer. While any is live
 * the checklist renders BLOCKED (every disc pending, every title `text-steel`)
 * and act two's trigger is disabled: do not send someone to another application
 * to perform a test that cannot pass.
 *
 * There is NO not-installed rung any more. `Locked 28.5` is deleted, string and
 * branch, and its wording is deliberately not quoted anywhere in this tree: an
 * absent voice is now a download presentation, never a line on the test screen
 * (design decision 24, the owner's *"that should never happen"*). Do not
 * reintroduce it as a safety net, not even unreachable. An unreachable branch
 * carrying a user-visible string is exactly how it survived the first time; if
 * the gate is ever wrong the honest symptom is a stuck download screen, which
 * is diagnosable, not a line contradicting the screen the user is on.
 */
function notConnectedStatus(provider: CodingAgentProvider): CardStatus {
  return {
    message: `${PROVIDER_LABEL[provider]} is no longer connected. Reconnect it in Configure.`,
    tone: "error",
  };
}
const RUNG_3_SPEECH_OFF: CardStatus = {
  message: "Speech is off, so nothing will be spoken. Turn it on in Configure.",
  tone: "attention",
};
function summarySourceStatus(provider: CodingAgentProvider): CardStatus {
  return {
    message:
      provider === "codex"
        ? "This test needs Codex's own summaries. Change it in Configure."
        : "This test needs Claude's own summaries. Change it in Configure.",
    tone: "error",
  };
}

/**
 * The two elapsed-time HINTS, copy `B11` and `B12` (design decision 27).
 *
 * They are what the retired post-fire rungs 2 and 6 became. Three things
 * changed, and all three follow from there being no fire and therefore no
 * honest verdict: the trigger is elapsed time rather than a timeout, the tone
 * drops from `error` to `attention` because nothing has been declared broken,
 * and the wording states an inference rather than a conclusion. They are hints,
 * not verdicts, and neither one ever stops the user continuing.
 */
function reconnectHint(provider: CodingAgentProvider | null): CardStatus {
  return {
    message:
      // Generic (unresolved, both connected): we cannot name one command yet, so
      // name both. The user knows which line applies because both are connected.
      provider === null
        ? "Nothing yet. If you just connected, run /reload-plugins in Claude Code or /hooks in Codex."
        : provider === "codex"
          ? "Nothing yet. If you just connected, run /hooks first."
          : "Nothing yet. If you just connected, run /reload-plugins first.",
    tone: "attention",
  };
}
const HINT_NOTHING_HEARD: CardStatus = {
  message: "If you have not heard anything, check your output device and volume.",
  tone: "attention",
};

/** design decision 13: the flow's hairline is a steel tint, NOT `--divider`,
 *  which is `rgba(255,255,255,0.25)` in Light and would be invisible on a
 *  near-white modal. */
const STEEL_HAIRLINE = "color-mix(in srgb, var(--color-steel) 22%, transparent)";

/** Act two fires something, so it has an honest clock. Generous on purpose: a
 *  premature failure line on a working system is worse than a slow one. */
const QUESTION_TIMEOUT_MS = 12_000;

/**
 * Hint and escape thresholds, owner-locked 2026-08-14 at 45s / 20s / 60s, to
 * be tuned from real use rather than guessed a second time. Halving them was
 * rejected: the reload-plugins hint would then fire before a normal user has
 * finished alt-tabbing and typing, which reads as nagging.
 */
const RELOAD_HINT_MS = 45_000;
const NOTHING_HEARD_HINT_MS = 20_000;
const GHOST_ESCAPE_MS = 60_000;

/**
 * The SWAP DELAY (design decisions 31 and 32, revised in
 * agent-onboarding-improvements): how long the finished checklist is held before
 * act two replaces it.
 *
 * The predecessor gated the swap on 1.6 seconds of continuous FOCUSED DWELL,
 * accumulated in ticks and counted ONLY while `document.hasFocus()` was true on
 * the modal. That was the root cause of exercise 2 never appearing: the user is
 * in their own coding-agent window while the checklist completes, so the modal
 * is blurred and the dwell never accrued. The focused-dwell gate was DROPPED
 * (not shortened): a shorter dwell would still require focus the user is not
 * giving, so it would fix nothing.
 *
 * This is now a plain wall-clock delay after completion, purely so a user who
 * happens to be looking at the finished checklist is not swapped mid-glance. It
 * never requires focus, and the swap still never moves focus (system.md: focus
 * moves only on a step change). If the user never returns, the `B8` summary
 * strip preserves the trace of act one either way.
 */
const SWAP_DELAY_MS = 1_600;

/**
 * The download variant's own clocks (design decision 24).
 *
 * The two retry cadences differ because the two refusals differ. A 409 means
 * the global download mutex is held by something else and will be released on
 * its own, so 5s is a cheap re-ask. Any other failure is a network or server
 * fault with no expected release, so 15s keeps `Whisperstream keeps trying`
 * literally true without hammering a broken backend.
 *
 * The install poll exists because `download.complete` is not sufficient on its
 * own: the modal can remount after that event has come and gone. It is the same
 * authoritative `getSpeechStatus()` re-check, on a clock, and it runs ONLY while
 * the download variant is on screen.
 */
const QUEUED_RETRY_MS = 5_000;
const START_FAILED_RETRY_MS = 15_000;
const WAITING_STATUS_POLL_MS = 4_000;

type QuestionPhase = "idle" | "waiting" | "done";
type Variant = "waiting" | "test";

/**
 * The four absent-voice states of design decision 24, in the order the gate
 * resolves them.
 *
 *   - `downloading` (A): something is actually transferring. `DownloadCard`,
 *     empty status line (`B16`).
 *   - `starting`    (B): absent, nothing in flight, and THIS STEP has posted
 *     the start. `DownloadCard`'s indeterminate marquee covers the gap before
 *     the first progress event, which is what that phase was built for. `B13`.
 *   - `queued`      (C): the start was refused with 409 because the global
 *     download mutex is busy. **No progress bar**, because there is no progress:
 *     a bar that never moves is the exact defect the original narrow gate
 *     existed to prevent. Halo glyph plus the `B14` caption instead.
 *   - `failed`      (D): the start failed for any other reason. State C's
 *     presentation with `B15` in the reserved status line, at `error` tone.
 */
type DownloadState = "downloading" | "starting" | "queued" | "failed";

/** Why a start attempt did not put a download in flight. `none` also covers
 *  "not attempted yet" and "attempted and accepted": in both of those the step
 *  is honestly in state B. */
type StartOutcome = "none" | "queued" | "failed";

/**
 * A 409 from `/api/speech/download/start` means the shared download mutex is
 * held, which is state C and NOT a failure. `fetchJSON` throws
 * `new Error(`${status}: ${text}`)`, so the status code is the message's first
 * token; anything else (including a thrown `TypeError` from a dead socket)
 * falls through to state D.
 */
function isDownloadMutexBusy(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /(^|\D)409(\D|$)/.test(message);
}

/** Act two's state machine. There is no `muted` state any more: round 02
 *  retired the muting rule entirely (decision 32), and its gate (exercise 1
 *  reaching a terminal state) no longer exists. */
type ExerciseState = "blocked" | "ready" | "running" | "done" | "failed";

/** Copy `Locked 24` + `A14`. The active label names what the app is waiting on,
 *  which doubles as a nudge when the user has expanded the card and stalled. */
const QUESTION_LABEL: Record<QuestionPhase, string> = {
  idle: "Not started",
  waiting: "Waiting for your answer",
  done: "Answered",
};

/** The three token-routed disc states, from the exercise machine. `failed`
 *  returns to `pending`: the exercise is retryable, and the failure is carried
 *  by the label and the status line rather than by a colour. */
function discStateOf(state: ExerciseState): SetupDotState {
  if (state === "running") return "active";
  if (state === "done") return "done";
  return "pending";
}

/** The engine whose model this step is waiting on and testing with. */
function targetEngineOf(
  status: SpeechStatus | null,
  selection: string | undefined,
): SpeechEngine {
  if (selection === "kokoro" || selection === "supertonic") return selection;
  return status?.resolved_engine ?? status?.preferred_engine ?? "supertonic";
}

/**
 * The variant gate (design decision 24, replaced this round).
 *
 * The gate is now `installed`, not `downloading`. A voice that is merely absent
 * used to fall through to the test variant, where the precondition ladder told
 * the user their voice was missing and to retry, which is the line the owner
 * ruled must never appear: *"that should never happen, we should show the voice
 * downloading window until its ready."*
 *
 * The original narrow gate's reasoning was real and is preserved elsewhere:
 * rendering a progress bar with nothing behind it is a bar that never moves.
 * The answer is that this step now STARTS the download itself (state B), and
 * the one state where it provably cannot (state C, the busy mutex) draws no bar
 * at all.
 *
 * The PIN-ONCE rule is untouched and not negotiable: the variant is decided at
 * mount and may only ever transition waiting to test, never the reverse. A
 * variant that flips back and forth as progress accrues is worse than a stale
 * one.
 */
function decideVariant(
  status: SpeechStatus | null,
  selection: string | undefined,
  speechEnabled: boolean = true,
): Variant {
  if (!speechEnabled) return "test";
  if (!status) return "test";
  const engine = targetEngineOf(status, selection);
  return status.engines[engine]?.installed ? "test" : "waiting";
}

/**
 * States C and D's mark: the dialog family's icon-halo idiom (a 48px
 * thin-stroke glyph over a radial `--halo-*` gradient at `scale(1.6)`), used
 * here for the same reason it exists there, to give a screen with one
 * significant thing to say something to look at.
 *
 * FROST, not gold or rose: the hue vocabulary is semantic, and a queued
 * download is informational. Nothing is wrong, nothing needs the user to act,
 * and a caution or error hue on a screen whose whole message is "you have
 * nothing to do" would contradict its own caption.
 *
 * A clock rather than a download arrow: the download arrow is `DownloadCard`'s
 * territory and would imply the transfer this state specifically does not have.
 */
function QueuedDownloadGlyph() {
  return (
    <div className="relative mx-auto h-12 w-12" aria-hidden="true">
      <div
        className="absolute inset-0 rounded-full"
        style={{
          background:
            "radial-gradient(circle, var(--halo-frost) 0%, transparent 70%)",
          transform: "scale(1.6)",
        }}
      />
      <svg viewBox="0 0 48 48" fill="none" className="relative h-full w-full">
        <circle
          cx="24"
          cy="24"
          r="14"
          stroke="var(--color-frost-deep)"
          strokeWidth="1.75"
        />
        <path
          d="M24 16v8.5l5.5 3.5"
          stroke="var(--color-frost-deep)"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </div>
  );
}

export interface SetupStepTestProps extends SetupStepTestShellProps {
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  /** The sole connected agent when exactly one is connected (bound at step
   *  entry), or `null` when both are connected. In the `null` case the run
   *  binds to whichever agent fires the first real completion. */
  provider: CodingAgentProvider | null;
  /** Seed only, so the variant does not have to flash while the authoritative
   *  re-check is in flight. `getSpeechStatus()` is what actually pins it. */
  speechStatus?: SpeechStatus | null;
}

export function SetupStepTest({
  config,
  onCanAdvanceChange,
  onChildDialogOpenChange,
  onGhostVisibleChange,
  subscribe,
  provider: initialProvider,
  speechStatus = null,
}: SetupStepTestProps) {
  /**
   * The provider this run is bound to. It is the sole connected agent when only
   * one is connected (`initialProvider` is non-null at step entry), or `null`
   * until the FIRST real `agent.completion` from any connected agent binds it to
   * whoever actually answered (both connected). Downstream copy, status, and the
   * exercise-2 gate all read the resolved value; Task 3.2 supplies the
   * agent-agnostic Step 4 copy shown while it is still `null`.
   */
  const [resolvedProvider, setResolvedProvider] =
    useState<CodingAgentProvider | null>(initialProvider);
  /** Mirror of `resolvedProvider` for the content-free event subscription, so
   *  latching the provider never tears down and rebuilds that listener. */
  const resolvedProviderRef = useRef<CodingAgentProvider | null>(
    initialProvider,
  );

  /**
   * Until the responder is known (both connected, none has answered yet) the
   * shipped named-provider presentation is preserved by falling back to the
   * install-list winner (`AGENT_PROVIDER_ORDER[0]`, Codex), which is exactly
   * what the shell pre-committed to before. Task 3.2 replaces this fallback
   * with the agent-agnostic copy. When one agent is connected this is always the
   * resolved provider, so the fallback never shows.
   */
  const provider: CodingAgentProvider =
    resolvedProvider ?? AGENT_PROVIDER_ORDER[0];

  const speechEnabled = config.agent_speech_enabled !== false;
  const summaryEnabled = config.agent_summary_text_enabled === true;
  const [status, setStatus] = useState<SpeechStatus | null>(speechStatus);
  const [variant, setVariant] = useState<Variant | null>(
    speechStatus
      ? decideVariant(speechStatus, config.agent_speech_engine, speechEnabled)
      : !speechEnabled
        ? "test"
        : null,
  );

  /**
   * The checklist's high-water mark: the number of lines PROVED complete.
   * Monotonic and back-filling by construction (decision 29). Four independent
   * booleans would let a dropped `speech.state` strand the user on line 2 after
   * their reply had already landed.
   */
  /** State C / D bookkeeping. `attempt` exists only so a scheduled retry can
   *  re-open the one-shot start guard; its value is never read. */
  const [startOutcome, setStartOutcome] = useState<StartOutcome>("none");
  const [startAttempt, setStartAttempt] = useState(0);
  /** A `kind:"tts"` progress event for THIS engine. It is what retires `B13`:
   *  once bytes are moving, the step is in state A and the status line goes
   *  back to `B16`'s locked empty slot. */
  const [ttsProgressSeen, setTtsProgressSeen] = useState(false);

  const [progress, setProgress] = useState(0);
  const [reloadHintDue, setReloadHintDue] = useState(false);
  const [nothingHeardHintDue, setNothingHeardHintDue] = useState(false);
  const [ghostDue, setGhostDue] = useState(false);

  /** One-way (design decision 32). Once act two is on screen there is no route
   *  back to the checklist: the flow is forward-only, it has no Back control,
   *  and there is nothing to re-run because the user can simply do another
   *  Claude turn. */
  const [swapped, setSwapped] = useState(false);

  const [questionPhase, setQuestionPhase] = useState<QuestionPhase>("idle");
  const [questionFailed, setQuestionFailed] = useState(false);
  const [questionPending, setQuestionPending] = useState(false);
  const [questionAttempted, setQuestionAttempted] = useState(false);
  const [previewArmState, setPreviewArmState] =
    useState<PreviewArmState>("pending");
  const previewArmStateRef = useRef<PreviewArmState>("pending");
  const armRequestIdRef = useRef(0);

  useEffect(() => {
    setPreviewArmState("pending");
    previewArmStateRef.current = "pending";
    const requestId = ++armRequestIdRef.current;
    armSetupDeliveryPreview({
      show_summary_text: summaryEnabled,
      agent_speech_enabled: speechEnabled,
    })
      .then((res) => {
        if (armRequestIdRef.current !== requestId) return;
        const nextState: PreviewArmState =
          res && res.ok === false ? "failed" : "ready";
        previewArmStateRef.current = nextState;
        setPreviewArmState(nextState);
      })
      .catch(() => {
        if (armRequestIdRef.current !== requestId) return;
        previewArmStateRef.current = "failed";
        setPreviewArmState("failed");
      });
    return () => {
      if (armRequestIdRef.current === requestId) {
        armRequestIdRef.current++;
      }
    };
  }, [speechEnabled, summaryEnabled]);

  /** Codex compose/speech events do not carry provider identity, so its chain
   * starts only after a provider-labelled Codex completion. Claude preserves
   * the shipped back-fill tolerance for its legacy provider-optional wire. */
  const providerChainStartedRef = useRef(false);

  /**
   * Set when `agent.compose.final` proves the reply DRAFT reached the rail
   * (line 3). Line 4 then needs a fresh delivery receipt from the reply
   * owner, so it can never claim a reply the user did not actually send.
   */
  const replyDraftLandedRef = useRef(false);

  const sawQuestionRef = useRef(false);
  const questionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /**
   * The DEFENSIVE GUARD of decision 30, pinned at mount so a config write
   * cannot change the checklist's length under the user. With
   * `claude_reply_auto_submit` on, `draft_ready` never surfaces and the user
   * never presses Enter, so line 4's event cannot arrive: render three lines
   * instead of a dead fourth. In practice the flag is always false here (it is
   * settable only in the Configure modal that setup hands off to AFTERWARDS),
   * which is exactly why the line is kept for everyone else.
   */
  const [lineCount] = useState<3 | 4>(() =>
    config.claude_reply_auto_submit === true ? 3 : 4,
  );

  const selection = config.agent_speech_engine;
  const targetEngine = useMemo(
    () => targetEngineOf(status, selection),
    [selection, status],
  );

  // Continue is always enabled here, and this step owns no blocking child
  // dialog.
  useEffect(() => {
    onCanAdvanceChange(true);
  }, [onCanAdvanceChange]);
  useEffect(() => {
    onChildDialogOpenChange(false);
  }, [onChildDialogOpenChange]);

  /**
   * Pin the variant ONCE and only ever transition waiting to test (design
   * decision 24), exactly `StepTest`'s mechanism for exactly its reason: a
   * variant that flips back and forth as progress accrues is worse than a
   * stale one.
   */
  const applyStatus = useCallback(
    (next: SpeechStatus) => {
      setStatus(next);
      setVariant((previous) =>
        previous === "test"
          ? "test"
          : decideVariant(next, selection, speechEnabled),
      );
    },
    [selection, speechEnabled],
  );

  /**
   * The gate is an authoritative `getSpeechStatus()` re-check of
   * `engines[e].installed`, NOT the `download.complete` event alone: the modal
   * can remount (a reopened flow, a step revisited) after the event has come
   * and gone, and a variant pinned on a missed event would strand the user on
   * a progress bar for a model that is already on disk.
   */
  const statusOwnerRef = useRef<AbortController | null>(null);
  const statusRefreshRef = useRef<Promise<void> | null>(null);
  const refreshStatus = useCallback((): Promise<void> => {
    if (statusRefreshRef.current) return statusRefreshRef.current;
    const owner = new AbortController();
    statusOwnerRef.current = owner;
    const timeout = window.setTimeout(() => owner.abort(), 5000);
    const aborted = new Promise<never>((_, reject) => {
      owner.signal.addEventListener("abort", () => reject(new DOMException("", "AbortError")), { once: true });
    });
    const pending = Promise.race([getSpeechStatus(owner.signal), aborted])
      .then((next) => { if (!owner.signal.aborted && statusOwnerRef.current === owner) applyStatus(next); })
      .catch(() => { /* Keep the pinned variant during transport recovery. */ })
      .finally(() => {
        window.clearTimeout(timeout);
        if (statusOwnerRef.current === owner) {
          statusOwnerRef.current = null;
          statusRefreshRef.current = null;
        }
      });
    statusRefreshRef.current = pending;
    return pending;
  }, [applyStatus]);

  useEffect(() => {
    void refreshStatus();
    return () => {
      statusOwnerRef.current?.abort();
      statusOwnerRef.current = null;
      statusRefreshRef.current = null;
    };
  }, [refreshStatus]);

  /**
   * Is the voice model actually transferring? Backend truth first, then the
   * progress events, because `status` is a snapshot and a start posted a moment
   * ago will not appear in it until the next re-check.
   */
  const engineStatus = status?.engines[targetEngine];
  const downloadInFlight =
    engineStatus?.downloading === true ||
    status?.current_download === targetEngine ||
    ttsProgressSeen;

  /**
   * THE START, and its idempotence (design decision 24, state B).
   *
   * Three guards, in this order, and all three are load-bearing:
   *
   *   1. `startedRef` makes this a ONE-SHOT per mount. The effect's deps move
   *      on every `getSpeechStatus()` re-check and on every status poll, so
   *      without it a slow download would post start once per re-check and
   *      409 against itself. Only a scheduled retry re-opens it, and a retry is
   *      only ever scheduled after an attempt has already come back refused.
   *   2. `installed` means there is nothing to download.
   *   3. `downloadInFlight` means someone already started it (Continue from
   *      step 2 usually did), and a second start would be the same self-inflicted
   *      409.
   *
   * The ref is set BEFORE the await, so two renders in the same tick cannot both
   * get past it. It is a ref rather than state for exactly that reason: a state
   * write would not be visible until the next render.
   *
   * **The outcome is NOT guarded, and that is deliberate.** It used to be
   * discarded by a `cancelled` flag scoped to the effect RUN, and this effect's
   * deps move on every authoritative re-check and on every 4s status poll: a
   * poll landing between the POST and its response threw the answer away, the
   * one-shot guard then refused to ask again, no retry was ever scheduled, and
   * the step sat in state B forever showing an indeterminate marquee for a
   * download nothing had accepted. React's StrictMode double-mount reproduced
   * that every time in development, which is how the queued state's UI QA
   * fixture caught it. A mount-scoped ref would have the same defect one level
   * up, because StrictMode's simulated unmount latches it. There is nothing
   * left to guard against: React 18 makes a state write on an unmounted
   * component a silent no-op, and the only writer here is a one-shot request.
   */
  const startedRef = useRef(false);
  useEffect(() => {
    if (variant !== "waiting") return;
    if (!status) return;
    if (status.engines[targetEngine]?.installed) return;
    if (downloadInFlight) return;
    if (startedRef.current) return;
    startedRef.current = true;

    void startSpeechModelDownload(targetEngine)
      .then(() => setStartOutcome("none"))
      .catch((error: unknown) => {
        setStartOutcome(isDownloadMutexBusy(error) ? "queued" : "failed");
      });
    // `startAttempt` is a deliberate dep: it is the retry's only way back in.
  }, [downloadInFlight, startAttempt, status, targetEngine, variant]);

  /**
   * The retry. It re-opens the one-shot guard rather than posting anything
   * itself, so every start in this component goes through the single guarded
   * path above and there is never more than one request in flight.
   */
  useEffect(() => {
    if (startOutcome === "none") return;
    if (downloadInFlight) return;
    const delay =
      startOutcome === "queued" ? QUEUED_RETRY_MS : START_FAILED_RETRY_MS;
    const timer = setTimeout(() => {
      startedRef.current = false;
      setStartAttempt((attempt) => attempt + 1);
    }, delay);
    return () => clearTimeout(timer);
  }, [downloadInFlight, startAttempt, startOutcome]);

  /**
   * The install poll, and the reason it is not just the `download.complete`
   * event: in state C nothing this step started ever completes, so the only
   * signal that the voice landed is the authoritative re-check. It runs only
   * while the download variant is on screen, and the pin-once rule means a
   * later poll can never drag a pinned test variant backwards.
   */
  useEffect(() => {
    if (variant !== "waiting") return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      await refreshStatus();
      if (!cancelled) timer = setTimeout(() => void tick(), WAITING_STATUS_POLL_MS);
    };
    timer = setTimeout(() => void tick(), WAITING_STATUS_POLL_MS);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [refreshStatus, variant]);

  /**
   * Teardown on unmount is mandatory and idempotent (design decision 5).
   * Without it a synthetic Stop leaves a blocked broker request stalling for
   * up to 120 seconds and a live compose lease that swallows the user's next
   * push-to-talk, which reads as the app breaking right after setup. The modal
   * fires the same call on close.
   */
  useEffect(() => {
    return () => {
      if (questionTimerRef.current) clearTimeout(questionTimerRef.current);
      void teardownSetup().catch(() => {
        // Best effort: a failed release must never block leaving the step.
      });
    };
  }, []);

  /** Monotonic. A later proof implies every earlier one. */
  const advanceTo = useCallback((level: number) => {
    if (previewArmStateRef.current !== "ready") return;
    setProgress((current) => (current >= level ? current : level));
  }, []);

  /**
   * Line 4's detector. The reply owner publishes a correlated receipt for a
   * delivered draft (`agent.reply.snapshot` receipts). A receipt that was not
   * there when the run started is the honest proof the loop closed; the
   * receipt carries no text.
   */
  const seenReplyReceiptsRef = useRef<Set<string> | null>(null);

  // The observed chain. Every branch is content-free: a phase, a boolean, an
  // opaque id, or an engine key. The reply draft and the question's `answers`
  // map are read for their PHASE only and never for their text.
  useEffect(() => {
    return subscribe((event) => {
      switch (event.type) {
        case "agent.completion": {
          if (previewArmStateRef.current !== "ready") break;
          const completion = event.completion;
          // Legacy tests/older Claude payloads omitted provider identity. Only
          // Claude may take that fallback; Codex must have positive identity.
          const completionProvider =
            completion?.kind === "response" ? completion.agent : "claude";
          const resolved = resolvedProviderRef.current;
          if (resolved === null) {
            // Unresolved (both agents connected, none has answered yet): the
            // first real completion from any connected agent binds the run to
            // that agent, then resolves-and-advances.
            resolvedProviderRef.current = completionProvider;
            setResolvedProvider(completionProvider);
          } else if (completionProvider !== resolved) {
            break;
          }
          providerChainStartedRef.current = true;
          advanceTo(1);
          break;
        }
        case "speech.state":
          if (previewArmStateRef.current !== "ready") break;
          // Line 2. Anything other than `speaking` is idle by contract, and
          // idle proves nothing. Only Claude keeps the lenient back-fill on
          // this provider-neutral wire; Codex (and the not-yet-resolved case,
          // where we do not yet know it is Claude) begins only after a
          // labelled completion has started the chain.
          if (
            resolvedProviderRef.current !== "claude" &&
            !providerChainStartedRef.current
          )
            break;
          if (event.state === "speaking" && speechEnabled) advanceTo(2);
          break;
        case "agent.compose.final":
          if (previewArmStateRef.current !== "ready") break;
          // Line 3. The authoritative draft landed in the reply rail. Its
          // `text` is deliberately not read. The same provider-neutral wire
          // rule applies to Codex compose final and to the unresolved case.
          if (
            resolvedProviderRef.current !== "claude" &&
            !providerChainStartedRef.current
          )
            break;
          advanceTo(3);
          // The authoritative draft landed in the rail. Record it so a dropped
          // `draft_ready`/`editing` snapshot cannot prevent line 4 from ever
          // completing (see `replyDraftLandedRef`).
          replyDraftLandedRef.current = true;
          break;
        case "agent.response_queue.snapshot": {
          if (previewArmStateRef.current !== "ready") break;
          const active = event.active;
          const activeProvider = active?.agent ?? "claude";
          if (active !== null && activeProvider !== resolvedProviderRef.current)
            break;
          if (
            resolvedProviderRef.current !== "claude" &&
            !providerChainStartedRef.current
          )
            break;
          if (
            !speechEnabled &&
            event.show_summary_text === true &&
            (active !== null || (event.completions && event.completions.length > 0))
          ) {
            advanceTo(2);
          }
          break;
        }
        case "agent.reply.snapshot": {
          const keys = event.receipts.map((receipt) => `${event.epoch}:${receipt.draft_id}`);
          if (seenReplyReceiptsRef.current === null || previewArmStateRef.current !== "ready") {
            seenReplyReceiptsRef.current = new Set(keys);
            break;
          }
          const seen = seenReplyReceiptsRef.current;
          const fresh = keys.some((key) => !seen.has(key));
          keys.forEach((key) => seen.add(key));
          if (lineCount === 4 && replyDraftLandedRef.current && fresh) {
            advanceTo(4);
          }
          break;
        }
        case "agent.question.snapshot": {
          if (resolvedProviderRef.current !== "claude") break;
          const active = event.active;
          if (!active) break;
          sawQuestionRef.current = true;
          setQuestionPhase((phase) =>
            phase === "idle"
              ? phase
              : active.phase === "answered"
                ? "done"
                : "waiting",
          );
          break;
        }
        case "download.progress": {
          // Bytes are moving, so the step is in state A and `B13` retires.
          // Same `kind:"tts"` PLUS engine filter as `subscribeTts`, and for the
          // same reason: a Parakeet download must never speak for this one.
          const tagged = event as { kind?: string; engine?: string };
          if (tagged.kind !== "tts") break;
          if (tagged.engine && tagged.engine !== targetEngine) break;
          setTtsProgressSeen(true);
          break;
        }
        case "download.complete":
          // Only the voice model's own completion re-checks. A Parakeet
          // completion must never move this step.
          if (event.kind === "tts") refreshStatus();
          break;
        default:
          break;
      }
    });
    // `resolvedProvider` is intentionally NOT a dep: the handler reads its live
    // value through `resolvedProviderRef`, so latching the provider must not
    // resubscribe (which would drop events mid-chain). `setResolvedProvider` is
    // a stable setter.
  }, [advanceTo, lineCount, refreshStatus, subscribe, targetEngine]);

  /**
   * The progress subscription MUST filter on `kind: "tts"` plus the engine, or
   * the Parakeet download drives the bar (design decision 24).
   */
  const subscribeTts = useCallback(
    (handler: (event: AppEvent) => void) =>
      subscribe((event) => {
        const tagged = event as { kind?: string; engine?: string };
        if (tagged.kind !== "tts") return;
        if (tagged.engine && tagged.engine !== targetEngine) return;
        handler(event);
      }),
    [subscribe, targetEngine],
  );

  // The preconditions, priority-ordered. Each blocks the checklist and disables
  // act two's trigger, because both acts run through the same speech and hook
  // path.
  // There is no not-installed rung: an absent voice cannot reach this variant
  // at all now, because `decideVariant` sends it to the download screen.
  const providerConnected =
    provider === "codex"
      ? config.codex_hud_enabled === true
      : config.claude_hud_enabled;
  const deliveryDisabled = !speechEnabled && !summaryEnabled;
  const blocked: CardStatus | null = !providerConnected
    ? notConnectedStatus(provider)
    : deliveryDisabled
      ? RUNG_3_SPEECH_OFF
      : speechEnabled &&
          config.agent_speech_summary_source !== undefined &&
          config.agent_speech_summary_source !== "agent"
        ? summarySourceStatus(provider)
        : previewArmState === "failed"
          ? PREVIEW_ARM_FAILED
          : previewArmState === "pending"
            ? PREVIEW_ARM_PENDING
            : null;

  /**
   * The download variant's own state, resolved in priority order (design
   * decision 24). Transfer first, because a download that is actually moving
   * outranks whatever a stale start attempt reported.
   */
  const downloadState: DownloadState = downloadInFlight
    ? "downloading"
    : startOutcome === "queued"
      ? "queued"
      : startOutcome === "failed"
        ? "failed"
        : "starting";

  /** `B16` is a locked ABSENCE for state A: `A9` states the state and `A11`'s
   *  subtitle makes the promise, so a third line would say it a third time. */
  const downloadStatus: CardStatus | null =
    downloadState === "starting"
      ? STARTING_DOWNLOAD
      : downloadState === "failed"
        ? START_FAILED
        : null;

  const checklistComplete = progress >= lineCount;

  /**
   * The hint clocks. `B11` runs from the moment the step opens (the user has
   * had 45 seconds to alt-tab and type); `B12` runs from the moment line 1
   * completes, because "20s with line 1 done and line 2 not" is only meaningful
   * relative to line 1. Both are cleared on unmount, and `B12`'s is also torn
   * down the moment line 2 lands, so a late timer cannot fire behind a line
   * that already passed.
   */
  useEffect(() => {
    const reload = setTimeout(() => setReloadHintDue(true), RELOAD_HINT_MS);
    const ghost = setTimeout(() => setGhostDue(true), GHOST_ESCAPE_MS);
    return () => {
      clearTimeout(reload);
      clearTimeout(ghost);
    };
  }, []);

  useEffect(() => {
    if (progress !== 1 || !speechEnabled) return;
    const timer = setTimeout(
      () => setNothingHeardHintDue(true),
      NOTHING_HEARD_HINT_MS,
    );
    return () => clearTimeout(timer);
  }, [progress, speechEnabled]);

  /**
   * ONE reserved status slot for the whole half (decision 27): preconditions
   * first, then hints. A second line would let a hint arriving at 45 seconds
   * reflow the step under a user who is mid-read, which is the reflow the
   * sizing rule forbids within a step.
   */
  const checklistStatus: CardStatus | null =
    blocked ??
    (reloadHintDue && progress < 1
      ? reconnectHint(resolvedProvider)
      : nothingHeardHintDue && progress === 1 && speechEnabled
        ? HINT_NOTHING_HEARD
        : checklistComplete
          ? CHECKLIST_COMPLETE
          : null);

  /**
   * The swap to act two (design decisions 31 and 32, revised in
   * agent-onboarding-improvements).
   *
   * Gated on `resolvedProvider === "claude"`, so it never fires while the run is
   * still unresolved (both connected, none answered) and never for Codex, which
   * has no structured-question act. It fires a fixed short delay after the
   * checklist completes, WITHOUT reading focus: the old design required 1.6s of
   * continuous `document.hasFocus()` on the modal, which the user in their own
   * coding-agent window almost never gives, so exercise 2 rarely appeared. The
   * delay only spares a user still glancing at the finished checklist from being
   * swapped mid-glance; it never blocks the swap, and the swap never moves focus
   * (that happens only on a step change).
   *
   * If a live precondition is present the checklist renders in its `blocked`
   * presentation with every disc pending, so swapping it away for a "first check
   * done" summary would contradict the line beside it; the gate waits for
   * `blocked === null`.
   */
  useEffect(() => {
    if (resolvedProvider !== "claude") return;
    if (swapped) return;
    if (!checklistComplete) return;
    if (blocked !== null) return;
    const timer = setTimeout(() => setSwapped(true), SWAP_DELAY_MS);
    return () => clearTimeout(timer);
  }, [blocked, checklistComplete, resolvedProvider, swapped]);

  /**
   * Persist the resolved agent's `*_verified` flag exactly once, the first time
   * its onboarding exercise (the checklist) completes. It feeds Task 1.3's
   * suppression of the stale per-agent "run /reload-plugins" / "run /hooks" hint
   * in Step 1 and Configure. This is the ONBOARDING-side write of the same bool
   * the backend also flips on a real completion (Task 1.2); it goes through the
   * existing `PUT /api/config` (no new endpoint). That endpoint reconstructs a
   * full replacement config, so the whole `config` is spread and the one flag
   * overridden, exactly as the shell's finish write does. It is guarded to fire
   * once and skips the write when the flag is already set, to avoid a redundant
   * disk write on every render.
   */
  const verifiedWrittenRef = useRef(false);
  useEffect(() => {
    if (resolvedProvider === null) return;
    if (blocked !== null) return;
    if (!checklistComplete) return;
    if (verifiedWrittenRef.current) return;
    const alreadyVerified =
      resolvedProvider === "codex"
        ? config.codex_verified === true
        : config.claude_verified === true;
    verifiedWrittenRef.current = true;
    if (alreadyVerified) return;
    const patch =
      resolvedProvider === "codex"
        ? { codex_verified: true }
        : { claude_verified: true };
    void saveConfig(patch).catch(() => {
      // Best effort: the hint retirement is a convenience and the backend also
      // flips this flag on the real completion, so a failed write is not fatal.
      // Re-open the guard so a later render can retry.
      verifiedWrittenRef.current = false;
    });
  }, [blocked, checklistComplete, config, resolvedProvider]);

  /**
   * Blocked renders every line pending and every title `text-steel` (decision
   * 27's `blocked` presentation): nothing has been proved, and a line lit while
   * a precondition is live would contradict the status line beside it.
   */
  const lineStates = useMemo<readonly SetupChecklistLineState[]>(
    () =>
      Array.from({ length: lineCount }, (_, index): SetupChecklistLineState => {
        if (blocked !== null) return "pending";
        if (index < progress) return "done";
        return index === progress ? "active" : "pending";
      }),
    [blocked, lineCount, progress],
  );

  const fireQuestion = useCallback(async () => {
    if (provider !== "claude") return;
    if (questionTimerRef.current) clearTimeout(questionTimerRef.current);
    sawQuestionRef.current = false;
    setQuestionFailed(false);
    setQuestionAttempted(true);
    setQuestionPending(true);
    setQuestionPhase("waiting");
    let ok = false;
    try {
      ok = (await fireSetupQuestion()).ok;
    } catch {
      ok = false;
    }
    setQuestionPending(false);
    if (!ok) {
      setQuestionPhase("idle");
      setQuestionFailed(true);
      return;
    }
    questionTimerRef.current = setTimeout(() => {
      questionTimerRef.current = null;
      if (!sawQuestionRef.current) {
        setQuestionPhase("idle");
        setQuestionFailed(true);
      }
    }, QUESTION_TIMEOUT_MS);
  }, [provider]);

  // Act two's state machine. A failure outranks the preconditions for the STATE
  // (the user just pressed the button and something specific happened), while
  // the status LINE keeps the shipped priority: a live precondition explains
  // the failure better than the failure does.
  const questionState: ExerciseState = questionFailed
    ? "failed"
    : questionPhase === "done"
      ? "done"
      : questionPhase !== "idle" || questionPending
        ? "running"
        : blocked !== null
          ? "blocked"
          : "ready";

  const questionLabel =
    questionState === "failed" ? DID_NOT_FINISH : QUESTION_LABEL[questionPhase];

  const questionStatus: CardStatus | null =
    blocked ?? (questionFailed ? QUESTION_FAILURE : null);

  /**
   * The ghost escape (design decision 9). It shows while the user is waiting on
   * a download, once act two has been ATTEMPTED and not completed, and now also
   * **once the step has been open 60 seconds with the checklist incomplete**.
   * A user 40 seconds into a step whose microphone is broken should not have to
   * fail a timeout to find the exit, and this half no longer has a timeout to
   * fail.
   */
  const ghostVisible =
    variant === "waiting" ||
    (ghostDue && !checklistComplete) ||
    (questionAttempted && questionState !== "done");
  useEffect(() => {
    onGhostVisibleChange(ghostVisible);
    return () => onGhostVisibleChange(false);
  }, [ghostVisible, onGhostVisibleChange]);

  if (variant === null) {
    // The authoritative re-check has not answered yet. Rendering the test
    // variant here and swapping to waiting a tick later would be exactly the
    // flip design decision 24 forbids.
    return null;
  }

  return (
    <AnimatePresence mode="wait">
      {variant === "waiting" ? (
        <motion.div
          key="waiting"
          className="flex flex-1 flex-col"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}
        >
          {/* The variant replaces the WHOLE step, heading included (`A9`), with
              no lede (`A10`). This is the owner's "showing hear a reply and
              downloading voice model is quite odd" fix: there is no second
              title on this screen to disagree with the first. */}
          <SetupStep heading={WAITING_HEADING} status={downloadStatus}>
            <div className="flex flex-1 items-center justify-center">
              {downloadState === "downloading" ||
              downloadState === "starting" ? (
                /* States A and B. `A11`: an empty title, so `DownloadCard`'s
                   own default subtitle passes through and this moment is
                   byte-identical in wording to first-run onboarding's
                   download. In state B its phase-1 indeterminate marquee is
                   covering the gap before the first progress event, which is
                   exactly what that phase was built for. */
                <DownloadCard
                  compact
                  hideFooter
                  title=""
                  subscribe={subscribeTts}
                />
              ) : (
                /* States C and D: NO progress bar. Nothing is transferring, so
                   a bar here would never move, which is the defect the narrow
                   gate used to prevent by refusing to render this screen at
                   all. The halo glyph carries the moment instead. */
                <div
                  data-setup-download-stalled={downloadState}
                  className="flex flex-col items-center"
                >
                  <QueuedDownloadGlyph />
                  {downloadState === "queued" ? (
                    <p
                      role="status"
                      className="mt-4 max-w-[280px] text-center text-[12px] leading-relaxed text-steel"
                    >
                      {QUEUED_CAPTION}
                    </p>
                  ) : null}
                </div>
              )}
            </div>
          </SetupStep>
        </motion.div>
      ) : (
        <motion.div
          key="test"
          className="flex flex-1 flex-col"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}
        >
          {/* One `SetupStep`, one `h2`, one reserved status line, across BOTH
              acts. The heading and lede change text on the swap; the ELEMENTS
              persist, so the section's accessible name updates with no focus
              event (design decision 32) and the step-level live region is never
              unmounted, which is what lets it carry `B9` and then `B10`. */}
          <SetupStep
            heading={
              swapped
                ? SWAPPED_HEADING
                : resolvedProvider
                  ? headingFor(resolvedProvider)
                  : UNRESOLVED_HEADING
            }
            lede={
              swapped
                ? SWAPPED_LEDE
                : resolvedProvider
                  ? ledeFor(resolvedProvider)
                  : UNRESOLVED_LEDE
            }
            status={swapped ? SWAP_ANNOUNCEMENT : checklistStatus}
          >
            {/* The body crossfade, on this file's own waiting-to-test precedent
                (0.18s opacity, `mode="wait"`): one motion vocabulary, not two.
                The height change rides the shell's 220ms tween. */}
            <AnimatePresence mode="wait">
              {swapped ? (
                <motion.div
                  key="question"
                  data-setup-act="2"
                  className="flex flex-1 flex-col gap-4"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.18 }}
                >
                  {/* `B8`, the trace act one leaves behind. Without it the swap
                      deletes the evidence of a check the user may never have
                      watched pass, which is research anti-pattern 3. */}
                  <div
                    data-setup-summary-strip
                    className="flex items-center gap-3"
                  >
                    <SetupStatusDot ordinal={1} state="done" />
                    <p className="text-[12px] leading-relaxed text-steel">
                      {summaryStripFor(provider)}
                    </p>
                  </div>

                  {/* Design decision 13: a steel tint, never `--divider`, which
                      is white 0.25 in Light and invisible on this near-white
                      modal. */}
                  <div
                    aria-hidden="true"
                    className="h-px"
                    style={{ background: STEEL_HAIRLINE }}
                  />

                  <SetupExercise
                    ordinal={2}
                    title={QUESTION_TITLE}
                    discState={discStateOf(questionState)}
                    stateLabel={questionLabel}
                    busy={questionState === "running"}
                    triggerLabel={TRY_IT}
                    triggerId="setup-trigger-question"
                    triggerDisabled={blocked !== null || questionPending}
                    onTrigger={() => void fireQuestion()}
                    status={questionStatus}
                    statusId="setup-status-question"
                  >
                    <p className="text-[13px] text-slate">{QUESTION_PROMPT}</p>
                    {/* Two option rows, each a digit chip plus its label. They
                        announce naturally as "1, A quieter voice", so no aria
                        override is added or wanted. */}
                    <div className="mt-1.5 flex flex-col gap-1">
                      {QUESTION_OPTIONS.map((option, index) => (
                        <div key={option} className="flex items-center gap-2">
                          <SetupKeyChip keys={String(index + 1)} />
                          <span className="text-[13px] text-steel">
                            {option}
                          </span>
                        </div>
                      ))}
                    </div>
                    <p className="mt-2 text-[12px] leading-relaxed text-steel">
                      {attributionFor(provider)}
                    </p>
                    {/* Copy `Locked 23`. Naming the HUD is load-bearing: the
                        keypress does nothing until the question is expanded in
                        that separate always-on-top window, and a user pressing
                        keys at this modal would conclude the feature is
                        broken. */}
                    <p className="mt-1 text-[12px] leading-relaxed text-steel">
                      Press{" "}
                      <SetupKeyChip keys={config.claude_view_shortcut ?? ""} />{" "}
                      in the HUD to open the question, then press{" "}
                      <SetupKeyChip keys="1" /> or <SetupKeyChip keys="2" /> to
                      answer.
                    </p>
                  </SetupExercise>
                </motion.div>
              ) : (
                <motion.div
                  key="checklist"
                  data-setup-act="1"
                  className="flex flex-1 flex-col"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.18 }}
                >
                  <SetupChecklist
                    states={lineStates}
                    pushToTalkShortcut={config.push_to_talk_shortcut}
                    provider={resolvedProvider}
                    speechEnabled={speechEnabled}
                  />
                </motion.div>
              )}
            </AnimatePresence>
          </SetupStep>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
