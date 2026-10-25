"""Exercise consent context, admission and storage via the real local API."""
from unittest.mock import AsyncMock, MagicMock
import sys

from fastapi.testclient import TestClient
import httpx
import pytest

from whisperstream import byok_consent
from whisperstream.config_manager import ConfigManager, WhisperstreamConfig
from whisperstream.llm_enhancer import LLMEnhancer
from whisperstream.server import WhisperstreamServer

pytestmark = pytest.mark.usefixtures("accepted_eula")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    cfg = WhisperstreamConfig(enhancement_enabled=True, enhancement_provider="ollama")
    app = MagicMock()
    app.config = cfg
    app.llama_server = None
    app.license_manager.maybe_revalidate = AsyncMock()
    app.license_manager.status.return_value = {"is_pro": True}
    app.stt_engine.transcribe.return_value = "synthetic raw transcript text"
    app.text_injector.process.side_effect = lambda text: text
    app.config_manager = ConfigManager()
    app.config_manager.save(cfg)
    app.reload_config.side_effect = lambda new: setattr(app, "config", new)
    enhancer = LLMEnhancer(cfg)
    app.llm_enhancer = enhancer
    captures = []
    def capture(request):
        captures.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "synthetic output"}}]})
    enhancer._client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    with TestClient(server.fastapi) as client:
        yield app, client, captures
    import asyncio
    asyncio.run(enhancer._client.aclose())


def accept(client, **settings):
    response = client.post("/api/byok-consent/context", json=settings)
    assert response.status_code == 200, response.text
    context = response.json()
    result = client.post("/api/byok-consent", json={**settings, "context_token": context["context_token"], "dialog_body_text": context["dialog_body_text"]})
    assert result.status_code == 200, result.text
    return context, result.json()


@pytest.mark.parametrize("path", ["/api/enhancement/check", "/api/enhancement/preview"])
@pytest.mark.parametrize("state", ["missing", "revoked", "changed_endpoint", "changed_model", "disabled"])
def test_routes_refuse_before_network(runtime, path, state):
    app, client, captures = runtime
    if state != "missing":
        accept(client, provider="ollama")
    if state == "revoked":
        byok_consent.remove_provider("ollama")
    elif state == "changed_endpoint":
        app.config.enhancement_ollama_endpoint = "https://other.invalid"
    elif state == "changed_model":
        app.config.enhancement_ollama_model = "different-cloud-model"
    elif state == "disabled":
        app.config.enhancement_enabled = False
    response = client.post(path, json={"text": "synthetic sample", "system_prompt": "synthetic prompt"})
    assert response.status_code == 403
    assert captures == []


def test_current_consent_allows_probe_and_preview(runtime):
    _, client, captures = runtime
    context, record = accept(client, provider="ollama")
    assert record["recipient_identity"] == context["recipient_identity"]
    assert record["eula_version"] == context["legal_identity"]["eula_version"]
    assert record["privacy_policy_content_hash"] == context["legal_identity"]["privacy_policy_content_hash"]
    assert client.post("/api/enhancement/check").json()["connected"]
    assert client.post("/api/enhancement/preview", json={"text": "synthetic sample", "system_prompt": "synthetic prompt"}).status_code == 200
    assert len(captures) == 2
    assert "synthetic prompt" not in captures[0].content.decode()


def test_changed_context_cannot_record_unseen_destination(runtime):
    app, client, captures = runtime
    context = client.post("/api/byok-consent/context", json={"provider": "ollama"}).json()
    app.config.enhancement_ollama_endpoint = "https://changed.invalid"
    response = client.post("/api/byok-consent", json={"provider": "ollama", "context_token": context["context_token"], "dialog_body_text": context["dialog_body_text"]})
    assert response.status_code == 409
    assert byok_consent.list_records() == []
    assert captures == []


def test_wrong_displayed_body_cannot_be_recorded(runtime):
    _, client, _ = runtime
    context = client.post("/api/byok-consent/context", json={"provider": "ollama"}).json()
    response = client.post("/api/byok-consent", json={"provider": "ollama", "context_token": context["context_token"], "dialog_body_text": "Everything stays local"})
    assert response.status_code == 409
    assert not byok_consent.list_records()


