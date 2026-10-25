"""Tests for the per-agent ``codex_verified``/``claude_verified`` config fields
(Task 1.2, agent-onboarding-improvements).

Covers two things:

1. Config round-trip/validate structure, modeled on
   ``tests/test_config_agent_speak_setup_completed.py`` (default/round-trip)
   and ``tests/test_config_validation.py`` (type-check via ``validate()``).
2. The ``server.py`` ``_handle_agent_event`` flip: a real ``kind=="response"``
   completion from a known agent sets that agent's ``*_verified`` flag True
   exactly once (no redundant ``config_manager.save`` on later completions),
   patterned on the endpoint tests in ``tests/test_agent_event_endpoint.py``
   (imports its client/mock helpers rather than re-rolling them).
"""

from __future__ import annotations

import unittest

import pytest

from whisperstream.config_manager import WhisperstreamConfig
from tests.test_agent_event_endpoint import (
    STOP_CLAUDE,
    _load_agent_event_fixture,
    _make_client,
    _make_controller,
    _make_mock_config,
)

pytestmark = pytest.mark.usefixtures("accepted_eula")


# ---------------------------------------------------------------------------
# Config structure: default / from_dict / to_dict / validate
# ---------------------------------------------------------------------------


class TestAgentVerifiedDefaults(unittest.TestCase):
    def test_codex_verified_defaults_false(self):
        self.assertFalse(WhisperstreamConfig().codex_verified)

    def test_claude_verified_defaults_false(self):
        self.assertFalse(WhisperstreamConfig().claude_verified)

    def test_default_validates(self):
        WhisperstreamConfig().validate()


class TestAgentVerifiedFromDict(unittest.TestCase):
    def test_from_dict_absent_key_stays_default(self):
        conf = WhisperstreamConfig.from_dict({})
        self.assertFalse(conf.codex_verified)
        self.assertFalse(conf.claude_verified)

    def test_from_dict_true(self):
        conf = WhisperstreamConfig.from_dict(
            {"codex_verified": True, "claude_verified": True}
        )
        self.assertTrue(conf.codex_verified)
        self.assertTrue(conf.claude_verified)

    def test_from_dict_independent_per_agent(self):
        """Verifying one agent must not flip the other."""
        conf = WhisperstreamConfig.from_dict({"claude_verified": True})
        self.assertTrue(conf.claude_verified)
        self.assertFalse(conf.codex_verified)


class TestAgentVerifiedToDict(unittest.TestCase):
    def test_to_dict_default_included(self):
        d = WhisperstreamConfig().to_dict()
        self.assertIn("codex_verified", d)
        self.assertIn("claude_verified", d)
        self.assertEqual(d["codex_verified"], False)
        self.assertEqual(d["claude_verified"], False)

    def test_round_trip_preserves_true(self):
        conf = WhisperstreamConfig(codex_verified=True, claude_verified=True)
        round_tripped = WhisperstreamConfig.from_dict(conf.to_dict())
        self.assertTrue(round_tripped.codex_verified)
        self.assertTrue(round_tripped.claude_verified)

    def test_round_trip_preserves_false(self):
        conf = WhisperstreamConfig(codex_verified=False, claude_verified=False)
        round_tripped = WhisperstreamConfig.from_dict(conf.to_dict())
        self.assertFalse(round_tripped.codex_verified)
        self.assertFalse(round_tripped.claude_verified)


class TestAgentVerifiedValidate(unittest.TestCase):
    def test_validate_rejects_non_bool_codex(self):
        conf = WhisperstreamConfig()
        conf.codex_verified = "yes"  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            conf.validate()

    def test_validate_rejects_non_bool_claude(self):
        conf = WhisperstreamConfig()
        conf.claude_verified = "yes"  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            conf.validate()


# ---------------------------------------------------------------------------
# server.py._handle_agent_event flip on kind=="response"
# ---------------------------------------------------------------------------


class TestAgentVerifiedFlipOnResponse:
    def test_claude_response_flips_claude_verified_and_saves_once(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        config = _make_mock_config(claude_verified=False)
        client, server = _make_client(config=config, speech_controller=controller)
        server.broadcast = lambda *_args, **_kwargs: None

        assert config.claude_verified is False

        resp = client.post("/api/claude-event", json=STOP_CLAUDE)

        assert resp.status_code == 200
        assert config.claude_verified is True
        server._app.config_manager.save.assert_called_once_with(config)

        # A later completion (with the flag already True) must not save again.
        server._app.config_manager.save.reset_mock()
        second = {
            **STOP_CLAUDE,
            "session_id": "claude-2",
            "prompt_id": "prompt-2",
        }
        resp2 = client.post("/api/claude-event", json=second)
        assert resp2.status_code == 200
        assert config.claude_verified is True
        server._app.config_manager.save.assert_not_called()

    def test_duplicate_response_does_not_flip_or_save(self, monkeypatch):
        """A retried duplicate completion returns early before the flip."""
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        config = _make_mock_config(claude_verified=False)
        client, server = _make_client(config=config, speech_controller=controller)
        server.broadcast = lambda *_args, **_kwargs: None

        first = client.post("/api/claude-event", json=STOP_CLAUDE)
        assert first.status_code == 200
        assert config.claude_verified is True
        server._app.config_manager.save.assert_called_once_with(config)
        server._app.config_manager.save.reset_mock()

        retry = client.post("/api/claude-event", json=dict(STOP_CLAUDE))
        assert retry.status_code == 200
        # Already verified from the first post; a duplicate retry saves nothing.
        server._app.config_manager.save.assert_not_called()

    def test_already_verified_claude_never_saves(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        config = _make_mock_config(claude_verified=True)
        client, server = _make_client(config=config, speech_controller=controller)
        server.broadcast = lambda *_args, **_kwargs: None

        resp = client.post("/api/claude-event", json=STOP_CLAUDE)

        assert resp.status_code == 200
        assert config.claude_verified is True
        server._app.config_manager.save.assert_not_called()

    def test_codex_response_flips_codex_verified_and_saves_once(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        config = _make_mock_config(codex_hud_enabled=True, codex_verified=False)
        client, server = _make_client(config=config, speech_controller=controller)
        server.broadcast = lambda *_args, **_kwargs: None

        codex_stop = _load_agent_event_fixture("codex_stop.json")

        assert config.codex_verified is False

        resp = client.post("/api/agent-event?agent=codex", json=codex_stop)

        assert resp.status_code == 200
        assert config.codex_verified is True
        server._app.config_manager.save.assert_called_once_with(config)

        # Claude's flag stays untouched by a Codex completion.
        assert config.claude_verified is not True

    def test_codex_response_does_not_touch_claude_verified(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        config = _make_mock_config(
            codex_hud_enabled=True, codex_verified=False, claude_verified=False
        )
        client, server = _make_client(config=config, speech_controller=controller)
        server.broadcast = lambda *_args, **_kwargs: None

        codex_stop = _load_agent_event_fixture("codex_stop.json")
        resp = client.post("/api/agent-event?agent=codex", json=codex_stop)

        assert resp.status_code == 200
        assert config.codex_verified is True
        assert config.claude_verified is False


if __name__ == "__main__":
    unittest.main()
