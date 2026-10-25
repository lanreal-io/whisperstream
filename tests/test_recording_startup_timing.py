from unittest.mock import MagicMock, call, patch

from whisperstream.main import WhisperstreamApp


def test_recording_startup_checkpoints_use_monotonic_milliseconds():
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock()

    with patch("whisperstream.main.time.monotonic", side_effect=[10.0, 10.012, 10.047]):
        app._begin_recording_startup_timing()
        app._recording_startup_checkpoint("start_chime_begin")
        app._recording_startup_checkpoint("start_chime_complete", terminal=True)

    assert app.logger.debug.call_args_list == [
        call(
            "recording startup checkpoint startup_seq=%d phase=precheck_complete "
            "elapsed_ms=0 delta_ms=0",
            1,
        ),
        call(
            "recording startup checkpoint startup_seq=%d phase=%s "
            "elapsed_ms=%d delta_ms=%d",
            1,
            "start_chime_begin",
            12,
            12,
        ),
        call(
            "recording startup checkpoint startup_seq=%d phase=%s "
            "elapsed_ms=%d delta_ms=%d",
            1,
            "start_chime_complete",
            47,
            35,
        ),
    ]
    assert app._recording_startup_timing_active is False


def test_direct_recording_start_starts_its_own_timing_sequence():
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock()

    with patch("whisperstream.main.time.monotonic", side_effect=[20.0, 20.004]):
        app._recording_startup_checkpoint("capture_open_begin")

    assert app._recording_startup_sequence == 1
    assert app.logger.debug.call_args_list[-1] == call(
        "recording startup checkpoint startup_seq=%d phase=%s "
        "elapsed_ms=%d delta_ms=%d",
        1,
        "capture_open_begin",
        4,
        4,
    )
