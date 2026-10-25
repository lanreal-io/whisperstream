"""Agreement-based, display-only recognition for ephemeral preview text.

The coordinator repeatedly decodes only the unconfirmed audio tail. Timed
tokens must agree across three completed passes before that prefix is confirmed
and the audio seek point advances. Engines that promise only text instead commit
whole audio segments cut at a pause. It has no connection to final
transcription, injection, persistence, statistics, or logging.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

import numpy as np

from .stt_contracts import DetailedTranscriptionResult, SttPreviewCapability

if TYPE_CHECKING:
    from .audio_capture import PreviewAudioSnapshot


logger = logging.getLogger(__name__)


AGREEMENT_PASSES = 3
MIN_CONFIRM_WORDS = 5
MIN_ACOUSTIC_AGE_SECONDS = 1.0
TIMESTAMP_TOLERANCE_SECONDS = 0.160
SEAM_SEEK_TOLERANCE_SECONDS = 0.001
SEAM_FRONTIER_TOLERANCE_SECONDS = 0.500
SEAM_SUFFIX_GROUPS = 2
SEAM_GUARD_GROUPS = 2
MAX_PREVIEW_WINDOW_SECONDS = 20.0
PREVIEW_BOOTSTRAP_AUDIO_SECONDS = 1.5
PREVIEW_CADENCE_SECONDS = 0.750
PREVIEW_TRAILING_SILENCE_SECONDS = 1.0
PREVIEW_BUFFER_PRESSURE_MARGIN_SECONDS = 2.0
PREVIEW_BUFFER_PRESSURE_RETAIN_SECONDS = 5.0
PREVIEW_PRESENTATION_VERSION = 1
# Text-only engines cannot map words to audio, so a segment is committed whole
# once it is long enough, cut at the quietest frame near its end. This bounds
# each decode well inside the preview ring.
TEXT_SEGMENT_COMMIT_SECONDS = 8.0
TEXT_SEGMENT_BOUNDARY_SEARCH_SECONDS = 3.0
TEXT_SEGMENT_MIN_REMAINDER_SECONDS = 0.3
TEXT_SEGMENT_FRAME_SECONDS = 0.1
_FLOAT_EPSILON = 1e-9
_CONTRACTION_PUNCTUATION = frozenset({"'", "’", "-", "‐", "‑"})


@dataclass(frozen=True, slots=True)
class PreviewUpdate:
    """One safe preview presentation update.

    ``trim_before_seconds`` is an absolute offset in the current recording.
    When present, the coordinator can seek future passes directly to the first
    unconfirmed word. ``reset`` means previous mutable-tail alignment should be
    discarded; confirmed text remains immutable.
    """

    text: str
    stable_char_count: int
    presentation_version: int = PREVIEW_PRESENTATION_VERSION
    mutable_start_char_count: int = 0
    freeze: bool = False
    trim_before_seconds: float | None = None
    reset: bool = False


@dataclass(frozen=True, slots=True)
class _TokenGroup:
    tokens: tuple[str, ...]
    timestamps: tuple[float, ...]
    complete: bool
    acoustic_end_seconds: float

    @property
    def start_seconds(self) -> float:
        return self.timestamps[0]


@dataclass(frozen=True, slots=True)
class _Hypothesis:
    groups: tuple[_TokenGroup, ...]


@dataclass(frozen=True, slots=True)
class _SeamContract:
    """One normal-confirmation boundary that later decodes may echo."""

    confirmed_suffix: tuple[_TokenGroup, ...]
    expected_tail_prefix: tuple[_TokenGroup, ...]
    seek_seconds: float


def _items(value: object) -> tuple[object, ...] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        try:
            return tuple(value)
        except Exception:  # noqa: BLE001 - preview metadata must fail safe
            return None
    return None


def _valid_parallel_numbers(
    value: object,
    *,
    expected_length: int,
    nonnegative: bool,
) -> bool:
    items = _items(value)
    if items is None or len(items) != expected_length:
        return False
    for item in items:
        if isinstance(item, bool):
            return False
        try:
            number = float(item)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(number) or (nonnegative and number < 0):
            return False
    return True


def _is_unsupported_piece(token: str) -> bool:
    # SentencePiece byte fallback and special/control pieces cannot be safely
    # reconstructed here without the recognizer's tokenizer model.
    return len(token) >= 2 and token.startswith("<") and token.endswith(">")


def _detokenize(tokens: Sequence[str]) -> str | None:
    pieces: list[str] = []
    for token in tokens:
        if not token or _is_unsupported_piece(token):
            return None
        if "▁" in token[1:]:
            return None
        pieces.append((" " + token[1:]) if token.startswith("▁") else token)
    return "".join(pieces).lstrip(" ")


def _is_independent_punctuation(token: str) -> bool:
    if token in _CONTRACTION_PUNCTUATION or token.startswith("▁"):
        return False
    return bool(token) and all(unicodedata.category(char).startswith("P") for char in token)


def _make_groups(
    tokens: tuple[str, ...],
    timestamps: tuple[float, ...],
) -> tuple[_TokenGroup, ...] | None:
    raw_groups: list[tuple[list[str], list[float], bool]] = []
    current_tokens: list[str] = []
    current_timestamps: list[float] = []

    def flush_word() -> None:
        if current_tokens:
            raw_groups.append((current_tokens.copy(), current_timestamps.copy(), False))
            current_tokens.clear()
            current_timestamps.clear()

    for token, timestamp in zip(tokens, timestamps, strict=True):
        if token.startswith("▁") or token.startswith(" "):
            flush_word()
            current_tokens.append(token)
            current_timestamps.append(timestamp)
        elif _is_independent_punctuation(token):
            flush_word()
            raw_groups.append(([token], [timestamp], True))
        else:
            current_tokens.append(token)
            current_timestamps.append(timestamp)
    flush_word()

    if not raw_groups:
        return None

    groups: list[_TokenGroup] = []
    for index, (group_tokens, group_timestamps, intrinsically_complete) in enumerate(raw_groups):
        next_start = raw_groups[index + 1][1][0] if index + 1 < len(raw_groups) else None
        complete = intrinsically_complete or next_start is not None
        acoustic_end = next_start if next_start is not None else group_timestamps[-1]
        groups.append(
            _TokenGroup(
                tokens=tuple(group_tokens),
                timestamps=tuple(group_timestamps),
                complete=complete,
                acoustic_end_seconds=acoustic_end,
            )
        )
    return tuple(groups)


def _parse_hypothesis(
    result: DetailedTranscriptionResult,
    *,
    audio_duration_seconds: float,
) -> _Hypothesis | None:
    # ============================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # Use `log_transcription_event()` from whisperstream.logger for metadata only
    # (char count, word count, duration, stage name). Never pass `text`,
    # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
    # ============================================================================
    if not isinstance(result.text, str) or not result.text:
        return None
    token_items = _items(result.tokens)
    timestamp_items = _items(result.timestamps)
    if token_items is None or timestamp_items is None or len(token_items) != len(timestamp_items):
        return None
    if not token_items or any(not isinstance(token, str) for token in token_items):
        return None

    tokens = tuple(token_items)
    if not _valid_parallel_numbers(
        timestamp_items,
        expected_length=len(tokens),
        nonnegative=True,
    ):
        return None
    timestamps = tuple(float(item) for item in timestamp_items)
    if any(later < earlier for earlier, later in zip(timestamps, timestamps[1:])):
        return None
    if timestamps[-1] > audio_duration_seconds + _FLOAT_EPSILON:
        return None

    if result.durations is not None and not _valid_parallel_numbers(
        result.durations,
        expected_length=len(tokens),
        nonnegative=True,
    ):
        return None
    if result.ys_log_probs is not None and not _valid_parallel_numbers(
        result.ys_log_probs,
        expected_length=len(tokens),
        nonnegative=False,
    ):
        return None

    detokenized = _detokenize(tokens)
    if detokenized is None or detokenized != result.text:
        return None
    groups = _make_groups(tokens, timestamps)
    if groups is None:
        return None
    return _Hypothesis(groups=groups)


def _groups_agree(groups: Sequence[_TokenGroup]) -> bool:
    first = groups[0]
    if any(group.tokens != first.tokens for group in groups[1:]):
        return False
    for token_index in range(len(first.timestamps)):
        values = [group.timestamps[token_index] for group in groups]
        if max(values) - min(values) > TIMESTAMP_TOLERANCE_SECONDS + _FLOAT_EPSILON:
            return False
    return True


def _flatten_tokens(groups: Sequence[_TokenGroup]) -> tuple[str, ...]:
    return tuple(token for group in groups for token in group.tokens)


def _group_key(group: _TokenGroup) -> str | None:
    if all(_is_independent_punctuation(token) for token in group.tokens):
        return None
    text = _detokenize(group.tokens)
    if text is None:
        return None
    normalized = unicodedata.normalize("NFKC", text).casefold()
    while (
        normalized
        and normalized[0] not in _CONTRACTION_PUNCTUATION
        and unicodedata.category(normalized[0]).startswith("P")
    ):
        normalized = normalized[1:]
    while (
        normalized
        and normalized[-1] not in _CONTRACTION_PUNCTUATION
        and unicodedata.category(normalized[-1]).startswith("P")
    ):
        normalized = normalized[:-1]
    return normalized or None


def _word_groups(
    groups: Sequence[_TokenGroup],
) -> tuple[_TokenGroup, ...]:
    return tuple(group for group in groups if _group_key(group) is not None)


def _word_group_entries(
    groups: Sequence[_TokenGroup],
) -> tuple[tuple[int, _TokenGroup], ...]:
    return tuple(
        (index, group)
        for index, group in enumerate(groups)
        if _group_key(group) is not None
    )


def _same_group_sequence(
    left: Sequence[_TokenGroup],
    right: Sequence[_TokenGroup],
) -> bool:
    if len(left) != len(right):
        return False
    left_keys = tuple(_group_key(group) for group in left)
    right_keys = tuple(_group_key(group) for group in right)
    return (
        all(key is not None for key in left_keys)
        and left_keys == right_keys
    )


class ParakeetPreviewAgreement:
    """Conservative three-pass agreement over an unconfirmed audio tail.

    Confirmed words are retained independently from the mutable hypothesis.
    Future recognizer passes begin at the first unconfirmed word, mirroring the
    agreement/seek architecture used by mature local streaming implementations.
    """

    def __init__(self) -> None:
        self._confirmed_groups: list[_TokenGroup] = []
        self._history: deque[_Hypothesis] = deque(maxlen=AGREEMENT_PASSES)
        self._latest_tail_groups: tuple[_TokenGroup, ...] = ()
        self._seam_contract: _SeamContract | None = None
        self._last_update = PreviewUpdate(
            text="",
            stable_char_count=0,
        )
        self._latest_full_update = PreviewUpdate(
            text="",
            stable_char_count=0,
            freeze=True,
        )

    def reset(self) -> None:
        self._confirmed_groups.clear()
        self._history.clear()
        self._latest_tail_groups = ()
        self._seam_contract = None
        self._last_update = PreviewUpdate(
            text="",
            stable_char_count=0,
        )
        self._latest_full_update = PreviewUpdate(
            text="",
            stable_char_count=0,
            freeze=True,
        )

    def restart_unconfirmed_tail(self) -> None:
        """Forget tail hypotheses while preserving already confirmed text."""
        self._history.clear()

    def preserve_before_unavailable_audio(
        self,
        unavailable_before_seconds: float,
    ) -> None:
        """Confirm the latest prefix whose source audio is leaving the ring.

        Normal sentence confirmation or the buffer-pressure valve should
        advance first. This last-resort path prefers a possibly imperfect
        current prefix over silently losing speech that can no longer be
        decoded. Once promoted here, that prefix is immutable. A bounded seam
        contract lets the next decode retain available acoustic context while
        removing only a uniquely proven echo of the promoted suffix.
        """
        if not self._latest_tail_groups:
            self._set_latest_full_update(())
            return
        preserve_count = 0
        for group in self._latest_tail_groups:
            if group.start_seconds + _FLOAT_EPSILON >= unavailable_before_seconds:
                break
            preserve_count += 1
        if preserve_count == 0:
            self._history.clear()
            return
        self._confirmed_groups.extend(
            self._latest_tail_groups[:preserve_count]
        )
        remaining = self._latest_tail_groups[preserve_count:]
        confirmed_words = _word_groups(self._confirmed_groups)
        remaining_words = _word_groups(remaining)
        self._seam_contract = (
            _SeamContract(
                confirmed_suffix=tuple(
                    confirmed_words[-SEAM_SUFFIX_GROUPS:]
                ),
                expected_tail_prefix=tuple(
                    remaining_words[:SEAM_GUARD_GROUPS]
                ),
                seek_seconds=unavailable_before_seconds,
            )
            if confirmed_words and remaining_words
            else None
        )
        self._history.clear()
        if remaining:
            self._history.append(_Hypothesis(groups=remaining))
        self._latest_tail_groups = remaining
        self._set_latest_full_update(remaining)

    def _normalize_confirmed_seam(
        self,
        hypothesis: _Hypothesis,
        *,
        audio_offset_seconds: float,
    ) -> _Hypothesis:
        """Strip one uniquely guarded echo at a confirmed-audio seam."""
        contract = self._seam_contract
        if (
            contract is None
            or abs(audio_offset_seconds - contract.seek_seconds)
            > SEAM_SEEK_TOLERANCE_SECONDS + _FLOAT_EPSILON
        ):
            return hypothesis

        groups = hypothesis.groups
        word_entries = _word_group_entries(groups)
        matches: list[tuple[int, int]] = []
        maximum_echo = min(
            SEAM_SUFFIX_GROUPS,
            len(contract.confirmed_suffix),
        )
        for echo_count in range(1, maximum_echo + 1):
            suffix = contract.confirmed_suffix[-echo_count:]
            guard = contract.expected_tail_prefix
            if len(word_entries) < echo_count + len(guard):
                continue
            echoed = tuple(
                group for _index, group in word_entries[:echo_count]
            )
            candidate_guard_entries = word_entries[
                echo_count:echo_count + len(guard)
            ]
            candidate_guard = tuple(
                group for _index, group in candidate_guard_entries
            )
            if not _same_group_sequence(echoed, suffix):
                continue
            if not _same_group_sequence(candidate_guard, guard):
                continue
            if (
                len(guard) >= echo_count
                and _same_group_sequence(guard[:echo_count], suffix)
            ):
                # The confirming hypothesis itself contains the same immediate
                # repeat, so the later leading occurrence is ambiguous.
                continue
            candidate_start = word_entries[0][1].start_seconds
            confirmed_end = suffix[-1].acoustic_end_seconds
            if (
                candidate_start
                > contract.seek_seconds
                + SEAM_FRONTIER_TOLERANCE_SECONDS
                + _FLOAT_EPSILON
                or candidate_start
                > confirmed_end
                + SEAM_FRONTIER_TOLERANCE_SECONDS
                + _FLOAT_EPSILON
            ):
                continue
            if any(
                abs(candidate.start_seconds - expected.start_seconds)
                > SEAM_FRONTIER_TOLERANCE_SECONDS + _FLOAT_EPSILON
                for candidate, expected in zip(
                    candidate_guard,
                    guard,
                    strict=True,
                )
            ):
                continue
            matches.append(
                (
                    echo_count,
                    candidate_guard_entries[0][0],
                )
            )

        if len(matches) != 1:
            return hypothesis
        return replace(
            hypothesis,
            groups=groups[matches[0][1]:],
        )

    def _compose(self, groups: Sequence[_TokenGroup]) -> tuple[str, int]:
        all_groups = [*self._confirmed_groups, *groups]
        text = _detokenize(_flatten_tokens(all_groups)) or ""
        stable_text = (
            _detokenize(_flatten_tokens(self._confirmed_groups)) or ""
        )
        return text, len(stable_text)

    def _set_latest_full_update(
        self,
        current_groups: Sequence[_TokenGroup],
    ) -> None:
        text, _stable_char_count = self._compose(current_groups)
        self._latest_full_update = PreviewUpdate(
            text=text,
            stable_char_count=len(text),
            mutable_start_char_count=len(text),
            freeze=True,
        )

    def _compose_update(
        self,
        *,
        current_groups: Sequence[_TokenGroup],
        trim_before_seconds: float | None,
    ) -> PreviewUpdate:
        text, stable_char_count = self._compose(current_groups)
        update = PreviewUpdate(
            text=text,
            stable_char_count=stable_char_count,
            mutable_start_char_count=stable_char_count,
            trim_before_seconds=trim_before_seconds,
        )
        self._latest_tail_groups = tuple(current_groups)
        self._last_update = update
        self._set_latest_full_update(current_groups)
        return update

    def freeze(self) -> PreviewUpdate:
        """Promote the latest decoded hypothesis without another ASR pass."""
        return self._latest_full_update

    def update(
        self,
        result: DetailedTranscriptionResult,
        *,
        audio_duration_seconds: float,
        audio_offset_seconds: float = 0.0,
    ) -> PreviewUpdate:
        """Reconcile one pass over the current unconfirmed audio tail."""
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if (
            isinstance(audio_duration_seconds, bool)
            or not math.isfinite(audio_duration_seconds)
            or audio_duration_seconds < 0
            or isinstance(audio_offset_seconds, bool)
            or not math.isfinite(audio_offset_seconds)
            or audio_offset_seconds < 0
        ):
            self.restart_unconfirmed_tail()
            return replace(
                self._last_update,
                trim_before_seconds=None,
                reset=True,
            )

        hypothesis = _parse_hypothesis(
            result,
            audio_duration_seconds=audio_duration_seconds,
        )
        if hypothesis is None:
            self.restart_unconfirmed_tail()
            return replace(
                self._last_update,
                trim_before_seconds=None,
                reset=True,
            )

        if audio_offset_seconds:
            shifted_groups = tuple(
                replace(
                    group,
                    timestamps=tuple(
                        timestamp + audio_offset_seconds
                        for timestamp in group.timestamps
                    ),
                    acoustic_end_seconds=(
                        group.acoustic_end_seconds + audio_offset_seconds
                    ),
                )
                for group in hypothesis.groups
            )
            hypothesis = _Hypothesis(groups=shifted_groups)

        hypothesis = self._normalize_confirmed_seam(
            hypothesis,
            audio_offset_seconds=audio_offset_seconds,
        )
        self._history.append(hypothesis)
        newly_stable = 0
        if len(self._history) == AGREEMENT_PASSES:
            common_length = min(len(item.groups) for item in self._history)
            newest = self._history[-1]
            agreed_count = 0
            for group_index in range(common_length):
                candidates = tuple(item.groups[group_index] for item in self._history)
                if not all(candidate.complete for candidate in candidates):
                    break
                if not _groups_agree(candidates):
                    break
                agreed_count += 1

            sentence_enders = frozenset({".", "!", "?", ";"})
            punctuation_indices = [
                index
                for index, group in enumerate(newest.groups[:agreed_count])
                if (
                    group.tokens
                    and group.tokens[-1]
                    and group.tokens[-1][-1] in sentence_enders
                )
            ]
            confirm_up_to = 0
            if len(punctuation_indices) >= 3:
                confirm_up_to = punctuation_indices[-3] + 1
            elif (
                audio_duration_seconds
                >= (
                    MAX_PREVIEW_WINDOW_SECONDS
                    - PREVIEW_BUFFER_PRESSURE_MARGIN_SECONDS
                    + PREVIEW_TRAILING_SILENCE_SECONDS
                )
            ):
                # VoiceInk normally commits only at sentence boundaries. A
                # bounded desktop capture ring also needs a pressure valve for
                # unusually long clauses so the ring cannot overtake speech
                # that has already agreed across all three passes.
                absolute_audio_end = (
                    audio_offset_seconds + audio_duration_seconds
                )
                for index, group in enumerate(
                    newest.groups[:agreed_count],
                    start=1,
                ):
                    acoustic_age = (
                        absolute_audio_end - group.acoustic_end_seconds
                    )
                    if (
                        acoustic_age + _FLOAT_EPSILON
                        < PREVIEW_BUFFER_PRESSURE_RETAIN_SECONDS
                    ):
                        break
                    confirm_up_to = index

            if confirm_up_to:
                confirmed_word_count = sum(
                    not all(
                        _is_independent_punctuation(token)
                        for token in group.tokens
                    )
                    for group in newest.groups[:confirm_up_to]
                )
                acoustic_age = (
                    audio_offset_seconds
                    + audio_duration_seconds
                    - newest.groups[confirm_up_to - 1].acoustic_end_seconds
                )
                if (
                    confirmed_word_count >= MIN_CONFIRM_WORDS
                    and acoustic_age + _FLOAT_EPSILON
                    >= MIN_ACOUSTIC_AGE_SECONDS
                ):
                    self._confirmed_groups.extend(
                        newest.groups[:confirm_up_to]
                    )
                    newly_stable = confirm_up_to
                    expected_tail = newest.groups[confirm_up_to:]
                    confirmed_words = _word_groups(
                        self._confirmed_groups
                    )
                    expected_tail_words = _word_groups(expected_tail)
                    self._seam_contract = (
                        _SeamContract(
                            confirmed_suffix=tuple(
                                confirmed_words[-SEAM_SUFFIX_GROUPS:]
                            ),
                            expected_tail_prefix=tuple(
                                expected_tail_words[:SEAM_GUARD_GROUPS]
                            ),
                            seek_seconds=(
                                expected_tail[0].start_seconds
                                if expected_tail
                                else self._confirmed_groups[
                                    -1
                                ].acoustic_end_seconds
                            ),
                        )
                        if confirmed_words and expected_tail_words
                        else None
                    )

        trim_before_seconds: float | None = None
        if newly_stable:
            for index, prior in enumerate(tuple(self._history)):
                self._history[index] = replace(
                    prior,
                    groups=prior.groups[newly_stable:],
                )
            newest_tail = self._history[-1].groups
            if newest_tail:
                trim_before_seconds = newest_tail[0].start_seconds
            else:
                trim_before_seconds = self._confirmed_groups[-1].acoustic_end_seconds

        current_groups = self._history[-1].groups if self._history else ()
        return self._compose_update(
            current_groups=current_groups,
            trim_before_seconds=trim_before_seconds,
        )


def _join_preview_text(left: str, right: str) -> str:
    # ============================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # ============================================================================
    if not left:
        return right
    if not right:
        return left
    # Scripts written without spaces (CJK) join directly; others need a space.
    if ord(left[-1]) >= 0x2E80 or ord(right[0]) >= 0x2E80:
        return left + right
    return f"{left} {right}"


def quiet_segment_boundary(audio: np.ndarray, sample_rate: int) -> int:
    """Return the sample index of the quietest frame near the segment end."""
    frame = max(1, int(round(sample_rate * TEXT_SEGMENT_FRAME_SECONDS)))
    end = audio.size - int(round(sample_rate * TEXT_SEGMENT_MIN_REMAINDER_SECONDS))
    start = max(
        frame,
        audio.size - int(round(sample_rate * TEXT_SEGMENT_BOUNDARY_SEARCH_SECONDS)),
    )
    if end - frame < start:
        return max(1, end)
    best_index = end
    best_energy = math.inf
    for frame_start in range(start, end - frame + 1, frame):
        window = audio[frame_start:frame_start + frame]
        energy = float(np.mean(window * window))
        if energy < best_energy:
            best_energy = energy
            best_index = frame_start + frame // 2
    return best_index


class TextPreviewAgreement:
    """Segment-commit preview for engines that return text without timings.

    The newest hypothesis for the open segment is shown as a mutable tail.
    Committed segments are immutable and never re-decoded.
    """

    def __init__(self) -> None:
        self._committed = ""
        self._tail = ""

    def _compose(self) -> PreviewUpdate:
        text = _join_preview_text(self._committed, self._tail)
        stable = len(self._committed)
        return PreviewUpdate(
            text=text,
            stable_char_count=stable,
            mutable_start_char_count=stable,
        )

    def update_tail(self, text: str) -> PreviewUpdate:
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        self._tail = text.strip()
        return self._compose()

    def commit_segment(self, text: str) -> PreviewUpdate:
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        self._committed = _join_preview_text(self._committed, text.strip())
        self._tail = ""
        return self._compose()

    def preserve_before_unavailable_audio(self, _seconds: float) -> None:
        """Keep the open segment's latest text when its audio leaves the ring."""
        self.commit_segment(self._tail)

    def freeze(self) -> PreviewUpdate:
        text = _join_preview_text(self._committed, self._tail)
        return PreviewUpdate(
            text=text,
            stable_char_count=len(text),
            mutable_start_char_count=len(text),
            freeze=True,
        )


