"""TranscriptStore — encrypted-at-rest history of dictations.

A SQLite index (``transcripts.db``) plus a ``recordings/`` directory under the
app data dir (``get_app_dir()``, honoring ``WHISPERSTREAM_DATA_DIR``). Transcript
text, enhancement provenance, waveform peaks, and audio bytes are encrypted
with AES-256-GCM via ``transcript_crypto`` (the data key is sealed at rest by
the selected platform secret provider). Metadata (timestamp, duration, model, and source)
stays plaintext so sort and pagination do not require decryption. Waveform
peaks and enhancement provenance each use a separate nonce/ciphertext pair so
the UI can decrypt them only while transcript content is available.

Threading: the SQLite connection is opened with ``check_same_thread=False`` and
one re-entrant lock serializes every connection operation, in-memory key
transition, and audio-file publication/removal lifecycle.

When the data key cannot be loaded (provider/account failure or a corrupt key
reference), ``decryptable`` is ``False``: ``save`` is a
no-op (returns ``None``) and ``list``/``get`` return the not-decryptable signal
instead of crashing, so the UI can surface a "can't open on this account"
banner.

# ============================================================================
# TRANSCRIPT DATA — DO NOT LOG
# MAKE SURE you do not log transcript data anywhere. If you add a new function
# that receives, returns, or passes transcript text, COPY this banner to it.
# Use `log_transcription_event()` from whisperstream.logger for metadata only
# (char count, word count, duration, stage name). Never pass `text`,
# `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
# Also never log key material (the data key or any nonce/ciphertext bytes).
# ============================================================================
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config_manager import get_app_dir
from .protected_secret_contract import ProtectedSecretStore
from .transcript_crypto import (
    DataKeyFailure,
    decrypt,
    decrypt_text,
    encrypt,
    encrypt_text,
    load_data_key,
    load_or_create_data_key_with_failure,
)

_log = logging.getLogger(__name__)

# How many characters of decrypted text feed the 2-line card snippet.
_SNIPPET_CHARS = 280

# Milliseconds in one day (retention windows are configured in days).
_DAY_MS = 86_400_000

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS recordings (
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
    enhancement_provenance BLOB,
    enhancement_provenance_nonce BLOB,
    source TEXT
)
"""

_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_recordings_created_at ON recordings(created_at)"

_PEAKS_CIPHERTEXT_COLUMN = "peaks_ciphertext"
_PEAKS_NONCE_COLUMN = "peaks_nonce"
_PROVENANCE_CIPHERTEXT_COLUMN = "enhancement_provenance"
_PROVENANCE_NONCE_COLUMN = "enhancement_provenance_nonce"
_PROVENANCE_VERSION = 1
_PROVENANCE_OUTCOMES = frozenset({"success", "timeout", "error"})
_MAX_PROFILE_ID_CHARS = 128
_MAX_PROFILE_NAME_CHARS = 256


class TranscriptIntegrityError(RuntimeError):
    """A requested encrypted history record failed authentication.

    The exception deliberately carries no record content, path, ciphertext, or
    key material. HTTP owners use only its type to return the typed
    ``not-decryptable`` state instead of misreporting a missing record.
    """


def history_write_available(store: Any | None) -> bool:
    """Authorize an encrypted history write only for a healthy existing store.

    History-disabled and no-store callers keep their established no-op behavior;
    this gate is for callers that have already decided to persist content. The
    cached corpus probe makes repeated writes cheap while still failing closed
    after any requested-read authentication failure or store mutation.
    """

    if store is None or not bool(getattr(store, "decryptable", False)):
        return False
    try:
        probe = getattr(store, "probe_content_integrity", None)
        return not callable(probe) or bool(probe())
    except Exception:
        return False


