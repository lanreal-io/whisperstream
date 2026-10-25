"""Tests for config reload and exact STT manager activation."""

from __future__ import annotations

import threading
from dataclasses import replace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from whisperstream.capture_contract import (
    CaptureCompletion,
    CaptureCompletionStatus,
    CaptureFailure,
    CaptureSelectionResolution,
    CaptureSession,
    CaptureTerminalEvent,
)
from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.managed_policy import ManagedLockdownPolicy


@pytest.fixture
def patched_app(tmp_path):
    """Construct a WhisperstreamApp with all hardware/model touchpoints mocked.

    Returns (app, manager sentinel) so tests can reset the cached manager
    on the app instance after construction.
    """
    with (
        patch("whisperstream.main.make_stt_manager"),
        patch("whisperstream.main.KeyboardShortcutManager") as _mock_keyboard,
        patch("whisperstream.main.AudioCapture") as _mock_audio_capture,
        patch("whisperstream.main.AudioFeedback") as _mock_audio_feedback,
        patch("whisperstream.main.LicenseManager") as _mock_license,
        patch("whisperstream.main.TextInjector") as _mock_text_injector,
        patch("whisperstream.speech.controller.SpeechController"),
        patch(
            "whisperstream.main.model_dir",
            return_value=tmp_path,
        ),
        # Pin config load to a fresh default so tests don't depend on the
        # user's real AppData config.toml (which drifts between runs).
        patch(
            "whisperstream.main.ConfigManager.load",
            return_value=WhisperstreamConfig(),
        ),
    ):
        from whisperstream.main import WhisperstreamApp

        app = WhisperstreamApp(headless=True)
        # Model the ready platform keyboard contract, including Mac recovery.
        app.keyboard.capture_active = False
        app.keyboard.listener_ready = True
        app.keyboard.last_cleanup_failure = None
        app.keyboard.ensure_ready.return_value = True
        # After construction, replace the mock-returned ParakeetManager
        # instance with our own sentinel so we can assert identity on it.
        sentinel = MagicMock(name="parakeet_sentinel")
        app.stt_engine = sentinel
        yield app, sentinel


def test_reload_config_same_model_keeps_parakeet_intact(patched_app) -> None:
    app, sentinel = patched_app
    # Change something unrelated (e.g. audio_feedback) but keep model + quant.
    new_config = replace(app.config, audio_feedback=not app.config.audio_feedback)
    app.reload_config(new_config)
    assert app.stt_engine is sentinel


def test_reload_config_reconciles_speech_after_installing_new_config(patched_app):
    app, _ = patched_app
    seen = []
    app.speech_controller.reconcile_residency.side_effect = lambda: seen.append(app.config.agent_speech_idle_timeout_seconds)
    app.reload_config(replace(app.config, agent_speech_idle_timeout_seconds=900))
    assert seen == [900]


def test_config_endpoint_persists_voice_timeout_and_reconciles_live_controller(
    accepted_eula, patched_app, tmp_path
):
    from whisperstream.config_manager import ConfigManager
    from whisperstream.server import WhisperstreamServer

    app, _ = patched_app
    # Replace the fixture's mocked loader with actual isolated persistence.
    app.config_manager = ConfigManager(config_path=tmp_path / "voice-config.toml")
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    seen = []
    app.speech_controller.reconcile_residency.side_effect = lambda: seen.append(app.config.agent_speech_idle_timeout_seconds)
    client = TestClient(server.fastapi)
    try:
        body = app.config.to_dict()
        body["agent_speech_idle_timeout_seconds"] = 900
        response = client.put("/api/config", json=body)
        assert response.status_code == 200
        assert response.json()["agent_speech_idle_timeout_seconds"] == 900
        assert client.get("/api/config").json()["agent_speech_idle_timeout_seconds"] == 900
        assert seen == [900]
        import tomllib
        assert tomllib.loads((tmp_path / "voice-config.toml").read_text())["agent_speech_idle_timeout_seconds"] == 900
    finally:
        client.close()
        server._clear_agent_speak_state(reason="shutdown")


