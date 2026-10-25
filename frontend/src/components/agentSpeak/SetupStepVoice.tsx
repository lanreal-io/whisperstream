import {
  useCallback,
  useEffect,
  useImperativeHandle,
  useState,
  type Ref,
} from "react";

import { InfoGlyph } from "../InfoGlyph";
import { ModeSelector, type ModeOption } from "../ModeSelector";
import { VOICE_MODEL_INFO, VOICE_MODEL_SELECTIONS } from "./voiceModelCopy";
import { missingAutomaticEngines } from "./automaticVoiceDownload";
import { ModelDownloadConfirmDialog } from "../ModelDownloadConfirmDialog";
import { RangeInput } from "../RangeInput";
import { SliderField, SliderValue } from "../SliderField";
import { Toggle } from "../Toggle";
import { VoiceModelMemoryDialog } from "../VoiceModelMemoryDialog";
import type { CardStatus } from "./SpeakSection";
import { SetupStep } from "./SetupStep";
import { VoicePicker, type VoiceItem } from "./VoicePicker";
import { useVoicePreview } from "./useVoicePreview";
import { getSpeechVoices, type SpeechEngine, type SpeechSelection, type SpeechStatus } from "../../lib/api/speech";
import { type AppConfig } from "../../lib/api/config";
import type { AppEvent } from "../../lib/socket/events";
import type { SetupStepHandle, SetupStepProps } from "./AgentSpeakSetupModal";

/**
 * Step 2 of the Agents setup flow: pick a voice, audition it, and start
 * its download on Continue.
 *
 * Round 01 issue 3 rebuilt this step on `SetupStep`. It no longer uses
 * `SpeakSection`, it draws no card, and it carries no `StatusLight`.
 *
 * Reply modes and voice settings use one full-width row grammar: label and
 * subtitle on the left, control on the right. The selected voice's icon-only
 * transport plays its bundled sample through `useVoicePreview`; the hook
 * applies the current speed with `HTMLAudioElement.playbackRate`, so the speed
 * control demonstrates itself without a model download or live synthesis.
 *
 * Three deletions, each with a locked reason:
 *
 *   - **The card is gone.** PR #404 deleted `SpeakInsetCard` and the
 *     `--surface-inset-card` tint it filled with; this flow has exactly one
 *     ground, `--surface-modal`, and exactly one elevation, the modal card.
 *   - **The `VoicePicker` trigger takes NO extra card, but DOES carry a
 *     scoped steel-tint border** (design decision 19, REVISED 2026-08-21 by
 *     owner feedback). #404 removed the trigger's chrome entirely on the
 *     grounds that a chrome-less trigger over `--surface-modal` in Light is the
 *     dialog family's own look (`ProfileEditorModal`'s Template picker does
 *     exactly that). On this centred step it went too far: unlike Configure's
 *     Voice-model row, where the picker sits among other rows, here the dropdown
 *     is a lone instrument with no filled sibling to anchor it (the
 *     `ModeSelector` above always has one filled pill; the dropdown has
 *     nothing), so white `--border-hairline` over white `--surface-control`
 *     over white `--surface-modal` composited to no edge at all and the control
 *     stopped reading as a dropdown. The fix is a border ONLY, via the shared
 *     `Dropdown`'s opt-in `triggerBorderColor` set to `VOICE_TRIGGER_BORDER`,
 *     the same both-theme steel-tint idiom as this picker's own
 *     `DISC_RING`/`GROUP_DIVIDER`. It is SCOPED to this flow: Configure's copy
 *     of `VoicePicker` keeps the `--border-hairline` default (owner decision
 *     2026-08-13), so the two surfaces still do not drift. (This effectively
 *     restores `design-direction.md` §7's `STEEL_BORDER` row for this trigger
 *     only. The engine control above it stays on `ModeSelector`'s own canonical
 *     pill styling, which this decision does not govern and this change does
 *     not touch.)
 *   - **The `StatusLight` and the gold `attention` memory line are gone**
 *     (design decision 20). The engine is auto-selected FROM the user's
 *     available RAM: there is no error, no user decision, and no action
 *     requested. Gold is the app's caution hue (`LowRamDialog`,
 *     `LowDiskSpaceDialog`), so flagging a correct automatic decision as
 *     caution reads as the interface apologising for itself, and it was the
 *     first thing on the screen in both themes. Configure keeps its own
 *     `StatusLight` and its own caution line, and is right to: there the user
 *     chose.
 *
 * The memory fact survives as a NEUTRAL caption under the selector (copy `A8`,
 * which is `Inherited 21`'s own first clause verbatim, so the caption and its
 * tooltip agree word for word) carrying the unchanged `InfoDot`.
 *
 * Two deliberate absences, both still locked:
 *
 *   - **No master speech toggle.** A switch that turns off the thing being set
 *     up is a trap, and the off switch lives in Configure (scope decision 8).
 *   - **No preview hint row** (`Locked 9`). The lede carries it: previews are
 *     the bundled clips `useVoicePreview` plays, so every row auditions BEFORE
 *     anything is installed. `useVoicePreview` makes no backend call and must
 *     never import `lib/api`; previews are never gated on install state.
 *
 * The download starts on Continue, through the shell's `commit` hook (design
 * decision 26). It does not block the advance: step 3 is useful work to do
 * while it downloads, and step 4 re-checks and can retry. A `409` from the
 * shared download mutex surfaces on the reserved status line and STILL
 * advances.
 */

