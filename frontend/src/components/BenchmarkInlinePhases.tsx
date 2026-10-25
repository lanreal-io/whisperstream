import { useEffect, useRef, type ReactNode } from "react";
import { motion } from "framer-motion";
import { DownloadCard } from "./onboarding/DownloadCard";
import type { AppEvent } from "../lib/socket/events";
import { LOCAL_MODEL_DISPLAY_LABEL } from "../lib/localModels";
import {
  SpeedBar,
  ModelChip,
  LifeSignStatus,
  DeviceCompletionStatus,
  completionTone,
  speedToneForWps,
  type BenchmarkDeviceVerdict,
  type ChipStatus,
} from "./BenchmarkVisuals";

/* Inline (onboarding) layouts for BenchmarkFlow's phases. BenchmarkFlow owns
   phase logic, the WS subscription and verdict firing; these render each phase
   inside the host's glass step card at the onboarding hero scale (24px Outfit
   Light, top-left). One ModelChip stays in the same position from download
   through verdict so the run reads as one continuous object. Footer actions
   belong to the host (onboarding design.md Decision 3); the card only holds
   the in-progress Cancel (download and prefilter only). */

const fade = {
  initial: { opacity: 0 },
  animate: { opacity: 1 },
  exit: { opacity: 0 },
  transition: { duration: 0.22, ease: [0.2, 0, 0, 1] as const },
};

const headingClass =
  "font-display text-[24px] font-light tracking-tight text-slate leading-tight outline-none";

function modelName(id: string) {
  return LOCAL_MODEL_DISPLAY_LABEL[id] ?? id;
}

/** Verdict headings take focus on phase change so screen readers announce the
 *  outcome (design.md Decision 13). The heading is not interactive, so it
 *  carries no visible focus ring. */
function InlineHeading({ children, focus = false }: { children: ReactNode; focus?: boolean }) {
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (focus) ref.current?.focus({ preventScroll: true });
  }, [focus]);
  return (
    <h2 ref={ref} id="benchmark-heading" tabIndex={focus ? -1 : undefined} className={headingClass}>
      {children}
    </h2>
  );
}

function InlineChip({ id, status, size }: { id: string; status: ChipStatus; size?: string }) {
  return (
    <div className="mt-[18px] flex items-center">
      <ModelChip id={id} status={status} animateActive={false} layoutPrefix={null} sizeLabel={size} />
    </div>
  );
}

function InlineCancel({ onClick }: { onClick: () => void }) {
  return (
    <div className="mt-4 flex items-center justify-end">
      <button
        onClick={onClick}
        className="px-4 py-1.5 rounded-full text-[13px] font-medium text-steel hover:text-slate hover:bg-[var(--surface-hover)] cursor-pointer transition-all duration-200"
      >
        Cancel
      </button>
    </div>
  );
}

export function InlinePrefilterPhase({
  modelId,
  modelSize,
  onCancel,
}: {
  modelId: string;
  modelSize?: string;
  onCancel: () => void;
}) {
  return (
    <motion.div {...fade} data-inline-phase="prefilter">
      <InlineHeading>{`Testing how fast ${modelName(modelId)} runs here`}</InlineHeading>
      <InlineChip id={modelId} status="active" size={modelSize} />
      <div className="mt-7">
        <SpeedBar mode="marquee" />
      </div>
      <div className="mt-3 min-h-[18px] flex items-center">
        <LifeSignStatus status="Checking your hardware..." />
      </div>
      <InlineCancel onClick={onCancel} />
    </motion.div>
  );
}

export function InlineDownloadPhase({
  modelId,
  modelSize,
  subscribe,
  onCancel,
}: {
  modelId: string;
  modelSize?: string;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  onCancel: () => void;
}) {
  return (
    <motion.div {...fade} data-inline-phase="download">
      <DownloadCard
        hideFooter
        title={`Downloading ${modelName(modelId)}`}
        subtitle="This can take a few minutes."
        headerSlot={<InlineChip id={modelId} status="active" size={modelSize} />}
        subscribe={subscribe}
      />
      <InlineCancel onClick={onCancel} />
    </motion.div>
  );
}

export function InlineMeasuringPhase({
  modelId,
  modelSize,
  liveWps,
  deviceName,
  completedVerdict,
}: {
  modelId: string;
  modelSize?: string;
  liveWps: number | undefined;
  deviceName?: string;
  completedVerdict?: BenchmarkDeviceVerdict;
}) {
  // No Cancel here: a running speed check cannot be stopped, matching the
  // modal, which offers Cancel only during intro, prefilter and download.
  return (
    <motion.div {...fade} data-inline-phase="benchmark">
      <InlineHeading>{`Testing how fast ${modelName(modelId)} runs here`}</InlineHeading>
      <InlineChip id={modelId} status="active" size={modelSize} />
      <div className="mt-7">
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
      </div>
      <div className="mt-3 min-h-[18px] flex items-center">
        {completedVerdict ? (
          <DeviceCompletionStatus verdict={completedVerdict} />
        ) : (
          <LifeSignStatus status={`Checking ${modelName(modelId)}`} active={liveWps != null} />
        )}
      </div>
    </motion.div>
  );
}

/** Shared verdict card: heading, subtitle, the persistent chip, the settled
 *  SpeedBar receipt in the verdict tone and an optional verdict chip. */
export function InlineVerdictPhase({
  phaseKey,
  title,
  body,
  modelId,
  modelSize,
  chipStatus,
  measuredWps,
  tone,
  verdictChip,
  settledReceipt = false,
}: {
  phaseKey: string;
  title: string;
  body: string;
  modelId: string;
  modelSize?: string;
  chipStatus: ChipStatus;
  /** Omitted (infrastructure error) = no receipt bar. */
  measuredWps?: number;
  tone?: "mint" | "caution" | "rose";
  verdictChip?: BenchmarkDeviceVerdict;
  /** Show the receipt at its value without re-counting (the declined card
   *  follows the marginal card with the same reading). */
  settledReceipt?: boolean;
}) {
  return (
    <motion.div {...fade} data-inline-phase={phaseKey}>
      <InlineHeading focus>{title}</InlineHeading>
      <p className="mt-2 text-[12px] leading-relaxed text-steel">{body}</p>
      <InlineChip id={modelId} status={chipStatus} size={modelSize} />
      {measuredWps != null && tone ? (
        <div className="mt-7">
          <SpeedBar mode="fill" value={measuredWps} tone={tone} settled={settledReceipt} />
        </div>
      ) : null}
      {verdictChip ? (
        <div className="mt-3">
          <DeviceCompletionStatus verdict={verdictChip} receipt />
        </div>
      ) : null}
    </motion.div>
  );
}
