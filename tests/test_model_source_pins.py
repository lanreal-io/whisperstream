"""Final-edition delivery exercises real HTTP streaming with a mock transport."""
import hashlib
import re
import logging
import threading
from pathlib import Path

import httpx
import pytest


from whisperstream import model_downloader as d


@pytest.fixture
def model(monkeypatch):
    model = d.ModelId.QWEN3_ASR_06B_INT8
    payloads = {name: (name + ' bytes').encode() for name in d.DOWNLOAD_REGISTRY[model].files}
    source = {'repo': 'test/model', 'revision': 'a' * 40,
              'files': {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in payloads.items()}}
    monkeypatch.setitem(d.MODEL_SOURCES, model.value, source)
    return model, source, payloads


def transport(monkeypatch, handler):
    monkeypatch.setattr(d, 'new_http_client', lambda: httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True,
        event_hooks={'request': [d._validate_hf_request]}))


def test_pinned_download_skips_existing_files_and_has_no_credentials(monkeypatch, tmp_path, model):
    mid, source, payloads = model
    first = next(iter(payloads))
    (tmp_path / first).write_bytes(b'existing')
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.host == 'huggingface.co'
        assert '/resolve/' + source['revision'] + '/' in request.url.path
        assert 'authorization' not in request.headers
        assert 'cookie' not in request.headers
        name = request.url.path.split(source['revision'] + '/', 1)[1]
        return httpx.Response(200, content=payloads[name])
    transport(monkeypatch, handle)
    progress = []
    d.download(mid, tmp_path, progress_callback=lambda n,t: progress.append((n,t)))
    assert len(requests) == len(payloads)-1
    assert (tmp_path / first).read_bytes() == b'existing'
    for name,data in list(payloads.items())[1:]: assert (tmp_path/name).read_bytes() == data
    assert [n for n,_ in progress] == sorted(n for n,_ in progress)
    assert progress[-1][0] == progress[-1][1]
    d.download(mid, tmp_path)
    assert len(requests) == len(payloads)-1


@pytest.mark.parametrize('status', [403,404,429,503])
def test_hf_failure_never_falls_back(monkeypatch,tmp_path,model,status):
    seen=[]
    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(status)
    transport(monkeypatch,handle)
    with pytest.raises(RuntimeError,match='Check your connection'):
        d.download(model[0],tmp_path)
    assert len(seen)==2
    assert all(url.startswith('https://huggingface.co/test/model/resolve/') for url in seen)
    assert not list(tmp_path.rglob('*.tmp'))


def test_cancel_is_terminal(monkeypatch,tmp_path,model):
    event=threading.Event(); seen=[]
    def handle(request):
        seen.append(request); event.set()
        return httpx.Response(200,content=b'cancel')
    transport(monkeypatch,handle)
    with pytest.raises(d.DownloadCancelled): d.download(model[0],tmp_path,cancel_event=event)
    assert len(seen)==1
    assert not list(tmp_path.rglob('*.tmp'))


def test_bad_hash_is_atomic_and_not_retried(monkeypatch,tmp_path,model):
    seen=[]
    def handle(request):
        seen.append(request); return httpx.Response(200,content=b'corrupt')
    transport(monkeypatch,handle)
    with pytest.raises(RuntimeError): d.download(model[0],tmp_path)
    assert len(seen)==1
    assert not [p for p in tmp_path.rglob('*') if p.is_file()]


@pytest.mark.parametrize('target',['https://whisperstream.io/model','http://huggingface.co/model','https://evil.test/model'])
def test_redirects_fail_closed(monkeypatch,tmp_path,model,target):
    seen=[]
    def handle(request):
        seen.append(request);return httpx.Response(302,headers={'location':target})
    transport(monkeypatch,handle)
    with pytest.raises(RuntimeError):d.download(model[0],tmp_path)
    assert len(seen)==1


def test_hf_cdn_redirect_is_allowed(monkeypatch,tmp_path,model):
    mid,source,payloads=model
    def handle(request):
        if request.url.host=='huggingface.co':
            return httpx.Response(302,headers={'location':'https://cas-bridge.xethub.hf.co'+request.url.path})
        name=request.url.path.split(source['revision']+'/',1)[1]
        return httpx.Response(200,content=payloads[name])
    transport(monkeypatch,handle);d.download(mid,tmp_path)
    assert all((tmp_path/name).read_bytes()==data for name,data in payloads.items())


def test_all_registry_sources_are_pinned_and_complete():
    for mid,spec in d.DOWNLOAD_REGISTRY.items():
        source=d.MODEL_SOURCES[mid.value]
        assert len(source['revision'])==40
        assert all(len(f['sha256'])==64 and f['size']>0 for f in source['files'].values())
        if not source.get('archive'): assert set(spec.files)<=set(source['files'])


def test_stream_1_mini_revision_is_pinned():
    assert re.fullmatch(r"[0-9a-f]{40}", d.MODEL_SOURCES["stream-1-mini"]["revision"])


def test_supertonic_install_failure_preserves_previous_files(monkeypatch,tmp_path):
    monkeypatch.setattr('whisperstream.config_manager.get_models_root',lambda:tmp_path)
    mid=d.ModelId.SUPERTONIC_3_INT8;dest=d.model_dir(mid);dest.mkdir()
    (dest/'keep.bin').write_bytes(b'existing')
    def fail(*args,**kwargs):raise OSError('offline')
    monkeypatch.setattr(d,'_download_from_hf',fail)
    with pytest.raises(OSError):d.download_archive(mid)
    assert (dest/'keep.bin').read_bytes()==b'existing'
    assert not list(tmp_path.glob('*.extract.tmp'))
