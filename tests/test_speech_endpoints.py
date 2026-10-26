"""Tests for the agent-speech ``/api/speech/*`` endpoint family (Phase 2, Task 2.2).

Covers:

1. Always registered (since 1.3.0, when the experimental 404 gate was removed):
   no route 404s even with the shipped default-off config or a production
   ``EXPERIMENTAL_AVAILABLE=False`` build, yet the feature stays inert -- the
   speech-producing routes are no-ops without a controller and the heavy TTS
   runtime (sherpa-onnx / sounddevice) is never imported.
2. ``GET /api/speech/status`` reports one structured, memory-aware selection
   snapshot with exact per-engine sizes, process health reasons, and concrete
   download recommendations.
3. ``POST /api/speech/download/start`` requires a concrete engine, preserves
   the shared thread/cancel-event guard, and tags every TTS event with that
   engine identity.
4. ``POST /api/speech/speak-sample`` accepts an explicit engine/voice so a
   debounced config save cannot redirect the preview; stop and compatible empty
   sample requests remain fire-and-forget no-ops without a controller.
5. ``POST /api/speech/speak-text`` (Task 1.2, onboarding voice-speed) speaks
   caller-submitted transcript-class text: strips and rejects empty/whitespace,
   caps length at ``SPEAK_TEXT_MAX_CHARS``, validates an explicit voice against
   the engine spec, and never logs the request text.

Uses FastAPI's TestClient with a mock WhisperstreamApp (mirrors
``test_models_relocate.py`` / ``test_experimental_gate.py``). No real
sherpa-onnx / sounddevice / network I/O.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("accepted_eula")


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_models_relocate.py / test_experimental_gate.py)
# ---------------------------------------------------------------------------


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.enhancement_provider = "none"
    cfg.enhancement_local_model = ""
    cfg.enhancement_local_endpoint = ""
    cfg.enhancement_api_key = ""
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.parakeet_quantization = "int8"
    cfg.word_boosts = []
    cfg.threads = None
    cfg.model_timeout = 0.0
    cfg.hotword_score = 1.5
    cfg.beam_width = 4
    cfg.agent_speech_engine = "auto"
    cfg.agent_speech_voice = 1
    cfg.agent_speech_kokoro_voice = "af_heart"
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.to_dict = lambda: {}
    return cfg


def _make_mock_app(*, config=None, speech_controller=None) -> MagicMock:
    app = MagicMock()
    app.config = config or _make_mock_config()
    app.stt_engine = MagicMock()
    app.llama_server = None
    app.config_manager = MagicMock()
    app.config_manager.save = MagicMock()
    app.reload_config = MagicMock()
    app.license_manager = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True, "trial_remaining_seconds": 0}
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.load_stt_engine_async = MagicMock()
    app.set_event_broadcaster = MagicMock()
    # Explicit, never a MagicMock auto-vivification: main.py sets this to None
    # unconditionally off-gate (Task 2.1), and the endpoints' getattr fallback
    # is exercised precisely by leaving it None here.
    app.speech_controller = speech_controller
    return app


def _make_client(*, config=None, speech_controller=None) -> tuple[TestClient, Any]:
    from whisperstream.server import WhisperstreamServer

    mock_app = _make_mock_app(config=config, speech_controller=speech_controller)
    server = WhisperstreamServer(mock_app, port=0, logger=None, register_hotkeys=False)
    client = TestClient(server.fastapi, raise_server_exceptions=True)
    return client, server


@pytest.fixture(autouse=True)
def windows_speech_runtime_contract(monkeypatch):
    # Existing cases exercise both Windows engines on every host. The explicit
    # Darwin cases below replace this seam to verify native support separately.
    monkeypatch.setattr("whisperstream.server.routes.speech.speech_engine_supported", lambda _key: True)


SPEECH_ROUTES = [
    ("get", "/api/speech/status"),
    ("get", "/api/speech/voices"),
    ("post", "/api/speech/download/start"),
    ("post", "/api/speech/stop"),
    ("post", "/api/speech/speak-sample"),
    ("post", "/api/speech/speak-text"),
]


def test_speech_routes_use_replaced_config_and_controller():
    original_controller = MagicMock()
    client, server = _make_client(speech_controller=original_controller)
    replacement_controller = MagicMock()
    server._app.config = _make_mock_config(agent_speech_voice=3)
    server._app.speech_controller = replacement_controller

    response = client.post("/api/speech/speak-sample", json={"engine": "supertonic"})
    assert response.status_code == 200
    replacement_controller.speak_sample.assert_called_once_with(
        engine="supertonic", voice=3,
    )
    original_controller.speak_sample.assert_not_called()

    assert client.post("/api/speech/stop").status_code == 200
    replacement_controller.stop.assert_called_once_with()
    original_controller.stop.assert_not_called()


# The TTS runtime: sherpa-onnx / sounddevice / model files live behind these.
# `engine_specs` is deliberately excluded -- it is the sherpa-free static
# metadata module the status/voices routes are allowed to import.
HEAVY_SPEECH_MODULES = (
    "whisperstream.speech.engines",
    "whisperstream.speech.supertonic_engine",
    "whisperstream.speech.kokoro_engine",
    "whisperstream.speech.kokoro_frontend",
    "whisperstream.speech.controller",
    "whisperstream.speech.playback",
    "whisperstream.speech.summarizer",
)


def _imported_heavy_speech_modules() -> list[str]:
    return [name for name in HEAVY_SPEECH_MODULES if name in sys.modules]


# ---------------------------------------------------------------------------
# Always registered (1.3.0) — reachable, but inert until config turns it on
# ---------------------------------------------------------------------------


class TestAlwaysRegisteredAndInert:
    """The experimental 404 gate was removed in 1.3.0, so every
    ``/api/speech/*`` route is registered in every build. The real gate is user
    config (``agent_speech_enabled`` / ``claude_hud_enabled``, both default-off)
    plus the absence of a live controller. These tests pin BOTH halves: the
    routes answer instead of 404ing, and with the shipped defaults they do
    nothing -- no speech is produced and the heavy TTS runtime stays unimported.
    """

    def test_no_route_404s_with_default_off_config(self, monkeypatch):
        monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)
        client, _server = _make_client(
            config=_make_mock_config(
                agent_speech_enabled=False, claude_hud_enabled=False
            ),
        )

        for method, route in SPEECH_ROUTES:
            resp = getattr(client, method)(route)
            assert resp.status_code != 404, (
                f"{method.upper()} {route} 404'd -- the route must always be "
                f"registered since 1.3.0"
            )

    def test_build_flag_off_does_not_gate_the_routes(self, monkeypatch):
        """Agent Speak ships in production builds now, so a compiled
        EXPERIMENTAL_AVAILABLE=False must NOT make the routes disappear. The
        build flag survives only for future unfinished features."""
        monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)
        monkeypatch.setattr("whisperstream._build_flags.EXPERIMENTAL_AVAILABLE", False)
        client, _server = _make_client(
            config=_make_mock_config(agent_speech_engine="supertonic")
        )

        with patch("whisperstream.server.routes.speech.is_present", return_value=False):
            resp = client.get("/api/speech/status")
        assert resp.status_code == 200
        assert resp.json()["engine"] == "supertonic"

    def test_stop_and_sample_are_noops_without_a_controller(self, monkeypatch):
        """With speech off, main.py leaves no live controller to drive, so the
        two speech-producing routes must be silent no-ops, never a 500."""
        monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)
        client, _server = _make_client(
            config=_make_mock_config(agent_speech_enabled=False),
            speech_controller=None,
        )

        for route in ("/api/speech/stop", "/api/speech/speak-sample"):
            resp = client.post(route)
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}

    def test_no_heavy_speech_module_imported(self, monkeypatch):
        """Reading status/voices may touch the sherpa-free ``engine_specs``, but
        must never pull in the TTS runtime (sherpa-onnx / sounddevice / model
        loading). Excludes download/start, which legitimately does work."""
        monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)
        saved = {
            k: v
            for k, v in list(sys.modules.items())
            if k in HEAVY_SPEECH_MODULES
        }
        for k in saved:
            del sys.modules[k]
        try:
            client, _server = _make_client(
                config=_make_mock_config(
                    agent_speech_enabled=False, claude_hud_enabled=False
                ),
                speech_controller=None,
            )
            for method, route in SPEECH_ROUTES:
                if route == "/api/speech/download/start":
                    continue
                getattr(client, method)(route)
            assert _imported_heavy_speech_modules() == []
        finally:
            sys.modules.update(saved)


# ---------------------------------------------------------------------------
# Gate ON — GET /api/speech/status
# ---------------------------------------------------------------------------


class TestSpeechStatus:
    def test_auto_high_memory_reports_structured_missing_state(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, _server = _make_client()

        with (
            patch("whisperstream.server.routes.speech.is_present", return_value=False),
            patch("whisperstream.system_info.get_total_ram_mb", return_value=32_768),
            patch(
                "whisperstream.system_info.get_available_ram_mb",
                return_value=8_192,
            ),
        ):
            resp = client.get("/api/speech/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["requested"] == "auto"
        assert body["preferred_engine"] == "kokoro"
        assert body["resolved_engine"] is None
        assert body["active_engine"] is None
        assert body["reason"] == "missing_kokoro"
        assert body["memory"] == {
            "total_mb": 32_768,
            "available_mb": 8_192,
            "eligible": True,
        }
        assert body["kokoro_memory_tier"] == "recommended"
        # Automatic downloads both models, Supertonic first.
        assert body["recommended_downloads"] == ["supertonic", "kokoro"]
        assert body["current_download"] is None
        assert body["engines"]["supertonic"] == {
            "key": "supertonic",
            "label": "Supertonic 3",
            "supported": True,
            "installed": False,
            "available": False,
            "unavailable_reason": "model_unavailable",
            "downloading": False,
            "download_bytes": 128_774_318,
            "installed_bytes": 145_316_356,
        }
        assert body["engines"]["kokoro"]["download_bytes"] == 360_545_851
        assert body["engines"]["kokoro"]["installed_bytes"] == 360_545_851

    def test_installed_supertonic_with_missing_mac_runtime_is_not_resolved(self):
        class HealthManager:
            @staticmethod
            def is_healthy(engine_key):
                return engine_key != "supertonic"

            @staticmethod
            def health_reason(engine_key):
                return "runtime_unavailable" if engine_key == "supertonic" else None

        class Controller:
            def __init__(self):
                self._engine_manager = HealthManager()

            @staticmethod
            def active_engine():
                return None

        client, _server = _make_client(
            config=_make_mock_config(agent_speech_engine="supertonic"),
            speech_controller=Controller(),
        )
        with (
            patch("whisperstream.server.routes.speech.is_present", return_value=True),
            patch("whisperstream.system_info.get_total_ram_mb", return_value=8_192),
            patch("whisperstream.system_info.get_available_ram_mb", return_value=4_096),
        ):
            body = client.get("/api/speech/status").json()

        assert body["engines"]["supertonic"] == {
            "key": "supertonic",
            "label": "Supertonic 3",
            "supported": True,
            "installed": True,
            "available": False,
            "unavailable_reason": "runtime_unavailable",
            "downloading": False,
            "download_bytes": 128_774_318,
            "installed_bytes": 145_316_356,
        }
        assert body["resolved_engine"] is None
        assert body["reason"] == "runtime_unavailable"
        assert body["recommended_downloads"] == []

    @pytest.mark.parametrize(
        ("total_mb", "available_mb", "reason", "tier"),
        [
            (32_768, 1_024, "low_memory", "caution"),
            (4_096, 512, "low_memory", "blocked"),
            (32_768, None, "unknown_memory", "unknown"),
        ],
    )
    def test_auto_memory_fallback_reasons(
        self, monkeypatch, total_mb, available_mb, reason, tier
    ):
        client, _server = _make_client()

        def installed(model):
            from whisperstream.model_downloader import ModelId

            return model is ModelId.SUPERTONIC_3_INT8

        with (
            patch("whisperstream.server.routes.speech.is_present", side_effect=installed),
            patch(
                "whisperstream.system_info.get_total_ram_mb",
                return_value=total_mb,
            ),
            patch(
                "whisperstream.system_info.get_available_ram_mb",
                return_value=available_mb,
            ),
        ):
            body = client.get("/api/speech/status").json()

        assert body["preferred_engine"] == "supertonic"
        assert body["resolved_engine"] == "supertonic"
        assert body["active_engine"] is None
        assert body["reason"] == reason
        assert body["kokoro_memory_tier"] == tier
        # Automatic still recommends the missing Kokoro for later.
        assert body["recommended_downloads"] == ["kokoro"]

    def test_status_reports_a_live_low_memory_hold_without_starting_one(self):
        class HealthManager:
            hold = True

            @staticmethod
            def is_healthy(_engine_key):
                return True

            @staticmethod
            def health_reason(_engine_key):
                return None

            @staticmethod
            def is_resident(_engine_key):
                return False

            @classmethod
            def low_memory_hold_active(cls):
                return cls.hold

            @staticmethod
            def hold_low_memory_fallback():
                raise AssertionError("status must not start a hold")

        class Controller:
            def __init__(self):
                self._engine_manager = HealthManager()

        client, _server = _make_client(speech_controller=Controller())
        with (
            patch("whisperstream.server.routes.speech.is_present", return_value=True),
            patch("whisperstream.system_info.get_total_ram_mb", return_value=32_768),
            patch("whisperstream.system_info.get_available_ram_mb", return_value=8_192),
        ):
            held = client.get("/api/speech/status").json()
            HealthManager.hold = False
            free = client.get("/api/speech/status").json()

        assert (held["resolved_engine"], held["reason"]) == ("supertonic", "low_memory")
        assert (free["resolved_engine"], free["reason"]) == ("kokoro", "kokoro")

    def test_auto_uses_kokoro_when_both_installed_and_memory_eligible(self):
        client, _server = _make_client()
        with (
            patch("whisperstream.server.routes.speech.is_present", return_value=True),
            patch("whisperstream.system_info.get_total_ram_mb", return_value=32_768),
            patch(
                "whisperstream.system_info.get_available_ram_mb",
                return_value=8_192,
            ),
        ):
            body = client.get("/api/speech/status").json()
        assert body["resolved_engine"] == "kokoro"
        assert body["active_engine"] is None
        assert body["reason"] == "kokoro"
        assert body["recommended_downloads"] == []

    def test_status_reports_actual_active_playback_separately_from_resolution(self):
        class Controller:
            @staticmethod
            def active_engine():
                return "supertonic"

        client, _server = _make_client(speech_controller=Controller())
        with (
            patch("whisperstream.server.routes.speech.is_present", return_value=True),
            patch("whisperstream.system_info.get_total_ram_mb", return_value=32_768),
            patch(
                "whisperstream.system_info.get_available_ram_mb",
                return_value=8_192,
            ),
        ):
            body = client.get("/api/speech/status").json()

        assert body["resolved_engine"] == "kokoro"
        assert body["active_engine"] == "supertonic"

    @pytest.mark.parametrize(
        ("health_reason", "expected_reason"),
        [
            ("load_error", "unhealthy_kokoro"),
            ("slow_first_sentence", "slow_kokoro"),
        ],
    )
    def test_auto_reports_live_kokoro_health_reason(
        self, health_reason, expected_reason
    ):
        class HealthManager:
            @staticmethod
            def is_installed(_engine_key):
                return True

            @staticmethod
            def is_available(engine_key):
                return engine_key == "supertonic"

            @staticmethod
            def is_healthy(engine_key):
                return engine_key != "kokoro"

            @staticmethod
            def health_reason(engine_key):
                return health_reason if engine_key == "kokoro" else None

        class Controller:
            def __init__(self):
                self._engine_manager = HealthManager()

        client, _server = _make_client(speech_controller=Controller())
        with (
            patch("whisperstream.server.routes.speech.is_present", return_value=True),
            patch("whisperstream.system_info.get_total_ram_mb", return_value=32_768),
            patch(
                "whisperstream.system_info.get_available_ram_mb",
                return_value=8_192,
            ),
        ):
            body = client.get("/api/speech/status").json()

        assert body["resolved_engine"] == "supertonic"
        assert body["reason"] == expected_reason

    def test_explicit_kokoro_preserves_health_reason_while_allowing_retry(self):
        class HealthManager:
            @staticmethod
            def is_installed(_engine_key):
                return True

            @staticmethod
            def is_available(engine_key):
                return engine_key == "supertonic"

            @staticmethod
            def is_healthy(engine_key):
                return engine_key != "kokoro"

            @staticmethod
            def health_reason(engine_key):
                return "load_failed" if engine_key == "kokoro" else None

        class Controller:
            def __init__(self):
                self._engine_manager = HealthManager()

        client, _server = _make_client(
            config=_make_mock_config(agent_speech_engine="kokoro"),
            speech_controller=Controller(),
        )
        with (
            patch("whisperstream.server.routes.speech.is_present", return_value=True),
            patch("whisperstream.system_info.get_total_ram_mb", return_value=32_768),
            patch(
                "whisperstream.system_info.get_available_ram_mb",
                return_value=8_192,
            ),
        ):
            body = client.get("/api/speech/status").json()

        assert body["resolved_engine"] == "kokoro"
        assert body["active_engine"] is None
        assert body["reason"] == "unhealthy_kokoro"

    def test_explicit_missing_engines_are_not_redirected(self):
        for engine, reason in (
            ("kokoro", "missing_kokoro"),
            ("supertonic", "missing_supertonic"),
        ):
            client, _server = _make_client(
                config=_make_mock_config(agent_speech_engine=engine)
            )
            with patch("whisperstream.server.routes.speech.is_present", return_value=False):
                body = client.get("/api/speech/status").json()
            assert body["preferred_engine"] == engine
            assert body["resolved_engine"] is None
            assert body["reason"] == reason
            assert body["recommended_downloads"] == [engine]

    def test_downloading_true_while_tts_thread_active(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, server = _make_client()

        server._tts_download_engine = "kokoro"
        alive_thread = MagicMock()
        alive_thread.is_alive.return_value = True
        server._download_thread = alive_thread

        resp = client.get("/api/speech/status")
        assert resp.status_code == 200
        assert resp.json()["downloading"] is True
        assert resp.json()["current_download"] == "kokoro"
        assert resp.json()["engines"]["kokoro"]["downloading"] is True

    def test_downloading_false_when_thread_active_flag_unset(self, monkeypatch):
        """A different (non-tts) download in flight must not read as tts downloading."""
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, server = _make_client()

        alive_thread = MagicMock()
        alive_thread.is_alive.return_value = True
        server._download_thread = alive_thread
        server._tts_download_engine = None  # e.g. a Parakeet/LLM download instead

        resp = client.get("/api/speech/status")
        assert resp.status_code == 200
        assert resp.json()["downloading"] is False
        assert resp.json()["current_download"] is None


# ---------------------------------------------------------------------------
# Gate ON — GET /api/speech/voices
# ---------------------------------------------------------------------------


class TestSpeechVoices:
    def test_lists_supertonic_voices_by_default(self, monkeypatch):
        """Default engine (supertonic) exposes its 10 preset speakers by sid."""
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, _server = _make_client()

        resp = client.get("/api/speech/voices")
        assert resp.status_code == 200
        body = resp.json()
        assert body["engine"] == "supertonic"
        assert body["engine_label"] == "Supertonic 3"
        assert len(body["voices"]) == 10
        assert body["voices"][0] == {
            "id": 0,
            "sid": 0,
            "label": "Ava",
            "group": "female",
            "reference_label": "Voice 0",
        }
        assert body["voices"][1] == {
            "id": 1,
            "sid": 1,
            "label": "Brianna",
            "group": "female",
            "reference_label": "Voice 1",
        }
        assert [v["sid"] for v in body["voices"]] == list(range(10))
        assert [type(v["id"]) for v in body["voices"]] == [int] * 10
        assert [v["label"] for v in body["voices"]] == [
            "Ava",
            "Brianna",
            "Claire",
            "Danielle",
            "Eleanor",
            "Frank",
            "George",
            "Henry",
            "Isaiah",
            "James",
        ]
        assert [v["group"] for v in body["voices"]] == [
            *(["female"] * 5),
            *(["male"] * 5),
        ]
        assert [v["reference_label"] for v in body["voices"]] == [
            f"Voice {sid}" for sid in range(10)
        ]

    def test_lists_kokoro_voices_with_string_ids(self, monkeypatch):
        client, _server = _make_client()

        resp = client.get("/api/speech/voices?engine=kokoro")
        assert resp.status_code == 200
        body = resp.json()
        assert body["engine"] == "kokoro"
        assert body["engine_label"] == "Kokoro"
        assert len(body["voices"]) == 9
        assert body["voices"][0] == {
            "id": "af_heart",
            "sid": "af_heart",
            "label": "Heart",
            "group": "female",
        }
        assert body["voices"][-1]["id"] == "am_puck"
        assert [type(v["id"]) for v in body["voices"]] == [str] * 9
        assert [v["label"] for v in body["voices"]] == [
            "Heart",
            "Bella",
            "Nicole",
            "Aoede",
            "Kore",
            "Sarah",
            "Fenrir",
            "Michael",
            "Puck",
        ]
        assert [v["group"] for v in body["voices"]] == [
            *(["female"] * 6),
            *(["male"] * 3),
        ]
        assert all("reference_label" not in voice for voice in body["voices"])

    @pytest.mark.parametrize("engine", ["auto", "unknown"])
    def test_rejects_non_concrete_engine(self, engine):
        client, _server = _make_client()
        resp = client.get(f"/api/speech/voices?engine={engine}")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Gate ON — POST /api/speech/download/start
# ---------------------------------------------------------------------------


class TestSpeechDownloadStart:
    def test_broadcasts_kind_tts_progress_and_complete(self, monkeypatch):
        from whisperstream.model_downloader import ModelId

        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, server = _make_client()

        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        # A hold gate so the (otherwise instant) fake download can't reach the
        # worker's `finally` (which nulls server._download_thread) before this
        # test has captured the thread reference below -- without it, the race
        # is genuinely nondeterministic (fails or passes depending on Windows
        # thread-scheduling luck).
        release = threading.Event()

        def fake_download_archive(model, *, progress_callback=None, cancel_event=None):
            assert model is ModelId.SUPERTONIC_3_INT8
            progress_callback(50, 100)
            progress_callback(100, 100)
            release.wait(timeout=5)

        with patch(
            "whisperstream.model_downloader.download_archive",
            side_effect=fake_download_archive,
        ):
            resp = client.post(
                "/api/speech/download/start",
                json={"engine": "supertonic"},
            )
            assert resp.status_code == 200
            assert resp.json() == {
                "status": "started",
                "engine": "supertonic",
            }
            thread = server._download_thread
            assert thread is not None
            release.set()
            thread.join(timeout=5)

        assert not thread.is_alive()
        download_events = [b for b in broadcasts if b["type"].startswith("download.")]
        assert download_events, "expected at least one download.* broadcast"
        assert all(b.get("kind") == "tts" for b in download_events)
        assert all(b.get("engine") == "supertonic" for b in download_events)
        assert any(b["type"] == "download.progress" for b in download_events)
        assert any(b["type"] == "download.complete" for b in download_events)
        # Cleaned up after completion.
        assert server._download_thread is None
        assert server._download_cancel_event is None
        assert server._tts_download_engine is None

    def test_broadcasts_kind_tts_error_on_failure(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, server = _make_client()

        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        # Same hold-gate rationale as the success test above.
        release = threading.Event()

        def fake_download_archive(model, *, progress_callback=None, cancel_event=None):
            release.wait(timeout=5)
            raise RuntimeError("network boom")

        with patch(
            "whisperstream.model_downloader.download_archive",
            side_effect=fake_download_archive,
        ):
            resp = client.post(
                "/api/speech/download/start",
                json={"engine": "kokoro"},
            )
            assert resp.status_code == 200
            thread = server._download_thread
            assert thread is not None
            release.set()
            thread.join(timeout=5)

        error_events = [b for b in broadcasts if b["type"] == "download.error"]
        assert len(error_events) == 1
        assert error_events[0]["kind"] == "tts"
        assert error_events[0]["engine"] == "kokoro"

    def test_rejects_concurrent_second_start(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, server = _make_client()
        server.broadcast = MagicMock()

        release = threading.Event()

        def fake_download_archive(model, *, progress_callback=None, cancel_event=None):
            release.wait(timeout=5)

        with patch(
            "whisperstream.model_downloader.download_archive",
            side_effect=fake_download_archive,
        ):
            resp1 = client.post(
                "/api/speech/download/start",
                json={"engine": "supertonic"},
            )
            assert resp1.status_code == 200

            resp2 = client.post(
                "/api/speech/download/start",
                json={"engine": "kokoro"},
            )
            assert resp2.status_code == 409
            assert resp2.json() == {"status": "busy"}

            release.set()
            server._download_thread.join(timeout=5)

    @pytest.mark.parametrize("engine", ["auto", "unknown"])
    def test_rejects_non_concrete_downloads(self, engine):
        client, _server = _make_client()
        resp = client.post(
            "/api/speech/download/start",
            json={"engine": engine},
        )
        assert resp.status_code == 422

    def test_downloads_kokoro_model_for_kokoro_request(self):
        from whisperstream.model_downloader import ModelId

        client, server = _make_client()
        server.broadcast = MagicMock()
        release = threading.Event()
        seen: list[ModelId] = []

        def fake_download_archive(model, **_kwargs):
            seen.append(model)
            release.wait(timeout=5)

        with patch(
            "whisperstream.model_downloader.download_archive",
            side_effect=fake_download_archive,
        ):
            resp = client.post(
                "/api/speech/download/start",
                json={"engine": "kokoro"},
            )
            assert resp.status_code == 200
            thread = server._download_thread
            assert thread is not None
            release.set()
            thread.join(timeout=5)

        assert seen == [ModelId.KOKORO_82M_FP32]


# ---------------------------------------------------------------------------
# Gate ON — POST /api/speech/stop
# ---------------------------------------------------------------------------


class TestSpeechStop:
    def test_returns_ok_with_no_controller(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, _server = _make_client(speech_controller=None)

        resp = client.post("/api/speech/stop")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_delegates_to_controller_stop(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = MagicMock()
        client, _server = _make_client(speech_controller=controller)

        resp = client.post("/api/speech/stop")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        controller.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Gate ON — POST /api/speech/speak-sample
# ---------------------------------------------------------------------------


class TestSpeechSpeakSample:
    def test_returns_ok_with_no_controller(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, _server = _make_client(speech_controller=None)

        resp = client.post("/api/speech/speak-sample")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_delegates_to_controller_speak_sample(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = MagicMock()
        client, _server = _make_client(speech_controller=controller)

        resp = client.post("/api/speech/speak-sample")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        controller.speak_sample.assert_called_once()

    def test_explicit_sample_uses_submitted_engine_and_voice(self):
        """A sample cannot be redirected by a stale debounced config save."""
        controller = MagicMock()
        config = _make_mock_config(
            agent_speech_engine="supertonic",
            agent_speech_voice=1,
            agent_speech_kokoro_voice="af_heart",
        )
        client, _server = _make_client(
            config=config,
            speech_controller=controller,
        )

        resp = client.post(
            "/api/speech/speak-sample",
            json={"engine": "kokoro", "voice": "am_puck"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        controller.speak_sample.assert_called_once_with(
            engine="kokoro",
            voice="am_puck",
        )

    def test_explicit_sample_uses_saved_voice_when_omitted(self):
        controller = MagicMock()
        config = _make_mock_config(agent_speech_voice=6)
        client, _server = _make_client(
            config=config,
            speech_controller=controller,
        )

        resp = client.post(
            "/api/speech/speak-sample",
            json={"engine": "supertonic"},
        )

        assert resp.status_code == 200
        controller.speak_sample.assert_called_once_with(
            engine="supertonic",
            voice=6,
        )

    @pytest.mark.parametrize(
        "payload",
        [
            {"engine": "auto", "voice": "af_heart"},
            {"engine": "kokoro", "voice": "not-a-voice"},
            {"engine": "supertonic", "voice": 20},
        ],
    )
    def test_rejects_invalid_explicit_sample(self, payload):
        controller = MagicMock()
        client, _server = _make_client(speech_controller=controller)

        resp = client.post("/api/speech/speak-sample", json=payload)

        assert resp.status_code == 422
        controller.speak_sample.assert_not_called()


class TestSpeechSpeakText:
    """``POST /api/speech/speak-text`` (Task 1.2): speaks caller-submitted,
    transcript-class text -- the onboarding voice-speed custom test box.
    Mirrors :class:`TestSpeechSpeakSample`'s coverage, plus the length cap and
    the no-content-logging privacy guarantee.
    """

    def test_returns_ok_with_no_controller(self):
        client, _server = _make_client(speech_controller=None)

        resp = client.post(
            "/api/speech/speak-text", json={"text": "Read this back to me."}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_delegates_to_controller_speak_text_with_submitted_text(self):
        controller = MagicMock()
        client, _server = _make_client(speech_controller=controller)

        resp = client.post(
            "/api/speech/speak-text", json={"text": "Read this back to me."}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        controller.speak_text.assert_called_once_with("Read this back to me.")

    def test_explicit_text_uses_submitted_engine_and_voice(self):
        """An explicit engine/voice cannot be redirected by a stale debounced
        config save, matching the sample endpoint's contract."""
        controller = MagicMock()
        config = _make_mock_config(
            agent_speech_engine="supertonic",
            agent_speech_voice=1,
            agent_speech_kokoro_voice="af_heart",
        )
        client, _server = _make_client(
            config=config,
            speech_controller=controller,
        )

        resp = client.post(
            "/api/speech/speak-text",
            json={
                "text": "Read this back to me.",
                "engine": "kokoro",
                "voice": "am_puck",
            },
        )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        controller.speak_text.assert_called_once_with(
            "Read this back to me.",
            engine="kokoro",
            voice="am_puck",
        )

    def test_explicit_text_uses_saved_voice_when_omitted(self):
        controller = MagicMock()
        config = _make_mock_config(agent_speech_voice=6)
        client, _server = _make_client(
            config=config,
            speech_controller=controller,
        )

        resp = client.post(
            "/api/speech/speak-text",
            json={"text": "Read this back to me.", "engine": "supertonic"},
        )

        assert resp.status_code == 200
        controller.speak_text.assert_called_once_with(
            "Read this back to me.",
            engine="supertonic",
            voice=6,
        )

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"text": ""},
            {"text": "   "},
            {"text": "x" * 501},
            {
                "text": "Read this back to me.",
                "engine": "auto",
                "voice": "af_heart",
            },
            {
                "text": "Read this back to me.",
                "engine": "kokoro",
                "voice": "not-a-voice",
            },
            {
                "text": "Read this back to me.",
                "engine": "supertonic",
                "voice": 20,
            },
        ],
    )
    def test_rejects_invalid_speak_text_requests(self, payload):
        controller = MagicMock()
        client, _server = _make_client(speech_controller=controller)

        resp = client.post("/api/speech/speak-text", json=payload)

        assert resp.status_code == 422
        controller.speak_text.assert_not_called()

    def test_exactly_max_chars_is_accepted(self):
        """The cap is inclusive: exactly SPEAK_TEXT_MAX_CHARS characters passes."""
        from whisperstream.server.routes.speech import SPEAK_TEXT_MAX_CHARS

        controller = MagicMock()
        client, _server = _make_client(speech_controller=controller)
        text = "x" * SPEAK_TEXT_MAX_CHARS

        resp = client.post("/api/speech/speak-text", json={"text": text})

        assert resp.status_code == 200
        controller.speak_text.assert_called_once_with(text)

    def test_success_never_logs_request_text(self, caplog):
        """Privacy: the request text is transcript-class and must never appear
        in any captured log record for a successful call (never log
        transcript content)."""
        caplog.set_level(logging.DEBUG)
        controller = MagicMock()
        client, _server = _make_client(speech_controller=controller)
        sentinel = "SENTINEL_SPEAK_TEXT_ENDPOINT_MUST_NOT_BE_LOGGED"

        resp = client.post("/api/speech/speak-text", json={"text": sentinel})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        controller.speak_text.assert_called_once_with(sentinel)
        for record in caplog.records:
            assert sentinel not in record.getMessage()
        assert sentinel not in caplog.text


class TestSpeechShutdown:
    @pytest.mark.asyncio
    async def test_server_lifecycle_runs_bounded_speech_shutdown(self):
        controller = MagicMock()
        controller.shutdown.return_value = True
        _client, server = _make_client(speech_controller=controller)
        fake_uvicorn = MagicMock()
        fake_uvicorn.serve = AsyncMock()
        server._maybe_prepare_llama_server = MagicMock()
        server._maybe_stop_llama_server = AsyncMock()

        await server._serve_with_llama(fake_uvicorn)

        controller.shutdown.assert_called_once_with(timeout=2.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

def test_mac_status_offers_supertonic_download_before_assets_exist(monkeypatch):
    from whisperstream.tts_playback import speech_engine_supported
    monkeypatch.setattr("whisperstream.server.routes.speech.speech_engine_supported", lambda key: speech_engine_supported(key, platform_name="Darwin"))
    client, server = _make_client(config=_make_mock_config(agent_speech_engine="supertonic"))
    with patch("whisperstream.server.routes.speech.is_present", return_value=False):
        body = client.get("/api/speech/status").json()
    assert body["engines"]["supertonic"]["supported"] is True
    assert body["engines"]["kokoro"]["supported"] is True
    assert body["reason"] == "missing_supertonic"
    assert body["recommended_downloads"] == ["supertonic"]


@pytest.fixture(autouse=True)
def experimental_voice_runtime(monkeypatch):
    """These cases exercise the retained experimental Kokoro implementation."""
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
