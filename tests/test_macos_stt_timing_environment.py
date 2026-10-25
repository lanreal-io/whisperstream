"""The audited native timing policy cannot drift with a caller's shell."""

from pathlib import Path
from unittest.mock import Mock, patch

from whisperstream.macos_stt_manager import MacosFluidAudioManager


def test_manager_pins_native_timing_without_mutating_caller_environment(monkeypatch):
    monkeypatch.setenv("TDT_EMISSION_DELAY_FRAMES", "17")
    environment = {"HOME": "/synthetic/home", "TDT_EMISSION_DELAY_FRAMES": "9"}
    with patch("whisperstream.macos_stt_manager.SttWorkerSupervisor") as supervisor, patch(
        "whisperstream.macos_stt_manager.SupervisedSttManager.__init__", return_value=None
    ):
        MacosFluidAudioManager(
            logger=Mock(), worker_path=Path("/synthetic/worker"),
            worker_environment=environment,
        )
    actual = supervisor.call_args.kwargs["environment"]
    assert actual == {"HOME": "/synthetic/home", "TDT_EMISSION_DELAY_FRAMES": "1"}
    assert environment["TDT_EMISSION_DELAY_FRAMES"] == "9"
