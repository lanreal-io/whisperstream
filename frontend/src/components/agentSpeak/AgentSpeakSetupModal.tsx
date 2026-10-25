import {
  agentRows, IDLE_ACTIVITY, type AgentProviderActivityMap, type AgentProviderLastConnectMap,
} from "./agentCompatibility";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, MotionConfig, motion } from "framer-motion";

import { StepIndicator } from "../onboarding/StepIndicator";
import { ContinueButton, GhostLink } from "../onboarding/buttons";
import { SetupStepPlugin } from "./SetupStepPlugin";
import { HoverTooltip } from "../HoverTooltip";
import {
  SetupStepVoice,
  type SetupStepVoiceProps,
} from "./SetupStepVoice";
import { SetupStepHotkey } from "./SetupStepHotkey";
import { SetupStepTest } from "./SetupStepTest";
import { saveConfig, type AppConfig } from "../../lib/api/config";
import { teardownSetup, type CodingAgentProvider } from "../../lib/api/agents";
import { type SpeechEngine, type SpeechStatus } from "../../lib/api/speech";
import type { AppEvent } from "../../lib/socket/events";
import { errorType } from "../../lib/logCapture";
import {
  setupPluginBlockReason,
  type AgentProviderResultMap,
} from "./AgentProviderInstallList";

/**
 * The Agents setup flow's shell: portal, backdrop, card, close X, Esc,
 * the three fixed bands, the forward-only step machine, the finish write, and
 * the mandatory teardown. It owns no step content.
 *
 * The concept (round 01 `design.md`): the setup flow is the first-run overlay's
 * SECOND ACT, not the Configure modal before it has been configured. It keeps
 * Configure's container (same 600px portal card, same `--backdrop-strong`, same
 * close X at `right-4 top-4`, same `p-7`, same 15px semibold title) and drops
 * Configure's interior grammar: every step is a `SetupStep`, not a
 * `SpeakSection`. `HudConfigModal`'s `cardStyle` / `backdropStyle` are
 * **copied** here on purpose and must not be imported: the dialog family has no
 * shared base by design.
 *
 * Two invariants the whole flow rests on:
 *
 *   1. **The card root may never take `overflow-hidden` or `overflow-y-auto`.**
 *      `HoverTooltip` is portal-less CSS, so an overflow ancestor clips it.
 *      The step slide is 16px precisely so no clipping container is needed:
 *      16 is smaller than the card's 28px (`p-7`) padding, so the sliding body
 *      never reaches the card border. The geometry does the clipping's job.
 *      At the app's 957x680 window the card's `100vh-40px` cap leaves about
 *      460px of body once chrome is paid for. Do not scroll the whole step.
 *      The connection rows have their own bounded list, which contains no
 *      tooltips; the frame and footer remain outside that scroll region.
 *   2. **Reflow scope, not a reflow ban.** ACROSS steps the body reflows
 *      freely, because the user just pressed Continue and expects the whole
 *      surface to change; the height tween below is what makes that read as the
 *      card breathing. WITHIN a step nothing reflows, with exactly THREE named
 *      exceptions: step 1's connection/trust disclosures expanding, step 4's one-way
 *      waiting-to-test crossfade, and step 4's one-way replacement of the live
 *      checklist by exercise 2 after its focused dwell. Each is either
 *      user-initiated or an arrival the user has been waiting for, and all
 *      three are animated. That scoping is what the reserved `CardStatusLine`s,
 *      the always-rendered footer slot, step 4's single reserved hint slot, and
 *      its never-collapsing checklist sub-text are protecting.
 */

// Copied from `HudConfigModal`, not imported (dialog family: copy, do not DRY).
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

/**
 * The step body's FLOOR. Not a reserved height: the body has no maximum and
 * grows past this whenever a step needs the room.
 *
 * The predecessor was a single reserved height measured off the TALLEST step
 * (362px, step 4's two-section test variant). Against a 162px step 1 that
 * pooled 200px of dead space under the content, which is the "there's a lot of
 * dead space, I wonder if it should be dynamic" complaint this round answers.
 * The reason it existed at all was an imported invariant, "a settings modal
 * must never reflow", which is true of Configure and false here: reflow at a
 * step boundary is free, because the user just asked for the surface to change.
 *
 * 240 is near the MEDIAN step, not the tallest. Steps land at or above it, and
 * the slack a short step leaves is distributed AROUND the instrument by
 * `SetupStep`'s `flex-1` region rather than pooling beneath it.
 */
export const STEP_BODY_FLOOR_H = 240;

