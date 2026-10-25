import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useReducedMotion } from "framer-motion";
import { SecondaryButton } from "./SecondaryButton";

// ---------------------------------------------------------------------------
// WaveformPlayer — the VoiceInk-style detail player. The waveform IS the
// scrubber (LOCKED, no separate slider): pointer-down/drag seeks, keyboard
// Left/Right nudges +-5s. Distinct from VoiceBars (a live presence indicator).
// Audio is fed by the Range-aware backend route so WebView2 can seek.
//
// Layout (round 01): two stacked rows inside the frost card — the waveform on
// its own full-width row (the scrubber), then a control bar beneath it
// (play/pause + time on the left; speed + save on the right). The waveform is
// resampled to a measured bar count via a ResizeObserver so it fits its
// container exactly at any width (no overflow, no 1px smear).
// ---------------------------------------------------------------------------

interface WaveformPlayerProps {
  /** Normalized waveform peaks (0..1), ~150-200 values. May be null/empty for
   *  records whose audio has no precomputed peaks; we render a flat strip. */
  peaks: number[] | null;
  /** transcriptAudioUrl(id) — the <audio> element fetches this directly. */
  audioUrl: string;
  /** Total clip length in ms (plaintext metadata; used for bar/seek math). */
  durationMs: number;
  /** Opens a native Save dialog and writes a decrypted, playable WAV. When
   *  omitted, the save control is not rendered. The page owns the Tauri dialog
   *  + decrypt round-trip; the player stays presentational. */
  onSave?: () => void | Promise<void>;
}

const SEEK_NUDGE_S = 5;
const PLAY_RATES = [1, 1.5, 2] as const;
type PlayRate = (typeof PLAY_RATES)[number];

// Bar geometry (kept from the original fine-grained look): a 2px bar in a 4px
// slot (2px bar + 2px gap). The strip holds exactly as many bars as fit its
// measured width, so it never overflows.
const BAR_W = 2;
const GAP = 2;
const MIN_BARS = 24;

/** "M:SS" tabular time. */
function fmt(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** Max-pool downsample of normalized 0..1 peaks to `target` buckets. Never
 *  upsamples (returns src unchanged when target >= src.length). Max-per-bucket
 *  preserves visible transients better than averaging, matching compute_peaks. */
function resamplePeaks(src: number[], target: number): number[] {
  if (target >= src.length) return src;
  const out = new Array<number>(target);
  for (let i = 0; i < target; i++) {
    const start = Math.floor((i * src.length) / target);
    const end = Math.max(start + 1, Math.floor(((i + 1) * src.length) / target));
    let m = 0;
    for (let j = start; j < end; j++) if (src[j] > m) m = src[j];
    out[i] = m;
  }
  return out;
}

function PlayIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M4.5 3.2v9.6c0 .6.66.96 1.17.63l7.2-4.8a.75.75 0 000-1.26l-7.2-4.8A.75.75 0 004.5 3.2z" />
    </svg>
  );
}

function PauseIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="currentColor"
      aria-hidden="true"
    >
      <rect x="4" y="3" width="3" height="10" rx="1" />
      <rect x="9" y="3" width="3" height="10" rx="1" />
    </svg>
  );
}

// Download-into-tray glyph for the "Save a copy" export control (arrow into a
// tray — a save-to-file-location idiom, not an internet download).
function SaveIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M8 2v7M5 6.5L8 9.5l3-3" />
      <path d="M3 10.5v2a1 1 0 001 1h8a1 1 0 001-1v-2" />
    </svg>
  );
}

// Small inline spinner (a sweeping arc) shown on the save button while the
// native Save dialog / decrypt round-trip is in flight.
function SaveSpinner() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      aria-hidden="true"
      className="animate-spin"
    >
      <path d="M7 1.5a5.5 5.5 0 015.5 5.5" />
    </svg>
  );
}

