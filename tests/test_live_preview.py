from __future__ import annotations

import threading
import time
from dataclasses import replace

import numpy as np
import pytest

from whisperstream.audio_capture import PreviewAudioSnapshot
from whisperstream.live_preview import (
    LivePreviewCoordinator,
    ParakeetPreviewAgreement,
    PreviewUpdate,
    TextPreviewAgreement,
    quiet_segment_boundary,
)
from whisperstream.onnx_stt_manager import DetailedTranscriptionResult


pytestmark = pytest.mark.usefixtures("accepted_eula")


AGREEMENT_TEXT = (
    "one two three four five. six seven. eight nine. "
    "ten eleven. twelve thirteen. tail"
)
AGREEMENT_TIMESTAMPS = tuple(0.2 + (0.4 * index) for index in range(14))
CONFIRMED_PREFIX = "one two three four five. six seven. eight nine."
FIRST_UNCONFIRMED_TIMESTAMP = AGREEMENT_TIMESTAMPS[9]


def detailed(
    text: str,
    tokens: tuple[str, ...] | None,
    timestamps: tuple[float, ...] | None,
    *,
    durations: tuple[float, ...] | None = None,
    log_probs: tuple[float, ...] | None = None,
) -> DetailedTranscriptionResult:
    return DetailedTranscriptionResult(
        text=text,
        tokens=tokens,
        timestamps=timestamps,
        durations=durations,
        ys_log_probs=log_probs,
    )


def leading_space_result(
    text: str,
    timestamps: tuple[float, ...] | None = None,
) -> DetailedTranscriptionResult:
    words = tuple(text.split())
    if timestamps is None:
        timestamps = tuple(0.1 + (0.4 * index) for index in range(len(words)))
    assert len(words) == len(timestamps)
    return detailed(
        text,
        tuple(f" {word}" for word in words),
        timestamps,
    )


def agreement_result() -> DetailedTranscriptionResult:
    return leading_space_result(AGREEMENT_TEXT, AGREEMENT_TIMESTAMPS)


def run_passes(
    engine: ParakeetPreviewAgreement,
    result: DetailedTranscriptionResult,
    *,
    count: int = 3,
    audio_duration_seconds: float = 7.0,
    audio_offset_seconds: float = 0.0,
) -> PreviewUpdate:
    update = PreviewUpdate(text="", stable_char_count=0)
    for _ in range(count):
        update = engine.update(
            result,
            audio_duration_seconds=audio_duration_seconds,
            audio_offset_seconds=audio_offset_seconds,
        )
    return update


def live_update(
    text: str,
    *,
    stable: int = 0,
    mutable: int = 0,
    trim: float | None = None,
    reset: bool = False,
) -> PreviewUpdate:
    return PreviewUpdate(
        text=text,
        stable_char_count=stable,
        mutable_start_char_count=mutable,
        trim_before_seconds=trim,
        reset=reset,
    )


def frozen_update(text: str) -> PreviewUpdate:
    return PreviewUpdate(
        text=text,
        stable_char_count=len(text),
        mutable_start_char_count=len(text),
        freeze=True,
    )


def test_newest_multi_token_tail_atomically_replaces_provisional_opening():
    engine = ParakeetPreviewAgreement()

    provisional = engine.update(
        leading_space_result("Hello, Haza go"),
        audio_duration_seconds=1.2,
    )
    corrected = engine.update(
        leading_space_result("Hello, how's it going?"),
        audio_duration_seconds=1.6,
    )
    extended = engine.update(
        leading_space_result(
            "Hello, how's it going? Just want to see it sort."
        ),
        audio_duration_seconds=4.0,
    )

    assert provisional == live_update("Hello, Haza go")
    assert corrected == live_update("Hello, how's it going?")
    assert extended == live_update(
        "Hello, how's it going? Just want to see it sort."
    )
    assert "Haza go" not in extended.text


def test_many_to_many_tail_corrections_always_equal_newest_hypothesis():
    engine = ParakeetPreviewAgreement()

    still_on = engine.update(
        leading_space_result("Um is this still on? What's that?"),
        audio_duration_seconds=2.8,
    )
    stone = engine.update(
        leading_space_result("Um is this stone? What's happening?"),
        audio_duration_seconds=2.8,
    )
    corrected_back = engine.update(
        leading_space_result(
            "Um is this still on? What's happening now?"
        ),
        audio_duration_seconds=3.2,
    )

    assert still_on.text == "Um is this still on? What's that?"
    assert stone.text == "Um is this stone? What's happening?"
    assert corrected_back.text == (
        "Um is this still on? What's happening now?"
    )
    assert "stone? on?" not in corrected_back.text


