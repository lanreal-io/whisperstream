"""REST contract for standard-history recovery, restore and Start fresh.

Uses a real TranscriptStore over an in-memory protected-secret store so the
missing-key path is exercised end to end without touching the OS keychain.

# SECRETS — DO NOT LOG. Recovery keys and passwords here are synthetic.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from whisperstream.protected_secret_contract import (
    MACOS_KEYCHAIN_PREFIX,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretPurpose,
    ProtectionAvailability,
)

pytestmark = pytest.mark.usefixtures("accepted_eula")

PASSWORD = "a long recovery passphrase"


class _Keychain:
    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}

    def availability(self) -> ProtectionAvailability:
        return ProtectionAvailability.AVAILABLE

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        del purpose
        reference = f"{MACOS_KEYCHAIN_PREFIX}{uuid.uuid4()}"
        self.items[reference] = bytes(secret)
        return reference

    def load(self, reference: str) -> bytes:
        if reference not in self.items:
            raise ProtectedSecretError(
                ProtectedSecretOperation.LOAD, ProtectedSecretFailure.NOT_FOUND
            )
        return self.items[reference]

    def delete(self, reference: str) -> None:
        self.items.pop(reference, None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    keychain = _Keychain()
    monkeypatch.setattr("whisperstream.transcript_crypto.get_protected_secret_store", lambda: keychain)

    from whisperstream.config_manager import WhisperstreamConfig
    from whisperstream.server import WhisperstreamServer
    from whisperstream.transcript_store import TranscriptStore

    app = MagicMock()
    app.config = WhisperstreamConfig(history_enabled=True, mac_history_opt_in=True)
    app.history_initializing = False
    app.reload_config.side_effect = lambda cfg: setattr(app, "config", cfg)
    app.transcript_store = TranscriptStore(protected_secret_store=keychain)
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    monkeypatch.setattr(server, "_protected_secret_store", lambda: keychain)
    client = TestClient(server.fastapi)
    yield client, app, keychain, tmp_path
    app.transcript_store.close()


def _save_record(store) -> str:
    record_id = store.save(
        raw_text="synthetic transcript", enhanced_text=None, duration_ms=5,
        model="synthetic", source="dictation", audio_bytes=None,
    )
    assert record_id
    return record_id


def _setup(client, password: str | None = None) -> str:
    response = client.post("/api/history/recovery/setup")
    assert response.status_code == 200, response.text
    recovery_key = response.json()["recovery_key"]
    body = {"generation": response.json()["generation"]}
    if password:
        body["password"] = password
    assert client.post("/api/history/recovery/confirm", json=body).json() == {"ok": True}
    return recovery_key


def _lose_keychain_key(app, keychain, tmp_path):
    from whisperstream.transcript_store import TranscriptStore

    app.transcript_store.close()
    keychain.items.clear()
    app.transcript_store = TranscriptStore(protected_secret_store=keychain)
    assert app.transcript_store.decryptable is False
    assert app.transcript_store.key_failure == "missing"


def test_existing_history_needs_setup_until_confirmed(env) -> None:
    client, app, _keychain, _tmp = env
    _save_record(app.transcript_store)
    status = client.get("/api/history/recovery/status").json()
    assert status["needs_setup"] is True
    assert status["has_recovery_key"] is False
    assert status["record_count"] == 1

    setup = client.post("/api/history/recovery/setup").json()
    shown, generation = setup["recovery_key"], setup["generation"]
    assert client.get("/api/history/recovery/status").json()["needs_setup"] is True

    weak = client.post("/api/history/recovery/confirm", json={"password": "short"})
    assert weak.status_code == 400 and weak.json()["error"] == "password_too_short"
    common = client.post("/api/history/recovery/confirm", json={"password": "password1234"})
    assert common.status_code == 400 and common.json()["error"] == "password_too_common"

    assert client.post(
        "/api/history/recovery/confirm", json={"password": PASSWORD, "generation": generation}
    ).json()["ok"]
    status = client.get("/api/history/recovery/status").json()
    assert status["needs_setup"] is False
    assert status["has_recovery_key"] is True
    assert status["has_recovery_password"] is True
    assert shown.count("-") == 4


def test_missing_key_pauses_saving_and_restores_with_recovery_key(env) -> None:
    client, app, keychain, tmp_path = env
    record_id = _save_record(app.transcript_store)
    recovery_key = _setup(client)
    _lose_keychain_key(app, keychain, tmp_path)

    status = client.get("/api/history/recovery/status").json()
    assert status["key_missing"] is True
    assert status["needs_setup"] is False
    listing = client.get("/api/transcripts").json()
    assert listing["availability"] == "key-missing"
    # Saving is paused: nothing is written and no replacement key is minted.
    assert app.transcript_store.save(
        raw_text="new", enhanced_text=None, duration_ms=1, model="m", source="dictation",
    ) is None
    assert keychain.items == {}

    wrong = client.post(
        "/api/history/recovery/unlock", json={"recovery_key": "AAAA-AAAA-AAAA-AAAA-AAAA"}
    ).json()
    assert wrong["ok"] is False and wrong["error"] == "recovery_key_invalid"

    restored = client.post("/api/history/recovery/unlock", json={"recovery_key": recovery_key})
    assert restored.json() == {"ok": True}, restored.text
    assert app.transcript_store.decryptable is True
    assert app.transcript_store.get(record_id) is not None
    assert len(keychain.items) == 1
    assert client.get("/api/history/recovery/status").json()["key_missing"] is False


def test_recovery_password_restores_and_backs_off(env) -> None:
    client, app, keychain, tmp_path = env
    _save_record(app.transcript_store)
    _setup(client, password=PASSWORD)
    _lose_keychain_key(app, keychain, tmp_path)

    for _ in range(4):
        body = client.post("/api/history/recovery/unlock", json={"password": "wrong wrong wrong"}).json()
        assert body["error"] == "recovery_password_invalid"
    locked = client.post("/api/history/recovery/unlock", json={"password": "wrong wrong wrong"}).json()
    assert locked["lockout_remaining_seconds"] > 0
    blocked = client.post("/api/history/recovery/unlock", json={"password": PASSWORD}).json()
    assert blocked["ok"] is False and blocked["lockout_remaining_seconds"] > 0


def test_unlock_requires_missing_key_and_one_secret(env) -> None:
    client, _app, _keychain, _tmp = env
    _setup(client)
    assert client.post("/api/history/recovery/unlock", json={}).json()["error"] == "missing_secret"
    response = client.post("/api/history/recovery/unlock", json={"recovery_key": "AAAA"})
    assert response.status_code == 409
    assert response.json()["error"] == "history_key_not_missing"


def test_start_fresh_deletes_history_and_keys(env) -> None:
    client, app, keychain, tmp_path = env
    _save_record(app.transcript_store)
    _setup(client)
    _lose_keychain_key(app, keychain, tmp_path)

    assert client.post("/api/history/reset", json={"confirm": "delete"}).status_code == 400
    response = client.post("/api/history/reset", json={"confirm": "DELETE"})
    assert response.json() == {"ok": True}, response.text
    assert app.transcript_store.decryptable is True
    assert app.transcript_store.record_count() == 0
    status = client.get("/api/history/recovery/status").json()
    assert status["key_missing"] is False
    assert status["has_recovery_key"] is False
    assert status["needs_setup"] is True
    assert not (tmp_path / "history_recovery.json").exists()


def test_regenerate_and_password_management(env) -> None:
    client, _app, _keychain, _tmp = env
    old = _setup(client)
    new = client.post("/api/history/recovery/regenerate").json()["recovery_key"]
    assert new != old
    assert client.post("/api/history/recovery/password", json={"password": PASSWORD}).json() == {
        "ok": True,
        "has_recovery_password": True,
    }
    assert client.post("/api/history/recovery/password", json={}).json()["has_recovery_password"] is False
    assert client.get("/api/history/recovery/status").json()["has_recovery_password"] is False


def test_recovery_routes_defer_to_lockdown(env) -> None:
    client, _app, _keychain, _tmp = env
    _setup(client)
    assert client.post("/api/lockdown/enable", json={"password": "test-pass-one"}).status_code == 200
    response = client.post("/api/history/recovery/setup")
    assert response.status_code == 409 and response.json()["error"] == "lockdown_active"
    status = client.get("/api/history/recovery/status").json()
    assert status["lockdown"] is True
    assert status["needs_setup"] is False


def test_lockdown_rejects_common_new_password(env) -> None:
    client, _app, _keychain, _tmp = env
    response = client.post("/api/lockdown/enable", json={"password": "password1234"})
    assert response.status_code == 400
    assert response.json()["error_code"] == "password_too_common"


def test_windows_enable_uses_the_same_route(env, monkeypatch) -> None:
    client, app, _keychain, _tmp = env
    from whisperstream.config_manager import WhisperstreamConfig

    app.config = WhisperstreamConfig(history_enabled=False, mac_history_opt_in=False)
    app._transcript_store_init_thread = None
    monkeypatch.setattr("whisperstream.server.app.platform.system", lambda: "Windows")
    response = client.post("/api/transcripts/enable", json={"history_consent": True})
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "needs_recovery_setup": True}
    assert app.config.history_enabled is True
    assert app.config.mac_history_opt_in is False


def test_enable_reports_missing_key_without_releasing_store(env, monkeypatch) -> None:
    client, app, keychain, tmp_path = env
    _save_record(app.transcript_store)
    _lose_keychain_key(app, keychain, tmp_path)
    app._transcript_store_init_thread = None
    store = app.transcript_store
    response = client.post("/api/transcripts/enable", json={"keychain_consent": True})
    assert response.status_code == 409
    assert response.json() == {"error": "history_key_missing"}
    assert app.transcript_store is store


def test_confirm_switches_a_regenerated_key(env) -> None:
    client, app, keychain, tmp_path = env
    _save_record(app.transcript_store)
    old = _setup(client)
    regenerated = client.post("/api/history/recovery/regenerate").json()
    new = regenerated["recovery_key"]
    assert client.get("/api/history/recovery/status").json()["has_recovery_key"] is True
    assert client.post(
        "/api/history/recovery/confirm", json={"generation": regenerated["generation"]}
    ).json() == {"ok": True}
    _lose_keychain_key(app, keychain, tmp_path)
    wrong = client.post("/api/history/recovery/unlock", json={"recovery_key": old}).json()
    assert wrong["error"] == "recovery_key_invalid"
    assert client.post("/api/history/recovery/unlock", json={"recovery_key": new}).json() == {"ok": True}


def test_stale_envelope_needs_setup_and_cannot_restore(env) -> None:
    client, app, keychain, tmp_path = env
    from whisperstream import history_recovery
    from whisperstream.transcript_store import TranscriptStore

    stale_key = _setup(client)
    # The protected key disappears while history is empty, so a new key is minted.
    app.transcript_store.close()
    keychain.items.clear()
    (tmp_path / "transcript.key").unlink()
    app.transcript_store = TranscriptStore(protected_secret_store=keychain)
    assert app.transcript_store.decryptable is True
    status = client.get("/api/history/recovery/status").json()
    assert status["has_recovery_key"] is False and status["needs_setup"] is True

    _save_record(app.transcript_store)
    _lose_keychain_key(app, keychain, tmp_path)
    response = client.post("/api/history/recovery/unlock", json={"recovery_key": stale_key})
    assert response.status_code == 409 and response.json()["error"] == "recovery_key_stale"
    assert history_recovery.has_recovery(tmp_path)
    assert keychain.items == {}


def test_status_hides_counts_under_lockdown(env) -> None:
    client, app, _keychain, _tmp = env
    _save_record(app.transcript_store)
    _setup(client)
    assert client.post("/api/lockdown/enable", json={"password": "test-pass-one"}).status_code == 200
    status = client.get("/api/history/recovery/status").json()
    assert status["record_count"] is None and status["has_audio"] is False