/** Copy `A2`. The step's job, not a noun label: it is the `SetupStep` `h2` and
 *  the focus target on every step change. */
const HEADING = "Choose how replies appear";

const LEDE =
  "Choose whether replies appear as written summaries, spoken audio, or both.";

/** Copy `Locked 10`. */
const DOWNLOAD_START_FAILURE =
  "Could not start the download. You can try again on the last step.";

/** Inherited copy 19 / 20 / 21, imported from Configure's own values. */
const VOICE_MODEL_LABEL = "Voice model";

const VOICE_MODEL_OPTIONS: ModeOption<SpeechSelection>[] = VOICE_MODEL_SELECTIONS;

/**
 * Copy `A8`, which supersedes `Inherited 22` ON THIS SURFACE ONLY. It is
 * `VOICE_MODEL_INFO`'s own first clause, verbatim, so the caption and the
 * tooltip it hangs off agree word for word instead of paraphrasing each other.
 * The tradeoff stays in the tooltip, where the user goes when they want to
 * override, and in the selector's own two options.
 */
const ENGINE_CAPTION = "Balance quality and memory use.";
const VOICE_CAPTION = "Choose how spoken summaries sound.";
const SPEED_CAPTION = "Adjust how quickly replies are read.";

/**
 * The collapsed voice-dropdown's resting border, THIS SURFACE ONLY. The shared
 * `Dropdown`'s default `--border-hairline` is white at 0.55, which over the
 * white `--surface-control` trigger fill on the near-opaque white
 * `--surface-modal` composites to no visible chrome in Light, so the trigger
 * read as flat text rather than a control (owner feedback, 2026-08-21). A
 * steel-tint line resolves dark-on-light in Light and light-on-dark in Dark
 * from one value, the same both-theme idiom as `VoicePicker`'s own `DISC_RING`
 * (40%) and `GROUP_DIVIDER` (22%). 32% reads as a clear control edge on the
 * 220px trigger without the weight of the disc ring. The value stays with THIS
 * caller and is passed through the opt-in `triggerBorderColor` prop: Configure's
 * copy of `VoicePicker` keeps the `--border-hairline` default (owner decision
 * 2026-08-13), so the two surfaces do not drift.
 */
const VOICE_TRIGGER_BORDER =
  "color-mix(in srgb, var(--color-steel) 32%, transparent)";

/** Inherited copy 23. */
const KOKORO_FAILURE = "Higher quality could not start. Try Lower memory instead.";

/** Inherited copy 25. */
const SAMPLE_FAILURE = "That sample could not play.";

interface PendingSpeechDownload {
  engine: SpeechEngine;
  model: SpeechStatus["engines"][SpeechEngine];
}

