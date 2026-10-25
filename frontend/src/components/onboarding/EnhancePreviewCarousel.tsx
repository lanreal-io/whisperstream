import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useReducedMotion } from "framer-motion";
import { EnhancePair, type SpokenSegment } from "./EnhancePair";

// The Enhance intro's three canned examples (correction, list, email) as a
// small carousel (round 01 design.md "Preview carousel",
// prototype-preview-carousel.html with the Split pair).
//
// - Autoplay dwells 5s / 6.5s / 8s; the current dot fills as progress. The
//   fill's CSS animation is the clock: its animationend advances the slide,
//   so pausing the animation pauses the carousel exactly.
// - Hover or focus inside, or a hidden window, pauses. Any arrow, dot or
//   arrow-key navigation stops autoplay for good; Play restarts it.
// - Reduced motion: no autoplay, no fill, no crossfade, no pause button.
// - Every slide shares one grid cell, so the stage always reserves the
//   tallest card and nothing below it moves.

export interface IntroCard {
  id: string;
  title: string;
  input: string;
  dropped?: string;
  expected: string;
  email?: boolean;
}

const DWELL_MS = [5000, 6500, 8000];
const CUE_DESCRIPTION = "This happens only when you dictate into an email app or website.";
const EASE = "cubic-bezier(0.2, 0, 0, 1)";

export function segmentsFor(card: IntroCard): SpokenSegment[] {
  const { input, dropped } = card;
  const at = dropped ? input.indexOf(dropped) : -1;
  if (!dropped || at < 0) return [{ text: input }];
  return [
    { text: input.slice(0, at) },
    { text: dropped, dropped: true },
    { text: input.slice(at + dropped.length) },
  ];
}