@pytest.mark.parametrize("failure", ["unreadable", "write_failed"])
def test_consent_storage_failure_returns_stable_error_and_preserves_evidence(runtime, monkeypatch, failure):
    _, client, captures = runtime
    _, prior_record = accept(client, provider="ollama")
    path = byok_consent.get_consent_path()
    if failure == "unreadable":
        path.write_bytes(b"\xff\xfe")
    else:
        def fail(*args, **kwargs):
            raise OSError("synthetic private disk detail")
        monkeypatch.setattr(byok_consent, "atomic_write_json", fail)
    before = path.read_bytes()
    context = client.post("/api/byok-consent/context", json={"provider": "ollama"}).json()
    response = client.post("/api/byok-consent", json={"provider": "ollama", "context_token": context["context_token"], "dialog_body_text": context["dialog_body_text"]})
    assert response.status_code == 503
    assert response.json() == {"error": "consent_store_unavailable", "error_code": "consent_store_unavailable"}
    assert path.read_bytes() == before
    assert captures == []


def test_status_only_acknowledges_current_recipient_and_retains_history(runtime):
    app, client, _ = runtime
    accept(client, provider="ollama")
    assert "ollama" in client.get("/api/byok-consent").json()["acknowledged_providers"]
    app.config.enhancement_ollama_endpoint = "https://changed.invalid"
    status = client.get("/api/byok-consent").json()
    assert "ollama" not in status["acknowledged_providers"]
    assert len(status["records"]) == 1


def test_previously_approved_draft_does_not_claim_current_dispatch_readiness(runtime):
    app, client, _ = runtime
    accept(client, provider="ollama", endpoint="https://other.invalid")
    pending = client.post("/api/byok-consent/context", json={"provider": "ollama", "endpoint": "https://other.invalid"}).json()
    assert pending["has_consent"] is True
    assert pending["matches_config"] is False
    app.config.enhancement_ollama_endpoint = "https://other.invalid"
    saved = client.post("/api/byok-consent/context", json={"provider": "ollama", "endpoint": "https://other.invalid"}).json()
    assert saved["matches_config"] is True


@pytest.mark.parametrize("field,value", [("enhancement_custom_base_url", "http://remote.invalid"), ("enhancement_ollama_endpoint", "http://remote.invalid"), ("enhancement_local_endpoint", "https://remote.invalid")])
def test_unsafe_endpoint_save_leaves_config_intact(runtime, field, value):
    app, client, captures = runtime
    original = app.config
    before = app.config_manager.config_path.read_bytes()
    response = client.put("/api/config", json={field: value})
    assert response.status_code == 400
    assert app.config is original
    assert app.config_manager.config_path.read_bytes() == before
    assert captures == []


def test_failed_config_save_preserves_disk_and_live_state(runtime, monkeypatch):
    app, client, _ = runtime
    before = app.config_manager.config_path.read_bytes()
    original = app.config
    from whisperstream.protected_secret import UnavailableProtectedSecretStore
    monkeypatch.setattr("whisperstream.key_encryption.get_protected_secret_store",
                        lambda: UnavailableProtectedSecretStore())
    response = client.put("/api/config", json={"enhancement_openai_api_key": "synthetic-secret"})
    assert response.status_code == 503
    assert response.json()["error_code"] == "secure_storage_failed"
    assert "synthetic-secret" not in response.text
    assert app.config is original
    assert app.config_manager.config_path.read_bytes() == before


@pytest.mark.parametrize("state", ["missing", "revoked", "changed_endpoint", "changed_model", "disabled"])
def test_compatibility_transcription_keeps_raw_without_authority(runtime, monkeypatch, state):
    import numpy as np
    from whisperstream import main
    app, client, captures = runtime
    monkeypatch.setattr(main, "_load_audio_file", lambda *_: (np.zeros(16, dtype=np.float32), 16000))
    if state != "missing":
        accept(client, provider="ollama")
    if state == "revoked":
        byok_consent.remove_provider("ollama")
    elif state == "changed_endpoint":
        app.config.enhancement_ollama_endpoint = "https://other.invalid"
    elif state == "changed_model":
        app.config.enhancement_ollama_model = "other-model"
    elif state == "disabled":
        app.config.enhancement_enabled = False
    response = client.post("/api/transcribe", files={"file": ("synthetic.wav", b"synthetic", "audio/wav")})
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "synthetic raw transcript text"
    assert captures == []


@pytest.mark.parametrize("state", ["missing", "revoked", "changed_endpoint", "changed_model", "disabled"])
def test_saved_text_reenhance_refuses_before_reading_history(runtime, state):
    app, client, captures = runtime
    if state != "missing":
        accept(client, provider="ollama")
    if state == "revoked":
        byok_consent.remove_provider("ollama")
    elif state == "changed_endpoint":
        app.config.enhancement_ollama_endpoint = "https://other.invalid"
    elif state == "changed_model":
        app.config.enhancement_ollama_model = "other-model"
    elif state == "disabled":
        app.config.enhancement_enabled = False
    response = client.post("/api/experimental/transcripts/synthetic/reenhance")
    assert response.status_code == 409, response.text
    app.transcript_store.get.assert_not_called()
    assert captures == []
