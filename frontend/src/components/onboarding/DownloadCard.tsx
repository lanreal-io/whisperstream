import { useEffect, useRef, useState, type ReactNode } from "react";
import { motion } from "framer-motion";
import type { AppEvent } from "../../lib/socket/events";

// Embedded (non-modal) download progress card for use inside the
// onboarding step 3 panel. Shown when the user reaches step 3 before the
// model has finished downloading.
//
// Layout decisions (settled after several iterations):
//
//   - Heading top-left at text-[24px], matching StepShortcut and StepTest.
//     No decorative dot on the heading itself; the heading is a clean
//     step title consistent with the rest of the flow.
//   - Subhead beneath the heading: "We'll continue automatically when
//     it's ready." Tells the user they don't need to act.
//   - Full-width progress bar.
//   - Metadata row: dot + ETA on the bottom-left, percentage on the
//     bottom-right (Carbon Design System convention for LTR progress
//     bars: label leading-edge, value trailing-edge). The pulsing
//     "active life sign" dot lives next to the live status text rather
//     than the static heading — that's where it actually means
//     something.
//   - Witty footer pinned to the bottom of the card via mt-auto. Quiet
//     italic line, character moment without competing with the rest.
//
// Bytes count and speed reading are deliberately NOT shown:
//   - Bytes (e.g. "287 MB / 600 MB") trigger non-technical-user anxiety
//     about the model's size. The user already opted in by clicking
//     Continue. The bar tells them how far along, the ETA tells them how
//     much longer; the absolute size answers no actionable question.
//   - Speed (e.g. "8.2 MB/s") is debug noise. ETA already covers the
//     "how much longer" question.
// The rolling speed sample is still calculated internally — we need it
// to derive the ETA — but never displayed.
//
// ETA format follows natural-language conventions (Carbon, Atlassian,
// macOS HIG, Windows installers): "About X seconds left" or "About X
// minutes left" depending on the magnitude. Singular/plural is handled.
// The "~40s left" abbreviated form is non-standard developer shorthand.
//
// Three runtime states:
//   - Phase 1 — no download.progress event yet: marquee shimmer bar
//     (matches App.tsx loading state). Metadata row shows
//     "· Connecting…" italic in the left position; percentage hidden.
//   - Phase 2 — first event landed but speed not yet calculated (~1s
//     window): real fill bar, "· Estimating…" italic on the left,
//     percentage on the right.
//   - Phase 3 — both data and speed: "· About 40 seconds left" on the
//     left, percentage on the right.

interface DownloadCardProps {
  /** Initial seed (0-1) so the bar doesn't snap from 0 if progress is mid-flight. */
  progress?: number;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  /** Hide the witty footer line. Used when the card is embedded inside the
   *  language-family confirm dialog where the dialog body already covers
   *  what the footer would say. Defaults to false (footer shown) so the
   *  onboarding StepTest path is unchanged. */
  hideFooter?: boolean;
  /** Heading text. Defaults to the onboarding copy. */
  title?: string;
  /** Subtitle text. Defaults to the onboarding copy. */
  subtitle?: string;
  /** Drop the min-height + footer-pin used by the onboarding step panel.
   *  Pass true when embedding inside a tight modal so the card doesn't
   *  reserve dead space below the metadata row. Defaults to false so
   *  onboarding is unchanged. */
  compact?: boolean;
  /** The transfer is complete but the backend is still verifying and
   *  installing the downloaded files. Keeps the completed bar visible while
   *  replacing the now-irrelevant ETA with finalization feedback. */
  finalizing?: boolean;
  /** Rendered between the subtitle and the progress track. The onboarding
   *  Enhance step puts the persistent ModelChip (name + size) here, so the
   *  card itself still never shows byte counts. */
  headerSlot?: ReactNode;
}

function formatEtaText(seconds: number): string {
  if (seconds <= 0) return "";
  if (seconds < 60) {
    return `About ${seconds} ${seconds === 1 ? "second" : "seconds"} left`;
  }
  const minutes = Math.round(seconds / 60);
  return `About ${minutes} ${minutes === 1 ? "minute" : "minutes"} left`;
}

