import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { DownloadCard } from "./onboarding/DownloadCard";
import { subscribeToDownloadEvents } from "../lib/socket/downloads";
import { startBenchmark, cancelLocalLLMDownload } from "../lib/api/enhancement";
import type { AppEvent } from "../lib/socket/events";
import { LOCAL_MODEL_DISPLAY_LABEL, LOCAL_MODEL_DESCRIPTION, LOCAL_MODEL_FIRST_PARTY } from "../lib/localModels";
import { ModelIdentityChip } from "../pages/enhancement/Glyphs";
import {
  InlineDownloadPhase,
  InlineMeasuringPhase,
  InlinePrefilterPhase,
  InlineVerdictPhase,
} from "./BenchmarkInlinePhases";
import {
  SpeedBar,
  ModelChip,
  WinnerHero,
  GlassPill,
  PrimaryPill,
  LifeSignStatus,
  DeviceCompletionStatus,
  completionTone,
  type BenchmarkDeviceVerdict,
  speedToneForWps,
  tokPerSecToWordsPerSec,
} from "./BenchmarkVisuals";

/* ---------- Dialog shell tokens ----------
   Lifted verbatim from EnableLocalConfirmDialog / LowRamDialog / DownloadDialog
   per the project's "no shared base dialog component" decision (design.md
   Decision 10). One frost-glass card, 420px wide, that evolves through phases.
   Backdrop uses --backdrop-strong (40px blur) — matches what this file uses. */
const dialogShellStyle = {
  background: "var(--surface-modal)",
  backdropFilter: "blur(24px)",
  WebkitBackdropFilter: "blur(24px)",
  border: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass)",
};

const backdropStyle = {
  background: "var(--backdrop-strong)",
  backdropFilter: "blur(40px)",
  WebkitBackdropFilter: "blur(40px)",
};

const glassPillStyle = {
  background: "var(--surface-modal)",
  border: "1px solid var(--border-hairline-steel)",
  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
};

const gradientPillStyle = {
  background: "var(--accent-fill)",
  boxShadow:
    "var(--shadow-pearl)",
};

// Minimum time an indeterminate card stays on screen so a sub-second backend
// transition does not flash past (design.md Decision 4).
const MIN_INDETERMINATE_MS = 400;
// A completed serial-device reading gets a deliberate beat before queued
// benchmark events advance the UI. Backend work continues without waiting.
const DEVICE_COMPLETION_DWELL_MS = 1000;

type BenchmarkStreamEvent = Extract<
  AppEvent,
  { type: "benchmark.progress" | "benchmark.result" }
>;

type Phase =
  | "declined"
  | "intro"
  | "prefilter"
  | "download"
  | "benchmark"
  | "success"
  | "marginal"
  | "failure"
  | "infrastructure_error";

/** Terminal verdict handed back to the parent (Enhancement.tsx), which issues
 *  the single combined config patch:
 *  - viable   -> provider=local, enable, cache verdicts. `appliedModelId` is the
 *                one measured model (Stream-1 Mini for the automatic first-setup
 *                check); the parent writes `enhancement_local_model` to it. It
 *                was just benched, so it is always on disk (no download gate).
 *                `verdicts` carries its REAL verdict to merge into the cache.
 *  - marginal -> the measured model runs, just slowly. The parent applies the
 *                chosen model (`appliedModelId`: the id to
 *                activate — enable local + set `enhancement_local_model`; `null` =
 *                keep the current model, do NOT enable) and merges `verdicts`
 *                (per-model tri-state) into the cache, all in ONE combined config
 *                patch (`handleBenchmarkVerdict`).
 *  - unviable -> route per the provider at flow-open (ordinary local -> Gemini
 *                chain; Lockdown -> stay local; cloud -> stay put), caching
 *                only measured `verdicts`. The parent auto-deletes the model on
 *                give-up only when THIS check downloaded it (`freshlyDownloaded`),
 *                so a model the user already had is never removed. */
export type BenchmarkVerdict =
  | {
      kind: "viable";
      appliedModelId?: string;
      verdicts?: Record<string, "viable" | "marginal" | "unviable">;
    }
  | {
      kind: "marginal";
      // Model to activate (enable local + set local_model); null = keep current,
      // do not enable.
      appliedModelId: string | null;
      // Per-model verdicts to merge into the cache.
      verdicts: Record<string, "viable" | "marginal" | "unviable">;
    }
  | {
      kind: "unviable";
      freshlyDownloaded: boolean;
      verdicts?: Record<string, "viable" | "marginal" | "unviable">;
    };

/** What the inline variant's host footer should offer. Fast and unviable
 *  verdicts fire on arrival (enhancement is already on, or stays off), so the
 *  host only needs actions for the marginal choice and the infrastructure
 *  retry. "declined" is the inline-only confirmation after Leave enhancement
 *  off. */
export type InlineBenchmarkState =
  | { phase: "running" }
  | { phase: "success" }
  | { phase: "marginal"; useAnyway: () => void; leaveOff: () => void }
  | { phase: "declined" }
  | { phase: "failure" }
  | { phase: "infrastructure_error"; retry: () => void };

