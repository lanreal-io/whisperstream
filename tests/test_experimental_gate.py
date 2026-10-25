"""Tests for the agent-speech experimental gate.

Covers the runtime env-var helper (``_experimental_enabled``) and the always-on
discovery probe ``GET /api/experimental/enabled``: strict-"1" semantics, probe
never 404s, and the compile-time build flag overrides the env var.

Since 1.3.0 the gate mechanism gates **nothing**: Agent Speak shipped and
came off it. It is deliberately retained for future unfinished
features, so the helper's strict-"1" semantics, the never-404 probe, and the
build-flag override are all still live contracts and still tested here.

What moved: the speech-specific hygiene coverage now pins **config-off**
inertness (``claude_hud_enabled`` / ``agent_speech_enabled``, both default-off)
instead of gate-off 404s, and the ``scripts/build.py`` guard is inverted -- it
asserts the shipped modules are NOT excluded from Nuitka's follow set and that
their runtime data payloads ARE staged. The static ``ast``-walking sweep for
module-scope ``whisperstream.speech`` imports outside the package is unchanged:
lazy in-function imports remain the convention so importing ``server`` never
drags in the TTS stack.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_server_endpoints.py)
# ---------------------------------------------------------------------------


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.enhancement_provider = "local"
    cfg.enhancement_local_model = "gemma4-e2b-instruct"
    cfg.enhancement_local_endpoint = ""
    cfg.enhancement_api_key = ""
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.parakeet_quantization = "int8"
    cfg.word_boosts = []
    cfg.threads = None
    cfg.model_timeout = 0.0
    cfg.hotword_score = 1.5
    cfg.beam_width = 4
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.to_dict = lambda: {
        "enhancement_provider": cfg.enhancement_provider,
        "enhancement_local_model": cfg.enhancement_local_model,
        "enhancement_local_endpoint": cfg.enhancement_local_endpoint,
    }
    return cfg


def _make_mock_app(*, config=None) -> MagicMock:
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
    return app


def _make_client(*, config=None) -> tuple[TestClient, Any]:
    from whisperstream.server import WhisperstreamServer

    server = WhisperstreamServer(_make_mock_app(config=config), port=0, logger=None, register_hotkeys=False)
    client = TestClient(server.fastapi, raise_server_exceptions=True)
    return client, server


# ---------------------------------------------------------------------------
# _experimental_enabled() helper
# ---------------------------------------------------------------------------


class TestExperimentalEnabledHelper:
    def test_returns_false_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)
        from whisperstream.server.app import _experimental_enabled
        assert _experimental_enabled() is False

    def test_returns_true_when_env_is_exactly_1(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        from whisperstream.server.app import _experimental_enabled
        assert _experimental_enabled() is True

    def test_returns_false_for_truthy_string_true(self, monkeypatch):
        """Only exactly '1' should be accepted — no truthy-ish strings."""
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "true")
        from whisperstream.server.app import _experimental_enabled
        assert _experimental_enabled() is False

    def test_returns_false_for_truthy_string_yes(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "yes")
        from whisperstream.server.app import _experimental_enabled
        assert _experimental_enabled() is False

    def test_returns_false_for_empty_string(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "")
        from whisperstream.server.app import _experimental_enabled
        assert _experimental_enabled() is False

    def test_returns_false_for_whitespace(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "  ")
        from whisperstream.server.app import _experimental_enabled
        assert _experimental_enabled() is False

    def test_returns_true_with_leading_trailing_whitespace(self, monkeypatch):
        """Whitespace around '1' should be stripped and accepted."""
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", " 1 ")
        from whisperstream.server.app import _experimental_enabled
        assert _experimental_enabled() is True


# ---------------------------------------------------------------------------
# GET /api/experimental/enabled endpoint
# ---------------------------------------------------------------------------


class TestGetExperimentalEnabled:
    def test_returns_enabled_false_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)
        client, _ = _make_client()
        resp = client.get("/api/experimental/enabled")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False}

    def test_returns_enabled_true_when_env_set(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, _ = _make_client()
        resp = client.get("/api/experimental/enabled")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": True}

    def test_endpoint_always_200_not_404(self, monkeypatch):
        """Discovery probe must never 404 — it returns enabled=false instead."""
        monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)
        client, _ = _make_client()
        resp = client.get("/api/experimental/enabled")
        assert resp.status_code == 200

    def test_build_flag_off_overrides_env(self, monkeypatch):
        """When EXPERIMENTAL_AVAILABLE is compiled False (production), the probe
        reports enabled=false even with WHISPERSTREAM_EXPERIMENTAL=1 set — the
        build-time flag is the master gate. Mirrors the dev-tools audit Check 3."""
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        monkeypatch.setattr("whisperstream._build_flags.EXPERIMENTAL_AVAILABLE", False)
        client, _ = _make_client()
        resp = client.get("/api/experimental/enabled")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False}


# ---------------------------------------------------------------------------
# Task 2.4(a): gate OFF inertness for the speech feature specifically
# ---------------------------------------------------------------------------

# Mirrors test_speech_endpoints.py's SPEECH_ROUTES list.
SPEECH_ROUTES = [
    ("get", "/api/speech/status"),
    ("get", "/api/speech/voices"),
    ("post", "/api/speech/download/start"),
    ("post", "/api/speech/stop"),
    ("post", "/api/speech/speak-sample"),
]


def _no_speech_modules_imported() -> bool:
    return not any(
        k == "whisperstream.speech" or k.startswith("whisperstream.speech.")
        for k in sys.modules
    )


class TestSpeechConfigOffInertness:
    """Since 1.3.0 the speech feature is gated by config, not by the
    experimental build/env gate, and it ships default-off. So the stored
    ``agent_speech_mode`` value alone must never be enough to light it up:
    with ``claude_hud_enabled=False`` (the shipped default),
    ``/api/claude-event`` stays a plain 200 no-op that imports no speech
    module at all, and the ``/api/speech/*`` routes -- though now always
    registered -- keep the TTS runtime unloaded.

    This is the coverage that protects users now that the build gate is gone,
    so it is stricter here than the old gate-off version: config-off must be
    airtight."""

    def test_claude_event_ok_and_no_speech_module_imported(self, monkeypatch):
        monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)
        # Start from a clean slate so a prior test's import doesn't produce a
        # false pass here.
        saved = {
            k: v
            for k, v in list(sys.modules.items())
            if k == "whisperstream.speech" or k.startswith("whisperstream.speech.")
        }
        for k in saved:
            del sys.modules[k]
        try:
            config = _make_mock_config(
                claude_hud_enabled=False,
                agent_speech_enabled=False,
                agent_speech_mode="verbatim",
            )
            client, _server = _make_client(config=config)

            resp = client.post(
                "/api/claude-event",
                json={"hook_event_name": "Stop", "last_assistant_message": "All done."},
            )

            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
            assert _no_speech_modules_imported()
        finally:
            sys.modules.update(saved)

    def test_all_speech_routes_reachable_but_load_no_tts_runtime(self, monkeypatch):
        monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)
        config = _make_mock_config(
            claude_hud_enabled=False,
            agent_speech_enabled=False,
            agent_speech_mode="verbatim",
        )
        client, _server = _make_client(config=config)

        # The heavy TTS runtime (sherpa-onnx / sounddevice / model loading).
        # engine_specs is excluded on purpose: it is the sherpa-free static
        # metadata module the status/voices routes are allowed to read.
        heavy = (
            "whisperstream.speech.engines",
            "whisperstream.speech.supertonic_engine",
            "whisperstream.speech.controller",
            "whisperstream.speech.playback",
            "whisperstream.speech.summarizer",
        )
        saved = {k: v for k, v in list(sys.modules.items()) if k in heavy}
        for k in saved:
            del sys.modules[k]
        try:
            # download/start is patched rather than skipped: it must also be
            # reachable, but it spawns a real worker thread, so stub the fetch
            # and join it instead of leaving a download running.
            with patch(
                "whisperstream.model_downloader.download_archive",
                return_value=None,
            ):
                for method, route in SPEECH_ROUTES:
                    resp = getattr(client, method)(route)
                    assert resp.status_code != 404, (
                        f"{method.upper()} {route} 404'd -- speech routes are "
                        f"always registered since 1.3.0"
                    )
                thread = _server._download_thread
                if thread is not None:
                    thread.join(timeout=5)
            assert [name for name in heavy if name in sys.modules] == []
        finally:
            sys.modules.update(saved)


# ---------------------------------------------------------------------------
# Task 2.4(b): static import hygiene -- no module-scope whisperstream.speech
# import anywhere outside the speech/ package itself
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_WHISPERSTREAM_DIR = _REPO_ROOT / "src" / "whisperstream"


class TestSpeechImportHygiene:
    """The speech package now ships, so this is no longer about Nuitka
    exclusions. It is about startup cost and blast radius: importing
    ``whisperstream.server`` or ``main`` must not drag in the TTS runtime
    (sherpa-onnx / sounddevice / model loading) for the overwhelming majority
    of users who never turn Agent Speak on. Lazy, in-function imports remain
    the only sanctioned way to reach into ``whisperstream.speech`` from
    elsewhere.

    Walks every top-level ``src/whisperstream/*.py`` module (non-recursive,
    so the ``speech/`` package itself and other subpackages like
    ``claude_hook/`` are naturally excluded) and asserts no module-scope
    ``ast.Import``/``ast.ImportFrom`` statement names the speech package,
    in any of its spellings: ``import whisperstream.speech``,
    ``from whisperstream.speech import ...``,
    ``from whisperstream import speech``, ``from .speech import ...``,
    ``from . import speech``.
    """

    def test_no_module_scope_import_of_speech_package(self):
        py_files = sorted(_SRC_WHISPERSTREAM_DIR.glob("*.py"))
        assert len(py_files) > 10, (
            f"expected many top-level source modules under {_SRC_WHISPERSTREAM_DIR}, "
            f"found {len(py_files)} -- path resolution is probably wrong"
        )

        offenders: list[str] = []
        for py_file in py_files:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            # tree.body holds ONLY module-scope statements; imports nested
            # inside a function/method body live under a FunctionDef node
            # instead and are never visited here, which is the point --
            # lazy in-function imports are allowed.
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "whisperstream.speech" or alias.name.startswith(
                            "whisperstream.speech."
                        ):
                            offenders.append(f"{py_file.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level == 0:
                        # Absolute form: `from whisperstream.speech import x`
                        # or `from whisperstream import speech`.
                        if module == "whisperstream.speech" or module.startswith(
                            "whisperstream.speech."
                        ):
                            offenders.append(f"{py_file.name}: from {module} import ...")
                        elif module == "whisperstream" and any(
                            alias.name == "speech" for alias in node.names
                        ):
                            offenders.append(
                                f"{py_file.name}: from whisperstream import speech"
                            )
                    else:
                        # Relative form: `from .speech import x` (module ==
                        # "speech") or `from . import speech` (module == "").
                        if module == "speech":
                            offenders.append(f"{py_file.name}: from .speech import ...")
                        elif module == "" and any(
                            alias.name == "speech" for alias in node.names
                        ):
                            offenders.append(f"{py_file.name}: from . import speech")

        assert offenders == [], (
            "module-scope import of whisperstream.speech found -- must be "
            f"lazy and in-function only: {offenders}"
        )


# ---------------------------------------------------------------------------
# scripts/build.py must now SHIP Agent Speak (1.3.0)
# ---------------------------------------------------------------------------


class TestBuildScriptShipsSpeechPackage:
    """Inverted from the pre-1.3.0 guard. Agent Speak ships in production
    builds now, so ``scripts/build.py`` must NOT carry a Nuitka
    ``--nofollow-import-to`` exclusion for any of its modules -- an exclusion
    would compile the feature out of the sidecar entirely.

    It must also stage their non-Python payloads. ``forwarder.ps1`` and the
    ``claude_plugin/`` tree are loaded via ``importlib.resources`` at runtime,
    so Nuitka cannot discover them from the import graph. Without these
    includes, Agent Speak imports fine in dev and fails only in a packaged
    install -- exactly the class of break a unit test should pin.
    """

    EXCLUDED_MODULES = (
        "whisperstream.speech",
        "whisperstream.claude_hud",
        "whisperstream.claude_hook",
        "whisperstream.agent_activity",
        "whisperstream.agent_events",
    )

    def test_build_script_does_not_exclude_shipped_modules(self):
        build_script = _REPO_ROOT / "scripts" / "build.py"
        text = build_script.read_text(encoding="utf-8")
        for module in self.EXCLUDED_MODULES:
            assert f'"--nofollow-import-to={module}"' not in text, (
                f"scripts/build.py excludes {module} from Nuitka's follow set, "
                f"but it ships from 1.3.0 (Agent Speak)"
            )

    def test_build_script_stages_runtime_data_payloads(self):
        build_script = _REPO_ROOT / "scripts" / "build.py"
        text = build_script.read_text(encoding="utf-8")
        assert (
            "--include-data-files=src/whisperstream/claude_hook/forwarder.ps1"
            "=whisperstream/claude_hook/forwarder.ps1"
        ) in text, "forwarder.ps1 is not staged; Agent Speak breaks in packaged builds"
        assert (
            "--include-data-dir=src/whisperstream/claude_plugin"
            "=whisperstream/claude_plugin"
        ) in text, "claude_plugin/ is not staged; Agent Speak breaks in packaged builds"
