"""Fresh installations can discover and request Kokoro without feature flags."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.server.routes import speech
from whisperstream.speech import availability
from whisperstream.speech.engine_specs import KOKORO_SPEC


@pytest.mark.parametrize("platform", ["Windows", "Darwin"])
def test_fresh_installation_exposes_kokoro_and_admits_download(
    monkeypatch, tmp_path, platform
):
    monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)
    monkeypatch.setattr("whisperstream._build_flags.EXPERIMENTAL_AVAILABLE", False)
    monkeypatch.setattr("whisperstream.config_manager.get_models_root", lambda: tmp_path)
    monkeypatch.setattr("whisperstream.config_manager.platform.system", lambda: platform)
    monkeypatch.setattr("whisperstream.system_info.get_total_ram_mb", lambda: 16384)
    monkeypatch.setattr("whisperstream.system_info.get_available_ram_mb", lambda: 8192)
    monkeypatch.setattr(
        speech, "speech_engine_supported", lambda key: platform == "Windows" or key == "kokoro"
    )
    config = WhisperstreamConfig()
    config.agent_speech_engine = "auto"
    starts = []
    api = FastAPI()
    speech.register_speech_routes(
        api,
        current_config=lambda: config,
        current_controller=lambda: None,
        current_download_engine=lambda: None,
        try_start_download=lambda key: starts.append(key) or True,
    )

    assert not availability.kokoro_cached(models_root=tmp_path)
    assert availability.kokoro_available(models_root=tmp_path)
    with TestClient(api) as client:
        response = client.get("/api/speech/status")
        assert response.status_code == 200
        status = response.json()
        assert status["engines"]["kokoro"]["installed"] is False
        assert status["engines"]["kokoro"]["supported"] is True
        # Automatic downloads both models where Supertonic runs (Windows).
        assert status["recommended_downloads"] == (
            ["supertonic", "kokoro"] if platform == "Windows" else ["kokoro"]
        )
        response = client.get("/api/speech/voices?engine=kokoro")
        assert response.status_code == 200
        assert [voice["id"] for voice in response.json()["voices"]] == [
            voice.id for voice in KOKORO_SPEC.voices
        ]
        assert len(response.json()["voices"]) == 9
        response = client.post("/api/speech/download/start", json={"engine": "kokoro"})
        assert response.status_code == 200
        assert response.json() == {"status": "started", "engine": "kokoro"}
    assert starts == ["kokoro"]
    assert list(tmp_path.iterdir()) == []