def test_dictionary_boost_save_does_not_reload_speech_model(patched_app) -> None:
    app, sentinel = patched_app
    app.reload_config(replace(app.config, word_boosts=["TanStack", "PostgreSQL"]))
    assert app.parakeet is sentinel
    sentinel.update_hotwords.assert_not_called()
    assert app.config.word_boosts == ["TanStack", "PostgreSQL"]


def test_reload_config_applies_recording_control_preference(patched_app) -> None:
    app, sentinel = patched_app
    assert app.config.show_recording_control is False

    new_config = replace(app.config, show_recording_control=True)
    app.reload_config(new_config)

    assert app.config.show_recording_control is True
    assert app.stt_engine is sentinel


def test_reload_config_model_change_keeps_parakeet_until_swap(patched_app) -> None:
    """When parakeet_model changes, the old ParakeetManager stays loaded so
    recording during the new model's download keeps working. The atomic swap
    happens later, in _run_model_download, once the new manager is ready.
    """
    app, sentinel = patched_app
    assert app.config.parakeet_model == "nemo-parakeet-tdt-0.6b-v3"
    new_config = replace(app.config, parakeet_model="nemo-parakeet-tdt-0.6b-v2")
    app.reload_config(new_config)
    assert app.stt_engine is sentinel


def test_reload_config_quantization_change_keeps_parakeet_until_swap(patched_app) -> None:
    """Same contract as the model change: quantization swap waits for the new
    ParakeetManager. Old manager keeps serving until then.
    """
    app, sentinel = patched_app
    assert app.config.parakeet_quantization == "int8"
    new_config = replace(app.config, parakeet_quantization="fp16")
    app.reload_config(new_config)
    assert app.stt_engine is sentinel


def test_install_stt_manager_atomically_tracks_exact_artifact_and_closes_old(
    patched_app,
) -> None:
    app, old_manager = patched_app
    new_manager = MagicMock(name="new_manager")
    target = replace(
        app.config,
        language="en",
        stt_model_selection_mode="manual",
        parakeet_model="nemo-parakeet-tdt-0.6b-v2",
    )
    app.reload_config(target)

    app.install_stt_manager(new_manager, target)

    assert app.parakeet is new_manager
    assert app.loaded_stt_artifact_id == "nemo-parakeet-tdt-0.6b-v2"
    old_manager.shutdown.assert_called_once_with()


def test_startup_loader_constructs_and_installs_exact_configured_artifact(
    patched_app,
) -> None:
    app, old_manager = patched_app
    old_manager.shutdown.reset_mock()
    app.parakeet = None
    app._loaded_stt_artifact_id = None
    app._loaded_stt_routing_state = None
    target = replace(
        app.config,
        language="ja",
        stt_model_selection_mode="manual",
        stt_backend="qwen",
        qwen_force_language=True,
    )
    app.config = target
    manager = MagicMock(name="startup_qwen_manager")

    with patch(
        "whisperstream.main.make_stt_manager",
        return_value=manager,
    ) as factory:
        app._load_parakeet_blocking()

    factory.assert_called_once_with(
        target,
        logger=app.logger,
        config_manager=app.config_manager,
    )
    assert app.parakeet is manager
    assert app.loaded_stt_artifact_id == "qwen3-asr-0.6b-int8"
    manager.shutdown.assert_not_called()


def test_startup_loader_closes_manager_when_routing_changes_during_construction(
    patched_app,
) -> None:
    app, _old_manager = patched_app
    app.parakeet = None
    app._loaded_stt_artifact_id = None
    app._loaded_stt_routing_state = None
    initial = replace(
        app.config,
        language="fr",
        stt_model_selection_mode="manual",
        stt_backend="parakeet",
        parakeet_model="nemo-parakeet-tdt-0.6b-v3",
    )
    changed = replace(
        initial,
        language="ja",
        stt_backend="qwen",
    )
    app.config = initial
    manager = MagicMock(name="stale_startup_manager")
    construction_started = threading.Event()
    finish_construction = threading.Event()
    errors: list[BaseException] = []

    def construct(*_args, **_kwargs):
        construction_started.set()
        assert finish_construction.wait(timeout=2.0)
        return manager

    def run_loader() -> None:
        try:
            app._load_parakeet_blocking()
        except BaseException as exc:  # capture the worker-thread assertion target
            errors.append(exc)

    with patch("whisperstream.main.make_stt_manager", side_effect=construct):
        loader = threading.Thread(target=run_loader)
        loader.start()
        assert construction_started.wait(timeout=2.0)
        app.config = changed
        finish_construction.set()
        loader.join(timeout=2.0)

    assert not loader.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "STT activation target is stale"
    manager.shutdown.assert_called_once_with()
    assert app.parakeet is None
    assert app.loaded_stt_artifact_id is None