interface BenchmarkFlowProps {
  /** "modal" (default) is the Enhancement settings dialog. "inline" renders
   *  the phases inside a host card (onboarding) with no backdrop or dialog
   *  shell; the host owns footer actions via `onInlineStateChange`, Gemini is
   *  never offered, and there is no retry on an unviable verdict. Inline
   *  starts at "prefilter" (the host owns its own intro). */
  variant?: "modal" | "inline";
  /** Inline only: reports the footer-relevant state on every phase change. */
  onInlineStateChange?: (state: InlineBenchmarkState) => void;
  /** "intro" for first-run enable (sets expectations before the 3 GB download);
   *  "prefilter" to skip the intro on a hardware re-check. */
  startPhase: "intro" | "prefilter";
  /** "setup" (default) for the first-run enable flow; "recheck" for the
   *  launch-time auto-rebench after the backend detected the graphics hardware
   *  changed. Recheck swaps the first-run copy ("Setting things up" /
   *  "You're all set") for context-aware strings and shows an explanatory strip,
   *  since that dialog auto-opens unprompted with no intro card to set context. */
  mode?: "setup" | "recheck";
  /** Benchmark mode. "auto" is the first-setup check: the backend measures
   *  only Stream-1 Mini and the intro reads "Set up local enhancement". "manual"
   *  benches exactly `benchModelId` (for example the Customizable opt-in). Both
   *  render one model chip. */
  benchMode?: "auto" | "manual";
  /** The catalog id this check measures (Stream-1 Mini for "auto"). */
  benchModelId?: string;
  /** Compact download size of `benchModelId` ("462 MB") for the intro copy and
   *  model card. Omitted = the size is left out. */
  benchModelSize?: string;
  /** Which failure card to show, decided by the provider at flow-open:
   *  "gemini" routes an ordinary Local failure to a free key, "current" keeps
   *  an existing cloud provider, and "local" keeps Lockdown on-device. */
  failureFallback: "gemini" | "current" | "local";
  /** Apply the measured model after a successful recheck. Used when a stale
   *  stored selection had to fall back to an available managed model. */
  applyModelOnSuccess?: boolean;
  currentCleanupEnabled?: boolean;
  /** The catalog id of the currently active local model (`config.
   *  enhancement_local_model`), snapshotted at flow-open. Used as the fallback
   *  receipt-bar model and reset target on "Test again". */
  currentModelId: string;
  /** Raw subscribe function from useAppSocket(). The download leg is filtered to
   *  `kind:"local_llm"` via subscribeToDownloadEvents; benchmark.* events are
   *  read directly. */
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  /** Abort with no config write (toggle stays visually off). Available during
   *  intro / prefilter / download only. */
  onCancel: () => void;
  /** Terminal verdict. Fires exactly once per run (StrictMode ref-guarded;
   *  re-armed by "Test again"). */
  onVerdict: (v: BenchmarkVerdict) => void;
}

