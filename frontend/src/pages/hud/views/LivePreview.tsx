import {
  type DictationModel,
  type EffectiveProfile,
  normalizePreviewText,
} from "../dictationState";
import { useReducedMotion, motion } from "framer-motion";
import { useRef, useCallback, useLayoutEffect } from "react";
import {
  PREVIEW_CARD_NON_TEXT_H,
  PREVIEW_COMPACT_WELL_H,
  PREVIEW_FONT_SIZE,
  PREVIEW_LINE_HEIGHT,
  PREVIEW_BOTTOM_THRESHOLD,
  PREVIEW_CARD_RADIUS,
  PILL_SPRING,
  PREVIEW_EXPANDED_CARD_W,
} from "../geometry";
import { HIDE_ANIMATION_MS } from "../presentation";
import { ProfileMark } from "../../../components/ProfileMark";
import { ProcessingSpinner, HudRecordingControls } from "./RecordingControls";
import { VoiceBars } from "../../../components/Waveform";
import { ReconciledPreviewText } from "../components/PreviewText";
import { HudExpandToggle } from "../components/HudExpandToggle";

export function PreviewCard({
  phase,
  preview,
  rms,
  peak,
  barColor,
  expanded,
  cardWidth,
  cardHeight,
  closing,
  hudOriginRecording,
  commandsDisabled,
  onCancel,
  onFinish,
  onExpandedChange,
  onNaturalTextHeightChange,
  agentCompose,
  profileMark,
}: {
  phase: "recording" | "processing";
  preview: DictationModel["preview"];
  rms: number;
  peak: number;
  barColor: string;
  expanded: boolean;
  cardWidth: number;
  cardHeight: number;
  closing: boolean;
  hudOriginRecording: boolean;
  commandsDisabled: boolean;
  onCancel: () => void;
  onFinish: () => void;
  onExpandedChange: (expanded: boolean) => void;
  onNaturalTextHeightChange: (height: number) => void;
  agentCompose: boolean;
  /** The effective enhancement profile for this dictation, or null. */
  profileMark: EffectiveProfile | null;
}) {
  const reducedMotion = useReducedMotion() ?? false;
  const visibleText = normalizePreviewText(preview?.text ?? "");
  const scrollerRef = useRef<HTMLDivElement>(null);
  const followLatestRef = useRef(true);
  const preservedScrollTopRef = useRef(0);
  const wellHeight = expanded
    ? Math.max(0, cardHeight - PREVIEW_CARD_NON_TEXT_H)
    : PREVIEW_COMPACT_WELL_H;

  const measureNaturalTextHeight = useCallback(
    (measurementWidth: number) => {
      const measurement = document.createElement("p");
      measurement.dataset.previewNaturalMeasure = "true";
      measurement.textContent = visibleText;
      Object.assign(measurement.style, {
        position: "fixed",
        left: "-100000px",
        top: "0",
        width: `${Math.max(1, measurementWidth)}px`,
        height: "auto",
        margin: "0",
        padding: "0",
        border: "0",
        visibility: "hidden",
        pointerEvents: "none",
        fontFamily: "var(--font-quote)",
        fontSize: `${PREVIEW_FONT_SIZE}px`,
        fontWeight: "480",
        lineHeight: String(PREVIEW_LINE_HEIGHT),
        letterSpacing: "-0.005em",
        overflowWrap: "anywhere",
        whiteSpace: "normal",
      });
      document.body.appendChild(measurement);
      const measuredHeight = Math.round(
        Math.max(
          measurement.scrollHeight,
          measurement.getBoundingClientRect().height,
        ),
      );
      measurement.remove();
      return measuredHeight;
    },
    [visibleText],
  );

  const onPreviewScroll = useCallback(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    const maxScrollTop = Math.max(
      0,
      scroller.scrollHeight - scroller.clientHeight,
    );
    followLatestRef.current =
      maxScrollTop - scroller.scrollTop <= PREVIEW_BOTTOM_THRESHOLD;
    preservedScrollTopRef.current = scroller.scrollTop;
  }, []);

  useLayoutEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    if (followLatestRef.current) {
      scroller.scrollTop = Math.max(
        0,
        scroller.scrollHeight - scroller.clientHeight,
      );
      preservedScrollTopRef.current = scroller.scrollTop;
      return;
    }
    scroller.scrollTop = preservedScrollTopRef.current;
  }, [visibleText]);

  const controlLabel = expanded ? "Collapse preview" : "Expand preview";

  return (
    <motion.div
      initial={false}
      animate={{
        width: cardWidth,
        height: cardHeight,
        borderRadius: PREVIEW_CARD_RADIUS,
      }}
      transition={PILL_SPRING}
      className="relative overflow-hidden flex flex-col"
      data-testid="live-preview-card"
      data-phase={phase}
      data-expanded={expanded ? "true" : "false"}
      data-closing={closing ? "true" : "false"}
      data-agent-compose={agentCompose ? "true" : "false"}
      style={{
        background: "var(--surface-overlay)",
        opacity: closing ? 0 : 1,
        transition:
          reducedMotion
            ? "none"
            : closing
              ? `opacity ${HIDE_ANIMATION_MS}ms ease`
              : "opacity 120ms ease",
        boxShadow:
          "inset 0 1px 0 var(--rim-specular), inset 0 -1px 0 rgba(255,255,255,0.2), var(--shadow-glass-sm)",
        padding: "12px 16px 13px",
      }}
    >
      <div
        className="relative flex shrink-0 items-center justify-center"
        data-testid="preview-view-controls"
        style={{ height: 20 }}
      >
        {profileMark !== null && (
          // The mirror position of the expand/collapse button already pinned to
          // `right: 0`. The centered bars / spinner are untouched, so the card's
          // fixed compact geometry and its expanded measurement path are too.
          <span className="absolute inset-y-0 left-0 flex items-center">
            <ProfileMark
              profileId={profileMark.profileId}
              name={profileMark.name}
              iconKey={profileMark.icon}
              size={16}
              testId="hud-profile-mark"
            />
          </span>
        )}
        {phase === "processing" ? (
          <ProcessingSpinner />
        ) : !hudOriginRecording ? (
          <VoiceBars
            state="recording"
            rms={rms}
            peak={peak}
            color={barColor}
          />
        ) : null}
        {/* The shared HUD expand pill. It is taller than this 20px row, so
            it is centered on the row and overhangs into the card's padding
            without moving the bars or the text well. */}
        <span className="absolute inset-y-0 right-0 flex items-center">
          <HudExpandToggle
            expanded={expanded}
            label={controlLabel}
            onToggle={() => {
              if (!expanded) {
                onNaturalTextHeightChange(
                  measureNaturalTextHeight(PREVIEW_EXPANDED_CARD_W - 32),
                );
              }
              onExpandedChange(!expanded);
            }}
          />
        </span>
      </div>
      <div
        ref={scrollerRef}
        className="relative shrink-0 overflow-y-auto overflow-x-hidden"
        aria-hidden="true"
        data-testid="preview-well"
        data-hud-interactive={expanded ? "true" : undefined}
        onScroll={onPreviewScroll}
        style={{
          height: wellHeight,
          marginTop: 9,
          overscrollBehavior: "contain",
          scrollBehavior: "auto",
          touchAction: "pan-y",
          WebkitMaskImage:
            expanded
              ? "none"
              : "linear-gradient(to bottom, transparent, black 10px)",
          maskImage: expanded
            ? "none"
            : "linear-gradient(to bottom, transparent, black 10px)",
        }}
      >
        <p
          className="m-0 w-full"
          data-testid="preview-text"
          style={{
            fontFamily: "var(--font-quote)",
            fontSize: PREVIEW_FONT_SIZE,
            fontWeight: 480,
            lineHeight: PREVIEW_LINE_HEIGHT,
            letterSpacing: "-0.005em",
            overflowWrap: "anywhere",
            color: agentCompose
              ? "var(--color-steel)"
              : "var(--color-slate)",
            opacity: agentCompose || phase === "processing" ? 0.72 : 1,
          }}
        >
          <ReconciledPreviewText
            text={visibleText}
            stableCharCount={preview?.stableCharCount ?? 0}
          />
        </p>
      </div>
      {phase === "recording" && hudOriginRecording ? (
        <div
          className="flex shrink-0 items-center"
          data-testid="preview-recording-controls"
          style={{ height: 24, marginTop: 8 }}
        >
          <HudRecordingControls
            rms={rms}
            peak={peak}
            barColor={barColor}
            disabled={commandsDisabled}
            compact
            onCancel={onCancel}
            onFinish={onFinish}
            testId="preview-outcome-controls"
          />
        </div>
      ) : null}
    </motion.div>
  );
}