def test_startup_loader_preserves_manager_installed_before_it_gets_guard(
    patched_app,
) -> None:
    app, old_manager = patched_app
    old_manager.shutdown.reset_mock()
    app.parakeet = None
    app._loaded_stt_artifact_id = None
    app._loaded_stt_routing_state = None
    target = replace(
        app.config,
        language="ja",
        stt_model_selection_mode="manual",
        stt_backend="qwen",
        qwen_force_language=True,
    )
    app.config = target
    switched_manager = MagicMock(name="switched_manager")
    loader_started = threading.Event()
    loader_finished = threading.Event()

    def run_loader() -> None:
        loader_started.set()
        app._load_parakeet_blocking()
        loader_finished.set()

    app._stt_model_operation_lock.acquire()
    try:
        with patch("whisperstream.main.make_stt_manager") as factory:
            loader = threading.Thread(target=run_loader)
            loader.start()
            assert loader_started.wait(timeout=2.0)
            app.install_stt_manager(switched_manager, target)
            app._stt_model_operation_lock.release()
            loader.join(timeout=2.0)

        assert not loader.is_alive()
        assert loader_finished.is_set()
        factory.assert_not_called()
    finally:
        if app._stt_model_operation_lock.locked():
            app._stt_model_operation_lock.release()

    assert app.parakeet is switched_manager
    assert app.loaded_stt_artifact_id == "qwen3-asr-0.6b-int8"
    switched_manager.shutdown.assert_not_called()


def test_async_startup_blocks_switch_and_relocation_then_installs_exact_model(
    patched_app,
    tmp_path,
    accepted_eula,
) -> None:
    from whisperstream.server import WhisperstreamServer

    app, _old_manager = patched_app
    app.parakeet = None
    app._loaded_stt_artifact_id = None
    app._loaded_stt_routing_state = None
    target = replace(
        app.config,
        language="ja",
        stt_model_selection_mode="manual",
        stt_backend="qwen",
        qwen_force_language=True,
    )
    app.config = target
    manager = MagicMock(name="async_startup_qwen_manager")
    construction_started = threading.Event()
    finish_construction = threading.Event()
    model_loaded = threading.Event()

    def construct(*_args, **_kwargs):
        construction_started.set()
        assert finish_construction.wait(timeout=2.0)
        return manager

    server = WhisperstreamServer(
        app,
        port=0,
        logger=app.logger,
        register_hotkeys=False,
    )
    assert server._model_operation_lock is app._stt_model_operation_lock

    def capture(event: dict) -> None:
        if event.get("type") == "model.loaded":
            model_loaded.set()

    app.set_event_broadcaster(capture)
    client = TestClient(server.fastapi, raise_server_exceptions=True)

    from whisperstream.windows_model_lifecycle import WindowsModelLifecycleAdapter
    server._model_lifecycle = WindowsModelLifecycleAdapter()

    with patch("whisperstream.main.make_stt_manager", side_effect=construct):
        app.load_parakeet_async()
        assert construction_started.wait(timeout=2.0)

        switch = client.post("/api/download/start")
        relocation = client.post(
            "/api/models/relocate",
            json={"destination": str(tmp_path / "destination")},
        )

        assert switch.status_code == 409
        assert switch.json() == {
            "status": "busy",
            "reason": "operation_busy",
        }
        assert relocation.status_code == 409
        assert relocation.json() == {
            "status": "busy",
            "reason": "operation_busy",
        }
        assert server._download_thread is None

        finish_construction.set()
        assert model_loaded.wait(timeout=2.0)

    assert app.parakeet is manager
    assert app.loaded_stt_artifact_id == "qwen3-asr-0.6b-int8"
    manager.shutdown.assert_not_called()