export function BenchmarkFlow({
  variant = "modal",
  onInlineStateChange,
  startPhase,
  mode = "setup",
  benchMode = "manual",
  benchModelId = "gemma4-e2b-instruct",
  benchModelSize,
  failureFallback,
  applyModelOnSuccess = false,
  currentCleanupEnabled = true,
  currentModelId,
  subscribe,
  onCancel,
  onVerdict,
}: BenchmarkFlowProps) {
  const isRecheck = mode === "recheck";
  const isFirstSetup = benchMode === "auto";
  // The single model this check measures.
  const initialRungId = benchModelId;
  const [phase, setPhase] = useState<Phase>(startPhase);
  const phaseRef = useRef<Phase>(startPhase);
  const verdictFiredRef = useRef(false); // StrictMode double-mount guard
  const benchmarkStartedRef = useRef(false); // kick the REST call once
  // True only when THIS flow downloaded the GGUF (MANUAL single-model bench's
  // benchmark.result.freshly_downloaded). Drives Option B auto-delete-on-give-up.
  const freshlyDownloadedRef = useRef(false);
  // REAL verdicts from benchmark.result, merged into the parent's cache by
  // every verdict fire (viable / unviable). Stashed in a ref so the fire
  // callbacks (SuccessPhase onDone, FailurePhase onGiveUp) read the latest.
  const verdictsRef = useRef<
    Record<string, "viable" | "marginal" | "unviable"> | undefined
  >(undefined);
  // Tracks when the current indeterminate card mounted so a fast backend
  // transition is held for MIN_INDETERMINATE_MS before advancing.
  const indeterminateSinceRef = useRef<number>(performance.now());
  // Latest streamed generated words/s tick during the measuring phase
  // (`benchmark.progress {phase:"running", wps}`). undefined until the first
  // tick arrives -> the SpeedBar sweeps marquee; once set it live-fills to the
  // measured throughput, classified against the visible scale. Reset to
  // undefined when a new device starts so serial measurements never look like
  // one benchmark plummeting.
  const [liveWps, setLiveWps] = useState<number | undefined>(undefined);
  // The model being measured, driven by benchmark.progress.model_id. Mirrored
  // into a ref so the once-registered WS handler reads the live value.
  const [activeRungId, setActiveRungId] = useState<string>(initialRungId);
  const activeRungIdRef = useRef<string>(initialRungId);
  // Every real Vulkan device is measured serially and the backend keeps the
  // fastest runnable result. Track device identity independently from the
  // model rung so a new device starts with a fresh meter.
  const [activeDevice, setActiveDevice] = useState<
    { name: string; index: number } | undefined
  >(undefined);
  const activeDeviceKeyRef = useRef<string | undefined>(undefined);
  // Set only during the one-second authoritative per-device completion beat.
  // The active device, number, and fill remain unchanged until queued events
  // are replayed at the end of the dwell.
  const [completedDeviceVerdict, setCompletedDeviceVerdict] = useState<
    BenchmarkDeviceVerdict | undefined
  >(undefined);
  // The measured rung's catalog id + its measured speed (words/sec), snapshotted
  // from benchmark.result. Drives the winner hero and the landed receipt bars on
  // the verdict cards. Defaults to the initial rung (so a chip has a name before
  // the result lands).
  const [measuredModelId, setMeasuredModelId] = useState<string>(initialRungId);
  const [measuredWps, setMeasuredWps] = useState<number>(0);

  // Keep phaseRef in sync so the WS handler (registered once) reads the live
  // phase without re-subscribing on every transition.
  useEffect(() => {
    phaseRef.current = phase;
    if (phase === "prefilter" || phase === "benchmark") {
      indeterminateSinceRef.current = performance.now();
    }
  }, [phase]);

  // Advance to `next`, honoring the indeterminate min-display window when we are
  // currently sitting on an indeterminate (prefilter / benchmark) card.
  const advanceFrom = (from: Phase, next: Phase) => {
    if (phaseRef.current !== from) return; // stale event, already moved on
    const held = performance.now() - indeterminateSinceRef.current;
    const wait = Math.max(0, MIN_INDETERMINATE_MS - held);
    if (wait === 0) {
      setPhase(next);
    } else {
      window.setTimeout(() => {
        if (phaseRef.current === from) setPhase(next);
      }, wait);
    }
  };

  // Kick the REST benchmark when we enter prefilter (intro -> Continue, or a
  // re-check that starts directly at prefilter). Runs once.
  useEffect(() => {
    if (phase !== "prefilter" || benchmarkStartedRef.current) return;
    benchmarkStartedRef.current = true;
    // The automatic first-setup check names no model: the backend measures
    // Stream-1 Mini. A manual check names exactly the model it measures.
    startBenchmark({ mode: benchMode, modelId: benchMode === "auto" ? undefined : benchModelId }).catch((e) => {
      const msg = String(e);
      // 409 = a benchmark is already running (StrictMode double-invoke or a
      // concurrent re-check). Join it rather than failing the flow.
      if (msg.includes("409")) return;
      console.error("[Benchmark] start failed error_type=RemoteError");
      setPhase("infrastructure_error");
    });
  }, [phase, benchMode, benchModelId]);

  // Single WS subscription for benchmark.progress / benchmark.result. The
  // download leg drives DownloadCard through subscribeToDownloadEvents below.
  // A per-device completion starts a presentation-only dwell; subsequent
  // benchmark events stay in a FIFO queue until that reading has been visible
  // for one second. Backend measurement never waits on this UI timer.
  useEffect(() => {
    let completionDwellTimer: number | undefined;
    let holdingDeviceCompletion = false;
    let replayingAfterDeviceCompletion = false;
    let disposed = false;
    const pendingBenchmarkEvents: BenchmarkStreamEvent[] = [];

    const handleEvent: (event: AppEvent) => void = (event) => {
      if (
        holdingDeviceCompletion &&
        (event.type === "benchmark.progress" ||
          event.type === "benchmark.result")
      ) {
        pendingBenchmarkEvents.push(event);
        return;
      }

      if (event.type === "benchmark.progress") {
        if (event.phase === "prefilter") {
          // Prefilter passed -> a real device is present; download the model.
          advanceFrom("prefilter", "download");
        } else if (event.phase === "device_complete") {
          const rung = event.model_id;
          const eventDevice =
            typeof event.device_name === "string" &&
            typeof event.device_index === "number"
              ? { name: event.device_name, index: event.device_index }
              : undefined;

          if (rung) {
            activeRungIdRef.current = rung;
            setActiveRungId(rung);
          }
          if (eventDevice) {
            activeDeviceKeyRef.current =
              `${eventDevice.index}:${eventDevice.name}`;
            setActiveDevice(eventDevice);
          }

          // Terminal llama.cpp tok/s is authoritative. A missing timing means
          // the device attempt failed closed, which displays as 0 WPS instead
          // of retaining a stale live estimate.
          const completedWps =
            typeof event.measured_tok_per_s === "number"
              ? tokPerSecToWordsPerSec(event.measured_tok_per_s)
              : 0;
          setLiveWps(completedWps);
          setCompletedDeviceVerdict(event.verdict ?? "unviable");
          if (
            phaseRef.current !== "success" &&
            phaseRef.current !== "marginal" &&
            phaseRef.current !== "failure"
          ) {
            setPhase("benchmark");
          }

          holdingDeviceCompletion = true;
          if (completionDwellTimer !== undefined) {
            window.clearTimeout(completionDwellTimer);
          }
          completionDwellTimer = window.setTimeout(() => {
            completionDwellTimer = undefined;
            if (disposed) return;
            holdingDeviceCompletion = false;
            replayingAfterDeviceCompletion = true;

            // Stop when replay reaches another completion. That event starts
            // its own dwell and leaves the remaining FIFO entries untouched.
            while (
              !holdingDeviceCompletion &&
              pendingBenchmarkEvents.length > 0
            ) {
              const next = pendingBenchmarkEvents.shift();
              if (next) handleEvent(next);
            }
            replayingAfterDeviceCompletion = false;
          }, DEVICE_COMPLETION_DWELL_MS);
        } else if (event.phase === "running") {
          setCompletedDeviceVerdict(undefined);
          const rung = event.model_id;
          const eventDevice =
            typeof event.device_name === "string" &&
            typeof event.device_index === "number"
              ? { name: event.device_name, index: event.device_index }
              : undefined;
          const eventDeviceKey = eventDevice
            ? `${eventDevice.index}:${eventDevice.name}`
            : undefined;
          const rungChanged = !!rung && rung !== activeRungIdRef.current;
          const deviceChanged =
            eventDeviceKey !== undefined &&
            eventDeviceKey !== activeDeviceKeyRef.current;
          if (rungChanged && rung) {
            // Defensive: follow the backend's model id so the chip names what
            // is actually being measured.
            activeRungIdRef.current = rung;
            setActiveRungId(rung);
          }
          if (eventDevice) {
            activeDeviceKeyRef.current = eventDeviceKey;
            setActiveDevice(eventDevice);
          }
          if (rungChanged || deviceChanged) {
            // Device-start events intentionally carry no rate. Reset before
            // the new device's first tick so serial GPU measurements cannot
            // look like one benchmark plummeting from the previous device.
            setLiveWps(undefined);
          } else {
            // New backends stream directly measured generated words/s. Keep a
            // legacy tok/s conversion so an older backend can still drive the
            // meter during a mixed-version upgrade.
            const wps =
              event.wps ??
              (event.tps != null
                ? tokPerSecToWordsPerSec(event.tps)
                : undefined);
            if (wps != null) setLiveWps(wps);
          }
          // Decode started -> show the seconds-long "Testing" card. When the
          // model is already present, the backend fires prefilter→download→running
          // faster than the min-display gate, so we may still be on prefilter (a
          // pending prefilter→download advance is queued) and advanceFrom(
          // "download", ...) would be DROPPED — sticking on "Downloading" for the
          // whole decode. Force the benchmark card from any non-terminal phase;
          // the pending prefilter→download advance then no-ops (phaseRef is now
          // "benchmark"). This is the long, important phase, so showing it
          // immediately (vs honoring the min-display flash) is correct.
          if (
            phaseRef.current !== "success" &&
            phaseRef.current !== "marginal" &&
            phaseRef.current !== "failure"
          ) {
            setPhase("benchmark");
          }
        }
        return;
      }
      if (event.type === "benchmark.result") {
        if (event.outcome === "infrastructure_error") {
          setPhase("infrastructure_error");
          return;
        }
        freshlyDownloadedRef.current = event.freshly_downloaded ?? false;
        // Snapshot the measured model + its speed BEFORE the phase transition so
        // the winner hero and landed receipt bars are ready whichever way
        // advanceFrom resolves. measured_model_id is absent on an older backend
        // -> fall back to the model this check targets.
        const measuredId = event.measured_model_id ?? benchModelId ?? currentModelId;
        verdictsRef.current = event.verdicts ?? { [measuredId]: event.verdict };
        const wps =
          event.measured_tok_per_s != null
            ? tokPerSecToWordsPerSec(event.measured_tok_per_s)
            : 0;
        setMeasuredModelId(measuredId);
        setMeasuredWps(wps);

        // Verdict routing: viable -> success, marginal -> the slow-use consent
        // card, unviable -> failure.
        let target: Phase;
        if (event.verdict === "viable") {
          target = "success";
        } else if (event.verdict === "marginal") {
          target = "marginal";
        } else {
          target = "failure";
        }

        // Advance from whatever phase we are currently in. viable/marginal always
        // follow a decode (a benchmark.progress "running" set a pending
        // setPhase("benchmark")), so from a non-benchmark phase we jump straight
        // to the target to avoid the min-display timeout racing the pending
        // transition and dropping the result. A pure unviable can also arrive
        // from prefilter (instant-fail, no download) with no pending decode, so
        // it honors the min-display gate from either indeterminate phase.
        const from = phaseRef.current;
        const decodeOrigin = event.verdict !== "unviable";
        // A buffered result replayed after the one-second device dwell has
        // already exceeded the generic 400ms indeterminate minimum, so it can
        // land immediately without introducing a second, accidental delay.
        if (replayingAfterDeviceCompletion) {
          setPhase(target);
        } else if (decodeOrigin) {
          if (from === "benchmark") {
            advanceFrom("benchmark", target);
          } else {
            setPhase(target);
          }
        } else if (from === "prefilter" || from === "benchmark") {
          advanceFrom(from, target);
        } else {
          setPhase(target);
        }
        return;
      }
    };

    const unsubscribe = subscribe(handleEvent);
    return () => {
      disposed = true;
      pendingBenchmarkEvents.length = 0;
      if (completionDwellTimer !== undefined) {
        window.clearTimeout(completionDwellTimer);
      }
      unsubscribe();
    };
    // subscribe is stable (useCallback in useAppSocket); register once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subscribe]);

  function fireVerdict(v: BenchmarkVerdict) {
    if (verdictFiredRef.current) return;
    verdictFiredRef.current = true;
    onVerdict(v);
  }

  // Option B "Test again": re-arm the run guards and drop back to prefilter,
  // which re-kicks startBenchmark(). The GGUF is already present, so the
  // download leg skips instantly (download_llm_model is idempotent) and the
  // decode test re-runs — recovering transient failures (VRAM busy, etc.)
  // without a 3 GB re-download. Never deletes the model.
  const handleRetry = () => {
    verdictFiredRef.current = false;
    benchmarkStartedRef.current = false;
    indeterminateSinceRef.current = performance.now();
    verdictsRef.current = undefined;
    setLiveWps(undefined); // next measuring phase starts on the marquee again
    setCompletedDeviceVerdict(undefined);
    activeRungIdRef.current = initialRungId;
    setActiveRungId(initialRungId);
    activeDeviceKeyRef.current = undefined;
    setActiveDevice(undefined);
    setMeasuredModelId(initialRungId);
    setMeasuredWps(0);
    setPhase("prefilter");
  };

  // Cancel during download aborts the in-flight GGUF download backend-side, then
  // tears down the flow with no config write. Intro / prefilter cancel just
  // unmounts (the prefilter is sub-second; no backend abort needed).
  const handleCancel = () => {
    if (phaseRef.current === "download") {
      cancelLocalLLMDownload().catch(() => undefined);
    }
    onCancel();
  };

  // Inline: fast and unviable verdicts apply on arrival. The fast card says
  // "Enhancement is on", so the write must already have happened; unviable
  // only caches the verdict (no retry, no cloud fallback in onboarding).
  const isInline = variant === "inline";
  useEffect(() => {
    if (!isInline) return;
    if (phase === "success") {
      fireVerdict({
        kind: "viable",
        appliedModelId: measuredModelId,
        verdicts: verdictsRef.current,
      });
    } else if (phase === "failure") {
      fireVerdict({
        kind: "unviable",
        freshlyDownloaded: freshlyDownloadedRef.current,
        verdicts: verdictsRef.current,
      });
    }
    // fireVerdict is guarded by verdictFiredRef; run on phase arrival only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isInline, phase]);

  const inlineStateRef = useRef(onInlineStateChange);
  inlineStateRef.current = onInlineStateChange;
  useEffect(() => {
    const report = inlineStateRef.current;
    if (!isInline || !report) return;
    if (phase === "marginal") {
      const verdicts = { ...verdictsRef.current, [measuredModelId]: "marginal" as const };
      report({
        phase,
        useAnyway: () =>
          fireVerdict({ kind: "marginal", appliedModelId: measuredModelId, verdicts }),
        leaveOff: () => {
          fireVerdict({ kind: "marginal", appliedModelId: null, verdicts });
          setPhase("declined");
        },
      });
    } else if (phase === "infrastructure_error") {
      report({ phase, retry: handleRetry });
    } else if (phase === "success" || phase === "failure" || phase === "declined") {
      report({ phase });
    } else {
      report({ phase: "running" });
    }
    // handleRetry/fireVerdict only touch refs and stable setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isInline, phase, measuredModelId]);

  if (isInline) {
    const downloadSubscribe = (handler: (event: AppEvent) => void) =>
      subscribeToDownloadEvents(
        subscribe,
        handler as Parameters<typeof subscribeToDownloadEvents>[1],
        "local_llm",
      );
    const measuredName = LOCAL_MODEL_DISPLAY_LABEL[measuredModelId] ?? measuredModelId;
    return (
      <div data-benchmark-variant="inline">
        <AnimatePresence mode="wait">
          {(phase === "intro" || phase === "prefilter") && (
            <InlinePrefilterPhase
              key="prefilter"
              modelId={benchModelId}
              modelSize={benchModelSize}
              onCancel={handleCancel}
            />
          )}
          {phase === "download" && (
            <InlineDownloadPhase
              key="download"
              modelId={benchModelId}
              modelSize={benchModelSize}
              subscribe={downloadSubscribe}
              onCancel={handleCancel}
            />
          )}
          {phase === "benchmark" && (
            <InlineMeasuringPhase
              key="benchmark"
              modelId={activeRungId}
              modelSize={benchModelSize}
              liveWps={liveWps}
              deviceName={activeDevice?.name}
              completedVerdict={completedDeviceVerdict}
            />
          )}
          {phase === "success" && (
            <InlineVerdictPhase
              key="success"
              phaseKey="success"
              title="Enhancement is on"
              body={`${measuredName} runs well on this computer. Try it with two quick examples.`}
              modelId={measuredModelId}
              modelSize={benchModelSize}
              chipStatus="landed"
              measuredWps={measuredWps}
              tone="mint"
              verdictChip="viable"
            />
          )}
          {phase === "marginal" && (
            <InlineVerdictPhase
              key="marginal"
              phaseKey="marginal"
              title={`${measuredName} runs slowly here`}
              body="It works, but your text may take a few extra seconds to appear after you stop speaking."
              modelId={measuredModelId}
              modelSize={benchModelSize}
              chipStatus="active"
              measuredWps={measuredWps}
              tone="caution"
              verdictChip="marginal"
            />
          )}
          {phase === "declined" && (
            <InlineVerdictPhase
              key="declined"
              phaseKey="declined"
              title={`${measuredName} runs slowly here`}
              body="Enhancement is off. You can turn it on anytime in Enhancement."
              modelId={measuredModelId}
              modelSize={benchModelSize}
              chipStatus="dimmed"
              measuredWps={measuredWps}
              tone="caution"
              settledReceipt
            />
          )}
          {phase === "failure" && (
            <InlineVerdictPhase
              key="failure"
              phaseKey="failure"
              title="This computer is too slow for enhancement"
              body="Enhancement stays off. Dictation works the same without it."
              modelId={measuredModelId}
              modelSize={benchModelSize}
              chipStatus="dimmed"
              measuredWps={measuredWps}
              tone="rose"
              verdictChip="unviable"
            />
          )}
          {phase === "infrastructure_error" && (
            <InlineVerdictPhase
              key="infrastructure_error"
              phaseKey="infrastructure_error"
              title="Couldn't complete the speed check"
              body="We couldn't download or test the local model. Check your connection or restart Whisperstream, then try again."
              modelId={benchModelId}
              modelSize={benchModelSize}
              chipStatus="dimmed"
            />
          )}
        </AnimatePresence>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        // Backdrop dismiss only on intro (matches EnableLocalConfirmDialog).
        // Once work is in flight, an accidental click must not abort it.
        onClick={phase === "intro" ? onCancel : undefined}
      />

      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[420px] rounded-[var(--radius-card)] p-7"
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="benchmark-heading"
      >
        <AnimatePresence mode="wait">
          {phase === "intro" && (
            <IntroPhase
              key="intro"
              firstSetup={isFirstSetup}
              modelId={benchModelId}
              modelSize={benchModelSize}
              onCancel={onCancel}
              onContinue={() => {
                activeRungIdRef.current = initialRungId;
                setActiveRungId(initialRungId);
                setPhase("prefilter");
              }}
            />
          )}

          {phase === "prefilter" && (
            <IndeterminatePhase
              key="prefilter"
              status="Checking your hardware..."
              onCancel={handleCancel}
              recheck={isRecheck}
            />
          )}

          {phase === "download" && (
            <motion.div
              key="download"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
            >
              {isRecheck && <RecheckContextStrip />}
              <DownloadCard
                compact
                hideFooter
                title="Downloading the model"
                subtitle="This can take a few minutes."
                subscribe={(handler) =>
                  subscribeToDownloadEvents(
                    subscribe,
                    handler as Parameters<typeof subscribeToDownloadEvents>[1],
                    "local_llm",
                  )
                }
              />
              <div className="mt-5 flex items-center justify-end">
                <CancelPill onClick={handleCancel} />
              </div>
            </motion.div>
          )}

          {phase === "benchmark" && (
              <MeasuringPhase
                key="benchmark"
                modelId={activeRungId}
                liveWps={liveWps}
                deviceName={activeDevice?.name}
                completedVerdict={completedDeviceVerdict}
                status={
                  isRecheck
                    ? "Testing your updated hardware..."
                    : `Checking ${LOCAL_MODEL_DISPLAY_LABEL[activeRungId] ?? activeRungId}`
                }
                recheck={isRecheck}
              />
          )}

          {phase === "success" && (
            <SuccessPhase
              key="success"
              recheck={isRecheck}
              winnerModelId={measuredModelId}
              landedWps={measuredWps}
              onDone={() => {
                // Recheck keeps the calmer "keep current, cache verdict" fire
                // (no model switch). Setup/manual applies the measured winner and
                // merges every rung's real verdict into the cache.
                if (isRecheck && !applyModelOnSuccess) {
                  fireVerdict({
                    kind: "viable",
                    verdicts: verdictsRef.current,
                  });
                } else {
                  fireVerdict({
                    kind: "viable",
                    appliedModelId: measuredModelId,
                    verdicts: verdictsRef.current,
                  });
                }
              }}
            />
          )}

          {phase === "marginal" && (
            <MarginalPhase
              key="marginal"
              currentCleanupEnabled={currentCleanupEnabled}
              modelId={measuredModelId}
              measuredWps={measuredWps}
              onVerdict={value => fireVerdict(value.kind === "marginal" ? {...value, verdicts: {...verdictsRef.current, ...value.verdicts}} : value)}
            />
          )}

          {phase === "failure" && (
            <FailurePhase
              key="failure"
              failureFallback={failureFallback}
              modelId={measuredModelId}
              measuredWps={measuredWps}
              onRetry={handleRetry}
              onGiveUp={() =>
                fireVerdict({
                  kind: "unviable",
                  freshlyDownloaded: freshlyDownloadedRef.current,
                  verdicts: verdictsRef.current,
                })
              }
            />
          )}

          {phase === "infrastructure_error" && (
            <InfrastructureFailurePhase
              key="infrastructure_error"
              onRetry={handleRetry}
              onDismiss={onCancel}
            />
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  );
}

/* ---------------------------------------------------------------------------
   Intro phase: one model, no chooser (design.md "Speed-check intro"). A
   non-interactive model card names the model with its size so the download is
   never a surprise. Cancel is focused on mount; Esc dismisses; Enter is
   intentionally NOT bound (read-first).
--------------------------------------------------------------------------- */
function IntroPhase({
  firstSetup,
  modelId,
  modelSize,
  onCancel,
  onContinue,
}: {
  onCancel: () => void;
  onContinue: () => void;
  firstSetup: boolean;
  modelId: string;
  modelSize?: string;
}) {
  const cancelBtnRef = useRef<HTMLButtonElement>(null);
  const name = LOCAL_MODEL_DISPLAY_LABEL[modelId] ?? modelId;
  const description = LOCAL_MODEL_DESCRIPTION[modelId];

  // Esc dismisses; Enter is intentionally NOT bound (read-first).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  useEffect(() => {
    cancelBtnRef.current?.focus();
  }, []);

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
    >
      <h2
        id="benchmark-heading"
        className="font-display text-[18px] font-normal text-slate text-center mb-3"
      >
        {firstSetup ? "Set up local enhancement" : `Try ${name}`}
      </h2>

      <p className="text-[13px] text-steel mb-4 leading-relaxed text-center">
        {`We'll check how fast ${name} runs on your computer. If it isn't downloaded yet, we'll get it first${modelSize ? ` (${modelSize})` : ""}.`}
      </p>

      <div
        className="flex items-center gap-2.5 text-left px-3.5 py-2.5 rounded-[14px] mb-6"
        style={{
          background: "var(--surface-control)",
          border: "1px solid rgba(107,130,153,0.28)",
        }}
        data-benchmark-model-card
      >
        <ModelIdentityChip kind={LOCAL_MODEL_FIRST_PARTY[modelId] ? "mark" : "pen"} />
        <span className="min-w-0 flex-1">
          <span className="block text-[13px] font-medium text-slate">{name}</span>
          {description && (
            <span className="block text-[11px] leading-4 text-steel">{description}</span>
          )}
        </span>
        {modelSize && (
          <span className="shrink-0 text-[11px] text-steel/70 tabular-nums">{modelSize}</span>
        )}
      </div>

      <div className="flex items-center justify-center gap-2.5">
        <button
          ref={cancelBtnRef}
          onClick={onCancel}
          className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
          style={glassPillStyle}
        >
          Cancel
        </button>
        <button
          onClick={onContinue}
          className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
          style={gradientPillStyle}
        >
          Start speed check
        </button>
      </div>
    </motion.div>
  );
}

/* ---------------------------------------------------------------------------
   Recheck context strip (recheck mode only) — the launch-time auto-rebench
   dialog opens unprompted after the backend detects the graphics hardware
   changed, so there is no intro card to set context the way the first-run flow
   has. This strip answers what changed / what we're doing / are dictations
   affected, up front, and stays constant across the progress phases. It carries
   the `benchmark-heading` id so the dialog's aria-labelledby still resolves.
--------------------------------------------------------------------------- */
function RecheckContextStrip() {
  return (
    <div className="mb-5">
      <h2
        id="benchmark-heading"
        className="font-display text-[18px] font-normal text-slate mb-2"
      >
        Your graphics hardware changed
      </h2>
      <p className="text-[13px] text-steel leading-relaxed">
        We're rechecking that local enhancement still runs well here. Dictation keeps
        working.
      </p>
    </div>
  );
}

/* ---------------------------------------------------------------------------
   Indeterminate phase (prefilter / benchmark) — DownloadCard's total===0
   marquee-shimmer treatment + a pulsing "active life sign" dot and an italic
   status line. Honest indeterminate: these legs are sub-second to seconds, so a
   percentage would be dishonest (design.md Decision 4).
--------------------------------------------------------------------------- */
function IndeterminatePhase({
  status,
  onCancel,
  recheck = false,
}: {
  status: string;
  onCancel?: () => void;
  recheck?: boolean;
}) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
    >
      {recheck ? (
        // The recheck strip carries the heading (and the id the dialog is
        // labelled by); the first-run "Setting things up" title is wrong here.
        <RecheckContextStrip />
      ) : (
        <h2
          id="benchmark-heading"
          className="font-display text-[18px] font-normal text-slate mb-2"
        >
          Setting things up
        </h2>
      )}

      {/* Marquee-shimmer track — DownloadCard's total===0 branch verbatim. */}
      <div
        className="mt-5 w-full h-2 rounded-full overflow-hidden relative"
        style={{ background: "var(--frost-mesh-1)" }}
      >
        <motion.div
          className="absolute inset-y-0 w-1/3 rounded-full"
          style={{
            background:
              "linear-gradient(90deg, transparent, var(--color-frost), transparent)",
          }}
          animate={{ x: ["-100%", "400%"] }}
          transition={{ duration: 1.5, repeat: Infinity, ease: "easeInOut" }}
        />
      </div>

      <div className="mt-3 flex items-center gap-1.5">
        <motion.span
          className="w-1.5 h-1.5 rounded-full shrink-0"
          style={{
            background: "var(--accent-fill)",
            boxShadow: "0 0 8px rgba(126, 174, 212, 0.55)",
          }}
          animate={{ opacity: [1, 0.4, 1] }}
          transition={{ duration: 1.6, repeat: Infinity, ease: "easeInOut" }}
        />
        <p className="text-[11px] text-steel/60 italic">{status}</p>
      </div>

      {onCancel && (
        <div className="mt-5 flex items-center justify-end">
          <CancelPill onClick={onCancel} />
        </div>
      )}
    </motion.div>
  );
}

/* ---------------------------------------------------------------------------
   Success phase — calm mint confirmation, single gradient pill (success is not
   a decision). The mint dot is the local-ready success token (system.md).
--------------------------------------------------------------------------- */
function SuccessPhase({
  onDone,
  recheck = false,
  winnerModelId,
  landedWps,
}: {
  onDone: () => void;
  recheck?: boolean;
  /** The measured winning model id. Its measuring chip morphs into the hero via
   *  the shared layoutId. Omitted (or in recheck mode) -> the calm mint-dot
   *  confirmation instead. */
  winnerModelId?: string;
  /** Landed measured speed (words/sec) for the mint receipt bar. */
  landedWps?: number;
}) {
  // Recheck stays the calm mint-dot confirmation; the first-run success blooms
  // the winner hero + the landed mint receipt bar (SpeedBar lands mint here).
  const showHero = !recheck && !!winnerModelId;
  // Stream-1 Mini gets its locked line; any other measured model (a manual
  // Customizable check) keeps the generic on-device line.
  const body = recheck
    ? "Nothing changes. Enhancement runs on this computer."
    : winnerModelId === "stream-1-mini"
      ? "Stream-1 Mini runs well here. Enhancement stays on this computer."
      : "Enhancement uses graphics acceleration. Enhancement runs on this computer.";
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
    >
      {showHero ? (
        <WinnerHero id={winnerModelId!} />
      ) : (
        <div className="flex justify-center mb-3">
          <span
            className="w-2 h-2 rounded-full bg-mint"
            style={{ boxShadow: "0 0 10px rgba(91, 175, 124, 0.5)" }}
            aria-hidden="true"
          />
        </div>
      )}

      <h2
        id="benchmark-heading"
        className="font-display text-[18px] font-normal text-slate text-center mb-2"
      >
        {recheck ? "Local enhancement still runs here" : "You're all set"}
      </h2>

      <p
        className={`text-[13px] text-steel leading-relaxed text-center ${
          landedWps ? "mb-4" : "mb-6"
        }`}
      >
        {body}
      </p>

      {landedWps ? (
        <div className="mb-5">
          <SpeedBar mode="fill" value={landedWps} tone="mint" />
        </div>
      ) : null}

      <div className="flex justify-center">
        <button
          onClick={onDone}
          className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
          style={gradientPillStyle}
        >
          {recheck ? "Done" : "Start using it"}
        </button>
      </div>
    </motion.div>
  );
}

/* ---------------------------------------------------------------------------
   Failure phase (unviable) — ported rose card: the local ladder chips dimmed +
   a rose landed SpeedBar (the receipt of the too-slow measurement) over the
   frost-glass shell. Rose reads as "not yet", not an error. Three variants by
   `failureFallback`:
   - "gemini"  (user was on local): the free-Google-key handoff + a privacy note.
   - "current" (user already on a working cloud provider): stay put, no Gemini
     push, lighter body.
   - "local"   (Lockdown): stay on-device and offer retry or dismiss.
   Both carry "Test again" (Option B retry, reuses the downloaded model). No
   keep/delete checkbox — auto-delete is the parent's job on give-up. Copy was
   clarified per user feedback so the headline names the speed check + the
   hardware verdict instead of the old "can't run cleanup yet" pending framing.
--------------------------------------------------------------------------- */
function FailurePhase({
  failureFallback,
  modelId,
  measuredWps,
  onRetry,
  onGiveUp,
}: {
  failureFallback: "gemini" | "current" | "local";
  modelId: string;
  /** Landed measured speed (words/sec) for the rose receipt bar. */
  measuredWps: number;
  onRetry: () => void;
  onGiveUp: () => void;
}) {
  const isGemini = failureFallback === "gemini";
  const isLocalOnly = failureFallback === "local";
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
    >
      <h2
        id="benchmark-heading"
        className="font-display text-[18px] font-normal text-slate text-center mb-2"
      >
        The speed check found this computer too slow for enhancement
      </h2>

      {/* The one model this check measured. */}
      <div className="flex items-center justify-center gap-2.5 my-4">
        <ModelChip id={modelId} status="dimmed" />
      </div>

      <div className="mb-4">
        <SpeedBar mode="fill" value={measuredWps} tone="rose" />
      </div>

      {!isGemini && (
        <p className="text-[13px] text-steel mb-5 leading-relaxed text-center">
          {isLocalOnly
            ? "Local enhancement stays off. You can retry the speed check when you're ready."
            : "Your enhancement settings have not changed."}
        </p>
      )}

      <div className="flex items-center justify-center gap-2.5">
        <GlassPill onClick={onRetry}>Test again</GlassPill>
        <PrimaryPill onClick={onGiveUp}>
          {isGemini ? "Use Gemini's free tier" : isLocalOnly ? "Dismiss" : "Got it"}
        </PrimaryPill>
      </div>
    </motion.div>
  );
}

function InfrastructureFailurePhase({
  onRetry,
  onDismiss,
}: {
  onRetry: () => void;
  onDismiss: () => void;
}) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
    >
      <h2
        id="benchmark-heading"
        className="font-display text-[18px] font-normal text-slate text-center mb-2"
      >
        Couldn't complete the speed check
      </h2>
      <p className="text-[13px] text-steel my-5 leading-relaxed text-center">
        We couldn't download or test the local model. Check your connection or
        restart Whisperstream, then try again.
      </p>
      <div className="flex items-center justify-center gap-2.5">
        <GlassPill onClick={onRetry}>Try again</GlassPill>
        <PrimaryPill onClick={onDismiss}>Dismiss</PrimaryPill>
      </div>
    </motion.div>
  );
}

