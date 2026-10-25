"""Unit tests for has_input_device() in audio_capture.py.

Pins these decisions:

- has_input_device() returns False when list_input_devices() returns [].
- has_input_device() returns True when list_input_devices() returns a
  non-empty list.
- has_input_device() returns False when list_input_devices() raises any
  Exception (caught, logged at warning, treated as no devices — defensive
  against PortAudio failures).

Tests use monkeypatch to swap list_input_devices with stubs without
touching the real sounddevice / PortAudio layer.
"""

from __future__ import annotations

import logging

import pytest

import whisperstream.audio_capture as ac_module
from whisperstream.audio_capture import has_input_device


def test_has_input_device_returns_false_when_no_devices(monkeypatch):
    """has_input_device() must return False when no input devices are found
    on either the cached or re-init pass.
    """
    monkeypatch.setattr(ac_module, "list_input_devices", lambda: [])
    monkeypatch.setattr(ac_module, "_reinit_portaudio", lambda: None)
    assert has_input_device() is False


def test_has_input_device_returns_true_when_devices_present(monkeypatch):
    """has_input_device() must return True when at least one device is found
    on the fast (cached) path, without calling _reinit_portaudio.
    """
    fake_device = {
        "name": "Microphone (Realtek)",
        "index": 0,
        "channels": 2,
        "sample_rate": 44100.0,
    }
    reinit_calls = {"count": 0}

    def _track_reinit():
        reinit_calls["count"] += 1

    monkeypatch.setattr(ac_module, "list_input_devices", lambda: [fake_device])
    monkeypatch.setattr(ac_module, "_reinit_portaudio", _track_reinit)
    assert has_input_device() is True
    assert reinit_calls["count"] == 0, "fast path should not re-init PortAudio"


def test_has_input_device_reinit_recovers_after_empty_cache(monkeypatch):
    """has_input_device() must re-initialise PortAudio when the cached
    enumeration shows zero devices, then re-check. This covers the
    plugged-in-after-launch case where PortAudio's cached list is stale.
    """
    fake_device = {
        "name": "Microphone (Realtek)",
        "index": 0,
        "channels": 2,
        "sample_rate": 44100.0,
    }
    call_count = {"count": 0}

    def _flaky_list():
        call_count["count"] += 1
        # First call: cache is stale, returns empty.
        # Second call (after _reinit_portaudio): mic is now visible.
        return [] if call_count["count"] == 1 else [fake_device]

    reinit_calls = {"count": 0}

    def _track_reinit():
        reinit_calls["count"] += 1

    monkeypatch.setattr(ac_module, "list_input_devices", _flaky_list)
    monkeypatch.setattr(ac_module, "_reinit_portaudio", _track_reinit)

    assert has_input_device() is True
    assert call_count["count"] == 2, "expected two enumeration calls (cached + post-reinit)"
    assert reinit_calls["count"] == 1, "expected one PortAudio re-init"


def test_has_input_device_returns_false_when_reinit_raises(monkeypatch, caplog):
    """If PortAudio re-init itself raises, has_input_device() must log a
    warning with the exception class and return False without re-checking.
    """
    monkeypatch.setattr(ac_module, "list_input_devices", lambda: [])

    def _raise_reinit():
        raise RuntimeError("PortAudio re-init failed")

    monkeypatch.setattr(ac_module, "_reinit_portaudio", _raise_reinit)

    with caplog.at_level(logging.WARNING, logger="whisperstream.audio_capture"):
        result = has_input_device()

    assert result is False
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("RuntimeError" in msg for msg in warning_messages), (
        f"Expected a warning mentioning 'RuntimeError'; got: {warning_messages}"
    )


def test_has_input_device_returns_false_when_list_raises(monkeypatch, caplog):
    """has_input_device() must catch any Exception from list_input_devices(),
    log a warning with the exception class name, and return False.
    """
    def _raise():
        raise OSError("PortAudio failed to initialise")

    monkeypatch.setattr(ac_module, "list_input_devices", _raise)

    with caplog.at_level(logging.WARNING, logger="whisperstream.audio_capture"):
        result = has_input_device()

    assert result is False
    # The warning log must include the exception class name.
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("OSError" in msg for msg in warning_messages), (
        f"Expected a warning mentioning 'OSError'; got: {warning_messages}"
    )