def test_newest_tail_preserves_intentional_repeated_words_exactly():
    engine = ParakeetPreviewAgreement()

    update = engine.update(
        leading_space_result("this is very very good good"),
        audio_duration_seconds=2.8,
    )

    assert update == live_update("this is very very good good")
    assert update.text.split().count("very") == 2
    assert update.text.split().count("good") == 2


def test_identical_quiet_pass_holds_then_resumed_speech_replaces_tail():
    engine = ParakeetPreviewAgreement()
    quiet = leading_space_result("hello how are you")

    first = engine.update(quiet, audio_duration_seconds=2.0)
    unchanged = engine.update(quiet, audio_duration_seconds=2.8)
    resumed = engine.update(
        leading_space_result("hello how are you doing today"),
        audio_duration_seconds=3.2,
    )

    assert unchanged == first
    assert resumed == live_update("hello how are you doing today")


def test_real_parakeet_leading_space_tokens_form_word_groups():
    engine = ParakeetPreviewAgreement()
    result = detailed(
        "hello world again fourth fifth sixth",
        (" hello", " world", " again", " fourth", " fifth", " sixth"),
        (0.1, 0.6, 1.1, 1.4, 1.7, 2.0),
    )

    update = engine.update(result, audio_duration_seconds=2.5)

    assert update == live_update("hello world again fourth fifth sixth")
    assert engine.freeze() == frozen_update(
        "hello world again fourth fifth sixth",
    )


def test_newest_unconfirmed_tail_replaces_prior_tail_without_union():
    engine = ParakeetPreviewAgreement()

    first = engine.update(
        leading_space_result("alpha beta gamma delta epsilon zeta eta"),
        audio_duration_seconds=3.0,
    )
    rolling = engine.update(
        leading_space_result(
            "alpha beta gamma delta epsilon zeta eta theta"
        ),
        audio_duration_seconds=3.5,
    )
    corrected = engine.update(
        leading_space_result("alpine bravo charlie delta echo zulu eta"),
        audio_duration_seconds=3.0,
    )

    assert first == live_update(
        "alpha beta gamma delta epsilon zeta eta"
    )
    assert rolling == live_update(
        "alpha beta gamma delta epsilon zeta eta theta"
    )
    assert corrected == live_update(
        "alpine bravo charlie delta echo zulu eta"
    )
    assert engine.freeze() == frozen_update(
        "alpine bravo charlie delta echo zulu eta",
    )


def test_mutable_presentation_advances_with_the_latest_spoken_words():
    engine = ParakeetPreviewAgreement()

    first = engine.update(
        leading_space_result("one two three four five"),
        audio_duration_seconds=2.0,
    )
    later = engine.update(
        leading_space_result("one two three four five six seven eight"),
        audio_duration_seconds=3.5,
    )
    rolling = engine.update(
        leading_space_result(
            "one two three four five six seven eight nine"
        ),
        audio_duration_seconds=4.0,
    )

    assert first == live_update("one two three four five")
    assert later == live_update("one two three four five six seven eight")
    assert rolling == live_update(
        "one two three four five six seven eight nine"
    )


def test_three_matching_timed_passes_confirm_through_third_newest_sentence():
    engine = ParakeetPreviewAgreement()
    result = agreement_result()

    first = engine.update(result, audio_duration_seconds=7.0)
    second = engine.update(result, audio_duration_seconds=7.0)
    third = engine.update(result, audio_duration_seconds=7.0)

    assert first == live_update(
        AGREEMENT_TEXT,
    )
    assert second == live_update(
        AGREEMENT_TEXT,
    )
    assert third == live_update(
        AGREEMENT_TEXT,
        stable=len(CONFIRMED_PREFIX),
        mutable=len(CONFIRMED_PREFIX),
        trim=pytest.approx(FIRST_UNCONFIRMED_TIMESTAMP),
    )
    assert engine.freeze() == frozen_update(AGREEMENT_TEXT)


def test_confirmed_prefix_is_immutable_while_newest_tail_rewrites():
    engine = ParakeetPreviewAgreement()
    confirmed = run_passes(engine, agreement_result())
    assert confirmed.stable_char_count == len(CONFIRMED_PREFIX)

    first_tail = engine.update(
        leading_space_result("ten wrong tail"),
        audio_duration_seconds=2.0,
        audio_offset_seconds=FIRST_UNCONFIRMED_TIMESTAMP,
    )
    corrected_tail = engine.update(
        leading_space_result("ten corrected very very tail"),
        audio_duration_seconds=2.4,
        audio_offset_seconds=FIRST_UNCONFIRMED_TIMESTAMP,
    )

    assert first_tail == live_update(
        f"{CONFIRMED_PREFIX} ten wrong tail",
        stable=len(CONFIRMED_PREFIX),
        mutable=len(CONFIRMED_PREFIX),
    )
    assert corrected_tail == live_update(
        f"{CONFIRMED_PREFIX} ten corrected very very tail",
        stable=len(CONFIRMED_PREFIX),
        mutable=len(CONFIRMED_PREFIX),
    )
    assert corrected_tail.text[: len(CONFIRMED_PREFIX)] == CONFIRMED_PREFIX