export type StepId = 1 | 2 | 3 | 4;

const TOTAL_STEPS = 4;

/** Locked copy 31. The label is the same in the same place for three steps; only
 *  the terminal action differs, and it is `Done` rather than `Finish` so it does
 *  not collide with the ghost link's `Finish without testing` (locked copy 27). */
const CONTINUE_LABELS: Record<StepId, string> = {
  1: "Continue",
  2: "Continue",
  3: "Continue",
  4: "Done",
};

/** Locked copy 1. Constant identity across all four steps, never the step name:
 *  the bands hold still so the object keeps its identity while its contents
 *  move, and the step's own `SpeakSection` title names the subject. */
const MODAL_TITLE = "Set up Agents";

/** Locked copy 34. */
const FINISH_FAILURE = "Could not save your setup. Try Done again.";

/**
 * The contract every step implements, so Tasks 2.4, 2.5 and 3.1 slot into this
 * shell without reshaping it. Each step ALSO takes its own domain props (step 1
 * the plugin status, step 2 the speech status and download plumbing, step 3 the
 * shortcut-capture transport); those are threaded from the shell's own props
 * when each step lands. These three are the shell-facing half.
 */
export interface SetupStepProps {
  config: AppConfig;
  /**
   * Report this step's Continue gate. Steps must call it on mount and on every
   * change. Step 1 reports `false` until the plugin is connected, step 3
   * `false` while a shortcut conflict is live, steps 2 and 4 always `true`
   * (design decision 10).
   */
  onCanAdvanceChange: (canAdvance: boolean) => void;
  /**
   * Steps that own a blocking child dialog report it so the shell's Esc handler
   * stands down (design decision 16). Only step 2 uses this, for
   * `ModelDownloadConfirmDialog` and `VoiceModelMemoryDialog`.
   * `ShortcutCapture` needs no equivalent: it already wins Esc through a
   * capture-phase listener with `stopPropagation`.
   */
  onChildDialogOpenChange: (open: boolean) => void;
}

/**
 * Step 4's extra shell-facing prop: the footer ghost escape renders only on
 * step 4, and only while the voice-model download is running or a half has
 * failed (design decision 14).
 */
export interface SetupStepTestProps extends SetupStepProps {
  onGhostVisibleChange: (visible: boolean) => void;
}

/**
 * Work a step runs when Continue is pressed, before the shell advances. Step 2
 * uses it to start the voice-model download through Configure's
 * `requestSpeechEngine` path; it resolves immediately and does not block the
 * advance (design decision 11). The shell holds one ref and calls `commit` if
 * the mounted step exposes it.
 */
export interface SetupStepHandle {
  commit?: () => void | Promise<void>;
}

export interface AgentSpeakSetupModalProps {
  open: boolean;
  /** Closing an unfinished flow leaves `agent_speak_setup_completed` false.
   *  The Agents page returns to its introduction, preserving provider choices. */
  onClose: () => void;
  /** Called only after the finish write succeeded, with the saved config so the
   *  host can apply the flag to its own state without a refetch. */
  onFinish: (saved: AppConfig) => void;
  config: AppConfig;
  /**
   * Step 1's plugin-lifecycle transport, threaded straight through to
   * `SetupStepPlugin`. It is the SAME set Configure already takes, so both
   * surfaces drive one lifecycle and one source of status copy rather than the
   * setup flow growing a second connect path.
   */
  providerActivity?: AgentProviderActivityMap;
  providerLastConnect?: AgentProviderLastConnectMap;
  providerResults: AgentProviderResultMap;
  onProviderToggle: (provider: CodingAgentProvider, checked: boolean) => void;
  onResumeAgents?: () => void;
  /**
   * Steps 2 and 3's transport, and again the SAME set Configure already takes,
   * so the setup flow drives one speech lifecycle and one capture protocol
   * rather than growing parallel ones.
   */
  onConfigChange: (patch: Partial<AppConfig>) => void;
  connected: boolean;
  send: (msg: Record<string, unknown>) => boolean;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  speechStatus: SpeechStatus | null;
  activeSpeechEngine?: SpeechEngine | null;
  speechPlaying: boolean;
  onRefreshSpeechStatus: () => Promise<SpeechStatus | null>;
  onDownload: (engines: SpeechEngine[]) => Promise<void>;
  onCancelDownload: () => Promise<void>;
  /** Dev renderer seam for deterministic exact-state browser QA, forwarded
   *  verbatim to step 2. See `SetupStepVoice`. Product callers never pass it. */
  uiQaVoiceFixture?: SetupStepVoiceProps["uiQaVoiceFixture"];
}

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

