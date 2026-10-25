"""One public Gemma model, verified fallback bytes and artifact-bound receipts."""
import hashlib
import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest


from whisperstream import gemma_artifacts as a
from whisperstream import enhancement_models as models
from whisperstream import model_downloader as d
from whisperstream.config_manager import WhisperstreamConfig, ConfigManager, get_local_gpu_verdict


@pytest.fixture
def artifacts(monkeypatch, tmp_path):
    payloads = {a.PUBLIC_ID: b"primary gguf", a.GOOGLE_ID: b"official google gguf"}
    for key, data in payloads.items():
        source = dict(d.MODEL_SOURCES[key])
        filename = next(iter(source["files"]))
        source["files"] = {filename: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
        monkeypatch.setitem(d.MODEL_SOURCES, key, source)
    monkeypatch.setattr("whisperstream.config_manager.get_local_llm_dir", lambda: tmp_path)
    return payloads


def test_availability_falls_back_and_reuses_verified_google(monkeypatch, tmp_path, artifacts):
    calls = []
    def handle(request):
        calls.append(str(request.url))
        if "/unsloth/" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, content=artifacts[a.GOOGLE_ID])
    monkeypatch.setattr(d, "new_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    d.download(d.ModelId(a.PUBLIC_ID), tmp_path)
    assert len(calls) == 3
    assert a.resolve(tmp_path) == (a.GOOGLE_ID, a.artifact_path(tmp_path, a.GOOGLE_ID))
    assert models.resolve_local_model_path(a.PUBLIC_ID).read_bytes() == artifacts[a.GOOGLE_ID]
    d.download(d.ModelId(a.PUBLIC_ID), tmp_path)
    assert len(calls) == 3
    assert [row["id"] for row in models.LOCAL_BUNDLED_MODELS] == ["stream-1-mini", a.PUBLIC_ID]
    assert models.get_local_bundled_model(a.GOOGLE_ID) is None


@pytest.mark.parametrize("failure", ["hash", "size", "disk", "cancel", "policy"])
def test_nonavailability_failures_never_try_google(monkeypatch, tmp_path, artifacts, failure):
    calls = []
    event = threading.Event()
    def handle(request):
        calls.append(request)
        if failure == "disk":
            raise OSError("disk full")
        if failure == "policy":
            raise RuntimeError("unsupported redirect")
        if failure == "cancel":
            event.set()
            return httpx.Response(503)
        return httpx.Response(200, content=b"x" * (len(artifacts[a.PUBLIC_ID]) if failure == "hash" else 1))
    monkeypatch.setattr(d, "new_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(d.DownloadCancelled if failure == "cancel" else RuntimeError):
        d.download(d.ModelId(a.PUBLIC_ID), tmp_path, cancel_event=event)
    assert len(calls) == 1
    assert not a.artifact_path(tmp_path, a.GOOGLE_ID).exists()


def test_existing_google_must_match_its_pin(monkeypatch, tmp_path, artifacts):
    a.artifact_path(tmp_path, a.GOOGLE_ID).write_bytes(b"bad restored file")
    monkeypatch.setattr(d, "new_http_client", lambda: pytest.fail("network"))
    with pytest.raises(RuntimeError):
        d.download(d.ModelId(a.PUBLIC_ID), tmp_path)


def test_marker_and_file_replacement_invalidate_receipt(tmp_path, artifacts):
    primary = a.artifact_path(tmp_path, a.PUBLIC_ID)
    google = a.artifact_path(tmp_path, a.GOOGLE_ID)
    primary.write_bytes(artifacts[a.PUBLIC_ID])
    google.write_bytes(artifacts[a.GOOGLE_ID])
    cfg = WhisperstreamConfig(enhancement_local_gpu_verdicts={a.PUBLIC_ID: "viable"})
    assert get_local_gpu_verdict(cfg, a.PUBLIC_ID) == "viable"
    cfg.enhancement_local_gpu_artifacts[a.PUBLIC_ID] = a.receipt(tmp_path)
    a.select(tmp_path, a.GOOGLE_ID)
    assert get_local_gpu_verdict(cfg, a.PUBLIC_ID) == "not_run"
    assert cfg.to_dict()["enhancement_local_gpu_verdicts"][a.PUBLIC_ID] == "not_run"
    cfg.enhancement_local_gpu_artifacts[a.PUBLIC_ID] = a.receipt(tmp_path)
    assert get_local_gpu_verdict(cfg, a.PUBLIC_ID) == "viable"
    google.write_bytes(b"new manual file")
    assert get_local_gpu_verdict(cfg, a.PUBLIC_ID) == "not_run"
    google.unlink()
    assert a.resolve(tmp_path)[0] == a.PUBLIC_ID
    assert get_local_gpu_verdict(cfg, a.PUBLIC_ID) == "not_run"


def test_legacy_google_selection_migrates_marker_and_own_receipt(tmp_path, artifacts):
    for key, data in artifacts.items():
        a.artifact_path(tmp_path, key).write_bytes(data)
    cfg = WhisperstreamConfig.from_dict({
        "enhancement_local_model": a.GOOGLE_ID,
        "enhancement_local_gpu_verdicts": {a.GOOGLE_ID: "marginal", a.PUBLIC_ID: "viable"},
        "enhancement_local_gpu_devices": {a.GOOGLE_ID: 2, a.PUBLIC_ID: 0},
    })
    assert cfg.enhancement_local_model == a.PUBLIC_ID
    assert a.resolve(tmp_path)[0] == a.GOOGLE_ID
    assert get_local_gpu_verdict(cfg, a.PUBLIC_ID) == "marginal"
    assert cfg.enhancement_local_gpu_devices[a.PUBLIC_ID] == 2
    manager = ConfigManager(config_path=tmp_path / "config.toml", models_root=tmp_path / "models")
    manager.save(cfg)
    assert manager.load().enhancement_local_gpu_artifacts == cfg.enhancement_local_gpu_artifacts


def test_missing_artifact_cannot_reuse_legacy_primary_receipt(tmp_path, artifacts):
    cfg = WhisperstreamConfig(enhancement_local_gpu_verdicts={a.PUBLIC_ID: "viable"})
    assert get_local_gpu_verdict(cfg, a.PUBLIC_ID) == "not_run"


@pytest.mark.parametrize("verdict", ["viable", "marginal", "unviable", "cancel"])
def test_active_recovery_measures_fallback_before_resuming(tmp_path, artifacts, verdict):
    from tests.test_server_endpoints import TestRunBenchmarkWorker
    from whisperstream.gpu_benchmark import BenchmarkResult
    helper = TestRunBenchmarkWorker()
    server, events = helper._server_with_broadcasts()
    cfg = WhisperstreamConfig(enhancement_enabled=True, enhancement_provider="local", enhancement_local_model=a.PUBLIC_ID,
                              enhancement_local_gpu_verdicts={a.PUBLIC_ID: "viable"})
    server._app.config = cfg
    server._loop = None
    server._benchmark.thread = None
    google = a.artifact_path(tmp_path, a.GOOGLE_ID)
    def fetch(model_id, cancel_event):
        assert model_id == a.PUBLIC_ID
        if not google.exists():
            google.write_bytes(artifacts[a.GOOGLE_ID])
            a.select(tmp_path, a.GOOGLE_ID)
        return True, False
    devices_benched = []
    async def bench(**kwargs):
        devices_benched.append(kwargs["device_index"])
        assert kwargs["gguf_path"] == google
        assert kwargs["prompt_key"] == "gemma"
        assert cfg.enhancement_enabled is False
        assert server._benchmark.thread is threading.current_thread()
        if verdict == "cancel":
            server._download_cancel_event.set()
        return BenchmarkResult(verdict="viable" if verdict == "cancel" else verdict,
                               predicted_per_second=100, predicted_n=200)
    patches = helper._auto_patches(server, tmp_path, helper._TWO_DEVICE if verdict == "cancel" else helper._ONE_DEVICE, fetch, bench)
    with patches[0], patches[1], patches[2], patches[4], patch.object(server._gguf_download, "fetch", side_effect=fetch):
        server._run_llm_model_download(a.PUBLIC_ID, recovery=True)
    assert cfg.enhancement_enabled is (verdict == "viable")
    assert cfg.enhancement_local_model == a.PUBLIC_ID
    assert server._benchmark.thread is None
    assert server._download_thread is None
    if verdict == "viable":
        assert get_local_gpu_verdict(cfg, a.PUBLIC_ID) == "viable"
        assert [e["type"] for e in events].count("download.complete") == 1
    elif verdict == "cancel":
        assert devices_benched == [0]
        assert any(e["type"] == "download.cancelled" for e in events)
        assert not any(e["type"] == "download.complete" for e in events)
    else:
        error = next(e for e in events if e["type"] == "download.error")
        assert error["reason"] == "model_benchmark_required" and error["restored"]
        assert not any(e["type"] == "download.complete" for e in events)


@pytest.mark.parametrize("stale_bindings", [{}, {a.PUBLIC_ID: "old-primary-binding"}])
def test_stale_client_cannot_replace_benchmark_artifact_binding(tmp_path, artifacts, stale_bindings):
    from tests.test_server_endpoints import _make_client
    a.artifact_path(tmp_path, a.GOOGLE_ID).write_bytes(artifacts[a.GOOGLE_ID])
    a.select(tmp_path, a.GOOGLE_ID)
    binding = a.receipt(tmp_path)
    cfg = WhisperstreamConfig(
        enhancement_local_gpu_verdicts={a.PUBLIC_ID: "viable"},
        enhancement_local_gpu_artifacts={a.PUBLIC_ID: binding},
    )
    client, server = _make_client(config=cfg)
    payload = cfg.to_dict()
    payload["enhancement_local_gpu_artifacts"] = stale_bindings
    response = client.put("/api/config", json=payload)
    assert response.status_code == 200
    assert response.json()["enhancement_local_gpu_artifacts"] == {a.PUBLIC_ID: binding}
    assert response.json()["enhancement_local_gpu_verdicts"][a.PUBLIC_ID] == "viable"
    saved = server._app.config_manager.save.call_args.args[0]
    assert get_local_gpu_verdict(saved, a.PUBLIC_ID) == "viable"