def test_install_stt_manager_rejects_active_dictation_without_swapping(
    patched_app,
) -> None:
    from whisperstream.main import SttActivationBusyError

    app, old_manager = patched_app
    new_manager = MagicMock(name="new_manager")
    target = replace(
        app.config,
        language="en",
        stt_model_selection_mode="manual",
        parakeet_model="nemo-parakeet-tdt-0.6b-v2",
    )
    app.reload_config(target)
    app._dictation_state = {**app._dictation_state, "phase": "recording"}

    with pytest.raises(SttActivationBusyError):
        app.install_stt_manager(new_manager, target)

    assert app.parakeet is old_manager
    old_manager.shutdown.assert_not_called()
    new_manager.shutdown.assert_called_once_with()


def test_failed_activation_restores_complete_prior_routing_state(
    patched_app,
) -> None:
    app, _old_manager = patched_app
    app.config_manager.save = MagicMock()
    app.config = replace(
        app.config,
        language="fr",
        stt_model_selection_mode="manual",
        stt_backend="parakeet",
        parakeet_model="nemo-parakeet-tdt-0.6b-v3",
        qwen_force_language=False,
    )
    app._record_loaded_stt_config(app.config)
    target = replace(
        app.config,
        language="ja",
        stt_model_selection_mode="manual",
        stt_backend="qwen",
        qwen_force_language=True,
    )
    app.reload_config(target)
    rollback = app.stt_activation_rollback_state(target)

    restored = app.restore_stt_routing_state(rollback, target)

    assert restored is True
    assert app.config.language == "fr"
    assert app.config.stt_model_selection_mode == "manual"
    assert app.config.stt_backend == "parakeet"
    assert app.config.parakeet_model == "nemo-parakeet-tdt-0.6b-v3"
    assert app.config.qwen_force_language is False
    app.config_manager.save.assert_called_once_with(app.config)


def test_commit_only_route_becomes_next_activation_rollback(
    patched_app,
) -> None:
    """A same-artifact save is the immediate prior state for later rollback."""
    app, _old_manager = patched_app
    app.config_manager.save = MagicMock()
    loaded = replace(
        app.config,
        language="fr",
        stt_model_selection_mode="manual",
        stt_backend="parakeet",
        parakeet_model="nemo-parakeet-tdt-0.6b-v3",
        qwen_force_language=False,
    )
    app.config = loaded
    app._record_loaded_stt_config(loaded)

    # Automatic French uses the same exact multilingual artifact, so this is
    # committed live without constructing another manager.
    committed = replace(loaded, stt_model_selection_mode="automatic")
    app.reload_config(committed)

    assert app._loaded_stt_routing_state.stt_model_selection_mode == "automatic"
    assert app._stt_activation_rollback is None

    target = replace(
        committed,
        language="ja",
        stt_model_selection_mode="manual",
        stt_backend="qwen",
        qwen_force_language=True,
    )
    app.reload_config(target)
    rollback = app.stt_activation_rollback_state(target)

    assert rollback.language == "fr"
    assert rollback.stt_model_selection_mode == "automatic"
    assert rollback.stt_backend == "parakeet"
    assert rollback.parakeet_model == "nemo-parakeet-tdt-0.6b-v3"
    assert rollback.qwen_force_language is False

    assert app.restore_stt_routing_state(rollback, target) is True
    assert app.config == committed


def test_manual_cohere_language_change_updates_loaded_manager_without_swap(
    patched_app,
) -> None:
    app, manager = patched_app
    loaded = replace(
        app.config,
        language="en",
        stt_model_selection_mode="manual",
        stt_backend="cohere",
        qwen_force_language=False,
    )
    app.config = loaded
    app._record_loaded_stt_config(loaded)
    manager.reset_mock()

    updated = replace(loaded, language="de")
    app.reload_config(updated)

    assert app.parakeet is manager
    assert app.loaded_stt_artifact_id == "cohere-transcribe-03-2026-int8"
    manager.set_language_hint.assert_called_once_with("de")