class LivePreviewCoordinator:
    """One recording's best-effort, single-flight agreement scheduler.

    Audio notifications collapse into one pending bit. Each admitted pass
    decodes the bounded audio tail beginning at the first unconfirmed word.
    Three timed-token agreements advance that seek point; the mutable
    hypothesis may revise while the confirmed prefix remains immutable.
    ``text_only`` engines use segment commits instead of timed agreement.
    """

    def __init__(
        self,
        *,
        recording_id: int,
        stt_manager: SttPreviewCapability,
        snapshot_provider: Callable[[], PreviewAudioSnapshot],
        publish: Callable[[int, int, PreviewUpdate], None],
        sample_rate: int = 16_000,
        min_audio_seconds: float = PREVIEW_BOOTSTRAP_AUDIO_SECONDS,
        cadence_seconds: float = PREVIEW_CADENCE_SECONDS,
        max_audio_seconds: float = MAX_PREVIEW_WINDOW_SECONDS,
        text_only: bool = False,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if (
            min_audio_seconds <= 0
            or cadence_seconds < 0
            or max_audio_seconds <= 0
        ):
            raise ValueError("preview timing values must be nonnegative")
        if min_audio_seconds > max_audio_seconds:
            raise ValueError("preview minimum must fit inside the audio window")
        self.recording_id = recording_id
        self._stt_manager = stt_manager
        self._snapshot_provider = snapshot_provider
        self._publish = publish
        self._sample_rate = sample_rate
        self._min_samples = int(round(sample_rate * min_audio_seconds))
        self._cadence_seconds = cadence_seconds
        self._max_samples = int(round(sample_rate * max_audio_seconds))
        self._agreement = ParakeetPreviewAgreement()
        self._text_agreement = TextPreviewAgreement() if text_only else None
        self._segment_commit_samples = int(
            round(sample_rate * TEXT_SEGMENT_COMMIT_SECONDS)
        )
        self._condition = threading.Condition()
        self._active = False
        self._pending = False
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()
        self._stopped.set()
        self._decode_start_sample = 0
        self._last_attempt_total = -1
        self._last_update = PreviewUpdate(text="", stable_char_count=0)
        self._latest_full_update = PreviewUpdate(text="", stable_char_count=0)
        self._last_completion = 0.0
        self._revision = 0

    def start(self) -> None:
        """Arm this generation and create its sole waiting worker."""
        with self._condition:
            self._active = True
            if self._thread is None:
                self._stopped.clear()
                self._thread = threading.Thread(
                    target=self._run,
                    daemon=True,
                    name=f"live-preview-{self.recording_id}",
                )
                self._thread.start()

    def notify_audio(self) -> None:
        """Coalesce captured audio into the latest pending preview attempt."""
        with self._condition:
            if not self._active:
                return
            self._pending = True
            self._condition.notify_all()

    def invalidate(self) -> None:
        """Invalidate immediately without joining an in-flight native decode."""
        with self._condition:
            self._active = False
            self._pending = False
            self._condition.notify_all()

    def freeze(self) -> None:
        """Publish the latest complete hypothesis without running another decode."""
        with self._condition:
            if not self._active or not self._latest_full_update.text:
                return
            update = self._latest_full_update
            if update == self._last_update:
                return
            self._last_update = update
            self._revision += 1
            revision = self._revision
        try:
            self._publish(self.recording_id, revision, update)
        except Exception as exc:  # noqa: BLE001 - preview is best effort
            logger.warning(
                "preview publish failed reason=%s", type(exc).__name__
            )

    def wait_stopped(self, timeout: float | None = None) -> bool:
        """Test/shutdown aid. The hotkey stop path deliberately never calls it."""
        return self._stopped.wait(timeout)

    def _run(self) -> None:
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        try:
            while True:
                with self._condition:
                    while self._active:
                        if not self._pending:
                            self._condition.wait()
                            continue
                        remaining = (
                            self._last_completion
                            + self._cadence_seconds
                            - time.monotonic()
                        )
                        if remaining > 0:
                            self._condition.wait(timeout=remaining)
                            continue
                        self._pending = False
                        break
                    if not self._active:
                        return

                try:
                    snapshot = self._snapshot_provider()
                    audio = np.asarray(snapshot.audio, dtype=np.float32).reshape(-1)
                    total_samples = int(snapshot.total_samples)
                    start_sample = int(snapshot.start_sample)
                except Exception as exc:  # noqa: BLE001 - preview must fail safe
                    logger.warning(
                        "preview snapshot failed reason=%s", type(exc).__name__
                    )
                    continue

                if audio.size > self._max_samples:
                    audio = audio[-self._max_samples :].copy()
                    start_sample = total_samples - audio.size

                if self._decode_start_sample < start_sample:
                    # Confirmation normally advances the seek point before the
                    # ring can overtake it. If inference was starved for more
                    # than the bounded window, preserve confirmed text and
                    # restart agreement from the oldest audio still available.
                    (self._text_agreement or self._agreement).preserve_before_unavailable_audio(
                        start_sample / self._sample_rate
                    )
                    self._decode_start_sample = start_sample
                if total_samples <= self._last_attempt_total:
                    continue
                local_start = self._decode_start_sample - start_sample
                preview_audio = audio[local_start:].copy()
                if preview_audio.size < self._min_samples:
                    continue
                self._last_attempt_total = total_samples
                boundary = None
                if (
                    self._text_agreement is not None
                    and preview_audio.size >= self._segment_commit_samples
                ):
                    boundary = quiet_segment_boundary(
                        preview_audio, self._sample_rate
                    )
                    preview_audio = preview_audio[:boundary]
                trailing_silence = np.zeros(
                    int(round(
                        self._sample_rate * PREVIEW_TRAILING_SILENCE_SECONDS
                    )),
                    dtype=np.float32,
                )
                decode_audio = np.concatenate(
                    (preview_audio, trailing_silence)
                )

                try:
                    result = self._stt_manager.try_transcribe_preview(
                        decode_audio,
                        sample_rate=self._sample_rate,
                    )
                except Exception as exc:  # noqa: BLE001 - preview is best effort
                    logger.warning(
                        "preview decode failed reason=%s", type(exc).__name__
                    )
                    result = None
                finally:
                    with self._condition:
                        self._last_completion = time.monotonic()

                if result is None:
                    continue

                if self._text_agreement is not None:
                    text = result.text if isinstance(result.text, str) else ""
                    if boundary is not None:
                        update = self._text_agreement.commit_segment(text)
                        self._decode_start_sample += boundary
                        with self._condition:
                            # Decode the remainder without waiting for audio.
                            self._last_attempt_total = -1
                            self._pending = True
                    else:
                        update = self._text_agreement.update_tail(text)
                    full_update = self._text_agreement.freeze()
                    with self._condition:
                        if not self._active:
                            return
                        self._latest_full_update = full_update
                        if update == self._last_update:
                            continue
                        self._last_update = update
                        self._revision += 1
                        revision = self._revision
                    try:
                        self._publish(self.recording_id, revision, update)
                    except Exception as exc:  # noqa: BLE001 - preview is best effort
                        logger.warning(
                            "preview publish failed reason=%s", type(exc).__name__
                        )
                    continue

                update = self._agreement.update(
                    result,
                    audio_duration_seconds=(
                        decode_audio.size / self._sample_rate
                    ),
                    audio_offset_seconds=(
                        self._decode_start_sample / self._sample_rate
                    ),
                )
                full_update = self._agreement.freeze()
                if update.trim_before_seconds is not None:
                    trim_sample = int(
                        round(update.trim_before_seconds * self._sample_rate)
                    )
                    self._decode_start_sample = max(
                        self._decode_start_sample,
                        min(trim_sample, total_samples),
                    )

                with self._condition:
                    if not self._active:
                        return
                    self._latest_full_update = full_update
                    if update == self._last_update:
                        continue
                    self._last_update = update
                    self._revision += 1
                    revision = self._revision
                try:
                    self._publish(self.recording_id, revision, update)
                except Exception as exc:  # noqa: BLE001 - preview is best effort
                    logger.warning(
                        "preview publish failed reason=%s", type(exc).__name__
                    )
        finally:
            self._stopped.set()


__all__ = [
    "LivePreviewCoordinator",
    "MAX_PREVIEW_WINDOW_SECONDS",
    "PREVIEW_BOOTSTRAP_AUDIO_SECONDS",
    "ParakeetPreviewAgreement",
    "PREVIEW_CADENCE_SECONDS",
    "PreviewUpdate",
    "TextPreviewAgreement",
]
