"""Focused REST, worker, WebSocket, hook, and speech consent boundaries."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from whisperstream.eula import AcceptanceGrant, AcceptanceGrantReason
from whisperstream.llm_enhancer import EnhancementResult
from whisperstream.main import WhisperstreamApp
from whisperstream.server.middleware import (
    _CONTENT_HTTP_PREFIXES,
    _CONTENT_HTTP_ROUTES,
)
from whisperstream.server.app import (
    _CONTENT_WS_MESSAGE_TYPES,
    WhisperstreamServer,
)


ACCEPTED = AcceptanceGrant(
    True,
    AcceptanceGrantReason.ACCEPTED,
    "1.3",
    "a" * 64,
    "b" * 64,
)
BLOCKED = AcceptanceGrant(
    False,
    AcceptanceGrantReason.ACCEPTANCE_MISSING,
    "1.3",
    "a" * 64,
)
MISMATCHED = AcceptanceGrant(
    False,
    AcceptanceGrantReason.ACCEPTANCE_MISMATCH,
    "1.7",
    "a" * 64,
    "b" * 64,
)


def _make_server(initial: AcceptanceGrant = BLOCKED):
    current = {"grant": initial}
    config = MagicMock()
    config.lockdown_mode = False
    config.claude_hud_enabled = False
    config.agent_speech_enabled = True
    config.agent_speech_activation = "always"
    config.enhancement_enabled = False
    config.enhancement_local_endpoint = ""
    config.default_profile_id = "chat"
    config.profiles = [{"id": "chat", "name": "Chat", "signature": ""}]
    config.history_enabled = True
    config.save_audio = False
    config.language = "en"
    config.parakeet_model = "parakeet-tdt-0.6b-v3"
    config.effective_provider.return_value = "local"

    app = MagicMock()
    app.config = config
    app.parakeet = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True}
    app.license_manager.can_start_recording.return_value = True
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.dictation_state_snapshot.return_value = None
    app._current_acceptance_grant.side_effect = lambda: current["grant"]
    app._acceptance_is_current.side_effect = (
        lambda grant: grant == ACCEPTED and current["grant"] == ACCEPTED
    )
    app.initialize_accepted_content_services.return_value = True
    for name in (
        "_snapshot_enhancement_profile",
        "_enhance_with_bounded_outcome",
        "_apply_email_signature",
    ):
        method = getattr(WhisperstreamApp, name)
        setattr(app, name, method.__get__(app, WhisperstreamApp))
    app._enhancement_provenance = WhisperstreamApp._enhancement_provenance
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    return server, app, current


def test_content_route_inventory_is_explicit_and_bounded():
    assert _CONTENT_HTTP_ROUTES == {
        ("POST", "/api/dictation/start"),
        ("POST", "/api/dictation/complete"),
        ("POST", "/api/agent-event"),
        ("POST", "/api/claude-event"),
        ("POST", "/api/claude-hook"),
        ("POST", "/api/codex-hook"),
        ("POST", "/api/agent-transport/claude"),
        ("POST", "/api/managed-sessions/register"),
        ("POST", "/api/managed-sessions/v2/register"),
        ("POST", "/api/enhancement/check"),
        ("POST", "/api/enhancement/preview"),
        ("POST", "/api/review"),
        ("POST", "/api/feedback"),
        ("POST", "/api/import"),
        ("POST", "/api/transcribe"),
    }
    assert _CONTENT_HTTP_PREFIXES == {"/api/transcripts"}
    assert {
        "agent.question.answer",
        "agent.draft.update",
        "agent.draft.submit",
    } <= _CONTENT_WS_MESSAGE_TYPES
    assert "agent.interaction.defer" not in _CONTENT_WS_MESSAGE_TYPES
    assert "agent.completion.ack" not in _CONTENT_WS_MESSAGE_TYPES


def test_setup_delivery_preview_cannot_rearm_after_acceptance_revoked_during_body():
    server, _app, current = _make_server(ACCEPTED)
    server.broadcast = MagicMock()
    endpoint = next(route.endpoint for route in server.fastapi.routes
                    if getattr(route, "path", None) ==
                    "/api/setup/agent-speak/delivery-preview")

    async def read_body():
        # Revocation clears the preview while this request is still reading.
        current["grant"] = BLOCKED
        server._setup_delivery_preview = None
        await asyncio.sleep(0)
        return {"show_summary_text": True, "agent_speech_enabled": False}

    result = asyncio.run(endpoint(SimpleNamespace(json=read_body)))
    assert server._setup_delivery_preview is None
    server.broadcast.assert_not_called()
    assert result.status_code == 403
    assert json.loads(result.body) == {"error": "eula_acceptance_required"}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/enhancement/preview", b"not-json"),
        ("post", "/api/review", b"not-json"),
        ("post", "/api/feedback", b"not-json"),
        ("post", "/api/import", b"not-json"),
        ("post", "/api/transcribe", b"not-multipart"),
        ("post", "/api/managed-sessions/v2/register", b"not-json"),
        ("post", "/api/agent-transport/claude", b"not-json"),
        ("post", "/api/managed-sessions/private-handle/reply", b"not-json"),
        ("get", "/api/managed-sessions/private-handle/pending", None),
        ("get", "/api/transcripts", None),
        ("get", "/api/transcripts/private-id/audio", None),
        ("delete", "/api/transcripts/private-id", None),
        ("post", "/api/transcripts/clear", None),
    ],
)
def test_blocked_content_routes_deny_before_validation_or_work(method, path, body):
    server, app, _current = _make_server()
    with TestClient(server.fastapi) as client:
        response = client.request(
            method,
            path,
            content=body,
            headers={"content-type": "application/json"} if body else None,
        )

    assert response.status_code == 403
    assert response.json() == {"error": "eula_acceptance_required"}
    app.parakeet.transcribe.assert_not_called()
    app.transcript_store.list.assert_not_called()
    app.transcript_store.get_audio_bytes.assert_not_called()
    app.transcript_store.delete.assert_not_called()
    app.transcript_store.clear.assert_not_called()
    assert server._managed_sessions is None


def test_agent_sessions_snapshot_is_classified_as_outbound_content():
    from whisperstream.server.websocket import _is_content_ws_event

    assert _is_content_ws_event({"type": "agent.sessions", "sessions": []}) is True


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/agent-event", {"ok": True}),
        ("/api/claude-event", {"ok": True}),
        ("/api/claude-hook", {}),
    ],
)
def test_blocked_claude_ingress_keeps_exact_success_shape_without_parsing(
    path, expected
):
    server, _app, _current = _make_server()
    with TestClient(server.fastapi) as client:
        response = client.post(
            path,
            content=b"definitely-not-json",
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 200
    assert response.json() == expected
    assert server._agent_activity is None
    assert server._claude_interactions is None


def test_blocked_import_denial_precedes_worker_creation(monkeypatch):
    import whisperstream.server.app as server_mod

    thread = MagicMock(side_effect=AssertionError("worker created"))
    monkeypatch.setattr(server_mod.threading, "Thread", thread)
    server, _app, _current = _make_server()

    with TestClient(server.fastapi) as client:
        response = client.post(
            "/api/import", json={"paths": ["private.wav"]}
        )

    assert response.status_code == 403
    assert response.json() == {"error": "eula_acceptance_required"}
    thread.assert_not_called()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/eula/status"),
        ("get", "/api/health"),
        ("get", "/api/version"),
        ("post", "/api/dictation/cancel"),
        ("post", "/api/speech/stop"),
        ("post", "/api/shutdown"),
    ],
)
def test_content_free_status_cancel_stop_and_shutdown_remain_available(method, path):
    server, app, _current = _make_server()
    app.cancel_hud_dictation.return_value = "cancelled"
    with TestClient(server.fastapi) as client:
        response = client.request(
            method,
            path,
            headers={
                "x-whisperstream-client": "hud-v1",
                "origin": "http://testserver",
            },
        )
    assert response.status_code == 200


def test_status_uses_authoritative_policy_state_and_exact_identity():
    server, _app, _current = _make_server()
    with TestClient(server.fastapi) as client:
        status = client.get("/api/eula/status").json()
    assert status["state"] == "first_run"
    assert status["grant_reason"] == "acceptance_missing"
    assert status["requirement_kind"] == "substantive_update"
    assert status["bundled_version"] == "2.1"
    assert "Hugging Face" in " ".join(status["summary"])
    assert "free" in " ".join(status["summary"]).lower()
    assert len(status["bundled_sha256"]) == 64
    assert status["needs_first_run"] is True


def test_prior_acceptance_mismatch_requires_substantive_update():
    server, _app, _current = _make_server(MISMATCHED)
    with TestClient(server.fastapi) as client:
        status = client.get("/api/eula/status").json()
    assert status["state"] == "substantive_update_required"
    assert status["grant_reason"] == "acceptance_mismatch"
    assert status["requirement_kind"] == "substantive_update"
    assert status["bundled_version"] == "2.1"
    assert status["needs_amendment"] is True


def test_accept_returns_only_after_durable_grant_and_activates_services(monkeypatch):
    import whisperstream.eula as eula

    server, app, current = _make_server()

    def persist(version, sha256, software_version):
        current["grant"] = ACCEPTED
        return {
            "eula_version": version,
            "eula_sha256": sha256,
            "software_version": software_version,
            "eula_accepted_at": "2026-08-04T12:00:00Z",
        }

    monkeypatch.setattr(eula, "write_acceptance", persist)
    with TestClient(server.fastapi) as client:
        response = client.post(
            "/api/eula/accept",
            json={"eula_version": "1.3", "eula_sha256": "a" * 64},
        )
    assert response.status_code == 200
    app.initialize_accepted_content_services.assert_called_once_with()


def test_accept_atomic_write_failure_appends_nothing_and_opens_no_services(
    monkeypatch, tmp_path
):
    """A failed evidence write is not acceptance, even through the real route."""
    import whisperstream.eula as eula

    server, app, _current = _make_server()
    monkeypatch.setattr(eula, "_eula_dir", lambda: tmp_path)
    monkeypatch.setattr(
        eula,
        "_atomic_write_json",
        MagicMock(side_effect=OSError("disk full")),
    )
    policy = eula.load_eula_policy_status()

    with TestClient(server.fastapi) as client:
        response = client.post(
            "/api/eula/accept",
            json={
                "eula_version": policy.terms_version,
                "eula_sha256": policy.terms_sha256,
            },
        )

    assert response.status_code == 500
    assert response.json() == {"error": "write_failed"}
    assert not (tmp_path / eula.ACCEPTANCE_FILENAME).exists()
    app.initialize_accepted_content_services.assert_not_called()


def test_blocked_websocket_omits_content_snapshots_and_rejects_content_message():
    server, _app, _current = _make_server()
    with TestClient(server.fastapi) as client:
        with client.websocket_connect(
            "/ws", headers={"origin": "http://testserver"}
        ) as ws:
            assert ws.receive_json()["type"] == "license.status"
            ws.send_json(
                {"type": "agent.draft.update", "target_id": "t", "text": "private"}
            )
            assert ws.receive_json() == {
                "type": "recording.blocked",
                "reason": "eula_acceptance_required",
            }
    assert server._agent_activity is None
    assert server._claude_interactions is None


@pytest.mark.parametrize("revoke_stage", ["load", "stt", "provider"])
def test_import_revalidates_each_stage_and_never_processes_or_persists(
    monkeypatch, revoke_stage
):
    import whisperstream.main as main

    server, app, current = _make_server(ACCEPTED)
    waveform = SimpleNamespace(size=1)

    def load(_path, _logger):
        if revoke_stage == "load":
            current["grant"] = BLOCKED
        return waveform, 16000

    def transcribe(*_args, **_kwargs):
        if revoke_stage == "stt":
            current["grant"] = BLOCKED
        return "raw"

    async def enhance(_text, **_kwargs):
        current["grant"] = BLOCKED
        return EnhancementResult("clean", "success")

    monkeypatch.setattr(main, "_load_audio_file", load)
    app.parakeet.transcribe.side_effect = transcribe
    app.config.enhancement_enabled = revoke_stage == "provider"
    app.llm_enhancer.enhance_with_outcome = AsyncMock(side_effect=enhance)
    server.broadcast = MagicMock()

    server._imports.run(["private.wav"], ACCEPTED)

    app.text_injector.process.assert_not_called()
    app.transcript_store.save.assert_not_called()


@pytest.mark.parametrize("decode_error", [RuntimeError, FileNotFoundError])
def test_import_expected_decode_error_after_revocation_discloses_no_failure(
    monkeypatch, decode_error
):
    import whisperstream.main as main

    server, app, current = _make_server(ACCEPTED)
    loaded = []
    events = []

    def load(path, _logger):
        loaded.append(path)
        current["grant"] = BLOCKED
        raise decode_error("synthetic decode failure after revocation")

    monkeypatch.setattr(main, "_load_audio_file", load)
    server.broadcast = lambda event, **_kwargs: events.append(event)

    server._imports._run_selections(["first.wav", "second.wav"], ACCEPTED)

    assert loaded == ["first.wav"]
    assert events == [
        {"type": "import.progress", "file": "first.wav", "status": "transcribing"}
    ]
    app.parakeet.transcribe.assert_not_called()
    app.text_injector.process.assert_not_called()
    app.transcript_store.save.assert_not_called()


@pytest.mark.parametrize("decode_error", [None, RuntimeError, FileNotFoundError])
def test_import_cancel_during_decode_suppresses_failure_and_releases_slot(
    monkeypatch, decode_error
):
    import concurrent.futures
    import threading
    import whisperstream.main as main

    server, app, _current = _make_server(ACCEPTED)
    entered = threading.Event()
    release = threading.Event()
    loaded = []
    events = []
    # Synthetic paths do not require a native picker. Keep the real operation,
    # executor, reservation, cancel event and completion callback.
    server._imports._current_file_workflows = lambda: SimpleNamespace()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    app._import_executor = executor

    def load(path, _logger):
        loaded.append(path)
        entered.set()
        assert release.wait(timeout=5)
        if decode_error is not None:
            raise decode_error("synthetic decode failure after cancellation")
        return SimpleNamespace(size=1), 16000

    monkeypatch.setattr(main, "_load_audio_file", load)
    server.broadcast = lambda event, **_kwargs: events.append(event)
    try:
        assert server._imports.try_start(["first.wav", "second.wav"], ACCEPTED)
        assert entered.wait(timeout=5)
        assert server._imports.active is True
        assert not server._imports.try_start(["third.wav"], ACCEPTED)
        server._imports.cancel()
    finally:
        release.set()
        executor.shutdown(wait=True)

    assert server._imports.active is False
    assert loaded == ["first.wav"]
    assert events == [
        {"type": "import.progress", "file": "first.wav", "status": "transcribing"}
    ]
    app.parakeet.transcribe.assert_not_called()
    app.text_injector.process.assert_not_called()
    app.transcript_store.save.assert_not_called()


@pytest.mark.parametrize("decode_error", [RuntimeError, FileNotFoundError])
def test_import_authorized_decode_failure_retains_friendly_reason_and_queue(
    monkeypatch, decode_error
):
    import whisperstream.main as main

    server, app, _current = _make_server(ACCEPTED)
    loaded = []
    events = []

    def load(path, _logger):
        loaded.append(path)
        raise decode_error("synthetic details must not be disclosed")

    monkeypatch.setattr(main, "_load_audio_file", load)
    server.broadcast = lambda event, **_kwargs: events.append(event)
    server._imports._run_selections(["first.wav", "second.wav"], ACCEPTED)

    assert loaded == ["first.wav", "second.wav"]
    assert events == [
        event
        for name in ["first.wav", "second.wav"]
        for event in [
            {"type": "import.progress", "file": name, "status": "transcribing"},
            {
                "type": "import.progress", "file": name, "status": "failed",
                "reason": "This format could not be transcribed. Convert it to WAV and try again.",
            },
        ]
    ]
    app.parakeet.transcribe.assert_not_called()
    app.transcript_store.save.assert_not_called()


@pytest.mark.parametrize("stop_reason", ["cancel", "revoke"])
def test_import_releases_real_file_selections_after_interrupted_decode(
    tmp_path, monkeypatch, stop_reason,
):
    import concurrent.futures
    import threading
    import whisperstream.main as main
    from whisperstream.history_security_contract import FileWorkflowKind, HistoryFailure
    from whisperstream.windows_history_security import WindowsFileWorkflowAdapter

    server, app, current = _make_server(ACCEPTED)
    files = [tmp_path / name for name in ("first.wav", "second.wav")]
    for source in files:
        source.write_bytes(b"synthetic source content")
    workflows = WindowsFileWorkflowAdapter()
    batch = workflows.register_paths(FileWorkflowKind.IMPORT_AUDIO, files)
    assert batch.failure is HistoryFailure.NONE
    server._imports._current_file_workflows = lambda: workflows
    entered, release = threading.Event(), threading.Event()
    loaded, events = [], []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    app._import_executor = executor

    def decode(path, _logger):
        loaded.append(path)
        entered.set()
        assert release.wait(timeout=5)
        return SimpleNamespace(size=1), 16000

    monkeypatch.setattr(main, "_load_audio_file", decode)
    server.broadcast = lambda event, **_kwargs: events.append(event)
    try:
        assert server._imports.try_start(list(batch.selections), ACCEPTED)
        assert entered.wait(timeout=5)
        assert workflows.active_selection_count == 2
        if stop_reason == "cancel":
            server._imports.cancel()
        else:
            current["grant"] = BLOCKED
    finally:
        release.set()
        executor.shutdown(wait=True)

    assert loaded == [str(files[0])]
    assert not server._imports.active
    assert workflows.active_selection_count == 0
    assert all(source.read_bytes() == b"synthetic source content" for source in files)
    assert events == [{"type": "import.progress", "file": "first.wav", "status": "transcribing"}]
    app.parakeet.transcribe.assert_not_called()
    app.text_injector.process.assert_not_called()
    app.transcript_store.save.assert_not_called()


def test_queued_agent_speech_rechecks_original_grant_at_promotion():
    server, app, current = _make_server(ACCEPTED)
    coordinator = MagicMock()
    captured = {}

    def enqueue(**kwargs):
        captured.update(kwargs)
        return object()

    coordinator.enqueue.side_effect = enqueue
    server._get_agent_response_coordinator = MagicMock(
        return_value=coordinator
    )
    app.speech_controller.can_speak.return_value = True

    server._queue_claude_response(
        result={"spoken_summary": "private", "session_id": "session"},
        completion_id="completion",
        completion_sequence=1,
        acceptance_grant=ACCEPTED,
    )
    current["grant"] = BLOCKED

    assert captured["speech_start"](MagicMock()) is False
    app.speech_controller.speak_spoken_summary.assert_not_called()


def test_direct_ws_handler_rechecks_before_broker_mutation():
    server, _app, _current = _make_server()
    ws = MagicMock()
    ws.send_text = AsyncMock()
    server._handle_claude_interaction_ws_message = AsyncMock()

    asyncio.run(
        server._handle_ws_message(
            {"type": "agent.question.answer", "text": "private"}, ws
        )
    )

    server._handle_claude_interaction_ws_message.assert_not_called()
    sent = ws.send_text.await_args.args[0]
    assert "eula_acceptance_required" in sent


def test_stats_failure_does_not_suppress_admitted_result():
    server, _app, _current = _make_server(ACCEPTED)
    server.stats.record_transcription = MagicMock(side_effect=ValueError("synthetic failure"))
    server._socket_delivery.publish = MagicMock()
    event = {"type": "transcription.result", "text": "synthetic words", "duration_ms": 12}

    server.broadcast(event, acceptance_grant=ACCEPTED)

    server.stats.record_transcription.assert_called_once_with("synthetic words", 12)
    server._socket_delivery.publish.assert_called_once_with(
        event, loop=server._loop, acceptance_grant=ACCEPTED,
    )


def test_preview_broadcast_does_not_record_usage():
    server, _app, _current = _make_server(ACCEPTED)
    server.stats.record_transcription = MagicMock()
    server._socket_delivery.publish = MagicMock()
    server.broadcast({"type": "transcription.preview", "text": "synthetic preview"}, acceptance_grant=ACCEPTED)
    server.stats.record_transcription.assert_not_called()


def test_outbound_content_broadcast_is_dropped_after_grant_revocation():
    """A queued result cannot escape after the request's exact grant is revoked."""
    server, _app, current = _make_server(ACCEPTED)
    server._loop = MagicMock()
    server.stats.record_transcription = MagicMock()
    ws = MagicMock()
    server._clients = {ws}
    current["grant"] = BLOCKED

    server.broadcast(
        {"type": "transcription.result", "text": "private"},
        acceptance_grant=ACCEPTED,
    )

    server._loop.call_soon_threadsafe.assert_not_called()
    server.stats.record_transcription.assert_not_called()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/enhancement/preview", b"not-json"),
        ("post", "/api/review", b"not-json"),
        ("post", "/api/feedback", b"not-json"),
        ("post", "/api/import", b"not-json"),
        ("post", "/api/transcribe", b"not-multipart"),
    ],
)
def test_exact_accepted_state_reaches_representative_content_route_contracts(
    method, path, body
):
    """Accepted requests reach route validation instead of the EULA middleware."""
    server, _app, _current = _make_server(ACCEPTED)
    with TestClient(server.fastapi) as client:
        response = client.request(
            method,
            path,
            content=body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code != 403, (path, response.text)
    assert response.json().get("error") != "eula_acceptance_required"


def test_exact_accepted_state_preserves_transcript_access():
    server, app, _current = _make_server(ACCEPTED)
    app.transcript_store.list.return_value = {
        "items": [{"id": "accepted", "text": "representative"}],
        "next_cursor": None,
        "decryptable": True,
    }

    with TestClient(server.fastapi) as client:
        response = client.get("/api/transcripts")

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == "accepted"
    app.transcript_store.list.assert_called_once_with(
        before=None, limit=20, q=None
    )


def test_history_wait_keeps_health_available_and_defers_key_rotation(monkeypatch):
    import threading
    import whisperstream.main as main_mod

    server, app, current = _make_server()
    current["grant"] = ACCEPTED
    app._headless = True
    app.config.mac_history_opt_in = True
    app._file_only = False
    app._shutting_down = False
    app._transcript_store_initialized = False
    app._transcript_store_init_thread = None
    app._transcript_store_lock = threading.Lock()
    app._transcript_store_start_lock = threading.Lock()
    app._transcript_store_publish_lock = threading.Lock()
    app.transcript_store = None
    # Bind the real startup seam, including its property, to the mocked app.
    type(app).history_initializing = WhisperstreamApp.history_initializing
    app.ensure_transcript_store_initialized = WhisperstreamApp.ensure_transcript_store_initialized.__get__(app)
    app.initialize_accepted_content_services = WhisperstreamApp.initialize_accepted_content_services.__get__(app)
    entered, release = threading.Event(), threading.Event()

    def blocked_store():
        entered.set()
        assert release.wait(5), "HTTP startup waited for history"
        return MagicMock()

    monkeypatch.setattr(main_mod.sys, "platform", "darwin")
    monkeypatch.setattr(main_mod, "TranscriptStore", blocked_store)
    monkeypatch.setattr(main_mod, "run_retention_sweep", MagicMock())
    try:
        with TestClient(server.fastapi) as client:
            assert entered.wait(1)
            assert client.get("/api/health").status_code == 200
            assert client.get("/api/eula/status").status_code == 200
            response = client.post("/api/lockdown/enable", json={})
            assert response.status_code == 503
            assert response.json()["reason"] == "history_initializing"
            assert app.transcript_store is None
            release.set()
            app._transcript_store_init_thread.join(2)
            assert not app.history_initializing
    finally:
        release.set()
        if app._transcript_store_init_thread is not None:
            app._transcript_store_init_thread.join(2)


def test_admitted_session_refuses_decline_and_different_agreement(tmp_path, monkeypatch):
    from whisperstream import eula

    monkeypatch.setattr(eula, "_eula_dir", lambda: tmp_path)
    policy = eula.load_eula_policy_status()
    record = eula.write_acceptance(policy.terms_version, policy.terms_sha256, "test")
    path = tmp_path / "acceptance.json"
    original = path.read_bytes()
    server, app, _ = _make_server()
    app._current_acceptance_grant.side_effect = eula.get_acceptance_grant
    app._acceptance_is_current.side_effect = eula.acceptance_grant_is_current
    with TestClient(server.fastapi) as client:
        assert client.post("/api/eula/decline-amendment", json={
            "declined_version": policy.terms_version, "pinned_version": "1.0",
        }).status_code == 409
        assert client.post("/api/eula/accept", json={
            "eula_version": policy.terms_version, "eula_sha256": "0" * 64,
        }).status_code == 400
        retry = client.post("/api/eula/accept", json={
            "eula_version": policy.terms_version, "eula_sha256": policy.terms_sha256,
        })
        assert retry.status_code == 200
        assert retry.json() == record
        assert path.read_bytes() == original
        path.unlink()
        status = client.get("/api/eula/status").json()
        assert status["state"] == "ready"
        assert status["acceptance"] == record
    assert not (tmp_path / "update-opt-out.json").exists()
