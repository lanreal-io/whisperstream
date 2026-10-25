"""Fresh Mac voice choice can resolve Kokoro without changing saved choices."""
from unittest.mock import MagicMock
import pytest
from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.macos_tts_playback import MacOSSpeechSynthesisAdapter
from whisperstream.speech.selection import EngineManager, MemorySnapshot, SelectionPolicy


@pytest.mark.parametrize('system,expected', [('Darwin','auto'),('Windows','supertonic')])
@pytest.mark.parametrize('saved', [None, '', 'invalid', 'kitten', 'kittentts'])
def test_missing_invalid_and_legacy_voice_defaults_follow_platform(monkeypatch, system, expected, saved):
    monkeypatch.setattr('whisperstream.config_manager.platform.system', lambda: system)
    assert WhisperstreamConfig().agent_speech_engine == expected
    payload = {} if saved is None else {'agent_speech_engine': saved}
    assert WhisperstreamConfig.from_dict(payload).agent_speech_engine == expected


@pytest.mark.parametrize('system', ['Darwin','Windows'])
@pytest.mark.parametrize('saved', ['auto','kokoro','supertonic'])
def test_explicit_voice_preferences_survive_platform_defaults(monkeypatch, system, saved):
    monkeypatch.setattr('whisperstream.config_manager.platform.system', lambda: system)
    assert WhisperstreamConfig.from_dict({'agent_speech_engine': saved}).agent_speech_engine == saved


def test_fresh_mac_automatic_voice_can_resolve_reviewed_kokoro_runtime(monkeypatch):
    monkeypatch.setattr('whisperstream.config_manager.platform.system', lambda: 'Darwin')
    engine = MagicMock()
    engine.is_installed.return_value = True
    manager = EngineManager(engine_factory=lambda key: engine)
    adapter = MacOSSpeechSynthesisAdapter(engine_manager=manager)
    policy = SelectionPolicy(manager, memory_probe=lambda: MemorySnapshot(16*1024,4*1024))
    try:
        default = WhisperstreamConfig().agent_speech_engine
        resolved = policy.resolve(default, supertonic_voice=1, kokoro_voice='af_heart')
        assert [candidate.engine_key for candidate in resolved.candidates] == ['kokoro']
        explicit = policy.resolve('supertonic', supertonic_voice=1, kokoro_voice='af_heart')
        assert not explicit.candidates
        assert manager.health_reason('supertonic') == 'runtime_unavailable'
    finally:
        adapter.close()
