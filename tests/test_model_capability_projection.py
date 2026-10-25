"""Safe additive descriptors preserve the source catalog and cloud cache."""

from unittest.mock import AsyncMock

from starlette.testclient import TestClient

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.enhancement_models import get_local_model_catalog
from tests.test_eula_server_boundary import _make_server


def test_cloud_capabilities_do_not_mutate_catalog(monkeypatch):
    catalog = {"gemini": [{"id": "synthetic", "name": "Synthetic"}]}
    resolve = AsyncMock(return_value=catalog)
    monkeypatch.setattr("whisperstream.cloud_model_manifest.resolve_cloud_model_catalog", resolve)
    server, _, _ = _make_server()
    with TestClient(server.fastapi) as client:
        response = client.get("/api/enhancement/models?provider=gemini")
    assert response.status_code == 200
    assert response.json()["bundled"][0]["capabilities"] == {
        "family": "customizable", "editable_prompt": True,
        "supported_modes": None,
    }
    assert "capabilities" not in catalog["gemini"][0]


def test_local_capabilities_preserve_source_catalog_membership(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("whisperstream.model_downloader.local_gguf_available", lambda *a, **k: False)
    server, app, _ = _make_server()
    app.config = WhisperstreamConfig()
    app.llama_server = None
    with TestClient(server.fastapi) as client:
        response = client.get("/api/enhancement/models?provider=local")
    assert response.status_code == 200
    by_id = {row["id"]: row["capabilities"] for row in response.json()["bundled"]}
    source_ids = {
        str(entry["id"])
        for entry in get_local_model_catalog(active_id=app.config.enhancement_local_model)
    }
    assert set(by_id) == source_ids
    assert {"stream-1-mini", "gemma4-e2b-instruct"} <= source_ids
    assert by_id["gemma4-e2b-instruct"]["family"] == "customizable"
    assert by_id["stream-1-mini"]["family"] == "trained-mode"
    assert by_id["stream-1-mini"]["editable_prompt"] is False