export function DownloadCard({
  progress = 0,
  subscribe,
  hideFooter = false,
  title = "Downloading the speech model",
  subtitle = "We'll continue automatically when it's ready.",
  compact = false,
  finalizing = false,
  headerSlot,
}: DownloadCardProps) {
  const [downloaded, setDownloaded] = useState(0);
  const [total, setTotal] = useState(0);
  const [speed, setSpeed] = useState(0);
  const lastRef = useRef({ time: 0, bytes: 0 });

  useEffect(() => {
    return subscribe((event) => {
      if (event.type !== "download.progress") return;
      setDownloaded(event.downloaded);
      if (event.total > 0) setTotal(event.total);
      // Rolling 1s sample for the speed read-out — used to compute ETA
      // internally. The speed itself is never displayed; users want
      // "how much longer", not "how fast".
      const now = performance.now() / 1000;
      if (lastRef.current.time > 0) {
        const dt = now - lastRef.current.time;
        if (dt >= 1) {
          const db = event.downloaded - lastRef.current.bytes;
          setSpeed(db / dt);
          lastRef.current = { time: now, bytes: event.downloaded };
        }
      } else {
        lastRef.current = { time: now, bytes: event.downloaded };
      }
    });
  }, [subscribe]);

  // Prefer real measurement; fall back to the seed prop so the bar fills
  // smoothly even before the first WS event arrives.
  const measuredPct = total > 0 ? Math.min((downloaded / total) * 100, 100) : 0;
  const pct = Math.max(measuredPct, progress * 100);

  const etaSec =
    speed > 0 && total > downloaded ? Math.ceil((total - downloaded) / speed) : 0;
  const etaText = formatEtaText(etaSec);

  // Phase 1 = indeterminate: no known total yet (no progress event, or every
  // file's Content-Length probe came back empty). Show the marquee + a
  // "Connecting..." label with the percentage hidden, rather than a misleading
  // static "0%" while bytes are actually streaming.
  const isPhase1 = total === 0;

  return (
    // The min-height only exists to pin the witty footer to the card bottom;
    // without the footer it would just reserve dead space.
    <div className={`flex flex-col w-full pt-1${compact || hideFooter ? "" : " min-h-[224px]"}`}>
      {/* Upper group: heading, subhead, bar, metadata. Anchored top-left
          (no items-center, no text-center) to match the layout of
          StepShortcut and StepTest. */}
      <div className="flex flex-col w-full">
        {/* `compact` embeds this card inside a settings list, where the
            onboarding hero scale reads as a broken layout: at 24px the title
            was LARGER than the host modal's own title. Compact scales the
            type down to settings density; it is not only a min-height drop. */}
        {/* An empty `title` omits the heading rather than rendering an empty
            one. A host that already names the download in its own heading
            passes `title=""` (the setup flow's waiting variant does, so
            "Downloading your voice" is said once, not twice); an empty <h2>
            would put an unnamed heading in the accessibility tree and show up
            as a blank rung in a screen reader's heading list. */}
        {title !== "" && (
          <h2
            className={
              compact
                ? "text-[12px] font-medium text-slate leading-tight"
                : "font-display text-[24px] font-light tracking-tight text-slate leading-tight"
            }
          >
            {title}
          </h2>
        )}
        <p
          className={
            compact
              ? "mt-0.5 text-[10px] leading-[14px] text-steel"
              : "mt-2 text-[12px] text-steel"
          }
        >
          {subtitle}
        </p>

        {headerSlot}

        {/* Progress track. Two render modes:
              - total === 0 (phase 1): marquee shimmer — a small bright
                segment travels across the muted track, matching the
                loading indicator in App.tsx exactly.
              - total > 0 (phase 2+): real fill — width animates from the
                current position to the measured percentage. */}
        <div
          className={`${compact ? "mt-2 h-1.5" : "mt-7 h-2"} w-full rounded-full overflow-hidden relative`}
          style={{ background: "var(--frost-mesh-1)" }}
        >
          {total > 0 ? (
            <motion.div
              key="fill"
              className="h-full rounded-full"
              style={{
                background: "var(--accent-fill)",
                boxShadow: "var(--shadow-frost-glow)",
              }}
              initial={{ width: "0%" }}
              animate={{ width: `${pct}%` }}
              transition={{ duration: 0.4, ease: "easeOut" }}
            />
          ) : (
            <motion.div
              className="absolute inset-y-0 w-1/3 rounded-full"
              style={{
                background:
                  "linear-gradient(90deg, transparent, var(--frost-mesh-1), transparent)",
              }}
              animate={{ x: ["-100%", "400%"] }}
              transition={{
                duration: 1.5,
                repeat: Infinity,
                ease: "easeInOut",
              }}
            />
          )}
        </div>

        {/* Metadata row: pulsing dot + ETA on the left, percentage on the
            right. The dot is the "active life sign" — moved here from the
            heading because progress is the live thing happening, not the
            title. */}
        <div className="mt-3 w-full flex items-center justify-between min-h-[18px]">
          <div className="flex items-center gap-1.5">
            <motion.span
              className="w-1.5 h-1.5 rounded-full shrink-0"
              style={{
                background: "var(--accent-fill)",
                boxShadow: "var(--shadow-frost-glow)",
              }}
              animate={{ opacity: [1, 0.4, 1] }}
              transition={{ duration: 1.6, repeat: Infinity, ease: "easeInOut" }}
            />
            {finalizing ? (
              <p className="text-[11px] text-steel/60 italic">
                Finishing up...
              </p>
            ) : isPhase1 ? (
              <p className="text-[11px] text-steel/60 italic">Connecting…</p>
            ) : etaText ? (
              <p className="text-[11px] text-steel/60">{etaText}</p>
            ) : (
              <p className="text-[11px] text-steel/60 italic">Estimating…</p>
            )}
          </div>
          {!isPhase1 && (
            <p className="text-[13px] font-medium text-slate font-display tabular-nums">
              {pct.toFixed(0)}%
            </p>
          )}
        </div>
      </div>

      {/* Quiet character moment pinned to the bottom of the card. Echoes
          the marketing trinity ("Your voice. Your machine. Your words.")
          and frames the wait as anticipation rather than friction. */}
      {!hideFooter && (
        <p className="mt-auto text-[11px] text-steel/50 italic">
          A short wait, then your voice becomes text.
        </p>
      )}
    </div>
  );
}
