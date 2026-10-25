"""Usage counters retain their saved snapshot when persistence fails."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from whisperstream import atomic_io
from whisperstream.server.stats import UsageStats


@pytest.mark.parametrize("saved", ["not json", "null", "{}", "[]", '"counter"', "42"])
def test_invalid_saved_object_recovers_without_blocking_recording(tmp_path, monkeypatch, saved):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    (tmp_path / "stats.json").write_text(saved, encoding="utf-8")

    stats = UsageStats()
    stats.record_transcription("two words", 125)

    assert stats.to_dict() == {
        "total_transcriptions": 1, "total_words": 2, "total_recording_ms": 125,
    }


@pytest.mark.parametrize("field", ["total_transcriptions", "total_words", "total_recording_ms"])
@pytest.mark.parametrize("invalid", [None, "4", True, -1, 1.5, float("nan"), float("inf"), []])
def test_invalid_counter_defaults_without_resetting_valid_counters(tmp_path, monkeypatch, field, invalid):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    saved = {"total_transcriptions": 3, "total_words": 8, "total_recording_ms": 100}
    saved[field] = invalid
    (tmp_path / "stats.json").write_text(json.dumps(saved), encoding="utf-8")

    stats = UsageStats()
    expected = {**saved, field: 0}
    assert stats.to_dict() == expected
    stats.record_transcription("two words", 25)
    expected["total_transcriptions"] += 1
    expected["total_words"] += 2
    expected["total_recording_ms"] += 25
    assert stats.to_dict() == expected


def test_partial_snapshot_keeps_valid_counters_and_drops_unknown_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    (tmp_path / "stats.json").write_text('{"total_words": 8, "unknown": "ignored"}', encoding="utf-8")

    stats = UsageStats()
    assert stats.to_dict() == {
        "total_transcriptions": 0, "total_words": 8, "total_recording_ms": 0,
    }
    stats.record_transcription("one", 5)
    assert UsageStats().to_dict() == {
        "total_transcriptions": 1, "total_words": 9, "total_recording_ms": 5,
    }


def test_stats_round_trip_and_snapshot_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    stats = UsageStats()
    stats.record_transcription("two words", 125)
    snapshot = stats.to_dict()
    snapshot["total_words"] = 999
    assert UsageStats().to_dict() == {
        "total_transcriptions": 1,
        "total_words": 2,
        "total_recording_ms": 125,
    }
    assert stats.to_dict()["total_words"] == 2


def test_failed_replace_preserves_saved_counters(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    stats = UsageStats()
    stats.record_transcription("first", 100)
    saved = (tmp_path / "stats.json").read_bytes()

    def interrupted_replace(source, destination):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(atomic_io.os, "replace", interrupted_replace)
    stats.record_transcription("second", 200)
    assert (tmp_path / "stats.json").read_bytes() == saved
    assert UsageStats().to_dict()["total_transcriptions"] == 1
    assert stats.to_dict()["total_transcriptions"] == 2


def test_concurrent_records_serialize_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    stats = UsageStats()
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: stats.record_transcription("two words", 10), range(20)))
    assert json.loads((tmp_path / "stats.json").read_text()) == {
        "total_transcriptions": 20,
        "total_words": 40,
        "total_recording_ms": 200,
    }