def _encrypted_history_exists_without_key(db_path: Path) -> bool:
    """Fail closed only when a keyless database already contains history.

    An unavailable provider can leave behind a newly initialized empty DB.  A
    later restart may safely create its first key for that empty store.  If any
    row already exists, however, minting a replacement key would turn a missing
    protected reference into misleading, undecryptable records.
    """

    if not db_path.exists():
        return False
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recordings'"
            ).fetchone()
            if table is None:
                return False
            row = connection.execute(
                "SELECT 1 FROM recordings LIMIT 1"
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        # A malformed or inaccessible existing index is not a fresh-store
        # signal. Keep key creation closed and let store initialization surface
        # the storage failure through its existing path.
        return True


class TranscriptStore:
    """Thread-safe, file-backed, encrypted store of dictation history."""

    def __init__(
        self,
        *,
        protected_secret_store: ProtectedSecretStore | None = None,
    ) -> None:
        app_dir = get_app_dir()
        self._db_path = app_dir / "transcripts.db"
        self._recordings_dir = app_dir / "recordings"
        self._data_key_path = app_dir / "transcript.key"
        self._protected_secret_store = protected_secret_store
        # One re-entrant lock owns the connection, the in-memory key lifetime,
        # and file-write -> row-commit publication. This prevents Lockdown from
        # clearing the key mid-encryption and prevents an orphan sweep from
        # observing a final audio filename before its row commits.
        self._lock = threading.RLock()
        # The platform availability adapter authenticates the whole persisted
        # corpus once per store revision. Repeated list refreshes reuse that
        # content-free result, while requested-page/detail/audio reads still
        # authenticate their exact ciphertext and raise a typed failure.
        self._integrity_revision = 0
        self._integrity_cache_revision = -1
        self._integrity_cache_result = False

        # Key sourcing depends on whether lockdown is active (F2):
        #   * NON-LOCKDOWN (no envelope): load (or create on first use) the
        #     DPAPI-sealed transcript.key exactly as before — path UNCHANGED.
        #   * LOCKDOWN (envelope present): the data key exists ONLY in memory
        #     after a successful unlock. Do NOT load or create a DPAPI-only key
        #     here — that would restore a key DPAPI alone can unwrap (the F2
        #     hole) and, since enable deletes transcript.key, would also mint a
        #     brand-new key that mismatches the envelope. Start with NO key
        #     (LOCKED); the unlock flow hands the key in via set_data_key() and
        #     lock/idle clears it via clear_data_key().
        # Local import avoids any module-load ordering coupling with the keystore.
        from . import lockdown_keystore

        self._lockdown = lockdown_keystore.is_lockdown_enabled(app_dir)
        # Why a non-Lockdown key is unavailable. MISSING means only a recovery
        # envelope or Start fresh can bring history back; saving stays paused.
        self.key_failure: DataKeyFailure | None = None
        if self._lockdown:
            self._data_key = None
        else:
            if (
                not self._data_key_path.exists()
                and _encrypted_history_exists_without_key(self._db_path)
            ):
                self._data_key = None
                self.key_failure = DataKeyFailure.MISSING
            else:
                self._data_key, self.key_failure = load_or_create_data_key_with_failure(
                    self._data_key_path,
                    store=self._protected_secret_store,
                )
        self.decryptable = self._data_key is not None

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL allows concurrent readers while a writer holds the lock.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_TABLE)
        self._conn.execute(_CREATE_INDEX)
        self._ensure_encrypted_peaks_schema()
        self._ensure_encrypted_provenance_schema()
        self._conn.commit()
        migration_result = self.migrate_legacy_peaks()
        if self._data_key is not None and migration_result is None:
            # Outside Lockdown there is no later unlock seam to retry through.
            # Fail history initialization closed instead of keeping legacy
            # fingerprinting metadata plaintext and readable indefinitely.
            self.close()
            raise RuntimeError("transcript peaks migration unavailable")

    # ── lockdown key lifecycle (F2) ───────────────────────────────────────── #
    # Under lockdown the data key lives ONLY in memory. The server hands it in
    # on a successful unlock and clears it on lock/idle; on disable it re-points
    # at the re-sealed DPAPI key. NEVER log the data key in any of these.

    def set_data_key(self, data_key: bytes) -> bool:
        """Install the in-memory data key from a successful lockdown unlock.

        Transitions the store to decryptable. DO NOT LOG ``data_key``.
        """
        # DO NOT LOG: data_key is key material.
        with self._lock:
            self._invalidate_integrity_cache_locked()
            self._data_key = data_key
            self.decryptable = data_key is not None
            self.key_failure = None
            # A store constructed while Lockdown is locked can add the schema
            # columns but cannot encrypt legacy plaintext peaks. Retry that
            # transactional migration every time an unlock supplies the key.
            migration_result = self._migrate_legacy_peaks_locked()
            if migration_result is None:
                self._data_key = None
                self.decryptable = False
                return False
            return True

    def clear_data_key(self) -> None:
        """Drop the in-memory data key on lock / idle / shutdown (lockdown).

        After this, reads decrypt nothing and ``save`` no-ops until the next
        unlock re-installs the key. DO NOT LOG key material.
        """
        # DO NOT LOG: _data_key is key material.
        with self._lock:
            self._invalidate_integrity_cache_locked()
            self._data_key = None
            self.decryptable = False

    def reload_data_key(self) -> bool:
        """Re-point at the protected key after Lockdown disable, reset or recovery.

        Disable and recovery re-materialize ``transcript.key`` first. Reset
        wipes history and removes the reference, so a first key may be minted
        only when no reference exists AND no encrypted rows remain, the same
        rule as construction. An existing reference is load-only and never
        replaced. DO NOT LOG key material.
        """
        # Load outside the critical section (DPAPI can be slow), then publish
        # and migrate atomically with respect to store readers/writers.
        if not self._data_key_path.exists() and _encrypted_history_exists_without_key(
            self._db_path
        ):
            data_key, failure = None, DataKeyFailure.MISSING
        elif self._data_key_path.exists():
            data_key, failure = load_data_key(
                self._data_key_path,
                store=self._protected_secret_store,
            )
        else:
            data_key, failure = load_or_create_data_key_with_failure(
                self._data_key_path,
                store=self._protected_secret_store,
            )
        with self._lock:
            self._invalidate_integrity_cache_locked()
            self._lockdown = False
            self.key_failure = failure
            self._data_key = data_key
            self.decryptable = data_key is not None
            migration_result = self._migrate_legacy_peaks_locked()
            if data_key is None or migration_result is None:
                self._data_key = None
                self.decryptable = False
                return False
            return True

    # ── internal helpers ──────────────────────────────────────────────────── #

    def _audio_rel(self, rid: str) -> str:
        """Audio path RELATIVE to the data dir (per scope decision 2)."""
        return f"recordings/{rid}.wav"

    def _invalidate_integrity_cache_locked(self) -> None:
        """Invalidate the corpus probe while the store lock is held."""

        self._integrity_revision += 1
        self._integrity_cache_revision = -1

    def _advance_integrity_cache_after_trusted_mutation_locked(self) -> None:
        """Advance a corpus revision after an authenticated app-owned mutation.

        A successful mutation writes or removes only data protected by this
        store's live key, so a verified-TRUE corpus remains verified without a
        full rescan. Unknown state remains unknown, and a pinned authentication
        failure remains false; a mutation must never turn either into success.
        """

        cached_for_current_revision = (
            self._integrity_cache_revision == self._integrity_revision
        )
        cached_result = self._integrity_cache_result
        self._integrity_revision += 1
        if cached_for_current_revision:
            self._integrity_cache_revision = self._integrity_revision
            self._integrity_cache_result = cached_result
        else:
            self._integrity_cache_revision = -1
            self._integrity_cache_result = False

    def _record_integrity_failure_locked(self) -> None:
        """Remember a content authentication failure for this store revision."""

        self._integrity_cache_revision = self._integrity_revision
        self._integrity_cache_result = False

    def _raise_integrity_failure_locked(self) -> None:
        self._record_integrity_failure_locked()
        raise TranscriptIntegrityError("encrypted transcript integrity unavailable")

    def _audio_path_for(self, rid: str) -> str:
        """Test/seam helper: the relative audio path for a record id."""
        return self._audio_rel(rid)

    def _abs_audio(self, rel: str) -> Path:
        return get_app_dir() / rel

    def _snippet(self, raw: str | None, enhanced: str | None) -> str:
        """A short snippet for the card, preferring enhanced text."""
        text = (enhanced if enhanced else raw) or ""
        text = " ".join(text.split())  # collapse whitespace for a clean 2-line clamp
        return text[:_SNIPPET_CHARS]

    def _ensure_encrypted_peaks_schema(self) -> None:
        """Add encrypted-peak columns without invalidating legacy databases."""
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(recordings)").fetchall()
        }
        if _PEAKS_CIPHERTEXT_COLUMN not in columns:
            self._conn.execute(
                f"ALTER TABLE recordings ADD COLUMN {_PEAKS_CIPHERTEXT_COLUMN} BLOB"
            )
        if _PEAKS_NONCE_COLUMN not in columns:
            self._conn.execute(
                f"ALTER TABLE recordings ADD COLUMN {_PEAKS_NONCE_COLUMN} BLOB"
            )

    def _ensure_encrypted_provenance_schema(self) -> None:
        """Add nullable provenance columns without needing the content key.

        This is intentionally separate from content migrations such as legacy
        waveform peaks. A locked Lockdown store can safely update the schema
        because no existing plaintext needs to be read or rewritten.
        """
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(recordings)").fetchall()
        }
        if _PROVENANCE_CIPHERTEXT_COLUMN not in columns:
            self._conn.execute(
                f"ALTER TABLE recordings ADD COLUMN "
                f"{_PROVENANCE_CIPHERTEXT_COLUMN} BLOB"
            )
        if _PROVENANCE_NONCE_COLUMN not in columns:
            self._conn.execute(
                f"ALTER TABLE recordings ADD COLUMN {_PROVENANCE_NONCE_COLUMN} BLOB"
            )

    @staticmethod
    def _validated_provenance(
        provenance: Any,
        *,
        has_enhanced: bool,
        stored_version: int | None = None,
    ) -> dict[str, str] | None:
        """Return the bounded public snapshot or fail closed to ``None``.

        The live pipeline passes an immutable object with attributes. Decrypted
        rows pass a JSON mapping plus ``stored_version``. Keeping validation at
        this boundary prevents malformed, future, or contradictory provenance
        from changing how readable transcript text is presented.
        """
        if provenance is None:
            return None
        if stored_version is not None:
            if (
                type(stored_version) is not int
                or stored_version != _PROVENANCE_VERSION
                or not isinstance(provenance, dict)
            ):
                return None
            if set(provenance) != {"profile_id", "profile_name", "outcome"}:
                return None
            profile_id = provenance.get("profile_id")
            profile_name = provenance.get("profile_name")
            outcome = provenance.get("outcome")
        else:
            try:
                profile_id = provenance.profile_id
                profile_name = provenance.profile_name
                outcome = provenance.outcome
            except Exception:
                return None

        if not isinstance(profile_id, str) or not profile_id.strip():
            return None
        if not isinstance(profile_name, str) or not profile_name.strip():
            return None
        if len(profile_id) > _MAX_PROFILE_ID_CHARS:
            return None
        if len(profile_name) > _MAX_PROFILE_NAME_CHARS:
            return None
        try:
            profile_id.encode("utf-8")
            profile_name.encode("utf-8")
        except UnicodeEncodeError:
            return None
        if not isinstance(outcome, str) or outcome not in _PROVENANCE_OUTCOMES:
            return None
        if outcome == "success" and not has_enhanced:
            return None
        return {
            "profile_id": profile_id,
            "profile_name": profile_name,
            "outcome": outcome,
        }

    def _decrypt_provenance(
        self,
        ciphertext: bytes | None,
        nonce: bytes | None,
        *,
        has_enhanced: bool,
    ) -> dict[str, str] | None:
        """Decrypt and validate one versioned provenance snapshot."""
        data_key = self._data_key
        if ciphertext is None or nonce is None or data_key is None:
            return None
        plaintext = decrypt(data_key, nonce, ciphertext)
        if plaintext is None:
            return None
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "profile_id",
            "profile_name",
            "outcome",
        }:
            return None
        version = payload.pop("v", None)
        return self._validated_provenance(
            payload,
            has_enhanced=has_enhanced,
            stored_version=version,
        )

    def migrate_legacy_peaks(self) -> int | None:
        """Encrypt legacy plaintext peak JSON transactionally when a key exists.

        A keyless Lockdown startup deliberately leaves legacy values untouched.
        The next successful unlock calls this method again. Any encryption or
        SQLite failure rolls back the complete batch, leaving every row eligible
        for a later retry instead of creating a partially migrated database.
        """
        with self._lock:
            return self._migrate_legacy_peaks_locked()

    def _migrate_legacy_peaks_locked(self) -> int | None:
        data_key = self._data_key
        if data_key is None:
            return 0
        rows = self._conn.execute(
            "SELECT id, peaks FROM recordings WHERE peaks IS NOT NULL"
        ).fetchall()
        if not rows:
            return 0
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                nonce, ciphertext = encrypt(
                    data_key, str(row["peaks"]).encode("utf-8")
                )
                self._conn.execute(
                    "UPDATE recordings SET peaks = NULL, peaks_ciphertext = ?, "
                    "peaks_nonce = ? WHERE id = ?",
                    (ciphertext, nonce, row["id"]),
                )
            self._conn.commit()
            # Legacy plaintext peaks are not authenticated input, so migration
            # cannot carry a prior TRUE corpus result across revisions.
            self._invalidate_integrity_cache_locked()
        except Exception as exc:
            self._conn.rollback()
            _log.warning(
                "transcript peaks migration status=retryable error_type=%s",
                type(exc).__name__,
            )
            return None
        _log.info("transcript peaks migration migrated=%d", len(rows))
        return len(rows)

    # ── write API ─────────────────────────────────────────────────────────── #

    def save(
        self,
        *,
        raw_text: str,
        enhanced_text: str | None,
        duration_ms: int,
        model: str,
        source: str,
        audio_bytes: bytes | None = None,
        sample_rate: int = 16000,  # noqa: ARG002 - WAV bytes already at this rate; kept for API symmetry
        peaks: list[float] | None = None,
        created_at: int | None = None,
        enhancement_provenance: Any | None = None,
    ) -> str | None:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        rid = uuid.uuid4().hex
        ts = int(created_at) if created_at is not None else int(time.time() * 1000)
        with self._lock:
            data_key = self._data_key
            if not self.decryptable or data_key is None:
                # Cannot encrypt without the data key: persist nothing rather
                # than writing recoverable plaintext or corrupt rows.
                return None

            raw_nonce, raw_ct = encrypt_text(data_key, raw_text)
            if enhanced_text is not None:
                enh_nonce, enh_ct = encrypt_text(data_key, enhanced_text)
            else:
                enh_nonce, enh_ct = None, None

            audio_rel: str | None = None
            audio_nonce: bytes | None = None
            if audio_bytes is not None:
                audio_nonce, audio_ct = encrypt(data_key, audio_bytes)
                audio_rel = self._audio_rel(rid)
                try:
                    self._recordings_dir.mkdir(parents=True, exist_ok=True)
                    self._abs_audio(audio_rel).write_bytes(audio_ct)
                except Exception as exc:
                    # Audio write failed; persist the text-only row rather than
                    # losing the transcript. Never log transcript content.
                    _log.warning(
                        "transcript audio write failed error_type=%s",
                        type(exc).__name__,
                    )
                    audio_rel = None
                    audio_nonce = None

            peaks_nonce: bytes | None = None
            peaks_ct: bytes | None = None
            if peaks is not None:
                peaks_json = json.dumps(peaks, separators=(",", ":"))
                peaks_nonce, peaks_ct = encrypt(
                    data_key, peaks_json.encode("utf-8")
                )

            provenance_nonce: bytes | None = None
            provenance_ct: bytes | None = None
            normalized_provenance = self._validated_provenance(
                enhancement_provenance,
                has_enhanced=enhanced_text is not None,
            )
            if normalized_provenance is not None:
                provenance_json = json.dumps(
                    {"v": _PROVENANCE_VERSION, **normalized_provenance},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                provenance_nonce, provenance_ct = encrypt(
                    data_key, provenance_json.encode("utf-8")
                )

            try:
                self._conn.execute(
                    "INSERT INTO recordings (id, created_at, duration_ms, model, "
                    "raw_text, raw_nonce, enhanced_text, enhanced_nonce, "
                    "audio_path, audio_nonce, peaks, peaks_ciphertext, peaks_nonce, "
                    "enhancement_provenance, enhancement_provenance_nonce, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                    (
                        rid,
                        ts,
                        int(duration_ms),
                        model,
                        raw_ct,
                        raw_nonce,
                        enh_ct,
                        enh_nonce,
                        audio_rel,
                        audio_nonce,
                        peaks_ct,
                        peaks_nonce,
                        provenance_ct,
                        provenance_nonce,
                        source,
                    ),
                )
                self._conn.commit()
                self._advance_integrity_cache_after_trusted_mutation_locked()
            except Exception:
                # Roll back before unlink while still holding the same lock.
                # This prevents a delayed commit from publishing a row that
                # points at audio already removed by failure cleanup.
                self._conn.rollback()
                if audio_rel is not None:
                    self._unlink_audio(audio_rel, reason="insert_failed")
                raise
        return rid

    # ── read API ──────────────────────────────────────────────────────────── #

    def list(
        self,
        *,
        before: int | None = None,
        limit: int = 20,
        q: str | None = None,
    ) -> dict:
        with self._lock:
            return self._list_locked(before=before, limit=limit, q=q)

    def has_records(self) -> bool:
        """Return content-free persisted-row presence without decrypting a row."""

        with self._lock:
            return (
                self._conn.execute("SELECT 1 FROM recordings LIMIT 1").fetchone()
                is not None
            )

    def record_count(self) -> int:
        """Return the persisted row count without decrypting anything."""

        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0])

    def has_audio(self) -> bool:
        """Return whether any saved row references audio, without decrypting."""

        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM recordings WHERE audio_path IS NOT NULL LIMIT 1"
                ).fetchone()
                is not None
            )

    def key_identity(self, data_key: bytes) -> tuple[int, int]:
        """Return (rows whose text *data_key* authenticates, total rows).

        AES-GCM authentication proves which key encrypted a row, so this tells
        a wrong key (opens none) apart from damaged content (opens most), which
        whole-corpus integrity can't. DO NOT LOG key or content.
        """
        # TRANSCRIPT DATA / KEY MATERIAL — DO NOT LOG.
        with self._lock:
            rows = self._conn.execute(
                "SELECT raw_text, raw_nonce FROM recordings"
            ).fetchall()
        opened = 0
        for row in rows:
            if row["raw_text"] is None or row["raw_nonce"] is None:
                continue
            if decrypt_text(data_key, row["raw_nonce"], row["raw_text"]) is not None:
                opened += 1
        return opened, len(rows)

    def key_opens_history(self, data_key: bytes) -> bool:
        """Return whether *data_key* is the key of saved history (True when empty).

        Restore uses this so a recovery envelope for another key can never be
        written back over history it cannot read. One authenticated row proves
        the key; a damaged row elsewhere doesn't disqualify it.
        DO NOT LOG key or content.
        """
        opened, total = self.key_identity(data_key)
        return total == 0 or opened > 0

    def data_key_for_recovery(self) -> bytes | None:
        """Return the in-memory data key so a recovery envelope can wrap it.

        Only the recovery routes may call this. DO NOT LOG the result.
        """
        # DO NOT LOG: key material.
        with self._lock:
            return self._data_key if self.decryptable else None

    def _list_locked(
        self,
        *,
        before: int | None = None,
        limit: int = 20,
        q: str | None = None,
    ) -> dict:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if not self.decryptable or self._data_key is None:
            return {"items": [], "next_cursor": None, "decryptable": False}

        limit = max(1, int(limit))
        query = q.strip().lower() if q else None

        # Newest-first; fetch one extra to decide next_cursor. When searching,
        # we must scan + decrypt page-by-page until we collect `limit` matches.
        params: list[Any] = []
        where = ""
        if before is not None:
            where = "WHERE created_at < ?"
            params.append(int(before))

        items: list[dict] = []
        next_cursor: int | None = None

        if query is None:
            rows = self._conn.execute(
                "SELECT id, created_at, duration_ms, raw_text, raw_nonce, "
                "enhanced_text, enhanced_nonce, audio_path, "
                "enhancement_provenance, enhancement_provenance_nonce "
                f"FROM recordings {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
            page = rows[:limit]
            if len(rows) > limit:
                next_cursor = page[-1]["created_at"]
            for row in page:
                items.append(self._to_list_item(row))
        else:
            # Substring search over decrypted enhanced-or-raw text. Scan in
            # created_at-descending batches; cursor still tracks created_at.
            cursor = before
            while len(items) < limit:
                batch_where = "WHERE created_at < ?" if cursor is not None else ""
                batch_params: tuple = (int(cursor),) if cursor is not None else ()
                rows = self._conn.execute(
                    "SELECT id, created_at, duration_ms, raw_text, raw_nonce, "
                    "enhanced_text, enhanced_nonce, audio_path, "
                    "enhancement_provenance, enhancement_provenance_nonce "
                    f"FROM recordings {batch_where} ORDER BY created_at DESC LIMIT ?",
                    (*batch_params, limit),
                ).fetchall()
                if not rows:
                    break
                cursor = rows[-1]["created_at"]
                for row in rows:
                    raw = self._dec(row["raw_text"], row["raw_nonce"])
                    if raw is None:
                        self._raise_integrity_failure_locked()
                    enh = self._dec(row["enhanced_text"], row["enhanced_nonce"])
                    if row["enhanced_text"] is not None and enh is None:
                        self._raise_integrity_failure_locked()
                    haystack = (enh or "") + "\n" + (raw or "")
                    if query in haystack.lower():
                        items.append(self._to_list_item(row, raw=raw, enh=enh))
                        if len(items) == limit:
                            next_cursor = row["created_at"]
                            break
                if len(rows) < limit:
                    break
            # If we filled exactly `limit` but there may be more matching rows,
            # next_cursor was set above. Otherwise leave it None.
            if len(items) < limit:
                next_cursor = None

        return {"items": items, "next_cursor": next_cursor}

    def _to_list_item(self, row: sqlite3.Row, raw: str | None = None, enh: str | None = None) -> dict:
        if raw is None:
            raw = self._dec(row["raw_text"], row["raw_nonce"])
        if raw is None:
            self._raise_integrity_failure_locked()
        if enh is None and row["enhanced_text"] is not None:
            enh = self._dec(row["enhanced_text"], row["enhanced_nonce"])
            if enh is None:
                self._raise_integrity_failure_locked()
        provenance = self._decrypt_provenance(
            row["enhancement_provenance"],
            row["enhancement_provenance_nonce"],
            has_enhanced=enh is not None,
        )
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "duration_ms": row["duration_ms"],
            "has_audio": row["audio_path"] is not None,
            "has_enhanced": bool(enh),
            "enhancement_provenance": provenance,
            "snippet": self._snippet(raw, enh),
        }

    def get(self, id: str) -> dict | None:  # noqa: A002 - matches the documented API
        with self._lock:
            return self._get_locked(id)

    def _get_locked(self, id: str) -> dict | None:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if not self.decryptable or self._data_key is None:
            return None
        row = self._conn.execute(
            "SELECT id, created_at, duration_ms, model, raw_text, raw_nonce, "
            "enhanced_text, enhanced_nonce, audio_path, peaks, "
            "peaks_ciphertext, peaks_nonce, enhancement_provenance, "
            "enhancement_provenance_nonce, source FROM recordings WHERE id = ?",
            (id,),
        ).fetchone()
        if row is None:
            return None
        raw = self._dec(row["raw_text"], row["raw_nonce"])
        if raw is None:
            self._raise_integrity_failure_locked()
        enh = self._dec(row["enhanced_text"], row["enhanced_nonce"]) if row["enhanced_text"] is not None else None
        if row["enhanced_text"] is not None and enh is None:
            self._raise_integrity_failure_locked()
        peaks = self._decrypt_peaks(row)
        provenance = self._decrypt_provenance(
            row["enhancement_provenance"],
            row["enhancement_provenance_nonce"],
            has_enhanced=enh is not None,
        )
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "duration_ms": row["duration_ms"],
            "model": row["model"],
            "source": row["source"],
            "raw_text": raw,
            "enhanced_text": enh,
            "enhancement_provenance": provenance,
            "peaks": peaks,
            "has_audio": row["audio_path"] is not None,
        }

    def _decrypt_peaks(self, row: sqlite3.Row) -> list[float] | None:
        ciphertext = row["peaks_ciphertext"]
        nonce = row["peaks_nonce"]
        if ciphertext is not None and nonce is not None and self._data_key is not None:
            plaintext = decrypt(self._data_key, nonce, ciphertext)
            if plaintext is None:
                self._raise_integrity_failure_locked()
            try:
                return json.loads(plaintext.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._raise_integrity_failure_locked()
        # Compatibility for a keyless-start migration that has not run yet, or
        # for a migration failure that will be retried at the next unlock.
        try:
            return json.loads(row["peaks"]) if row["peaks"] else None
        except json.JSONDecodeError:
            self._raise_integrity_failure_locked()

    def get_audio_bytes(self, id: str) -> bytes | None:  # noqa: A002 - matches the documented API
        with self._lock:
            return self._get_audio_bytes_locked(id)

    def _get_audio_bytes_locked(self, id: str) -> bytes | None:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if not self.decryptable or self._data_key is None:
            return None
        row = self._conn.execute(
            "SELECT audio_path, audio_nonce FROM recordings WHERE id = ?",
            (id,),
        ).fetchone()
        if row is None or row["audio_path"] is None or row["audio_nonce"] is None:
            return None
        abs_path = self._abs_audio(row["audio_path"])
        try:
            ct = abs_path.read_bytes()
        except Exception:
            self._raise_integrity_failure_locked()
        plaintext = decrypt(self._data_key, row["audio_nonce"], ct)
        if plaintext is None:
            self._raise_integrity_failure_locked()
        return plaintext

    def _dec(self, ct: bytes | None, nonce: bytes | None) -> str | None:
        if ct is None or nonce is None or self._data_key is None:
            return None
        return decrypt_text(self._data_key, nonce, ct)

    def probe_content_integrity(self) -> bool:
        """Authenticate core text, peaks and referenced audio without disclosure.

        This is a fail-closed availability probe for the D-047 platform owner.
        It performs no migration or mutation and returns only a Boolean. Missing
        optional fields remain valid; an unreadable referenced audio file or any
        core AES-GCM/UTF-8/JSON authentication failure returns ``False``.
        Optional provenance is excluded: invalid metadata intentionally degrades
        to null independently of readable content, as documented by Transcripts.
        """
        with self._lock:
            if self._integrity_cache_revision == self._integrity_revision:
                return self._integrity_cache_result
            if not self.decryptable or self._data_key is None:
                self._record_integrity_failure_locked()
                return False
            result = True
            try:
                rows = self._conn.execute(
                    "SELECT raw_text, raw_nonce, enhanced_text, enhanced_nonce, "
                    "audio_path, audio_nonce, peaks_ciphertext, peaks_nonce "
                    "FROM recordings"
                ).fetchall()
                for row in rows:
                    if self._dec(row["raw_text"], row["raw_nonce"]) is None:
                        result = False
                        break
                    if row["enhanced_text"] is not None and self._dec(
                        row["enhanced_text"], row["enhanced_nonce"]
                    ) is None:
                        result = False
                        break
                    if row["peaks_ciphertext"] is not None:
                        raw_peaks = decrypt(
                            self._data_key,
                            row["peaks_nonce"],
                            row["peaks_ciphertext"],
                        )
                        if raw_peaks is None:
                            result = False
                            break
                        try:
                            decoded_peaks = json.loads(raw_peaks.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            result = False
                            break
                        if not isinstance(decoded_peaks, list):
                            result = False
                            break
                    if row["audio_path"] is not None:
                        ciphertext = self._abs_audio(row["audio_path"]).read_bytes()
                        if decrypt(
                            self._data_key,
                            row["audio_nonce"],
                            ciphertext,
                        ) is None:
                            result = False
                            break
            except (OSError, sqlite3.Error, TypeError, ValueError):
                result = False
            self._integrity_cache_revision = self._integrity_revision
            self._integrity_cache_result = result
            return result

    # ── delete / clear / retention ─────────────────────────────────────────── #

    def delete(self, id: str) -> bool:  # noqa: A002 - matches the documented API
        with self._lock:
            row = self._conn.execute(
                "SELECT audio_path FROM recordings WHERE id = ?", (id,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM recordings WHERE id = ?", (id,))
            self._conn.commit()
            self._advance_integrity_cache_after_trusted_mutation_locked()
            self._unlink_audio(row["audio_path"], reason="row_deleted")
        return True

    def clear(self) -> int:
        """Ordinarily delete all rows and unlink app-owned audio. Returns the row count.

        SQLite DELETE and filesystem unlink are logical removal, not physical
        overwrite or a secure-erasure guarantee.
        """
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
            self._conn.execute("DELETE FROM recordings")
            self._conn.commit()
            self._advance_integrity_cache_after_trusted_mutation_locked()
            # No row references survive, so ordinarily unlink every app-owned
            # audio file. A failed unlink remains an orphan for a later sweep.
            if self._recordings_dir.exists():
                for f in self._recordings_dir.glob("*.wav"):
                    try:
                        f.unlink()
                    except Exception as exc:
                        _log.warning(
                            "transcript audio unlink status=retryable reason=clear "
                            "error_type=%s",
                            type(exc).__name__,
                        )
        return int(count)

    def wipe_and_rekey(self, remove_old_keys: Callable[[], None]) -> bool:
        """Start fresh: delete every row and swap in a new key as one step.

        The old key is dropped before anything is deleted and the store lock is
        held throughout, so a concurrent save either finishes before the wipe
        or waits and encrypts under the new key. A save can never land under
        a key whose protected copy and recovery envelope are already gone.
        *remove_old_keys* deletes those copies; a failure propagates and leaves
        the store without a key. DO NOT LOG key material.
        """
        with self._lock:
            self._invalidate_integrity_cache_locked()
            self._data_key = None
            self.decryptable = False
            self.clear()
            remove_old_keys()
            self._lockdown = False
            data_key, failure = load_or_create_data_key_with_failure(
                self._data_key_path,
                store=self._protected_secret_store,
            )
            self.key_failure = failure
            self._data_key = data_key
            self.decryptable = data_key is not None
            if data_key is None or self._migrate_legacy_peaks_locked() is None:
                self._data_key = None
                self.decryptable = False
                return False
            return True

    def purge_older_than(self, cutoff_ms: int) -> int:
        """Delete rows with created_at < cutoff + their audio files. Returns count."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, audio_path FROM recordings WHERE created_at < ?",
                (int(cutoff_ms),),
            ).fetchall()
            if not rows:
                return 0
            self._conn.execute("DELETE FROM recordings WHERE created_at < ?", (int(cutoff_ms),))
            self._conn.commit()
            self._advance_integrity_cache_after_trusted_mutation_locked()
            for row in rows:
                self._unlink_audio(row["audio_path"], reason="retention")
        return len(rows)

    def sweep_orphans(self) -> int:
        """Delete files in recordings/ not referenced by any row. Returns count."""
        if not self._recordings_dir.exists():
            return 0
        with self._lock:
            referenced = {
                Path(r["audio_path"]).name
                for r in self._conn.execute(
                    "SELECT audio_path FROM recordings WHERE audio_path IS NOT NULL"
                ).fetchall()
            }
            removed = 0
            for f in self._recordings_dir.glob("*.wav"):
                if f.name not in referenced:
                    try:
                        f.unlink()
                        removed += 1
                    except Exception as exc:
                        _log.warning(
                            "transcript orphan cleanup status=retryable error_type=%s",
                            type(exc).__name__,
                        )
        return removed

    def _unlink_audio(self, audio_path: str | None, *, reason: str) -> bool:
        if not audio_path:
            return True
        try:
            self._abs_audio(audio_path).unlink()
            return True
        except FileNotFoundError:
            return True
        except Exception as exc:
            _log.warning(
                "transcript audio unlink status=retryable reason=%s error_type=%s",
                reason,
                type(exc).__name__,
            )
            return False

    def close(self) -> None:
        """Release the connection and in-memory content key idempotently."""
        with self._lock:
            self._data_key = None
            self.decryptable = False
            conn = getattr(self, "_conn", None)
            self._conn = None
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def run_retention_sweep(
    config: Any,
    store: "TranscriptStore | None",
    *,
    raise_on_error: bool = False,
) -> int:
    """Purge transcripts past the retention window, then sweep orphan audio.

    Reused by startup, microphone/import saves, successful Lockdown unlock, and
    the retention-change endpoint to keep the store bounded without a timer.

    Behavior:
      * When ``config.transcript_retention_days > 0``: compute
        ``cutoff_ms = now_ms - days * 86_400_000`` and ``purge_older_than(cutoff)``.
      * When ``transcript_retention_days == 0`` (Forever): SKIP the age purge.
      * In BOTH cases, ALWAYS ``sweep_orphans()`` afterward.

    A ``None`` store (init failed) or any sweep error is swallowed so callers can
    invoke this freely on startup/after-save without guarding. Returns the number
    of rows purged by the age sweep (0 when Forever, disabled, or on error).
    """
    if store is None:
        return 0
    # When the data key is unavailable the store is read-only/no-op; skip.
    if not getattr(store, "decryptable", True):
        return 0

    purged = 0
    try:
        days = int(getattr(config, "transcript_retention_days", 0) or 0)
        if days > 0:
            cutoff_ms = int(time.time() * 1000) - days * _DAY_MS
            purged = store.purge_older_than(cutoff_ms)
        # Orphan sweep runs regardless of the retention window (Forever included).
        orphans = store.sweep_orphans()
    except Exception as exc:
        # Metadata only — never log transcript text or key material.
        _log.warning("retention sweep failed reason=%s", type(exc).__name__)
        if raise_on_error:
            raise
        return 0

    # Metadata-only count (no transcript text).
    if purged or orphans:
        _log.info("retention sweep purged=%d orphans=%d", purged, orphans)
    return purged
