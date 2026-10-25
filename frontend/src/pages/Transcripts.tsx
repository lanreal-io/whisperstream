import {
  useState,
  useEffect,
  useId,
  useRef,
  useCallback,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import type { AppConfig } from "../lib/api/config";
import type { DevTranscriptEnhancementResult, DevTranscriptExecutionMode, TranscriptEnhancementProvenance, TranscriptReenhancementResult, TranscriptListItem, TranscriptListResponse, TranscriptRecord } from "../lib/api/transcripts";
import type { EnhancementModel } from "../lib/api/enhancement";
import type { AppEvent } from "../lib/socket/events";
import type { LockStatus } from "../lib/useLockdown";
import { enableTranscriptHistory, purgeTranscripts, clearTranscripts, deleteTranscript, enhanceDevTranscript, listTranscripts, getTranscript, reenhanceTranscript, transcriptAudioUrl, DevTranscriptEnhancementError, TranscriptReenhancementError } from "../lib/api/transcripts";
import { useAudioImport } from "../lib/useAudioImport";
import { EnhancementIcon } from "../components/EnhancementIcon";
import { CopyGlyph } from "../components/CopyGlyph";
import { BakeoffWorkspace } from "../components/BakeoffWorkspace";
import { useBakeoffSession } from "./transcripts/useBakeoffSession";
import { HoverTooltip } from "../components/HoverTooltip";
import { PrimaryButton } from "../components/PrimaryButton";
import { ReadingWell } from "../components/ReadingWell";
import { SecondaryButton } from "../components/SecondaryButton";
import { SettingsPopover } from "../components/SettingsPopover";
import { Toggle } from "../components/Toggle";
import { HistoryRecoverySetupDialog, type HistoryRecoverySetupReason } from "../components/HistoryRecoverySetupDialog";
import { useHistoryRecovery, type HistoryRecoveryHandle } from "../lib/useHistoryRecovery";
import { historyPlatform } from "../lib/historyRecoveryCopy";
import { HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE } from "../lib/api/lockdown";
import { TranscriptConfirmDialog } from "../components/TranscriptConfirmDialog";
import { RecoveryUnlockScreen } from "../components/RecoveryUnlockScreen";
import { StartFreshDialog } from "../components/StartFreshDialog";
import { RecoverySetupGate } from "../components/RecoverySetupGate";
import { RecoverySettingsDialog } from "../components/RecoverySettingsDialog";
import { WaveformPlayer } from "../components/WaveformPlayer";
import { errorType } from "../lib/logCapture";
import type { DailyUseCapabilityReport } from "../lib/dailyUseCapabilities";
import {
  readBoundedNativeSaveResponse,
  saveMacosPrivateFile,
  supportsMacosPrivateFileWorkflows,
} from "../lib/nativeFileWorkflows";
import { useExperimental } from "../lib/experimental";

const PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 200;

// Page crossfade — matches App.tsx's tab transition (tween 0.15, ease [0.2,0,0,1]).
const VIEW_TRANSITION = {
  type: "tween" as const,
  duration: 0.15,
  ease: [0.2, 0, 0, 1] as const,
};

// ---------------------------------------------------------------------------
// Icons (inline SVG, stroke 1.5, round caps/joins — same idiom as siblings)
// ---------------------------------------------------------------------------

function ShieldIcon({ size = 12 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M7 1.5l4.5 1.8v3.2c0 2.8-1.9 4.6-4.5 5.5C4.4 11.1 2.5 9.3 2.5 6.5V3.3L7 1.5z" />
      <path d="M5 6.8l1.4 1.4L9 5.5" />
    </svg>
  );
}

function SearchIcon() {
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
      className="shrink-0 text-steel"
      aria-hidden="true"
    >
      <circle cx="7" cy="7" r="4.5" />
      <path d="M10.5 10.5L14 14" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M7 2.5v9M2.5 7h9" />
    </svg>
  );
}

function SparklesIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M5.2 1.5c.35 1.85 1.45 2.95 3.3 3.3-1.85.35-2.95 1.45-3.3 3.3C4.85 6.25 3.75 5.15 1.9 4.8c1.85-.35 2.95-1.45 3.3-3.3z" />
      <path d="M10.25 7.25c.2 1.1.85 1.75 1.95 1.95-1.1.2-1.75.85-1.95 1.95-.2-1.1-.85-1.75-1.95-1.95 1.1-.2 1.75-.85 1.95-1.95z" />
    </svg>
  );
}

function TranscriptVersionChip({
  version,
  subtle = false,
  provenanceTooltip,
  tooltipId,
  focusVisibleClassName,
}: {
  version: "enhanced" | "original";
  subtle?: boolean;
  provenanceTooltip?: TranscriptProvenanceTooltip | null;
  tooltipId?: string;
  focusVisibleClassName?: string;
}) {
  const enhanced = version === "enhanced";
  const chip = (
    <span
      data-transcript-version={version}
      className={`inline-flex items-center gap-1 rounded-full px-1.5 py-[1px] font-display text-[9px] font-semibold uppercase tracking-[0.08em] shrink-0 ${subtle ? "text-steel/70" : "text-steel"}`}
      style={{
        background: enhanced
          ? `color-mix(in srgb, var(--color-frost) ${subtle ? "8%" : "15%"}, transparent)`
          : "var(--surface-control)",
        border: enhanced
          ? `1px solid color-mix(in srgb, var(--color-frost) ${subtle ? "18%" : "30%"}, transparent)`
          : "1px solid var(--border-hairline)",
        boxShadow: `inset 0 1px 0 rgb(255 255 255 / calc(${subtle ? "0.35" : "0.6"} * var(--rim-scale)))`,
      }}
    >
      {enhanced && (
        <span className={subtle ? "text-frost-deep/70" : "text-frost-deep"}>
          <EnhancementIcon className="size-2.5" data-transcript-sparkle />
        </span>
      )}
      {enhanced ? "Enhanced" : "Original"}
    </span>
  );

  if (!provenanceTooltip) return chip;
  return (
    <HoverTooltip
      content={<TranscriptProvenanceTooltipContent tooltip={provenanceTooltip} />}
      side="bottom-left"
      width="w-64"
      bubbleId={tooltipId}
      focusVisibleClassName={focusVisibleClassName}
    >
      {chip}
    </HoverTooltip>
  );
}

type TranscriptProvenanceTooltip = {
  profileName?: string;
  explanation?: string;
};

const COMPLETE_TIMEOUT_COPY =
  "Enhancement took too long, so the original transcript was used.";
const COMPLETE_ERROR_COPY =
  "Enhancement couldn’t finish, so the original transcript was used.";
const PARTIAL_TIMEOUT_COPY =
  "Some of this transcript took too long to enhance, so the original wording was kept.";
const PARTIAL_ERROR_COPY =
  "Some of this transcript couldn’t be enhanced, so the original wording was kept.";

function validatedTranscriptProvenance(
  value: unknown,
  hasEnhanced: boolean,
): TranscriptEnhancementProvenance | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const fields = Object.keys(value).sort();
  if (
    fields.length !== 3 ||
    fields[0] !== "outcome" ||
    fields[1] !== "profile_id" ||
    fields[2] !== "profile_name"
  ) {
    return null;
  }

  const candidate = value as Record<string, unknown>;
  const profileId = candidate.profile_id;
  const profileName = candidate.profile_name;
  const outcome = candidate.outcome;
  if (
    typeof profileId !== "string" ||
    !profileId.trim() ||
    [...profileId].length > 128 ||
    typeof profileName !== "string" ||
    !profileName.trim() ||
    [...profileName].length > 256 ||
    (outcome !== "success" && outcome !== "timeout" && outcome !== "error") ||
    (outcome === "success" && !hasEnhanced)
  ) {
    return null;
  }
  return {
    profile_id: profileId,
    profile_name: profileName,
    outcome,
  };
}

function transcriptProvenanceTooltip(
  value: unknown,
  hasEnhanced: boolean,
): TranscriptProvenanceTooltip | null {
  const provenance = validatedTranscriptProvenance(value, hasEnhanced);
  if (!provenance) return null;
  if (provenance.outcome === "success") {
    return { profileName: provenance.profile_name };
  }
  if (hasEnhanced) {
    return {
      profileName: provenance.profile_name,
      explanation:
        provenance.outcome === "timeout"
          ? PARTIAL_TIMEOUT_COPY
          : PARTIAL_ERROR_COPY,
    };
  }
  return {
    explanation:
      provenance.outcome === "timeout"
        ? COMPLETE_TIMEOUT_COPY
        : COMPLETE_ERROR_COPY,
  };
}

function TranscriptProvenanceTooltipContent({
  tooltip,
}: {
  tooltip: TranscriptProvenanceTooltip;
}) {
  return (
    <span className="block min-w-0 break-words">
      {tooltip.profileName && (
        <span className="block">Profile: {tooltip.profileName}</span>
      )}
      {tooltip.explanation && (
        <span className={`block ${tooltip.profileName ? "mt-1" : ""}`}>
          {tooltip.explanation}
        </span>
      )}
    </span>
  );
}

function FireIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M7.3 1.4c.4 2-1.1 2.9-2.1 4.1C4.3 6.6 4 7.5 4 8.5a3.1 3.1 0 006.2 0c0-1.8-1-3.5-2.9-5.1.1 1.1-.3 1.8-.9 2.4" />
      <path d="M7 7.2c.8.8 1.2 1.5 1.2 2.1A1.2 1.2 0 015.8 9c0-.5.3-1.1 1.2-1.8z" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 7.5l3 3 5-6" />
    </svg>
  );
}

function XIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <line x1="3" y1="3" x2="9" y2="9" />
      <line x1="9" y1="3" x2="3" y2="9" />
    </svg>
  );
}

// Trash/bin glyph for the (permanent) delete action — unambiguously "delete",
// vs the X used for transient "remove from import queue". Same stroke idiom as
// the sibling icons.
function TrashIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M2.5 4h9" />
      <path d="M5.5 4V3a1 1 0 011-1h1a1 1 0 011 1v1" />
      <path d="M3.7 4l.42 6.5A1.3 1.3 0 005.4 11.7h3.2a1.3 1.3 0 001.28-1.2L10.3 4" />
      <path d="M6 6.2v3M8 6.2v3" />
    </svg>
  );
}

function BackChevron() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="shrink-0"
      aria-hidden="true"
    >
      <path d="M8.5 3L4.5 7l4 4" />
    </svg>
  );
}

// A waveform/transcript glyph for the empty + disabled state icon container.
function WaveformGlyph() {
  return (
    <svg
      width="48"
      height="48"
      viewBox="0 0 48 48"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M8 24v0M14 18v12M20 12v24M26 16v16M32 10v28M38 18v12M44 24v0" />
    </svg>
  );
}

// Upload / drop-target glyph for the import drop zone (tray + up arrow).
function UploadGlyph() {
  return (
    <svg
      width="40"
      height="40"
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M20 25V8M14 14l6-6 6 6" />
      <path d="M8 24v6a2 2 0 002 2h20a2 2 0 002-2v-6" />
    </svg>
  );
}

// Small inline spinner for the "Transcribing" queue pill (a sweeping arc).
function Spinner() {
  return (
    <motion.svg
      width="11"
      height="11"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      aria-hidden="true"
      animate={{ rotate: 360 }}
      transition={{ duration: 0.9, repeat: Infinity, ease: "linear" }}
    >
      <path d="M7 1.5a5.5 5.5 0 015.5 5.5" />
    </motion.svg>
  );
}

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

