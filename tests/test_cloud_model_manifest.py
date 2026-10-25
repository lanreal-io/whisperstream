from __future__ import annotations

import json

import httpx
import pytest

from whisperstream import cloud_model_manifest as manifest


def _document() -> dict:
    return {
        "schema_version": 1,
        "providers": {
            "gemini": [
                {"id": "gemini-lite", "label": "Gemini Lite", "recommended": True},
                {"id": "gemini-flash", "label": "Gemini Flash"},
            ],
            "openai": [
                {"id": "openai-light", "label": "OpenAI Light", "recommended": True},
                {"id": "openai-medium", "label": "OpenAI Medium"},
            ],
            "anthropic": [
                {"id": "anthropic-light", "label": "Anthropic Light", "recommended": True},
                {"id": "anthropic-medium", "label": "Anthropic Medium"},
            ],
        },
    }


def _bytes(document: dict) -> bytes:
    return json.dumps(document).encode()


@pytest.fixture(autouse=True)
def _clear_memory_cache():
    manifest.clear_memory_cache()
    yield
    manifest.clear_memory_cache()


def test_bundled_manifest_is_exact_locked_catalog():
    catalog = manifest.load_bundled_manifest()
    assert [[row["id"] for row in catalog[p]] for p in manifest.PROVIDERS] == [
        ["gemini-3.5-flash-lite", "gemini-3.7-flash"],
        ["gpt-5.6-luna", "gpt-5.6-terra"],
        ["claude-haiku-4-5-20251001", "claude-sonnet-5"],
    ]
    for rows in catalog.values():
        assert sum(row.get("recommended") is True for row in rows) == 1
        assert "free" not in rows[0]


def test_new_thinking_models_require_updated_major_without_migrating_defaults():
    catalog = manifest.load_bundled_manifest(app_version="2.0.0")
    assert catalog["anthropic"][-1]["id"] == "claude-haiku-5-5"
    assert catalog["openai"][-1]["id"] == "gpt-6-luna"
    assert catalog["anthropic"][0]["recommended"] is True
    assert catalog["openai"][0]["recommended"] is True
    for provider in ("anthropic", "openai"):
        assert catalog[provider][-1]["minimum_app_version"] == "2.0.0"
        assert len(manifest.load_bundled_manifest(app_version="1.6.0")[provider]) == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=True),
        lambda d: d.update(schema_version=2),
        lambda d: d["providers"].pop("gemini"),
        lambda d: d["providers"]["gemini"][0].update(endpoint="https://example.com"),
        lambda d: d["providers"]["gemini"][0].update(recommended="yes"),
        lambda d: d["providers"]["gemini"][1].update(recommended=True),
        lambda d: d["providers"]["openai"][0].update(id="gemini-lite"),
        lambda d: d["providers"]["anthropic"][0].update(minimum_app_version="next"),
    ],
)
def test_validator_rejects_unbounded_or_inconsistent_documents(mutate):
    document = _document()
    mutate(document)
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_manifest_bytes(_bytes(document), app_version="1.2.3")


def test_validator_rejects_oversized_document():
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_manifest_bytes(b" " * (manifest.MAX_MANIFEST_BYTES + 1), app_version="1.0.0")


def test_minimum_app_version_filters_rows():
    document = _document()
    document["providers"]["openai"][1]["minimum_app_version"] = "2.0.0"
    catalog = manifest.parse_manifest_bytes(_bytes(document), app_version="1.9.9")
    assert [row["id"] for row in catalog["openai"]] == ["openai-light"]


@pytest.mark.asyncio
async def test_catalog_is_bundled_only_and_makes_no_request(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    (tmp_path / "cloud-models.json").write_bytes(_bytes(_document()))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: pytest.fail("unexpected catalog request"))

    first = await manifest.resolve_cloud_model_catalog(app_version="1.0.0", now=lambda: 10)
    second = await manifest.resolve_cloud_model_catalog(app_version="1.0.0", now=lambda: 11)

    assert first == manifest.load_bundled_manifest(app_version="1.0.0")
    assert second == first
    assert list(tmp_path.iterdir()) == [tmp_path / "cloud-models.json"]


def test_lockdown_rejects_catalog_before_network(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from starlette.testclient import TestClient
    from tests.test_eula_server_boundary import _make_server
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    fetch = AsyncMock(side_effect=AssertionError("catalog must not run"))
    monkeypatch.setattr(manifest, "resolve_cloud_model_catalog", fetch)
    server, app, _ = _make_server()
    app.config.lockdown_mode = True
    with TestClient(server.fastapi) as client:
        response = client.get("/api/enhancement/models?provider=gemini")
    assert response.status_code == 403
    assert response.json()["error_code"] == "lockdown_provider_unavailable"
    fetch.assert_not_awaited()
