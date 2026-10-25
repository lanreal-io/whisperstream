import { isPlainRecord, hasOnlyKeys, isSequence, isPositiveSequence } from "./validation";


export interface TranscriptBakeoffMetadata {
  serving_contract: "raw-v1" | "wrap-v2" | "chatml" | null;
  device_mode: "cpu" | "gpu" | null;
  resident_warm: boolean | null;
  load_ms: number | null;
  inference_ms: number | null;
  execution_mode: "whole" | "batched" | null;
  chunk_count: number | null;
}


export interface TranscriptBakeoffStartedEvent {
  type: "enhancement.bakeoff.started";
  version: 1;
  run_id: string;
  generation: number;
  total_count: number;
}


export interface TranscriptBakeoffProgressEvent {
  type: "enhancement.bakeoff.progress";
  version: 1;
  run_id: string;
  generation: number;
  pair_index: number;
  state: "queued" | "loading" | "running" | "failed" | "cancelled";
  finished_count: number;
  total_count: number;
  error_code: string | null;
  metadata: TranscriptBakeoffMetadata;
}


export interface TranscriptBakeoffResultEvent {
  type: "enhancement.bakeoff.result";
  version: 1;
  run_id: string;
  generation: number;
  pair_index: number;
  state: "complete" | "unchanged";
  finished_count: number;
  total_count: number;
  output: string;
  metadata: TranscriptBakeoffMetadata;
}


export interface TranscriptBakeoffRevokedEvent {
  type: "enhancement.bakeoff.revoked";
  version: 1;
  run_id: string;
  generation: number;
  reason:
    | "experimental_disabled"
    | "eula_acceptance_required"
    | "lockdown_locked"
    | "shutdown";
}


export type TranscriptBakeoffEvent =
  | TranscriptBakeoffStartedEvent
  | TranscriptBakeoffProgressEvent
  | TranscriptBakeoffResultEvent
  | TranscriptBakeoffRevokedEvent;


export const BAKEOFF_ID_RE = /^[A-Za-z0-9_-]{1,256}$/;

export const BAKEOFF_ERROR_RE = /^[a-z0-9_]{1,64}$/;


export function parseTranscriptBakeoffMetadata(
  value: unknown,
): TranscriptBakeoffMetadata | null {
  if (
    !isPlainRecord(value) ||
    !hasOnlyKeys(value, [
      "serving_contract",
      "device_mode",
      "resident_warm",
      "load_ms",
      "inference_ms",
      "execution_mode",
      "chunk_count",
    ]) ||
    Object.keys(value).length !== 7
  ) {
    return null;
  }
  if (
    ![null, "raw-v1", "wrap-v2", "chatml"].includes(
      value.serving_contract as never,
    )
  ) return null;
  if (![null, "cpu", "gpu"].includes(value.device_mode as never)) {
    return null;
  }
  if (
    value.resident_warm !== null &&
    typeof value.resident_warm !== "boolean"
  ) return null;
  for (const key of ["load_ms", "inference_ms"] as const) {
    if (value[key] !== null && !isSequence(value[key])) return null;
  }
  if (
    ![null, "whole", "batched"].includes(value.execution_mode as never)
  ) return null;
  if (
    value.chunk_count !== null &&
    !isPositiveSequence(value.chunk_count)
  ) return null;
  return value as unknown as TranscriptBakeoffMetadata;
}


/** Strictly validate the privacy-sensitive Bakeoff event family. */
export function parseTranscriptBakeoffEvent(
  value: unknown,
): TranscriptBakeoffEvent | null {
  if (
    !isPlainRecord(value) ||
    typeof value.type !== "string" ||
    value.version !== 1 ||
    typeof value.run_id !== "string" ||
    !BAKEOFF_ID_RE.test(value.run_id) ||
    !isPositiveSequence(value.generation)
  ) {
    return null;
  }
  if (value.type === "enhancement.bakeoff.started") {
    if (!hasOnlyKeys(value, [
      "type", "version", "run_id", "generation", "total_count",
    ])) return null;
    if (
      !isSequence(value.total_count) ||
      value.total_count < 2 ||
      value.total_count > 8
    ) return null;
    return value as unknown as TranscriptBakeoffStartedEvent;
  }
  if (value.type === "enhancement.bakeoff.revoked") {
    if (!hasOnlyKeys(value, [
      "type", "version", "run_id", "generation", "reason",
    ])) return null;
    if (![
      "experimental_disabled",
      "eula_acceptance_required",
      "lockdown_locked",
      "shutdown",
    ].includes(value.reason as string)) return null;
    return value as unknown as TranscriptBakeoffRevokedEvent;
  }
  if (
    value.type !== "enhancement.bakeoff.progress" &&
    value.type !== "enhancement.bakeoff.result"
  ) {
    return null;
  }
  const result = value.type === "enhancement.bakeoff.result";
  const allowed = [
    "type", "version", "run_id", "generation", "pair_index", "state",
    "finished_count", "total_count", "metadata",
    ...(result ? ["output"] : ["error_code"]),
  ];
  if (
    !hasOnlyKeys(value, allowed) ||
    Object.keys(value).length !== allowed.length
  ) return null;
  if (
    !isSequence(value.total_count) ||
    value.total_count < 2 ||
    value.total_count > 8 ||
    !isSequence(value.pair_index) ||
    value.pair_index >= value.total_count ||
    !isSequence(value.finished_count) ||
    value.finished_count > value.total_count ||
    parseTranscriptBakeoffMetadata(value.metadata) === null
  ) return null;
  if (result) {
    if (![
      "complete", "unchanged",
    ].includes(value.state as string)) return null;
    if (
      typeof value.output !== "string" ||
      value.output.length > 800_000
    ) return null;
    return value as unknown as TranscriptBakeoffResultEvent;
  }
  if (![
    "queued", "loading", "running", "failed", "cancelled",
  ].includes(value.state as string)) return null;
  if (
    value.error_code !== null &&
    (typeof value.error_code !== "string" || !BAKEOFF_ERROR_RE.test(value.error_code))
  ) return null;
  return value as unknown as TranscriptBakeoffProgressEvent;
}