/** "Jun 20, 2:14 PM" — the card timestamp format. */
function formatTimestamp(createdAtMs: number): string {
  const d = new Date(createdAtMs);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Default filename for the "Save a copy" export: whisperstream-YYYY-MM-DD-HHMM.wav. */
function defaultAudioFilename(createdAtMs: number): string {
  const d = new Date(createdAtMs);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `whisperstream-${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(
    d.getDate(),
  )}-${pad(d.getHours())}${pad(d.getMinutes())}.wav`;
}

/** Retention day -> human label, for the retention-shrink confirm copy. Mirrors
 *  the LOCKED retention option labels in SettingsPopover. */
function retentionLabel(days: number): string {
  switch (days) {
    case 0:
      return "Forever";
    case 1:
      return "1 day";
    case 7:
      return "7 days";
    case 30:
      return "30 days";
    default:
      return `${days} days`;
  }
}

// ---------------------------------------------------------------------------
// Development candidate evaluation
// ---------------------------------------------------------------------------

const CANDIDATE_UNAVAILABLE =
  "This candidate's model file is unavailable. Choose another candidate in Enhancement.";
const CANDIDATE_WARMING =
  "The active model is still warming. Try again in a moment.";
const CANDIDATE_BUSY =
  "The local model is busy. Try again when the current task finishes.";
const CANDIDATE_FAILURE =
  "Couldn't test this transcript. Make sure the active model is ready, then try again.";

function candidateFailureCopy(
  error: unknown,
  activeModelId: string,
): string {
  if (!(error instanceof DevTranscriptEnhancementError)) {
    return CANDIDATE_FAILURE;
  }

  if (
    error.code === "candidate_unavailable" ||
    error.code === "local_candidate_required"
  ) {
    return CANDIDATE_UNAVAILABLE;
  }
  if (
    error.code === "local_model_cold" ||
    error.code === "local_model_warming" ||
    error.code === "local_model_switching" ||
    error.code === "local_enhancer_unavailable"
  ) {
    return CANDIDATE_WARMING;
  }
  if (
    error.code === "local_model_busy" ||
    error.code === "recording_active"
  ) {
    return CANDIDATE_BUSY;
  }
  if (error.code === "unsupported_profile_mode") {
    const modelId = error.modelId || activeModelId;
    const profileMode = error.profileMode || "this mode";
    return `${modelId} was not trained for ${profileMode}. Switch profiles before testing it.`;
  }
  return CANDIDATE_FAILURE;
}

function executionLabel(mode: DevTranscriptExecutionMode): "Whole" | "Batched" {
  return mode === "whole" ? "Whole" : "Batched";
}

const REENHANCE_FAILURE =
  "Couldn't re-enhance this transcript. Check the provider connection, then try again.";

function reenhancementFailureCopy(error: unknown): string {
  if (!(error instanceof TranscriptReenhancementError)) return REENHANCE_FAILURE;
  if (error.status === 423) return "Unlock transcripts, then try again.";
  if (error.code === "enhancement_disabled") {
    return "Turn on AI enhancement, then try again.";
  }
  if (error.code === "enhancement_consent_required") {
    return "Approve the current provider in Enhancement, then try again.";
  }
  if (error.code === "enhancement_not_configured") {
    return "Finish setting up the current provider in Enhancement, then try again.";
  }
  if (error.code === "default_profile_unavailable") {
    return "Choose a default enhancement profile, then try again.";
  }
  if (error.code === "local_model_warming") {
    return "The local model is warming. Try again in a moment.";
  }
  if (error.code === "local_model_unavailable") {
    return "Download the selected local model in Enhancement, then try again.";
  }
  if (
    error.code === "enhancement_busy" ||
    error.code === "recording_active"
  ) {
    return "Whisperstream is busy with another dictation. Try again when it finishes.";
  }
  if (error.code === "enhancement_configuration_changed") {
    return "Enhancement settings changed during the run. Try again.";
  }
  return REENHANCE_FAILURE;
}

function ReenhancementPanel({
  running,
  result,
  error,
}: {
  running: boolean;
  result: TranscriptReenhancementResult | null;
  error: string | null;
}) {
  if (!running && !result && !error) return null;

  return (
    <section
      role="region"
      aria-label="Re-enhanced result"
      aria-busy={running}
      className="rounded-[var(--radius-row)] p-3 space-y-2"
      style={{
        background: "var(--surface-raised)",
        border: "1px solid var(--border-hairline)",
        boxShadow: "var(--shadow-glass-sm)",
      }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-[13px] font-medium text-slate">Re-enhanced</h3>
          {result && (
            <p className="mt-0.5 truncate text-[11px] text-steel">
              {result.provider} · {result.model_id || "Default model"} ·{" "}
              {result.profile_name || result.profile_id}
            </p>
          )}
        </div>
        <span
          className={`shrink-0 text-[10px] font-medium uppercase tracking-wider ${
            error ? "text-rose" : result?.unchanged ? "text-steel" : "text-mint"
          }`}
        >
          {running
            ? "Running"
            : error
              ? "Needs attention"
              : result?.unchanged
                ? "Unchanged"
                : "Complete"}
        </span>
      </div>

      {running && (
        <div className="flex items-center gap-2 text-[12px] text-steel">
          <span className="animate-pulse text-frost">
            <SparklesIcon />
          </span>
          Running the saved original through your current setup...
        </div>
      )}

      {error && (
        <p role="alert" className="text-[11px] leading-relaxed text-rose">
          {error}
        </p>
      )}

      {result && !running && (
        <div className="space-y-2">
          <p className="text-[10px] text-steel/60">
            {`${executionLabel(result.mode)} · ${result.chunk_count} ${
              result.chunk_count === 1 ? "chunk" : "chunks"
            } · ${result.elapsed_ms} ms`}
          </p>
          {result.unchanged && (
            <p className="text-[11px] leading-relaxed text-steel">
              {result.unchanged_reason === "short_input"
                ? "This transcript is short enough that production enhancement leaves it unchanged."
                : "The provider returned the original text, or a production safeguard kept it unchanged."}
            </p>
          )}
          <ReadingWell
            maxHeight={220}
            ariaLabel="Re-enhanced output"
            fontSize={13}
            fontFamily="var(--font-quote)"
            fontWeight={400}
          >
            {result.output}
          </ReadingWell>
        </div>
      )}
    </section>
  );
}

function CandidateModeSelector({
  value,
  disabled,
  onChange,
}: {
  value: DevTranscriptExecutionMode;
  disabled: boolean;
  onChange: (mode: DevTranscriptExecutionMode) => void;
}) {
  const options: {
    value: DevTranscriptExecutionMode;
    label: "Whole" | "Batched";
  }[] = [
    { value: "whole", label: "Whole" },
    { value: "batched", label: "Batched" },
  ];
  const buttonRefs = useRef<
    Partial<Record<DevTranscriptExecutionMode, HTMLButtonElement>>
  >({});

  const moveSelection = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    currentIndex: number,
  ) => {
    if (
      event.key === "ArrowLeft" ||
      event.key === "ArrowRight" ||
      event.key === "ArrowUp" ||
      event.key === "ArrowDown" ||
      event.key === "Home" ||
      event.key === "End"
    ) {
      event.preventDefault();
      const next =
        event.key === "Home"
          ? options[0].value
          : event.key === "End"
            ? options[options.length - 1].value
            : options[
                event.key === "ArrowRight" || event.key === "ArrowDown"
                  ? (currentIndex + 1) % options.length
                  : (currentIndex - 1 + options.length) % options.length
              ].value;
      onChange(next);
      buttonRefs.current[next]?.focus();
    }
  };

  return (
    <div
      role="radiogroup"
      aria-label="Execution mode"
      className="inline-flex rounded-full p-1 gap-1"
      style={{
        background: "var(--surface-control)",
        border: "1px solid var(--border-hairline)",
      }}
    >
      {options.map((option, index) => {
        const selected = option.value === value;
        return (
          <button
            ref={(element) => {
              buttonRefs.current[option.value] = element ?? undefined;
            }}
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            tabIndex={selected ? 0 : -1}
            disabled={disabled}
            onClick={() => onChange(option.value)}
            onKeyDown={(event) => moveSelection(event, index)}
            className={`h-7 min-w-[72px] rounded-full px-3 text-[12px] font-medium transition-colors duration-150 disabled:cursor-default ${
              selected ? "text-white" : "text-steel hover:text-slate"
            }`}
            style={{
              background: selected ? "var(--accent-fill)" : "transparent",
              border: selected
                ? "1px solid var(--border-accent)"
                : "1px solid transparent",
            }}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

function ContractSpine({
  activeModelId,
  activeModel,
  mode,
  running,
  result,
}: {
  activeModelId: string;
  activeModel: EnhancementModel | null | undefined;
  mode: DevTranscriptExecutionMode;
  running: boolean;
  result: DevTranscriptEnhancementResult | null;
}) {
  const values = [
    { label: "Model", value: result?.model_id || activeModelId || "None" },
    {
      label: "Contract",
      value:
        result?.serving_contract ||
        (activeModel?.id === activeModelId
          ? activeModel.serving_contract
          : null) ||
        "Unknown",
    },
    {
      label: "Execution",
      value: executionLabel(result?.mode || mode),
    },
    {
      label: "Elapsed",
      value: running
        ? "Testing..."
        : result
          ? `${result.elapsed_ms} ms`
          : "Not run",
    },
  ];

  return (
    <dl
      aria-label="Candidate test contract"
      className="grid grid-cols-2 gap-x-3 gap-y-2 rounded-[var(--radius-input)] px-3 py-2 min-[1100px]:grid-cols-4"
      style={{
        background: "var(--surface-control)",
        border: "1px solid var(--border-hairline)",
      }}
    >
      {values.map((item) => (
        <div key={item.label} className="min-w-0">
          <dt className="text-[9px] font-medium uppercase tracking-[0.08em] text-steel/60">
            {item.label}
          </dt>
          <dd className="mt-0.5 truncate text-[11px] font-medium text-slate">
            {item.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function CandidateTestPanel({
  activeModelId,
  activeModel,
  mode,
  running,
  result,
  error,
  announcement,
  onModeChange,
  onRun,
}: {
  activeModelId: string;
  activeModel: EnhancementModel | null | undefined;
  mode: DevTranscriptExecutionMode;
  running: boolean;
  result: DevTranscriptEnhancementResult | null;
  error: string | null;
  announcement: string;
  onModeChange: (mode: DevTranscriptExecutionMode) => void;
  onRun: () => void;
}) {
  const panelId = `candidate-test-${activeModelId || "none"}`;
  const helperId = `${panelId}-helper`;
  const errorId = `${panelId}-error`;
  const candidateReady =
    activeModel?.id === activeModelId &&
    activeModel.source === "candidate" &&
    activeModel.artifact_available === true &&
    activeModel.selectable !== false;
  const catalogResolved = activeModel !== undefined;
  const unavailable = catalogResolved && !candidateReady;
  const visibleError = error || (unavailable ? CANDIDATE_UNAVAILABLE : null);
  const controlsDisabled = running || !candidateReady;

  return (
    <section
      role="region"
      aria-labelledby={`${panelId}-title`}
      aria-describedby={`${helperId}${visibleError ? ` ${errorId}` : ""}`}
      aria-busy={running}
      className="h-full rounded-[var(--radius-row)] p-4 space-y-3"
      style={{
        background: "var(--surface-raised)",
        border: "1px solid var(--border-hairline)",
        boxShadow: "var(--shadow-glass-sm)",
      }}
    >
      <div className="space-y-1">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h3
              id={`${panelId}-title`}
              className="text-[13px] font-medium text-slate"
            >
              Candidate test
            </h3>
            <p className="mt-0.5 break-all text-[11px] text-steel">
              Active: {activeModelId || "None"}
            </p>
          </div>
          {result && (
            <span className="shrink-0 text-[10px] font-medium uppercase tracking-wider text-mint">
              Complete
            </span>
          )}
        </div>
        <p id={helperId} className="text-[11px] leading-relaxed text-steel">
          Uses this transcript&apos;s original text. Switch models in Enhancement
          to compare another candidate.
        </p>
      </div>

      <ContractSpine
        activeModelId={activeModelId}
        activeModel={activeModel}
        mode={mode}
        running={running}
        result={result}
      />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <CandidateModeSelector
          value={mode}
          disabled={controlsDisabled}
          onChange={onModeChange}
        />
        <PrimaryButton
          type="button"
          disabled={controlsDisabled}
          loading={running}
          aria-busy={running}
          onClick={onRun}
          className="h-9 px-4 text-[12px]"
        >
          {running ? "Testing..." : "Run with active model"}
        </PrimaryButton>
      </div>

      {visibleError && (
        <p
          id={errorId}
          className="text-[11px] leading-relaxed text-rose"
          aria-live="polite"
        >
          {visibleError}
        </p>
      )}

      {result && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-3">
            <span className="text-[11px] uppercase tracking-wider text-steel">
              Candidate output
            </span>
            <span className="text-[10px] text-steel/60">
              {`${result.profile_mode[0].toUpperCase()}${result.profile_mode.slice(
                1,
              )} profile · ${result.chunk_count} ${
                result.chunk_count === 1 ? "chunk" : "chunks"
              }`}
            </span>
          </div>
          <ReadingWell
            maxHeight={220}
            ariaLabel="Candidate output"
            fontSize={13}
            fontFamily="var(--font-quote)"
            fontWeight={400}
          >
            {result.output}
          </ReadingWell>
        </div>
      )}

      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Transcript card
// ---------------------------------------------------------------------------

function TranscriptCard({
  item,
  animateEntrance,
  entranceDelay,
  reducedMotion,
  expanded,
  onToggle,
  onCopy,
  onDelete,
  experimentalEnabled,
  bakeoffEnabled,
  bakeoffActive,
  onOpenBakeoff,
  devEnabled,
  activeModelId,
  activeModel,
}: {
  item: TranscriptListItem;
  /** Play the entrance (mount) animation. False on search / live-refresh so the
   *  list reconciles instantly with no re-stagger. */
  animateEntrance: boolean;
  /** Stagger delay (seconds) when animateEntrance is true. */
  entranceDelay: number;
  reducedMotion: boolean;
  /** This card is the single open one in the accordion. */
  expanded: boolean;
  /** Toggle this card open/closed (parent enforces single-open). */
  onToggle: () => void;
  /** Copy the given text (parent writes clipboard + announces "Copied"). */
  onCopy: (text: string) => void;
  onDelete: () => void;
  experimentalEnabled: boolean;
  bakeoffEnabled: boolean;
  bakeoffActive: boolean;
  onOpenBakeoff: (
    transcriptId: string,
    trigger: HTMLButtonElement,
  ) => void;
  devEnabled: boolean;
  activeModelId: string;
  activeModel: EnhancementModel | null | undefined;
}) {
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const provenanceTooltipId = `transcript-provenance-${useId()}`;
  // What the header Copy copies while expanded. CardDetail reports the
  // currently shown full transcript and version through reportShownText.
  const shownTextRef = useRef<string>(item.snippet);
  const [shownVersion, setShownVersion] = useState<"enhanced" | "original">(
    item.has_enhanced ? "enhanced" : "original",
  );
  const reportShownText = useCallback((t: string, version: "enhanced" | "original") => {
    shownTextRef.current = t;
    setShownVersion(version);
  }, []);
  // Candidate-test state intentionally lives at card level. CardDetail
  // unmounts when the accordion collapses, while the ephemeral result should
  // survive a collapse/reopen for as long as this list row remains mounted.
  const [candidateMode, setCandidateMode] =
    useState<DevTranscriptExecutionMode>("whole");
  const [candidateRunning, setCandidateRunning] = useState(false);
  const [candidateResult, setCandidateResult] =
    useState<DevTranscriptEnhancementResult | null>(null);
  const [candidateError, setCandidateError] = useState<string | null>(null);
  const [candidateAnnouncement, setCandidateAnnouncement] = useState("");
  const candidateRequestEpoch = useRef(0);
  const [reenhancementRunning, setReenhancementRunning] = useState(false);
  const [reenhancementResult, setReenhancementResult] =
    useState<TranscriptReenhancementResult | null>(null);
  const [reenhancementError, setReenhancementError] = useState<string | null>(null);
  const reenhancementRequestEpoch = useRef(0);

  useEffect(() => {
    candidateRequestEpoch.current += 1;
    setCandidateRunning(false);
    setCandidateResult(null);
    setCandidateError(null);
    setCandidateAnnouncement("");
  }, [activeModelId, devEnabled]);

  const runCandidateTest = useCallback(async () => {
    if (!devEnabled || candidateRunning || !activeModelId) return;
    const requestEpoch = candidateRequestEpoch.current + 1;
    candidateRequestEpoch.current = requestEpoch;
    setCandidateRunning(true);
    setCandidateError(null);
    setCandidateAnnouncement("");
    try {
      const next = await enhanceDevTranscript(
        item.id,
        activeModelId,
        candidateMode,
      );
      if (candidateRequestEpoch.current !== requestEpoch) return;
      setCandidateResult(next);
      setCandidateAnnouncement(
        `Candidate test complete for ${next.model_id}.`,
      );
    } catch (error) {
      if (candidateRequestEpoch.current !== requestEpoch) return;
      const message = candidateFailureCopy(error, activeModelId);
      setCandidateError(message);
      // The visible error owns aria-live. Keep the separate completion
      // announcer empty so assistive technology does not hear the same error
      // twice.
      setCandidateAnnouncement("");
    } finally {
      if (candidateRequestEpoch.current === requestEpoch) {
        setCandidateRunning(false);
      }
    }
  }, [
    activeModelId,
    candidateMode,
    candidateRunning,
    devEnabled,
    item.id,
  ]);

  const runReenhancement = useCallback(async () => {
    if (!experimentalEnabled || reenhancementRunning) return;
    if (!expanded) onToggle();
    const requestEpoch = reenhancementRequestEpoch.current + 1;
    reenhancementRequestEpoch.current = requestEpoch;
    setReenhancementRunning(true);
    setReenhancementResult(null);
    setReenhancementError(null);
    try {
      const next = await reenhanceTranscript(item.id);
      if (reenhancementRequestEpoch.current !== requestEpoch) return;
      setReenhancementResult(next);
    } catch (error) {
      if (reenhancementRequestEpoch.current !== requestEpoch) return;
      setReenhancementError(reenhancementFailureCopy(error));
    } finally {
      if (reenhancementRequestEpoch.current === requestEpoch) {
        setReenhancementRunning(false);
      }
    }
  }, [
    expanded,
    experimentalEnabled,
    item.id,
    onToggle,
    reenhancementRunning,
  ]);

  // Fetch the full (decrypted) record when the card opens, then cache it for the
  // card's lifetime so re-opening is instant. We gate the expanded body on this
  // loaded record (below) rather than mounting a "Loading..." placeholder: the
  // backend decrypt is only ~5ms, so the expand still feels instant, but the
  // open animation now measures the FINAL height and runs once. Previously the
  // body mounted while loading, the height animation measured the tiny
  // placeholder, and the text + waveform jumped in when the fetch resolved
  // ("the header shows, then the rest pops in").
  const [record, setRecord] = useState<TranscriptRecord | null>(null);
  const [detailFailed, setDetailFailed] = useState(false);
  const recordRequestRef = useRef<Promise<TranscriptRecord> | null>(null);
  const loadRecord = useCallback(() => {
    if (record) return Promise.resolve(record);
    if (!recordRequestRef.current) {
      recordRequestRef.current = getTranscript(item.id)
        .then((next) => {
          setRecord(next);
          return next;
        })
        .finally(() => {
          recordRequestRef.current = null;
        });
    }
    return recordRequestRef.current;
  }, [item.id, record]);
  useEffect(() => {
    if (!expanded || record || detailFailed) return;
    let cancelled = false;
    loadRecord()
      .catch((err) => {
        if (cancelled) return;
        console.error(`[Transcripts] detail fetch failed error_type=${errorType(err)}`);
        setDetailFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [expanded, record, detailFailed, loadRecord]);
  // The expanded body mounts only once the record is loaded (or the fetch failed).
  const bodyReady = expanded && (record !== null || detailFailed);

  useEffect(() => {
    return () => {
      if (copyTimer.current) clearTimeout(copyTimer.current);
    };
  }, []);

  // Scroll an opened card into view so a card near the bottom never expands
  // off-screen (block: "nearest" leaves an already-visible card put). Keyed on
  // bodyReady so the scroll fires once the content is actually mounted.
  useEffect(() => {
    if (!bodyReady || !cardRef.current) return;
    cardRef.current.scrollIntoView({
      block: "nearest",
      behavior: reducedMotion ? "auto" : "smooth",
    });
  }, [bodyReady, reducedMotion]);

  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const fullRecord = record ?? (await loadRecord());
      const text = expanded && record
        ? shownTextRef.current
        : fullRecord.enhanced_text || fullRecord.raw_text;
      onCopy(text);
      setCopied(true);
      if (copyTimer.current) clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => setCopied(false), 1200);
    } catch (err) {
      console.error(`[Transcripts] copy fetch failed error_type=${errorType(err)}`);
    }
  };

  const timestampLabel = formatTimestamp(item.created_at);
  const collapsedVersion = item.has_enhanced ? "enhanced" : "original";
  const collapsedProvenanceTooltip = transcriptProvenanceTooltip(
    item.enhancement_provenance,
    item.has_enhanced,
  );
  const showCollapsedVersionChip =
    item.has_enhanced || collapsedProvenanceTooltip !== null;
  const copyVersion = expanded ? shownVersion : collapsedVersion;

  return (
    <motion.div
      ref={cardRef}
      data-transcript-card
      // initial=false renders straight at the resting state (no mount animation)
      // when we don't want an entrance — that's what makes search snappy.
      initial={animateEntrance ? { opacity: 0, y: 8 } : false}
      animate={{ opacity: 1, y: 0 }}
      transition={
        animateEntrance
          ? { delay: entranceDelay, type: "spring", stiffness: 400, damping: 30 }
          : { duration: 0 }
      }
      className="group/transcript-card relative rounded-[var(--radius-row)] px-4 py-3 transition-colors duration-200"
      style={{
        background: expanded ? "var(--surface-tile-expanded)" : "var(--surface-tile)",
        // Defined edge + frost lift so each entry reads as a distinct tile
        // against the light panel (the old 0.35 border on 0.08 fill was nearly
        // invisible, so adjacent cards blurred into one another).
        border: "1px solid var(--border-hairline)",
        boxShadow: "var(--shadow-glass-sm)",
      }}
      onMouseEnter={(e) => {
        if (!expanded) e.currentTarget.style.background = "var(--surface-tile-hover)";
      }}
      onMouseLeave={(e) => {
        if (!expanded) e.currentTarget.style.background = "var(--surface-tile)";
      }}
    >
      {/* Header = the accordion toggle. */}
      <div
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        aria-describedby={
          !expanded && collapsedProvenanceTooltip
            ? provenanceTooltipId
            : undefined
        }
        aria-label={
          expanded
            ? `Collapse transcript from ${timestampLabel}`
            : `Open transcript from ${timestampLabel}`
        }
        onClick={onToggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}
        className="group/transcript-header cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-frost rounded-[var(--radius-input)]"
      >
        {/* Meta row: just the timestamp (left-aligned); pr-28 keeps a long
            timestamp clear of the top-right actions. Duration lives only in the
            waveform player now (it read as redundant in the card header). */}
        {/* pr-32 (128px) clears the four-action rail: 4 x 24px + 3 x 4px gap +
            the rail's own right-3 inset, so a long timestamp can never collide
            with the Bakeoff fire icon. */}
        <div className="flex items-center gap-2 pr-32">
          <span className="text-[11px] text-steel/60">{timestampLabel}</span>
          {!expanded && showCollapsedVersionChip && (
            <TranscriptVersionChip
              version={collapsedVersion}
              subtle
              provenanceTooltip={collapsedProvenanceTooltip}
              tooltipId={provenanceTooltipId}
              focusVisibleClassName="group-focus-visible/transcript-header:opacity-100"
            />
          )}
        </div>

        {/* Snippet shows only while collapsed; the expanded body replaces it.
            font-quote (Inter) matches the expanded transcript so there's no
            font shift on open. */}
        {!expanded && (
          <p className="font-quote font-normal text-[13px] text-slate leading-relaxed line-clamp-2 mt-1.5">
            {item.snippet}
          </p>
        )}
      </div>

      {/* Experimental actions, Copy, and Delete, top-right. Visible when
          expanded; hover-revealed when collapsed. */}
      <div
        className={`absolute top-3 right-3 flex items-center gap-1 transition-opacity duration-200 ${
          expanded
            ? "opacity-100"
            : "opacity-0 group-hover/transcript-card:opacity-100 focus-within:opacity-100"
        }`}
      >
        {bakeoffEnabled && (
          <button
            onClick={(event) => {
              event.stopPropagation();
              onOpenBakeoff(item.id, event.currentTarget);
            }}
            aria-label="Open enhancement bakeoff"
            aria-haspopup="dialog"
            aria-expanded={bakeoffActive}
            data-bakeoff-trigger-active={bakeoffActive ? "true" : undefined}
            className="w-6 h-6 flex items-center justify-center rounded-full text-steel hover:text-frost cursor-pointer"
          >
            <FireIcon />
          </button>
        )}
        {experimentalEnabled && (
          <button
            onClick={(event) => {
              event.stopPropagation();
              void runReenhancement();
            }}
            disabled={reenhancementRunning}
            aria-label={
              reenhancementRunning
                ? "Re-enhancing transcript"
                : "Re-enhance transcript"
            }
            className="w-6 h-6 flex items-center justify-center rounded-full text-steel hover:text-frost disabled:cursor-default disabled:text-steel/40 cursor-pointer"
          >
            <span className={reenhancementRunning ? "animate-pulse" : undefined}>
              <SparklesIcon />
            </span>
          </button>
        )}
        <button
          onClick={(event) => void handleCopy(event)}
          aria-label={`Copy ${copyVersion} transcript`}
          className="w-6 h-6 flex items-center justify-center rounded-full text-steel hover:text-slate cursor-pointer"
        >
          {copied ? <CheckIcon /> : <CopyGlyph />}
        </button>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          aria-label="Delete transcript"
          className="w-6 h-6 flex items-center justify-center rounded-full text-steel hover:text-rose cursor-pointer"
        >
          <TrashIcon />
        </button>
      </div>

      {/* Expanded body: only ONE card animates at a time (single-open). Mounts
          only once the record is loaded, so the height animation runs once to
          the final size (see the fetch comment above). */}
      <AnimatePresence initial={false}>
        {bodyReady && (
          <motion.div
            key="body"
            initial={
              reducedMotion ? { height: "auto", opacity: 1 } : { height: 0, opacity: 0 }
            }
            animate={{ height: "auto", opacity: 1 }}
            exit={
              reducedMotion ? { height: "auto", opacity: 1 } : { height: 0, opacity: 0 }
            }
            transition={
              reducedMotion ? { duration: 0 } : { duration: 0.22, ease: [0.2, 0, 0, 1] }
            }
            style={{ overflow: "hidden" }}
          >
            <div className="pt-3">
              {record ? (
                <CardDetail
                  record={record}
                  onShownText={reportShownText}
                  provenanceTooltipId={provenanceTooltipId}
                  reenhancementPanel={
                    experimentalEnabled ? (
                      <ReenhancementPanel
                        running={reenhancementRunning}
                        result={reenhancementResult}
                        error={reenhancementError}
                      />
                    ) : undefined
                  }
                  candidatePanel={
                    devEnabled ? (
                      <CandidateTestPanel
                        activeModelId={activeModelId}
                        activeModel={activeModel}
                        mode={candidateMode}
                        running={candidateRunning}
                        result={candidateResult}
                        error={candidateError}
                        announcement={candidateAnnouncement}
                        onModeChange={setCandidateMode}
                        onRun={runCandidateTest}
                      />
                    ) : undefined
                  }
                />
              ) : (
                <p className="text-[12px] text-steel">
                  This transcript could not be opened.
                </p>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Empty / disabled states (centered shell, Dictionary EmptyState idiom)
// ---------------------------------------------------------------------------

function ImportCtaPill({ onClick }: { onClick: () => void }) {
  return (
    <SecondaryButton
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-full px-3 h-9 text-[13px] font-medium text-slate"
    >
      <PlusIcon /> Add file
    </SecondaryButton>
  );
}

function EmptyState({
  reducedMotion,
  onImport,
}: {
  reducedMotion: boolean;
  onImport: () => void;
}) {
  const breathingAnimation = reducedMotion ? {} : { scale: [1, 1.02, 1] };
  const breathingTransition = reducedMotion
    ? {}
    : { duration: 4, repeat: Infinity, ease: "easeInOut" as const };

  return (
    <div className="flex flex-col items-center justify-center pt-4 gap-6">
      <motion.div
        animate={breathingAnimation}
        transition={breathingTransition}
        className="w-[120px] h-[120px] rounded-[var(--radius-card)] flex items-center justify-center text-steel/50"
        style={{
          background: "var(--surface-raised)",
          border: "1px solid var(--border-hairline)",
          boxShadow: "var(--shadow-glass-sm)",
        }}
      >
        <WaveformGlyph />
      </motion.div>
      <div className="text-center">
        <h2 className="font-display text-[18px] font-medium text-slate mb-2">
          No transcripts yet
        </h2>
        <p className="text-[12px] text-steel leading-relaxed max-w-[360px]">
          Your dictations will show up here as you use Whisperstream. Or add an
          audio file to transcribe.
        </p>
      </div>
      <ImportCtaPill onClick={onImport} />
    </div>
  );
}

function SecurityUnavailableState({
  availability,
}: {
  availability: "key-error" | "not-decryptable" | "unavailable";
}) {
  const message =
    availability === "unavailable"
      ? "Protected transcript storage is unavailable right now."
      : availability === "key-error"
        ? "The transcript encryption key is unavailable for this device account."
        : "Saved transcript data could not be decrypted.";
  return (
    <div
      role="status"
      className="rounded-[var(--radius-row)] px-4 py-3 text-[12px] text-steel"
      style={{
        background: "var(--surface-raised)",
        border: "1px solid var(--border-hairline)",
      }}
    >
      {message} Whisperstream will not replace it with an empty history.
    </div>
  );
}

function DisabledState({
  historyDisabled,
  firstMacOptIn = false,
  reducedMotion,
  onReEnable,
  lockdownNoEnvelope,
  onRequestLockdownSetup,
}: {
  historyDisabled: boolean;
  firstMacOptIn?: boolean;
  reducedMotion: boolean;
  onReEnable: () => void;
  /** True when Lockdown Mode is on but no password envelope exists yet (the
   *  history-off lockdown). Saving history is impossible in this state (the
   *  backend rejects a raw history_enabled:true write, F5), so the one-click
   *  re-enable toggle is replaced with a "Set up password" action that routes
   *  to Lockdown setup instead. */
  lockdownNoEnvelope: boolean;
  onRequestLockdownSetup?: () => void;
}) {
  const breathingAnimation = reducedMotion ? {} : { scale: [1, 1.02, 1] };
  const breathingTransition = reducedMotion
    ? {}
    : { duration: 4, repeat: Infinity, ease: "easeInOut" as const };

  return (
    <div className="flex flex-col items-center justify-center pt-4 gap-6">
      <motion.div
        animate={breathingAnimation}
        transition={breathingTransition}
        className="w-[120px] h-[120px] rounded-[var(--radius-card)] flex items-center justify-center text-steel/40"
        style={{
          background: "var(--surface-control)",
          border: "1px solid var(--border-standard)",
          boxShadow: "var(--shadow-glass-sm)",
        }}
      >
        {/* Pause glyph distinguishes "deliberately off" from "nothing yet". */}
        <svg
          width="44"
          height="44"
          viewBox="0 0 44 44"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="22" cy="22" r="15" />
          <path d="M18 17v10M26 17v10" />
        </svg>
      </motion.div>
      <div className="text-center">
        <h2 className="font-display text-[18px] font-medium text-slate mb-2">
          {historyDisabled ? "History is disabled" : lockdownNoEnvelope ? "Set a password to keep saving" : firstMacOptIn ? "Transcript history is off" : "Saving is paused"}
        </h2>
        <p className="text-[12px] text-steel leading-relaxed max-w-[360px]">
          {historyDisabled
            ? `${HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE} You can still dictate without saving a history.`
            : lockdownNoEnvelope
            ? "Lockdown Mode keeps saved transcripts locked behind a password. Set one to start saving again."
            : firstMacOptIn
              ? "Choose whether to keep an encrypted history on this Mac. Dictation works with saving off."
              : "New dictations are not being saved right now. Turn Save transcripts back on to start keeping them again."}
        </p>
      </div>
      {historyDisabled ? null : lockdownNoEnvelope ? (
        // No envelope exists, so there is nothing to toggle on (the backend
        // rejects a raw history_enabled:true write here). Route to the
        // Lockdown setup flow instead of a re-enable toggle.
        <SecondaryButton
          onClick={() => onRequestLockdownSetup?.()}
          className="inline-flex items-center gap-1.5 rounded-full px-4 h-9 text-[13px] font-medium text-slate"
        >
          Set up password
        </SecondaryButton>
      ) : (
        // Surface the Save transcripts toggle directly (one-click re-enable).
        <div
          className="flex items-center gap-3 rounded-full px-4 py-2"
          style={{
            background: "var(--surface-control)",
            border: "1px solid var(--border-hairline)",
          }}
        >
          <span className="text-[13px] text-slate">Save transcripts</span>
          <Toggle checked={false} onChange={() => onReEnable()} ariaLabel="Save transcripts" />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// List view
// ---------------------------------------------------------------------------

interface ListViewProps {
  historyDisabled: boolean;
  isMac: boolean;
  config: AppConfig;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  onOpenImport: () => void;
  onRetentionChange: (days: number) => void;
  onClearAll: () => void;
  onDelete: (id: string) => void;
  /** True when Lockdown Mode is on but no password envelope exists (F5). Save
   *  transcripts must not be turned on directly in this state. */
  lockdownNoEnvelope: boolean;
  /** Routes the user to Lockdown setup (Home, auto-opens the setup modal) so
   *  they can set a transcript password instead of a blocked raw enable. */
  onRequestLockdownSetup?: () => void;
  /** Turn saving on through the page's setup dialog (Mac and Windows). */
  onRequestEnable: () => void;
  /** Inline confirmation under the controls row (setup done, restored). */
  notice?: ReactNode;
  /** The list reported a missing key; the page shows the restore screen. */
  onKeyMissing?: () => void;
  /** Opens recovery settings from the gear menu; absent when unavailable. */
  onRecoverySettings?: () => void;
  experimentalEnabled: boolean;
  bakeoffEnabled: boolean;
  bakeoffActiveTranscriptId: string | null;
  onOpenBakeoff: (
    transcriptId: string,
    trigger: HTMLButtonElement,
  ) => void;
  devEnabled: boolean;
  activeModelId: string;
  activeModel: EnhancementModel | null | undefined;
}

function ListView({
  historyDisabled,
  isMac,
  config,
  onConfigChange,
  subscribe,
  onOpenImport,
  onRetentionChange,
  onClearAll,
  onDelete,
  lockdownNoEnvelope,
  onRequestLockdownSetup,
  onRequestEnable,
  notice,
  onKeyMissing,
  onRecoverySettings,
  experimentalEnabled,
  bakeoffEnabled,
  bakeoffActiveTranscriptId,
  onOpenBakeoff,
  devEnabled,
  activeModelId,
  activeModel,
}: ListViewProps) {
  const reducedMotion = useReducedMotion() ?? false;

  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [items, setItems] = useState<TranscriptListItem[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [availability, setAvailability] = useState<
    NonNullable<TranscriptListResponse["availability"]>
  >("empty");
  // Index at which an appended (load-more) page begins, so only the new rows get
  // their entrance and the existing rows do not re-stagger. -1 = first page.
  const [appendFrom, setAppendFrom] = useState(-1);
  // Entrance-animation mode for the current items render:
  //   "initial" = first load (stagger every row in),
  //   "append"  = load-more (only the appended rows animate),
  //   "none"    = search / live-refresh (rows reconcile in place with NO
  //               entrance animation, so typing in the search box is instant).
  const [animMode, setAnimMode] = useState<"initial" | "append" | "none">("initial");
  // Whether the first page has ever loaded. After it has, searches and live
  // refreshes swap items in place (stale-while-revalidate) instead of flipping
  // `loading` and tearing the whole list down — the teardown was what made every
  // keystroke replay the entrance stagger, even when the results were identical.
  const hasLoadedRef = useRef(false);
  const [copyAnnounce, setCopyAnnounce] = useState("");
  // The single expanded card (accordion, single-open). Opening one closes the
  // other for free, since only the card whose id matches renders its body.
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const toggleExpand = (id: string) =>
    setExpandedId((cur) => (cur === id ? null : id));

  const needsHistoryOptIn = isMac && config.mac_history_opt_in !== true;
  const requestHistoryChange = (enabled: boolean) => {
    if (historyDisabled) return;
    if (enabled && lockdownNoEnvelope) {
      onRequestLockdownSetup?.();
      return;
    }
    if (enabled) {
      onRequestEnable();
      return;
    }
    onConfigChange({ history_enabled: false });
  };
  const historyOn = !historyDisabled && config.history_enabled;

  // Debounce the search query (~200ms) before it drives the q= fetch.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [query]);

  const fetchFirstPage = useCallback(
    (q: string) => {
      // Only the very FIRST load shows the loading teardown. Later searches and
      // live refreshes keep the current rows on screen and swap in the new ones
      // when they arrive, so the list never unmounts and re-staggers.
      if (!hasLoadedRef.current) setLoading(true);
      listTranscripts({ limit: PAGE_SIZE, q: q || undefined })
        .then((res) => {
          // First load staggers in; every later result set reconciles silently.
          setAnimMode(hasLoadedRef.current ? "none" : "initial");
          setAppendFrom(-1);
          setItems(res.items);
          setNextCursor(res.next_cursor);
          setAvailability(res.availability ?? (res.decryptable === false ? "not-decryptable" : "ready"));
          hasLoadedRef.current = true;
        })
        .catch((err) => {
          console.error(`[Transcripts] list failed error_type=${errorType(err)}`);
          setAnimMode("none");
          setItems([]);
          setNextCursor(null);
          setAvailability("unavailable");
          hasLoadedRef.current = true;
        })
        .finally(() => setLoading(false));
    },
    [],
  );

  // Fetch page 1 on mount and whenever the debounced search changes.
  useEffect(() => {
    fetchFirstPage(debouncedQuery);
  }, [debouncedQuery, fetchFirstPage]);

  // Live refresh: any transcripts.changed event refetches page 1 (after a
  // delete / clear / import landing). Keeps the current search applied.
  useEffect(() => {
    const unsub = subscribe((event) => {
      if (event.type === "transcripts.changed") {
        fetchFirstPage(debouncedQuery);
      }
    });
    return unsub;
  }, [subscribe, fetchFirstPage, debouncedQuery]);

  const handleLoadMore = () => {
    if (nextCursor == null || loadingMore) return;
    setLoadingMore(true);
    listTranscripts({ before: nextCursor, limit: PAGE_SIZE, q: debouncedQuery || undefined })
      .then((res) => {
        const nextAvailability =
          res.availability ??
          (res.decryptable === false ? "not-decryptable" : "ready");
        setAvailability(nextAvailability);
        if (nextAvailability !== "ready") {
          setAnimMode("none");
          setAppendFrom(-1);
          setItems([]);
          setNextCursor(null);
          setExpandedId(null);
          return;
        }
        setAnimMode("append");
        setAppendFrom(items.length);
        setItems((prev) => [...prev, ...res.items]);
        setNextCursor(res.next_cursor);
      })
      .catch((err) => {
        console.error(`[Transcripts] load-more failed error_type=${errorType(err)}`);
      })
      .finally(() => setLoadingMore(false));
  };

  const handleCopy = (text: string) => {
    // Card-header Copy passes the complete displayed version, whether the card
    // is collapsed or expanded. Copy exactly what was passed.
    navigator.clipboard?.writeText(text).catch(() => {
      /* clipboard unavailable; aria-live still announces optimistically */
    });
    // aria-live announcement (LOCKED: "Copied"). Reset first so repeat copies
    // re-announce.
    setCopyAnnounce("");
    requestAnimationFrame(() => setCopyAnnounce("Copied"));
  };

  // Reset the open card when the query changes (it may be filtered out).
  useEffect(() => {
    setExpandedId(null);
  }, [debouncedQuery]);

  // After any refetch (delete / clear / import), drop a dangling expandedId that
  // is no longer in the list so the accordion state can't point at a missing row.
  useEffect(() => {
    if (expandedId && !items.some((it) => it.id === expandedId)) {
      setExpandedId(null);
    }
  }, [items, expandedId]);

  // Esc collapses the open card (the import view keeps its own Esc-to-list).
  useEffect(() => {
    if (!expandedId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setExpandedId(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [expandedId]);

  // A missing key belongs to the page-level restore screen, not this list.
  useEffect(() => {
    if (availability === "key-missing") onKeyMissing?.();
  }, [availability, onKeyMissing]);

  const showEmpty = !loading && items.length === 0 && historyOn;
  const showDisabled = !historyOn;
  const keyMissing = availability === "key-missing";
  const securityUnavailable = ["key-error", "not-decryptable", "unavailable"].includes(
    availability,
  );

  return (
    <div className="space-y-4">
      {/* Header row: title + subtitle + Encrypted badge. */}
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30 }}
        className="flex items-start justify-between gap-3"
      >
        <div>
          <h2 className="text-[18px] font-medium text-slate mb-1">Transcripts</h2>
          <p className="text-[12px] text-steel leading-relaxed">
            Your past dictations, kept on this device and searchable.
          </p>
        </div>

        <HoverTooltip
          side="auto"
          width="w-64"
          content="Saved transcript text and audio are encrypted on disk and protected for this device account."
        >
          <span
            className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-steel cursor-default"
            style={{
              background: "var(--surface-control)",
              border: "1px solid var(--border-hairline)",
            }}
          >
            <ShieldIcon />
            <span className="text-[10px] font-medium uppercase tracking-wider">
              Encrypted
            </span>
          </span>
        </HoverTooltip>
      </motion.div>

      {/* Controls row (Option B, lean): search (flex-grow) | import pill | gear. */}
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.02, type: "spring", stiffness: 400, damping: 30 }}
        className="flex items-center gap-2"
      >
        <div
          className="frost-field-group flex items-center gap-2 flex-1 min-w-[200px] rounded-[var(--radius-input)] px-3 h-9"
          style={{
            background: "var(--surface-control)",
            border: "1px solid var(--border-hairline)",
          }}
        >
          <SearchIcon />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search transcripts..."
            aria-label="Search transcripts"
            className="flex-1 min-w-0 bg-transparent text-[13px] text-slate outline-none placeholder:text-steel/40"
          />
        </div>

        <SecondaryButton
          onClick={onOpenImport}
          className="inline-flex items-center gap-1.5 rounded-full px-3 h-9 text-[13px] font-medium text-slate shrink-0"
        >
          <PlusIcon /> Add file
        </SecondaryButton>

        <SettingsPopover
          historyEnabled={historyOn}
          historyDisabled={historyDisabled}
          onHistoryChange={requestHistoryChange}
          saveAudio={!historyDisabled && config.save_audio}
          onSaveAudioChange={(v) => { if (!historyDisabled) onConfigChange({ save_audio: v }); }}
          retentionDays={config.transcript_retention_days}
          onRetentionChange={onRetentionChange}
          onClearAll={onClearAll}
          onRecoverySettings={onRecoverySettings}
        />
      </motion.div>

      {/* aria-live region for the copy-success announcement (visual feedback is
          the check-swap on the card; this gives non-visual users parity). */}
      <div aria-live="polite" className="sr-only">
        {copyAnnounce}
      </div>

      {notice}

      {/* Body: disabled / empty / card list. When history is OFF we show the
          paused shell but STILL list any existing records below it. */}
      {showDisabled && (
        <DisabledState
          historyDisabled={historyDisabled}
          firstMacOptIn={needsHistoryOptIn}
          reducedMotion={reducedMotion}
          onReEnable={() => requestHistoryChange(true)}
          lockdownNoEnvelope={lockdownNoEnvelope}
          onRequestLockdownSetup={onRequestLockdownSetup}
        />
      )}

      {!loading && historyOn && securityUnavailable && (
        <SecurityUnavailableState
          availability={availability as "key-error" | "not-decryptable" | "unavailable"}
        />
      )}

      {showEmpty && !securityUnavailable && !keyMissing && (
        <EmptyState reducedMotion={reducedMotion} onImport={onOpenImport} />
      )}

      {!loading && items.length > 0 && (
        <div className="space-y-2.5">
          {items.map((item, i) => {
            // Animate the entrance only on the initial load (stagger all) and on
            // load-more (stagger only the appended rows). On search / refresh
            // (animMode "none") rows reconcile in place with no animation, so
            // filtering feels instant. Rows that persist across an update keep
            // their key and never remount, so they don't re-animate regardless.
            const isAppended =
              animMode === "append" && appendFrom >= 0 && i >= appendFrom;
            const animateEntrance =
              !reducedMotion && (animMode === "initial" || isAppended);
            const entranceDelay = animateEntrance
              ? (isAppended ? i - appendFrom : i) * 0.03
              : 0;
            return (
              <TranscriptCard
                key={item.id}
                item={item}
                animateEntrance={animateEntrance}
                entranceDelay={entranceDelay}
                reducedMotion={reducedMotion}
                expanded={item.id === expandedId}
                onToggle={() => toggleExpand(item.id)}
                onCopy={handleCopy}
                onDelete={() => onDelete(item.id)}
                experimentalEnabled={experimentalEnabled}
                bakeoffEnabled={bakeoffEnabled}
                bakeoffActive={item.id === bakeoffActiveTranscriptId}
                onOpenBakeoff={onOpenBakeoff}
                devEnabled={devEnabled}
                activeModelId={activeModelId}
                activeModel={activeModel}
              />
            );
          })}

          {nextCursor != null && (
            // pb-1 gives the button breathing room so its bottom edge is never
            // clipped against the scroll container's bottom.
            <div className="flex justify-center pt-1 pb-1">
              <SecondaryButton
                onClick={handleLoadMore}
                disabled={loadingMore}
                className="inline-flex items-center justify-center rounded-full px-5 h-9 text-[13px] font-medium text-slate"
                style={{ opacity: loadingMore ? 0.6 : 1 }}
              >
                {loadingMore ? "Loading..." : "Load more"}
              </SecondaryButton>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail / Import view stubs (Phase 5 / Phase 6). The back-affordance + swap
// must work here; the bodies are filled in by later tasks.
// ---------------------------------------------------------------------------

function BackLink({ onBack }: { onBack: () => void }) {
  return (
    <button
      onClick={onBack}
      className="inline-flex items-center gap-1 text-[12px] text-steel hover:text-slate transition-colors duration-150 cursor-pointer"
    >
      <BackChevron />
      Back to transcripts
    </button>
  );
}

// ---------------------------------------------------------------------------
// Detail: text block (label + always-visible copy + ReadingWell body)
// ---------------------------------------------------------------------------

function TextBlock({
  label,
  ariaLabel = label,
  ariaDescribedBy,
  labelAccessory,
  text,
  toggle,
}: {
  label: string;
  ariaLabel?: string;
  ariaDescribedBy?: string;
  labelAccessory?: ReactNode;
  text: string;
  /** Optional header affordance (the Show original/enhanced toggle), right-aligned.
   *  The copy button lives at the card-header level now (one Copy per card), so
   *  this block no longer renders its own. */
  toggle?: ReactNode;
}) {
  return (
    <div className="group/transcript-detail space-y-1.5">
      <div className="flex items-center justify-between gap-2 min-h-[24px]">
        <div className="flex items-center gap-2">
          <span className="text-[11px] uppercase tracking-wider text-steel">
            {label}
          </span>
          {labelAccessory}
        </div>
        {toggle && <div className="shrink-0">{toggle}</div>}
      </div>
      {/* Content-sized: grows to fit short transcripts, caps at 220px + scrolls.
          The neutral quoted-content font (Inter) so the transcript reads as the
          user's words through the quoted-content token. 13px: Inter's
          larger x-height reads a touch bigger, and 13 matches the collapsed
          snippet so there's no size shift on expand. */}
      <ReadingWell
        maxHeight={220}
        ariaLabel={ariaLabel}
        ariaDescribedBy={ariaDescribedBy}
        fontSize={13}
        fontFamily="var(--font-quote)"
        fontWeight={400}
      >
        {text}
      </ReadingWell>
    </div>
  );
}

// ---------------------------------------------------------------------------
// CardDetail — the expanded body of a transcript card (round 02: inline, no
// Back link). Presentational: it receives the already-loaded record from the
// parent (TranscriptCard fetches it and gates the open animation on it, so the
// expand runs once to the final size). Renders ONE transcript block (Enhanced
// default + Show original toggle; no toggle when no enhancement), the
// WaveformPlayer or the no-audio note, and a demoted metadata row. It reports
// the currently-shown text up via onShownText so the single card-header Copy
// button copies exactly what is on screen.
// ---------------------------------------------------------------------------

function CardDetail({
  record,
  onShownText,
  provenanceTooltipId,
  reenhancementPanel,
  candidatePanel,
}: {
  record: TranscriptRecord;
  onShownText: (text: string, version: "enhanced" | "original") => void;
  provenanceTooltipId: string;
  reenhancementPanel?: ReactNode;
  candidatePanel?: ReactNode;
}) {
  // Single transcript view: defaults to the enhanced text; the toggle reveals
  // the raw "original". Reset to the default whenever a different record shows.
  // The reset happens during render, not in an effect: a mount-time effect
  // could flush after an early toggle click and silently undo it.
  const [showOriginal, setShowOriginal] = useState(false);
  const [shownRecordId, setShownRecordId] = useState(record.id);
  if (shownRecordId !== record.id) {
    setShownRecordId(record.id);
    setShowOriginal(false);
  }

  // "Save a copy": open a native Save dialog and write a DECRYPTED, playable
  // WAV to the chosen path. The decrypted bytes come from the same Range-aware
  // audio endpoint the <audio> element already streams, so this adds no new
  // exposure. Dynamic imports + try/catch make it a graceful no-op in the Vite
  // dev browser (no Tauri runtime), mirroring useAudioImport. Never log bytes.
  const handleSaveAudio = useCallback(async () => {
    try {
      const filename = defaultAudioFilename(record.created_at);
      if (await supportsMacosPrivateFileWorkflows()) {
        const res = await fetch(transcriptAudioUrl(record.id));
        if (!res.ok) throw new Error(`audio fetch ${res.status}`);
        const bytes = await readBoundedNativeSaveResponse(res);
        await saveMacosPrivateFile("export-transcript", filename, bytes);
        return;
      }
      const { save } = await import("@tauri-apps/plugin-dialog");
      const dest = await save({
        defaultPath: filename,
        filters: [{ name: "Audio", extensions: ["wav"] }],
      });
      if (!dest) return; // user cancelled
      const res = await fetch(transcriptAudioUrl(record.id));
      if (!res.ok) throw new Error(`audio fetch ${res.status}`);
      const bytes = new Uint8Array(await res.arrayBuffer());
      const { writeFile } = await import("@tauri-apps/plugin-fs");
      await writeFile(dest, bytes);
    } catch (err) {
      console.error(`[Transcripts] save audio failed error_type=${errorType(err)}`);
    }
  }, [record]);

  const hasEnhanced = !!record.enhanced_text;
  const showingOriginal = !hasEnhanced || showOriginal;
  const shownText = showingOriginal
    ? record.raw_text
    : record.enhanced_text ?? record.raw_text;

  // Report the on-screen text up so the card-header Copy copies what is shown.
  useEffect(() => {
    onShownText(shownText, showingOriginal ? "original" : "enhanced");
  }, [shownText, showingOriginal, onShownText]);

  const version = showingOriginal ? "original" : "enhanced";
  const ariaLabel = showingOriginal
    ? "Transcript (Original)"
    : "Transcript (Enhanced)";
  // `showOriginal` means the user deliberately switched away from an enhanced
  // result. Its Original chip is a view choice, not an enhancement fallback,
  // so it must never inherit the stored outcome explanation.
  const provenanceTooltip = showOriginal
    ? null
    : transcriptProvenanceTooltip(
        record.enhancement_provenance,
        hasEnhanced,
      );
  const provenanceDescriptionId = provenanceTooltip
    ? provenanceTooltipId
    : undefined;
  const toggle = hasEnhanced ? (
    <button
      onClick={() => setShowOriginal((v) => !v)}
      aria-describedby={provenanceDescriptionId}
      className="text-[11px] text-steel hover:text-slate transition-colors duration-150 cursor-pointer"
    >
      {showingOriginal ? "Show enhanced" : "Show original"}
    </button>
  ) : undefined;

  return (
    <div className="space-y-4">
      <div
        data-testid={candidatePanel ? "candidate-comparison" : undefined}
        className={
          candidatePanel
            ? "grid grid-cols-1 gap-4 min-[1100px]:grid-cols-2"
            : undefined
        }
      >
        <TextBlock
          label="Transcript"
          ariaLabel={ariaLabel}
          ariaDescribedBy={provenanceDescriptionId}
          labelAccessory={
            <TranscriptVersionChip
              version={version}
              provenanceTooltip={provenanceTooltip}
              tooltipId={provenanceTooltipId}
              focusVisibleClassName="group-focus-within/transcript-detail:opacity-100"
            />
          }
          text={shownText}
          toggle={toggle}
        />
        {candidatePanel}
      </div>

      {reenhancementPanel}

      {/* Audio: the WaveformPlayer renders here when has_audio; otherwise the
          LOCKED, cause-agnostic no-audio note. */}
      {record.has_audio ? (
        <div data-testid="waveform-slot">
          <WaveformPlayer
            peaks={record.peaks}
            audioUrl={transcriptAudioUrl(record.id)}
            durationMs={record.duration_ms}
            onSave={handleSaveAudio}
          />
        </div>
      ) : (
        <p className="text-[12px] text-steel">
          No audio was saved for this transcript.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Import view (Phase 6 / Task 6.2): dashed drop zone + "Choose files" picker +
// the supported-format caption + a per-file progress queue driven by the
// `import.progress` WS events. Task 6.1 owns the PATH delivery (native picker +
// drag-drop via useAudioImport); this view renders the drop zone (brightening
// while dragActive) and the queue.
// ---------------------------------------------------------------------------

// Local queue-row status. "waiting" is a UI-only seed state for a file that has
// been added but not yet started; the backend's import.progress events carry
// "transcribing" | "done" | "failed" (see ws.ts), which advance the row.
type QueueStatus = "waiting" | "transcribing" | "done" | "failed";

interface QueueItem {
  /** Stable key + match key: the file basename (import.progress events are
   *  keyed by basename). */
  file: string;
  status: QueueStatus;
  /** Failed-state friendly reason from the event (never a raw exception). */
  reason?: string;
}

// Status pill tokens (LOCKED colors per design). "waiting" reuses the steel
// pill; the labels are the LOCKED copy values.
const STATUS_PILL: Record<
  QueueStatus,
  { label: string; bg: string; color: string }
> = {
  waiting: {
    label: "Waiting",
    bg: "var(--surface-control)",
    color: "var(--color-steel)",
  },
  transcribing: {
    label: "Transcribing",
    bg: "var(--border-accent)",
    color: "var(--color-frost-deep)",
  },
  done: { label: "Done", bg: "rgba(91,175,124,0.14)", color: "#2E6B48" },
  failed: { label: "Failed", bg: "rgba(196,139,139,0.14)", color: "#8B4545" },
};

/** Sublabel by status (LOCKED). done -> "Added to your list"; failed -> the
 *  friendly reason from the event; waiting/transcribing -> none. */
function queueSublabel(item: QueueItem): string | null {
  if (item.status === "done") return "Added to your list";
  if (item.status === "failed") return item.reason || null;
  return null;
}

function QueueRow({
  item,
  onRemove,
}: {
  item: QueueItem;
  onRemove: () => void;
}) {
  const pill = STATUS_PILL[item.status];
  const sublabel = queueSublabel(item);
  return (
    <div
      className="flex items-center gap-3 rounded-[var(--radius-row)] px-4 py-2.5"
      style={{
        background: "var(--surface-raised)",
        border: "1px solid var(--border-hairline)",
      }}
    >
      <div className="flex-1 min-w-0">
        <div className="text-[13px] text-slate truncate">{item.file}</div>
        {sublabel && (
          <div className="text-[11px] text-steel/70 truncate">{sublabel}</div>
        )}
      </div>

      <span
        className="shrink-0 inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[10px] font-medium uppercase tracking-wider"
        style={{ background: pill.bg, color: pill.color }}
      >
        {item.status === "transcribing" && <Spinner />}
        {pill.label}
      </span>

      <button
        onClick={onRemove}
        aria-label={item.status === "transcribing" ? "Cancel" : "Remove"}
        className="shrink-0 w-6 h-6 flex items-center justify-center rounded-full text-steel hover:text-rose cursor-pointer"
      >
        <XIcon />
      </button>
    </div>
  );
}

function ImportView({
  onBack,
  dragActive,
  pickerError,
  importUnavailable,
  onChooseFiles,
  subscribe,
}: {
  onBack: () => void;
  dragActive: boolean;
  pickerError: boolean;
  importUnavailable: boolean;
  onChooseFiles: () => void;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
}) {
  // The queue is keyed by file basename so the import.progress events (also
  // keyed by basename) can find and advance the matching row. A row is created
  // on first sight of a file (its first event is "transcribing") and seeded as
  // "Waiting" if it is known before any event arrives.
  const [queue, setQueue] = useState<QueueItem[]>([]);

  // Drive the queue from import.progress WS events. Each event upserts the row
  // for its file: create it if unseen, otherwise advance the existing row.
  useEffect(() => {
    const unsub = subscribe((event) => {
      if (event.type !== "import.progress") return;
      setQueue((prev) => {
        const idx = prev.findIndex((q) => q.file === event.file);
        const next: QueueItem = {
          file: event.file,
          status: event.status,
          reason: event.reason,
        };
        if (idx === -1) return [...prev, next];
        const copy = prev.slice();
        copy[idx] = next;
        return copy;
      });
    });
    return unsub;
  }, [subscribe]);

  const removeRow = (file: string) =>
    setQueue((prev) => prev.filter((q) => q.file !== file));

  return (
    <div className="space-y-4">
      <BackLink onBack={onBack} />

      {/* Dashed drop zone. Brightens while a native drag is over the window
          (dragActive from useAudioImport, driven by Tauri onDragDropEvent). */}
      <div
        className="rounded-[var(--radius-card)] flex flex-col items-center justify-center text-center gap-3 px-6 py-10 transition-colors duration-150"
        style={{
          background: dragActive
            ? "var(--border-accent)"
            : "var(--surface-raised)",
          border: dragActive
            ? "2px solid var(--color-frost)"
            : "2px dashed var(--border-accent)",
        }}
      >
        <div className="text-steel/60">
          <UploadGlyph />
        </div>
        <p className="text-[14px] font-medium text-slate">
          Drop audio files here
        </p>
        <SecondaryButton
          onClick={onChooseFiles}
          disabled={importUnavailable}
          className="inline-flex items-center justify-center rounded-full px-4 h-9 text-[13px] font-medium text-slate"
        >
          Choose files
        </SecondaryButton>
      </div>

      {/* Supported-format caption (LOCKED). */}
      <p className="text-[11px] text-steel/70 text-center leading-relaxed">
        WAV files always work. Other formats (like MP3 or M4A) may not import on
        every setup.
      </p>

      {pickerError && (
        <p role="alert" className="text-[11px] text-rose text-center">
          Couldn't open the file picker. Try again.
        </p>
      )}
      {importUnavailable && (
        <p role="alert" className="text-[11px] text-rose text-center">
          Saved transcript storage is unavailable. Return to the list for details.
        </p>
      )}

      {/* Per-file progress queue (driven by import.progress events). */}
      {queue.length > 0 && (
        <div className="space-y-2">
          {queue.map((item) => (
            <QueueRow
              key={item.file}
              item={item}
              onRemove={() => removeRow(item.file)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Top-level page: view-state machine (list / detail / import)
// ---------------------------------------------------------------------------

// Round 02: the detail view is gone — transcripts expand inline within the list
// (single-open accordion owned by ListView). Only the import flow is a full swap.
type View = { view: "list" } | { view: "import" };

interface TranscriptsProps {
  config: AppConfig;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  /** Raw subscribe function from useAppSocket(). Used for live list refresh on
   *  transcripts.changed / import.progress events. */
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  /** Live lock status from useLockdown (App.tsx). When `locked` is true the
   *  lock screen overlay (Surface C, Task 5.5) gates all transcript content.
   *  Optional so the page renders safely before App's hook fires. */
  lockStatus?: LockStatus;
  /** Navigate to Home and auto-open the Lockdown setup modal (F5). Used when
   *  the user tries to turn Save transcripts on under a no-envelope lockdown,
   *  where a raw history_enabled:true write would 400. */
  onRequestLockdownSetup?: () => void;
  /** Verified compile-time plus backend development gate from App. When false,
   *  candidate evaluation is structurally absent from every transcript card. */
  devEnabled?: boolean;
  /** Configured local model ID used as the endpoint's optimistic race guard. */
  activeModelId?: string;
  /** Active catalog row resolved by App. Undefined is loading; null is not a
   *  candidate. Kept separate from the ID so stale catalog rows cannot run. */
  activeModel?: EnhancementModel | null;
  capabilities?: DailyUseCapabilityReport | null;
  /** Standard-history recovery status from App. When omitted (dev previews,
   *  tests) the page reads it itself. */
  recovery?: HistoryRecoveryHandle;
  /** Bumped by the one-time launch prompt to open the required setup. */
  recoverySetupRequest?: number;
}

/** Inline mint confirmation under the controls row (copy-review sections 3, 4, 6). */
type RecoveryNotice = "setup-done" | "required-done" | "restored";

function MintNote({ children }: { children: ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 400, damping: 30 }}
      role="status"
      className="flex items-start gap-2 rounded-[var(--radius-row)] px-4 py-2.5 text-[12px] text-slate leading-relaxed"
      style={{
        background: "color-mix(in srgb, var(--color-mint) 10%, transparent)",
        border: "1px solid color-mix(in srgb, var(--color-mint) 30%, transparent)",
      }}
    >
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="var(--color-mint)" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 mt-[2px]" aria-hidden="true">
        <path d="M2.5 7.2l2.8 2.8 6-6.5" />
      </svg>
      <span>{children}</span>
    </motion.div>
  );
}

export function Transcripts({
  config,
  onConfigChange,
  subscribe,
  lockStatus,
  onRequestLockdownSetup,
  devEnabled = false,
  activeModelId = "",
  activeModel,
  capabilities,
  recovery: recoveryProp,
  recoverySetupRequest = 0,
}: TranscriptsProps) {
  const experimentalEnabled = useExperimental();
  const platform = historyPlatform(capabilities?.platform);
  const lockdownOn = (config.lockdown_mode ?? false) || (lockStatus?.enabled ?? false);
  const historyPolicyOff = lockStatus?.historyDisabled === true;
  const ownRecovery = useHistoryRecovery(
    subscribe,
    !recoveryProp && !lockdownOn && !historyPolicyOff,
  );
  const recovery = recoveryProp ?? ownRecovery;
  const refreshRecovery = recovery.refresh;
  const [setupDialog, setSetupDialog] = useState<{
    reason: HistoryRecoverySetupReason;
    passwordProtected: boolean;
  } | null>(null);
  const [notice, setNotice] = useState<RecoveryNotice | null>(null);
  const [startFreshOpen, setStartFreshOpen] = useState(false);
  const recoveryStatus = recovery.status;
  // The protected key is gone, or loads but can't read saved history: restore
  // or Start fresh replaces the tab content.
  const keyMissing =
    !lockdownOn &&
    (recoveryStatus?.key_missing === true || recoveryStatus?.key_mismatch === true);
  // Existing history without a recovery key: only the setup gate shows.
  const needsSetup =
    !lockdownOn && !keyMissing && !historyPolicyOff && recoveryStatus?.needs_setup === true;
  const [settingsOpen, setSettingsOpen] = useState(false);

  // The launch prompt asks for the required setup; open it once status agrees.
  const handledSetupRequest = useRef(0);
  useEffect(() => {
    if (recoverySetupRequest === 0 || recoverySetupRequest === handledSetupRequest.current) return;
    if (!needsSetup) return;
    handledSetupRequest.current = recoverySetupRequest;
    setSetupDialog({ reason: "required", passwordProtected: false });
  }, [recoverySetupRequest, needsSetup]);

  // Turning saving on. Standard history always goes through the setup dialog
  // (intro, then the recovery key when none exists yet). Lockdown keeps its
  // password-protected path: the first Mac opt-in shows the intro, otherwise
  // saving turns on directly as before.
  const requestEnable = () => {
    if (lockdownOn) {
      if (platform === "mac" && config.mac_history_opt_in !== true) {
        setSetupDialog({ reason: "enable", passwordProtected: true });
      } else {
        onConfigChange({ history_enabled: true });
      }
      return;
    }
    setNotice(null);
    setSetupDialog({ reason: "enable", passwordProtected: false });
  };

  const handleRestored = () => {
    setNotice("restored");
    void refreshRecovery();
  };

  // Start fresh deleted the old history. Saving restarts (turning it back on if
  // it was off), then the new recovery key is set up.
  const handleStartFreshDeleted = async () => {
    setStartFreshOpen(false);
    setNotice(null);
    if (!config.history_enabled) {
      try {
        await enableTranscriptHistory(true);
        onConfigChange({ history_enabled: true });
      } catch (err) {
        console.error(`[Transcripts] start fresh enable failed error_type=${errorType(err)}`);
      }
    }
    await refreshRecovery();
    setSetupDialog({ reason: "start-fresh", passwordProtected: false });
  };

  const finishSetup = () => {
    const reason = setupDialog?.reason;
    const passwordProtected = setupDialog?.passwordProtected ?? false;
    setSetupDialog(null);
    if (!passwordProtected) {
      setNotice(reason === "required" ? "required-done" : "setup-done");
    }
    void refreshRecovery();
  };

  const noticeNode =
    notice === "setup-done" ? (
      <MintNote>
        Saving is on. Your recovery key is set.{" "}
        <button
          type="button"
          onClick={() => setSettingsOpen(true)}
          className="underline underline-offset-2 cursor-pointer"
          style={{ color: "var(--color-frost-deep)" }}
        >
          Recovery settings
        </button>
      </MintNote>
    ) : notice === "required-done" ? (
      <MintNote>Recovery key set. Your transcripts are backed up.</MintNote>
    ) : notice === "restored" ? (
      <MintNote>
        <strong className="font-medium">History restored.</strong>{" "}
        {platform === "mac"
          ? "The key is back in Keychain, so you won't need it again on this Mac."
          : "The key is back in your Windows account, so you won't need it again on this PC."}
      </MintNote>
    ) : null;
  // Derived lock state. Task 5.5 mounts the Lockdown RecoveryUnlockScreen when
  // `isLocked` is true; `useLockdown` (App.tsx) only ever sets `locked` while
  // Lockdown Mode is enabled, so non-lockdown installs never gate the tab.
  const isLocked = lockStatus?.locked ?? false;
  const bakeoffEnabled =
    experimentalEnabled &&
    !(config.lockdown_mode ?? false) &&
    !(lockStatus?.enabled ?? false) &&
    !isLocked;
  const lockoutRemainingSeconds = lockStatus?.lockoutRemainingSeconds ?? 0;
  // F5: Lockdown Mode is on but no password envelope exists yet (the
  // history-off lockdown). Saving transcript history is impossible until a
  // password is set — config_manager.validate() rejects a raw
  // history_enabled:true write in this state. Guards the Save transcripts
  // toggle below (SettingsPopover + DisabledState) so it never fires that PUT.
  const lockdownNoEnvelope = (config.lockdown_mode ?? false) && !(lockStatus?.hasEnvelope ?? false);
  // On a successful unlock, re-fetch lock status from the backend so the
  // overlay dismisses (lockStatus.refresh updates App's useLockdown state).
  const refreshLockStatus = lockStatus?.refresh;
  const handleUnlocked = useCallback(() => {
    refreshLockStatus?.();
  }, [refreshLockStatus]);

  const {
    bakeoffState, bakeoffStarting, bakeoffStartFailure, bakeoffCancelling,
    bakeoffRetrying, bakeoffRetryFailure, openBakeoff, closeBakeoff,
    runBakeoff, cancelBakeoff, retryBakeoffPair, editBakeoffLineup,
  } = useBakeoffSession(bakeoffEnabled, subscribe);

  const [view, setView] = useState<View>({ view: "list" });

  // Destructive-action dialog state (Task 4.5 integration points).
  // pendingRetention: the smaller window the user picked, held until confirm.
  const [pendingRetention, setPendingRetention] = useState<number | null>(null);
  const [clearAllOpen, setClearAllOpen] = useState(false);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);

  // Native picker + drag-drop path delivery (Task 6.1). The drag-drop listener
  // is registered ONLY while the import view is active, so drops elsewhere in
  // the tab don't import; chooseFiles opens the native file picker. Both are a
  // graceful no-op outside the Tauri shell. Task 6.2 consumes dragActive to
  // brighten the drop zone.
  const { dragActive, pickerError, importUnavailable, chooseFiles } = useAudioImport(
    view.view === "import",
    capabilities?.platform === "Darwin",
  );

  const goList = useCallback(() => setView({ view: "list" }), []);

  // Esc returns to the list from the import view. (An open card's Esc-to-collapse
  // is handled inside ListView while the list is shown.)
  useEffect(() => {
    if (view.view === "list") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") goList();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [view.view, goList]);

  const handleRetentionChange = (days: number) => {
    const current = config.transcript_retention_days;
    // Widening (larger positive window) or switching to Forever (0) commits
    // directly with no purge. Shrinking (any -> a smaller positive, or
    // Forever(0) -> any positive) must go behind the confirm dialog.
    const isShrink = days !== 0 && (current === 0 || days < current);
    if (isShrink) {
      // Task 4.5 integration point: open the retention-shrink confirm BEFORE
      // committing the config change + calling purgeTranscripts.
      setPendingRetention(days);
      return;
    }
    onConfigChange({ transcript_retention_days: days });
  };

  const confirmRetentionShrink = () => {
    if (pendingRetention == null) return;
    const days = pendingRetention;
    setPendingRetention(null);
    onConfigChange({ transcript_retention_days: days });
    purgeTranscripts(days).catch((err) => {
      console.error(`[Transcripts] purge failed error_type=${errorType(err)}`);
    });
  };

  const confirmClearAll = () => {
    setClearAllOpen(false);
    clearTranscripts().catch((err) => {
      console.error(`[Transcripts] clear failed error_type=${errorType(err)}`);
    });
  };

  const confirmDelete = () => {
    const id = pendingDeleteId;
    setPendingDeleteId(null);
    if (!id) return;
    deleteTranscript(id).catch((err) => {
      console.error(`[Transcripts] delete failed error_type=${errorType(err)}`);
    });
  };

  return (
    <div className="h-full relative">
      {/* Surface C (Task 5.5): the transcript lock screen is an in-tab,
          full-surface overlay (NOT a PopupShell window, design Decision 7).
          When the backend has re-sealed the store (`isLocked`), the list/import
          content below is NOT rendered so nothing leaks behind the overlay; the
          sidebar / other tabs remain usable. The overlay fades in/out via
          AnimatePresence (idle re-lock arrives through the lockStatus prop,
          which App's useLockdown drives off the `transcripts.locked` WS event). */}
      {!isLocked && !keyMissing && !needsSetup && (
        <AnimatePresence mode="popLayout">
          <motion.div
            key={view.view}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={VIEW_TRANSITION}
          >
            {view.view === "list" && (
              <ListView
                historyDisabled={lockStatus?.historyDisabled === true}
                isMac={capabilities?.platform === "Darwin"}
                config={config}
                onConfigChange={onConfigChange}
                subscribe={subscribe}
                onOpenImport={() => setView({ view: "import" })}
                onRetentionChange={handleRetentionChange}
                onClearAll={() => setClearAllOpen(true)}
                onDelete={(id) => setPendingDeleteId(id)}
                lockdownNoEnvelope={lockdownNoEnvelope}
                onRequestLockdownSetup={onRequestLockdownSetup}
                onRequestEnable={requestEnable}
                onKeyMissing={refreshRecovery}
                onRecoverySettings={
                  !lockdownOn && !historyPolicyOff && recoveryStatus?.has_recovery_key === true
                    ? () => setSettingsOpen(true)
                    : undefined
                }
                notice={noticeNode}
                experimentalEnabled={experimentalEnabled}
                bakeoffEnabled={bakeoffEnabled}
                bakeoffActiveTranscriptId={
                  bakeoffState.session?.open
                    ? bakeoffState.session.transcriptId
                    : null
                }
                onOpenBakeoff={openBakeoff}
                devEnabled={devEnabled}
                activeModelId={activeModelId}
                activeModel={activeModel}
              />
            )}
            {view.view === "import" && (
              <ImportView
                onBack={goList}
                dragActive={dragActive}
                pickerError={pickerError}
                importUnavailable={importUnavailable}
                onChooseFiles={chooseFiles}
                subscribe={subscribe}
              />
            )}
          </motion.div>
        </AnimatePresence>
      )}

      <HistoryRecoverySetupDialog
        open={setupDialog !== null && !historyPolicyOff}
        platform={platform}
        reason={setupDialog?.reason ?? "enable"}
        passwordProtected={setupDialog?.passwordProtected ?? false}
        saveAudio={config.save_audio}
        onEnabled={() => onConfigChange({ history_enabled: true })}
        onKeyMissing={() => {
          setSetupDialog(null);
          void refreshRecovery();
        }}
        onComplete={finishSetup}
        onCancel={() => {
          setSetupDialog(null);
          void refreshRecovery();
        }}
      />

      <AnimatePresence>
        {needsSetup && !isLocked && (
          <RecoverySetupGate
            key="recovery-setup-gate"
            platform={platform}
            recordCount={recoveryStatus?.record_count ?? null}
            onSetUp={() => setSetupDialog({ reason: "required", passwordProtected: false })}
          />
        )}
      </AnimatePresence>

      <RecoverySettingsDialog
        open={settingsOpen && !lockdownOn}
        platform={platform}
        hasRecoveryPassword={recoveryStatus?.has_recovery_password ?? false}
        onClose={() => setSettingsOpen(false)}
        onChanged={() => void refreshRecovery()}
      />

      <StartFreshDialog
        open={startFreshOpen && keyMissing}
        recordCount={recoveryStatus?.record_count ?? null}
        hasAudio={recoveryStatus?.has_audio ?? false}
        hasRecoveryPassword={recoveryStatus?.has_recovery_password ?? false}
        onCancel={() => setStartFreshOpen(false)}
        onDeleted={() => void handleStartFreshDeleted()}
      />

      <AnimatePresence>
        {keyMissing && !isLocked && (
          <RecoveryUnlockScreen
            key="missing-key-screen"
            variant="missing-key"
            platform={platform}
            hasRecoveryPassword={recoveryStatus?.has_recovery_password ?? false}
            hasRecoveryKey={recoveryStatus?.has_recovery_key ?? true}
            initialLockoutSeconds={recoveryStatus?.key_lockout_remaining_seconds ?? 0}
            initialPasswordLockoutSeconds={recoveryStatus?.password_lockout_remaining_seconds ?? 0}
            onUnlocked={handleRestored}
            onStartFresh={() => setStartFreshOpen(true)}
          />
        )}
      </AnimatePresence>

      <AnimatePresence>
        {isLocked && (
          <RecoveryUnlockScreen
            key="lock-screen"
            initialLockoutSeconds={lockoutRemainingSeconds}
            onUnlocked={handleUnlocked}
          />
        )}
      </AnimatePresence>

      {/* Bakeoff workspace. Structurally absent unless both Experimental gates
          and the Lockdown authority checks pass; keyed by transcript so a
          session change drops the previous lineup with its session. Closing
          only hides it, so a reopened session keeps its lineup and results. */}
      {bakeoffEnabled && bakeoffState.session && (
        <BakeoffWorkspace
          key={bakeoffState.session.transcriptId}
          open={bakeoffState.session.open}
          phase={bakeoffState.session.phase}
          transcriptId={bakeoffState.session.transcriptId}
          pairs={bakeoffState.session.pairs}
          finishedCount={bakeoffState.session.finishedCount}
          totalCount={bakeoffState.session.totalCount}
          profiles={config.profiles ?? []}
          activeModelId={config.enhancement_local_model ?? activeModelId}
          starting={bakeoffStarting}
          startFailure={bakeoffStartFailure}
          runId={bakeoffState.session.runId}
          cancelling={bakeoffCancelling}
          retryingPairIndex={bakeoffRetrying}
          retryFailure={bakeoffRetryFailure}
          onRun={runBakeoff}
          onClose={closeBakeoff}
          onCancelRemaining={cancelBakeoff}
          onEditLineup={editBakeoffLineup}
          onRetryPair={retryBakeoffPair}
        />
      )}

      {/* Destructive confirm dialogs (Task 4.5): copied EnableLocalConfirmDialog
          shell with a rose primary; LOCKED copy from copy-review.md. */}
      <TranscriptConfirmDialog
        open={pendingRetention != null}
        headline={`Keep only the last ${retentionLabel(pendingRetention ?? 0)}?`}
        body={`Transcripts older than ${retentionLabel(
          pendingRetention ?? 0,
        )} will be deleted, along with their audio. This cannot be undone.`}
        confirmLabel="Delete older"
        onConfirm={confirmRetentionShrink}
        onCancel={() => setPendingRetention(null)}
      />
      <TranscriptConfirmDialog
        open={clearAllOpen}
        headline="Delete all transcripts?"
        body="This removes every transcript and its audio. It can't be undone."
        confirmLabel="Delete all"
        onConfirm={confirmClearAll}
        onCancel={() => setClearAllOpen(false)}
      />
      <TranscriptConfirmDialog
        open={pendingDeleteId != null}
        headline="Delete this transcript?"
        body="This deletes the transcript and its audio. This cannot be undone."
        confirmLabel="Delete"
        onConfirm={confirmDelete}
        onCancel={() => setPendingDeleteId(null)}
      />
    </div>
  );
}
