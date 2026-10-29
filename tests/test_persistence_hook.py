"""Tests for the mic-path persistence hook (Task 2.2).

`_enhance_then_inject` (single-call path) and `_enhance_then_inject_chunked`
(chunked path) persist each completed dictation to the encrypted TranscriptStore
AFTER the result is broadcast and injected, so saving never adds paste latency.

These tests construct a `WhisperstreamApp` WITHOUT its heavy __init__ (via
__new__, mirroring tests/test_inject_suppression.py), wire a stubbed injector +
enhancer + a real TranscriptStore rooted at a temp data dir, hand the hook a
fake `_last_waveform`, then assert the store ended up with exactly the expected
record. No real ASR model is needed.

The store loads its AES data key through `transcript_crypto.load_or_create_data_key`
(sealed via the DPAPI wrapper); on a Windows dev box that round-trips, so the
decrypt-on-read assertions exercise the real crypto path.
"""
from __future__ import annotations

import logging
import concurrent.futures
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from whisperstream.main import WhisperstreamApp
from whisperstream.llm_enhancer import EnhancementResult
from whisperstream.text_injector import DeliveryOutcome


pytestmark = pytest.mark.usefixtures("accepted_eula")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _SyncExecutor:
    """Drop-in for ThreadPoolExecutor whose submit() runs synchronously.

    Lets the test assert on `transcript_store.list()` immediately after the
    hook returns, without racing a background thread.
    """

    def submit(self, fn, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        fn(*args, **kwargs)
        return None


class _QueuedExecutor:
    def __init__(self):
        self.callbacks = []

    def submit(self, fn, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        self.callbacks.append(lambda: fn(*args, **kwargs))
        return concurrent.futures.Future()

    def run_next(self):
        self.callbacks.pop(0)()


class _TransitionExecutor:
    def __init__(self):
        self.submissions = []
        self.sentinel_submitted = threading.Event()
        self.sentinel = concurrent.futures.Future()

    def submit(self, fn, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        self.submissions.append((fn, args, kwargs))
        self.sentinel_submitted.set()
        return self.sentinel


def _make_app(store, *, history_enabled=True, save_audio=True, enhancement_enabled=False):
    """A WhisperstreamApp stub wired with just what the persist hooks touch."""
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app.audio_feedback = MagicMock()
    app.stt_engine = MagicMock()
    app.text_injector = MagicMock()
    # process() echoes its first arg so the broadcast text is deterministic.
    app.text_injector.process.side_effect = lambda text, *a, **k: text
    app.text_injector.last_transcript = None
    app.config = SimpleNamespace(
        language="en",
        error_sound_path=None,
        enhancement_enabled=enhancement_enabled,
        enhancement_provider="local",
        enhancement_local_endpoint="",
        # The consent gate reads the provider chokepoint, not the stored field.
        effective_provider=lambda: "local",
        enhancement_chunked_cleanup=True,
        parakeet_model="nemo-parakeet-tdt-0.6b-v3",
        history_enabled=history_enabled,
        mac_history_opt_in=history_enabled,
        save_audio=save_audio,
        transcript_retention_days=7,
        # Power Mode (round 04): non-empty default so the no-default raw gate
        # does NOT trip and enhancement still runs for these persistence tests.
        default_profile_id="chat",
        power_mode_map={},
        site_map={},
        profiles=[{"id": "chat", "name": "Chat", "prompt": "x"}],
        # keystroke-input (Task 1.5): effective-style resolution reads these.
        input_style="clipboard",
    )
    app._broadcast = MagicMock()
    app._inject_suppressed = False
    app._executor = _SyncExecutor()
    app._persistence_executor = _SyncExecutor()
    app.review_state = MagicMock()
    app.transcript_store = store
    app._last_waveform = None
    # Round-04 app state the merged inject path reads (issue 14 override +
    # #159 app-identity + #184 trial clock); __new__ skips __init__ so wire them.
    app._dictation_app = None
    app._manual_profile_id = None
    app._override_anchor = None
    app.license_manager = MagicMock()
    return app


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A fresh TranscriptStore rooted at a temp data dir (no real app dir touch)."""
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    from whisperstream.transcript_store import TranscriptStore

    s = TranscriptStore()
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# Single-call path
# --------------------------------------------------------------------------- #


def test_single_path_persists_with_audio(store):
    app = _make_app(store, save_audio=True)
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    app._enhance_then_inject("hello world", 1.5)

    result = store.list()
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert "hello world" in item["snippet"]
    assert item["has_audio"] is True
    assert item["duration_ms"] == 1500

    full = store.get(item["id"])
    assert full["raw_text"] == "hello world"
    # No enhancement ran → enhanced_text is None.
    assert full["enhanced_text"] is None
    assert full["model"] == "nemo-parakeet-tdt-0.6b-v3"
    assert full["peaks"] is not None  # peaks saved only when audio saved


def test_single_path_save_audio_false_text_only(store):
    app = _make_app(store, save_audio=False)
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    app._enhance_then_inject("text only please", 2.0)

    result = store.list()
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["has_audio"] is False  # no WAV written when save_audio is False
    full = store.get(item["id"])
    assert full["raw_text"] == "text only please"
    assert full["peaks"] is None  # peaks saved only when audio saved
    assert store.get_audio_bytes(item["id"]) is None


def _broadcast_payloads(app):
    """The dict payloads passed to app._broadcast across this dictation."""
    return [c.args[0] for c in app._broadcast.call_args_list if c.args]


def test_single_path_broadcasts_transcripts_changed(store):
    """A successful live-dictation save notifies open clients so an open
    Transcripts list refreshes immediately, mirroring the import path. Without
    this the record only shows after the tab is remounted."""
    app = _make_app(store, save_audio=True)
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    app._enhance_then_inject("hello world", 1.5)

    assert len(store.list()["items"]) == 1  # the record actually persisted
    app._broadcast.assert_any_call({"type": "transcripts.changed"})


def test_history_disabled_persists_nothing(store):
    app = _make_app(store, history_enabled=False)
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    app._enhance_then_inject("should not persist", 1.0)

    assert store.list()["items"] == []
    assert app._last_waveform is None
    # Nothing persisted -> no list-refresh event.
    assert {"type": "transcripts.changed"} not in _broadcast_payloads(app)


def test_corrupt_history_denies_live_dictation_persistence_before_save() -> None:
    store = MagicMock()
    store.decryptable = True
    store.probe_content_integrity.return_value = False
    app = _make_app(store, history_enabled=True, save_audio=True)
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    app._persist_transcript(
        raw_text="must not append to corrupt history",
        enhanced_text=None,
        duration=1.0,
    )

    store.save.assert_not_called()
    app._broadcast.assert_not_called()
    assert app._last_waveform is None


def test_lockdown_epoch_revokes_queued_normal_mode_persistence(store):
    app = _make_app(store, history_enabled=True, save_audio=True)
    app._persistence_epoch_lock = threading.Lock()
    app._persistence_epoch = 0
    queued = _QueuedExecutor()
    app._persistence_executor = queued

    app._persist_transcript(
        raw_text="queued private dictation",
        enhanced_text=None,
        duration=1.0,
        waveform=np.ones(160, dtype=np.float32),
    )
    with app._persistence_epoch_lock:
        app._persistence_epoch += 1
    app.config.history_enabled = False
    queued.run_next()

    assert store.list()["items"] == []


def test_worker_uses_current_lockdown_state_for_save_audit(
    store, monkeypatch
):
    import whisperstream.main as main_module

    app = _make_app(store, history_enabled=True, save_audio=False)
    app._persistence_epoch_lock = threading.Lock()
    app._persistence_epoch = 0
    queued = _QueuedExecutor()
    app._persistence_executor = queued
    audit = MagicMock()
    monkeypatch.setattr(main_module, "log_access", audit)

    app._persist_transcript(
        raw_text="audited dictation",
        enhanced_text=None,
        duration=1.0,
        waveform=None,
    )
    app.config.lockdown_mode = True
    queued.run_next()

    item = store.list()["items"][0]
    audit.assert_called_once_with("save", item["id"], enabled=True)


def test_persistence_submission_is_closed_for_entire_transition_drain(store):
    app = _make_app(store, history_enabled=True, save_audio=True)
    app._persistence_epoch_lock = threading.Lock()
    app._persistence_epoch = 0
    app._persistence_accepting = True
    transition_executor = _TransitionExecutor()
    app._persistence_executor = transition_executor
    transition = threading.Thread(target=app.begin_persistence_transition)
    transition.start()
    assert transition_executor.sentinel_submitted.wait(timeout=2)

    app._persist_transcript(
        raw_text="must not enter the transition queue",
        enhanced_text=None,
        duration=1.0,
        waveform=np.ones(160, dtype=np.float32),
    )

    # Only the drain sentinel was accepted. The concurrent content closure was
    # rejected while the submission gate remained closed.
    assert len(transition_executor.submissions) == 1
    transition_executor.sentinel.set_result(None)
    transition.join(timeout=2)
    assert not transition.is_alive()
    app.end_persistence_transition()
    assert store.list()["items"] == []


def test_empty_text_persists_nothing(store):
    app = _make_app(store)
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    app._enhance_then_inject("   ", 1.0)

    assert store.list()["items"] == []
    assert app._last_waveform is None


def test_inject_suppressed_persists_nothing(store):
    """Onboarding test recordings must not land in history."""
    app = _make_app(store)
    app._inject_suppressed = True
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    app._enhance_then_inject("onboarding test", 1.0)

    assert store.list()["items"] == []
    assert app._last_waveform is None


def test_single_path_with_enhancement_captures_raw_and_enhanced(store):
    app = _make_app(store, enhancement_enabled=True)
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    # Stub the enhancer so do_enhance succeeds and rewrites the text.
    enhancer = MagicMock()

    async def _enhance(_text, **kwargs):  # accept profile_id (Power Mode routing)
        return EnhancementResult("Hello, world.", "success")

    enhancer.enhance_with_outcome = AsyncMock(side_effect=_enhance)
    # Keep length under the chunked threshold so the single-call path is taken.
    enhancer.will_enhance.return_value = True
    app.llm_enhancer = enhancer

    app._enhance_then_inject("hello world", 1.0)

    result = store.list()
    assert len(result["items"]) == 1
    full = store.get(result["items"][0]["id"])
    assert full["raw_text"] == "hello world"
    assert full["enhanced_text"] == "Hello, world."
    assert full["enhancement_provenance"] == {
        "profile_id": "chat",
        "profile_name": "Chat",
        "outcome": "success",
    }
    # Snippet prefers enhanced text.
    assert "Hello, world." in result["items"][0]["snippet"]


@pytest.mark.parametrize(
    ("outcome", "expected_enhanced", "expected_provenance"),
    [
        ("success", "hello world", "success"),
        ("timeout", None, "timeout"),
        ("error", None, "error"),
        ("not_attempted", None, None),
    ],
)
def test_single_outcome_drives_truthful_persistence(
    store, outcome, expected_enhanced, expected_provenance
):
    app = _make_app(store, enhancement_enabled=True)
    app._persist_transcript = MagicMock()
    enhancer = MagicMock()
    enhancer.will_enhance.return_value = outcome != "not_attempted"
    enhancer.enhance_with_outcome = AsyncMock(
        return_value=EnhancementResult("hello world", outcome)
    )
    app.llm_enhancer = enhancer

    app._enhance_then_inject("hello world", 1.0)

    persisted = app._persist_transcript.call_args.kwargs
    assert persisted["enhanced_text"] == expected_enhanced
    provenance = persisted["enhancement_provenance"]
    if expected_provenance is None:
        assert provenance is None
    else:
        assert provenance.profile_id == "chat"
        assert provenance.profile_name == "Chat"
        assert provenance.outcome == expected_provenance


def test_successful_unchanged_output_is_still_enhanced(store):
    app = _make_app(store, enhancement_enabled=True)
    app._persist_transcript = MagicMock()
    enhancer = MagicMock()
    enhancer.will_enhance.return_value = True
    enhancer.enhance_with_outcome = AsyncMock(
        return_value=EnhancementResult("already clean text", "success")
    )
    app.llm_enhancer = enhancer

    app._enhance_then_inject("already clean text", 1.0)

    persisted = app._persist_transcript.call_args.kwargs
    assert persisted["enhanced_text"] == "already clean text"
    assert persisted["enhancement_provenance"].outcome == "success"


def test_profile_snapshot_survives_rename_during_enhancement(store):
    app = _make_app(store, enhancement_enabled=True)
    app.config.profiles[0]["name"] = "Before rename"
    app._persist_transcript = MagicMock()
    enhancer = MagicMock()
    enhancer.will_enhance.return_value = True

    async def _rename_during_call(text, **_kwargs):
        app.config.profiles[0]["name"] = "After rename"
        return EnhancementResult(text, "success")

    enhancer.enhance_with_outcome = AsyncMock(side_effect=_rename_during_call)
    app.llm_enhancer = enhancer

    app._enhance_then_inject("profile rename race", 1.0)

    provenance = app._persist_transcript.call_args.kwargs[
        "enhancement_provenance"
    ]
    assert provenance.profile_id == "chat"
    assert provenance.profile_name == "Before rename"


def test_evaluator_contention_is_no_attempt_without_provenance(store):
    app = _make_app(store, enhancement_enabled=True)
    app._persist_transcript = MagicMock()
    app.evaluation_lease = MagicMock()
    app.evaluation_lease.try_acquire.return_value = None
    enhancer = MagicMock()
    enhancer.will_enhance.return_value = True
    enhancer.enhance_with_outcome = AsyncMock()
    app.llm_enhancer = enhancer

    app._enhance_then_inject("evaluator is already busy", 1.0)

    enhancer.enhance_with_outcome.assert_not_awaited()
    persisted = app._persist_transcript.call_args.kwargs
    assert persisted["enhanced_text"] is None
    assert persisted["enhancement_provenance"] is None


@pytest.mark.parametrize(
    ("outcome", "expects_signature"),
    [
        ("success", True),
        ("timeout", False),
        ("error", False),
        ("not_attempted", False),
    ],
)
def test_single_email_signature_requires_success(
    store, outcome, expects_signature
):
    app = _make_app(store, enhancement_enabled=True)
    app.config.default_profile_id = "email"
    app.config.profiles.append(
        {
            "id": "email",
            "name": "Email",
            "prompt": "p",
            "signature": "Best,\nAlex",
        }
    )
    app._persist_transcript = MagicMock()
    enhancer = MagicMock()
    enhancer.will_enhance.return_value = outcome != "not_attempted"
    enhancer.enhance_with_outcome = AsyncMock(
        return_value=EnhancementResult("Email body text", outcome)
    )
    app.llm_enhancer = enhancer

    app._enhance_then_inject("Email body text", 1.0)

    delivered = app.text_injector.inject.call_args.args[0]
    assert delivered.endswith("Best,\nAlex") is expects_signature
    assert delivered.count("Best,\nAlex") == (1 if expects_signature else 0)


def test_persist_failure_does_not_break_dictation(store):
    """A store error during persist must not propagate out of the hook."""
    app = _make_app(store)
    app._last_waveform = np.ones(16_000, dtype=np.float32)
    boom = MagicMock()
    boom.save.side_effect = RuntimeError("disk full")
    app.transcript_store = boom

    # Must NOT raise.
    app._enhance_then_inject("resilient", 1.0)

    # Inject still happened (dictation unaffected).
    app.text_injector.inject.assert_called_once()
    # The save failed, so no list-refresh event fires (broadcast sits AFTER save).
    assert {"type": "transcripts.changed"} not in _broadcast_payloads(app)
    assert app._last_waveform is None


def test_none_store_is_safe(store):
    """transcript_store may be None when init failed (Task 2.1)."""
    app = _make_app(store)
    app.transcript_store = None
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    app._enhance_then_inject("no store", 1.0)  # must not raise

    app.text_injector.inject.assert_called_once()
    assert app._last_waveform is None


def test_missing_persistence_executor_releases_waveform(store):
    app = _make_app(store)
    app._persistence_executor = None
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    app._enhance_then_inject("no persistence worker", 1.0)

    assert app._last_waveform is None
    assert store.list()["items"] == []


def test_persistence_submit_failure_releases_waveform(store):
    class _RejectedExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            raise RuntimeError("executor stopped")

    app = _make_app(store)
    app._persistence_executor = _RejectedExecutor()
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    app._enhance_then_inject("submit rejected", 1.0)

    assert app._last_waveform is None
    assert store.list()["items"] == []


def test_old_worker_waveform_release_never_clears_newer_recording(store):
    app = _make_app(store, history_enabled=False)
    old_waveform = np.ones(8, dtype=np.float32)
    newer_waveform = np.zeros(8, dtype=np.float32)
    app._last_waveform = newer_waveform

    app._persist_transcript(
        raw_text="old worker", enhanced_text=None, duration=1.0,
        waveform=old_waveform,
    )

    assert app._last_waveform is newer_waveform


def test_agent_compose_history_uses_distinct_source(store):
    app = _make_app(store)
    lease = SimpleNamespace(
        generation=7,
        recording_id=11,
        session_handle="opaque",
    )
    app._agent_compose_leases = MagicMock()
    app._agent_compose_leases.is_current.return_value = True
    context = SimpleNamespace(
        waveform=np.ones(16_000, dtype=np.float32),
        dictation_app=None,
        inject_suppressed=False,
        agent_compose_lease=lease,
        manual_profile_id=None,
        override_anchor=None,
        screen_reader_detected=False,
    )

    app._enhance_then_inject("reply for agent", 1.0, context=context)

    rid = store.list()["items"][0]["id"]
    assert store.get(rid)["source"] == "agent_reply"


def test_agent_compose_uses_shared_enhancement_provenance_path(store):
    app = _make_app(store, enhancement_enabled=True)
    app._persist_transcript = MagicMock()
    enhancer = MagicMock()
    enhancer.will_enhance.return_value = True
    enhancer.enhance_with_outcome = AsyncMock(
        return_value=EnhancementResult("Clean agent reply", "success")
    )
    app.llm_enhancer = enhancer
    lease = SimpleNamespace(
        generation=7,
        recording_id=11,
        session_handle="opaque",
    )
    app._agent_compose_leases = MagicMock()
    app._agent_compose_leases.is_current.return_value = True
    context = SimpleNamespace(
        waveform=np.ones(16_000, dtype=np.float32),
        dictation_app=None,
        inject_suppressed=False,
        agent_compose_lease=lease,
        manual_profile_id=None,
        override_anchor=None,
        screen_reader_detected=False,
        recording_id=None,
    )

    app._enhance_then_inject("agent reply source text", 1.0, context=context)

    persisted = app._persist_transcript.call_args.kwargs
    assert persisted["source"] == "agent_reply"
    assert persisted["enhanced_text"] == "Clean agent reply"
    assert persisted["enhancement_provenance"].profile_name == "Chat"
    assert persisted["enhancement_provenance"].outcome == "success"


# --------------------------------------------------------------------------- #
# Chunked path
# --------------------------------------------------------------------------- #


def test_chunked_path_persists_raw_and_enhanced(store):
    app = _make_app(store, enhancement_enabled=True)
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    # Enhancer for the chunked path: enhance_chunk echoes the chunk.
    enhancer = MagicMock()

    async def _enhance_chunk(chunk, **kwargs):  # accept profile_id (Power Mode routing)
        return EnhancementResult(chunk, "success")

    enhancer.enhance_chunk_with_outcome = AsyncMock(side_effect=_enhance_chunk)
    app.llm_enhancer = enhancer

    # text_injector for the chunked path: paste_chunk sets last_transcript.
    # Accept **kwargs so the input_style kwarg (Task 1.5) doesn't break the call.
    app.text_injector.paste_chunk.side_effect = (
        lambda processed, first, last, **kwargs: (
            setattr(
                app.text_injector,
                "last_transcript",
                processed,
            )
            or DeliveryOutcome.focused()
        )
    )

    app._enhance_then_inject_chunked("the quick brown fox", 3.0)

    result = store.list()
    assert len(result["items"]) == 1
    full = store.get(result["items"][0]["id"])
    # Chunked path: raw = input text, enhanced = text_injector.last_transcript.
    assert full["raw_text"] == "the quick brown fox"
    assert full["enhanced_text"] == "the quick brown fox"
    assert full["duration_ms"] == 3000


# --------------------------------------------------------------------------- #
# Logging guardrail: no transcript text in logs
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Task 4.5 — Waveform nulled after persist
# --------------------------------------------------------------------------- #


def test_last_waveform_is_none_after_persist(store):
    """_last_waveform must be None immediately after _persist_transcript_to_store
    completes — it must NOT linger until the next recording start.

    This is an unconditional cleanup (not lockdown-only) so raw audio does not
    sit in memory between recordings.
    """
    app = _make_app(store, save_audio=True)
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    app._enhance_then_inject("waveform lifecycle test", 1.0)

    # The executor is synchronous in tests, so the persist is done by the time
    # _enhance_then_inject returns.  _last_waveform must be None NOW.
    assert app._last_waveform is None, (
        "_last_waveform must be nulled after persist completes "
        "(not lingering until next recording start)"
    )


def test_last_waveform_is_none_after_persist_chunked(store):
    """Same lifecycle guarantee on the chunked path."""
    app = _make_app(store, enhancement_enabled=True, save_audio=True)
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    # Wire a stub enhancer (mirrors test_chunked_path_persists_raw_and_enhanced).
    enhancer = MagicMock()

    async def _enhance_chunk(chunk, **kwargs):
        return EnhancementResult(chunk, "success")

    enhancer.enhance_chunk_with_outcome = AsyncMock(side_effect=_enhance_chunk)
    app.llm_enhancer = enhancer

    # Chunked path uses text_injector.paste_chunk + last_transcript.
    # (**kwargs absorbs paste_chunk's input_style kwarg added by keystroke mode.)
    app.text_injector.paste_chunk.side_effect = (
        lambda processed, first, last, **kwargs: (
            setattr(
                app.text_injector,
                "last_transcript",
                processed,
            )
            or DeliveryOutcome.focused()
        )
    )
    app._enhance_then_inject_chunked("waveform lifecycle chunked", 1.0)

    assert app._last_waveform is None, (
        "_last_waveform must be nulled after persist completes on the chunked path"
    )


def test_last_waveform_is_none_even_when_save_audio_false(store):
    """Even when save_audio=False the waveform must be nulled after persist
    (it was held but not written — still should not linger in memory)."""
    app = _make_app(store, save_audio=False)
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    app._enhance_then_inject("text only lifecycle test", 1.0)

    assert app._last_waveform is None, (
        "_last_waveform must be nulled after persist even when save_audio=False"
    )


def test_no_transcript_text_in_logs(store):
    """The persist path must never log raw or enhanced transcript content."""
    secret = "topsecretdictationphrase"
    app = _make_app(store)
    # Use a real logger that records into caplog instead of a MagicMock.
    app.logger = logging.getLogger("test_persistence_hook.nolog")
    app._last_waveform = np.ones(16_000, dtype=np.float32)

    import logging as _logging

    records: list[_logging.LogRecord] = []

    class _Capture(_logging.Handler):
        def emit(self, record):  # noqa: ANN001
            records.append(record)

    handler = _Capture()
    app.logger.addHandler(handler)
    app.logger.setLevel(_logging.DEBUG)
    try:
        app._enhance_then_inject(secret, 1.0)
    finally:
        app.logger.removeHandler(handler)

    # The record persisted, but the secret never appears in any log line.
    assert len(store.list()["items"]) == 1
    for record in records:
        assert secret not in record.getMessage()


def test_cleanup_drains_finalizer_then_nested_persistence_and_clears_content():
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app._cleanup_lock = __import__("threading").Lock()
    app._cleanup_complete = False
    app._last_waveform = np.ones(4, dtype=np.float32)
    app._dictation_app = object()
    app._chunk_futures = [object()]
    app._agent_compose_recording = SimpleNamespace(generation=9)
    app._agent_compose_pending_generation = 9
    app.text_injector = SimpleNamespace(
        _last_transcript="processed", _last_original="raw"
    )
    store_mock = MagicMock()
    app.transcript_store = store_mock
    app._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    app._persistence_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    completed: list[str] = []

    def _finalize():
        app._persistence_executor.submit(lambda: completed.append("persisted"))

    app._executor.submit(_finalize)
    app.cleanup()

    assert completed == ["persisted"]
    assert app._agent_compose_recording is None
    assert app.transcript_store is None
    store_mock.clear_data_key.assert_called_once()
    store_mock.close.assert_called_once()
    assert app._last_waveform is None
    assert app.text_injector._last_transcript is None
    assert app.text_injector._last_original is None


def test_managed_history_denies_save_even_with_enabled_config(store):
    from whisperstream.managed_policy import ManagedLockdownPolicy
    app = _make_app(store, history_enabled=True, save_audio=True)
    app.managed_lockdown_policy = ManagedLockdownPolicy(True, "enabled", True, "enabled")
    app._last_waveform = np.ones(16000, dtype=np.float32)
    app._persist_transcript(raw_text="synthetic policy test", enhanced_text=None, duration=1.0)
    assert store.list()["items"] == []
    assert app._last_waveform is None


def test_queued_save_rechecks_machine_history_authority(store):
    from whisperstream.managed_policy import ManagedLockdownPolicy
    app = _make_app(store)
    queued = _QueuedExecutor()
    app._persistence_executor = queued
    app._last_waveform = np.ones(16000, dtype=np.float32)
    app._persist_transcript(raw_text="synthetic queued test", enhanced_text=None, duration=1.0)
    assert len(queued.callbacks) == 1
    app.managed_lockdown_policy = ManagedLockdownPolicy(True, "enabled", True, "enabled")
    queued.run_next()
    assert store.list()["items"] == []