def test_normal_confirmation_seam_strips_one_guarded_case_variant_echo():
    engine = ParakeetPreviewAgreement()
    run_passes(engine, agreement_result())

    update = engine.update(
        leading_space_result(
            "NINE. ten eleven. twelve",
            (0.0, 0.05, 0.4, 0.8),
        ),
        audio_duration_seconds=1.2,
        audio_offset_seconds=FIRST_UNCONFIRMED_TIMESTAMP,
    )

    assert update == live_update(
        f"{CONFIRMED_PREFIX} ten eleven. twelve",
        stable=len(CONFIRMED_PREFIX),
        mutable=len(CONFIRMED_PREFIX),
    )
    assert "nine. NINE." not in update.text
    assert engine.freeze().text == update.text


def test_normal_confirmation_seam_strips_one_unique_two_group_echo():
    engine = ParakeetPreviewAgreement()
    run_passes(engine, agreement_result())

    update = engine.update(
        leading_space_result(
            "eight nine. ten eleven. twelve",
            (0.0, 0.05, 0.1, 0.5, 0.9),
        ),
        audio_duration_seconds=1.3,
        audio_offset_seconds=FIRST_UNCONFIRMED_TIMESTAMP,
    )

    assert update.text == f"{CONFIRMED_PREFIX} ten eleven. twelve"


def test_normal_confirmation_seam_preserves_legitimate_immediate_repeat():
    repeated_text = (
        "one two three four five. six seven. eight go. "
        "go next. more words. tail"
    )
    repeated_timestamps = tuple(
        0.25 * index for index in range(len(repeated_text.split()))
    )
    engine = ParakeetPreviewAgreement()
    confirmed = run_passes(
        engine,
        leading_space_result(repeated_text, repeated_timestamps),
        audio_duration_seconds=5.0,
    )
    stable_text = confirmed.text[: confirmed.stable_char_count]
    assert stable_text.endswith("go.")
    assert confirmed.text[confirmed.mutable_start_char_count :].lstrip().startswith(
        "go next."
    )

    seek = confirmed.trim_before_seconds
    assert seek is not None
    update = engine.update(
        leading_space_result("go next. more words.", (0.0, 0.25, 0.5, 0.75)),
        audio_duration_seconds=1.0,
        audio_offset_seconds=seek,
    )

    assert update.text.startswith(f"{stable_text} go next.")
    assert "go. go next." in update.text


def test_normal_confirmation_seam_fails_closed_on_changed_guard():
    engine = ParakeetPreviewAgreement()
    run_passes(engine, agreement_result())

    update = engine.update(
        leading_space_result(
            "nine. changed guard words",
            (0.0, 0.05, 0.4, 0.8),
        ),
        audio_duration_seconds=1.2,
        audio_offset_seconds=FIRST_UNCONFIRMED_TIMESTAMP,
    )

    assert update.text == f"{CONFIRMED_PREFIX} nine. changed guard words"


def test_normal_confirmation_seam_fails_closed_away_from_seek_frontier():
    engine = ParakeetPreviewAgreement()
    run_passes(engine, agreement_result())

    update = engine.update(
        leading_space_result(
            "nine. ten eleven. twelve",
            (0.6, 0.65, 0.9, 1.1),
        ),
        audio_duration_seconds=1.3,
        audio_offset_seconds=FIRST_UNCONFIRMED_TIMESTAMP,
    )

    assert update.text == f"{CONFIRMED_PREFIX} nine. ten eleven. twelve"


def test_confirmation_requires_three_consecutive_matching_timed_passes():
    engine = ParakeetPreviewAgreement()
    result = agreement_result()
    shifted = agreement_result()
    shifted_timestamps = list(shifted.timestamps or ())
    shifted_timestamps[0] += 0.161
    shifted = detailed(
        shifted.text,
        shifted.tokens,
        tuple(shifted_timestamps),
    )

    updates = [
        engine.update(result, audio_duration_seconds=7.0),
        engine.update(result, audio_duration_seconds=7.0),
        engine.update(shifted, audio_duration_seconds=7.0),
        engine.update(result, audio_duration_seconds=7.0),
        engine.update(result, audio_duration_seconds=7.0),
    ]

    assert all(update.stable_char_count == 0 for update in updates)
    confirmed = engine.update(result, audio_duration_seconds=7.0)
    assert confirmed.stable_char_count == len(CONFIRMED_PREFIX)


