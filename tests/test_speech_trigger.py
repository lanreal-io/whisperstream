"""Tests for marked-summary speech and chime suppression.

TDD: written before the implementation change to server.py's ``kind ==
"response"`` branch. Covers:

1. Master switch, activation, a valid final ``🔊 `` line, voice availability,
   and a non-empty turn drive which of {speak, chime} fires, never both.
2. Marked summaries bypass the retired local summary warm-up path.
3. The ``needs_input`` branch is unaffected.
4. Empty-turn responses never speak.

Uses FastAPI's TestClient with a mock WhisperstreamApp (mirrors
test_speech_endpoints.py / test_claude_hud_events.py). No real sherpa-onnx /
sounddevice / llama-server subprocess is touched -- LlamaServerSupervisor
construction is patched per test_models_relocate.py's
``TestReloadModels.test_reload_creates_fresh_supervisor_when_none`` precedent.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


pytestmark = pytest.mark.usefixtures("accepted_eula")
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_speech_endpoints.py / test_claude_hud_events.py)
# ---------------------------------------------------------------------------


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.enhancement_provider = "none"
    cfg.enhancement_local_model = "gemma4-e2b-instruct"
    cfg.enhancement_local_endpoint = ""
    cfg.enhancement_api_key = ""
    cfg.enhancement_local_idle_timeout = 600.0
    cfg.enhancement_local_gpu_verdict = "not_run"
    cfg.enhancement_local_gpu_device_index = -1
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.parakeet_quantization = "int8"
    cfg.word_boosts = []
    cfg.threads = None
    cfg.model_timeout = 0.0
    cfg.hotword_score = 1.5
    cfg.beam_width = 4
    cfg.claude_hud_enabled = True
    cfg.agent_speech_enabled = True
    cfg.agent_speech_activation = "always_on"
    cfg.agent_speech_summary_source = "agent"
    cfg.agent_speech_mode = "verbatim"
    cfg.start_sound_path = None
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.to_dict = lambda: {}
    return cfg


def _make_mock_app(*, config=None, speech_controller=None, llama_server=None) -> MagicMock:
    app = MagicMock()
    app.config = config or _make_mock_config()
    app.stt_engine = MagicMock()
    app.llama_server = llama_server
    app.config_manager = MagicMock()
    app.config_manager.save = MagicMock()
    app.reload_config = MagicMock()
    app.license_manager = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True, "trial_remaining_seconds": 0}
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.load_stt_engine_async = MagicMock()
    app.set_event_broadcaster = MagicMock()
    app.audio_feedback = MagicMock()
    # Explicit, never a MagicMock auto-vivification -- see test_speech_endpoints.py.
    app.speech_controller = speech_controller
    return app


def _make_client(
    *, config=None, speech_controller=None, llama_server=None
) -> tuple[TestClient, Any]:
    from whisperstream.server import WhisperstreamServer

    mock_app = _make_mock_app(
        config=config, speech_controller=speech_controller, llama_server=llama_server
    )
    server = WhisperstreamServer(mock_app, port=0, logger=None, register_hotkeys=False)
    client = TestClient(server.fastapi, raise_server_exceptions=True, headers={"x-whisperstream-hook-token": server._agent_hook_token})
    return client, server


def _make_controller(*, can_speak=True) -> MagicMock:
    def accept_speech(*_args, on_terminal, **_kwargs):
        on_terminal("completed")
        return True

    controller = MagicMock()
    controller.can_speak.return_value = can_speak
    controller.speak_response = AsyncMock(return_value=None)
    controller.speak_spoken_summary = MagicMock(side_effect=accept_speech)
    return controller


STOP_BODY_WITH_TEXT = {
    "hook_event_name": "Stop",
    "last_assistant_message": "All tests pass.\n🔊 All tests pass.",
}
STOP_BODY_EMPTY = {
    "hook_event_name": "Stop",
    "last_assistant_message": "",
}
NOTIFICATION_NEEDS_INPUT = {
    "hook_event_name": "Notification",
    "message": "permission_prompt",
}


# ---------------------------------------------------------------------------
# Master switch: speech vs chime (mutually exclusive)
# ---------------------------------------------------------------------------


class TestSpeechVsChime:
    def test_verbatim_mode_speaks_and_suppresses_chime(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        config = _make_mock_config(claude_hud_enabled=True, agent_speech_mode="verbatim")
        client, server = _make_client(config=config, speech_controller=controller)

        resp = client.post("/api/claude-event", json=STOP_BODY_WITH_TEXT)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        controller.speak_spoken_summary.assert_called_once()
        args, kwargs = controller.speak_spoken_summary.call_args
        assert args == ("All tests pass.",)
        assert (
            kwargs["completion_id"]
            == server._agent_activity.snapshot()[0].completion_id
        )
        assert callable(kwargs["on_terminal"])
        controller.speak_response.assert_not_called()
        server._app.audio_feedback.play_start.assert_not_called()

    def test_mode_off_plays_chime_and_no_speech(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        config = _make_mock_config(
            claude_hud_enabled=True,
            agent_speech_enabled=False,
            agent_speech_mode="off",
        )
        client, server = _make_client(config=config, speech_controller=controller)

        resp = client.post("/api/claude-event", json=STOP_BODY_WITH_TEXT)

        assert resp.status_code == 200
        controller.speak_response.assert_not_called()
        controller.speak_spoken_summary.assert_not_called()
        server._app.audio_feedback.play_start.assert_called_once()

    def test_model_missing_plays_chime_and_no_speech(self, monkeypatch):
        """can_speak() == False (voice model not installed) -> chime, no speech."""
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=False)
        config = _make_mock_config(claude_hud_enabled=True, agent_speech_mode="verbatim")
        client, server = _make_client(config=config, speech_controller=controller)

        resp = client.post("/api/claude-event", json=STOP_BODY_WITH_TEXT)

        assert resp.status_code == 200
        controller.speak_response.assert_not_called()
        controller.speak_spoken_summary.assert_not_called()
        server._app.audio_feedback.play_start.assert_called_once()

    def test_empty_turn_plays_chime_and_no_speech(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        config = _make_mock_config(claude_hud_enabled=True, agent_speech_mode="verbatim")
        client, server = _make_client(config=config, speech_controller=controller)

        resp = client.post("/api/claude-event", json=STOP_BODY_EMPTY)

        assert resp.status_code == 200
        controller.speak_response.assert_not_called()
        controller.speak_spoken_summary.assert_not_called()
        server._app.audio_feedback.play_start.assert_called_once()

    def test_no_controller_plays_chime_and_never_raises(self, monkeypatch):
        """Gate on but speech_controller is None -- must not raise; chime plays."""
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        config = _make_mock_config(claude_hud_enabled=True, agent_speech_mode="verbatim")
        client, server = _make_client(config=config, speech_controller=None)

        resp = client.post("/api/claude-event", json=STOP_BODY_WITH_TEXT)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        server._app.audio_feedback.play_start.assert_called_once()

    def test_free_text_notification_cannot_create_needs_input(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        config = _make_mock_config(claude_hud_enabled=True, agent_speech_mode="verbatim")
        client, server = _make_client(config=config, speech_controller=controller)

        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        resp = client.post("/api/claude-event", json=NOTIFICATION_NEEDS_INPUT)

        assert resp.status_code == 200
        assert broadcasts == []
        controller.speak_response.assert_not_called()
        controller.speak_spoken_summary.assert_not_called()
        server._app.audio_feedback.play_start.assert_not_called()


# ---------------------------------------------------------------------------
# Marked summaries bypass the retired local summary warm-up path
# ---------------------------------------------------------------------------


class TestWarmKick:
    def test_summary_mode_does_not_touch_existing_supervisor(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        supervisor = MagicMock()
        config = _make_mock_config(claude_hud_enabled=True, agent_speech_mode="summary")
        client, server = _make_client(
            config=config, speech_controller=controller, llama_server=supervisor
        )
        sentinel_loop = object()
        server._loop = sentinel_loop

        resp = client.post("/api/claude-event", json=STOP_BODY_WITH_TEXT)

        assert resp.status_code == 200
        supervisor.kick_warm.assert_not_called()
        supervisor.mark_active.assert_not_called()
        assert server._app.llama_server is supervisor  # reused, not replaced
        controller.speak_spoken_summary.assert_called_once()
        controller.speak_response.assert_not_called()

    def test_verbatim_mode_never_touches_supervisor(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        supervisor = MagicMock()
        config = _make_mock_config(claude_hud_enabled=True, agent_speech_mode="verbatim")
        client, server = _make_client(
            config=config, speech_controller=controller, llama_server=supervisor
        )
        server._loop = object()

        resp = client.post("/api/claude-event", json=STOP_BODY_WITH_TEXT)

        assert resp.status_code == 200
        supervisor.kick_warm.assert_not_called()
        supervisor.mark_active.assert_not_called()

    def test_summary_mode_does_not_construct_lazy_supervisor_when_missing(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        config = _make_mock_config(
            claude_hud_enabled=True,
            agent_speech_mode="summary",
            enhancement_local_model="gemma4-e2b-instruct",
        )
        client, server = _make_client(
            config=config, speech_controller=controller, llama_server=None
        )
        server._loop = object()

        fake_binary = tmp_path / "llama-server.exe"
        fake_binary.write_bytes(b"exe")
        (tmp_path / "gemma.gguf").write_bytes(b"gguf")

        mock_supervisor_cls = MagicMock()
        mock_supervisor_instance = MagicMock()
        mock_supervisor_cls.return_value = mock_supervisor_instance

        with (
            patch(
                "whisperstream.llama_server.find_bundled_llama_server",
                return_value=fake_binary,
            ),
            patch("whisperstream.llama_server.LlamaServerSupervisor", mock_supervisor_cls),
            patch(
                "whisperstream.enhancement_models.get_local_bundled_model",
                return_value={"gguf_filename": "gemma.gguf"},
            ),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=tmp_path),
        ):
            resp = client.post("/api/claude-event", json=STOP_BODY_WITH_TEXT)

        assert resp.status_code == 200
        mock_supervisor_cls.assert_not_called()
        assert server._app.llama_server is None
        mock_supervisor_instance.kick_warm.assert_not_called()
        mock_supervisor_instance.mark_active.assert_not_called()
        controller.speak_spoken_summary.assert_called_once()
        controller.speak_response.assert_not_called()

    def test_summary_mode_skips_warm_kick_when_binary_missing(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        config = _make_mock_config(claude_hud_enabled=True, agent_speech_mode="summary")
        client, server = _make_client(
            config=config, speech_controller=controller, llama_server=None
        )
        server._loop = object()

        with patch(
            "whisperstream.llama_server.find_bundled_llama_server", return_value=None
        ):
            resp = client.post("/api/claude-event", json=STOP_BODY_WITH_TEXT)

        assert resp.status_code == 200
        assert server._app.llama_server is None
        controller.speak_spoken_summary.assert_called_once()
        controller.speak_response.assert_not_called()

    def test_summary_mode_skips_warm_kick_when_gguf_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        config = _make_mock_config(
            claude_hud_enabled=True,
            agent_speech_mode="summary",
            enhancement_local_model="gemma4-e2b-instruct",
        )
        client, server = _make_client(
            config=config, speech_controller=controller, llama_server=None
        )
        server._loop = object()

        fake_binary = tmp_path / "llama-server.exe"
        fake_binary.write_bytes(b"exe")

        with (
            patch(
                "whisperstream.llama_server.find_bundled_llama_server",
                return_value=fake_binary,
            ),
            patch(
                "whisperstream.enhancement_models.get_local_bundled_model",
                return_value={"gguf_filename": "missing.gguf"},
            ),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=tmp_path),
        ):
            resp = client.post("/api/claude-event", json=STOP_BODY_WITH_TEXT)

        assert resp.status_code == 200
        assert server._app.llama_server is None
        controller.speak_spoken_summary.assert_called_once()
        controller.speak_response.assert_not_called()

    def test_summary_mode_skips_warm_kick_when_model_id_unknown(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        config = _make_mock_config(
            claude_hud_enabled=True,
            agent_speech_mode="summary",
            enhancement_local_model="",
        )
        client, server = _make_client(
            config=config, speech_controller=controller, llama_server=None
        )
        server._loop = object()

        with (
            patch(
                "whisperstream.llama_server.find_bundled_llama_server",
                return_value=None,
            ),
        ):
            resp = client.post("/api/claude-event", json=STOP_BODY_WITH_TEXT)

        assert resp.status_code == 200
        assert server._app.llama_server is None
        controller.speak_spoken_summary.assert_called_once()
        controller.speak_response.assert_not_called()

    def test_warm_kick_error_never_raises(self, monkeypatch):
        """A raising supervisor lookup must not break the fire-and-forget contract."""
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        supervisor = MagicMock()
        supervisor.kick_warm.side_effect = RuntimeError("boom")
        config = _make_mock_config(claude_hud_enabled=True, agent_speech_mode="summary")
        client, server = _make_client(
            config=config, speech_controller=controller, llama_server=supervisor
        )
        server._loop = object()

        resp = client.post("/api/claude-event", json=STOP_BODY_WITH_TEXT)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        supervisor.kick_warm.assert_not_called()
        controller.speak_spoken_summary.assert_called_once()
        controller.speak_response.assert_not_called()


# ---------------------------------------------------------------------------
# name_prefix threading (Task 2.3): only a user-chosen name is spoken
# ---------------------------------------------------------------------------

STOP_BODY_NAMED = {
    "hook_event_name": "Stop",
    "last_assistant_message": "All tests pass.\n🔊 All tests pass.",
    "session_id": "claude-named-session",
}
NAME_BODY = {
    "hook_event_name": "UserPromptExpansion",
    "expansion_type": "slash_command",
    "command_source": "plugin",
    "command_name": "whisperstream:name",
    "prompt": "/whisperstream:name speak-harness",
    "session_id": "claude-named-session",
}


class TestNamePrefixResponseSpeech:
    def test_named_session_response_gets_name_prefix(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        config = _make_mock_config(claude_hud_enabled=True, agent_speech_mode="verbatim")
        client, server = _make_client(config=config, speech_controller=controller)

        assert client.post("/api/claude-event", json=NAME_BODY).status_code == 200

        resp = client.post("/api/claude-event", json=STOP_BODY_NAMED)

        assert resp.status_code == 200
        controller.speak_spoken_summary.assert_called_once()
        _args, kwargs = controller.speak_spoken_summary.call_args
        assert kwargs["name_prefix"] == "speak-harness"

    def test_unnamed_session_response_gets_no_name_prefix(self, monkeypatch):
        """The project-basename fallback / literal Claude are never spoken."""
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller(can_speak=True)
        config = _make_mock_config(claude_hud_enabled=True, agent_speech_mode="verbatim")
        client, server = _make_client(config=config, speech_controller=controller)

        resp = client.post("/api/claude-event", json=STOP_BODY_WITH_TEXT)

        assert resp.status_code == 200
        controller.speak_spoken_summary.assert_called_once()
        _args, kwargs = controller.speak_spoken_summary.call_args
        assert kwargs["name_prefix"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
