import { useCallback, useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { open } from "@tauri-apps/plugin-shell";
import { PopupShell } from "../components/PopupShell";
import { PrimaryButton } from "../components/PrimaryButton";
import { SecondaryButton } from "../components/SecondaryButton";
import { dismissReview } from "../lib/api/feedback";
import { getLockdownStatus } from "../lib/api/lockdown";
import { errorType, flushLogSync } from "../lib/logCapture";
import { applyWindowLifecycle, publishPopupVisibility } from "../lib/windowLifecycle";

// Fixed destinations. Nothing is appended: no content, identity or rating
// leaves the device, and GitHub submission is the user's separate action.
export const GITHUB_STAR_URL = "https://github.com/lanreal-io/whisperstream";
export const GITHUB_ISSUE_URL = "https://github.com/lanreal-io/whisperstream/issues/new/choose";

export type ReviewStep = "question" | "star" | "issue";
export type ReviewNotice = "error" | "lockdown";
export type ReviewDismissReason = "later" | "github";

const NOTICE_COPY: Record<ReviewNotice, string> = {
  error: "Couldn't open your browser. Try again, or use the link in About.",
  lockdown: "Unavailable in Lockdown Mode.",
};

const stepTransition = { duration: 0.22, ease: [0.2, 0, 0, 1] as const };

// Sync this popup's log buffer to the backend before hiding so console
// output is reachable in the log export after the window is gone.
async function hideAndFlush() {
  try {
    await flushLogSync();
  } catch {
    /* ignore */
  }
  if (!(await applyWindowLifecycle("review", "hide"))) {
    await getCurrentWindow().hide();
    await publishPopupVisibility("popup-review", false);
  }
}

function StarGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true" className="flex-none">
      <path
        d="M8 1.6l1.9 3.85 4.25.62-3.08 3 .73 4.23L8 11.3l-3.8 2 .73-4.23-3.08-3 4.25-.62L8 1.6z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

const titleClass =
  "font-display text-[18px] font-normal leading-[1.3] text-slate text-center [text-wrap:balance]";

interface ReviewContentProps {
  onDismiss: (reason: ReviewDismissReason) => void;
  initialStep?: ReviewStep;
  /** Dev QA only: paints a notice without a failed open or Lockdown policy. */
  initialNotice?: ReviewNotice | null;
}

export function ReviewContent({ onDismiss, initialStep = "question", initialNotice = null }: ReviewContentProps) {
  const [step, setStep] = useState<ReviewStep>(initialStep);
  const [notice, setNotice] = useState<ReviewNotice | null>(initialNotice);
  const [opening, setOpening] = useState(false);
  const [entered, setEntered] = useState(false);
  const dismissed = useRef(false);
  const openingRef = useRef(false);
  const ctaRef = useRef<HTMLDivElement>(null);
  const refocusCta = useRef(false);
  const reduceMotion = useReducedMotion();

  const dismissOnce = useCallback((reason: ReviewDismissReason) => {
    if (dismissed.current) return;
    dismissed.current = true;
    onDismiss(reason);
  }, [onDismiss]);

  const snooze = useCallback(() => {
    if (openingRef.current) return;
    dismissOnce("later");
  }, [dismissOnce]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      snooze();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [snooze]);

  // Move focus onto the CTA that replaced Yes / Not really so keyboard users
  // keep their place; the first paint leaves focus to the window.
  useEffect(() => {
    if (entered) ctaRef.current?.querySelector("button")?.focus({ preventScroll: true });
  }, [entered, step]);

  // After a failed or blocked open, return focus once the CTA is re-enabled.
  useEffect(() => {
    if (opening || !refocusCta.current) return;
    refocusCta.current = false;
    ctaRef.current?.querySelector("button")?.focus({ preventScroll: true });
  }, [opening, notice]);

  const choose = (next: ReviewStep) => {
    setNotice(null);
    setEntered(true);
    setStep(next);
  };

  const openGitHub = useCallback(async (target: "star" | "issue") => {
    if (openingRef.current || dismissed.current) return;
    openingRef.current = true;
    setOpening(true);
    setNotice(null);
    let outcome: "opened" | ReviewNotice = "error";
    try {
      // Fresh policy read before every handoff; unknown or incomplete blocks.
      let allowed = false;
      try {
        const status = await getLockdownStatus();
        allowed = status.enabled === false && status.managed === false;
      } catch {
        allowed = false;
      }
      if (!allowed) {
        outcome = "lockdown";
      } else {
        await open(target === "star" ? GITHUB_STAR_URL : GITHUB_ISSUE_URL);
        outcome = "opened";
      }
    } catch (err) {
      console.warn(`[ReviewPopup] open failed target=${target} error_type=${errorType(err)}`);
      outcome = "error";
    }
    openingRef.current = false;
    setOpening(false);
    if (outcome === "opened") {
      console.log(`[ReviewPopup] github opened target=${target}`);
      dismissOnce("github");
      return;
    }
    if (outcome === "lockdown") console.log(`[ReviewPopup] open blocked target=${target} reason=lockdown`);
    refocusCta.current = true;
    setNotice(outcome);
  }, [dismissOnce]);

  const enter = entered && !reduceMotion ? { opacity: 0, y: 6 } : false;

  return (
    <div className="relative">
      <button
        type="button"
        onClick={snooze}
        aria-label="Close"
        className="absolute -top-[15px] -right-[15px] z-10 w-6 h-6 rounded-full flex items-center justify-center transition-colors duration-200 cursor-pointer text-steel/55 hover:text-slate"
        style={{ border: "none", background: "transparent" }}
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
          <path d="M3 3L9 9M9 3L3 9" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" />
        </svg>
      </button>

      <motion.div
        key={step}
        initial={enter}
        animate={{ opacity: 1, y: 0 }}
        transition={stepTransition}
        className="flex flex-col items-center py-2 text-center"
        data-review-step={step}
      >
        {step === "question" ? (
          <>
            <h2 id="review-title" className={titleClass}>Enjoying Whisperstream?</h2>
            <div className="mt-5 w-full flex flex-col gap-2" role="group" aria-labelledby="review-title">
              <PrimaryButton fullWidth className="h-10 text-[13px]" onClick={() => choose("star")}>
                Yes
              </PrimaryButton>
              <SecondaryButton
                className="w-full h-10 rounded-full text-[13px] font-medium text-slate"
                onClick={() => choose("issue")}
              >
                Not really
              </SecondaryButton>
            </div>
          </>
        ) : (
          <>
            <h2 id="review-title" className={titleClass}>
              {step === "star" ? "Glad you like it" : "Sorry to hear that"}
            </h2>
            <div ref={ctaRef} className="mt-5 w-full">
              <PrimaryButton
                fullWidth
                loading={opening}
                aria-busy={opening}
                aria-label={step === "star"
                  ? "Star Whisperstream on GitHub (opens your browser)"
                  : "Open an issue on GitHub (opens your browser)"}
                className="h-10 text-[13px] inline-flex items-center justify-center gap-2"
                onClick={() => void openGitHub(step)}
              >
                {opening ? (
                  <span>Opening…</span>
                ) : step === "star" ? (
                  <>
                    <StarGlyph />
                    <span>Star on GitHub</span>
                  </>
                ) : (
                  <span>Open an issue</span>
                )}
              </PrimaryButton>
            </div>
            <div className="mt-2 w-full min-h-10 flex items-center justify-center">
              {notice ? (
                <p role="alert" className="m-0 max-w-[300px] text-[11px] leading-[1.4] text-rose-text [text-wrap:balance]">
                  {NOTICE_COPY[notice]}
                </p>
              ) : (
                <p className="m-0 max-w-[300px] text-[12px] leading-[1.5] text-steel [text-wrap:pretty]">
                  {step === "star"
                    ? "A star on GitHub helps more people find Whisperstream."
                    : "Tell us what's getting in your way with an issue on GitHub."}
                </p>
              )}
            </div>
          </>
        )}
      </motion.div>
    </div>
  );
}

const STEPS: ReviewStep[] = ["question", "star", "issue"];

export function ReviewPopup() {
  const [mountKey, setMountKey] = useState(0);
  const [initialStep, setInitialStep] = useState<ReviewStep>("question");
  // Dev QA may emit an init payload before `:show`; production shows always
  // start at the question.
  const pendingStep = useRef<ReviewStep | null>(null);

  useEffect(() => {
    const unlistenInit = listen<{ initialStep?: string }>("popup-review:init", (event) => {
      const requested = event.payload?.initialStep;
      pendingStep.current = STEPS.includes(requested as ReviewStep) ? requested as ReviewStep : null;
    });
    const unlistenShow = listen("popup-review:show", () => {
      const step = pendingStep.current ?? "question";
      pendingStep.current = null;
      console.log(`[ReviewPopup] show received initial_step=${step}`);
      setInitialStep(step);
      setMountKey((key) => key + 1);
    });
    return () => {
      unlistenInit.then((fn) => fn());
      unlistenShow.then((fn) => fn());
    };
  }, []);

  const handleDismiss = useCallback(async (reason: ReviewDismissReason) => {
    await dismissReview(reason).catch(() => {});
    await hideAndFlush();
  }, []);

  return (
    <PopupShell width={384}>
      <ReviewContent key={mountKey} initialStep={initialStep} onDismiss={(reason) => void handleDismiss(reason)} />
    </PopupShell>
  );
}
