"""Tests for the agent-speech SpeechController + PTT barge-in (Phase 2, Task 2.1).

TDD: written before the implementation. Three groups:

1. Barge-in in ``main.py``: ``_start_recording`` must call
   ``speech_controller.stop()`` before any capture setup, and be a no-op when the controller is None (the never-raise
   contract, still reachable via early-startup / test stubs).
2. ``main.py`` wiring: Agent Speak ships from 1.3.0, so every app constructs a
   ``SpeechController``. The gate moved to config, so what is asserted now is
   that with the shipped default (``agent_speech_enabled=False``) the live
   controller refuses to speak.
3. ``SpeechController`` behavior: mode/voice/speed/volume resolution, the
   never-silent summary path, newest-wins across the summarize-latency window
   (a stop or a newer response during the ``summarize`` await discards the
   stale result), never-raise public surface, and the static sample string.

No audio hardware and no real voice model are touched: the controller's engine
``is_installed`` and its ``PlaybackOwner.speak``/``stop`` are stubbed per
instance. Async controller methods are driven with ``asyncio.run`` (the repo's
convention -- see ``test_speech_summarizer.py``; no ``pytest.mark.asyncio``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from tests._platform_test_support import StopAfterBargeIn, _bare_app


pytestmark = pytest.mark.usefixtures("accepted_eula")


# ===========================================================================
# Group 1 -- PTT barge-in stop in _start_recording (main.py)
# ===========================================================================


def test_start_recording_calls_stop_before_capture_setup():
    """Barge-in stop() runs first, before any capture setup."""
    order: list[str] = []
    controller = MagicMock()
    controller.stop.side_effect = lambda: order.append("stop")

    app = _bare_app(controller=controller, order=order)
    with pytest.raises(StopAfterBargeIn):
        app._start_recording()

    controller.stop.assert_called_once()
    assert order == ["stop", "capture"], (
        f"barge-in stop() must precede capture setup, got: {order}"
    )


def test_start_recording_barge_in_noop_when_controller_none():
    """Gate off -> speech_controller is None -> barge-in is a no-op, never raises."""
    app = _bare_app(controller=None)
    with pytest.raises(StopAfterBargeIn):
        app._start_recording()
    app._invalidate_live_preview.assert_called_once()
    app.license_manager.can_start_recording.assert_not_called()


# ===========================================================================
# Group 2 -- main.py construction / config-off inertness
# ===========================================================================


@contextlib.contextmanager
def _construct_app(monkeypatch, tmp_path):
    """Construct a headless WhisperstreamApp with all hardware/model/platform
    touchpoints mocked.

    Agent Speak ships from 1.3.0, so there is no build/env gate left to toggle
    here: the controller is constructed unconditionally and the config decides
    whether it is allowed to make sound.
    """
    monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)

    from whisperstream.config_manager import WhisperstreamConfig

    with (
        patch("whisperstream.main.make_stt_manager"),
        patch("whisperstream.main.KeyboardShortcutManager"),
        patch("whisperstream.main.AudioCapture"),
        patch("whisperstream.main.AudioFeedback"),
        patch("whisperstream.main.LicenseManager"),
        patch("whisperstream.main.TextInjector"),
        patch("whisperstream.main.ConfigManager.model_dir", return_value=tmp_path),
        patch(
            "whisperstream.main.ConfigManager.load",
            return_value=WhisperstreamConfig(),
        ),
        patch("whisperstream.main.create_application_context_adapter"),
    ):
        from whisperstream.main import WhisperstreamApp

        app = WhisperstreamApp(headless=True)
        yield app


def test_app_always_constructs_speech_controller(monkeypatch, tmp_path):
    """Agent Speak ships from 1.3.0, so every launch builds the controller."""
    with _construct_app(monkeypatch, tmp_path) as app:
        from whisperstream.speech.controller import SpeechController

        assert app.speech_controller is not None
        assert isinstance(app.speech_controller, SpeechController)


def test_default_config_controller_exists_but_refuses_to_speak(monkeypatch, tmp_path):
    """Inverted from the pre-1.3.0 gate-off test.

    Agent Speak ships now, so the controller IS constructed on every launch and
    ``whisperstream.speech`` IS imported. What must still hold is the thing the
    user feels: with the shipped default (``agent_speech_enabled=False``) the
    controller refuses every speech request, so no audio is ever produced. The
    config toggle replaced the build gate as the only thing standing between a
    default install and a talking app, so it is asserted against the real
    controller and a real default config rather than a mock.
    """
    with _construct_app(monkeypatch, tmp_path) as app:
        from whisperstream.speech.controller import SpeechController

        controller = app.speech_controller
        assert isinstance(controller, SpeechController)
        assert app.config.agent_speech_enabled is False

        # Stub playback + engine so a leaked "yes" would be loudly visible here
        # instead of trying to open a real audio device.
        spoken: list[str] = []
        controller._playback = SimpleNamespace(
            speak=lambda text, **kwargs: spoken.append(text),
            stop=lambda: None,
        )
        controller._engine = SimpleNamespace(is_installed=lambda: True)

        terminals: list[str] = []
        accepted = controller.speak_spoken_summary(
            "All done.", on_terminal=terminals.append
        )

        assert accepted is False
        assert spoken == []
        assert terminals == ["unavailable"]


# ===========================================================================
# Group 3 -- SpeechController behavior
# ===========================================================================


def _make_controller(
    *,
    mode="verbatim",
    enabled=None,
    summary_source="agent",
    installed=True,
    supervisor=None,
    voice=0,
    speed=1.0,
    volume=1.0,
    model="gemma4-e2b-instruct",
    engine="supertonic",
    kokoro_voice="af_heart",
):
    """Build a SpeechController with an observable playback and a controllable
    engine-installed flag, without loading the real voice model or opening any
    audio stream.

    Returns (controller, config, speak_calls, stop_calls, broadcasts).
    """
    from whisperstream.speech.controller import SpeechController

    config = SimpleNamespace(
        agent_speech_mode=mode,
        agent_speech_voice=voice,
        agent_speech_speed=speed,
        agent_speech_volume=volume,
        agent_speech_engine=engine,
        agent_speech_kokoro_voice=kokoro_voice,
        enhancement_local_model=model,
        # Keep legacy unit expectations immediate unless a queue test opts into
        # a gap explicitly.
        claude_reply_queue_delay_seconds=0,
    )
    if enabled is not None:
        config.agent_speech_enabled = enabled
        config.agent_speech_summary_source = summary_source
    broadcasts: list[dict] = []
    controller = SpeechController(
        config_provider=lambda: config,
        broadcast=broadcasts.append,
        logger=logging.getLogger("test.speech.controller"),
        supervisor_provider=lambda: supervisor,
    )

    # Pretend the voice model is (not) installed without touching disk.
    controller._engine.is_installed = lambda: installed  # type: ignore[method-assign]
    # Platform-neutral controller tests explicitly simulate an approved
    # Supertonic runtime even when the host is Darwin, where production keeps
    # the current Windows-only wheel quarantined.
    controller._engine_manager.mark_healthy("supertonic")

    # Observe playback instead of opening a sounddevice OutputStream.
    speak_calls: list[dict] = []
    def _record_speak(
        text,
        sid=0,
        speed=1.0,
        volume=1.0,
        completion_id=None,
        on_terminal=None,
        candidates=None,
    ):
        call = {"text": text, "sid": sid, "speed": speed, "volume": volume}
        if completion_id is not None:
            call["completion_id"] = completion_id
        if on_terminal is not None:
            call["on_terminal"] = on_terminal
        if candidates is not None:
            call["candidates"] = candidates
        speak_calls.append(call)

    controller._playback.speak = _record_speak  # type: ignore[method-assign]
    stop_calls: list[bool] = []
    controller._playback.stop = lambda: stop_calls.append(True)  # type: ignore[method-assign]

    return controller, config, speak_calls, stop_calls, broadcasts


def test_construction_is_cheap_no_engine_load():
    """Constructing the controller loads neither the sherpa engine nor a stream."""
    controller, *_ = _make_controller()
    # SupertonicEngine keeps its sherpa handle None until load(), never at construct.
    assert controller._engine._engine is None


def test_out_of_range_voice_uses_static_supertonic_default():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        mode="verbatim", voice=99
    )
    asyncio.run(controller.speak_response("hello there"))
    assert speak_calls[0]["sid"] == 1


def test_can_speak_reflects_engine_installed():
    installed_ctrl, *_ = _make_controller(installed=True)
    missing_ctrl, *_ = _make_controller(installed=False)
    assert installed_ctrl.can_speak() is True
    assert missing_ctrl.can_speak() is False


def test_missing_engine_preference_defaults_to_automatic() -> None:
    from whisperstream.speech.selection import MemorySnapshot

    controller, config, _speak, _stop, _bc = _make_controller(
        engine="auto",
        installed=True,
    )
    del config.agent_speech_engine
    controller._selection_policy._memory_probe = lambda: MemorySnapshot(8192, 4096)

    assert controller.can_speak() is True


def test_automatic_preserves_each_engine_saved_voice() -> None:
    from whisperstream.speech.selection import MemorySnapshot

    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        enabled=True,
        engine="auto",
        voice=6,
        kokoro_voice="af_bella",
    )
    kokoro = controller._engine_manager.engine("kokoro")
    kokoro.is_installed = lambda: True  # type: ignore[method-assign]
    controller._selection_policy._memory_probe = lambda: MemorySnapshot(
        32 * 1024,
        8 * 1024,
    )

    assert controller.speak_spoken_summary("Finished.") is True

    candidates = speak_calls[0]["candidates"]
    assert [(item.engine_key, item.voice_id) for item in candidates] == [
        ("kokoro", "af_bella"),
        ("supertonic", 6),
    ]


def test_explicit_kokoro_keeps_string_voice_and_never_adds_supertonic() -> None:
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        enabled=True,
        engine="kokoro",
        kokoro_voice="am_puck",
    )
    kokoro = controller._engine_manager.engine("kokoro")
    kokoro.is_installed = lambda: True  # type: ignore[method-assign]

    assert controller.speak_spoken_summary("Finished.") is True

    candidates = speak_calls[0]["candidates"]
    assert [(item.engine_key, item.voice_id) for item in candidates] == [
        ("kokoro", "am_puck")
    ]


def test_explicit_kokoro_unavailable_does_not_switch_to_installed_supertonic() -> None:
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        enabled=True,
        engine="kokoro",
        installed=True,
    )
    kokoro = controller._engine_manager.engine("kokoro")
    kokoro.is_installed = lambda: False  # type: ignore[method-assign]
    terminals: list[str] = []

    accepted = controller.speak_spoken_summary(
        "Finished.",
        on_terminal=terminals.append,
    )

    assert accepted is False
    assert speak_calls == []
    assert terminals == ["unavailable"]


def test_shutdown_delegates_with_bounded_timeout_and_never_raises() -> None:
    controller, *_ = _make_controller()
    calls: list[float] = []
    controller._playback.shutdown = lambda timeout: calls.append(timeout) or True

    assert controller.shutdown(timeout=0.25) is True
    assert len(calls) == 1
    assert 0 <= calls[0] <= 0.25

    controller._playback.shutdown = lambda _timeout: (_ for _ in ()).throw(
        RuntimeError("teardown failed")
    )
    assert controller.shutdown(timeout=0.01) is False


def test_spoken_summary_uses_direct_playback_without_local_summarizer(monkeypatch):
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        enabled=True,
        summary_source="agent",
        voice=3,
        speed=1.5,
        volume=0.5,
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("the local summarizer must not run")

    monkeypatch.setattr(
        "whisperstream.speech.controller.summarizer.speak_text_for",
        forbidden,
    )

    controller.speak_spoken_summary(
        "  Finished the settings flow.  ",
        completion_id="completion-1",
    )

    assert len(speak_calls) == 1
    assert {
        key: value
        for key, value in speak_calls[0].items()
        if key != "on_terminal"
    } == {
        "text": "Finished the settings flow.",
        "sid": 3,
        "speed": 1.5,
        "volume": 0.5,
        "completion_id": "completion-1",
    }
    assert callable(speak_calls[0]["on_terminal"])


def test_spoken_summary_is_silent_when_master_toggle_is_off():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(enabled=False)

    controller.speak_spoken_summary("Marked summary.")

    assert speak_calls == []


def test_spoken_summary_reports_unavailable_terminal_before_playback():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(enabled=False)
    reasons: list[str] = []

    scheduled = controller.speak_spoken_summary(
        "Marked summary.",
        completion_id="completion",
        on_terminal=reasons.append,
    )

    assert scheduled is False
    assert speak_calls == []
    assert reasons == ["unavailable"]


def test_spoken_summary_forwards_terminal_once_through_queue_wrapper():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)
    reasons: list[str] = []

    scheduled = controller.speak_spoken_summary(
        "Marked summary.",
        completion_id="completion",
        on_terminal=reasons.append,
    )

    assert scheduled is True
    terminal = speak_calls[0]["on_terminal"]
    terminal("completed")
    terminal("failed")
    assert reasons == ["completed"]


def _wait_until(predicate, *, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def test_normal_speech_is_fifo_and_question_options_finish_before_later_summary():
    """A later cross-coordinator utterance cannot supersede question options."""

    controller, cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)
    cfg.claude_reply_queue_delay_seconds = 0
    question = "Which release channel? 1. Stable 2. Beta"
    terminals: list[tuple[str, str]] = []

    assert controller.speak_spoken_summary(
        question,
        completion_id="question-1",
        on_terminal=lambda reason: terminals.append(("question", reason)),
    )
    assert controller.speak_spoken_summary(
        "The later response is ready.",
        completion_id="response-2",
        on_terminal=lambda reason: terminals.append(("response", reason)),
    )

    assert [call["text"] for call in speak_calls] == [question]
    speak_calls[0]["on_terminal"]("completed")
    assert _wait_until(lambda: len(speak_calls) == 2)
    assert [call["text"] for call in speak_calls] == [
        question,
        "The later response is ready.",
    ]
    assert terminals == [("question", "completed")]

    speak_calls[1]["on_terminal"]("completed")
    assert terminals == [
        ("question", "completed"),
        ("response", "completed"),
    ]


def test_normal_speech_preserves_configured_gap_between_utterances():
    controller, cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)
    cfg.claude_reply_queue_delay_seconds = 0.08

    controller.speak_spoken_summary("First.")
    controller.speak_spoken_summary("Second.")
    terminal_at = time.monotonic()
    speak_calls[0]["on_terminal"]("completed")

    time.sleep(0.03)
    assert len(speak_calls) == 1
    assert _wait_until(lambda: len(speak_calls) == 2)
    assert time.monotonic() - terminal_at >= 0.07
    speak_calls[1]["on_terminal"]("completed")


def test_gap_already_elapsed_does_not_double_delay_next_utterance():
    controller, cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)
    cfg.claude_reply_queue_delay_seconds = 0.1
    controller._last_normal_terminal_at = time.monotonic() - 0.2

    controller.speak_spoken_summary("Promoted after response quiet time.")

    assert [call["text"] for call in speak_calls] == [
        "Promoted after response quiet time."
    ]
    speak_calls[0]["on_terminal"]("completed")


def test_speech_arriving_during_dictation_waits_for_idle_plus_gap():
    controller, cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)
    cfg.claude_reply_queue_delay_seconds = 0.08
    recording = [True]
    controller._recording_provider = lambda: recording[0]

    assert controller.speak_spoken_summary("Queued behind dictation.") is True
    time.sleep(0.07)
    assert speak_calls == []

    idle_at = time.monotonic()
    recording[0] = False
    time.sleep(0.03)
    assert speak_calls == []
    assert _wait_until(lambda: len(speak_calls) == 1)
    assert time.monotonic() - idle_at >= 0.07
    speak_calls[0]["on_terminal"]("completed")


def test_stop_flushes_active_and_pending_callbacks_exactly_once():
    controller, _cfg, speak_calls, stop_calls, _bc = _make_controller(enabled=True)
    terminals: list[tuple[str, str]] = []

    controller.speak_spoken_summary(
        "First.", on_terminal=lambda reason: terminals.append(("first", reason))
    )
    controller.speak_spoken_summary(
        "Second.", on_terminal=lambda reason: terminals.append(("second", reason))
    )
    active_terminal = speak_calls[0]["on_terminal"]

    controller.stop()

    assert stop_calls == [True]
    assert terminals == [("first", "stopped"), ("second", "stopped")]
    assert len(speak_calls) == 1
    active_terminal("stopped")
    assert terminals == [("first", "stopped"), ("second", "stopped")]


def test_spoken_summary_is_silent_for_deferred_whisperstream_source():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        enabled=True,
        summary_source="whisperstream",
    )

    controller.speak_spoken_summary("Marked summary.")

    assert speak_calls == []


def test_spoken_summary_empty_text_is_silent():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)

    controller.speak_spoken_summary(" \r\n ")

    assert speak_calls == []


# ---------------------------------------------------------------------------
# name_prefix (Task 2.3): the spoken session-name prefix
# ---------------------------------------------------------------------------


def test_spoken_summary_name_prefix_hyphen_normalizes_and_terminates():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)

    controller.speak_spoken_summary(
        "Tests are passing.", name_prefix="speak-harness"
    )

    assert speak_calls[0]["text"] == "speak harness. Tests are passing."


def test_spoken_summary_name_prefix_underscore_normalizes():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)

    controller.speak_spoken_summary("Done.", name_prefix="pay_kit")

    assert speak_calls[0]["text"] == "pay kit. Done."


def test_spoken_summary_name_prefix_no_double_terminator_period():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)

    controller.speak_spoken_summary("Done.", name_prefix="speak harness.")

    assert speak_calls[0]["text"] == "speak harness. Done."


def test_spoken_summary_name_prefix_no_double_terminator_bang():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)

    controller.speak_spoken_summary("Done.", name_prefix="speak harness!")

    assert speak_calls[0]["text"] == "speak harness! Done."


def test_spoken_summary_name_prefix_none_is_byte_identical_to_today():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)

    controller.speak_spoken_summary("Tests are passing.", name_prefix=None)

    assert speak_calls[0]["text"] == "Tests are passing."


def test_spoken_summary_name_prefix_that_normalizes_empty_is_treated_as_none():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)

    controller.speak_spoken_summary("Tests are passing.", name_prefix="---")

    assert speak_calls[0]["text"] == "Tests are passing."


def test_spoken_summary_does_not_mutate_the_caller_string():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(enabled=True)

    original = "Tests are passing."
    controller.speak_spoken_summary(original, name_prefix="speak-harness")

    assert original == "Tests are passing."


def test_legacy_full_response_path_is_silent_for_normalized_config(monkeypatch):
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        mode="summary",
        enabled=True,
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("the local summarizer must not run")

    monkeypatch.setattr(
        "whisperstream.speech.controller.summarizer.speak_text_for",
        forbidden,
    )

    asyncio.run(controller.speak_response("Arbitrary full response content."))

    assert speak_calls == []


def test_speak_response_verbatim_calls_playback_with_cleaned_text():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode="verbatim")
    asyncio.run(controller.speak_response("**Hello** world."))
    assert len(speak_calls) == 1
    # prepare_verbatim strips the emphasis markers.
    assert speak_calls[0]["text"] == "Hello world."


def test_speak_response_passes_voice_speed_volume():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        mode="verbatim", voice=3, speed=1.5, volume=0.5
    )
    asyncio.run(controller.speak_response("hello there"))
    assert speak_calls[0]["sid"] == 3
    assert speak_calls[0]["speed"] == 1.5
    assert speak_calls[0]["volume"] == 0.5


def test_speak_response_passes_completion_id_to_playback():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode="verbatim")

    asyncio.run(
        controller.speak_response("hello there", completion_id="completion-1")
    )

    assert speak_calls[0]["completion_id"] == "completion-1"


def test_speak_response_off_mode_is_noop():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode="off")
    asyncio.run(controller.speak_response("something"))
    assert speak_calls == []


def test_speak_response_model_missing_is_noop():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        mode="verbatim", installed=False
    )
    asyncio.run(controller.speak_response("something"))
    assert speak_calls == []


def test_speak_response_empty_text_is_noop():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode="verbatim")
    asyncio.run(controller.speak_response("   \n  "))
    assert speak_calls == []


def test_speak_response_summary_speaks_summary(monkeypatch):
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        mode="summary", supervisor=SimpleNamespace(base_url="http://127.0.0.1:1/v1")
    )
    from whisperstream.speech import summarizer as summ

    async def fake(text, mode, supervisor, model_name):
        return "short summary"

    monkeypatch.setattr(summ, "speak_text_for", fake)
    asyncio.run(controller.speak_response("a long agent reply"))
    assert len(speak_calls) == 1
    assert speak_calls[0]["text"] == "short summary"


def test_stop_during_summary_discards_stale_result(monkeypatch):
    """Newest-wins across the summarize window: a stop() during the summarize
    await bumps the generation, so the completed summary is discarded (never
    spoken) and the stop is delegated to playback.
    """
    controller, _cfg, speak_calls, stop_calls, _bc = _make_controller(
        mode="summary", supervisor=SimpleNamespace(base_url="http://127.0.0.1:1/v1")
    )
    from whisperstream.speech import summarizer as summ

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(text, mode, supervisor, model_name):
            started.set()
            await release.wait()
            return "STALE SUMMARY"

        monkeypatch.setattr(summ, "speak_text_for", slow)
        task = asyncio.create_task(controller.speak_response("resp A"))
        await started.wait()
        controller.stop()  # bumps controller generation -> A goes stale
        release.set()
        await task

    asyncio.run(scenario())
    assert speak_calls == []  # stale summary never played
    assert stop_calls == [True]  # stop delegated to PlaybackOwner


def test_newer_response_discards_older_summary(monkeypatch):
    """A newer speak_response during an older one's summarize await wins: the
    newer summary plays; the older, now-stale summary is discarded.
    """
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        mode="summary", supervisor=SimpleNamespace(base_url="http://127.0.0.1:1/v1")
    )
    from whisperstream.speech import summarizer as summ

    async def scenario():
        gate_a = asyncio.Event()
        release_a = asyncio.Event()
        calls = {"n": 0}

        async def summ_fn(text, mode, supervisor, model_name):
            calls["n"] += 1
            if calls["n"] == 1:
                gate_a.set()
                await release_a.wait()
                return "SUMMARY A"
            return "SUMMARY B"

        monkeypatch.setattr(summ, "speak_text_for", summ_fn)
        task_a = asyncio.create_task(
            controller.speak_response("resp A", completion_id="completion-a")
        )
        await gate_a.wait()  # A parked mid-summarize
        await controller.speak_response(
            "resp B", completion_id="completion-b"
        )  # B completes + speaks
        release_a.set()  # A resumes, now stale
        await task_a

    asyncio.run(scenario())
    assert len(speak_calls) == 1
    assert speak_calls[0]["text"] == "SUMMARY B"
    assert speak_calls[0]["completion_id"] == "completion-b"


def test_speak_response_never_raises_on_summarizer_error(monkeypatch):
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        mode="summary", supervisor=SimpleNamespace(base_url="http://127.0.0.1:1/v1")
    )
    from whisperstream.speech import summarizer as summ

    async def boom(text, mode, supervisor, model_name):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(summ, "speak_text_for", boom)
    # Must not propagate.
    asyncio.run(controller.speak_response("resp"))
    assert speak_calls == []


def test_speak_sample_speaks_static_string():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller()
    from whisperstream.speech.controller import SAMPLE_TEXT

    controller.speak_sample()
    assert len(speak_calls) == 1
    assert speak_calls[0]["text"] == SAMPLE_TEXT
    assert "completion_id" not in speak_calls[0]


def test_speak_sample_noop_when_model_missing():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(installed=False)
    controller.speak_sample()
    assert speak_calls == []


def test_stop_before_any_speak_is_noop_and_delegates():
    controller, _cfg, _speak, stop_calls, _bc = _make_controller()
    controller.stop()  # must not raise
    assert stop_calls == [True]


def test_sample_string_has_no_em_dash():
    from whisperstream.speech.controller import SAMPLE_TEXT

    assert "—" not in SAMPLE_TEXT


# ===========================================================================
# Group 3b -- speak_text (Task 1.1: onboarding voice-speed custom test text)
# ===========================================================================


def test_speak_text_explicit_supertonic_voice_speaks_submitted_text():
    """An explicit engine/voice resolves the submitted voice, not the saved one."""
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        engine="supertonic", voice=0
    )
    controller.speak_text("Read this back to me.", engine="supertonic", voice=3)
    assert len(speak_calls) == 1
    assert speak_calls[0]["text"] == "Read this back to me."
    assert speak_calls[0]["sid"] == 3


def test_speak_text_explicit_kokoro_voice_speaks_submitted_text():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        engine="supertonic", kokoro_voice="af_heart"
    )
    # Kokoro has no historical single-instance alias like supertonic's
    # `controller._engine`, so make the keyed manager report it installed.
    controller._engine_manager.is_installed = lambda engine_key: True  # type: ignore[method-assign]

    controller.speak_text("Read this back to me.", engine="kokoro", voice="am_puck")

    assert len(speak_calls) == 1
    call = speak_calls[0]
    assert call["text"] == "Read this back to me."
    assert call["candidates"][0].engine_key == "kokoro"
    assert call["candidates"][0].voice_id == "am_puck"


def test_speak_text_noop_when_model_missing():
    """can_speak False (no installed engine) is a silent no-op."""
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(installed=False)
    controller.speak_text("Read this back to me.")
    assert speak_calls == []


def test_speak_text_never_raises_on_playback_error():
    controller, _cfg, _speak, _stop, _bc = _make_controller()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("playback broke")

    controller._playback.speak = _boom  # type: ignore[method-assign]
    controller.speak_text("Read this back to me.")  # must not raise


def test_speak_text_never_logs_text_content(caplog):
    """Privacy: user-typed test text must never appear in any log record.

    Covers both the success path (fire-and-forget, no log call at all) and the
    exception path (only ``reason=<type>`` metadata is logged), per the
    "never log transcript content" rule.
    """
    caplog.set_level(logging.DEBUG)
    sentinel = "SENTINEL_ONBOARDING_VOICE_SPEED_TEXT_MUST_NOT_BE_LOGGED"

    # Success path.
    controller, _cfg, speak_calls, _stop, _bc = _make_controller()
    controller.speak_text(sentinel)
    assert speak_calls[0]["text"] == sentinel  # sanity: the text really was spoken
    for record in caplog.records:
        assert sentinel not in record.getMessage()
    assert sentinel not in caplog.text

    caplog.clear()

    # Exception path: playback raises; the handler must log metadata only.
    controller2, _cfg2, _speak2, _stop2, _bc2 = _make_controller()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("playback broke")

    controller2._playback.speak = _boom  # type: ignore[method-assign]
    controller2.speak_text(sentinel)  # must not raise
    for record in caplog.records:
        assert sentinel not in record.getMessage()
    assert sentinel not in caplog.text


# ===========================================================================
# Group 4 -- speak_event (Phase 2: conversational templates)
# ===========================================================================


def _wire_speak_if_idle(controller, *, idle=True):
    """Stub playback.speak_if_idle, recording calls; returns the call list."""
    calls: list[dict] = []

    def _speak_if_idle(text, sid=0, speed=1.0, volume=1.0):
        calls.append({"text": text, "sid": sid, "speed": speed, "volume": volume})
        return idle

    controller._playback.speak_if_idle = _speak_if_idle  # type: ignore[method-assign]
    return calls


def test_needs_input_speaks_template_and_interrupts():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode="verbatim")
    gen_before = controller._generation

    controller.speak_event("needs_input", "claude")

    assert len(speak_calls) == 1
    assert speak_calls[0]["text"] == "Claude needs your approval to continue."
    assert "completion_id" not in speak_calls[0]
    # Claims newest-wins ownership (interrupts a pending summarized response).
    assert controller._generation > gen_before


def test_needs_input_speaks_in_every_non_off_mode():
    for mode in ("verbatim", "summary", "conversational"):
        controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode=mode)
        controller.speak_event("needs_input", "claude")
        assert len(speak_calls) == 1, f"mode={mode}"
        assert speak_calls[0]["text"] == "Claude needs your approval to continue."


def test_speak_event_mode_off_is_silent():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode="off")
    idle_calls = _wire_speak_if_idle(controller)
    for kind in ("needs_input", "error", "progress"):
        controller.speak_event(kind, "claude")
    assert speak_calls == []
    assert idle_calls == []


def test_progress_requires_conversational_mode():
    for mode in ("verbatim", "summary"):
        controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode=mode)
        idle_calls = _wire_speak_if_idle(controller)
        controller.speak_event("progress", "claude", "subagent_done")
        assert idle_calls == [], f"mode={mode}"
        assert speak_calls == []


def test_progress_speaks_via_speak_if_idle_in_conversational():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode="conversational")
    idle_calls = _wire_speak_if_idle(controller)
    gen_before = controller._generation

    controller.speak_event("progress", "claude", "subagent_done")

    assert len(idle_calls) == 1
    assert idle_calls[0]["text"] == "A subtask finished."
    # Never interrupts: no interrupt-path speak, no generation bump (a pending
    # summarized response must survive a progress line).
    assert speak_calls == []
    assert controller._generation == gen_before


def test_error_speaks_in_all_non_off_modes():
    for mode in ("verbatim", "summary", "conversational"):
        controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode=mode)
        controller.speak_event("error", "claude")
        assert len(speak_calls) == 1, f"mode={mode}"
        assert speak_calls[0]["text"] == "Claude hit an error."


def test_speak_event_debounce_collapses_repeats():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode="verbatim")
    controller.speak_event("needs_input", "claude")
    controller.speak_event("needs_input", "claude")  # inside the 5s window
    assert len(speak_calls) == 1


def test_speak_event_uses_configured_voice_speed_volume():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        mode="verbatim", voice=5, speed=1.4, volume=0.6
    )
    controller.speak_event("needs_input", "claude")
    assert len(speak_calls) == 1
    assert {
        key: value
        for key, value in speak_calls[0].items()
        if key != "on_terminal"
    } == {
        "text": "Claude needs your approval to continue.",
        "sid": 5,
        "speed": 1.4,
        "volume": 0.6,
    }


def test_normal_speak_event_waits_while_recording_but_progress_is_dropped():
    """Ordinary speech waits behind dictation; low-priority progress does not."""
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode="conversational")
    idle_calls = _wire_speak_if_idle(controller)
    recording = [True]
    controller._recording_provider = lambda: recording[0]

    controller.speak_event("needs_input", "claude")
    controller.speak_event("progress", "claude", "subagent_done")

    assert speak_calls == []
    assert idle_calls == []
    recording[0] = False
    assert _wait_until(lambda: len(speak_calls) == 1)
    assert speak_calls[0]["text"] == "Claude needs your approval to continue."
    speak_calls[0]["on_terminal"]("completed")


def test_speak_event_recording_probe_error_fails_open():
    def _boom():
        raise RuntimeError("probe broke")

    controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode="verbatim")
    controller._recording_provider = _boom
    controller.speak_event("needs_input", "claude")
    assert len(speak_calls) == 1


def test_speak_event_noop_when_model_missing():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(installed=False)
    controller.speak_event("needs_input", "claude")
    assert speak_calls == []


def test_speak_event_never_raises():
    controller, _cfg, _speak, _stop, _bc = _make_controller(mode="verbatim")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("playback broke")

    controller._playback.speak = _boom  # type: ignore[method-assign]
    controller.speak_event("needs_input", "claude")  # must not raise


def test_speak_event_templates_have_no_em_dash():
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(mode="conversational")
    idle_calls = _wire_speak_if_idle(controller)
    controller.speak_event("needs_input", "claude")
    controller.speak_event("error", "claude")
    controller.speak_event("progress", "claude", "subagent_done")
    for call in speak_calls + idle_calls:
        assert "—" not in call["text"]


def test_conversational_mode_summarizes_responses():
    """speak_response in conversational mode takes the summary path."""
    supervisor = MagicMock()
    controller, _cfg, speak_calls, _stop, _bc = _make_controller(
        mode="conversational", supervisor=supervisor
    )

    async def _fake_speak_text_for(text, mode, sup, model):
        assert mode == "conversational"
        assert sup is supervisor  # supervisor IS passed (summary-shaped mode)
        return "Short summary."

    with patch(
        "whisperstream.speech.controller.summarizer.speak_text_for",
        _fake_speak_text_for,
    ):
        asyncio.run(controller.speak_response("A long agent reply."))

    assert [c["text"] for c in speak_calls] == ["Short summary."]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

@pytest.fixture(autouse=True)
def _enable_optional_kokoro(monkeypatch):
    # Exercise retained two-engine behavior; final-edition boundary tests cover availability.
    from whisperstream import _build_flags
    monkeypatch.setattr(_build_flags, "EXPERIMENTAL_AVAILABLE", True)
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
