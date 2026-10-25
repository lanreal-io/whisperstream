import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { WHATS_NEW_LABEL, WHATS_NEW_STEPS } from "../lib/whatsNew";
import type { WhatsNewHero } from "../lib/whatsNew";
import { CohereTranscribeHero } from "./spotlight/heroes/CohereTranscribeHero";
import { DictionaryBoostHero } from "./spotlight/heroes/DictionaryBoostHero";
import { Stream1Hero } from "./spotlight/heroes/Stream1Hero";

/* ---------------------------------------------------------------------------
   WhatsNewTour: the one-time, non-skippable "What's new in Whisperstream 2.0"
   tour. A sibling of FeatureSpotlight: the family-A frost-glass shell, hero
   band and pill styles are lifted verbatim (dialog shells stay separate by
   design; no shared base). The tour label and dot indicator form a persistent
   frame in the hero light; only the hero and step content change.

   No dismiss paths: no close control, the backdrop is inert and Escape is
   swallowed. The only exit is "Start dictating" on the final step, which fires
   `onComplete`; the caller persists the ID. Owns no app state.
--------------------------------------------------------------------------- */

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
  boxShadow: "var(--shadow-pearl)",
};

const HEROES: Record<WhatsNewHero, () => ReactNode> = {
  cohere: () => <CohereTranscribeHero />,
  boost: () => <DictionaryBoostHero />,
  stream1: () => <Stream1Hero />,
};

/** Breathing frost bloom behind the hero (FeatureSpotlight's). Static under reduced motion. */
function FeatureBloom({ reduce }: { reduce: boolean }) {
  return (
    <motion.div
      aria-hidden="true"
      className="absolute inset-x-[-10%] top-[-20%] h-[200%] pointer-events-none"
      style={{
        background:
          "radial-gradient(60% 70% at 50% 55%, color-mix(in srgb, var(--color-frost) 42%, transparent), transparent 72%)",
        filter: "blur(4px)",
      }}
      initial={false}
      animate={reduce ? { opacity: 0.7 } : { opacity: [0.62, 1, 0.62], scale: [0.97, 1.03, 0.97] }}
      transition={reduce ? undefined : { duration: 4.2, repeat: Infinity, ease: "easeInOut" }}
    />
  );
}

/** Three-dot step indicator. Decorative; the sr-only text carries the count. */
function TourDots({ current, total, reduce }: { current: number; total: number; reduce: boolean }) {
  return (
    <>
      <div aria-hidden="true" className="flex h-2 items-center gap-2">
        {Array.from({ length: total }, (_, i) => {
          const n = i + 1;
          const active = n === current;
          const complete = n < current;
          return (
            <span
              key={n}
              data-state={active ? "active" : complete ? "complete" : "upcoming"}
              className="h-1.5 w-1.5 rounded-full"
              style={{
                background: active
                  ? "var(--accent-fill)"
                  : complete
                    ? "color-mix(in srgb, var(--color-frost-deep) 55%, transparent)"
                    : "color-mix(in srgb, var(--color-steel) 40%, transparent)",
                boxShadow: active ? "var(--shadow-frost-glow)" : "none",
                transform: active ? "scale(1.3)" : "none",
                transition: reduce ? "none" : "background 240ms ease, box-shadow 240ms ease, transform 240ms ease",
              }}
            />
          );
        })}
      </div>
      <span className="sr-only">
        Step {current} of {total}
      </span>
    </>
  );
}

export interface WhatsNewTourProps {
  /** Fired only by "Start dictating" on the final step; caller persists WHATS_NEW_ID. */
  onComplete: () => void;
  /** 1-based starting step, for QA states. Clamped to the step range. */
  initialStep?: number;
}