/* ---------------------------------------------------------------------------
   Measuring phase (was IndeterminatePhase for "benchmark") — the ported chip
   row + SpeedBar. The chip for the model under test is active; the bar sweeps
   marquee until the first streamed WPS tick, then live-fills to the measured
   throughput with the visible scale's semantic classification. Its chip stays
   static because there is no model handoff to communicate. On success the chip
   still morphs into the WinnerHero via the shared layoutId.
--------------------------------------------------------------------------- */
function MeasuringPhase({
  modelId,
  liveWps,
  deviceName,
  completedVerdict,
  status,
  recheck = false,
}: {
  modelId: string;
  liveWps: number | undefined;
  deviceName?: string;
  completedVerdict?: BenchmarkDeviceVerdict;
  status: string;
  recheck?: boolean;
}) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
    >
      {recheck ? (
        <RecheckContextStrip />
      ) : (
        <h2
          id="benchmark-heading"
          className="font-display text-[18px] font-normal text-slate mb-2"
        >
          Checking how fast it runs
        </h2>
      )}

      <div className="flex items-center gap-2.5 mt-4 mb-5">
        <ModelChip id={modelId} status="active" animateActive={false} />
      </div>

      {deviceName ? (
        <p className="text-[11px] text-steel mb-2 truncate" title={deviceName}>
          Testing on {deviceName}
        </p>
      ) : null}

      <SpeedBar
        mode={liveWps != null ? "fill" : "marquee"}
        wps={liveWps}
        settled={completedVerdict != null}
        tone={
          completedVerdict
            ? completionTone(completedVerdict)
            : liveWps != null
              ? speedToneForWps(liveWps)
              : "frost"
        }
      />

      <div className="mt-3">
        {completedVerdict ? (
          <DeviceCompletionStatus verdict={completedVerdict} />
        ) : (
          <LifeSignStatus status={status} active={liveWps != null} />
        )}
      </div>
    </motion.div>
  );
}