@pytest.mark.parametrize("target", [
    {"stt_model_selection_mode": "automatic", "stt_backend": "parakeet",
     "parakeet_model": "nemo-parakeet-tdt-0.6b-v2"},
    {"stt_backend": "qwen", "language": "ja", "qwen_force_language": True},
])
def test_leaving_cohere_keeps_loaded_language_until_replacement(
    patched_app, tmp_path, target,
) -> None:
    """Exercise the real shared parent, not a permissive language-method mock."""
    from whisperstream.cohere_manager import CohereManager
    from tests._final_stt_support import _FakeSupervisor

    app, _ = patched_app
    loaded = replace(app.config, language="en", stt_model_selection_mode="manual",
                     stt_backend="cohere", qwen_force_language=False)
    app.config = loaded
    app._record_loaded_stt_config(loaded)
    supervisor = _FakeSupervisor()
    manager = CohereManager(
        model_name="cohere-transcribe-03-2026-int8", language="en",
        logger=app.logger, model_dir=tmp_path, timeout=0, _supervisor=supervisor,
    )
    app.stt_engine = manager
    app.config_manager.save = MagicMock()
    requested = replace(loaded, **target)
    try:
        app.reload_config(requested)
        assert app.stt_engine is manager
        assert app.loaded_stt_artifact_id == "cohere-transcribe-03-2026-int8"
        assert supervisor.language_calls == []
        assert manager._forced_language == "en"
        assert app.restore_stt_routing_state(
            app.stt_activation_rollback_state(requested), requested,
        ) is True
        assert app.config == loaded
        assert supervisor.language_calls == ["en"]
    finally:
        manager.shutdown()


def test_cohere_environment_override_uses_loaded_backend_for_language_hint(
    patched_app, tmp_path, monkeypatch,
) -> None:
    from whisperstream.cohere_manager import CohereManager
    from tests._final_stt_support import _FakeSupervisor

    app, _ = patched_app
    monkeypatch.setenv("WHISPERSTREAM_STT_BACKEND", "cohere")
    loaded = replace(app.config, language="en", stt_model_selection_mode="automatic",
                     stt_backend="parakeet", qwen_force_language=False)
    app.config = loaded
    app._record_loaded_stt_config(loaded)
    supervisor = _FakeSupervisor()
    manager = CohereManager(
        model_name="cohere-transcribe-03-2026-int8", language="en",
        logger=app.logger, model_dir=tmp_path, timeout=0, _supervisor=supervisor,
    )
    app.stt_engine = manager
    try:
        app.reload_config(replace(loaded, language="de"))
        assert supervisor.language_calls == ["de"]
        assert app.loaded_stt_artifact_id == "cohere-transcribe-03-2026-int8"
    finally:
        manager.shutdown()


def test_unload_stt_manager_closes_and_clears_loaded_identity(
    patched_app,
) -> None:
    app, manager = patched_app
    app._record_loaded_stt_config(app.config)

    app.unload_stt_manager()

    assert app.parakeet is None
    assert app.loaded_stt_artifact_id is None
    assert app._loaded_stt_routing_state is None
    assert app._stt_activation_rollback is None
    manager.shutdown.assert_called_once_with()


def test_cleanup_closes_loaded_stt_manager(patched_app) -> None:
    app, manager = patched_app

    app.cleanup()

    assert app.parakeet is None
    assert app.loaded_stt_artifact_id is None
    manager.shutdown.assert_called_once_with()


def test_reload_config_view_shortcut_change_reregisters_hotkeys(patched_app) -> None:
    app, _ = patched_app
    new_config = replace(
        app.config,
        claude_view_shortcut="ctrl+shift+v",
    )

    app.reload_config(new_config)

    app.keyboard.unhook_all.assert_called_once_with()
    app.keyboard.register_extra_hotkey.assert_any_call(
        "ctrl+shift+v", app._on_claude_view_press, key="claude_view"
    )