export function WaveformPlayer({ peaks, audioUrl, durationMs, onSave }: WaveformPlayerProps) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const stripRef = useRef<HTMLDivElement>(null);
  const reducedMotion = useReducedMotion();

  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState<PlayRate>(1);
  const [dragging, setDragging] = useState(false);
  const [stripWidth, setStripWidth] = useState(0);
  const [saving, setSaving] = useState(false);

  // Prefer the element's own duration once metadata loads (the durationMs prop
  // is the persisted clip length and is the authoritative fallback before load).
  const [audioDuration, setAudioDuration] = useState(durationMs / 1000);
  const totalSeconds = audioDuration > 0 ? audioDuration : durationMs / 1000;

  // Keep the <audio> element's playbackRate in sync with the cycle button.
  useEffect(() => {
    const el = audioRef.current;
    if (el) el.playbackRate = rate;
  }, [rate]);

  // Measure the strip; recompute the bar count on any resize so the waveform
  // always fills its row exactly (the I4 fit mechanism).
  useLayoutEffect(() => {
    const el = stripRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0;
      if (w > 0) setStripWidth(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Wire <audio> events to local state.
  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    const onTime = () => setCurrentTime(el.currentTime);
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onEnded = () => {
      setPlaying(false);
      setCurrentTime(0);
    };
    const onMeta = () => {
      if (Number.isFinite(el.duration) && el.duration > 0) {
        setAudioDuration(el.duration);
      }
    };
    el.addEventListener("timeupdate", onTime);
    el.addEventListener("play", onPlay);
    el.addEventListener("pause", onPause);
    el.addEventListener("ended", onEnded);
    el.addEventListener("loadedmetadata", onMeta);
    return () => {
      el.removeEventListener("timeupdate", onTime);
      el.removeEventListener("play", onPlay);
      el.removeEventListener("pause", onPause);
      el.removeEventListener("ended", onEnded);
      el.removeEventListener("loadedmetadata", onMeta);
    };
  }, []);

  const playedFraction = totalSeconds > 0 ? currentTime / totalSeconds : 0;

  const seekToFraction = (frac: number) => {
    const el = audioRef.current;
    if (!el) return;
    const clamped = Math.min(1, Math.max(0, frac));
    const target = clamped * totalSeconds;
    el.currentTime = target;
    setCurrentTime(target); // immediate visual feedback while dragging
  };

  const seekFromPointer = (clientX: number, stripEl: HTMLElement) => {
    const rect = stripEl.getBoundingClientRect();
    if (rect.width <= 0) return;
    seekToFraction((clientX - rect.left) / rect.width);
  };

  // Pointer-down anywhere on the strip seeks; drag scrubs continuously;
  // pointer-up commits. Pointer capture keeps the drag alive past the bars.
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const strip = stripRef.current;
    if (!strip) return;
    strip.setPointerCapture?.(e.pointerId);
    setDragging(true);
    seekFromPointer(e.clientX, strip);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging) return;
    const strip = stripRef.current;
    if (!strip) return;
    seekFromPointer(e.clientX, strip);
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging) return;
    const strip = stripRef.current;
    strip?.releasePointerCapture?.(e.pointerId);
    setDragging(false);
  };

  // Keyboard: Left/Right nudge +-5s when the strip is focused.
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      seekToFraction((currentTime - SEEK_NUDGE_S) / (totalSeconds || 1));
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      seekToFraction((currentTime + SEEK_NUDGE_S) / (totalSeconds || 1));
    }
  };

  const togglePlay = () => {
    const el = audioRef.current;
    if (!el) return;
    if (el.paused) {
      el.play().catch(() => {
        /* autoplay/permission edge — leave the button in the paused state */
      });
    } else {
      el.pause();
    }
  };

  const cycleRate = () => {
    setRate((prev) => {
      const idx = PLAY_RATES.indexOf(prev);
      return PLAY_RATES[(idx + 1) % PLAY_RATES.length];
    });
  };

  const handleSave = async () => {
    if (!onSave || saving) return;
    setSaving(true);
    try {
      await onSave();
    } finally {
      setSaving(false);
    }
  };

  // Graceful fallback for a null/empty peaks array: a flat minimal strip so the
  // scrubber stays usable (audio records normally carry peaks).
  const sourceBars = peaks && peaks.length > 0 ? peaks : new Array(MIN_BARS).fill(0);
  // Fit-to-width: render exactly as many bars as the measured strip can hold,
  // downsampling the source peaks to that count. Before the first measure, fall
  // back to the source length (corrected on the layout effect's first pass).
  const targetBars =
    stripWidth > 0
      ? Math.min(
          sourceBars.length,
          Math.max(MIN_BARS, Math.floor((stripWidth + GAP) / (BAR_W + GAP))),
        )
      : sourceBars.length;
  const bars = resamplePeaks(sourceBars, targetBars);

  return (
    <div
      className="rounded-3xl p-4 flex flex-col gap-3"
      style={{
        // Frost-tinted "control surface" so the player reads as a distinct
        // thing from the neutral-white transcript reading well above it (they
        // used to be the same white frost panel and blurred together). The
        // frost wash ties it to the player's own accent (the bars + play btn).
        background: "color-mix(in srgb, var(--color-frost) 10%, transparent)",
        border: "1px solid var(--border-accent)",
        boxShadow: "var(--shadow-glass-sm)",
      }}
    >
      <audio ref={audioRef} src={audioUrl} preload="metadata" />

      {/* Row 1: waveform = scrubber, full width (LOCKED contract unchanged) */}
      <div
        ref={stripRef}
        role="slider"
        aria-label="Seek"
        aria-valuemin={0}
        aria-valuemax={Math.round(totalSeconds)}
        aria-valuenow={Math.round(currentTime)}
        tabIndex={0}
        className="w-full h-10 flex items-end gap-[2px] overflow-hidden cursor-pointer select-none touch-none"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onKeyDown={onKeyDown}
      >
        {bars.map((p, i) => {
          const played = i / bars.length <= playedFraction;
          return (
            <div
              key={i}
              style={{
                height: `${Math.max(2, p * 28)}px`,
                width: BAR_W,
                flexShrink: 0,
                borderRadius: 1,
                background: played
                  ? "linear-gradient(to top, var(--color-frost-deep), var(--color-frost))"
                  : "color-mix(in srgb, var(--frost-muted) 45%, transparent)",
              }}
            />
          );
        })}
      </div>

      {/* Row 2: control bar — play + time (left), speed + save (right) */}
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={togglePlay}
          aria-label={playing ? "Pause" : "Play"}
          onMouseEnter={(e) => {
            if (!reducedMotion) e.currentTarget.style.transform = "scale(1.05)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.transform = "";
          }}
          className="w-7 h-7 shrink-0 rounded-full flex items-center justify-center text-white cursor-pointer transition-transform"
          style={{
            background:
              "linear-gradient(135deg, var(--color-frost), var(--color-frost-deep))",
          }}
        >
          {playing ? <PauseIcon /> : <PlayIcon />}
        </button>

        <span className="text-[11px] text-steel tabular-nums">
          {fmt(currentTime)} / {fmt(totalSeconds)}
        </span>

        <div className="flex-1" />

        <SecondaryButton
          type="button"
          onClick={cycleRate}
          aria-label={`Playback speed ${rate}x`}
          className="shrink-0 rounded-full px-2.5 h-7 text-[12px] font-medium text-steel"
        >
          {rate}x
        </SecondaryButton>

        {onSave && (
          <SecondaryButton
            type="button"
            onClick={handleSave}
            disabled={saving}
            aria-label="Save a copy"
            className="shrink-0 w-7 h-7 rounded-full flex items-center justify-center text-steel hover:text-slate"
          >
            {saving ? <SaveSpinner /> : <SaveIcon />}
          </SecondaryButton>
        )}
      </div>
    </div>
  );
}
