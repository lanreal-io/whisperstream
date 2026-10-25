import type { BakeoffStartFailure } from "../pages/transcripts/bakeoffSession";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import { motion, useReducedMotion } from "framer-motion";
import { Badge, type BadgeTone } from "./Badge";
import { Dropdown, type DropdownOption } from "./Dropdown";
import { ModeSelector } from "./ModeSelector";
import { ReadingWell } from "./ReadingWell";
import { SecondaryButton } from "./SecondaryButton";
import { getEnhancementModels } from "../lib/api/enhancement";
import { getTranscript } from "../lib/api/transcripts";
import type { EnhancementModel } from "../lib/api/enhancement";
import type { TranscriptBakeoffSelection } from "../lib/api/transcripts";
import type { TranscriptBakeoffMetadata } from "../lib/socket/bakeoffEvents";
import {
  LOCAL_MODEL_DISPLAY_LABEL,
} from "../lib/localModels";
import { errorType } from "../lib/logCapture";
import { diffTranscripts, type TranscriptDiff } from "../lib/transcriptDiff";
import { SemanticDiffLegend, SemanticDiffSegments } from "./SemanticDiff";

/* ---------- Dialog-family tokens ----------
   Copied from ProfileEditorModal because "the dialog family has
   no shared base, by design" — do NOT DRY these into a shared <Dialog>. This is
   a Family B workspace modal: portalled to document.body, --backdrop-strong
   (40px blur) scrim, --surface-modal glass shell. */
const cardStyle: React.CSSProperties = {
  background: "var(--surface-modal)",
  backdropFilter: "blur(24px)",
  WebkitBackdropFilter: "blur(24px)",
  border: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass)",
};

const backdropStyle: React.CSSProperties = {
  background: "var(--backdrop-strong)",
  backdropFilter: "blur(40px)",
  WebkitBackdropFilter: "blur(40px)",
};

/** Level-3 inset control surface for a lineup row and the disclosure block. */
const insetStyle: React.CSSProperties = {
  background: "var(--surface-control)",
  border: "1px solid var(--border-hairline)",
  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale)))",
};

// ---------------------------------------------------------------------------
// Bounds and geometry
// ---------------------------------------------------------------------------

/** Mirrors the backend's `_MIN_BAKEOFF_PAIRS`. */
export const MIN_BAKEOFF_PAIRS = 2;
/** Mirrors the backend's `_MAX_BAKEOFF_PAIRS`. */
export const MAX_BAKEOFF_PAIRS = 8;
/** Mirrors the backend's `_MAX_BAKEOFF_MODELS` (unique resident processes). */
export const MAX_BAKEOFF_MODELS = 4;

/** Target width of one answer card in the comparison rail (design.md). */
export const BAKEOFF_CARD_TARGET_PX = 380;
/** Minimum readable answer-card width. Never traded away to fit one more card. */
export const BAKEOFF_CARD_MIN_PX = 340;
/** Gap between two answer cards in the rail. */
export const BAKEOFF_CARD_GAP_PX = 16;
/** The workspace's own horizontal padding (p-7 on both sides). */
export const BAKEOFF_WORKSPACE_PADDING_PX = 56;
/** Total horizontal viewport clearance: a 12px minimum gutter per side. */
export const BAKEOFF_VIEWPORT_GUTTER_PX = 24;

/** Preferred workspace width (px) for a lineup of `count` comparisons.
 *
 *  The workspace grows by one readable card column per comparison instead of
 *  stopping at an arbitrary desktop maximum, so a 2560x1440 display reveals
 *  several complete cards at once. The caller clamps it to the viewport. */
export function bakeoffPreferredWidth(count: number): number {
  const columns = Math.min(
    MAX_BAKEOFF_PAIRS,
    Math.max(MIN_BAKEOFF_PAIRS, Math.round(count) || MIN_BAKEOFF_PAIRS),
  );
  return (
    BAKEOFF_WORKSPACE_PADDING_PX +
    columns * BAKEOFF_CARD_TARGET_PX +
    (columns - 1) * BAKEOFF_CARD_GAP_PX
  );
}

/** Viewport-capped width expression. 12px minimum side gutters, no 1440px cap. */
export function bakeoffWorkspaceWidth(count: number): string {
  return `min(calc(100vw - ${BAKEOFF_VIEWPORT_GUTTER_PX}px), ${bakeoffPreferredWidth(count)}px)`;
}

// ---------------------------------------------------------------------------
// Locked copy (copy-review.md "Locked Copy Decisions", approved 2026-08-16)
// ---------------------------------------------------------------------------

export const BAKEOFF_PAIR_STATE_LABEL = {
  queued: "Queued",
  loading: "Loading model",
  running: "Running enhancement",
  complete: "Complete",
  unchanged: "Unchanged",
  failed: "Failed",
  cancelled: "Cancelled",
} as const;

export type BakeoffLaneState = keyof typeof BAKEOFF_PAIR_STATE_LABEL;

const UNAVAILABLE_MODEL_COPY =
  "Download this model in Enhancement, then try again.";
const MISSING_CANDIDATE_COPY =
  "This candidate's model file is unavailable. Choose another model.";
const BUSY_COPY = "Finish the current recording or enhancement, then try again.";
const CONFIG_CHANGED_COPY =
  "Enhancement settings changed. Run the bakeoff again with the latest setup.";
const GENERIC_FAILURE_COPY =
  "This result could not finish. Retry it or continue with the other results.";
const MINIMUM_LINEUP_COPY =
  "Add at least two different model and profile combinations.";
const UNCHANGED_COPY =
  "The model returned the original, or a safeguard kept it unchanged.";
const DIFF_FALLBACK_COPY =
  "This result is too long to highlight safely. Showing clean text instead.";
const DEVICE_DISCLOSURE_COPY =
  "Your active model keeps its current device. Additional models use CPU, so compare output quality rather than speed.";

/** `{model} could not load. Remove it or retry this result.` */
export function loadFailureCopy(modelLabel: string): string {
  return `${modelLabel} could not load. Remove it or retry this result.`;
}

/** `{model} could not finish this result. Retry when the app is idle.` */
export function inferenceFailureCopy(modelLabel: string): string {
  return `${modelLabel} could not finish this result. Retry when the app is idle.`;
}

/** `{model} was not trained for the {profile} profile. Choose another profile.` */
export function unsupportedProfileCopy(
  modelLabel: string,
  profileName: string,
): string {
  return `${modelLabel} was not trained for the ${profileName} profile. Choose another profile.`;
}