def test_reload_config_rewires_device_loss_on_next_start(
    patched_app,
) -> None:
    app, _ = patched_app
    replacement = MagicMock()
    session = CaptureSession(
        request_id="capture-before-reload",
        generation=9,
        device_id="opaque-input",
        selection_resolution=CaptureSelectionResolution.SELECTED,
    )
    event = CaptureTerminalEvent(
        request_id=session.request_id,
        generation=session.generation,
        failure=CaptureFailure.DEVICE_LOST,
        owner_epoch=28,
    )
    active_capture = app.audio_capture
    active_capture.owner_epoch = event.owner_epoch
    active_capture.active_session = session
    active_capture.cancel_capture.return_value = CaptureCompletion(
        request_id=session.request_id,
        generation=session.generation,
        status=CaptureCompletionStatus.FAILED,
        failure=CaptureFailure.DEVICE_LOST,
    )
    active_capture.close.return_value = True
    replacement.owner_epoch = 29

    app._recording = True
    app._recording_origin = "toggle"
    app._active_capture_session_key = (
        event.owner_epoch,
        session.request_id,
        session.generation,
    )
    app._recording_acceptance_grant = object()
    app._stop_timer = None
    app._idle_timer = None
    app._chunk_futures = []
    app._last_waveform = object()
    app._invalidate_live_preview = MagicMock()
    app._restore_system_volume = MagicMock()
    app.dictation_state_snapshot = MagicMock(
        return_value={"recording_id": 1, "phase": "recording"}
    )
    app._transition_dictation_state = MagicMock(return_value=True)
    app._broadcast = MagicMock()
    app._play_recording_chime = MagicMock()
    app._close_audio_policy = MagicMock()
    app._dispatch_error_dialog = MagicMock()
    app.keyboard.reset_mock()

    with patch(
        "whisperstream.main.AudioCapture",
        return_value=replacement,
    ) as capture_factory:
        app.reload_config(replace(app.config, audio_input_device="Changed A"))
        app.reload_config(replace(app.config, audio_input_device="Changed B"))
        capture_factory.assert_not_called()
        active_capture.close.assert_not_called()
        assert app._recording is True
        settled = threading.Event()
        original_settle = app._settle_capture_terminal_event

        def settle(received: CaptureTerminalEvent) -> None:
            original_settle(received)
            settled.set()

        app._settle_capture_terminal_event = settle
        app._enqueue_capture_terminal_event(event)
        assert settled.wait(2.0)
        capture_factory.assert_not_called()
        with app._lock:
            assert app._ensure_audio_capture_selection_locked()

    terminal_callback = capture_factory.call_args.kwargs["terminal_callback"]
    assert terminal_callback.__self__ is app
    assert terminal_callback.__func__ is app._enqueue_capture_terminal_event.__func__
    assert capture_factory.call_args.kwargs["device"] == "Changed B"
    active_capture.cancel_capture.assert_called_once_with(session)
    active_capture.close.assert_called_once_with()
    app._dispatch_error_dialog.assert_called_once()
    app.keyboard.reset_toggle_state.assert_called_once_with()
    assert app._recording is False
    assert app._active_capture_session_key is None


@pytest.mark.parametrize("terminal", ["normal", "cancel"])
def test_active_reload_coalesces_to_latest_selection_without_interrupting(
    patched_app,
    terminal: str,
) -> None:
    app, _ = patched_app
    active_capture = app.audio_capture
    active_capture.close.return_value = True
    active_capture.stop.return_value = np.empty(0, dtype=np.float32)
    app._recording = True
    app._recording_origin = "toggle"
    app._active_capture_session_key = (31, "capture", 1)
    app._recording_acceptance_grant = object()
    app._stop_timer = None
    app._idle_timer = None
    app._chunk_futures = []
    app._chunk_start_time = 0.0
    app._recording_start_time = 1.0
    app._last_waveform = None
    app._dictation_app = None
    app._agent_compose_recording = None
    app._agent_compose_pending_generation = None
    app._invalidate_live_preview = MagicMock()
    app._freeze_live_preview = MagicMock()
    app._restore_system_volume = MagicMock()
    app._close_audio_policy = MagicMock()
    app._play_recording_chime = MagicMock()
    app._broadcast = MagicMock()
    app._dispatch_error_dialog = MagicMock()
    app._acceptance_is_current = MagicMock(return_value=True)
    app.dictation_state_snapshot = MagicMock(
        return_value={
            "recording_id": 5,
            "phase": "recording",
            "screen_reader_detected": False,
        }
    )
    app._transition_dictation_state = MagicMock(return_value=True)
    app._executor = MagicMock()
    app.license_manager.status.return_value = {}
    app.license_manager.trial_expired = False
    app.keyboard.reset_mock()
    replacement = MagicMock()
    replacement.owner_epoch = 32

    with patch(
        "whisperstream.main.AudioCapture",
        return_value=replacement,
    ) as capture_factory:
        app.reload_config(replace(app.config, audio_input_device="Changed A"))
        app.reload_config(replace(app.config, audio_input_device="Changed B"))
        capture_factory.assert_not_called()
        active_capture.close.assert_not_called()
        assert app._recording is True

        with app._lock:
            if terminal == "normal":
                app._stop_recording()
            else:
                app._cancel_recording(trigger="hud")
        assert capture_factory.call_count == 0
        with app._lock:
            assert app._ensure_audio_capture_selection_locked()
        assert capture_factory.call_count == 1
    assert capture_factory.call_args.kwargs["device"] == "Changed B"
    active_capture.close.assert_called_once_with()
    assert app.audio_capture is replacement
    assert app._audio_capture_device == "Changed B"
    assert app._recording is False
    app._dispatch_error_dialog.assert_not_called()
    app.keyboard.reset_toggle_state.assert_not_called()
    if app._idle_timer is not None:
        app._idle_timer.cancel()
