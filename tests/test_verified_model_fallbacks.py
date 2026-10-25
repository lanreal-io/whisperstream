"""Exercise the source and archive trust boundaries with real HTTP streaming."""
import hashlib
import io
import tarfile
import threading

import httpx
import pytest


from whisperstream import model_downloader as d


URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/test.tar.bz2"


def archive_bytes(payloads, extra=()):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tf:
        for name, data in payloads.items():
            member = tarfile.TarInfo("model/" + name)
            member.size = len(data)
            tf.addfile(member, io.BytesIO(data))
        for member, data in extra:
            tf.addfile(member, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


@pytest.fixture
def model(monkeypatch):
    mid = d.ModelId.QWEN3_ASR_06B_INT8
    payloads = {name: (name + " payload").encode() for name in d.DOWNLOAD_REGISTRY[mid].files}
    source = {
        "repo": "test/model", "revision": "a" * 40,
        "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in payloads.items()},
    }
    data = archive_bytes(payloads)
    source["github"] = {
        "url": URL, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
        "directory": "model", "member_count": len(payloads),
        "unpacked_size": sum(map(len, payloads.values())),
    }
    monkeypatch.setitem(d.MODEL_SOURCES, mid.value, source)
    return mid, source, payloads, data


def transport(monkeypatch, handler):
    # Preserve the actual source-specific factory, timeout and request hooks.
    real_client = httpx.Client
    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(d.httpx, "Client", client)


@pytest.mark.parametrize("status", [401, 403, 404, 408, 410, 429, 500, 503])
def test_source_failure_uses_pinned_archive_and_preserves_cache(monkeypatch, tmp_path, model, status):
    mid, source, payloads, archive = model
    existing = next(iter(payloads))
    (tmp_path / existing).write_bytes(b"existing manual copy")
    seen = []
    def handler(request):
        seen.append(request)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if request.url.host == "huggingface.co":
            return httpx.Response(status)
        if request.url.host == "github.com":
            assert str(request.url) == URL
            return httpx.Response(302, headers={"location": "https://release-assets.githubusercontent.com/asset"})
        return httpx.Response(200, content=archive)
    transport(monkeypatch, handler)
    progress = []
    d.download(mid, tmp_path, progress_callback=lambda n, total: progress.append((n, total)))
    assert [r.url.host for r in seen] == ["huggingface.co"] * 2 + ["github.com", "release-assets.githubusercontent.com"]
    assert (tmp_path / existing).read_bytes() == b"existing manual copy"
    assert all((tmp_path / name).read_bytes() == data for name, data in payloads.items() if name != existing)
    assert progress[-1][0] == progress[-1][1]
    assert not list(tmp_path.glob(".model-download-*"))


def test_partial_hf_completion_is_retained_and_progress_counts_both_sources(monkeypatch, tmp_path, model):
    mid, _, payloads, archive = model
    first = next(iter(payloads))
    def handler(request):
        if request.url.host == "github.com":
            return httpx.Response(200, content=archive)
        if request.url.path.endswith("/" + first):
            return httpx.Response(200, content=payloads[first])
        return httpx.Response(503)
    transport(monkeypatch, handler)
    replacements = []
    replace = d.os.replace
    def observed_replace(src, dst):
        replacements.append(dst)
        return replace(src, dst)
    monkeypatch.setattr(d.os, "replace", observed_replace)
    reports = []
    d.download(mid, tmp_path, progress_callback=lambda n, total: reports.append((n, total)))
    assert replacements.count(tmp_path / first) == 1
    assert [n for n, _ in reports] == sorted(n for n, _ in reports)
    assert reports[-1] == (len(payloads[first]) + len(archive),) * 2


@pytest.mark.parametrize("failure", ["cancel", "disk", "hash", "redirect", "400", "timeout"])
def test_only_availability_failure_can_start_backup(monkeypatch, tmp_path, model, failure):
    mid, _, _, archive = model
    event = threading.Event()
    seen = []
    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "github.com":
            return httpx.Response(200, content=archive)
        if failure == "timeout":
            raise httpx.ReadTimeout("stalled", request=request)
        if failure == "disk":
            raise OSError("disk full")
        if failure == "cancel":
            event.set()
            return httpx.Response(503)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": URL})
        return httpx.Response(400 if failure == "400" else 200, content=b"bad hash")
    transport(monkeypatch, handler)
    if failure == "timeout":
        d.download(mid, tmp_path)
        assert seen == ["huggingface.co", "huggingface.co", "github.com"]
    else:
        with pytest.raises(d.DownloadCancelled if failure == "cancel" else RuntimeError):
            d.download(mid, tmp_path, cancel_event=event)
        assert seen == ["huggingface.co"]
        assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("target", [
    "https://evil.test/asset", "http://release-assets.githubusercontent.com/asset",
    "https://github.com/stranger/repo/model", "https://huggingface.co/model",
    "https://release-assets.githubusercontent.com.evil.test/asset",
    "https://release-assets.githubusercontent.com:8443/asset",
    "https://user:password@release-assets.githubusercontent.com/asset",
])
def test_github_redirect_policy_is_terminal(monkeypatch, tmp_path, model, target):
    mid = model[0]
    seen = []
    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(503) if request.url.host == "huggingface.co" else httpx.Response(302, headers={"location": target})
    transport(monkeypatch, handler)
    with pytest.raises(RuntimeError):
        d.download(mid, tmp_path)
    assert seen == ["huggingface.co", "huggingface.co", "github.com"]
    assert not list(tmp_path.iterdir()) or all(p.is_dir() and not list(p.iterdir()) for p in tmp_path.iterdir())


@pytest.mark.parametrize("damage", ["hash", "too_long", "short"])
def test_archive_verified_before_parser_runs(monkeypatch, tmp_path, model, damage):
    mid, source, _, archive = model
    if damage == "hash":
        source["github"]["sha256"] = "0" * 64
    else:
        source["github"]["size"] += 1 if damage == "short" else -1
    transport(monkeypatch, lambda request: httpx.Response(503) if request.url.host == "huggingface.co" else httpx.Response(200, content=archive))
    monkeypatch.setattr(d.tarfile, "open", lambda *a, **kw: pytest.fail("unverified archive reached parser"))
    with pytest.raises(RuntimeError):
        d.download(mid, tmp_path)
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


@pytest.mark.parametrize("damage", ["traversal", "absolute", "backslash", "symlink", "hardlink", "duplicate", "count", "expanded", "missing", "file_hash", "file_size", "sparse"])
def test_archive_members_must_match_closed_manifest(monkeypatch, tmp_path, model, damage):
    mid, source, payloads, archive = model
    extra = []
    first = next(iter(payloads))
    if damage in {"traversal", "absolute", "backslash", "symlink", "hardlink", "duplicate"}:
        name = {"traversal": "model/../escape", "absolute": "/escape", "backslash": "model/..\\escape", "duplicate": "model/" + first}.get(damage, "model/link")
        member = tarfile.TarInfo(name)
        if damage in {"symlink", "hardlink"}:
            member.type = tarfile.SYMTYPE if damage == "symlink" else tarfile.LNKTYPE
            member.linkname = "../../escape"
        extra = [(member, None)]
        source["github"]["member_count"] += 1
    elif damage == "count":
        source["github"]["member_count"] -= 1
    elif damage == "expanded":
        source["github"]["unpacked_size"] -= 1
    elif damage == "missing":
        payloads.pop(first)
    elif damage == "file_hash":
        source["files"][first]["sha256"] = "0" * 64
    elif damage == "file_size":
        source["files"][first]["size"] += 1
    elif damage == "sparse":
        monkeypatch.setattr(d.tarfile.TarInfo, "issparse", lambda _: True)
    archive = archive_bytes(payloads, extra)
    source["github"].update(sha256=hashlib.sha256(archive).hexdigest(), size=len(archive))
    transport(monkeypatch, lambda request: httpx.Response(503) if request.url.host == "huggingface.co" else httpx.Response(200, content=archive))
    with pytest.raises(RuntimeError):
        d.download(mid, tmp_path)
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


@pytest.mark.parametrize("when", ["before_request", "last_download_chunk", "extraction", "installation"])
def test_cancel_cleans_scratch_and_preserves_existing(monkeypatch, tmp_path, model, when):
    mid, _, _, archive = model
    event = threading.Event()
    keep = tmp_path / "keep.bin"
    keep.write_bytes(b"working")
    seen = []
    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(503) if request.url.host == "huggingface.co" else httpx.Response(200, content=archive)
    transport(monkeypatch, handler)
    def progress(n, total):
        if when == "last_download_chunk":
            event.set()
    if when == "before_request":
        event.set()
    if when == "extraction":
        original = d.tarfile.TarFile.extractfile
        def extractfile(*args, **kwargs):
            event.set()
            return original(*args, **kwargs)
        monkeypatch.setattr(d.tarfile.TarFile, "extractfile", extractfile)
    if when == "installation":
        original = d._extract_verified_stt_archive
        def extract(*args):
            original(*args)
            event.set()
        monkeypatch.setattr(d, "_extract_verified_stt_archive", extract)
    with pytest.raises(d.DownloadCancelled):
        d.download(mid, tmp_path, cancel_event=event, progress_callback=progress)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == [keep]
    if when == "before_request":
        assert seen == []


def test_failed_backup_does_not_loop(monkeypatch, tmp_path, model):
    seen = []
    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(503)
    transport(monkeypatch, handler)
    with pytest.raises(RuntimeError):
        d.download(model[0], tmp_path)
    assert seen == ["huggingface.co", "huggingface.co", "github.com"]


@pytest.mark.parametrize("where", ["download", "installation"])
def test_filesystem_failure_cleans_scratch_without_replacing_cache(monkeypatch, tmp_path, model, where):
    mid, _, payloads, archive = model
    first = next(iter(payloads))
    (tmp_path / first).write_bytes(b"working")
    seen = []
    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(503) if request.url.host == "huggingface.co" else httpx.Response(200, content=archive)
    transport(monkeypatch, handler)
    replace = d.os.replace
    def failing_replace(src, dst):
        if where == "download" or str(dst).endswith(".onnx"):
            raise OSError("write failed")
        return replace(src, dst)
    monkeypatch.setattr(d.os, "replace", failing_replace)
    with pytest.raises(RuntimeError):
        d.download(mid, tmp_path)
    assert (tmp_path / first).read_bytes() == b"working"
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == [tmp_path / first]
    assert seen == ["huggingface.co", "huggingface.co", "github.com"]


def test_github_pins_only_cover_reviewed_stt_models():
    expected = {d.ModelId.PARAKEET_V2_INT8, d.ModelId.PARAKEET_V3_INT8, d.ModelId.QWEN3_ASR_06B_INT8, d.ModelId.COHERE_TRANSCRIBE_INT8}
    actual = {mid for mid, source in d.MODEL_SOURCES.items() if source.get("github")}
    assert actual == expected
    for mid in actual:
        pin = d.MODEL_SOURCES[mid]["github"]
        assert pin["url"].startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/")
        assert pin["url"].endswith(".tar.bz2")
        assert len(pin["sha256"]) == 64 and pin["size"] > 0
        assert pin["member_count"] > 0 and pin["unpacked_size"] > 0