/** `This lineup needs about {ram}. Remove a model and try again.` */
export function insufficientMemoryCopy(requiredMb: number | null): string {
  return `This lineup needs about ${formatMemory(requiredMb ?? 0)}. Remove a model and try again.`;
}

/** Human memory size. Mirrors the catalog's MB unit; GB past 1024 MB. */
export function formatMemory(mb: number): string {
  if (!Number.isFinite(mb) || mb <= 0) return "0 MB";
  if (mb < 1024) return `${Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}

/** Per-model resident estimate supplied by the backend after it consults the
 * private GGUF artifact. Older production-only backends retain the catalog-RAM
 * fallback; unknown candidates contribute no invented fixed-size value. */
export function estimateResidentMb(model: EnhancementModel): number {
  const residentRam =
    typeof model.resident_ram_mb === "number" ? model.resident_ram_mb : 0;
  if (residentRam > 0) return residentRam;
  const catalogRam = typeof model.ram_mb === "number" ? model.ram_mb : 0;
  return catalogRam > 0 ? Math.ceil(catalogRam * 1.25) + 512 : 0;
}

/** Whether the frontend can estimate this model's footprint from the catalog. */
export function hasMemoryEstimate(model: EnhancementModel): boolean {
  return estimateResidentMb(model) > 0;
}

// ---------------------------------------------------------------------------
// Catalog helpers
// ---------------------------------------------------------------------------

export interface BakeoffProfile {
  id: string;
  name: string;
  mode?: string;
}

/** Display label for one catalog row. Production rows reuse the shared
 *  local-model naming; candidates use their registry label, falling back to
 *  the experiment id for older backends. */
export function bakeoffModelLabel(model: EnhancementModel): string {
  if (model.source === "candidate") {
    return model.label ?? model.experiment_id ?? model.id;
  }
  return LOCAL_MODEL_DISPLAY_LABEL[model.id] ?? model.label ?? model.id;
}

/** A model can run only when its artifact is on disk; the backend re-checks. */
export function bakeoffModelRunnable(model: EnhancementModel): boolean {
  if (model.artifact_available === false) return false;
  if (model.source === "candidate") {
    return model.artifact_available === true && model.selectable !== false;
  }
  // Legacy backends omit artifact_available for production rows entirely.
  return model.artifact_available !== undefined
    ? model.artifact_available === true
    : true;
}

/** Recovery copy for a catalog row that cannot run right now. */
export function bakeoffModelUnavailableCopy(
  model: EnhancementModel,
): string | null {
  if (bakeoffModelRunnable(model)) return null;
  return model.source === "candidate"
    ? MISSING_CANDIDATE_COPY
    : UNAVAILABLE_MODEL_COPY;
}

/** Backend-authoritative profile mode derivation (`server._validate_bakeoff_
 *  selections`): a built-in id IS its mode, a custom profile stores one, and
 *  anything else falls back to chat. */
export function bakeoffProfileMode(profile: BakeoffProfile): string {
  if (profile.id === "chat" || profile.id === "code" || profile.id === "email") {
    return profile.id;
  }
  const stored = profile.mode;
  return stored === "chat" || stored === "code" || stored === "email"
    ? stored
    : "chat";
}

/** Only a candidate declares authoritative `trained_modes`; production rows run
 *  every profile. The backend remains authoritative either way. */
export function bakeoffProfileSupported(
  model: EnhancementModel | undefined,
  profile: BakeoffProfile,
): boolean {
  if (!model || model.source !== "candidate") return true;
  const trained = model.trained_modes;
  if (!Array.isArray(trained)) return true;
  return trained.includes(bakeoffProfileMode(profile) as "chat" | "code" | "email");
}

/** Map one start-request failure onto approved recovery copy. */
export function bakeoffStartErrorCopy(failure: BakeoffStartFailure): string {
  switch (failure.code) {
    case "recording_active":
    case "evaluation_busy":
      return BUSY_COPY;
    case "insufficient_current_memory":
      return insufficientMemoryCopy(failure.requiredMb ?? null);
    case "model_unavailable":
    case "model_artifact_unavailable":
      return UNAVAILABLE_MODEL_COPY;
    case "candidate_artifact_unavailable":
      return MISSING_CANDIDATE_COPY;
    case "profile_unavailable":
      return CONFIG_CHANGED_COPY;
    case "invalid_selection_count":
    case "duplicate_selection":
      return MINIMUM_LINEUP_COPY;
    default:
      return GENERIC_FAILURE_COPY;
  }
}

/** Map one terminal pair failure onto approved per-lane recovery copy. The
 *  backend sends a stable, content-free code; the model label comes from the
 *  catalog the workspace already holds. */
export function bakeoffPairErrorCopy(
  code: string | null,
  modelLabel: string,
  source?: EnhancementModel["source"],
): string {
  switch (code) {
    case "model_load_failed":
    // A rejected memory admission also means "this model could not load", and
    // the event carries no RAM figure to fill the lineup-level copy with.
    case "insufficient_current_memory":
    case "insufficient_initial_memory":
    case "memory_estimate_unavailable":
    case "memory_probe_unavailable":
      return loadFailureCopy(modelLabel);
    case "evaluation_failed":
    case "result_too_large":
      return inferenceFailureCopy(modelLabel);
    case "model_unavailable":
    case "model_artifact_unavailable":
      return source === "candidate"
        ? MISSING_CANDIDATE_COPY
        : UNAVAILABLE_MODEL_COPY;
    case "candidate_artifact_unavailable":
      return MISSING_CANDIDATE_COPY;
    case "profile_unavailable":
      return CONFIG_CHANGED_COPY;
    case "recording_active":
    case "evaluation_busy":
      return BUSY_COPY;
    default:
      return GENERIC_FAILURE_COPY;
  }
}

// ---------------------------------------------------------------------------
// Execution metadata
// ---------------------------------------------------------------------------

/** Compact duration. Sub-second work stays in ms so a warm run reads honestly. */
export function formatBakeoffDuration(ms: number | null | undefined): string | null {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return null;
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** The contract-spine execution facts for one finished pair: identity is the
 *  card heading, these are the subordinate second rung, and the answer is
 *  last. `compact` is the visible chip line; `spoken` is the screen-reader
 *  sentence, because the compact line's separators read as noise. */
export function bakeoffMetadataFacts(
  metadata: TranscriptBakeoffMetadata | null,
): { compact: string[]; spoken: string[] } {
  const compact: string[] = [];
  const spoken: string[] = [];
  if (metadata === null) return { compact, spoken };

  if (metadata.serving_contract) {
    compact.push(metadata.serving_contract);
    spoken.push(`Serving contract ${metadata.serving_contract}`);
  }
  if (metadata.device_mode) {
    const device = metadata.device_mode === "gpu" ? "GPU" : "CPU";
    compact.push(device);
    spoken.push(`Device ${device}`);
  }
  if (typeof metadata.resident_warm === "boolean") {
    const warm = metadata.resident_warm ? "Warm" : "Cold";
    compact.push(warm);
    spoken.push(metadata.resident_warm ? "Already resident" : "Cold start");
  }
  const load = formatBakeoffDuration(metadata.load_ms);
  if (load !== null) {
    compact.push(`Load ${load}`);
    spoken.push(`Model load ${load}`);
  }
  if (metadata.execution_mode) {
    const shape = metadata.execution_mode === "batched" ? "Batched" : "Whole";
    compact.push(shape);
    spoken.push(shape);
  }
  if (typeof metadata.chunk_count === "number" && metadata.chunk_count > 0) {
    const chunks = `${metadata.chunk_count} ${
      metadata.chunk_count === 1 ? "chunk" : "chunks"
    }`;
    compact.push(chunks);
    spoken.push(chunks);
  }
  const inference = formatBakeoffDuration(metadata.inference_ms);
  if (inference !== null) {
    compact.push(`Enhance ${inference}`);
    spoken.push(`Enhancement ${inference}`);
  }
  return { compact, spoken };
}

const LANE_TONE: Record<BakeoffLaneState, BadgeTone> = {
  queued: "neutral",
  loading: "frost",
  running: "frost",
  complete: "mint",
  unchanged: "neutral",
  failed: "rose",
  cancelled: "neutral",
};

const TERMINAL_LANE_STATES = new Set<BakeoffLaneState>([
  "complete",
  "unchanged",
  "failed",
  "cancelled",
]);

// ---------------------------------------------------------------------------
// Icons (inline SVG, stroke 1.5 — same idiom as the sibling dialogs)
// ---------------------------------------------------------------------------

function XGlyph() {
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
      <path d="M3 3l6 6M9 3l-6 6" />
    </svg>
  );
}

function PlusGlyph() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M7 3v8M3 7h8" />
    </svg>
  );
}

function MinusGlyph() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M3 7h8" />
    </svg>
  );
}

/** Sideways chevron that rotates when its disclosure opens, matching
 *  `AdvancedDisclosure`'s idiom. */
function ChevronGlyph({ open }: { open: boolean }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{
        transform: `rotate(${open ? 90 : 0}deg)`,
        transition: "transform 200ms",
      }}
    >
      <path d="M4.5 2.5L8 6l-3.5 3.5" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Workspace
// ---------------------------------------------------------------------------

export interface BakeoffLane {
  selection: TranscriptBakeoffSelection;
  state: BakeoffLaneState;
  /** Ephemeral model output. Present only for a complete/unchanged result. */
  output?: string | null;
  errorCode?: string | null;
  metadata?: TranscriptBakeoffMetadata | null;
}

export interface BakeoffWorkspaceProps {
  /** Closing hides the workspace; the caller keeps the session alive. */
  open: boolean;
  phase: "configure" | "running" | "results";
  /** The saved record under comparison. Used only to read its raw text once. */
  transcriptId: string;
  /** Request-order lanes. Empty while the lineup is still being configured. */
  pairs: readonly BakeoffLane[];
  finishedCount: number;
  totalCount: number;
  /** Enhancement profiles from the live config. Never mutated here. */
  profiles: readonly BakeoffProfile[];
  /** Configured local model id, used for the initial lineup seed. */
  activeModelId?: string;
  /** A start request is in flight. */
  starting?: boolean;
  /** Lineup-level failure from the last start request. */
  startFailure?: BakeoffStartFailure | null;
  /** Server run identity, present once a run has been accepted. */
  runId?: string | null;
  /** A Cancel remaining request is in flight. */
  cancelling?: boolean;
  /** The lane whose retry request is in flight. Inference is serialized, so at
   *  most one retry can be pending. */
  retryingPairIndex?: number | null;
  /** A refused retry, reported beside the lane it belongs to. */
  retryFailure?: { pairIndex: number; failure: BakeoffStartFailure } | null;
  onRun: (selections: TranscriptBakeoffSelection[]) => void;
  onClose: () => void;
  onCancelRemaining?: () => void;
  onEditLineup?: () => void;
  onRetryPair?: (pairIndex: number) => void;
  /** Deterministic, content-free data for the dev-only dialog/UI QA registry.
   *  Ordinary product callers omit this and retain the live API boundary. */
  uiQaFixture?: BakeoffWorkspaceUiQaFixture;
}

export interface BakeoffWorkspaceUiQaFixture {
  models: readonly EnhancementModel[];
  rawText: string;
  activeModelId?: string;
  catalogActiveId?: string;
  catalogRunning?: boolean;
  initialSelections?: readonly TranscriptBakeoffSelection[];
  initialView?: "diff" | "clean";
}

interface LineupRow {
  key: number;
  modelId: string;
  profileId: string;
}

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

/**
 * BakeoffWorkspace — the Family B lineup + comparison shell (design.md).
 *
 * The fire action opens this dedicated workspace rather than expanding the
 * transcript card: an explicit model/profile lineup plus several long outputs
 * exceeds a card's information capacity. One stable shell carries both modes,
 * so the lineup's request order becomes the comparison's lane order.
 *
 * Everything here is memory-only. The workspace never writes config, history,
 * the clipboard, or localStorage; the caller owns the session state and its
 * run-generation guard.
 */
export function BakeoffWorkspace({
  open,
  phase,
  transcriptId,
  pairs,
  finishedCount,
  totalCount,
  profiles,
  activeModelId = "",
  starting = false,
  startFailure = null,
  runId = null,
  cancelling = false,
  retryingPairIndex = null,
  retryFailure = null,
  onRun,
  onClose,
  onCancelRemaining,
  onEditLineup,
  onRetryPair,
  uiQaFixture,
}: BakeoffWorkspaceProps) {
  const reducedMotion = useReducedMotion();
  const titleId = useId();
  const validationId = useId();
  const cardRef = useRef<HTMLDivElement>(null);
  const nextRowKey = useRef(uiQaFixture?.initialSelections?.length ?? 0);

  const [models, setModels] = useState<EnhancementModel[] | null>(() =>
    uiQaFixture ? [...uiQaFixture.models] : null,
  );
  const [catalogFailed, setCatalogFailed] = useState(false);
  const [catalogActiveId, setCatalogActiveId] = useState(
    uiQaFixture?.catalogActiveId ?? "",
  );
  const [catalogRunning, setCatalogRunning] = useState(
    uiQaFixture?.catalogRunning === true,
  );
  const [rows, setRows] = useState<LineupRow[]>(() =>
    (uiQaFixture?.initialSelections ?? []).map((selection, index) => ({
      key: index + 1,
      modelId: selection.model_id,
      profileId: selection.profile_id,
    })),
  );
  const [diffView, setDiffView] = useState<"diff" | "clean">(
    uiQaFixture?.initialView ?? "diff",
  );
  const [originalOpen, setOriginalOpen] = useState(true);
  const [rawText, setRawText] = useState<string | null>(
    uiQaFixture?.rawText ?? null,
  );
  const [rawUnavailable, setRawUnavailable] = useState(false);
  const [announcement, setAnnouncement] = useState("");

  // The merged local catalog is the only source of model options: production
  // repository rows plus registered development candidates. Bundled ids are
  // never hardcoded and an unregistered GGUF stays intentionally invisible.
  useEffect(() => {
    if (uiQaFixture) return;
    let cancelled = false;
    getEnhancementModels("local")
      .then((response) => {
        if (cancelled) return;
        setModels(response.bundled ?? []);
        setCatalogActiveId(response.active_id ?? "");
        setCatalogRunning(response.running === true);
      })
      .catch((error) => {
        if (cancelled) return;
        console.error(
          `[Bakeoff] model catalog failed error_type=${errorType(error)}`,
        );
        setCatalogFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [uiQaFixture]);

  // The one controlled input. It is read once, only after a run exists (the
  // lineup itself needs no transcript text), and lives here for exactly as
  // long as the workspace is mounted. Lockdown, transcript change, and reset
  // unmount the workspace, which is what releases this decrypted copy.
  useEffect(() => {
    if (uiQaFixture) return;
    if (phase === "configure" || rawText !== null || rawUnavailable) return;
    if (!transcriptId) return;
    let cancelled = false;
    getTranscript(transcriptId)
      .then((record) => {
        if (cancelled) return;
        setRawText(record.raw_text ?? "");
      })
      .catch((error) => {
        if (cancelled) return;
        console.error(
          `[Bakeoff] original transcript failed error_type=${errorType(error)}`,
        );
        setRawUnavailable(true);
      });
    return () => {
      cancelled = true;
    };
  }, [phase, rawText, rawUnavailable, transcriptId, uiQaFixture]);

  const modelById = useMemo(() => {
    const map = new Map<string, EnhancementModel>();
    for (const model of models ?? []) map.set(model.id, model);
    return map;
  }, [models]);

  const profileById = useMemo(() => {
    const map = new Map<string, BakeoffProfile>();
    for (const profile of profiles) map.set(profile.id, profile);
    return map;
  }, [profiles]);

  // Visible catalog rows. Production rows that are neither installed nor
  // publishable stay hidden (the Enhancement picker's hide-until-published
  // gate, which self-heals when a fine-tune publishes); every other row stays
  // visible, disabled, and carries its recovery copy.
  const visibleModels = useMemo(() => {
    return (models ?? []).filter((model) => {
      if (model.source === "candidate") return true;
      return bakeoffModelRunnable(model) || model.selectable !== false;
    });
  }, [models]);

  const makeRow = useCallback((modelId: string, profileId: string): LineupRow => {
    nextRowKey.current += 1;
    return { key: nextRowKey.current, modelId, profileId };
  }, []);

  // Seed two distinct combinations once the catalog resolves: the configured
  // model with two compatible profiles, falling back to two models on one
  // profile when only a single profile can run.
  useEffect(() => {
    if (models === null || rows.length > 0 || profiles.length === 0) return;
    const runnable = visibleModels.filter(bakeoffModelRunnable);
    const preferred =
      runnable.find((model) => model.id === activeModelId) ??
      runnable[0] ??
      visibleModels[0];
    if (!preferred) return;
    const compatible = profiles.filter((profile) =>
      bakeoffProfileSupported(preferred, profile),
    );
    const usableProfiles = compatible.length > 0 ? compatible : profiles;
    if (usableProfiles.length >= 2) {
      setRows([
        makeRow(preferred.id, usableProfiles[0].id),
        makeRow(preferred.id, usableProfiles[1].id),
      ]);
      return;
    }
    const second = runnable.find((model) => model.id !== preferred.id);
    setRows([
      makeRow(preferred.id, usableProfiles[0].id),
      makeRow(second?.id ?? preferred.id, usableProfiles[0].id),
    ]);
  }, [
    activeModelId,
    makeRow,
    models,
    profiles,
    rows.length,
    visibleModels,
  ]);

  // Mount focus on the dialog itself so the label is announced before the
  // lineup controls; Tab then cycles inside the trap below.
  useEffect(() => {
    if (!open) return;
    cardRef.current?.focus({ preventScroll: true });
  }, [open]);

  const trapFocus = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab") return;
    const card = cardRef.current;
    if (!card) return;
    const nodes = Array.from(
      card.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
    );
    if (nodes.length === 0) {
      event.preventDefault();
      card.focus({ preventScroll: true });
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement as HTMLElement | null;
    const inside = active !== null && card.contains(active) && active !== card;
    if (event.shiftKey) {
      if (!inside || active === first) {
        event.preventDefault();
        last.focus();
      }
      return;
    }
    if (!inside || active === last) {
      event.preventDefault();
      first.focus();
    }
  }, []);

  const uniqueModelIds = useMemo(
    () => new Set(rows.map((row) => row.modelId)),
    [rows],
  );

  const selections = useMemo<TranscriptBakeoffSelection[]>(
    () =>
      rows.map((row) => ({ model_id: row.modelId, profile_id: row.profileId })),
    [rows],
  );

  const duplicateKeys = useMemo(() => {
    const seen = new Set<string>();
    const duplicates = new Set<string>();
    for (const row of rows) {
      const key = `${row.modelId}::${row.profileId}`;
      if (seen.has(key)) duplicates.add(key);
      seen.add(key);
    }
    return duplicates;
  }, [rows]);

  const rowGuidance = useCallback(
    (row: LineupRow): string | null => {
      const model = modelById.get(row.modelId);
      const profile = profileById.get(row.profileId);
      if (!model) return CONFIG_CHANGED_COPY;
      const unavailable = bakeoffModelUnavailableCopy(model);
      if (unavailable) return unavailable;
      if (!profile) return CONFIG_CHANGED_COPY;
      if (!bakeoffProfileSupported(model, profile)) {
        return unsupportedProfileCopy(bakeoffModelLabel(model), profile.name);
      }
      return null;
    },
    [modelById, profileById],
  );

  const blockedRows = rows.some((row) => rowGuidance(row) !== null);
  const lineupValid =
    rows.length >= MIN_BAKEOFF_PAIRS &&
    rows.length <= MAX_BAKEOFF_PAIRS &&
    uniqueModelIds.size <= MAX_BAKEOFF_MODELS &&
    duplicateKeys.size === 0 &&
    !blockedRows;

  const validationMessage = useMemo(() => {
    if (phase !== "configure") return null;
    if (startFailure) return bakeoffStartErrorCopy(startFailure);
    if (rows.length === 0) return null;
    if (rows.length < MIN_BAKEOFF_PAIRS || duplicateKeys.size > 0) {
      return MINIMUM_LINEUP_COPY;
    }
    return null;
  }, [duplicateKeys.size, phase, rows.length, startFailure]);

  // Estimated incremental footprint: the already-running production model keeps
  // its process, so only newly resident models add memory.
  const footprint = useMemo(() => {
    const residentId = catalogRunning ? catalogActiveId : "";
    let totalMb = 0;
    const estimated: string[] = [];
    for (const modelId of uniqueModelIds) {
      const model = modelById.get(modelId);
      if (!model || modelId === residentId) continue;
      totalMb += estimateResidentMb(model);
      if (
        model.resident_ram_estimated === true ||
        !hasMemoryEstimate(model)
      ) {
        estimated.push(bakeoffModelLabel(model));
      }
    }
    return { totalMb, estimated, count: uniqueModelIds.size };
  }, [catalogActiveId, catalogRunning, modelById, uniqueModelIds]);

  const setRowModel = (key: number, modelId: string) => {
    setRows((current) =>
      current.map((row) => (row.key === key ? { ...row, modelId } : row)),
    );
  };

  const setRowProfile = (key: number, profileId: string) => {
    setRows((current) =>
      current.map((row) => (row.key === key ? { ...row, profileId } : row)),
    );
  };

  const removeRow = (key: number) => {
    setRows((current) =>
      current.length <= MIN_BAKEOFF_PAIRS
        ? current
        : current.filter((row) => row.key !== key),
    );
  };

  const addRow = () => {
    setRows((current) => {
      if (current.length >= MAX_BAKEOFF_PAIRS) return current;
      const last = current[current.length - 1];
      const model = last ? modelById.get(last.modelId) : undefined;
      // Copy the last row's model so Add never silently grows the resident
      // model count; prefer its first unused compatible profile.
      const used = new Set(
        current.filter((row) => row.modelId === last?.modelId).map((row) => row.profileId),
      );
      const nextProfile =
        profiles.find(
          (profile) =>
            !used.has(profile.id) && bakeoffProfileSupported(model, profile),
        ) ??
        profiles.find((profile) => bakeoffProfileSupported(model, profile)) ??
        profiles[0];
      if (!last || !nextProfile) return current;
      return [...current, makeRow(last.modelId, nextProfile.id)];
    });
  };

  const modelOptions = useCallback(
    (row: LineupRow): DropdownOption<string>[] => {
      const otherModelIds = new Set(
        rows.filter((entry) => entry.key !== row.key).map((entry) => entry.modelId),
      );
      return visibleModels.map((model) => {
        const unavailable = bakeoffModelUnavailableCopy(model);
        const wouldExceedModels =
          !otherModelIds.has(model.id) &&
          otherModelIds.size >= MAX_BAKEOFF_MODELS;
        const metadata =
          model.source === "candidate"
            ? [model.parameter_size, model.role, model.status]
                .filter((value): value is string => Boolean(value))
                .join(" · ")
            : undefined;
        return {
          value: model.id,
          label: bakeoffModelLabel(model),
          description:
            unavailable ??
            (wouldExceedModels
              ? `A bakeoff can load up to ${MAX_BAKEOFF_MODELS} models.`
              : metadata),
          disabled: unavailable !== null || wouldExceedModels,
        };
      });
    },
    [rows, visibleModels],
  );

  const profileOptions = useCallback(
    (row: LineupRow): DropdownOption<string>[] => {
      const model = modelById.get(row.modelId);
      return profiles.map((profile) => {
        const supported = bakeoffProfileSupported(model, profile);
        return {
          value: profile.id,
          label: profile.name,
          description: supported
            ? undefined
            : unsupportedProfileCopy(
                model ? bakeoffModelLabel(model) : row.modelId,
                profile.name,
              ),
          disabled: !supported,
        };
      });
    },
    [modelById, profiles],
  );

  const comparisonCount = phase === "configure" ? rows.length : pairs.length;
  const preferredWidth = bakeoffPreferredWidth(comparisonCount);

  const laneIdentity = useCallback(
    (lane: BakeoffLane): string => {
      const model = modelById.get(lane.selection.model_id);
      const profile = profileById.get(lane.selection.profile_id);
      return `${model ? bakeoffModelLabel(model) : lane.selection.model_id}, ${
        profile ? profile.name : lane.selection.profile_id
      }`;
    },
    [modelById, profileById],
  );

  const completedCount = pairs.filter(
    (pair) => pair.state === "complete" || pair.state === "unchanged",
  ).length;
  const allTerminal =
    pairs.length > 0 && pairs.every((pair) => TERMINAL_LANE_STATES.has(pair.state));
  const completionSummary = `${completedCount} of ${totalCount} results completed.`;

  // Polite announcements: one pair transition plus honest overall progress.
  // Elapsed time deliberately never announces while a result is running.
  const previousStates = useRef<BakeoffLaneState[] | null>(null);
  useEffect(() => {
    if (phase === "configure") {
      previousStates.current = null;
      setAnnouncement("");
      return;
    }
    const current = pairs.map((pair) => pair.state);
    const previous = previousStates.current;
    previousStates.current = current;
    const tail = allTerminal
      ? completionSummary
      : `${finishedCount} of ${totalCount} finished`;
    if (previous === null || previous.length !== current.length) {
      setAnnouncement(tail);
      return;
    }
    const changed = current.findIndex((state, index) => state !== previous[index]);
    if (changed === -1) return;
    setAnnouncement(
      `${laneIdentity(pairs[changed])}. ${
        BAKEOFF_PAIR_STATE_LABEL[current[changed]]
      }. ${tail}`,
    );
  }, [
    allTerminal,
    completionSummary,
    finishedCount,
    laneIdentity,
    pairs,
    phase,
    totalCount,
  ]);

  if (!open) return null;

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: reducedMotion ? 0 : 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        onClick={onClose}
        aria-hidden="true"
      />

      <motion.div
        ref={cardRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={trapFocus}
        data-bakeoff-width={preferredWidth}
        initial={
          reducedMotion
            ? { opacity: 0 }
            : { scale: 0.92, opacity: 0, y: 12 }
        }
        animate={
          reducedMotion ? { opacity: 1 } : { scale: 1, opacity: 1, y: 0 }
        }
        transition={
          reducedMotion
            ? { duration: 0 }
            : { type: "spring", stiffness: 400, damping: 30, delay: 0.08 }
        }
        className="relative flex flex-col rounded-[var(--radius-card)] p-7 outline-none"
        style={{
          ...cardStyle,
          width: bakeoffWorkspaceWidth(comparisonCount),
          maxWidth: `calc(100vw - ${BAKEOFF_VIEWPORT_GUTTER_PX}px)`,
          maxHeight: `calc(100vh - ${BAKEOFF_VIEWPORT_GUTTER_PX}px)`,
        }}
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="absolute top-4 right-4 w-6 h-6 rounded-full flex items-center justify-center text-steel/60 hover:text-slate transition-colors cursor-pointer"
        >
          <XGlyph />
        </button>

        {/* Header stays visible while the body scrolls. */}
        <div className="flex items-start justify-between gap-6 pr-8 shrink-0">
          <div className="min-w-0">
            <h2 id={titleId} className="text-[18px] font-medium text-slate">
              Compare enhancements
            </h2>
            <p className="mt-1 text-[12px] text-steel leading-relaxed">
              Run the same raw transcript through each model and profile.
            </p>
          </div>
          {phase !== "configure" && (
            <div className="flex items-center gap-3 shrink-0 pt-0.5">
              <p className="text-[11px] font-medium text-steel">
                {finishedCount} of {totalCount} finished
              </p>
              {/* One control for every completed card. Diff is the default. */}
              <ModeSelector<"diff" | "clean">
                value={diffView}
                onChange={setDiffView}
                ariaLabel="Difference view"
                options={[
                  { value: "diff", label: "Diff" },
                  { value: "clean", label: "Clean" },
                ]}
              />
            </div>
          )}
        </div>

        <div className="mt-5 min-h-0 flex-1 overflow-y-auto">
          {phase === "configure" ? (
            <LineupEditor
              catalogFailed={catalogFailed}
              loading={models === null && !catalogFailed}
              rows={rows}
              modelById={modelById}
              profileById={profileById}
              footprint={footprint}
              modelOptions={modelOptions}
              profileOptions={profileOptions}
              rowGuidance={rowGuidance}
              onModelChange={setRowModel}
              onProfileChange={setRowProfile}
              onRemove={removeRow}
              onAdd={addRow}
            />
          ) : (
            <ComparisonView
              pairs={pairs}
              modelById={modelById}
              profileById={profileById}
              rawText={rawText}
              originalOpen={originalOpen}
              onToggleOriginal={() => setOriginalOpen((value) => !value)}
              diffView={diffView}
              reducedMotion={reducedMotion === true}
              retryingPairIndex={retryingPairIndex}
              retryFailure={retryFailure}
              onRetryPair={runId ? onRetryPair : undefined}
            />
          )}
        </div>

        <div className="mt-5 flex items-end justify-between gap-6 shrink-0">
          {phase !== "configure" && (
            <div className="min-w-0 space-y-1">
              {diffView === "diff" && rawText !== null && <SemanticDiffLegend />}
              {/* Named again here so a borrowed-GPU lane's timing is never read
                  as directly comparable with a pool-CPU lane's. */}
              <p className="text-[11px] text-steel leading-relaxed">
                {DEVICE_DISCLOSURE_COPY}
              </p>
              {allTerminal && (
                <p className="text-[11px] text-slate leading-relaxed">
                  {completionSummary}
                </p>
              )}
            </div>
          )}
          {phase === "configure" && (
          <p
            id={validationId}
            className={`text-[11px] leading-relaxed ${
              validationMessage ? "text-gold" : "text-transparent"
            }`}
          >
            {validationMessage ?? " "}
          </p>
          )}
          <div className="flex items-center gap-2.5 shrink-0">
            {phase === "running" && runId !== null && onCancelRemaining && (
              <SecondaryButton
                onClick={onCancelRemaining}
                disabled={cancelling}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate"
              >
                Cancel remaining
              </SecondaryButton>
            )}
            {phase === "results" && onEditLineup && (
              <SecondaryButton
                onClick={onEditLineup}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate"
              >
                Edit lineup
              </SecondaryButton>
            )}
            <SecondaryButton
              onClick={onClose}
              className="px-5 py-2 rounded-full text-[13px] font-medium text-slate"
            >
              Close
            </SecondaryButton>
            {phase === "configure" && (
              <button
                type="button"
                onClick={() => onRun(selections)}
                disabled={!lineupValid || starting}
                aria-describedby={validationMessage ? validationId : undefined}
                className="px-5 py-2 rounded-full text-[13px] font-medium cursor-pointer disabled:cursor-default transition-all duration-200"
                style={
                  lineupValid && !starting
                    ? {
                        background: "var(--accent-fill)",
                        color: "#fff",
                        boxShadow: "var(--shadow-pearl)",
                      }
                    : {
                        background: "var(--surface-control)",
                        color: "var(--color-steel)",
                        border: "1px solid var(--border-hairline)",
                        opacity: 0.6,
                      }
                }
              >
                Run bakeoff
              </button>
            )}
            {phase === "results" && (
              <button
                type="button"
                onClick={() => onRun(pairs.map((pair) => ({ ...pair.selection })))}
                disabled={starting}
                className="px-5 py-2 rounded-full text-[13px] font-medium cursor-pointer disabled:cursor-default transition-all duration-200"
                style={
                  starting
                    ? {
                        background: "var(--surface-control)",
                        color: "var(--color-steel)",
                        border: "1px solid var(--border-hairline)",
                        opacity: 0.6,
                      }
                    : {
                        background: "var(--accent-fill)",
                        color: "#fff",
                        boxShadow: "var(--shadow-pearl)",
                      }
                }
              >
                Run again
              </button>
            )}
          </div>
        </div>

        <div aria-live="polite" className="sr-only">
          {announcement}
        </div>
      </motion.div>
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// Lineup editor
// ---------------------------------------------------------------------------

function LineupEditor({
  catalogFailed,
  loading,
  rows,
  modelById,
  profileById,
  footprint,
  modelOptions,
  profileOptions,
  rowGuidance,
  onModelChange,
  onProfileChange,
  onRemove,
  onAdd,
}: {
  catalogFailed: boolean;
  loading: boolean;
  rows: LineupRow[];
  modelById: Map<string, EnhancementModel>;
  profileById: Map<string, BakeoffProfile>;
  footprint: { totalMb: number; estimated: string[]; count: number };
  modelOptions: (row: LineupRow) => DropdownOption<string>[];
  profileOptions: (row: LineupRow) => DropdownOption<string>[];
  rowGuidance: (row: LineupRow) => string | null;
  onModelChange: (key: number, modelId: string) => void;
  onProfileChange: (key: number, profileId: string) => void;
  onRemove: (key: number) => void;
  onAdd: () => void;
}) {
  if (catalogFailed) {
    return (
      <p role="status" className="text-[12px] text-steel">
        Models could not be loaded. Close this and try again.
      </p>
    );
  }
  if (loading || rows.length === 0) {
    return (
      <p role="status" className="text-[12px] text-steel">
        Loading models.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        {rows.map((row, index) => {
          const guidance = rowGuidance(row);
          const guidanceId = `bakeoff-row-${row.key}-guidance`;
          const model = modelById.get(row.modelId);
          const profile = profileById.get(row.profileId);
          const identity = [
            model ? bakeoffModelLabel(model) : row.modelId,
            profile ? profile.name : row.profileId,
          ].join(", ");
          return (
            <div key={row.key}>
              <div
                role="group"
                aria-label={`Comparison ${index + 1}`}
                aria-describedby={guidance ? guidanceId : undefined}
                className="flex flex-col sm:flex-row sm:items-center gap-3 rounded-[var(--radius-row)] p-3"
                style={insetStyle}
              >
                <div className="flex-1 min-w-0">
                  <span className="block mb-1 text-[11px] font-medium text-steel">
                    Model
                  </span>
                  <Dropdown
                    value={row.modelId}
                    options={modelOptions(row)}
                    onChange={(next) => onModelChange(row.key, next)}
                    ariaLabel={`Model for comparison ${index + 1}`}
                    triggerClassName="w-full"
                    placeholder="Model"
                  />
                </div>
                <div className="flex-1 min-w-0">
                  <span className="block mb-1 text-[11px] font-medium text-steel">
                    Profile
                  </span>
                  <Dropdown
                    value={row.profileId}
                    options={profileOptions(row)}
                    onChange={(next) => onProfileChange(row.key, next)}
                    ariaLabel={`Profile for comparison ${index + 1}`}
                    triggerClassName="w-full"
                    placeholder="Profile"
                  />
                </div>
                <button
                  type="button"
                  onClick={() => onRemove(row.key)}
                  disabled={rows.length <= MIN_BAKEOFF_PAIRS}
                  aria-label={`Remove comparison ${identity}`}
                  className="self-end sm:self-auto sm:mt-5 w-6 h-6 shrink-0 flex items-center justify-center rounded-full text-steel hover:text-rose disabled:cursor-default disabled:text-steel/40 cursor-pointer"
                >
                  <MinusGlyph />
                </button>
              </div>
              {guidance && (
                <p
                  id={guidanceId}
                  className="mt-1 px-3 text-[11px] leading-relaxed text-gold"
                >
                  {guidance}
                </p>
              )}
            </div>
          );
        })}
      </div>

      <div className="flex items-center gap-3">
        <SecondaryButton
          onClick={onAdd}
          disabled={rows.length >= MAX_BAKEOFF_PAIRS}
          className="inline-flex items-center gap-1.5 rounded-full px-3 h-8 text-[12px] font-medium text-slate"
        >
          <PlusGlyph /> Add comparison
        </SecondaryButton>
        {rows.length >= MAX_BAKEOFF_PAIRS && (
          <span className="text-[11px] text-steel">
            A bakeoff can compare up to {MAX_BAKEOFF_PAIRS} combinations.
          </span>
        )}
      </div>

      {/* Resident footprint, until-exit residency, and device disclosure. */}
      <div
        className="rounded-[var(--radius-row)] p-3 space-y-1"
        style={insetStyle}
      >
        {footprint.totalMb > 0 && (
          <p className="text-[12px] text-slate leading-relaxed">
            {footprint.count} {footprint.count === 1 ? "model" : "models"} will
            use about {formatMemory(footprint.totalMb)}.
          </p>
        )}
        {footprint.estimated.length > 0 && (
          <p className="text-[11px] text-steel leading-relaxed">
            Memory includes an estimate for {footprint.estimated.join(", ")}.
          </p>
        )}
        <p className="text-[11px] text-steel leading-relaxed">
          Selected models stay loaded until you exit Whisperstream.
        </p>
        <p className="text-[11px] text-steel leading-relaxed">
          {DEVICE_DISCLOSURE_COPY}
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Comparison view: one compact Original well over one horizontal rail of peer
// answer cards.
//
// The rail NEVER becomes a vertical answer list. Cards keep their 380 px
// target and 340 px minimum readable width at every supported desktop width;
// the workspace grows toward the viewport first (see bakeoffPreferredWidth),
// and only then does the rail continue horizontally with scroll snapping and a
// visible edge peek. That clipped next card is the wayfinding.
//
// Cards also have NO independent body scroller: the workspace's own vertical
// scroll carries every card together, so their headings and paragraph starts
// stay aligned across the rail. That is why the answer body is not a
// `ReadingWell` (it owns a scroll container by design); the Original well,
// which genuinely is a small capped scroller, still is one.
// ---------------------------------------------------------------------------

function ComparisonView({
  pairs,
  modelById,
  profileById,
  rawText,
  originalOpen,
  onToggleOriginal,
  diffView,
  reducedMotion,
  retryingPairIndex,
  retryFailure,
  onRetryPair,
}: {
  pairs: readonly BakeoffLane[];
  modelById: Map<string, EnhancementModel>;
  profileById: Map<string, BakeoffProfile>;
  rawText: string | null;
  originalOpen: boolean;
  onToggleOriginal: () => void;
  diffView: "diff" | "clean";
  reducedMotion: boolean;
  retryingPairIndex: number | null;
  retryFailure: { pairIndex: number; failure: BakeoffStartFailure } | null;
  onRetryPair?: (pairIndex: number) => void;
}) {
  return (
    <div>
      {/* The controlled input, once, above every answer. */}
      {rawText !== null && (
        <div className="mb-4">
          <button
            type="button"
            onClick={onToggleOriginal}
            aria-expanded={originalOpen}
            className="mb-1.5 inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-steel hover:text-slate transition-colors cursor-pointer"
          >
            Original
            <ChevronGlyph open={originalOpen} />
          </button>
          {originalOpen && (
            <ReadingWell
              maxHeight={96}
              ariaLabel="Original transcript"
              fontSize={12}
              fontFamily="var(--font-quote)"
              fontWeight={400}
            >
              {rawText}
            </ReadingWell>
          )}
        </div>
      )}

      {/* Focusable because a completed card has no control of its own: without
          this, a keyboard-only user could not reach the rest of the rail. */}
      <div
        data-bakeoff-rail="true"
        role="group"
        aria-label="Comparison results"
        tabIndex={0}
        className="flex gap-4 items-start overflow-x-auto pb-1 outline-none"
        style={{ scrollSnapType: "x proximity" }}
      >
        {pairs.map((pair, index) => (
          <AnswerCard
            key={`${pair.selection.model_id}-${pair.selection.profile_id}-${index}`}
            pair={pair}
            index={index}
            model={modelById.get(pair.selection.model_id)}
            profile={profileById.get(pair.selection.profile_id)}
            rawText={rawText}
            diffView={diffView}
            reducedMotion={reducedMotion}
            retrying={retryingPairIndex === index}
            retryFailure={
              retryFailure?.pairIndex === index ? retryFailure.failure : null
            }
            onRetry={onRetryPair}
          />
        ))}
      </div>
    </div>
  );
}

/** One peer answer card. Every lineup slot renders one of these in request
 *  order, in every state, so the rail geometry never jumps as events arrive
 *  and a peer's failure never removes a completed neighbour. */
function AnswerCard({
  pair,
  index,
  model,
  profile,
  rawText,
  diffView,
  reducedMotion,
  retrying,
  retryFailure,
  onRetry,
}: {
  pair: BakeoffLane;
  index: number;
  model: EnhancementModel | undefined;
  profile: BakeoffProfile | undefined;
  rawText: string | null;
  diffView: "diff" | "clean";
  reducedMotion: boolean;
  retrying: boolean;
  retryFailure: BakeoffStartFailure | null;
  onRetry?: (pairIndex: number) => void;
}) {
  const modelLabel = model
    ? bakeoffModelLabel(model)
    : pair.selection.model_id;
  const profileName = profile ? profile.name : pair.selection.profile_id;
  const identity = `${modelLabel}, ${profileName}`;
  const output = pair.output ?? null;
  const errorId = `bakeoff-lane-${index}-error`;

  // Keyed on the two strings, so a sibling lane's event never recomputes this
  // lane's diff. Bounded and deterministic; an over-large answer comes back
  // `ok: false` and falls through to clean text plus the approved sentence.
  const diff = useMemo<TranscriptDiff | null>(() => {
    if (rawText === null || output === null) return null;
    return diffTranscripts(rawText, output);
  }, [output, rawText]);

  const shownDiff = diffView === "diff" && diff !== null && diff.ok ? diff : null;
  const diffUnavailable = diffView === "diff" && diff !== null && !diff.ok;
  const { compact, spoken } = bakeoffMetadataFacts(pair.metadata ?? null);
  const failed = pair.state === "failed";
  // A refused retry is the newer fact, so it replaces the original failure
  // reason until the lane produces a fresh terminal state.
  const errorCopy = retryFailure
    ? bakeoffStartErrorCopy(retryFailure)
    : failed
      ? bakeoffPairErrorCopy(pair.errorCode ?? null, modelLabel, model?.source)
      : null;

  return (
    <motion.section
      aria-label={identity}
      aria-describedby={errorCopy ? errorId : undefined}
      data-bakeoff-card={index}
      data-bakeoff-state={pair.state}
      initial={reducedMotion ? { opacity: 0 } : { opacity: 0, y: 6 }}
      animate={reducedMotion ? { opacity: 1 } : { opacity: 1, y: 0 }}
      transition={
        reducedMotion
          ? { duration: 0 }
          : { type: "spring", stiffness: 400, damping: 30 }
      }
      className="shrink-0 rounded-[var(--radius-row)] p-3 space-y-2"
      style={{
        ...insetStyle,
        width: BAKEOFF_CARD_TARGET_PX,
        minWidth: BAKEOFF_CARD_MIN_PX,
        scrollSnapAlign: "start",
      }}
    >
      {/* Identity first, execution facts second, the answer last. */}
      <div className="flex items-start justify-between gap-2">
        <p className="min-w-0 truncate text-[12px] font-medium text-slate">
          {modelLabel} · {profileName}
        </p>
        <Badge
          label={BAKEOFF_PAIR_STATE_LABEL[pair.state]}
          tone={LANE_TONE[pair.state]}
          size="sm"
        />
      </div>

      {compact.length > 0 && (
        <>
          <p aria-hidden="true" className="text-[11px] text-steel leading-relaxed">
            {compact.join(" · ")}
          </p>
          <span className="sr-only">{`${spoken.join(". ")}.`}</span>
        </>
      )}

      {shownDiff !== null && shownDiff.changed && (
        <p className="text-[11px] font-medium">
          <span style={{ color: "var(--color-mint)" }}>+{shownDiff.added}</span>{" "}
          <span style={{ color: "var(--color-rose)" }}>
            {"−"}
            {shownDiff.removed}
          </span>
        </p>
      )}

      {pair.state === "unchanged" && (
        <p className="text-[11px] text-steel leading-relaxed">{UNCHANGED_COPY}</p>
      )}

      {diffUnavailable && (
        <p className="text-[11px] text-steel leading-relaxed">
          {DIFF_FALLBACK_COPY}
        </p>
      )}

      {output !== null && (
        <div
          className="rounded-2xl px-4 py-3 text-[12px] leading-[1.65]"
          style={{
            background: "var(--surface-control)",
            border: "1px solid var(--border-hairline)",
            boxShadow:
              "inset 0 1px 2px rgba(107, 130, 153, 0.10), inset 0 -1px 0 rgba(255, 255, 255, 0.4)",
            fontFamily: "var(--font-quote)",
            fontWeight: 400,
            color: "var(--color-slate)",
            whiteSpace: "pre-wrap",
          }}
        >
          {shownDiff !== null ? <SemanticDiffSegments diff={shownDiff} /> : output}
        </div>
      )}

      {errorCopy && (
        <p id={errorId} className="text-[11px] text-rose leading-relaxed">
          {errorCopy}
        </p>
      )}

      {(failed || pair.state === "cancelled") && onRetry && (
        <SecondaryButton
          onClick={() => onRetry(index)}
          disabled={retrying}
          aria-label={`Retry this result ${identity}`}
          className="rounded-full px-3 h-8 text-[12px] font-medium text-slate"
        >
          Retry this result
        </SecondaryButton>
      )}
    </motion.section>
  );
}