def test_managed_reload_reconciles_tampered_runtime_config(patched_app) -> None:
    """An internal reload cannot bypass the startup-only policy repair."""
    app, _ = patched_app
    app.managed_lockdown_policy = ManagedLockdownPolicy(
        enabled=True,
        state="enabled",
    )
    app.config.lockdown_mode = True
    app.config.history_enabled = False
    app.config.enhancement_provider = "local"
    tampered = replace(
        app.config,
        lockdown_mode=False,
        history_enabled=True,
        enhancement_enabled=True,
        enhancement_provider="ollama",
        enhancement_local_endpoint="https://attacker.invalid/v1",
        enhancement_ollama_endpoint="https://attacker.invalid/ollama",
    )

    app.reload_config(tampered)

    assert app.config.lockdown_mode is True
    assert app.config.history_enabled is False
    assert app.config.enhancement_enabled is False
    assert app.config.enhancement_provider == "local"
    assert app.config.enhancement_local_endpoint == ""
    assert app.config.effective_provider() == "local"


def test_superseded_startup_does_not_report_model_failure(patched_app):
    from whisperstream.main import SttActivationStaleError
    app, _ = patched_app
    app._broadcast = MagicMock()
    app._dispatch_error_dialog = MagicMock()
    app._load_stt_engine_blocking = MagicMock(side_effect=SttActivationStaleError("STT activation target is stale"))
    with patch("whisperstream.main.threading.Thread") as thread:
        thread.side_effect = lambda **kwargs: MagicMock(start=kwargs["target"])
        app.load_stt_engine_async()
    app._dispatch_error_dialog.assert_not_called()
    app._broadcast.assert_called_once_with({"type": "model.loading"})


def test_startup_missing_model_opens_download_state_without_failure_popup(patched_app):
    app, _ = patched_app
    app.parakeet = None
    app._broadcast = MagicMock()
    app._dispatch_error_dialog = MagicMock()
    app._load_stt_engine_blocking = MagicMock()
    with patch("whisperstream.main.threading.Thread") as thread:
        thread.side_effect = lambda **kwargs: MagicMock(start=kwargs["target"])
        app.load_stt_engine_async()
    app._dispatch_error_dialog.assert_not_called()
    assert app._broadcast.call_args_list[-1].args[0] == {
        "type": "model.missing", "model_name": app.configured_stt_artifact_id,
    }


def test_live_clipboard_preferences_preserve_injector(patched_app) -> None:
    app, _ = patched_app
    injector = app.text_injector
    app.reload_config(replace(app.config, clipboard_behavior=False, clipboard_clear_delay=0.2))
    assert app.text_injector is injector
    injector.update_clipboard_settings.assert_called_once_with(behavior=False, clear_delay=0.2)
    injector.close.assert_not_called()


def test_initialized_dictation_generations_reject_stale_terminal_state(patched_app):
    app, _ = patched_app
    app._broadcast = MagicMock()
    idle = app.dictation_state_snapshot()
    assert idle["phase"] == "idle"
    assert idle["recording_id"] == 0
    assert idle["origin"] is None
    idle["phase"] = "changed copy"
    assert app.dictation_state_snapshot()["phase"] == "idle"

    first = app._begin_dictation_state(origin="toggle", screen_reader_detected=False)
    assert app._transition_dictation_state(first, "processing") is True
    second = app._begin_dictation_state(origin="push_to_talk", screen_reader_detected=False)
    assert second > first
    assert app._transition_dictation_state(first, "idle") is False
    assert app.dictation_state_snapshot()["recording_id"] == second
    assert app.dictation_state_snapshot()["phase"] == "recording"
    assert app._transition_dictation_state(second, "idle") is True
    assert app.dictation_state_snapshot()["phase"] == "idle"


