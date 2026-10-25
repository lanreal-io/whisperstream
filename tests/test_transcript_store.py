"""Tests for the TranscriptStore module (SQLite index + encrypted WAV CRUD).

These tests use a temp data dir via the ``WHISPERSTREAM_DATA_DIR`` env var
(monkeypatched) so they NEVER touch the real app data directory. The store loads
its AES data key through ``transcript_crypto.load_or_create_data_key`` (sealed
via the DPAPI wrapper); on this Windows dev box that round-trips, so the
decrypt-on-read assertions exercise the real crypto path.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A fresh TranscriptStore rooted at a temp data dir."""
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    from whisperstream.transcript_store import TranscriptStore

    s = TranscriptStore()
    yield s
    s.close()


def _wav_bytes(seconds: float = 0.25, sample_rate: int = 16000) -> bytes:
    """A tiny valid 16-bit PCM mono WAV as in-memory bytes."""
    import io

    n = int(seconds * sample_rate)
    samples = (np.sin(np.linspace(0, 20 * np.pi, n)) * 0.5 * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def _provenance(
    *,
    profile_id: str = "code",
    profile_name: str = "Code",
    outcome: str = "success",
) -> SimpleNamespace:
    """Match the immutable provenance object passed by the live pipeline."""
    return SimpleNamespace(
        profile_id=profile_id,
        profile_name=profile_name,
        outcome=outcome,
    )


def _replace_provenance_payload(store, rid: str, payload: object) -> None:
    """Test-only seam for simulating future, malformed, and corrupt rows."""
    from whisperstream.transcript_crypto import encrypt

    nonce, ciphertext = encrypt(
        store._data_key,
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )
    store._conn.execute(
        "UPDATE recordings SET enhancement_provenance = ?, "
        "enhancement_provenance_nonce = ? WHERE id = ?",
        (ciphertext, nonce, rid),
    )
    store._conn.commit()


def _create_legacy_transcripts_db(path: Path) -> None:
    """Create the pre-provenance recordings schema without opening the store."""
    conn = sqlite3.connect(str(path / "transcripts.db"))
    try:
        conn.execute(
            """
            CREATE TABLE recordings (
                id TEXT PRIMARY KEY,
                created_at INTEGER,
                duration_ms INTEGER,
                model TEXT,
                raw_text BLOB,
                raw_nonce BLOB,
                enhanced_text BLOB,
                enhanced_nonce BLOB,
                audio_path TEXT,
                audio_nonce BLOB,
                peaks TEXT,
                peaks_ciphertext BLOB,
                peaks_nonce BLOB,
                source TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# WAL pragma + schema
# --------------------------------------------------------------------------- #


def test_wal_pragma_is_set(store, tmp_path):
    """The DB is opened in WAL journal mode."""
    db_path = tmp_path / "transcripts.db"
    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_decryptable_true_on_fresh_store(store):
    """A fresh store on this machine can load/create its data key."""
    assert store.decryptable is True


def test_legacy_schema_adds_nullable_provenance_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    _create_legacy_transcripts_db(tmp_path)
    import whisperstream.transcript_store as ts_mod

    monkeypatch.setattr(
        ts_mod, "load_or_create_data_key_with_failure", lambda _path, **_k: (b"k" * 32, None)
    )
    migrated = ts_mod.TranscriptStore()
    try:
        columns = {
            row["name"]: row
            for row in migrated._conn.execute(
                "PRAGMA table_info(recordings)"
            ).fetchall()
        }
        assert columns["enhancement_provenance"]["notnull"] == 0
        assert columns["enhancement_provenance_nonce"]["notnull"] == 0
    finally:
        migrated.close()


def test_keyless_lockdown_adds_schema_and_unlocks_without_content_migration(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    _create_legacy_transcripts_db(tmp_path)
    from whisperstream import lockdown_keystore
    import whisperstream.transcript_store as ts_mod

    monkeypatch.setattr(lockdown_keystore, "is_lockdown_enabled", lambda _path: True)
    locked = ts_mod.TranscriptStore()
    try:
        assert locked.decryptable is False
        columns = {
            row["name"]
            for row in locked._conn.execute("PRAGMA table_info(recordings)").fetchall()
        }
        assert {
            "enhancement_provenance",
            "enhancement_provenance_nonce",
        } <= columns
        assert locked.save(
            raw_text="must not persist while locked",
            enhanced_text=None,
            duration_ms=1,
            model="m",
            source="dictation",
            enhancement_provenance=_provenance(outcome="timeout"),
        ) is None

        assert locked.set_data_key(b"d" * 32) is True
        rid = locked.save(
            raw_text="persists after unlock",
            enhanced_text=None,
            duration_ms=1,
            model="m",
            source="dictation",
            enhancement_provenance=_provenance(outcome="timeout"),
        )
        assert locked.get(rid)["enhancement_provenance"] == {
            "profile_id": "code",
            "profile_name": "Code",
            "outcome": "timeout",
        }
    finally:
        locked.close()


# --------------------------------------------------------------------------- #
# save -> get round-trip (raw + enhanced + audio)
# --------------------------------------------------------------------------- #


def test_save_get_roundtrip_with_audio(store):
    """raw, enhanced, and audio bytes all decrypt back correctly."""
    audio = _wav_bytes()
    rid = store.save(
        raw_text="the raw transcript",
        enhanced_text="The enhanced transcript.",
        duration_ms=2500,
        model="parakeet-v3",
        source="dictation",
        audio_bytes=audio,
        peaks=[0.1, 0.5, 1.0],
    )
    assert rid is not None

    rec = store.get(rid)
    assert rec is not None
    assert rec["raw_text"] == "the raw transcript"
    assert rec["enhanced_text"] == "The enhanced transcript."
    assert rec["duration_ms"] == 2500
    assert rec["model"] == "parakeet-v3"
    assert rec["peaks"] == [0.1, 0.5, 1.0]
    assert rec["has_audio"] is True
    assert isinstance(rec["created_at"], int)

    # Audio bytes decrypt back to the exact original WAV.
    assert store.get_audio_bytes(rid) == audio


@pytest.mark.parametrize(
    ("enhanced_text", "outcome"),
    [
        ("The enhanced transcript.", "success"),
        (None, "timeout"),
        ("The partially enhanced transcript.", "error"),
    ],
)
def test_provenance_round_trips_through_list_and_detail(
    store, enhanced_text, outcome
):
    rid = store.save(
        raw_text="the raw transcript",
        enhanced_text=enhanced_text,
        duration_ms=2500,
        model="parakeet-v3",
        source="dictation",
        enhancement_provenance=_provenance(outcome=outcome),
    )

    expected = {
        "profile_id": "code",
        "profile_name": "Code",
        "outcome": outcome,
    }
    assert store.get(rid)["enhancement_provenance"] == expected
    assert store.list()["items"][0]["enhancement_provenance"] == expected


def test_legacy_and_unattempted_records_return_null_provenance(store):
    rid = store.save(
        raw_text="ordinary original transcript",
        enhanced_text=None,
        duration_ms=1,
        model="m",
        source="dictation",
    )

    assert store.get(rid)["enhancement_provenance"] is None
    assert store.list()["items"][0]["enhancement_provenance"] is None


def test_provenance_is_ciphertext_at_rest(store, tmp_path):
    profile_id = "private-profile-id-9fc4"
    profile_name = "Private client profile 9fc4"
    rid = store.save(
        raw_text="at rest provenance test",
        enhanced_text="At rest provenance test.",
        duration_ms=1,
        model="m",
        source="dictation",
        enhancement_provenance=_provenance(
            profile_id=profile_id,
            profile_name=profile_name,
        ),
    )

    conn = sqlite3.connect(str(tmp_path / "transcripts.db"))
    try:
        row = conn.execute(
            "SELECT enhancement_provenance, enhancement_provenance_nonce "
            "FROM recordings WHERE id = ?",
            (rid,),
        ).fetchone()
    finally:
        conn.close()

    assert isinstance(row[0], bytes)
    assert profile_id.encode() not in row[0]
    assert profile_name.encode() not in row[0]
    assert b'"outcome":"success"' not in row[0]
    assert isinstance(row[1], bytes) and len(row[1]) == 12
    for db_file in tmp_path.glob("transcripts.db*"):
        db_bytes = db_file.read_bytes()
        assert profile_id.encode() not in db_bytes
        assert profile_name.encode() not in db_bytes


@pytest.mark.parametrize(
    "provenance",
    [
        _provenance(profile_id=""),
        _provenance(profile_name=""),
        _provenance(outcome="provider exploded"),
        _provenance(outcome=["error"]),
        _provenance(profile_id="i" * 129),
        _provenance(profile_name="n" * 257),
        _provenance(profile_name="\ud800"),
        {"profile_id": "code", "profile_name": "Code"},
    ],
)
def test_invalid_provenance_is_omitted_without_losing_transcript(
    store, provenance
):
    rid = store.save(
        raw_text="raw remains readable",
        enhanced_text="Enhanced remains readable.",
        duration_ms=1,
        model="m",
        source="dictation",
        enhancement_provenance=provenance,
    )

    record = store.get(rid)
    assert record["raw_text"] == "raw remains readable"
    assert record["enhanced_text"] == "Enhanced remains readable."
    assert record["enhancement_provenance"] is None


def test_success_provenance_without_enhanced_text_is_omitted(store):
    rid = store.save(
        raw_text="complete fallback",
        enhanced_text=None,
        duration_ms=1,
        model="m",
        source="dictation",
        enhancement_provenance=_provenance(outcome="success"),
    )

    assert store.get(rid)["raw_text"] == "complete fallback"
    assert store.get(rid)["enhancement_provenance"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"v": 2, "profile_id": "code", "profile_name": "Code", "outcome": "success"},
        {"v": True, "profile_id": "code", "profile_name": "Code", "outcome": "success"},
        {"v": 1.0, "profile_id": "code", "profile_name": "Code", "outcome": "success"},
        {"v": 1, "profile_id": "code", "profile_name": "Code", "outcome": "unknown"},
        {"v": 1, "profile_id": "code", "profile_name": "Code", "outcome": ["error"]},
        {"v": 1, "profile_id": "code", "profile_name": "n" * 257, "outcome": "success"},
        ["not", "an", "object"],
    ],
)
def test_unknown_or_invalid_encrypted_provenance_degrades_to_null(store, payload):
    rid = store.save(
        raw_text="raw remains readable",
        enhanced_text="Enhanced remains readable.",
        duration_ms=1,
        model="m",
        source="dictation",
        enhancement_provenance=_provenance(),
    )
    _replace_provenance_payload(store, rid, payload)

    record = store.get(rid)
    assert record["raw_text"] == "raw remains readable"
    assert record["enhanced_text"] == "Enhanced remains readable."
    assert record["enhancement_provenance"] is None
    assert store.list()["items"][0]["enhancement_provenance"] is None


def test_corrupt_or_incomplete_provenance_ciphertext_degrades_to_null(store):
    ids = []
    for suffix in ("corrupt", "missing nonce"):
        ids.append(
            store.save(
                raw_text=f"raw {suffix}",
                enhanced_text=f"Enhanced {suffix}.",
                duration_ms=1,
                model="m",
                source="dictation",
                enhancement_provenance=_provenance(),
            )
        )
    store._conn.execute(
        "UPDATE recordings SET enhancement_provenance = ? WHERE id = ?",
        (b"not authenticated ciphertext", ids[0]),
    )
    store._conn.execute(
        "UPDATE recordings SET enhancement_provenance_nonce = NULL WHERE id = ?",
        (ids[1],),
    )
    store._conn.commit()

    for rid in ids:
        record = store.get(rid)
        assert record["raw_text"].startswith("raw ")
        assert record["enhancement_provenance"] is None


def test_impossible_persisted_success_pairing_degrades_to_null(store):
    rid = store.save(
        raw_text="original after fallback",
        enhanced_text=None,
        duration_ms=1,
        model="m",
        source="dictation",
        enhancement_provenance=_provenance(outcome="timeout"),
    )
    _replace_provenance_payload(
        store,
        rid,
        {"v": 1, "profile_id": "code", "profile_name": "Code", "outcome": "success"},
    )

    record = store.get(rid)
    assert record["raw_text"] == "original after fallback"
    assert record["enhancement_provenance"] is None


def test_waveform_peaks_are_ciphertext_at_rest(store, tmp_path):
    rid = store.save(
        raw_text="peak privacy",
        enhanced_text=None,
        duration_ms=10,
        model="m",
        source="dictation",
        peaks=[0.123456, 0.75],
    )
    conn = sqlite3.connect(str(tmp_path / "transcripts.db"))
    try:
        row = conn.execute(
            "SELECT peaks, peaks_ciphertext, peaks_nonce FROM recordings WHERE id = ?",
            (rid,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] is None
    assert isinstance(row[1], bytes) and b"0.123456" not in row[1]
    assert isinstance(row[2], bytes) and len(row[2]) == 12
    assert store.get(rid)["peaks"] == [0.123456, 0.75]


def test_legacy_plaintext_peaks_migrate_on_unlocked_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    from whisperstream.transcript_store import TranscriptStore

    seeded = TranscriptStore()
    rid = seeded.save(
        raw_text="legacy", enhanced_text=None, duration_ms=1, model="m",
        source="dictation", peaks=[0.2, 0.8],
    )
    seeded.close()
    conn = sqlite3.connect(str(tmp_path / "transcripts.db"))
    conn.execute(
        "UPDATE recordings SET peaks = ?, peaks_ciphertext = NULL, peaks_nonce = NULL "
        "WHERE id = ?",
        ("[0.2,0.8]", rid),
    )
    conn.commit()
    conn.close()

    migrated = TranscriptStore()
    try:
        assert migrated.get(rid)["peaks"] == [0.2, 0.8]
        row = migrated._conn.execute(
            "SELECT peaks, peaks_ciphertext, peaks_nonce FROM recordings WHERE id = ?",
            (rid,),
        ).fetchone()
        assert row["peaks"] is None
        assert row["peaks_ciphertext"] is not None
        assert row["peaks_nonce"] is not None
    finally:
        migrated.close()


def test_keyless_startup_defers_peaks_and_unlock_retry_is_transactional(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    from whisperstream import lockdown_keystore
    import whisperstream.transcript_store as ts_mod

    seeded = ts_mod.TranscriptStore()
    data_key = seeded._data_key
    rid = seeded.save(
        raw_text="legacy locked", enhanced_text=None, duration_ms=1, model="m",
        source="dictation", peaks=[0.4],
    )
    seeded.close()
    conn = sqlite3.connect(str(tmp_path / "transcripts.db"))
    conn.execute(
        "UPDATE recordings SET peaks = '[0.4]', peaks_ciphertext = NULL, "
        "peaks_nonce = NULL WHERE id = ?",
        (rid,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(lockdown_keystore, "is_lockdown_enabled", lambda _p: True)
    locked = ts_mod.TranscriptStore()
    assert locked.decryptable is False
    assert locked._conn.execute(
        "SELECT peaks FROM recordings WHERE id = ?", (rid,)
    ).fetchone()["peaks"] == "[0.4]"

    original_encrypt = ts_mod.encrypt
    monkeypatch.setattr(ts_mod, "encrypt", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert locked.set_data_key(data_key) is False
    assert locked.decryptable is False
    assert locked._conn.execute(
        "SELECT peaks FROM recordings WHERE id = ?", (rid,)
    ).fetchone()["peaks"] == "[0.4]"

    monkeypatch.setattr(ts_mod, "encrypt", original_encrypt)
    assert locked.set_data_key(data_key) is True
    assert locked.get(rid)["peaks"] == [0.4]
    assert locked._conn.execute(
        "SELECT peaks FROM recordings WHERE id = ?", (rid,)
    ).fetchone()["peaks"] is None
    locked.close()


def test_get_unknown_id_returns_none(store):
    assert store.get("does-not-exist") is None
    assert store.get_audio_bytes("does-not-exist") is None


# --------------------------------------------------------------------------- #
# list ordering + cursor pagination
# --------------------------------------------------------------------------- #


def test_list_newest_first(store):
    ids = []
    for i in range(3):
        ids.append(
            store.save(
                raw_text=f"text {i}",
                enhanced_text=None,
                duration_ms=1000,
                model="m",
                source="dictation",
                created_at=1000 + i,  # explicit ascending timestamps
            )
        )
    result = store.list()
    listed = [item["id"] for item in result["items"]]
    # newest (largest created_at) first
    assert listed == list(reversed(ids))


def test_cursor_pagination(store):
    """N=25, limit=20 -> 20 items + next_cursor; second page -> the rest, next_cursor=None."""
    for i in range(25):
        store.save(
            raw_text=f"row {i}",
            enhanced_text=None,
            duration_ms=1000,
            model="m",
            source="dictation",
            created_at=1000 + i,
            enhancement_provenance=_provenance(outcome="timeout"),
        )

    page1 = store.list(limit=20)
    assert len(page1["items"]) == 20
    assert page1["next_cursor"] is not None

    page2 = store.list(before=page1["next_cursor"], limit=20)
    assert len(page2["items"]) == 5
    assert page2["next_cursor"] is None

    # No overlap between the two pages.
    ids1 = {it["id"] for it in page1["items"]}
    ids2 = {it["id"] for it in page2["items"]}
    assert ids1.isdisjoint(ids2)
    assert len(ids1 | ids2) == 25
    assert all(
        item["enhancement_provenance"]["outcome"] == "timeout"
        for item in page1["items"] + page2["items"]
    )


def test_list_snippet_prefers_enhanced(store):
    store.save(
        raw_text="raw version here",
        enhanced_text="enhanced version here",
        duration_ms=1000,
        model="m",
        source="dictation",
    )
    item = store.list()["items"][0]
    assert "enhanced version here" in item["snippet"]
    assert item["has_enhanced"] is True


def test_list_marks_original_only_items(store):
    store.save(
        raw_text="original version here",
        enhanced_text=None,
        duration_ms=1000,
        model="m",
        source="dictation",
    )
    item = store.list()["items"][0]
    assert item["has_enhanced"] is False


def test_list_has_audio_flag(store):
    store.save(
        raw_text="with audio",
        enhanced_text=None,
        duration_ms=1000,
        model="m",
        source="dictation",
        audio_bytes=_wav_bytes(),
    )
    store.save(
        raw_text="no audio",
        enhanced_text=None,
        duration_ms=1000,
        model="m",
        source="dictation",
    )
    items = store.list()["items"]
    by_snip = {it["snippet"][:8]: it["has_audio"] for it in items}
    # snippet "with aud" -> True, "no audio" -> False
    assert any(v for v in by_snip.values())
    assert any(v is False for v in by_snip.values())


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #


def test_search_filters_on_decrypted_text(store):
    store.save(raw_text="quick brown fox", enhanced_text=None, duration_ms=1, model="m", source="dictation")
    store.save(raw_text="lazy dog sleeps", enhanced_text=None, duration_ms=1, model="m", source="dictation")
    store.save(
        raw_text="another fox tale",
        enhanced_text="A polished FOX story.",
        duration_ms=1,
        model="m",
        source="dictation",
        enhancement_provenance=_provenance(),
    )

    result = store.list(q="fox")
    snippets = [it["snippet"] for it in result["items"]]
    assert len(result["items"]) == 2  # "quick brown fox" + enhanced "FOX" (case-insensitive)
    assert all("fox" in s.lower() for s in snippets)
    assert any(
        item["enhancement_provenance"] == {
            "profile_id": "code",
            "profile_name": "Code",
            "outcome": "success",
        }
        for item in result["items"]
    )

    # No matches.
    assert store.list(q="zzzznomatch")["items"] == []


def test_search_is_case_insensitive(store):
    store.save(raw_text="Hello World", enhanced_text=None, duration_ms=1, model="m", source="dictation")
    assert len(store.list(q="hello")["items"]) == 1
    assert len(store.list(q="WORLD")["items"]) == 1


# --------------------------------------------------------------------------- #
# text-only save (no audio)
# --------------------------------------------------------------------------- #


def test_text_only_save_has_no_audio(store):
    rid = store.save(
        raw_text="text only, no voice",
        enhanced_text=None,
        duration_ms=1500,
        model="m",
        source="dictation",
    )
    rec = store.get(rid)
    assert rec["has_audio"] is False
    assert store.get_audio_bytes(rid) is None


# --------------------------------------------------------------------------- #
# delete / clear
# --------------------------------------------------------------------------- #


def test_delete_removes_row_and_file(store, tmp_path):
    rid = store.save(
        raw_text="delete me",
        enhanced_text=None,
        duration_ms=1,
        model="m",
        source="dictation",
        audio_bytes=_wav_bytes(),
    )
    rec = store.get(rid)
    assert rec is not None
    # Locate the on-disk audio file.
    rel = store._audio_path_for(rid)  # internal helper
    audio_file = tmp_path / rel
    assert audio_file.exists()

    assert store.delete(rid) is True
    assert store.get(rid) is None
    assert not audio_file.exists()
    # Deleting again is a no-op False.
    assert store.delete(rid) is False


def test_clear_empties_rows_and_files(store, tmp_path):
    for i in range(3):
        store.save(
            raw_text=f"clear {i}",
            enhanced_text=None,
            duration_ms=1,
            model="m",
            source="dictation",
            audio_bytes=_wav_bytes(),
        )
    recordings_dir = tmp_path / "recordings"
    assert len(list(recordings_dir.glob("*.wav"))) == 3

    count = store.clear()
    assert count == 3
    assert store.list()["items"] == []
    assert len(list(recordings_dir.glob("*.wav"))) == 0


# --------------------------------------------------------------------------- #
# purge_older_than (retention)
# --------------------------------------------------------------------------- #


def test_purge_older_than_deletes_only_old(store, tmp_path):
    old_id = store.save(
        raw_text="old one",
        enhanced_text=None,
        duration_ms=1,
        model="m",
        source="dictation",
        audio_bytes=_wav_bytes(),
        created_at=1000,
    )
    new_id = store.save(
        raw_text="new one",
        enhanced_text=None,
        duration_ms=1,
        model="m",
        source="dictation",
        audio_bytes=_wav_bytes(),
        created_at=9_000_000,
    )
    old_file = tmp_path / store._audio_path_for(old_id)
    new_file = tmp_path / store._audio_path_for(new_id)
    assert old_file.exists() and new_file.exists()

    purged = store.purge_older_than(5000)
    assert purged == 1
    assert store.get(old_id) is None
    assert store.get(new_id) is not None
    assert not old_file.exists()
    assert new_file.exists()


# --------------------------------------------------------------------------- #
# sweep_orphans
# --------------------------------------------------------------------------- #


def test_sweep_orphans_removes_unreferenced_keeps_referenced(store, tmp_path):
    rid = store.save(
        raw_text="referenced",
        enhanced_text=None,
        duration_ms=1,
        model="m",
        source="dictation",
        audio_bytes=_wav_bytes(),
    )
    referenced = tmp_path / store._audio_path_for(rid)
    assert referenced.exists()

    # Drop an orphan file into recordings/ that no row references.
    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir(parents=True, exist_ok=True)
    orphan = recordings_dir / "deadbeef-orphan.wav"
    orphan.write_bytes(b"junk")
    assert orphan.exists()

    removed = store.sweep_orphans()
    assert removed == 1
    assert not orphan.exists()
    assert referenced.exists()


def test_sweep_orphans_on_empty_recordings_dir(store):
    """No crash when recordings/ is empty or missing."""
    assert store.sweep_orphans() == 0


def test_insert_failure_rolls_back_then_removes_new_audio(store, tmp_path):
    store._conn.execute(
        "CREATE TRIGGER reject_record BEFORE INSERT ON recordings "
        "BEGIN SELECT RAISE(ABORT, 'reject'); END"
    )
    store._conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        store.save(
            raw_text="failed row", enhanced_text=None, duration_ms=1,
            model="m", source="dictation", audio_bytes=_wav_bytes(),
        )

    assert store._conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 0
    assert list((tmp_path / "recordings").glob("*.wav")) == []


def test_commit_failure_rolls_back_before_audio_cleanup(store, tmp_path):
    real_conn = store._conn

    class _FailCommit:
        def __getattr__(self, name):
            return getattr(real_conn, name)

        def commit(self):
            raise sqlite3.OperationalError("commit rejected")

    store._conn = _FailCommit()
    with pytest.raises(sqlite3.OperationalError):
        store.save(
            raw_text="failed commit", enhanced_text=None, duration_ms=1,
            model="m", source="dictation", audio_bytes=_wav_bytes(),
        )
    store._conn = real_conn

    assert real_conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 0
    assert list((tmp_path / "recordings").glob("*.wav")) == []


def test_sweep_waits_for_audio_row_publication(store, tmp_path, monkeypatch):
    original_write = Path.write_bytes
    audio_written = threading.Event()
    release_write = threading.Event()

    def _blocking_write(path, data):
        result = original_write(path, data)
        if path.parent.name == "recordings":
            audio_written.set()
            assert release_write.wait(timeout=2)
        return result

    monkeypatch.setattr(Path, "write_bytes", _blocking_write)
    saved: list[str | None] = []
    save_thread = threading.Thread(
        target=lambda: saved.append(
            store.save(
                raw_text="concurrent", enhanced_text=None, duration_ms=1,
                model="m", source="dictation", audio_bytes=_wav_bytes(),
            )
        )
    )
    save_thread.start()
    assert audio_written.wait(timeout=2)

    swept: list[int] = []
    sweep_thread = threading.Thread(target=lambda: swept.append(store.sweep_orphans()))
    sweep_thread.start()
    time.sleep(0.05)
    assert sweep_thread.is_alive(), "sweep must wait for save publication lock"
    release_write.set()
    save_thread.join(timeout=2)
    sweep_thread.join(timeout=2)

    assert swept == [0]
    rid = saved[0]
    assert rid is not None
    assert (tmp_path / store._audio_path_for(rid)).exists()


def test_clear_key_waits_for_inflight_save(store, monkeypatch):
    import whisperstream.transcript_store as ts_mod

    original_encrypt_text = ts_mod.encrypt_text
    encryption_started = threading.Event()
    release_encryption = threading.Event()

    def _blocking_encrypt_text(key, text):
        encryption_started.set()
        assert release_encryption.wait(timeout=2)
        return original_encrypt_text(key, text)

    monkeypatch.setattr(ts_mod, "encrypt_text", _blocking_encrypt_text)
    saved: list[str | None] = []
    save_thread = threading.Thread(
        target=lambda: saved.append(
            store.save(
                raw_text="locked save", enhanced_text=None, duration_ms=1,
                model="m", source="dictation",
            )
        )
    )
    save_thread.start()
    assert encryption_started.wait(timeout=2)
    clear_thread = threading.Thread(target=store.clear_data_key)
    clear_thread.start()
    time.sleep(0.05)
    assert clear_thread.is_alive(), "key clear must wait for save critical section"
    release_encryption.set()
    save_thread.join(timeout=2)
    clear_thread.join(timeout=2)

    assert saved[0] is not None
    assert store.decryptable is False


def test_orphan_unlink_failure_is_observable_and_retried(
    store, tmp_path, monkeypatch, caplog
):
    recordings = tmp_path / "recordings"
    recordings.mkdir(parents=True, exist_ok=True)
    orphan = recordings / "privacy-sensitive-name.wav"
    orphan.write_bytes(b"orphan")
    original_unlink = Path.unlink
    failed = False

    def _fail_once(path, *args, **kwargs):
        nonlocal failed
        if path == orphan and not failed:
            failed = True
            raise PermissionError("private path detail")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _fail_once)
    assert store.sweep_orphans() == 0
    assert orphan.exists()
    assert "status=retryable" in caplog.text
    assert "privacy-sensitive-name" not in caplog.text
    assert "private path detail" not in caplog.text
    assert store.sweep_orphans() == 1
    assert not orphan.exists()


# --------------------------------------------------------------------------- #
# run_retention_sweep helper (Task 2.3): age purge + orphan sweep
# --------------------------------------------------------------------------- #


class _Cfg:
    """Minimal stand-in for WhisperstreamConfig (only the field the helper reads)."""

    def __init__(self, days: int) -> None:
        self.transcript_retention_days = days


def test_retention_sweep_purges_old_and_orphans(store, tmp_path):
    """7-day window: old rows purged, recent kept, orphan audio file removed."""
    import time as _time

    from whisperstream.transcript_store import run_retention_sweep

    now_ms = int(_time.time() * 1000)
    old_a = store.save(
        raw_text="old A", enhanced_text=None, duration_ms=1, model="m",
        source="dictation", audio_bytes=_wav_bytes(),
        created_at=now_ms - 30 * 86_400_000,  # 30 days ago
    )
    old_b = store.save(
        raw_text="old B", enhanced_text=None, duration_ms=1, model="m",
        source="dictation", created_at=now_ms - 10 * 86_400_000,  # 10 days ago
    )
    recent = store.save(
        raw_text="recent", enhanced_text=None, duration_ms=1, model="m",
        source="dictation", audio_bytes=_wav_bytes(),
        created_at=now_ms - 1 * 86_400_000,  # 1 day ago (inside 7-day window)
    )

    # Drop an orphan audio file that no row references.
    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir(parents=True, exist_ok=True)
    orphan = recordings_dir / "deadbeef-orphan.wav"
    orphan.write_bytes(b"junk")
    assert orphan.exists()

    purged = run_retention_sweep(_Cfg(7), store)

    assert purged == 2  # both old rows
    assert store.get(old_a) is None
    assert store.get(old_b) is None
    assert store.get(recent) is not None
    assert not orphan.exists()  # orphan swept too


def test_retention_sweep_forever_skips_age_purge_but_sweeps_orphans(store, tmp_path):
    """Forever (0): no age purge, but orphan audio is still removed."""
    import time as _time

    from whisperstream.transcript_store import run_retention_sweep

    now_ms = int(_time.time() * 1000)
    old = store.save(
        raw_text="ancient", enhanced_text=None, duration_ms=1, model="m",
        source="dictation", created_at=now_ms - 365 * 86_400_000,  # a year ago
    )

    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir(parents=True, exist_ok=True)
    orphan = recordings_dir / "deadbeef-orphan.wav"
    orphan.write_bytes(b"junk")

    purged = run_retention_sweep(_Cfg(0), store)

    assert purged == 0  # Forever: no age purge
    assert store.get(old) is not None  # ancient row survives
    assert not orphan.exists()  # orphan still swept


def test_retention_sweep_none_store_is_noop():
    """A None store (init failed) is a safe no-op returning 0."""
    from whisperstream.transcript_store import run_retention_sweep

    assert run_retention_sweep(_Cfg(7), None) == 0


# --------------------------------------------------------------------------- #
# not-decryptable signal (data key unavailable)
# --------------------------------------------------------------------------- #


def test_not_decryptable_save_is_noop_and_list_signals(tmp_path, monkeypatch):
    """When the data key can't be loaded, save is a no-op and list/get signal it."""
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    import whisperstream.transcript_store as ts_mod

    monkeypatch.setattr(
        ts_mod,
        "load_or_create_data_key_with_failure",
        lambda _path, **_k: (None, ts_mod.DataKeyFailure.MISSING),
    )
    s = ts_mod.TranscriptStore()
    try:
        assert s.decryptable is False
        # save returns None (no-op) and writes nothing.
        rid = s.save(
            raw_text="should not persist",
            enhanced_text=None,
            duration_ms=1,
            model="m",
            source="dictation",
            audio_bytes=_wav_bytes(),
        )
        assert rid is None
        result = s.list()
        assert result["items"] == []
        assert result.get("decryptable") is False
        assert s.get("whatever") is None
    finally:
        s.close()


def test_reload_data_key_fails_closed_when_migration_needs_retry(
    store, monkeypatch
):
    import whisperstream.transcript_store as ts_mod

    data_key = store._data_key
    monkeypatch.setattr(ts_mod, "load_data_key", lambda _path, **_k: (data_key, None))
    monkeypatch.setattr(
        ts_mod, "load_or_create_data_key_with_failure", lambda _path, **_k: (data_key, None)
    )
    monkeypatch.setattr(store, "_migrate_legacy_peaks_locked", lambda: None)

    assert store.reload_data_key() is False
    assert store.decryptable is False
    assert store._data_key is None