export interface SetupStepVoiceProps extends SetupStepProps {
  /** React 19 ref-as-prop: the shell calls `commit()` on Continue. */
  ref?: Ref<SetupStepHandle>;
  showSummaries?: boolean;
  readAloud?: boolean;
  onShowSummariesChange?: (show: boolean) => void;
  onReadAloudChange?: (read: boolean) => void;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  speechStatus: SpeechStatus | null;
  activeSpeechEngine?: SpeechEngine | null;
  speechPlaying: boolean;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  onRefreshSpeechStatus: () => Promise<SpeechStatus | null>;
  onDownload: (engines: SpeechEngine[]) => Promise<void>;
  onCancelDownload: () => Promise<void>;
  /**
   * Dev renderer seam for deterministic exact-state browser QA, identical in
   * shape and reason to `HudConfigModal`'s. The QA runner has no backend, so
   * `getSpeechVoices` always rejects and the step's HERO would only ever be
   * screenshotted in its error state, which is exactly the element this round
   * most needs an eyes-on Light/Dark verdict on. When present, the roster is
   * supplied directly. Product callers never pass it.
   */
  uiQaVoiceFixture?: { voices: VoiceItem[]; playingKey: string | null };
}

export function SetupStepVoice({
  ref,
  config,
  showSummaries: propShowSummaries,
  readAloud: propReadAloud,
  onShowSummariesChange,
  onReadAloudChange,
  onCanAdvanceChange,
  onChildDialogOpenChange,
  onConfigChange,
  speechStatus,
  activeSpeechEngine,
  speechPlaying,
  subscribe,
  onRefreshSpeechStatus,
  onDownload,
  onCancelDownload,
  uiQaVoiceFixture,
}: SetupStepVoiceProps) {
  const [internalShowSummaries, setInternalShowSummaries] = useState(
    propShowSummaries ?? config.agent_summary_text_enabled ?? true,
  );
  const [internalReadAloud, setInternalReadAloud] = useState(
    propReadAloud ?? config.agent_speech_enabled ?? true,
  );
  const showSummaries = propShowSummaries ?? internalShowSummaries;
  const readAloud = propReadAloud ?? internalReadAloud;

  const handleToggleSummaries = (next: boolean) => {
    if (showSummaries && !readAloud && !next) return;
    setInternalShowSummaries(next);
    onShowSummariesChange?.(next);
  };

  const handleToggleReadAloud = (next: boolean) => {
    if (!showSummaries && readAloud && !next) return;
    setInternalReadAloud(next);
    onReadAloudChange?.(next);
  };
  const [voiceItems, setVoiceItems] = useState<VoiceItem[]>([]);
  const [voicesLoading, setVoicesLoading] = useState(true);
  const [voicesError, setVoicesError] = useState(false);
  const [downloadStartFailed, setDownloadStartFailed] = useState(false);
  const [pendingSpeechDownload, setPendingSpeechDownload] =
    useState<PendingSpeechDownload | null>(null);
  const [memoryDialogStatus, setMemoryDialogStatus] =
    useState<SpeechStatus | null>(null);

  // Bundled pre-rendered clips played in the frontend: no backend call and no
  // engine load, which is what lets step 2 audition every voice BEFORE any
  // voice model exists on disk.
  const {
    playingKey,
    error: sampleError,
    toggle: togglePreview,
    stopAll: stopPreview,
  } = useVoicePreview(config.agent_speech_speed, config.agent_speech_volume);

  const speechSelection = config.agent_speech_engine ?? "auto";
  // What the selector shows: the saved choice, Automatic included, or the
  // engine whose download is waiting for Continue.
  const displayedSelection: SpeechSelection = pendingSpeechDownload?.engine ?? speechSelection;
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
  const visibleVoice: string | number =
    visibleEngine === "kokoro"
      ? (config.agent_speech_kokoro_voice ?? "af_heart")
      : config.agent_speech_voice;

  // Continue is ALWAYS enabled here (design decision 25): every choice on this
  // step has a working default and is re-editable in Configure.
  useEffect(() => {
    onCanAdvanceChange(true);
  }, [onCanAdvanceChange]);

  // The shell's Esc handler must stand down while either child dialog owns the
  // screen (design decision 8), and must come back the moment it does not.
  const childDialogOpen =
    pendingSpeechDownload !== null || memoryDialogStatus !== null;
  useEffect(() => {
    onChildDialogOpenChange(childDialogOpen);
    return () => onChildDialogOpenChange(false);
  }, [childDialogOpen, onChildDialogOpenChange]);

  useEffect(() => {
    let cancelled = false;
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
        if (cancelled) return;
        setVoicesError(true);
        setVoicesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [stopPreview, uiQaVoiceFixture, visibleEngine]);

  /**
   * Configure's `requestSpeechEngine`, unchanged: an installed engine is
   * selected outright, and an absent one raises `ModelDownloadConfirmDialog`
   * (`eventKind="tts"`) rather than starting a silent transfer.
   */
  const requestSpeechEngine = useCallback(
    (engine: SpeechEngine, status: SpeechStatus) => {
      setMemoryDialogStatus(null);
      if (status.engines[engine].supported === false) return;
      if (status.engines[engine].installed) {
        setPendingSpeechDownload(null);
        onConfigChange({ agent_speech_engine: engine });
        return;
      }
      setPendingSpeechDownload({ engine, model: status.engines[engine] });
    },
    [onConfigChange],
  );

  const handleVoiceModelChange = useCallback(
    async (selection: SpeechSelection) => {
      setDownloadStartFailed(false);
      if (selection === "auto") {
        // Automatic downloads both models on Continue; the backend picks an
        // installed model that fits each time it speaks.
        setMemoryDialogStatus(null);
        setPendingSpeechDownload(null);
        onConfigChange({ agent_speech_engine: "auto" });
        return;
      }
      const engine = selection;
      if (engine === "supertonic") {
        if (speechStatus) requestSpeechEngine(engine, speechStatus);
        return;
      }
      const status = (await onRefreshSpeechStatus()) ?? speechStatus;
      if (!status) return;
      // The memory dialog BLOCKS on a non-recommended tier, exactly as it does
      // in Configure: Higher quality on a machine without the headroom is the
      // one choice here that can make the feature worse.
      if (status.kokoro_memory_tier !== "recommended") {
        setMemoryDialogStatus(status);
        return;
      }
      requestSpeechEngine("kokoro", status);
    },
    [onConfigChange, onRefreshSpeechStatus, requestSpeechEngine, speechStatus],
  );

  /**
   * Continue's commit (design decision 26). The download-on-Continue behaviour
   * is unchanged: the press starts the transfer for the engine on screen.
   * Consent for it is preserved and made MORE explicit by the not-installed
   * Play path (copy-review.md flag 1, RESOLVED): pressing Play before download
   * raises `ModelDownloadConfirmDialog` with an explicit "Download" button, so
   * the reworked lede dropping the old "we'll download it when you continue"
   * clause does not remove consent. It never blocks the advance and never
   * rejects: a `409` from the shared download mutex is a real, ordinary case
   * (the Parakeet model or a just-finished archive can hold it), and it
   * surfaces on the reserved status line while the flow moves on.
   */
  const commit = useCallback(async () => {
    if (!readAloud) return;
    const status = speechStatus;
    if (!status) return;
    if (speechSelection === "auto") {
      // Automatic downloads both models, Supertonic first (owner decision,
      // 2026-10-25); the queue skips nothing already on its way.
      const engines = missingAutomaticEngines(status).filter(
        (engine) =>
          !status.engines[engine].downloading &&
          status.current_download !== engine,
      );
      if (engines.length === 0) return;
      setDownloadStartFailed(false);
      try {
        await onDownload(engines);
      } catch {
        setDownloadStartFailed(true);
      }
      return;
    }
    const engine = visibleEngine;
    const engineStatus = status.engines[engine];
    // Already here, already coming: the confirm dialog may have started it.
    if (!engineStatus || engineStatus.supported === false || engineStatus.installed || engineStatus.downloading) {
      return;
    }
    if (status.current_download === engine) return;
    setDownloadStartFailed(false);
    try {
      await onDownload([engine]);
    } catch {
      // Never log: the failure is a user-visible status line, and the payload
      // carries nothing worth a diagnostic.
      setDownloadStartFailed(true);
    }
  }, [onDownload, readAloud, speechSelection, speechStatus, visibleEngine]);

  useImperativeHandle(ref, () => ({ commit }), [commit]);

  // `slow_kokoro` means Automatic preferred the faster fallback for a prior
  // utterance. It is not a startup failure: an explicit Kokoro choice remains
  // available and is the supported direct-retry path. Reserve the hard error
  // for structural failures that actually prevented Kokoro from running.
  const explicitKokoroFailure =
    speechSelection === "kokoro" &&
    speechStatus?.reason === "unhealthy_kokoro";

  /**
   * The status-line ladder for step 2 (copy-review.md, locked hierarchy):
   * engine failure (error) > download-start failure (error) > sample-preview
   * failure (normal) > empty.
   *
   * The `normal`-tone bundled-preview fault sits below the two `error` faults
   * and above empty. It is blameless and recoverable.
   *
   * Transient download and preview states are not faults and never appear
   * here. The `attention`-toned memory rung that
   * used to sit second is deliberately absent (design decision 20): it described
   * a correct automatic selection, now the neutral caption under the selector.
   * Configure's "Connect Codex or Claude Code above" rung is likewise absent:
   * its "above" points at Configure's provider list, and here provider setup is
   * a step BEHIND, already gated by step 1's Continue.
   */
  const status: CardStatus | null = !readAloud
    ? null
    : explicitKokoroFailure
      ? { message: speechStatus?.engines.supertonic.supported === false ? "Higher quality could not start. Try again." : KOKORO_FAILURE, tone: "error" }
      : downloadStartFailed
        ? { message: DOWNLOAD_START_FAILURE, tone: "error" }
        : sampleError
          ? { message: SAMPLE_FAILURE, tone: "normal" }
          : null;

  const pendingEngineStatus = pendingSpeechDownload?.model ?? null;

  return (
    <>
      <SetupStep heading={HEADING} lede={LEDE} status={status}>
        <div className="flex flex-1 flex-col items-center">
          {/* Wide delivery-mode rows share the heading's left edge. */}
          <div className="flex w-full flex-col gap-1" data-setup-reply-modes>
            <div
              className="flex min-h-[52px] w-full items-center justify-between gap-6 py-1.5"
              data-setup-reply-mode-row="summaries"
            >
              <div className="min-w-0 flex-1 text-left">
                <div className="text-[13px] font-medium text-slate">
                  Show summaries
                </div>
                <p
                  id="show-summaries-description"
                  className="mt-0.5 text-[11px] leading-relaxed text-steel/70"
                >
                  Display a written summary in the overlay.
                </p>
              </div>
              <Toggle
                checked={showSummaries}
                disabled={showSummaries && !readAloud}
                onChange={handleToggleSummaries}
                ariaLabel="Show written summaries for coding agent replies"
                ariaDescribedBy="show-summaries-description reply-mode-constraint"
              />
            </div>

            <div
              className="flex min-h-[52px] w-full items-center justify-between gap-6 py-1.5"
              data-setup-reply-mode-row="speech"
            >
              <div className="min-w-0 flex-1 text-left">
                <div className="text-[13px] font-medium text-slate">
                  Read replies aloud
                </div>
                <p
                  id="read-aloud-description"
                  className="mt-0.5 text-[11px] leading-relaxed text-steel/70"
                >
                  Play summaries using your selected voice.
                </p>
              </div>
              <Toggle
                checked={readAloud}
                disabled={!showSummaries && readAloud}
                onChange={handleToggleReadAloud}
                ariaLabel="Read coding agent replies aloud"
                ariaDescribedBy="read-aloud-description reply-mode-constraint"
              />
            </div>

            <span id="reply-mode-constraint" className="sr-only">
              At least one reply mode must remain enabled.
            </span>
          </div>

          {readAloud && (
            <div
              className="mt-2 flex w-full flex-col gap-1"
              data-setup-voice-controls
            >
              <div
                className="flex min-h-[52px] w-full items-center justify-between gap-6 py-1.5"
                data-setup-voice-control-row="model"
              >
                <div className="min-w-0 flex-1 text-left">
                  <div className="text-[13px] font-medium text-slate">
                    {VOICE_MODEL_LABEL}
                  </div>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-steel/70">
                    {ENGINE_CAPTION}
                  </p>
                </div>
                {/* Each pill carries its own "i" and explains itself on hover
                    or focus (owner, 2026-10-25), matching the settings picker's
                    per-option "i". The glyph is a cue inside the pill button,
                    not a second control; the pill's tooltip holds the text. */}
                <div className="flex shrink-0 items-center">
                  <ModeSelector
                    value={displayedSelection}
                    options={VOICE_MODEL_OPTIONS.map((option) => {
                      const unsupported =
                        option.value !== "auto" &&
                        speechStatus?.engines[option.value].supported === false;
                      return {
                        ...option,
                        label: (
                          <span className="inline-flex items-center gap-1.5">
                            {option.label}
                            <InfoGlyph aria-hidden="true" className="opacity-70" />
                          </span>
                        ),
                        ariaLabel: typeof option.label === "string" ? option.label : undefined,
                        disabled: unsupported,
                        hint: unsupported ? "This voice model isn't available on this platform." : VOICE_MODEL_INFO[option.value],
                      };
                    })}
                    onChange={(selection) => void handleVoiceModelChange(selection)}
                    ariaLabel={VOICE_MODEL_LABEL}
                    className="shrink-0 justify-end"
                  />
                </div>
              </div>

              <div
                className="flex min-h-[52px] w-full items-center justify-between gap-6 py-1.5"
                data-setup-voice-control-row="voice"
              >
                <div className="min-w-0 flex-1 text-left">
                  <div className="text-[13px] font-medium text-slate">Voice</div>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-steel/70">
                    {VOICE_CAPTION}
                  </p>
                </div>
                <div className="flex w-[240px] shrink-0 justify-end">
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
                    playingKey={playingKey}
                    onTogglePlay={togglePreview}
                    triggerClassName="w-[198px]"
                    triggerBorderColor={VOICE_TRIGGER_BORDER}
                    panelWidth={240}
                  />
                </div>
              </div>

              <div
                className="flex min-h-[52px] w-full items-center justify-between gap-6 py-1.5"
                data-setup-voice-control-row="speed"
              >
                <div className="min-w-0 flex-1 text-left">
                  <div className="text-[13px] font-medium text-slate">Speed</div>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-steel/70">
                    {SPEED_CAPTION}
                  </p>
                </div>
                <SliderField
                  className="w-[240px] shrink-0"
                  value={<SliderValue>{`${config.agent_speech_speed.toFixed(1)}x`}</SliderValue>}
                >
                  <RangeInput
                    ariaLabel="Speech speed"
                    ariaValueText={`${config.agent_speech_speed.toFixed(1)}x`}
                    min={0.5}
                    max={2}
                    step={0.1}
                    value={config.agent_speech_speed}
                    onChange={(agent_speech_speed) =>
                      onConfigChange({ agent_speech_speed })
                    }
                    className=""
                  />
                </SliderField>
              </div>

            </div>
          )}
        </div>
      </SetupStep>

      {pendingSpeechDownload && pendingEngineStatus && (
        <ModelDownloadConfirmDialog
          targetModel={{
            name: pendingEngineStatus.label,
            downloadBytes: pendingEngineStatus.download_bytes,
            installedBytes: pendingEngineStatus.installed_bytes,
          }}
          eventKind="tts"
          targetEngine={pendingSpeechDownload.engine}
          subscribe={subscribe}
          onConfirm={() => onDownload([pendingSpeechDownload.engine])}
          onComplete={() => {
            const engine = pendingSpeechDownload.engine;
            onConfigChange({ agent_speech_engine: engine });
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
              requestSpeechEngine("kokoro", memoryDialogStatus)
            }
            onUseLowerMemory={() =>
              requestSpeechEngine("supertonic", memoryDialogStatus)
            }
            onDismiss={() => setMemoryDialogStatus(null)}
          />
        )}
    </>
  );
}