def test_punctuation_confirmation_accepts_five_words_at_exactly_one_second_age():
    result = leading_space_result(
        "one two three. four. five. six. seven. tail",
        tuple(0.1 * index for index in range(8)),
    )

    exact = run_passes(
        ParakeetPreviewAgreement(),
        result,
        audio_duration_seconds=1.5,
    )
    too_young = run_passes(
        ParakeetPreviewAgreement(),
        result,
        audio_duration_seconds=1.499,
    )

    assert exact == live_update(
        "one two three. four. five. six. seven. tail",
        stable=len("one two three. four. five."),
        mutable=len("one two three. four. five."),
        trim=pytest.approx(0.5),
    )
    assert too_young == live_update(
        "one two three. four. five. six. seven. tail",
    )


def test_punctuation_confirmation_rejects_fewer_than_five_confirmed_words():
    result = leading_space_result(
        "one two. three. four. five. six. tail",
        tuple(0.1 * index for index in range(7)),
    )

    update = run_passes(
        ParakeetPreviewAgreement(),
        result,
        audio_duration_seconds=2.0,
    )

    assert update == live_update(
        "one two. three. four. five. six. tail",
    )


def test_buffer_pressure_confirms_agreed_words_before_ring_overtake():
    text = " ".join(f"word{index}" for index in range(40))
    result = leading_space_result(
        text,
        tuple(0.4 * index for index in range(40)),
    )

    update = run_passes(
        ParakeetPreviewAgreement(),
        result,
        audio_duration_seconds=19.0,
    )

    assert update.stable_char_count > 0
    assert update.trim_before_seconds is not None
    assert update.text.startswith("word0 word1 word2")
    assert update.text.endswith("word39")