/* ---------------------------------------------------------------------------
   Marginal phase (tone caution) — the measured model runs, just slowly.
   Against-advice = SECONDARY glass pill ("Use it anyway"); the safe path is
   PRIMARY ("Keep my current model"). Each button fires exactly ONE `marginal`
   BenchmarkVerdict, which the parent turns into a single combined config patch.
   The SpeedBar carries the `caution` tone (signal yellow, --color-caution) —
   retired off the mustard `gold` tone in Issue 4.
--------------------------------------------------------------------------- */
function MarginalPhase({
  currentCleanupEnabled,
  modelId,
  measuredWps,
  onVerdict,
}: {
  currentCleanupEnabled: boolean;
  modelId: string;
  measuredWps: number;
  onVerdict: (v: BenchmarkVerdict) => void;
}) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
    >
      <h2
        id="benchmark-heading"
        className="font-display text-[18px] font-normal text-slate text-center mb-3"
      >
        {`${LOCAL_MODEL_DISPLAY_LABEL[modelId] ?? modelId} runs slowly here`}
      </h2>

      <div className="flex justify-center mb-3"><ModelChip id={modelId} status="active" animateActive={false} /></div>
      <div className="mb-5">
        <SpeedBar mode="fill" value={measuredWps} tone="caution" />
      </div>

      <div className="flex items-center justify-center gap-2.5">
        <GlassPill
          onClick={() =>
            onVerdict({
              kind: "marginal",
              appliedModelId: modelId,
              verdicts: { [modelId]: "marginal" },
            })
          }
        >
          Use it anyway
        </GlassPill>
        <PrimaryPill
          onClick={() =>
            onVerdict({
              kind: "marginal",
              appliedModelId: null,
              verdicts: { [modelId]: "marginal" },
            })
          }
        >
          {currentCleanupEnabled ? "Keep my current model" : "Keep local enhancement off"}
        </PrimaryPill>
      </div>

      <p className="text-[12px] text-steel leading-relaxed text-center mt-4">
        Enhancement may take longer after you finish speaking.
      </p>
    </motion.div>
  );
}

/* ---------------------------------------------------------------------------
   Shared Cancel pill — the quiet ghost-button treatment from DownloadDialog
   (text-steel, no fill, hover wash). Used in prefilter / download.
--------------------------------------------------------------------------- */
function CancelPill({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="px-4 py-1.5 rounded-full text-[13px] font-medium text-steel hover:text-slate hover:bg-white/40 cursor-pointer transition-all duration-200"
    >
      Cancel
    </button>
  );
}

