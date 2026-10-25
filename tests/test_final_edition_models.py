"""Production boundary and migration do not depend on UI visibility."""
import pytest
from whisperstream import _build_flags
from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.speech.availability import kokoro_available, kokoro_cached, kokoro_download_enabled
from whisperstream.speech.engine_specs import get_engine_spec
from whisperstream.speech.selection import normalize_selection
from whisperstream import model_downloader as d
from tests.test_speech_endpoints import _make_client, _make_mock_config

pytestmark = pytest.mark.usefixtures('accepted_eula')

@pytest.mark.parametrize('compiled,runtime', [(False,'1'),(True,'0'),(True,'')])
def test_standard_offers_kokoro_without_experimental_gate(monkeypatch,tmp_path,compiled,runtime):
    monkeypatch.setattr(_build_flags, 'EXPERIMENTAL_AVAILABLE', compiled)
    monkeypatch.setenv('WHISPERSTREAM_EXPERIMENTAL', runtime)
    assert kokoro_available()
    assert kokoro_download_enabled()
    assert normalize_selection('kokoro') == 'kokoro'
    assert normalize_selection('auto') == 'auto'
    assert get_engine_spec('kokoro').key == 'kokoro'
    from whisperstream.speech.kokoro_engine import KokoroEngine
    KokoroEngine()
    client, _ = _make_client(config=_make_mock_config(agent_speech_engine='kokoro'))
    assert set(client.get('/api/speech/status').json()['engines']) == {'kokoro', 'supertonic'}
    assert client.get('/api/speech/voices?engine=kokoro').status_code == 200

@pytest.mark.parametrize('engine',['auto','kokoro','supertonic'])
def test_migration_preserves_speech_enabled(monkeypatch,engine):
    monkeypatch.delenv('WHISPERSTREAM_EXPERIMENTAL',raising=False)
    config=WhisperstreamConfig.from_dict({'agent_speech_engine':engine,'agent_speech_enabled':True})
    assert config.agent_speech_engine == engine
    assert config.agent_speech_enabled




@pytest.fixture
def cached_kokoro(monkeypatch, tmp_path):
    monkeypatch.setattr(_build_flags, "EXPERIMENTAL_AVAILABLE", False)
    monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    spec = d.DOWNLOAD_REGISTRY[d.ModelId.KOKORO_82M_FP32]
    directory = tmp_path / "models" / spec.dir_name
    for name in spec.files:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cached model fixture")
    return directory

@pytest.mark.parametrize("engine", ["auto", "kokoro"])
def test_standard_preserves_complete_cached_kokoro(cached_kokoro, engine, monkeypatch):
    assert kokoro_available()
    assert normalize_selection(engine) == engine
    config = WhisperstreamConfig.from_dict({"agent_speech_engine": engine, "agent_speech_enabled": True})
    assert config.agent_speech_engine == engine
    assert config.agent_speech_enabled
    assert get_engine_spec("kokoro").key == "kokoro"
    from whisperstream.speech.kokoro_engine import KokoroEngine
    KokoroEngine()  # Construction remains lazy; no inference with fixture bytes.
    client, _ = _make_client(config=_make_mock_config(agent_speech_engine=engine))
    assert "kokoro" in client.get("/api/speech/status").json()["engines"]
    assert client.get("/api/speech/voices?engine=kokoro").status_code == 200
    monkeypatch.setattr(d.httpx, "Client", lambda *a, **kw: pytest.fail("cached model attempted HTTP"))
    d.download_archive(d.ModelId.KOKORO_82M_FP32)

@pytest.mark.parametrize("damage", ["missing", "empty"])
def test_partial_kokoro_preserves_preference_and_allows_repair(cached_kokoro, damage):
    path = cached_kokoro / d.DOWNLOAD_REGISTRY[d.ModelId.KOKORO_82M_FP32].files[-1]
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'')
    assert not kokoro_cached()
    assert kokoro_available()
    assert kokoro_download_enabled()
    assert WhisperstreamConfig.from_dict({'agent_speech_engine': 'kokoro'}).agent_speech_engine == 'kokoro'

def test_custom_storage_preserves_cached_kokoro(cached_kokoro, monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path / "other"))
    config = WhisperstreamConfig.from_dict({"model_storage_dir": str(tmp_path), "agent_speech_engine": "kokoro"})
    assert config.agent_speech_engine == "kokoro"

def test_cached_model_disappearing_preserves_download_permission(cached_kokoro, monkeypatch):
    monkeypatch.setattr(d, 'is_present', lambda model: False)
    def unavailable(**kwargs):
        raise OSError('synthetic transport unavailable')
    monkeypatch.setattr(d, 'new_http_client', unavailable)
    with pytest.raises(OSError, match='synthetic transport unavailable'):
        d.download_archive(d.ModelId.KOKORO_82M_FP32)

@pytest.mark.parametrize("system,expected", [("Windows", "supertonic"), ("Darwin", "auto")])
def test_new_preference_keeps_platform_default_even_with_cached_kokoro(cached_kokoro, monkeypatch, system, expected):
    monkeypatch.setattr("whisperstream.config_manager.platform.system", lambda: system)
    assert WhisperstreamConfig.from_dict({}).agent_speech_engine == expected


def test_status_recognizes_legacy_kokoro_cache(monkeypatch, tmp_path):
    from whisperstream.speech.availability import LEGACY_KOKORO_DIR, LEGACY_KOKORO_FILES
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    directory = tmp_path / "models" / LEGACY_KOKORO_DIR
    directory.mkdir(parents=True)
    for name in LEGACY_KOKORO_FILES:
        (directory / name).write_bytes(b"synthetic legacy file")
    assert not d.is_present(d.ModelId.KOKORO_82M_FP32)
    client, _ = _make_client(config=_make_mock_config(agent_speech_engine="kokoro"))
    assert client.get("/api/speech/status").json()["engines"]["kokoro"]["installed"]