def test_repeated_different_tail_becomes_newest_authoritative_text():
    engine = ParakeetPreviewAgreement()
    seed = leading_space_result(
        "seed one two three four five",
        (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
    )
    ambiguous = leading_space_result(
        "alpha beta gamma delta epsilon zeta",
        (0.1, 0.3, 0.5, 0.7, 0.9, 1.1),
    )
    engine.update(
        seed,
        audio_duration_seconds=1.2,
    )

    for _ in range(3):
        update = engine.update(
            ambiguous,
            audio_duration_seconds=2.0,
        )
        assert update.text == "alpha beta gamma delta epsilon zeta"

    assert engine._confirmed_groups == []
    recovered = engine.update(
        leading_space_result(
            "seed one two three four five six seven",
            (0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4),
        ),
        audio_duration_seconds=1.6,
    )
    assert recovered.text == "seed one two three four five six seven"
    assert engine.freeze().text == recovered.text


def test_high_cardinality_valid_metadata_publishes_without_recursion():
    engine = ParakeetPreviewAgreement()
    words = tuple(f"word{index}" for index in range(300))
    result = leading_space_result(
        " ".join(words),
        tuple(0.05 * index for index in range(len(words))),
    )

    first = engine.update(result, audio_duration_seconds=19.0)
    second = engine.update(result, audio_duration_seconds=19.0)

    expected = " ".join(words)
    assert first.text == expected
    assert second == first
    assert engine.freeze().text == expected


def test_ring_fallback_preserves_latest_words_before_their_audio_disappears():
    engine = ParakeetPreviewAgreement()
    engine.update(
        leading_space_result(
            "alpha beta gamma delta epsilon zeta",
            (0.1, 0.3, 0.5, 0.7, 0.9, 1.1),
        ),
        audio_duration_seconds=2.0,
    )

    engine.preserve_before_unavailable_audio(0.9)
    update = engine.update(
        detailed(
            "delta, epsilon zeta eta theta",
            (" delta", ",", " epsilon", " zeta", " eta", " theta"),
            (0.0, 0.05, 0.1, 0.3, 0.5, 0.7),
        ),
        audio_duration_seconds=2.0,
        audio_offset_seconds=0.9,
    )

    assert update.text == "alpha beta gamma delta epsilon zeta eta theta"
    assert update.stable_char_count == len("alpha beta gamma delta")


def test_ring_fallback_preserves_legitimate_immediate_repeat():
    engine = ParakeetPreviewAgreement()
    engine.update(
        leading_space_result(
            "alpha beta make make sure now",
            (0.1, 0.3, 0.62, 0.9, 1.1, 1.3),
        ),
        audio_duration_seconds=1.5,
    )

    engine.preserve_before_unavailable_audio(0.75)
    update = engine.update(
        leading_space_result(
            "make sure now",
            (0.15, 0.35, 0.55),
        ),
        audio_duration_seconds=0.8,
        audio_offset_seconds=0.75,
    )

    assert update.text == "alpha beta make make sure now"
    assert update.text.split().count("make") == 2
    assert engine.freeze().text == update.text


def test_ring_fallback_fails_closed_when_the_expected_guard_changes():
    engine = ParakeetPreviewAgreement()
    engine.update(
        leading_space_result(
            "alpha beta gamma delta epsilon zeta",
            (0.1, 0.3, 0.5, 0.7, 0.9, 1.1),
        ),
        audio_duration_seconds=2.0,
    )

    engine.preserve_before_unavailable_audio(0.9)
    update = engine.update(
        leading_space_result(
            "delta changed guard",
            (0.0, 0.1, 0.3),
        ),
        audio_duration_seconds=0.8,
        audio_offset_seconds=0.9,
    )

    assert update.text == "alpha beta gamma delta delta changed guard"


def test_ring_fallback_without_remaining_group_has_no_seam_contract():
    engine = ParakeetPreviewAgreement()
    engine.update(
        leading_space_result(
            "alpha beta make",
            (0.1, 0.3, 0.62),
        ),
        audio_duration_seconds=0.8,
    )

    engine.preserve_before_unavailable_audio(0.75)

    assert engine._seam_contract is None
    assert engine.freeze().text == "alpha beta make"


def test_invalid_metadata_then_ring_overtake_keeps_cross_boundary_word():
    engine = ParakeetPreviewAgreement()
    engine.update(
        leading_space_result(
            "alpha beta gamma delta epsilon zeta",
            (0.1, 0.3, 0.5, 0.7, 0.9, 1.1),
        ),
        audio_duration_seconds=1.3,
    )
    engine.update(
        detailed("broken", (" other",), (0.1,)),
        audio_duration_seconds=1.3,
    )

    engine.preserve_before_unavailable_audio(0.85)
    update = engine.update(
        leading_space_result(
            "epsilon zeta eta theta",
            (0.05, 0.25, 0.45, 0.65),
        ),
        audio_duration_seconds=0.8,
        audio_offset_seconds=0.85,
    )

    assert update.text == (
        "alpha beta gamma delta epsilon zeta eta theta"
    )
    assert engine.freeze().text == update.text


def test_trim_offsets_are_absolute_when_decoding_an_offset_tail():
    engine = ParakeetPreviewAgreement()

    update = run_passes(
        engine,
        agreement_result(),
        audio_duration_seconds=7.0,
        audio_offset_seconds=12.5,
    )

    assert update.trim_before_seconds == pytest.approx(
        12.5 + FIRST_UNCONFIRMED_TIMESTAMP
    )
    assert update.text == AGREEMENT_TEXT


@pytest.mark.parametrize(
    "result",
    [
        DetailedTranscriptionResult(text="unsafe tail"),
        detailed("unsafe tail", (" unsafe", " tail"), (0.1,)),
        detailed("unsafe tail", (" unsafe", " tail"), (0.1, float("nan"))),
        detailed("unsafe tail", (" unsafe", " tail"), (0.1, -0.2)),
        detailed("unsafe tail", (" safe", " tail"), (0.1, 0.5)),
        detailed(
            "unsafe tail",
            (" unsafe", " tail"),
            (0.1, 0.5),
            durations=(0.2,),
        ),
    ],
)
def test_malformed_metadata_fails_closed_without_replacing_confirmed_prefix(result):
    engine = ParakeetPreviewAgreement()
    latest_valid = run_passes(engine, agreement_result())

    update = engine.update(result, audio_duration_seconds=7.0)

    assert update == replace(
        latest_valid,
        trim_before_seconds=None,
        reset=True,
    )
    assert engine.freeze() == frozen_update(AGREEMENT_TEXT)


def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "condition did not become true before timeout"


class _MutablePreviewSource:
    def __init__(self, snapshot: PreviewAudioSnapshot) -> None:
        self._lock = threading.Lock()
        self._snapshot = snapshot

    def set(self, snapshot: PreviewAudioSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def __call__(self) -> PreviewAudioSnapshot:
        with self._lock:
            return self._snapshot


def _snapshot(
    size: int,
    *,
    total: int | None = None,
    start: int = 0,
) -> PreviewAudioSnapshot:
    return PreviewAudioSnapshot(
        audio=np.arange(start, start + size, dtype=np.float32),
        total_samples=(start + size) if total is None else total,
        start_sample=start,
    )


def _coordinator(
    *,
    recording_id: int,
    manager: object,
    source: _MutablePreviewSource,
    published: list[tuple[int, int, PreviewUpdate]],
    max_audio_seconds: float = 20.0,
) -> LivePreviewCoordinator:
    return LivePreviewCoordinator(
        recording_id=recording_id,
        stt_manager=manager,
        snapshot_provider=source,
        publish=lambda *args: published.append(args),
        sample_rate=10,
        min_audio_seconds=1.5,
        cadence_seconds=0.0,
        max_audio_seconds=max_audio_seconds,
    )


def _stop(coordinator: LivePreviewCoordinator) -> None:
    coordinator.invalidate()
    assert coordinator.wait_stopped(timeout=1.0)


def test_coordinator_bootstraps_at_1_5_seconds_with_one_second_silence_padding():
    source = _MutablePreviewSource(_snapshot(14))
    calls: list[np.ndarray] = []
    published: list[tuple[int, int, PreviewUpdate]] = []

    class Manager:
        def try_transcribe_preview(self, audio, *, sample_rate):
            assert sample_rate == 10
            calls.append(audio.copy())
            return leading_space_result(
                "hello world again",
                (0.1, 0.5, 0.9),
            )

    coordinator = _coordinator(
        recording_id=7,
        manager=Manager(),
        source=source,
        published=published,
    )
    coordinator.start()
    try:
        coordinator.notify_audio()
        time.sleep(0.03)
        assert calls == []

        source.set(_snapshot(15))
        coordinator.notify_audio()
        _wait_until(lambda: len(published) == 1)
    finally:
        _stop(coordinator)

    np.testing.assert_array_equal(calls[0][:15], np.arange(15, dtype=np.float32))
    np.testing.assert_array_equal(calls[0][15:], np.zeros(10, dtype=np.float32))
    assert published == [
        (
            7,
            1,
            PreviewUpdate(text="hello world again", stable_char_count=0),
        )
    ]


def test_coordinator_is_single_flight_coalesces_audio_and_retries_after_busy():
    source = _MutablePreviewSource(_snapshot(15))
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[np.ndarray] = []
    active = 0
    max_active = 0
    published: list[tuple[int, int, PreviewUpdate]] = []

    class Manager:
        def try_transcribe_preview(self, audio, *, sample_rate):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            calls.append(audio.copy())
            call_number = len(calls)
            if call_number == 1:
                first_started.set()
                assert release_first.wait(timeout=1.0)
                result = leading_space_result("first complete hypothesis")
            elif call_number == 2:
                result = None
            else:
                result = leading_space_result("newest complete hypothesis")
            active -= 1
            return result

    coordinator = _coordinator(
        recording_id=9,
        manager=Manager(),
        source=source,
        published=published,
    )
    coordinator.start()
    try:
        coordinator.notify_audio()
        assert first_started.wait(timeout=1.0)

        source.set(_snapshot(20))
        coordinator.notify_audio()
        source.set(_snapshot(30))
        coordinator.notify_audio()
        time.sleep(0.03)
        assert len(calls) == 1

        release_first.set()
        _wait_until(lambda: len(calls) == 2)
        assert len(calls[1]) == 40

        source.set(_snapshot(31))
        coordinator.notify_audio()
        _wait_until(lambda: len(calls) == 3)
        _wait_until(
            lambda: published[-1][2].text == "newest complete hypothesis"
        )
    finally:
        release_first.set()
        _stop(coordinator)

    assert max_active == 1
    assert [len(call) for call in calls] == [25, 40, 41]
    assert [update.text for _, _, update in published] == [
        "first complete hypothesis",
        "newest complete hypothesis",
    ]


def test_coordinator_advances_future_decode_to_absolute_confirmed_seek_point():
    source = _MutablePreviewSource(_snapshot(80))
    calls: list[np.ndarray] = []
    published: list[tuple[int, int, PreviewUpdate]] = []

    class Manager:
        def try_transcribe_preview(self, audio, *, sample_rate):
            calls.append(audio.copy())
            if len(calls) <= 3:
                return agreement_result()
            return leading_space_result(
                "ten eleven. twelve thirteen. fourteen fifteen. tail"
            )

    coordinator = _coordinator(
        recording_id=11,
        manager=Manager(),
        source=source,
        published=published,
    )
    coordinator.start()
    try:
        for total in (80, 81, 82):
            source.set(_snapshot(total))
            coordinator.notify_audio()
            _wait_until(lambda total=total: len(calls) >= total - 79)
        _wait_until(
            lambda: any(
                update.trim_before_seconds is not None
                for _, _, update in published
            )
        )

        source.set(_snapshot(83))
        coordinator.notify_audio()
        _wait_until(lambda: len(calls) == 4)
    finally:
        _stop(coordinator)

    seek_sample = round(FIRST_UNCONFIRMED_TIMESTAMP * 10)
    np.testing.assert_array_equal(
        calls[3][:-10],
        np.arange(seek_sample, 83, dtype=np.float32),
    )
    np.testing.assert_array_equal(calls[3][-10:], np.zeros(10, dtype=np.float32))


def test_coordinator_freeze_publishes_full_hypothesis_without_another_decode():
    source = _MutablePreviewSource(_snapshot(15))
    calls = 0
    published: list[tuple[int, int, PreviewUpdate]] = []

    class Manager:
        def try_transcribe_preview(self, audio, *, sample_rate):
            nonlocal calls
            calls += 1
            return leading_space_result("hello world again")

    coordinator = _coordinator(
        recording_id=13,
        manager=Manager(),
        source=source,
        published=published,
    )
    coordinator.start()
    try:
        coordinator.notify_audio()
        _wait_until(lambda: len(published) == 1)
        coordinator.freeze()
        _wait_until(lambda: len(published) == 2)
    finally:
        _stop(coordinator)

    assert calls == 1
    assert published == [
        (
            13,
            1,
            live_update("hello world again"),
        ),
        (
            13,
            2,
            frozen_update("hello world again"),
        ),
    ]


def test_coordinator_invalidation_is_nonblocking_and_drops_late_result():
    source = _MutablePreviewSource(_snapshot(15))
    decode_started = threading.Event()
    release_decode = threading.Event()
    published: list[tuple[int, int, PreviewUpdate]] = []

    class Manager:
        def try_transcribe_preview(self, audio, *, sample_rate):
            decode_started.set()
            assert release_decode.wait(timeout=1.0)
            return leading_space_result("too late to publish")

    coordinator = _coordinator(
        recording_id=15,
        manager=Manager(),
        source=source,
        published=published,
    )
    coordinator.start()
    coordinator.notify_audio()
    assert decode_started.wait(timeout=1.0)

    started = time.perf_counter()
    coordinator.invalidate()
    assert time.perf_counter() - started < 0.05
    assert not coordinator.wait_stopped(timeout=0.02)

    release_decode.set()
    assert coordinator.wait_stopped(timeout=1.0)
    assert published == []


def test_ring_overtake_restarts_tail_agreement_without_losing_confirmed_prefix():
    source = _MutablePreviewSource(_snapshot(80))
    calls: list[np.ndarray] = []
    published: list[tuple[int, int, PreviewUpdate]] = []

    class Manager:
        def try_transcribe_preview(self, audio, *, sample_rate):
            calls.append(audio.copy())
            if len(calls) <= 3:
                return agreement_result()
            return leading_space_result("fresh words after ring overtake")

    coordinator = _coordinator(
        recording_id=17,
        manager=Manager(),
        source=source,
        published=published,
        max_audio_seconds=10.0,
    )
    coordinator.start()
    try:
        for total in (80, 81, 82):
            source.set(_snapshot(total))
            coordinator.notify_audio()
            _wait_until(lambda total=total: len(calls) >= total - 79)
        _wait_until(
            lambda: any(
                update.trim_before_seconds is not None
                for _, _, update in published
            )
        )

        source.set(_snapshot(30, total=100, start=70))
        coordinator.notify_audio()
        _wait_until(
            lambda: published[-1][2].text
            == f"{AGREEMENT_TEXT} fresh words after ring overtake"
        )
    finally:
        _stop(coordinator)

    np.testing.assert_array_equal(
        calls[3][:-10],
        np.arange(70, 100, dtype=np.float32),
    )
    assert published[-1][2] == live_update(
        f"{AGREEMENT_TEXT} fresh words after ring overtake",
        stable=len(AGREEMENT_TEXT),
        mutable=len(AGREEMENT_TEXT),
    )


def test_pressure_confirmation_seam_uses_punctuation_audio_frontier():
    engine = ParakeetPreviewAgreement()
    # A standalone comma remains at the audio frontier after the last word is
    # confirmed. Its seek must match the actual trim, not the next word's onset.
    raw = detailed(
        "one two three four five yeah, wait how next",
        (" one", " two", " three", " four", " five", " yeah", ",", " wait", " how", " next"),
        (1.0, 2.0, 3.0, 4.0, 5.0, 14.0, 14.5, 14.74, 15.1, 15.5),
    )
    confirmed = run_passes(engine, raw, audio_duration_seconds=19.5)
    assert confirmed.trim_before_seconds == 14.5
    stable = confirmed.text[:confirmed.stable_char_count]
    assert stable == "one two three four five yeah"

    update = engine.update(
        leading_space_result("yeah wait how next", (0.0, 0.24, 0.6, 1.0)),
        audio_duration_seconds=2.0,
        audio_offset_seconds=confirmed.trim_before_seconds,
    )
    assert update.text == f"{stable} wait how next"
    assert update.text[:update.stable_char_count] == stable
    assert engine.freeze().text == update.text


def _text_coordinator(
    *,
    recording_id: int,
    manager: object,
    source: _MutablePreviewSource,
    published: list[tuple[int, int, PreviewUpdate]],
) -> LivePreviewCoordinator:
    return LivePreviewCoordinator(
        recording_id=recording_id,
        stt_manager=manager,
        snapshot_provider=source,
        publish=lambda *args: published.append(args),
        sample_rate=10,
        min_audio_seconds=1.5,
        cadence_seconds=0.0,
        text_only=True,
    )


def test_text_only_coordinator_publishes_results_without_timestamps():
    # Qwen and Cohere return text only; timed agreement rejected every pass.
    source = _MutablePreviewSource(_snapshot(15))
    published: list[tuple[int, int, PreviewUpdate]] = []

    class Manager:
        def try_transcribe_preview(self, audio, *, sample_rate):
            return DetailedTranscriptionResult(text=" Hello world. ")

    coordinator = _text_coordinator(
        recording_id=21, manager=Manager(), source=source, published=published
    )
    coordinator.start()
    try:
        coordinator.notify_audio()
        _wait_until(lambda: len(published) == 1)
        coordinator.freeze()
        _wait_until(lambda: len(published) == 2)
    finally:
        _stop(coordinator)

    assert published == [
        (21, 1, PreviewUpdate(text="Hello world.", stable_char_count=0)),
        (21, 2, frozen_update("Hello world.")),
    ]


def test_text_only_coordinator_commits_long_segment_at_its_quietest_frame():
    # 9 s of loud audio at 10 Hz with one silent frame at 7.0 s.
    audio = np.ones(90, dtype=np.float32)
    audio[70] = 0.0
    source = _MutablePreviewSource(
        PreviewAudioSnapshot(audio=audio, total_samples=90, start_sample=0)
    )
    calls: list[int] = []
    published: list[tuple[int, int, PreviewUpdate]] = []
    texts = iter(["First part.", "second part"])

    class Manager:
        def try_transcribe_preview(self, decode_audio, *, sample_rate):
            calls.append(decode_audio.size - 10)  # minus trailing silence
            return DetailedTranscriptionResult(text=next(texts))

    coordinator = _text_coordinator(
        recording_id=22, manager=Manager(), source=source, published=published
    )
    coordinator.start()
    try:
        coordinator.notify_audio()
        _wait_until(lambda: len(published) == 2)
    finally:
        _stop(coordinator)

    # The commit pass decodes up to the pause; the next decodes the remainder
    # without waiting for new audio.
    assert calls == [70, 20]
    assert published[0][2] == PreviewUpdate(
        text="First part.", stable_char_count=11, mutable_start_char_count=11
    )
    assert published[1][2] == PreviewUpdate(
        text="First part. second part",
        stable_char_count=11,
        mutable_start_char_count=11,
    )


def test_text_agreement_tail_revises_while_committed_text_stays():
    agreement = TextPreviewAgreement()
    agreement.commit_segment("One two.")
    assert agreement.update_tail("three").text == "One two. three"
    update = agreement.update_tail("three four")
    assert update.text == "One two. three four"
    assert update.stable_char_count == len("One two.")
    agreement.preserve_before_unavailable_audio(5.0)
    assert agreement.update_tail("").text == "One two. three four"


def test_text_agreement_joins_cjk_segments_without_spaces():
    agreement = TextPreviewAgreement()
    agreement.commit_segment("你好。")
    assert agreement.update_tail("世界").text == "你好。世界"


def test_quiet_segment_boundary_stays_inside_the_search_window():
    audio = np.ones(100, dtype=np.float32)
    audio[10] = 0.0  # Quiet, but outside the last 3 s.
    boundary = quiet_segment_boundary(audio, 10)
    assert 70 <= boundary <= 97


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [("text", True), ("timed_tokens", False), (None, False)],
)
def test_app_selects_text_preview_from_declared_capability(
    monkeypatch, metadata, expected
):
    import whisperstream.main as main_module
    from whisperstream.stt_contracts import (
        SttCapabilities,
        SttPreviewMetadata,
        SttPreviewSupport,
    )

    captured: dict[str, object] = {}

    class FakeCoordinator:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            pass

    monkeypatch.setattr(main_module, "LivePreviewCoordinator", FakeCoordinator)
    app = main_module.WhisperstreamApp.__new__(main_module.WhisperstreamApp)
    app._recording_acceptance_grant = None
    app._require_acceptance = lambda *_args, **_kwargs: True
    app._live_preview_lock = threading.Lock()
    app._live_preview_coordinator = None

    class Capture:
        sample_rate = 16_000

        def preview_snapshot(self):
            raise AssertionError("not called")

    app.audio_capture = Capture()

    class Manager:
        capabilities = SttCapabilities(
            preview=None
            if metadata is None
            else SttPreviewSupport(
                metadata=SttPreviewMetadata(metadata),
                nonblocking_admission=True,
            )
        )

    app._start_live_preview(1, Manager())
    assert captured["text_only"] is expected
