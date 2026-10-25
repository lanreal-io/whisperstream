"""Reviewed synthetic regressions; no recordings or native input are used."""
import logging
from unittest.mock import MagicMock

import pytest

from whisperstream.live_preview import ParakeetPreviewAgreement
from whisperstream.onnx_stt_manager import DetailedTranscriptionResult
from whisperstream.text_injector import TextInjector
from whisperstream.windows_delivery_adapter import WindowsDeliveryAdapter


def test_punctuation_frontier_does_not_repeat_confirmed_word():
    engine = ParakeetPreviewAgreement()
    raw = DetailedTranscriptionResult(
        text="one two three four five yeah, wait how next",
        tokens=(" one", " two", " three", " four", " five", " yeah", ",", " wait", " how", " next"),
        timestamps=(1.0, 2.0, 3.0, 4.0, 5.0, 14.0, 14.5, 14.74, 15.1, 15.5),
    )
    for _ in range(3):
        confirmed = engine.update(raw, audio_duration_seconds=19.5)
    assert confirmed.trim_before_seconds == 14.5
    stable = confirmed.text[:confirmed.stable_char_count]
    assert stable == "one two three four five yeah"
    update = engine.update(
        DetailedTranscriptionResult(
            text="yeah wait how next",
            tokens=(" yeah", " wait", " how", " next"),
            timestamps=(0.0, 0.24, 0.6, 1.0),
        ),
        audio_duration_seconds=2.0,
        audio_offset_seconds=confirmed.trim_before_seconds,
    )
    assert update.text == f"{stable} wait how next"
    assert engine.freeze().text == update.text


def test_distinct_spoken_repetition_remains():
    update = ParakeetPreviewAgreement().update(
        DetailedTranscriptionResult(
            text="very very good good",
            tokens=(" very", " very", " good", " good"),
            timestamps=(0.1, 0.5, 0.9, 1.3),
        ), audio_duration_seconds=2.0,
    )
    assert update.text == "very very good good"


@pytest.fixture
def injector(monkeypatch):
    keyboard = MagicMock()
    keyboard.snapshot_held_modifiers.return_value = []
    clipboard = MagicMock()
    clipboard.paste.return_value = "synthetic-prior"
    timers = []

    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback = delay, callback
            self.started = False
            timers.append(self)
        def start(self):
            self.started = True

    delivery = WindowsDeliveryAdapter(
        keyboard_manager=keyboard, logger=logging.getLogger("synthetic"),
        clipboard=clipboard, timer_factory=Timer, sleep=lambda _: None,
        clipboard_behavior=True, clipboard_clear_delay=0.75,
    )
    value = TextInjector(
        keyboard_manager=keyboard, logger=logging.getLogger("synthetic"),
        word_filters=[], word_replacements={}, post_processing="",
        clipboard_behavior=True, clipboard_clear_delay=0.75,
        delivery_adapter=delivery,
    )
    return value, clipboard, timers


def test_live_preference_preserves_existing_timer_and_remembered_text(injector):
    value, clipboard, timers = injector
    value.inject("synthetic-first")
    old = timers[0]
    value.remember_original("synthetic-original")
    original = value.last_original
    value.update_clipboard_settings(behavior=False, clear_delay=0.2)
    assert old.delay == 0.75 and old.started
    old.callback()
    assert clipboard.copy.call_args.args == ("synthetic-prior",)
    assert value.last_transcript == "synthetic-first"
    assert value.last_original == original
    value.inject("synthetic-second")
    assert len(timers) == 1
    value.update_clipboard_settings(behavior=True, clear_delay=0.2)
    value.inject("synthetic-third")
    assert timers[-1].delay == 0.2


@pytest.mark.parametrize("abort", [False, True])
def test_live_disable_restores_admitted_chunk_snapshot_on_finish_or_abort(injector, abort):
    value, clipboard, timers = injector
    value._last_transcript = "synthetic-completed"
    value.paste_chunk("synthetic-first", first=True, last=False)
    assert timers == []
    value.update_clipboard_settings(behavior=False, clear_delay=0.2)
    if abort:
        value.abort_chunked_paste()
        assert value.last_transcript == "synthetic-completed"
    else:
        value.paste_chunk("synthetic-second", first=False, last=True)
        assert value.last_transcript == "synthetic-first synthetic-second"
    assert len(timers) == 1 and timers[0].delay == 0.2
    timers[0].callback()
    assert clipboard.copy.call_args.args == ("synthetic-prior",)
    value.abort_chunked_paste()
    assert len(timers) == 1
