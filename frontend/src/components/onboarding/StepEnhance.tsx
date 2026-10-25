import { useCallback, useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import type { AppConfig } from "../../lib/api/config";
import type { AppEvent } from "../../lib/socket/events";
import { saveConfig } from "../../lib/api/config";
import {
  cancelLocalLLMDownload,
  deleteLocalModel,
  getEnhancementModels,
  getLlamaStatus,
} from "../../lib/api/enhancement";
import { AUTO_BENCHMARK_MODEL_ID, LOCAL_MODEL_DISPLAY_LABEL } from "../../lib/localModels";
import { errorType } from "../../lib/logCapture";
import {
  BenchmarkFlow,
  type BenchmarkVerdict,
  type InlineBenchmarkState,
} from "../BenchmarkFlow";
import { LifeSignStatus } from "../BenchmarkVisuals";
import {
  buildBenchmarkVerdictPatch,
  onboardingVerdictContext,
} from "../../pages/enhancement/benchmarkVerdictPatch";
import { formatModelSizeShort } from "../../pages/enhancement/localModels";
import { EnhancePair } from "./EnhancePair";
import { EnhancePreviewCarousel } from "./EnhancePreviewCarousel";
import { KeyChip } from "./KeyChip";
import { RecStatus } from "./RecStatus";
import examples from "./enhanceExamples.json";

// Onboarding's optional last step: turn on local enhancement with Stream-1
// Mini, measure it inline, then try two read-aloud examples through the real
// mic and pipeline (design.md, copy-review.md Locked Copy Decisions).
//
//   intro -> run (BenchmarkFlow inline: download, speed check, verdict) -> try
//
// The overlay renders the footer; this step reports it through
// onFooterChange on every sub-phase change. Skipping never downloads anything
// from the intro, and cancels an in-flight download from the run.
//
// Try-it labels results by the backend's `enhancement` outcome on
// transcription.result (present while onboarding suppresses injection), never
// by comparing raw and enhanced text. Only the intro shows an expected output.

export interface FooterAction {
  label: string;
  onClick: () => void;
  disabled?: boolean;
}

export interface FooterSpec {
  ghost?: FooterAction;
  secondary?: FooterAction;
  primary: FooterAction;
}

type SubPhase = "intro" | "run" | "try";

type TryResult = {
  raw: string;
  text: string;
  enhancement: "applied" | "skipped" | "failed" | "off";
};

type TryState =
  | { kind: "ready" }
  | { kind: "listening" }
  | { kind: "enhancing" }
  | { kind: "miss" }
  | { kind: "result"; result: TryResult };

const MODEL_ID = AUTO_BENCHMARK_MODEL_ID;
const MODEL_NAME = LOCAL_MODEL_DISPLAY_LABEL[MODEL_ID] ?? MODEL_ID;
const SKIP_LABEL = "Skip & finish";

const fade = {
  initial: { opacity: 0 },
  animate: { opacity: 1 },
  exit: { opacity: 0 },
  transition: { duration: 0.22, ease: [0.2, 0, 0, 1] as const },
};

const heroClass =
  "font-display text-[24px] font-light tracking-tight text-slate leading-tight outline-none";

interface StepEnhanceProps {
  config: AppConfig;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  /** Completes setup (the overlay's handleFinish). */
  onFinish: () => void;
  /** True while the overlay is saving setup_completed. */
  finishing?: boolean;
  onFooterChange: (spec: FooterSpec) => void;
}

export function StepEnhance({
  config,
  subscribe,
  onConfigChange,
  onFinish,
  finishing = false,
  onFooterChange,
}: StepEnhanceProps) {
  const [subPhase, setSubPhase] = useState<SubPhase>("intro");
  const [runKey, setRunKey] = useState(0);
  const [inlineState, setInlineState] = useState<InlineBenchmarkState>({ phase: "running" });
  const [modelSize, setModelSize] = useState<string | undefined>(undefined);
  const subPhaseRef = useRef<SubPhase>("intro");
  subPhaseRef.current = subPhase;
  const runningRef = useRef(false);
  runningRef.current = subPhase === "run" && inlineState.phase === "running";

  // Model size for the intro meta line and the chip ("462 MB").
  useEffect(() => {
    let cancelled = false;
    getEnhancementModels("local")
      .then((res) => {
        const model = res.bundled?.find((m) => m.id === MODEL_ID);
        if (!cancelled && model?.disk_mb && model.disk_mb > 0) {
          setModelSize(formatModelSizeShort(model.disk_mb));
        }
      })
      .catch((err) => {
        console.warn(`[StepEnhance] model catalog failed error_type=${errorType(err)}`);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Leaving mid-run (Skip & finish, or the overlay unmounting) stops an
  // in-flight download through the shared local-download cancel. The cancel
  // endpoint is a no-op when nothing is downloading.
  const cancelRun = useCallback(() => {
    if (runningRef.current) cancelLocalLLMDownload().catch(() => undefined);
  }, []);
  useEffect(() => cancelRun, [cancelRun]);

  const skip = useCallback(() => {
    cancelRun();
    onFinish();
  }, [cancelRun, onFinish]);

  const applyVerdict = useCallback(
    (v: BenchmarkVerdict) => {
      const plan = buildBenchmarkVerdictPatch(v, config, onboardingVerdictContext(MODEL_ID));
      if (plan.deleteModelIds.length > 0) {
        // Deferred so the benchmark's llama-server releases the GGUF first.
        const ids = plan.deleteModelIds;
        window.setTimeout(() => {
          ids.forEach((id) => deleteLocalModel(id).catch(() => undefined));
        }, 400);
      }
      if (!plan.patch) return;
      const patch = plan.patch;
      // Save directly so the backend starts warming the model now (App's
      // writer is debounced), and mirror it into App state.
      saveConfig(patch).catch((err) =>
        console.error(`[StepEnhance] verdict save failed error_type=${errorType(err)}`),
      );
      onConfigChange(patch);
    },
    [config, onConfigChange],
  );

  // ---- Try-it ----
  const [exampleIndex, setExampleIndex] = useState(0);
  const [tryState, setTryState] = useState<TryState>({ kind: "ready" });
  const tryStateRef = useRef<TryState>(tryState);
  tryStateRef.current = tryState;
  const [llamaReady, setLlamaReady] = useState(false);

  useEffect(() => {
    if (subPhase !== "try") return;
    let cancelled = false;
    const unsubscribe = subscribe((event) => {
      if (event.type === "llama.status") {
        // An error state still lets the user try: the result then reports
        // that enhancement did not run, instead of waiting forever.
        setLlamaReady(event.state === "ready" || event.state === "error");
        return;
      }
      const current = tryStateRef.current.kind;
      if (event.type === "state.recording") {
        if (event.recording) setTryState({ kind: "listening" });
        else if (current === "listening") setTryState({ kind: "enhancing" });
      } else if (event.type === "dictation.state") {
        if (event.phase === "recording") setTryState({ kind: "listening" });
        else if (event.phase === "processing" && current === "listening") {
          setTryState({ kind: "enhancing" });
        } else if (
          event.phase === "idle" &&
          (current === "listening" || current === "enhancing")
        ) {
          // Finalize publishes the result before idle, so idle while still
          // waiting means nothing was transcribed.
          setTryState({ kind: "miss" });
        }
      } else if (event.type === "transcription.result") {
        const text = (event.text || "").trim();
        if (!text) {
          setTryState({ kind: "miss" });
          return;
        }
        setTryState({
          kind: "result",
          result: {
            raw: (event.raw_text ?? event.text).trim(),
            text,
            enhancement: event.enhancement ?? "off",
          },
        });
      }
    });
    getLlamaStatus()
      .then((status) => {
        if (!cancelled) setLlamaReady(status.state === "ready" || status.state === "error");
      })
      .catch((err) => {
        console.warn(`[StepEnhance] llama status failed error_type=${errorType(err)}`);
      });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [subPhase, subscribe]);

  const hasResult = tryState.kind === "result";

  // ---- Footer ----
  const onFooterChangeRef = useRef(onFooterChange);
  onFooterChangeRef.current = onFooterChange;
  useEffect(() => {
    const skipAction: FooterAction = { label: SKIP_LABEL, onClick: skip, disabled: finishing };
    let spec: FooterSpec;
    if (subPhase === "intro") {
      spec = {
        ghost: { label: SKIP_LABEL, onClick: onFinish, disabled: finishing },
        primary: {
          label: "Turn on enhancement",
          onClick: () => {
            setInlineState({ phase: "running" });
            setRunKey((k) => k + 1);
            setSubPhase("run");
          },
          disabled: finishing,
        },
      };
    } else if (subPhase === "run") {
      const s = inlineState;
      if (s.phase === "success") {
        spec = {
          ghost: skipAction,
          primary: { label: "Try it", onClick: () => setSubPhase("try"), disabled: finishing },
        };
      } else if (s.phase === "marginal") {
        spec = {
          // Skipping here equals leaving it off; the verdict is still cached.
          ghost: {
            label: SKIP_LABEL,
            onClick: () => {
              s.leaveOff();
              onFinish();
            },
            disabled: finishing,
          },
          secondary: {
            label: "Use it anyway",
            onClick: () => {
              s.useAnyway();
              setSubPhase("try");
            },
            disabled: finishing,
          },
          primary: { label: "Leave enhancement off", onClick: s.leaveOff, disabled: finishing },
        };
      } else if (s.phase === "declined" || s.phase === "failure") {
        spec = { primary: { label: "Finish", onClick: onFinish, disabled: finishing } };
      } else if (s.phase === "infrastructure_error") {
        spec = {
          ghost: skipAction,
          primary: { label: "Try again", onClick: s.retry, disabled: finishing },
        };
      } else {
        spec = {
          ghost: skipAction,
          primary: { label: "Continue", onClick: () => undefined, disabled: true },
        };
      }
    } else {
      const last = exampleIndex === examples.tryIt.length - 1;
      spec = {
        ghost: skipAction,
        primary: last
          ? { label: "Finish", onClick: onFinish, disabled: finishing || !hasResult }
          : {
              label: "Next example",
              onClick: () => {
                setExampleIndex((i) => i + 1);
                setTryState({ kind: "ready" });
              },
              disabled: finishing || !hasResult,
            },
      };
    }
    onFooterChangeRef.current(spec);
  }, [subPhase, inlineState, exampleIndex, hasResult, finishing, skip, onFinish]);

  return (
    <div
      className="rounded-[var(--radius-card)] mx-auto flex flex-col"
      style={{
        width: 560,
        minHeight: 280,
        padding: 28,
        // Same glass card recipe as StepShortcut and StepTest.
        background: "var(--surface-card)",
        backdropFilter: "blur(24px)",
        border: "1px solid var(--border-hairline)",
        borderTop: "1px solid var(--border-emphasis)",
        boxShadow: "var(--shadow-glass-sm)",
      }}
      data-enhance-phase={subPhase}
    >
      <AnimatePresence mode="wait">
        {subPhase === "intro" && (
          <motion.div key="intro" {...fade} className="flex flex-1 flex-col">
            <p className="mb-1.5 text-[10px] font-medium uppercase tracking-[0.2em] text-steel/70">
              Optional
            </p>
            <h2 className={heroClass}>Polish your dictation</h2>
            <p className="mt-1.5 text-[12px] leading-normal text-steel">
              Enhancement catches mid-sentence corrections and tidies punctuation and numbers. It runs on this computer, so your words stay private.
            </p>
            <EnhancePreviewCarousel cards={examples.intro} />
            <p className="mt-auto pt-3 text-[11px] text-steel/75">
              {modelSize
                ? `One-time download, ${modelSize}. You can change this anytime in Enhancement.`
                : "You can change this anytime in Enhancement."}
            </p>
          </motion.div>
        )}

        {subPhase === "run" && (
          <motion.div key={`run-${runKey}`} {...fade}>
            <BenchmarkFlow
              variant="inline"
              startPhase="prefilter"
              mode="setup"
              benchMode="auto"
              benchModelId={MODEL_ID}
              benchModelSize={modelSize}
              failureFallback="current"
              currentModelId={config.enhancement_local_model || MODEL_ID}
              currentCleanupEnabled={false}
              subscribe={subscribe}
              onCancel={() => setSubPhase("intro")}
              onVerdict={applyVerdict}
              onInlineStateChange={setInlineState}
            />
          </motion.div>
        )}

        {subPhase === "try" && (
          <motion.div key="try" {...fade}>
            <TryIt
              index={exampleIndex}
              state={tryState}
              warm={llamaReady}
              shortcut={config.primary_shortcut}
              onRetry={() => setTryState({ kind: "ready" })}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function TryIt({
  index,
  state,
  warm,
  shortcut,
  onRetry,
}: {
  index: number;
  state: TryState;
  warm: boolean;
  shortcut: string;
  onRetry: () => void;
}) {
  const example = examples.tryIt[index];
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, [index]);

  const result = state.kind === "result" ? state.result : null;
  const applied = result?.enhancement === "applied";
  const unchanged = applied && result.text === result.raw;

  // Split pair: the script stands alone (full width) until enhancement
  // starts, then the surface splits and the right column carries the
  // shimmer, the result or the not-enhanced note.
  let pair;
  if (result) {
    pair = (
      <EnhancePair
        className="mt-[18px]"
        spoken={{ label: "What you said", tone: "said", text: result.raw }}
        written={
          applied
            ? { variant: "result", label: "After enhancement", text: result.text }
            : {
                variant: "missing",
                label: "After enhancement",
                note: "Enhancement didn't run this time, so this is your plain transcript.",
                retryLabel: "Try again",
                onRetry,
              }
        }
      />
    );
  } else {
    pair = (
      <EnhancePair
        className="mt-[18px]"
        spoken={{ label: "Say this", tone: "script", text: example.input }}
        written={state.kind === "enhancing" ? { variant: "pending", label: "After enhancement" } : undefined}
      />
    );
  }

  const prompt = (
    <p className="text-[13px] text-steel">
      Hold <KeyChip keys={shortcut} /> and read it aloud.
    </p>
  );

  let row;
  if (unchanged) {
    row = (
      <p className="mt-2 text-[12px] text-steel" role="status" aria-live="polite">
        Nothing needed fixing this time.
      </p>
    );
  } else {
    let left = null;
    let right;
    if (result) {
      right = <RecStatus state="got_it" live />;
    } else if (state.kind === "miss") {
      right = (
        <RecStatus state="miss" live>
          <span>
            Didn't catch that. Hold <KeyChip keys={shortcut} /> and try again.
          </span>
        </RecStatus>
      );
    } else if (!warm && state.kind === "ready") {
      right = <LifeSignStatus status={`Getting ${MODEL_NAME} ready…`} />;
    } else {
      left = prompt;
      right = (
        <RecStatus
          state={state.kind === "listening" ? "listening" : state.kind === "enhancing" ? "enhancing" : "ready"}
          live
        />
      );
    }
    row = (
      <div className="mt-4 flex min-h-[26px] items-center justify-between gap-3">
        {left ?? right}
        {left ? right : null}
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <h2 ref={headingRef} tabIndex={-1} id="benchmark-heading" className={heroClass}>
          See what enhancement does
        </h2>
        <span className="whitespace-nowrap text-[11px] tabular-nums text-steel/70">
          {`Example ${index + 1} of ${examples.tryIt.length}`}
        </span>
      </div>
      {pair}
      {row}
    </div>
  );
}