def test_reply_capture_lifecycle_names_its_compose_lease(patched_app):
    """The HUD must know a reply owns the recording from its first event."""
    from whisperstream.agent_compose import AgentComposeLeaseStore

    app, _ = patched_app
    app._broadcast = MagicMock()
    leases = AgentComposeLeaseStore()
    generation = leases.acquire("opaque-session", ttl_seconds=30).generation
    app._agent_compose_leases = leases
    app._agent_compose_pending_generation = generation

    recording_id = app._begin_dictation_state(origin="push_to_talk", screen_reader_detected=False)
    events = [call.args[0] for call in app._broadcast.call_args_list]
    assert events[0]["type"] == "dictation.state"
    assert events[0]["compose_generation"] == generation
    assert events[1]["type"] == "agent.compose.state"
    assert app._transition_dictation_state(recording_id, "processing") is True
    assert app.dictation_state_snapshot()["compose_generation"] == generation

    app._agent_compose_pending_generation = None
    app._begin_dictation_state(origin="push_to_talk", screen_reader_detected=False)
    assert "compose_generation" not in app.dictation_state_snapshot()


@pytest.mark.parametrize("model_present", [False, True])
def test_recording_starts_and_publishes_lifecycle_without_prepared_model(
    patched_app, accepted_eula, monkeypatch, model_present,
):
    import whisperstream.main as main_mod

    app, model = patched_app
    # The constructor predates accepted_eula's patch and captures its providers.
    monkeypatch.setattr(app, "_acceptance_grant_provider", main_mod.get_acceptance_grant)
    monkeypatch.setattr(app, "_acceptance_grant_revalidator", main_mod.acceptance_grant_is_current)
    app._recording_acceptance_grant = app._current_acceptance_grant()
    app.stt_engine = model if model_present else None
    app.config = replace(
        app.config, enhancement_enabled=False, show_live_preview=False,
        toggle_safety_timeout=0,
    )
    app.audio_capture = MagicMock(sample_rate=16000)
    app.audio_feedback = MagicMock()
    app._broadcast = MagicMock()
    executor = MagicMock()
    monkeypatch.setattr(app, "_executor", executor)
    monkeypatch.setattr("whisperstream.main.screen_reader_active", lambda: False)
    monkeypatch.setattr(app, "_capture_application_context", MagicMock(return_value=None))

    assert app._start_recording() is True

    assert app._recording is True
    app.audio_capture.start.assert_called_once_with(discard_until_armed=True)
    app.audio_capture.arm_capture.assert_called_once_with()
    state = app.dictation_state_snapshot()
    assert state["recording_id"] == 1
    assert state["phase"] == "recording"
    app._broadcast.assert_any_call(state)
    app._broadcast.assert_any_call({
        "type": "state.recording", "recording": True,
        "screen_reader_detected": False,
    })
    if model_present:
        executor.submit.assert_called_once_with(model.ensure_loaded)
    else:
        executor.submit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("outcome", ["success", "timeout", "error", "not_attempted"])
async def test_main_preserves_typed_outcomes_without_string_api(patched_app, chunked, outcome):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from whisperstream.enhancement_outcomes import EnhancementResult

    app, _ = patched_app
    result = EnhancementResult("Synthetic unchanged text", outcome)
    method = AsyncMock(return_value=result)
    method_name = "enhance_chunk_with_outcome" if chunked else "enhance_with_outcome"
    app.llm_enhancer = SimpleNamespace(**{method_name: method})
    timeout, error = MagicMock(), MagicMock()

    actual = await app._enhance_with_bounded_outcome(
        result.text, profile_id="chat", on_timeout=timeout, on_error=error, chunked=chunked,
    )

    assert actual is result
    method.assert_awaited_once_with(
        result.text, profile_id="chat", on_timeout=timeout, on_error=error,
    )
