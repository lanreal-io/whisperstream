"""Exercise pinned public model delivery at the real HTTPX request boundary."""
import hashlib
import io
import tarfile
import threading

import httpx
import pytest

from whisperstream import model_downloader as d


@pytest.fixture
def payload(monkeypatch):
    mid = d.ModelId.PARAKEET_V3_INT8
    files = {n: n.encode() for n in d.DOWNLOAD_REGISTRY[mid].files}
    source = {"repo": "test/model", "revision": "a" * 40,
              "files": {n: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                        for n, b in files.items()}}
    monkeypatch.setitem(d.MODEL_SOURCES, mid.value, source)
    return mid, files


def transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(d.httpx, "Client", lambda **kw: original(
        transport=httpx.MockTransport(handler), **kw))


def test_download_uses_pinned_hugging_face_files(monkeypatch, tmp_path, payload):
    mid, files = payload
    seen = []
    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=files[request.url.path.rsplit("/", 1)[-1]])
    transport(monkeypatch, handle)
    d.download(mid, tmp_path)
    assert set(seen) == {f"https://huggingface.co/test/model/resolve/{'a' * 40}/{n}" for n in files}
    assert all((tmp_path / n).read_bytes() == b for n, b in files.items())


def test_model_sources_have_no_company_delivery_route():
    for source in d.MODEL_SOURCES.values():
        assert set(source) <= {"repo", "revision", "files", "github"}
        for value in source.get("github", {}).values():
            assert "whisperstream.io" not in str(value)


@pytest.mark.parametrize("target", ["https://github.com/test", "http://huggingface.co/test",
    "https://evil.test/test", "https://huggingface.co.evil.test/test"])
def test_hugging_face_redirect_cannot_escape(monkeypatch, payload, tmp_path, target):
    seen = []
    transport(monkeypatch, lambda r: seen.append(r) or httpx.Response(302, headers={"location": target}))
    with pytest.raises(RuntimeError):
        d.download(payload[0], tmp_path)
    assert len(seen) == 1


@pytest.mark.parametrize("header", ["Authorization"])
def test_hugging_face_rejects_credentials_before_transport(monkeypatch, header):
    transport(monkeypatch, lambda r: pytest.fail("credential-bearing request escaped"))
    with d.new_http_client() as client:
        with pytest.raises(RuntimeError):
            client.get("https://huggingface.co/test", headers={header: "test"})


def test_github_client_only_allows_the_pinned_upstream_asset(monkeypatch):
    seen = []
    transport(monkeypatch, lambda r: seen.append(r) or httpx.Response(200))
    url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/test.tar.bz2"
    with d.new_http_client(github_url=url) as client:
        assert client.get(url).status_code == 200
        with pytest.raises(RuntimeError):
            client.get("https://huggingface.co/test")
    assert len(seen) == 1


def test_cancel_and_cache_do_not_make_requests(monkeypatch, payload, tmp_path):
    mid, files = payload
    transport(monkeypatch, lambda r: pytest.fail("unexpected network"))
    event = threading.Event()
    event.set()
    with pytest.raises(d.DownloadCancelled):
        d.download(mid, tmp_path, cancel_event=event)
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    d.download(mid, tmp_path)


def test_corruption_is_terminal(monkeypatch, payload, tmp_path):
    seen = []
    transport(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, content=b"bad"))
    with pytest.raises(RuntimeError):
        d.download(payload[0], tmp_path)
    assert len(seen) == 1
    assert not list(tmp_path.glob("*.onnx"))


def test_source_failure_without_github_backup_retries_hugging_face_only(monkeypatch, payload, tmp_path):
    seen = []
    transport(monkeypatch, lambda r: seen.append(r.url.host) or httpx.Response(503))
    with pytest.raises(RuntimeError):
        d.download(payload[0], tmp_path)
    assert seen == ["huggingface.co"] * 2


@pytest.mark.parametrize("mid", [mid for mid, spec in d.DOWNLOAD_REGISTRY.items()
                                if spec.kind != "tts" and mid.value in d.MODEL_SOURCES])
def test_every_file_model_downloads_from_hugging_face(monkeypatch, tmp_path, mid):
    source = dict(d.MODEL_SOURCES[mid.value])
    contents = {n: ("model " + n).encode() for n in source["files"]}
    source["files"] = {n: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                       for n, b in contents.items()}
    monkeypatch.setitem(d.MODEL_SOURCES, mid.value, source)
    seen = []
    def handle(request):
        seen.append(request.url.host)
        name = next(n for n in contents if request.url.path.endswith("/" + n))
        return httpx.Response(200, content=contents[name])
    transport(monkeypatch, handle)
    d.download(mid, tmp_path)
    assert set(seen) == {"huggingface.co"}
    # Existing regular files remain usable without another request.
    seen.clear()
    d.download(mid, tmp_path)
    assert not seen