export function AgentSpeakSetupModal({
  open,
  onClose,
  onFinish,
  config,
  providerActivity = IDLE_ACTIVITY,
  providerLastConnect = {},
  providerResults,
  onProviderToggle,
  onResumeAgents,
  onConfigChange,
  connected,
  send,
  subscribe,
  speechStatus,
  activeSpeechEngine,
  speechPlaying,
  onRefreshSpeechStatus,
  onDownload,
  onCancelDownload,
  uiQaVoiceFixture,
}: AgentSpeakSetupModalProps) {
  // Step 1 guarantees at least one agent is active before this test can be
  // reached. When exactly one is connected we resolve to it at step entry;
  // when both are connected we pass `null` and let SetupStepTest bind the run
  // to whichever agent actually fires the first real completion, instead of
  // pre-committing to the install-list winner (Codex).
  const providerRows = agentRows(config, providerResults, providerActivity, providerLastConnect);
  const codexConnected = providerRows.codex.state === "connected";
  const claudeConnected = providerRows.claude.state === "connected";
  const pluginBlockReason = setupPluginBlockReason(providerRows);
  const continueHintId = useId();
  const setupTestProvider: CodingAgentProvider | null =
    codexConnected && claudeConnected
      ? null
      : codexConnected
        ? "codex"
        : claudeConnected
          ? "claude"
          : null;
  const [step, setStep] = useState<StepId>(1);
  const [showSummaries, setShowSummaries] = useState(true);
  const [readAloud, setReadAloud] = useState(true);
  const [canAdvance, setCanAdvance] = useState(true);
  const [childDialogOpen, setChildDialogOpen] = useState(false);
  const [ghostVisible, setGhostVisible] = useState(false);
  const [finishFailed, setFinishFailed] = useState(false);
  const [finishPending, setFinishPending] = useState(false);
  const stepHandleRef = useRef<SetupStepHandle | null>(null);
  const teardownDoneRef = useRef(false);
  /** The measured height of the mounted step, or `null` before the first
   *  measurement (and in jsdom, which reports every box as zero). */
  const [stepHeight, setStepHeight] = useState<number | null>(null);
  const stepResizeRef = useRef<ResizeObserver | null>(null);

  /**
   * Forward-only (design decision 5): there is no Back button and no new Back
   * primitive, so the slide never reverses and the finish write happens exactly
   * once. The direction stays as a named constant because the transition is
   * written in the design's `x: 16 * direction` form.
   */
  const direction = 1;

  /**
   * Teardown is mandatory and idempotent (design decision 15). Without it a
   * synthetic Stop leaves a blocked broker request stalling for up to 120
   * seconds and a live compose lease that swallows the user's next
   * push-to-talk. The ref makes it fire exactly once per opened session, so the
   * explicit close path and the unmount cleanup cannot double-fire.
   */
  const runTeardown = useCallback(() => {
    if (teardownDoneRef.current) return;
    teardownDoneRef.current = true;
    void teardownSetup().catch(() => {
      // Best effort: a failed release must never block closing the modal.
    });
  }, []);

  useEffect(() => {
    if (!open) return;
    teardownDoneRef.current = false;
    setStep(1);
    setShowSummaries(true);
    setReadAloud(true);
    setCanAdvance(true);
    setChildDialogOpen(false);
    setGhostVisible(false);
    setFinishFailed(false);
    setFinishPending(false);
    return () => {
      runTeardown();
    };
  }, [open, runTeardown]);

  const handleClose = useCallback(() => {
    runTeardown();
    onClose();
  }, [onClose, runTeardown]);

  const handleFinish = useCallback(async () => {
    setFinishPending(true);
    setFinishFailed(false);
    try {
      const graceSeconds = showSummaries ? 20 : 6;
      const saved = await saveConfig({
        agent_speak_setup_completed: true,
        agent_summary_text_enabled: showSummaries,
        agent_speech_enabled: readAloud,
        claude_reply_grace_seconds: graceSeconds,
      });
      runTeardown();
      onFinish(saved);
    } catch (err) {
      // Mirrors `OnboardingOverlay.handleFinish`: the modal stays open with its
      // state intact so the user can retry (design decision 17).
      console.error(
        `[Config] agent speak setup finish failed error_type=${errorType(err)}`,
      );
      setFinishFailed(true);
    } finally {
      setFinishPending(false);
    }
  }, [onFinish, readAloud, runTeardown, showSummaries]);

  const handleAdvance = useCallback(() => {
    const handle = stepHandleRef.current;
    if (handle?.commit) void handle.commit();
    if (step === TOTAL_STEPS) {
      void handleFinish();
      return;
    }
    setFinishFailed(false);
    setGhostVisible(false);
    setStep((current) => (current + 1) as StepId);
  }, [handleFinish, step]);

  // Esc closes, EXCEPT while step 2's download-confirm or memory dialog owns
  // the screen (design decision 16, copying Configure's guard).
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || childDialogOpen) return;
      event.preventDefault();
      handleClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [childDialogOpen, handleClose, open]);

  /**
   * Focus moves to the step's `SetupStep` `h2` on every change, so the change
   * is announced instead of stranding a screen-reader user on a Continue button
   * whose page moved under them (design decision 8).
   *
   * Retargeted from `h3` this round, and it is an accessibility gain rather
   * than a mechanical rename: every heading is now the step's JOB
   * (`Connect your coding agents`, `Pick a voice`, `Set your shortcut`,
   * `Try it out`) where the `SpeakSection` titles were noun labels (`Agents`,
   * `Voice`, `Replies`). The first thing a screen-reader user hears on a step change is
   * now what the step is asking of them.
   *
   * The modal's own `Set up Agents` title is also an `h2`, and is
   * deliberately out of reach here: the query is scoped to the sliding body.
   *
   * This is a mount ref on the sliding body rather than an effect keyed on
   * `step`: `AnimatePresence mode="wait"` holds the incoming step until the
   * outgoing one has exited, so an effect would find and focus the OLD heading.
   *
   * The same callback starts measuring the incoming step, for the same reason:
   * it is the one moment we are certain the new body is in the document.
   */
  const mountStepBody = useCallback((element: HTMLDivElement | null) => {
    stepResizeRef.current?.disconnect();
    stepResizeRef.current = null;
    if (!element) return;

    const heading = element.querySelector("h2");
    if (heading) {
      heading.tabIndex = -1;
      heading.focus();
    }

    const measure = () => {
      const next = element.offsetHeight;
      // jsdom reports 0 for everything; leaving the target at `auto` there is
      // the correct no-op rather than animating the body to nothing.
      if (next > 0) setStepHeight(next);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    stepResizeRef.current = observer;
  }, []);

  useEffect(() => {
    return () => {
      stepResizeRef.current?.disconnect();
      stepResizeRef.current = null;
    };
  }, []);

  const effectiveConfig = useMemo(
    () => ({
      ...config,
      agent_summary_text_enabled: showSummaries,
      agent_speech_enabled: readAloud,
      claude_reply_grace_seconds: showSummaries ? 20 : 6,
    }),
    [config, readAloud, showSummaries],
  );

  if (!open) return null;

  const stepProps: SetupStepProps = {
    config: effectiveConfig,
    onCanAdvanceChange: setCanAdvance,
    onChildDialogOpenChange: setChildDialogOpen,
  };

  return createPortal(
    <MotionConfig reducedMotion="user">
      <div className="fixed inset-0 z-50 flex items-center justify-center">
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4 }}
          className="absolute inset-0"
          style={backdropStyle}
          onClick={handleClose}
        />

        {/* No `overflow-hidden` and no `overflow-y-auto` on this root, ever:
            `HoverTooltip` is portal-less CSS and an overflow ancestor clips it.
            The 16px step slide is what makes that affordable. */}
        <motion.div
          initial={{ scale: 0.92, opacity: 0, y: 12 }}
          animate={{ scale: 1, opacity: 1, y: 0 }}
          transition={{
            type: "spring",
            stiffness: 400,
            damping: 30,
            delay: 0.08,
          }}
          className="relative flex max-h-[calc(100vh-40px)] w-[600px] max-w-[calc(100vw-40px)] flex-col rounded-[var(--radius-card)] p-7"
          style={cardStyle}
          role="dialog"
          aria-modal="true"
          aria-labelledby="agent-speak-setup-title"
        >
          <button
            type="button"
            onClick={handleClose}
            aria-label="Close"
            className="absolute right-4 top-4 flex h-6 w-6 cursor-pointer items-center justify-center rounded-full text-steel/60 transition-colors hover:text-slate"
          >
            <XGlyph />
          </button>

          {/* Band 1: constant identity. */}
          <h2
            id="agent-speak-setup-title"
            className="pr-8 text-[15px] font-semibold text-slate"
          >
            {MODAL_TITLE}
          </h2>

          {/* Band 2: `StepIndicator` unchanged, centered. There is deliberately
              no step subtitle (locked copy 2): it would be a second heading
              level in a card the design holds to one rung, and it would repeat
              the section title one row below. */}
          <div className="mt-3 flex justify-center">
            <StepIndicator current={step} total={TOTAL_STEPS} />
          </div>

          {/* Floor plus grow. The body declares a 240px floor and NO maximum,
              and animates to the incoming step's measured height at the step
              boundary, running with the existing 16px slide and honouring the
              `MotionConfig reducedMotion="user"` above. `mode="wait"` means one
              child at a time, so the floor alone holds the box: no absolute
              positioning, and no `overflow` (invariant 1). */}
          <motion.div
            className="relative mt-4"
            style={{ minHeight: STEP_BODY_FLOOR_H }}
            animate={{ height: stepHeight ?? "auto" }}
            transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
          >
            <AnimatePresence mode="wait" custom={direction}>
              <motion.div
                key={step}
                ref={mountStepBody}
                custom={direction}
                className="flex flex-col"
                style={{ minHeight: STEP_BODY_FLOOR_H }}
                initial={{ opacity: 0, x: 16 * direction }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -16 * direction }}
                transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
              >
                {step === 1 && (
                  <SetupStepPlugin
                    {...stepProps}
                    providerActivity={providerActivity}
                    providerLastConnect={providerLastConnect}
                    providerResults={providerResults}
                    onProviderToggle={onProviderToggle}
                    onResumeAgents={onResumeAgents}
                  />
                )}
                {step === 2 && (
                  <SetupStepVoice
                    ref={stepHandleRef}
                    {...stepProps}
                    showSummaries={showSummaries}
                    readAloud={readAloud}
                    onShowSummariesChange={setShowSummaries}
                    onReadAloudChange={setReadAloud}
                    onConfigChange={onConfigChange}
                    speechStatus={speechStatus}
                    activeSpeechEngine={activeSpeechEngine}
                    speechPlaying={speechPlaying}
                    subscribe={subscribe}
                    onRefreshSpeechStatus={onRefreshSpeechStatus}
                    onDownload={onDownload}
                    onCancelDownload={onCancelDownload}
                    uiQaVoiceFixture={uiQaVoiceFixture}
                  />
                )}
                {step === 3 && (
                  <SetupStepHotkey
                    {...stepProps}
                    onConfigChange={onConfigChange}
                    connected={connected}
                    send={send}
                    subscribe={subscribe}
                  />
                )}
                {step === 4 && (
                  <SetupStepTest
                    {...stepProps}
                    onGhostVisibleChange={setGhostVisible}
                    subscribe={subscribe}
                    speechStatus={speechStatus}
                    provider={setupTestProvider}
                  />
                )}
              </motion.div>
            </AnimatePresence>
          </motion.div>

          {/* Band 3: the footer. The left slot is ALWAYS rendered and reserved,
              so the one step that carries a ghost link (and the finish failure)
              never moves the primary button. */}
          <div className="mt-5 flex min-h-[20px] items-center justify-between gap-5">
            <div data-setup-footer-slot>
              {finishFailed ? (
                <p
                  role="alert"
                  className="text-[12px] leading-relaxed"
                  style={{ color: "var(--color-rose)" }}
                >
                  {FINISH_FAILURE}
                </p>
              ) : step === TOTAL_STEPS && ghostVisible ? (
                <GhostLink onClick={() => void handleFinish()}>
                  Finish without testing
                </GhostLink>
              ) : null}
            </div>
            <HoverTooltip
              content={step === 1 ? pluginBlockReason : null}
              side="auto"
              bubbleId={continueHintId}
              focusVisibleClassName="group-focus-within:opacity-100"
            >
              <span
                tabIndex={step === 1 && pluginBlockReason ? 0 : undefined}
                aria-describedby={step === 1 && pluginBlockReason ? continueHintId : undefined}
                aria-label={step === 1 && pluginBlockReason ? "Continue unavailable" : undefined}
              >
                <ContinueButton
                  label={CONTINUE_LABELS[step]}
                  onClick={handleAdvance}
                  // Step 1 is derived directly: opening/resetting the modal must
                  // not overwrite the child's initial connection gate.
                  disabled={(step === 1 ? pluginBlockReason !== null : !canAdvance) || finishPending}
                />
              </span>
            </HoverTooltip>
          </div>
        </motion.div>
      </div>
    </MotionConfig>,
    document.body,
  );
}