export function WhatsNewTour({ onComplete, initialStep = 1 }: WhatsNewTourProps) {
  const total = WHATS_NEW_STEPS.length;
  const [index, setIndex] = useState(() => Math.min(Math.max(Math.round(initialStep) - 1, 0), total - 1));
  const [dir, setDir] = useState<1 | -1>(1);
  const [announcement, setAnnouncement] = useState("");
  const primaryRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [contentHeight, setContentHeight] = useState<number | null>(null);
  const reduce = useReducedMotion() ?? false;
  const step = WHATS_NEW_STEPS[index];
  const last = index === total - 1;

  const go = (delta: 1 | -1) => {
    const next = Math.min(Math.max(index + delta, 0), total - 1);
    if (next === index) return;
    setDir(delta);
    setIndex(next);
    setAnnouncement(`Step ${next + 1} of ${total}: ${WHATS_NEW_STEPS[next].feature}`);
  };

  // Non-skippable: Escape is swallowed. Enter is not bound. Tab is trapped over
  // the visible buttons (Back is unmounted on step 1).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        return;
      }
      if (e.key !== "Tab") return;
      const buttons = dialogRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
      if (!buttons?.length) return;
      const first = buttons[0];
      const lastButton = buttons[buttons.length - 1];
      const inside = dialogRef.current?.contains(document.activeElement) ?? false;
      if (e.shiftKey && (document.activeElement === first || !inside)) {
        e.preventDefault();
        lastButton.focus();
      } else if (!e.shiftKey && (document.activeElement === lastButton || !inside)) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Focus the primary action on mount and on every step change.
  useEffect(() => {
    primaryRef.current?.focus();
  }, [index]);

  // Capture before mount focus and return it on unmount.
  const previousFocus = useRef<HTMLElement | null>(
    typeof document !== "undefined" && document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );
  useEffect(
    () => () => {
      if (previousFocus.current?.isConnected) previousFocus.current.focus();
    },
    [],
  );

  // Track the step content's natural height so the card eases between steps
  // instead of snapping. Reduced motion skips the height animation.
  useLayoutEffect(() => {
    const el = contentRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => setContentHeight(el.offsetHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const slide = reduce
    ? {
        initial: { opacity: 0 },
        animate: { opacity: 1, transition: { duration: 0.12 } },
        exit: { opacity: 0, transition: { duration: 0.09 } },
      }
    : {
        initial: { opacity: 0, x: 12 * dir },
        animate: { opacity: 1, x: 0, transition: { duration: 0.24, ease: [0.2, 0, 0, 1] as const } },
        exit: { opacity: 0, x: -10 * dir, transition: { duration: 0.14, ease: "easeIn" as const } },
      };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop: intentionally inert (non-skippable). */}
      <motion.div
        data-whats-new-backdrop=""
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
      />

      <motion.div
        ref={dialogRef}
        initial={reduce ? { opacity: 0 } : { scale: 0.92, opacity: 0, y: 12 }}
        animate={reduce ? { opacity: 1 } : { scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[460px] max-w-[calc(100vw-40px)] overflow-hidden rounded-[var(--radius-card)]"
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="whats-new-label"
        aria-describedby="whats-new-headline whats-new-body"
      >
        {/* Hero band with the persistent frame (tour label + dots) in its light. */}
        <div className="relative overflow-hidden rounded-t-[var(--radius-card)] pt-[18px]">
          <div
            aria-hidden="true"
            className="absolute inset-0"
            style={{
              background:
                "radial-gradient(120% 120% at 50% 12%, color-mix(in srgb, var(--color-frost-light) 45%, transparent), transparent 70%)",
            }}
          />
          <FeatureBloom reduce={reduce} />
          <div className="relative z-10 flex flex-col items-center gap-2.5">
            <p id="whats-new-label" className="m-0 font-display text-[12px] font-medium tracking-[0.04em] text-steel">
              {WHATS_NEW_LABEL}
            </p>
            <TourDots current={index + 1} total={total} reduce={reduce} />
          </div>
          <div className="relative z-10 flex h-[132px] items-center justify-center" aria-hidden="true">
            <AnimatePresence mode="wait" initial={false}>
              <motion.div key={index} {...slide}>
                {HEROES[step.hero]()}
              </motion.div>
            </AnimatePresence>
          </div>
          <div
            aria-hidden="true"
            className="absolute inset-x-0 bottom-0 h-px"
            style={{
              background:
                "linear-gradient(90deg, transparent, var(--border-emphasis) 20%, var(--border-emphasis) 80%, transparent)",
            }}
          />
        </div>

        {/* Content */}
        <div className="px-7 pb-7 pt-6 max-[420px]:px-[22px] max-[420px]:pb-[22px] max-[420px]:pt-5">
          <motion.div
            className="overflow-hidden"
            initial={false}
            animate={contentHeight == null || reduce ? undefined : { height: contentHeight }}
            transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
          >
            <div ref={contentRef}>
              <AnimatePresence mode="wait" initial={false}>
                <motion.div key={index} {...slide}>
                  <span className="mb-2 inline-block font-display text-[10px] font-semibold uppercase tracking-[0.12em] text-frost-deep">
                    {step.eyebrow}
                  </span>
                  <h2
                    id="whats-new-headline"
                    className="mb-2 font-display text-[23px] font-light leading-tight text-slate max-[420px]:text-[21px]"
                    style={{ textWrap: "balance" } as React.CSSProperties}
                  >
                    {step.headline}
                  </h2>
                  <p id="whats-new-body" className="m-0 max-w-[40ch] text-[13.5px] leading-relaxed text-steel">
                    {step.body}
                  </p>
                  <p className="mb-0 mt-2.5 flex items-center gap-[7px] text-[12.5px] text-slate">
                    <span aria-hidden="true" className="h-[5px] w-[5px] flex-none rounded-full bg-frost-deep" />
                    {step.hint}
                  </p>
                </motion.div>
              </AnimatePresence>
            </div>
          </motion.div>

          {/* primary pill LAST in DOM -> right side; row right-aligned (app convention) */}
          <div className="mt-[22px] flex items-center justify-end gap-2.5">
            {index > 0 && (
              <button
                type="button"
                onClick={() => go(-1)}
                className="cursor-pointer rounded-full px-5 py-2 text-[13px] font-medium text-slate transition-all duration-200"
                style={glassPillStyle}
              >
                Back
              </button>
            )}
            <button
              ref={primaryRef}
              type="button"
              onClick={() => (last ? onComplete() : go(1))}
              className="min-w-[84px] cursor-pointer rounded-full px-5 py-2 text-[13px] font-medium text-white transition-all duration-200"
              style={gradientPillStyle}
            >
              {last ? "Start dictating" : "Next"}
            </button>
          </div>
        </div>
        <div className="sr-only" aria-live="polite">
          {announcement}
        </div>
      </motion.div>
    </div>
  );
}
