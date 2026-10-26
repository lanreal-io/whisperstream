import { useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import type { AppConfig } from "../../lib/api/config";
import type { SttArtifactId } from "../../lib/api/models";
import type { AppEvent } from "../../lib/socket/events";
import { getSttStatus } from "../../lib/api/models";
import { errorType } from "../../lib/logCapture";
import { KeyChip } from "./KeyChip";
import { DownloadCard } from "./DownloadCard";
import { MicPicker } from "./MicPicker";
import { RecStatus } from "./RecStatus";

// Onboarding step 3 — the hands-on test step.
//
// This is the only step where the user actually interacts with the
// transcription pipeline. The backend's text injector is suppressed for
// the lifetime of this step (wired in OnboardingOverlay via inject.suppress
// WS messages — Task 1 + Task 2). Transcribed text arrives via the
// `transcription.result` WS event and is written into the controlled
// textarea via React state.
//
// Locked design (design.md §Step 3):
//   - Same 560×280px panel as step 2 (visual continuity is load-bearing)
//   - AnimatePresence cross-fades between test variant and DownloadCard
//   - Readiness means /api/stt/status.loaded_artifact_id matches the exact
//     Automatic target. A generic health/model-loaded signal is insufficient.
//   - On download.complete, keep the completed progress UI visible while the
//     backend activates the target, then cross-fade only after exact readiness
//   - Recording state: Ready (grey) → Listening (frost pulsing) → Got it
//     (green, 1200ms hold)
//   - Three-state machine holds at "listening" until transcription.result
//     arrives — flashing back to "ready" between recording-stop and
//     transcription-arrival looks worse than holding

interface StepTestProps {
  config: AppConfig;
  targetArtifactId: SttArtifactId;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  send: (msg: Record<string, unknown>) => boolean;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  onHasRecordedChange: (recorded: boolean) => void;
  /**
   * Forces showDownloadCard true regardless of derived state. Used by the
   * verification screenshot pass to capture the download variant; otherwise
   * leave undefined and let mount-time logic decide.
   */
  forceShowDownloadCard?: boolean;
}

type RecState = "ready" | "listening" | "got_it";

export function StepTest({
  config,
  targetArtifactId,
  subscribe,
  send: _send,
  onConfigChange,
  onHasRecordedChange,
  forceShowDownloadCard,
}: StepTestProps) {
  const [text, setText] = useState("");
  const [recState, setRecState] = useState<RecState>("ready");
  const [downloadProgress, setDownloadProgress] = useState(0);
  const [targetReady, setTargetReady] = useState(false);
  const [downloadFinished, setDownloadFinished] = useState(false);
  const targetReadyRef = useRef(false);
  const gotItTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const completeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // One effect owns the exact-readiness probe and the event subscription so
  // activation cannot complete in a blind gap between them. Subscribe first,
  // then probe: completion before subscription is observed by the GET, while
  // completion after subscription triggers another GET from the event.
  useEffect(() => {
    let cancelled = false;
    let latestReadinessRequest = 0;
    targetReadyRef.current = false;
    setTargetReady(false);

    const refreshReadiness = async () => {
      const request = ++latestReadinessRequest;
      try {
        const status = await getSttStatus();
        if (!cancelled && request === latestReadinessRequest) {
          const ready = status.loaded_artifact_id === targetArtifactId;
          targetReadyRef.current = ready;
          setTargetReady(ready);
        }
      } catch (err) {
        if (!cancelled && request === latestReadinessRequest) {
          // Fail closed: an unavailable inventory must never make test
          // dictation look ready for an unverified recognizer.
          targetReadyRef.current = false;
          setTargetReady(false);
          console.error(`[StepTest] getSttStatus failed error_type=${errorType(err)}`);
        }
      }
    };

    const unsubscribe = subscribe((event) => {
      if (event.type === "state.recording") {
        if (targetReadyRef.current && event.recording) {
          setRecState("listening");
        }
        // When recording stops we deliberately HOLD at "listening" until
        // transcription.result lands — flashing back to "ready" in the gap
        // between recording-stop and transcription-arrival looks worse than
        // holding. transcription.result is what flips us to "got_it".
      } else if (event.type === "transcription.result") {
        if (!targetReadyRef.current) return;
        const incoming = event.text || "";
        if (incoming.trim() === "") return;
        setText((prev) => (prev ? `${prev} ${incoming}` : incoming));
        setRecState("got_it");
        if (gotItTimerRef.current) clearTimeout(gotItTimerRef.current);
        gotItTimerRef.current = setTimeout(() => {
          setRecState("ready");
          gotItTimerRef.current = null;
        }, 1200);
        onHasRecordedChange(true);
      } else if (event.type === "download.progress") {
        if (event.total > 0) {
          setDownloadProgress(event.downloaded / event.total);
        }
      } else if (event.type === "download.complete") {
        setDownloadProgress(1);
        setDownloadFinished(true);
        void refreshReadiness();
      } else if (
        event.type === "model.loaded" ||
        event.type === "model.load_failed" ||
        event.type === "download.error" ||
        event.type === "download.cancelled"
      ) {
        void refreshReadiness();
      }
    });

    void refreshReadiness();

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [subscribe, onHasRecordedChange, targetArtifactId]);

  // Preserve the settled 400ms completion beat, but start it only after the
  // authoritative status reports that the exact target is live.
  const [showDownloadCardState, setShowDownloadCardState] = useState(true);
  useEffect(() => {
    if (!targetReady) {
      setShowDownloadCardState(true);
      return;
    }
    if (completeTimerRef.current) clearTimeout(completeTimerRef.current);
    completeTimerRef.current = setTimeout(() => {
      setShowDownloadCardState(false);
      completeTimerRef.current = null;
    }, downloadFinished ? 400 : 0);
  }, [downloadFinished, targetReady]);

  useEffect(() => {
    return () => {
      if (gotItTimerRef.current) clearTimeout(gotItTimerRef.current);
      if (completeTimerRef.current) clearTimeout(completeTimerRef.current);
    };
  }, []);

  // Decide which variant to actually render this frame.
  const showDownloadCard = useMemo(() => {
    if (forceShowDownloadCard) return true;
    return showDownloadCardState;
  }, [forceShowDownloadCard, showDownloadCardState]);

  return (
    <div
      className="rounded-[var(--radius-card)] mx-auto"
      style={{
        width: 560,
        minHeight: 280,
        padding: 28,
        // Glass card pattern shared with StepShortcut — see
        // that file for the rationale on the asymmetric top border and
        // the softer drop shadow.
        background: "var(--surface-card)",
        backdropFilter: "blur(24px)",
        border: "1px solid var(--border-hairline)",
        borderTop: "1px solid var(--border-emphasis)",
        boxShadow: "var(--shadow-glass-sm)",
      }}
    >
      <AnimatePresence mode="wait">
        {showDownloadCard ? (
          <motion.div
            key="download"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
          >
            <DownloadCard
              progress={downloadProgress}
              subscribe={subscribe}
              finalizing={downloadFinished && !targetReady}
            />
          </motion.div>
        ) : (
          <motion.div
            key="test"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
          >
            <h2 className="font-display text-[24px] font-light tracking-tight text-slate leading-tight">
              Try it out
            </h2>
            <p className="mt-2 text-[12px] text-steel">
              Try a quick recording. Your words will land in the box below.
            </p>

            {/* Centered prompt line with inline KeyChip */}
            <p className="mt-5 text-center text-[13px] text-steel">
              Hold <KeyChip keys={config.push_to_talk_shortcut} /> and say something.
            </p>

            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Your transcription will appear here."
              rows={3}
              className="mt-3 w-full rounded-2xl px-4 py-3 text-[13px] text-slate leading-relaxed outline-none resize-none placeholder:text-steel/40 transition-all duration-300"
              style={{
                // Recessed input style — matches the PillInput recipe. The
                // dark inset shadow makes the field look like content lives
                // INSIDE it, not on top of it.
                background: "var(--surface-control)",
                border: "1px solid var(--border-emphasis)",
                boxShadow: "inset 0 2px 4px rgba(0, 0, 0, 0.02)",
              }}
              onFocus={(e) => {
                // Option 2: focus = global outline only; no fill change on click.
                e.currentTarget.style.outline = "";
              }}
              onBlur={(e) => {
                e.currentTarget.style.background = "var(--surface-control)";
                e.currentTarget.style.borderColor = "var(--border-emphasis)";
                e.currentTarget.style.boxShadow =
                  "inset 0 2px 4px rgba(0, 0, 0, 0.02)";
              }}
            />

            <div className="mt-3 flex items-center justify-between">
              <MicPicker
                value={config.audio_input_device}
                savedLabel={config.audio_input_device_label}
                savedAvailable={config.audio_input_device_available}
                selectionMode={config.audio_input_device_selection_mode}
                onChange={onConfigChange}
              />
              <RecStatus state={recState} />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
