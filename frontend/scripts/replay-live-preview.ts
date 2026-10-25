/**
 * Privacy-safe stdin bridge for the real HUD dictation reducer.
 *
 * The Python replay harness sends ephemeral preview text on stdin. By default,
 * stdout contains only counts, omission positions, labels, and boolean
 * verdicts. The explicit dev-only --show-text flow asks this script to return
 * reviewed diagnostic text to its caller; no production module imports it.
 */

import { dictationReducer } from "../src/pages/hud/dictationState";
import {
  additivePrefixDuplicatePositions,
  comparableWords,
  exactVisibleGoldAlignment,
  missingExactGoldPositions,
  shouldProtectGoldAnchor,
} from "./live-preview-oracle";

interface PreviewTraceEvent {
  revision: number;
  text: string;
  stable_char_count: number;
  presentation_version: 1;
  mutable_start_char_count: number;
  freeze: boolean;
  timestamp_ms: number;
  active_speech_ms?: number;
  rms_dbfs: number;
  quiet: boolean;
}

interface RecordingTrace {
  label: string;
  recording_id: number;
  expected_final_text?: string;
  events: PreviewTraceEvent[];
}

interface ReplayInput {
  show_text?: boolean;
  recordings: RecordingTrace[];
}

interface TimelineCheckpoint {
  timestamp_ms: number;
  revision: number;
  freeze: boolean;
  quiet: boolean;
  rms_dbfs: number;
  stable_char_count: number;
  mutable_start_char_count: number;
  furthest_gold_position: number;
  gold_interior_omission_positions: number[];
  additive_prefix_duplicate_positions: number[];
  active_speech_ms: number;
  changed_update_gap_ms: number;
  active_speech_since_previous_update_ms: number;
  publication_stall_since_previous_update: boolean;
  stable_prefix_violation: boolean;
  seam_duplicate_candidate: boolean;
  backend_event_text: string;
  hud_visible_text: string;
}

interface ReplayResult {
  label: string;
  events_processed: number;
  pre_freeze_visible_words: number;
  frozen_visible_words: number;
  first_backend_hud_mismatch_revision: number | null;
  backend_text_rendered_exactly: boolean;
  first_stable_prefix_violation_revision: number | null;
  stable_prefix_immutable: boolean;
  first_seam_duplicate_revision: number | null;
  first_seam_duplicate_timestamp_ms: number | null;
  seam_duplicate_candidates: number;
  seam_duplicate_candidates_absent: boolean;
  first_freeze_text_mismatch_revision: number | null;
  freeze_matches_last_live_text: boolean;
  gold_words: number;
  gold_checkpoints: number;
  furthest_gold_position: number;
  first_gold_interior_omission_revision: number | null;
  first_gold_interior_omission_timestamp_ms: number | null;
  first_gold_interior_omission_positions: number[];
  max_gold_interior_omissions: number;
  max_gold_interior_missing_run: number;
  gold_span_preserved: boolean;
  first_additive_prefix_duplicate_revision: number | null;
  first_additive_prefix_duplicate_timestamp_ms: number | null;
  first_additive_prefix_duplicate_positions: number[];
  max_additive_prefix_duplicates: number;
  additive_prefix_duplicates_absent: boolean;
  first_publication_stall_after_revision: number | null;
  first_publication_stall_start_timestamp_ms: number | null;
  first_publication_stall_end_timestamp_ms: number | null;
  first_publication_stall_gap_ms: number;
  first_publication_stall_active_speech_ms: number;
  max_changed_update_gap_ms: number;
  max_changed_update_active_speech_ms: number;
  publication_stall_absent: boolean;
  quiet_checkpoints: number;
  quiet_mismatch_checkpoints: number;
  quiet_checkpoint_max_run: number;
  quiet_span_preserved: boolean;
  freeze_protected_gold_missing_positions: number[];
  freeze_protected_gold_missing_words: number;
  freeze_protected_gold_max_missing_run: number;
  freeze_protected_gold_covered: boolean;
  freeze_full_gold_missing_positions: number[];
  freeze_full_gold_missing_words: number;
  pre_freeze_visible_text?: string;
  frozen_visible_text?: string;
  saved_final_text?: string;
  timeline?: TimelineCheckpoint[];
}

