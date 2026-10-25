"""Synthetic Windows failure paths must never persist unsealed secrets."""
import sys
from types import SimpleNamespace

import pytest

from whisperstream import key_encryption, transcript_crypto, lockdown_keystore
from whisperstream.config_manager import ConfigManager, WhisperstreamConfig
from whisperstream.protected_secret_contract import ProtectedSecretError
from whisperstream.windows_protected_secret import WindowsDpapiProtectedSecretStore


def use_dpapi(monkeypatch, native):
    from whisperstream import windows_protected_secret as adapter
    monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")
    monkeypatch.setattr(adapter, "win32crypt", native)
    monkeypatch.setattr(adapter, "_WIN32CRYPT_AVAILABLE", native is not None)
    store = WindowsDpapiProtectedSecretStore()
    monkeypatch.setattr(key_encryption, "get_protected_secret_store", lambda: store)
    monkeypatch.setattr(transcript_crypto, "get_protected_secret_store", lambda: store)


@pytest.fixture(params=["missing", "failure"])
def broken_dpapi(request, monkeypatch):
    def fail(*args):
        raise RuntimeError("synthetic secret must not reach errors")
    use_dpapi(monkeypatch, None if request.param == "missing" else SimpleNamespace(CryptProtectData=fail))


def test_encrypt_failure_raises_without_secret(broken_dpapi):
    with pytest.raises(RuntimeError) as error:
        key_encryption.encrypt_api_key("synthetic-private-key")
    assert "synthetic" not in str(error.value)


@pytest.mark.parametrize("ciphertext", [None, b"", "unexpected plaintext"])
def test_invalid_dpapi_result_cannot_be_persisted(monkeypatch, ciphertext):
    use_dpapi(monkeypatch, SimpleNamespace(CryptProtectData=lambda *args: ciphertext))
    with pytest.raises(ProtectedSecretError):
        key_encryption.encrypt_api_key("synthetic-private-key")


@pytest.mark.parametrize("field", ["enhancement_api_key", "enhancement_gemini_api_key", "enhancement_openai_api_key", "enhancement_anthropic_api_key", "enhancement_custom_api_key"])
def test_config_failure_preserves_prior_file(tmp_path, monkeypatch, broken_dpapi, field):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    manager = ConfigManager()
    prior = b'# prior encrypted config\nenhancement_api_key = "enc:cHJpb3I="\n'
    manager.config_path.write_bytes(prior)
    config = WhisperstreamConfig()
    setattr(config, field, "synthetic-private-key")
    with pytest.raises(RuntimeError):
        manager.save(config)
    assert manager.config_path.read_bytes() == prior


def test_first_history_key_failure_leaves_no_file(tmp_path, broken_dpapi):
    path = tmp_path / "transcript.key"
    assert transcript_crypto.load_or_create_data_key(path) is None
    assert not path.exists()


def test_reseal_failure_preserves_existing_key(tmp_path, broken_dpapi):
    path = tmp_path / "transcript.key"
    path.write_text("enc:prior", encoding="utf-8")
    with pytest.raises(RuntimeError):
        transcript_crypto.save_data_key(path, b"s" * 32)
    assert path.read_text(encoding="utf-8") == "enc:prior"


@pytest.mark.parametrize("prepare", [False, True])
def test_lockdown_disable_failure_preserves_envelope(tmp_path, monkeypatch, broken_dpapi, prepare):
    envelope = tmp_path / "lockdown.key.json"
    envelope.write_text('{"synthetic": "recovery envelope"}', encoding="utf-8")
    monkeypatch.setattr(lockdown_keystore, "is_lockdown_enabled", lambda _: True)
    monkeypatch.setattr(lockdown_keystore, "unlock_with_password", lambda *_: b"s" * 32)
    operation = lockdown_keystore.prepare_disable_lockdown if prepare else lockdown_keystore.disable_lockdown
    assert not operation(tmp_path, "synthetic password")
    assert envelope.read_text(encoding="utf-8") == '{"synthetic": "recovery envelope"}'
    assert not (tmp_path / "transcript.key").exists()