def test_stream_1_mini_downloads_its_pinned_file_and_rejects_other_hosts(
    monkeypatch, tmp_path,
):
    mid = d.ModelId.STREAM_1_MINI_Q4
    body = b"stream-1-mini weights"
    source = dict(d.MODEL_SOURCES[mid.value])
    source["files"] = {"stream-1-mini-vocabulary-q4_k_m.gguf": {
        "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}}
    monkeypatch.setitem(d.MODEL_SOURCES, mid.value, source)
    # A cached E artifact must not satisfy the new vocabulary revision.
    (tmp_path / "stream-1-mini-q4_k_m.gguf").write_bytes(b"historical E")
    seen = []
    transport(monkeypatch, lambda r: seen.append(str(r.url)) or httpx.Response(200, content=body))
    d.download(mid, tmp_path)
    assert (tmp_path / "stream-1-mini-q4_k_m.gguf").read_bytes() == b"historical E"
    assert seen == [
        "https://huggingface.co/whisperstream/stream-1-mini/resolve/"
        f"{source['revision']}/stream-1-mini-vocabulary-q4_k_m.gguf"
    ]
    assert (tmp_path / "stream-1-mini-vocabulary-q4_k_m.gguf").read_bytes() == body

    (tmp_path / "stream-1-mini-vocabulary-q4_k_m.gguf").unlink()
    transport(monkeypatch, lambda r: httpx.Response(
        302, headers={"location": "https://example.com/x.gguf"}))
    with pytest.raises(RuntimeError):
        d.download(mid, tmp_path)


@pytest.fixture
def supertonic(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("whisperstream.config_manager.get_models_root", lambda: tmp_path / "models")
    mid = d.ModelId.SUPERTONIC_3_INT8
    source = dict(d.MODEL_SOURCES[mid.value])
    contents = {n: ("voice " + n).encode() for n in source["files"]}
    source["files"] = {n: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                       for n, b in contents.items()}
    monkeypatch.setitem(d.MODEL_SOURCES, mid.value, source)
    return mid, source, contents


def test_voice_delivery_and_progress(monkeypatch, supertonic):
    mid, source, contents = supertonic
    seen = []
    def handle(request):
        seen.append(request.url.host)
        return httpx.Response(200, content=contents[request.url.path.rsplit("/", 1)[-1]])
    transport(monkeypatch, handle)
    progress = []
    d.download_archive(mid, progress_callback=lambda done, total: progress.append((done, total)))
    assert set(seen) == {"huggingface.co"}
    assert all((d.model_dir(mid) / n).read_bytes() == b for n, b in contents.items())
    assert progress[-1][0] == progress[-1][1]
    seen.clear()
    d.download_archive(mid)
    assert not seen


@pytest.mark.parametrize("failure", ["file_hash", "cancel"])
def test_voice_failure_preserves_old_bundle(monkeypatch, supertonic, failure):
    mid, source, contents = supertonic
    dest = d.model_dir(mid)
    dest.mkdir(parents=True)
    (dest / "voice.bin").write_bytes(b"old voice")
    # A stale archive scratch file from an earlier release is cleaned up.
    stale = dest.parent / f"{d.DOWNLOAD_REGISTRY[mid].dir_name}.tar.bz2"
    stale.write_bytes(b"stale")
    event = threading.Event()
    if failure == "file_hash":
        source["files"]["voice.bin"]["sha256"] = "0" * 64
    transport(monkeypatch, lambda r: httpx.Response(200, content=contents[r.url.path.rsplit("/", 1)[-1]]))
    def progress(*_):
        if failure == "cancel":
            event.set()
    with pytest.raises(d.DownloadCancelled if failure == "cancel" else RuntimeError):
        d.download_archive(mid, cancel_event=event, progress_callback=progress)
    assert (dest / "voice.bin").read_bytes() == b"old voice"
    assert not list(dest.parent.glob("*.tmp"))
    assert not list(dest.parent.glob("*.bz2"))


def test_upstream_kokoro_delivery_uses_pinned_files(monkeypatch, tmp_path):
    from whisperstream.speech import availability
    monkeypatch.setattr(availability, "require_engine_available", lambda _: None)
    monkeypatch.setattr(availability, "kokoro_download_enabled", lambda: True)
    monkeypatch.setattr("whisperstream.config_manager.get_models_root", lambda: tmp_path)
    mid = d.ModelId.KOKORO_82M_FP32
    source = dict(d.MODEL_SOURCES[mid.value])
    assert "archive" not in source
    contents = {name: name.encode() for name in source["files"]}
    source["files"] = {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                       for name, data in contents.items()}
    monkeypatch.setitem(d.MODEL_SOURCES, mid.value, source)
    seen = []
    def handle(request):
        seen.append(str(request.url))
        name = next(name for name in contents if request.url.path.endswith("/" + name))
        return httpx.Response(200, content=contents[name])
    transport(monkeypatch, handle)
    d.download_archive(mid)
    assert set(seen) == {f"https://huggingface.co/{source['repo']}/resolve/{source['revision']}/{name}"
                         for name in contents}
    assert all((d.model_dir(mid) / name).read_bytes() == data
               for name, data in contents.items())


def test_partial_multifile_download_resumes_remaining_files(monkeypatch, payload, tmp_path):
    mid, files = payload
    first = next(iter(files))
    seen = []
    failing = [True]
    def handle(request):
        name = request.url.path.rsplit("/", 1)[-1]
        seen.append(name)
        if name != first and failing[0]:
            return httpx.Response(404)
        return httpx.Response(200, content=files[name])
    transport(monkeypatch, handle)
    with pytest.raises(RuntimeError):
        d.download(mid, tmp_path)
    assert (tmp_path / first).read_bytes() == files[first]
    seen.clear()
    failing[0] = False
    d.download(mid, tmp_path)
    assert first not in seen
    assert all((tmp_path / n).read_bytes() == b for n, b in files.items())