const PUBLICATION_STALL_MIN_GAP_MS = 6_000;
const PUBLICATION_STALL_MIN_ACTIVE_SPEECH_MS = 2_000;

function words(text: string): string[] {
  const normalized = text.trim().replace(/\s+/g, " ");
  return normalized ? normalized.split(" ") : [];
}

function codePointSlice(text: string, start: number, end?: number): string {
  return Array.from(text).slice(start, end).join("");
}

function longestMissingRun(positions: number[]): number {
  let longest = 0;
  let current = 0;
  let previous = -1;
  for (const position of positions) {
    current = position === previous + 1 ? current + 1 : 1;
    longest = Math.max(longest, current);
    previous = position;
  }
  return longest;
}

function replayRecording(
  trace: RecordingTrace,
  showText: boolean,
): ReplayResult {
  let state = dictationReducer(
    {
      authoritative: false,
      recordingId: null,
      origin: null,
      phase: "idle",
      hudEnabled: true,
      previewEnabled: false,
      screenReaderDetected: false,
      preview: null,
      previewRevealed: false,
      composeGeneration: null,
      composePreview: false,
    },
    {
      type: "lifecycle",
      event: {
        type: "dictation.state",
        version: 1,
        recording_id: trace.recording_id,
        phase: "recording",
        origin: "push_to_talk",
        hud_enabled: true,
        preview_enabled: true,
        screen_reader_detected: false,
      },
    },
  );

  let preFreezeVisible = "";
  let frozenVisible = "";
  let firstBackendHudMismatchRevision: number | null = null;
  let previousStablePrefix = "";
  let previousStableCount = 0;
  let firstStablePrefixViolationRevision: number | null = null;
  let firstSeamDuplicateRevision: number | null = null;
  let firstSeamDuplicateTimestamp: number | null = null;
  let seamDuplicateCandidates = 0;
  let firstFreezeTextMismatchRevision: number | null = null;
  let furthestGoldPosition = 0;
  let firstGoldOmissionRevision: number | null = null;
  let firstGoldOmissionTimestamp: number | null = null;
  let firstGoldOmissionPositions: number[] = [];
  let maxGoldOmissions = 0;
  let maxGoldMissingRun = 0;
  let firstPrefixDuplicateRevision: number | null = null;
  let firstPrefixDuplicateTimestamp: number | null = null;
  let firstPrefixDuplicatePositions: number[] = [];
  let maxPrefixDuplicates = 0;
  let previousLiveRevision: number | null = null;
  let previousLiveTimestamp: number | null = null;
  let previousLiveActiveSpeechMs = 0;
  let previousLiveFurthestGoldPosition = 0;
  let firstPublicationStallAfterRevision: number | null = null;
  let firstPublicationStallStartTimestamp: number | null = null;
  let firstPublicationStallEndTimestamp: number | null = null;
  let firstPublicationStallGap = 0;
  let firstPublicationStallActiveSpeech = 0;
  let maxChangedUpdateGap = 0;
  let maxChangedUpdateActiveSpeech = 0;
  let goldCheckpoints = 0;
  let quietCheckpoints = 0;
  let quietMismatchCheckpoints = 0;
  let currentQuietRun = 0;
  let maximumQuietRun = 0;
  const timeline: TimelineCheckpoint[] = [];
  const goldWords = comparableWords(trace.expected_final_text ?? "").length;
  const protectedGoldPositions = new Array<boolean>(goldWords).fill(false);
  const seenExactGoldPositions = new Array<boolean>(goldWords).fill(false);

  for (const event of trace.events) {
    state = dictationReducer(state, {
      type: "preview",
      event: {
        type: "transcription.preview",
        recording_id: trace.recording_id,
        revision: event.revision,
        text: event.text,
        stable_char_count: event.stable_char_count,
        presentation_version: event.presentation_version,
        mutable_start_char_count: event.mutable_start_char_count,
        freeze: event.freeze,
      },
    });
    const visible = state.preview?.text ?? "";
    if (
      visible !== event.text &&
      firstBackendHudMismatchRevision === null
    ) {
      firstBackendHudMismatchRevision = event.revision;
    }
    const stablePrefixViolation =
      event.stable_char_count < previousStableCount ||
      !visible.startsWith(previousStablePrefix);
    if (
      stablePrefixViolation &&
      firstStablePrefixViolationRevision === null
    ) {
      firstStablePrefixViolationRevision = event.revision;
    }
    const stableText = codePointSlice(
      visible,
      0,
      event.stable_char_count,
    );
    const mutableText = codePointSlice(
      visible,
      event.mutable_start_char_count,
    );
    const stableWords = comparableWords(stableText);
    const mutableWords = comparableWords(mutableText);
    const seamDuplicate =
      !event.freeze &&
      stableWords.length > 0 &&
      mutableWords.length > 0 &&
      stableWords[stableWords.length - 1] === mutableWords[0];
    if (seamDuplicate) {
      seamDuplicateCandidates += 1;
      if (firstSeamDuplicateRevision === null) {
        firstSeamDuplicateRevision = event.revision;
        firstSeamDuplicateTimestamp = event.timestamp_ms;
      }
    }
    if (
      event.freeze &&
      visible !== preFreezeVisible &&
      firstFreezeTextMismatchRevision === null
    ) {
      firstFreezeTextMismatchRevision = event.revision;
    }
    const activeSpeechMs = Math.max(
      0,
      Math.trunc(event.active_speech_ms ?? 0),
    );
    const changedUpdateGap =
      previousLiveTimestamp === null
        ? 0
        : Math.max(0, event.timestamp_ms - previousLiveTimestamp);
    const activeSpeechSincePrevious =
      previousLiveTimestamp === null
        ? 0
        : Math.max(0, activeSpeechMs - previousLiveActiveSpeechMs);
    maxChangedUpdateGap = Math.max(
      maxChangedUpdateGap,
      changedUpdateGap,
    );
    maxChangedUpdateActiveSpeech = Math.max(
      maxChangedUpdateActiveSpeech,
      activeSpeechSincePrevious,
    );
    const publicationStall =
      previousLiveRevision !== null &&
      changedUpdateGap >= PUBLICATION_STALL_MIN_GAP_MS &&
      activeSpeechSincePrevious >=
        PUBLICATION_STALL_MIN_ACTIVE_SPEECH_MS &&
      goldWords > 0 &&
      previousLiveFurthestGoldPosition < goldWords;
    if (
      publicationStall &&
      firstPublicationStallAfterRevision === null
    ) {
      firstPublicationStallAfterRevision = previousLiveRevision;
      firstPublicationStallStartTimestamp = previousLiveTimestamp;
      firstPublicationStallEndTimestamp = event.timestamp_ms;
      firstPublicationStallGap = changedUpdateGap;
      firstPublicationStallActiveSpeech =
        activeSpeechSincePrevious;
    }
    if (event.freeze) {
      frozenVisible = visible;
    } else {
      preFreezeVisible = visible;
    }

    const alignment = exactVisibleGoldAlignment(
      trace.expected_final_text ?? "",
      visible,
      event.freeze
        ? undefined
        : furthestGoldPosition +
            Math.max(1, comparableWords(event.text).length),
    );
    const prefixDuplicates = additivePrefixDuplicatePositions(
      trace.expected_final_text ?? "",
      visible,
    );
    maxPrefixDuplicates = Math.max(
      maxPrefixDuplicates,
      prefixDuplicates.length,
    );
    if (
      prefixDuplicates.length > 0 &&
      firstPrefixDuplicateRevision === null
    ) {
      firstPrefixDuplicateRevision = event.revision;
      firstPrefixDuplicateTimestamp = event.timestamp_ms;
      firstPrefixDuplicatePositions = prefixDuplicates;
    }
    let omissions: number[];
    if (event.freeze) {
      omissions = missingExactGoldPositions(
        alignment,
        goldWords,
        protectedGoldPositions,
      );
    } else {
      goldCheckpoints += 1;
      furthestGoldPosition = Math.max(
        furthestGoldPosition,
        alignment.furthestGoldPosition,
      );
      for (
        let index = 0;
        index < alignment.exactCovered.length;
        index += 1
      ) {
        if (
          seenExactGoldPositions[index] &&
          !protectedGoldPositions[index] &&
          !alignment.exactCovered[index] &&
          alignment.alignedVisiblePositions[index] >= 0
        ) {
          // A one-to-one mutable substitution explicitly supersedes the old
          // exact occurrence. A deletion has no aligned visible position and
          // remains sticky so boundary advancement cannot hide it.
          seenExactGoldPositions[index] = false;
        }
      }
      let protectedAnchorEnd = 0;
      for (
        let index = 0;
        index < alignment.exactCovered.length;
        index += 1
      ) {
        if (alignment.exactCovered[index]) {
          seenExactGoldPositions[index] = true;
        }
        // Only an exact one-to-one occurrence before the backend's explicit
        // mutable boundary advances the protection frontier. Exact occurrences
        // seen on an earlier mutable revision also become protected when a
        // later exact anchor crosses them, so a word cannot disappear on the
        // same event that ages it out of the mutable tail. Recognition
        // substitutions remain diagnostic and never establish eligibility.
        if (
          alignment.exactCovered[index] &&
          shouldProtectGoldAnchor({
            exactVisiblePosition:
              alignment.exactVisiblePositions[index],
            visibleText: visible,
            mutableStartCharCount:
              event.mutable_start_char_count,
          })
        ) {
          protectedAnchorEnd = index + 1;
        }
      }
      for (let index = 0; index < protectedAnchorEnd; index += 1) {
        if (seenExactGoldPositions[index]) {
          protectedGoldPositions[index] = true;
        }
      }
      omissions = missingExactGoldPositions(
        alignment,
        goldWords,
        protectedGoldPositions,
      );
      maxGoldOmissions = Math.max(maxGoldOmissions, omissions.length);
      maxGoldMissingRun = Math.max(
        maxGoldMissingRun,
        longestMissingRun(omissions),
      );
      if (
        omissions.length > 0 &&
        firstGoldOmissionRevision === null
      ) {
        firstGoldOmissionRevision = event.revision;
        firstGoldOmissionTimestamp = event.timestamp_ms;
        firstGoldOmissionPositions = omissions;
      }

      if (event.quiet) {
        quietCheckpoints += 1;
        currentQuietRun += 1;
        maximumQuietRun = Math.max(maximumQuietRun, currentQuietRun);
        if (omissions.length > 0) quietMismatchCheckpoints += 1;
      } else {
        currentQuietRun = 0;
      }
    }

    if (showText) {
      timeline.push({
        timestamp_ms: event.timestamp_ms,
        revision: event.revision,
        freeze: event.freeze,
        quiet: event.quiet,
        rms_dbfs: event.rms_dbfs,
        stable_char_count: event.stable_char_count,
        mutable_start_char_count: event.mutable_start_char_count,
        furthest_gold_position: event.freeze
          ? goldWords
          : furthestGoldPosition,
        gold_interior_omission_positions: omissions,
        additive_prefix_duplicate_positions: prefixDuplicates,
        active_speech_ms: activeSpeechMs,
        changed_update_gap_ms: changedUpdateGap,
        active_speech_since_previous_update_ms:
          activeSpeechSincePrevious,
        publication_stall_since_previous_update:
          publicationStall,
        stable_prefix_violation: stablePrefixViolation,
        seam_duplicate_candidate: seamDuplicate,
        backend_event_text: event.text,
        hud_visible_text: visible,
      });
    }
    if (event.stable_char_count >= previousStableCount) {
      previousStablePrefix = stableText;
      previousStableCount = event.stable_char_count;
    }
    if (!event.freeze) {
      previousLiveRevision = event.revision;
      previousLiveTimestamp = event.timestamp_ms;
      previousLiveActiveSpeechMs = activeSpeechMs;
      previousLiveFurthestGoldPosition = furthestGoldPosition;
    }
  }
  if (!frozenVisible) frozenVisible = preFreezeVisible;

  const savedFinalText = trace.expected_final_text ?? "";
  const frozenAlignment = exactVisibleGoldAlignment(
    savedFinalText,
    frozenVisible,
  );
  const freezeProtectedGoldMissing = missingExactGoldPositions(
    frozenAlignment,
    goldWords,
    protectedGoldPositions,
  );
  const freezeFullGoldMissing = missingExactGoldPositions(
    frozenAlignment,
    goldWords,
  );
  const result: ReplayResult = {
    label: trace.label,
    events_processed: trace.events.length,
    pre_freeze_visible_words: words(preFreezeVisible).length,
    frozen_visible_words: words(frozenVisible).length,
    first_backend_hud_mismatch_revision:
      firstBackendHudMismatchRevision,
    backend_text_rendered_exactly:
      firstBackendHudMismatchRevision === null,
    first_stable_prefix_violation_revision:
      firstStablePrefixViolationRevision,
    stable_prefix_immutable:
      firstStablePrefixViolationRevision === null,
    first_seam_duplicate_revision: firstSeamDuplicateRevision,
    first_seam_duplicate_timestamp_ms: firstSeamDuplicateTimestamp,
    seam_duplicate_candidates: seamDuplicateCandidates,
    seam_duplicate_candidates_absent:
      firstSeamDuplicateRevision === null,
    first_freeze_text_mismatch_revision:
      firstFreezeTextMismatchRevision,
    freeze_matches_last_live_text:
      firstFreezeTextMismatchRevision === null,
    gold_words: goldWords,
    gold_checkpoints: goldCheckpoints,
    furthest_gold_position: furthestGoldPosition,
    first_gold_interior_omission_revision:
      firstGoldOmissionRevision,
    first_gold_interior_omission_timestamp_ms:
      firstGoldOmissionTimestamp,
    first_gold_interior_omission_positions:
      firstGoldOmissionPositions,
    max_gold_interior_omissions: maxGoldOmissions,
    max_gold_interior_missing_run: maxGoldMissingRun,
    gold_span_preserved: firstGoldOmissionRevision === null,
    first_additive_prefix_duplicate_revision:
      firstPrefixDuplicateRevision,
    first_additive_prefix_duplicate_timestamp_ms:
      firstPrefixDuplicateTimestamp,
    first_additive_prefix_duplicate_positions:
      firstPrefixDuplicatePositions,
    max_additive_prefix_duplicates: maxPrefixDuplicates,
    additive_prefix_duplicates_absent:
      firstPrefixDuplicateRevision === null,
    first_publication_stall_after_revision:
      firstPublicationStallAfterRevision,
    first_publication_stall_start_timestamp_ms:
      firstPublicationStallStartTimestamp,
    first_publication_stall_end_timestamp_ms:
      firstPublicationStallEndTimestamp,
    first_publication_stall_gap_ms: firstPublicationStallGap,
    first_publication_stall_active_speech_ms:
      firstPublicationStallActiveSpeech,
    max_changed_update_gap_ms: maxChangedUpdateGap,
    max_changed_update_active_speech_ms:
      maxChangedUpdateActiveSpeech,
    publication_stall_absent:
      firstPublicationStallAfterRevision === null,
    quiet_checkpoints: quietCheckpoints,
    quiet_mismatch_checkpoints: quietMismatchCheckpoints,
    quiet_checkpoint_max_run: maximumQuietRun,
    quiet_span_preserved: quietMismatchCheckpoints === 0,
    freeze_protected_gold_missing_positions:
      freezeProtectedGoldMissing,
    freeze_protected_gold_missing_words:
      freezeProtectedGoldMissing.length,
    freeze_protected_gold_max_missing_run: longestMissingRun(
      freezeProtectedGoldMissing,
    ),
    freeze_protected_gold_covered:
      goldWords === 0 || freezeProtectedGoldMissing.length === 0,
    freeze_full_gold_missing_positions: freezeFullGoldMissing,
    freeze_full_gold_missing_words: freezeFullGoldMissing.length,
  };
  if (showText) {
    result.pre_freeze_visible_text = preFreezeVisible;
    result.frozen_visible_text = frozenVisible;
    result.saved_final_text = savedFinalText;
    result.timeline = timeline;
  }
  return result;
}

// Darwin subprocess pipes may be nonblocking. Consume the stream without
// synchronous reads so EAGAIN cannot interrupt the privacy-safe stdin bridge.
process.stdin.setEncoding("utf8");
let payload = "";
for await (const chunk of process.stdin) {
  payload += chunk;
}
const input = JSON.parse(payload) as ReplayInput;
const output = {
  recordings: input.recordings.map((trace) =>
    replayRecording(trace, input.show_text === true),
  ),
};
// Flush the complete report before vite-node closes its execution context.
const stdoutHandle = (process.stdout as typeof process.stdout & {
  _handle?: { setBlocking: (blocking: boolean) => void };
})._handle;
stdoutHandle?.setBlocking(true);
process.stdout.write(`${JSON.stringify(output)}\n`);