export function EnhancePreviewCarousel({ cards }: { cards: IntroCard[] }) {
  const reduce = useReducedMotion() ?? false;
  const uid = useId();
  const cueId = `${uid}-cue`;
  const [index, setIndex] = useState(0);
  // Bumped to restart the dot fill (slide change, Play, unpause).
  const [run, setRun] = useState(0);
  const [stopped, setStopped] = useState(false);
  const [userPaused, setUserPaused] = useState(false);
  const [hover, setHover] = useState(false);
  const [focusWithin, setFocusWithin] = useState(false);
  const [hidden, setHidden] = useState(() => typeof document !== "undefined" && document.hidden);
  const rootRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const onVisibility = () => setHidden(document.hidden);
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  const count = cards.length;
  const autoplay = !reduce && !stopped;
  const playing = autoplay && !userPaused;
  const held = hover || focusWithin || hidden;

  const show = useCallback(
    (n: number) => {
      setIndex(((n % count) + count) % count);
      setRun((r) => r + 1);
    },
    [count],
  );
  const navigate = useCallback(
    (n: number) => {
      setStopped(true);
      show(n);
    },
    [show],
  );

  const togglePlay = () => {
    if (stopped || userPaused) {
      setStopped(false);
      setUserPaused(false);
    } else {
      setUserPaused(true);
    }
    setRun((r) => r + 1);
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    navigate(index + (e.key === "ArrowRight" ? 1 : -1));
  };

  // A native listener: React maps onAnimationEnd to a vendor-prefixed event
  // where AnimationEvent is missing (jsdom), and the fill is the clock.
  const fillRef = useRef<HTMLElement>(null);
  const advance = useRef<() => void>(() => {});
  advance.current = () => {
    if (playing) show(index + 1);
  };
  useEffect(() => {
    const el = fillRef.current;
    if (!el) return;
    const onEnd = (e: AnimationEvent) => {
      if (e.animationName === "enhance-dwell" || !e.animationName) advance.current();
    };
    el.addEventListener("animationend", onEnd);
    return () => el.removeEventListener("animationend", onEnd);
  }, [index, run]);

  const paused = userPaused || stopped || reduce;
  const fade = reduce ? "none" : `opacity 0.22s ${EASE}`;

  return (
    <section
      ref={rootRef}
      role="region"
      aria-roledescription="carousel"
      aria-label="Enhancement examples"
      className="mt-2.5"
      onPointerEnter={() => setHover(true)}
      onPointerLeave={() => setHover(false)}
      onFocus={() => setFocusWithin(true)}
      onBlur={(e) => {
        if (!rootRef.current?.contains(e.relatedTarget as Node | null)) setFocusWithin(false);
      }}
      data-carousel-index={index}
      data-carousel-playing={playing || undefined}
    >
      <span id={cueId} className="sr-only">{CUE_DESCRIPTION}</span>
      <div className="flex min-h-6 items-center justify-between gap-3">
        <div className="grid min-w-0 flex-1" aria-hidden="true">
          {cards.map((card, i) => (
            <div
              key={card.id}
              className="truncate text-[13px] font-medium text-slate"
              style={{
                gridArea: "1 / 1",
                opacity: i === index ? 1 : 0,
                visibility: i === index ? "visible" : "hidden",
                transition: fade,
              }}
            >
              {card.title}
            </div>
          ))}
        </div>
        <div className="flex shrink-0 items-center gap-0.5" onKeyDown={onKeyDown}>
          {!reduce && (
            <CarouselButton label={paused ? "Play examples" : "Pause examples"} onClick={togglePlay}>
              {paused ? <PlayIcon /> : <PauseIcon />}
            </CarouselButton>
          )}
          <CarouselButton label="Previous example" onClick={() => navigate(index - 1)}>
            <ChevronLeft />
          </CarouselButton>
          <div className="flex items-center gap-1 px-1">
            {cards.map((card, i) => {
              const current = i === index;
              return (
                <button
                  key={card.id}
                  type="button"
                  aria-label={`Show ${card.title}`}
                  aria-current={current ? "true" : undefined}
                  onClick={() => navigate(i)}
                  className="relative h-1.5 cursor-pointer overflow-hidden rounded-full before:absolute before:-inset-x-1 before:-inset-y-[9px] before:content-['']"
                  style={{
                    width: current ? 20 : 6,
                    background: current
                      ? "color-mix(in srgb, var(--color-frost) 30%, transparent)"
                      : "color-mix(in srgb, var(--color-steel) 35%, transparent)",
                    transition: reduce ? "none" : `width 0.22s ${EASE}, background 0.2s`,
                  }}
                  data-dot={i}
                >
                  {current && (
                    <i
                      key={run}
                      aria-hidden="true"
                      className="absolute inset-0 rounded-[inherit]"
                      style={{
                        background: "var(--accent-fill)",
                        transformOrigin: "left",
                        animation: playing
                          ? `enhance-dwell ${DWELL_MS[i] ?? DWELL_MS[DWELL_MS.length - 1]}ms linear forwards`
                          : "none",
                        animationPlayState: held ? "paused" : "running",
                      }}
                      ref={fillRef}
                      data-dwell-fill
                    />
                  )}
                </button>
              );
            })}
          </div>
          <CarouselButton label="Next example" onClick={() => navigate(index + 1)}>
            <ChevronRight />
          </CarouselButton>
        </div>
      </div>
      <div className="mt-2 grid" aria-live="off">
        {cards.map((card, i) => {
          const current = i === index;
          return (
            <div
              key={card.id}
              role="group"
              aria-roledescription="slide"
              aria-label={`${i + 1} of ${count}: ${card.title}`}
              aria-describedby={card.email ? cueId : undefined}
              aria-hidden={current ? undefined : true}
              className="self-start"
              style={{
                gridArea: "1 / 1",
                opacity: current ? 1 : 0,
                visibility: current ? "visible" : "hidden",
                transition: reduce
                  ? "none"
                  : current
                    ? fade
                    : `opacity 0.22s ${EASE}, visibility 0s 0.22s`,
              }}
              data-slide={card.id}
            >
              <EnhancePair
                size="intro"
                email={card.email}
                spoken={{ label: "You say", tone: "example", segments: segmentsFor(card) }}
                written={{ variant: "expected", label: "You get", text: card.expected }}
              />
            </div>
          );
        })}
      </div>
    </section>
  );
}

function CarouselButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      className="grid h-6 w-6 cursor-pointer place-items-center rounded-full text-steel/70 transition-colors duration-200 hover:bg-[var(--surface-hover)] hover:text-slate"
    >
      {children}
    </button>
  );
}

const iconProps = { width: 10, height: 10, viewBox: "0 0 10 10", "aria-hidden": true } as const;

function ChevronLeft() {
  return (
    <svg {...iconProps} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6.2 2 3.2 5l3 3" />
    </svg>
  );
}

function ChevronRight() {
  return (
    <svg {...iconProps} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3.8 2l3 3-3 3" />
    </svg>
  );
}

function PauseIcon() {
  return (
    <svg {...iconProps} fill="currentColor">
      <rect x="2.4" y="2" width="1.8" height="6" rx=".6" />
      <rect x="5.8" y="2" width="1.8" height="6" rx=".6" />
    </svg>
  );
}

function PlayIcon() {
  return (
    <svg {...iconProps} fill="currentColor">
      <path d="M3 2.2v5.6a.4.4 0 0 0 .6.35l4.6-2.8a.4.4 0 0 0 0-.7L3.6 1.85a.4.4 0 0 0-.6.35z" />
    </svg>
  );
}
