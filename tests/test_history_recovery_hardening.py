"""Regressions for the 2026-10-23 history key recovery security review.

Each test reproduces one finding from an independent security review of
history key recovery (or its follow-up re-reviews) against the real
TranscriptStore and server routes over an in-memory protected-secret store.

# SECRETS — DO NOT LOG. Every key and password here is synthetic.
"""

from __future__ import annotations

import pytest

from tests.test_history_recovery_api import (  # noqa: F401 - pytest fixture import
    PASSWORD,
    _lose_keychain_key,
    _save_record,
    _setup,
    env,
)

pytestmark = pytest.mark.usefixtures("accepted_eula")

LOCKDOWN_PASSWORD = "test-pass-one"


def _enable_lockdown(client) -> str:
    response = client.post("/api/lockdown/enable", json={"password": LOCKDOWN_PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["recovery_code"]


# --- Finding 1: every password check shares the unlock backoff ---------------


def _lock_out(client) -> None:
    for _ in range(5):
        client.post("/api/lockdown/unlock", json={"password": "wrong password guess"})
    assert client.get("/api/lockdown/status").json()["lockout_remaining_seconds"] > 0


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/lockdown/change-password", {"current_password": LOCKDOWN_PASSWORD, "new_password": "another long passphrase"}),
        ("/api/lockdown/view-recovery", {"password": LOCKDOWN_PASSWORD}),
        ("/api/lockdown/disable", {"password": LOCKDOWN_PASSWORD}),
    ],
)
def test_password_routes_refuse_during_lockout(env, path, body) -> None:
    client, _app, _keychain, tmp_path = env
    from whisperstream import lockdown_keystore

    _enable_lockdown(client)
    _lock_out(client)
    response = client.post(path, json=body).json()
    assert response["ok"] is False
    assert response["lockout_remaining_seconds"] > 0
    # Even the correct password does nothing while locked out.
    assert lockdown_keystore.is_lockdown_enabled(tmp_path)
    assert lockdown_keystore.unlock_with_password(tmp_path, LOCKDOWN_PASSWORD) is not None


def test_wrong_passwords_on_management_routes_count_toward_lockout(env) -> None:
    client, _app, _keychain, _tmp = env
    _enable_lockdown(client)
    for _ in range(5):
        client.post(
            "/api/lockdown/change-password",
            json={"current_password": "wrong password guess", "new_password": "another long passphrase"},
        )
    assert client.get("/api/lockdown/status").json()["lockout_remaining_seconds"] > 0
    blocked = client.post("/api/lockdown/unlock", json={"password": LOCKDOWN_PASSWORD}).json()
    assert blocked["ok"] is False and blocked["lockout_remaining_seconds"] > 0


# --- Finding 3: setup never overwrites the envelope that opens history ------


def _install_mismatched_key(app, keychain, tmp_path) -> None:
    """Swap in a valid key that belongs to different history (a restored backup)."""
    import base64
    import os

    from whisperstream.protected_secret_contract import ProtectedSecretPurpose
    from whisperstream.transcript_store import TranscriptStore

    app.transcript_store.close()
    reference = keychain.store(
        base64.b64encode(os.urandom(32)), purpose=ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY
    )
    (tmp_path / "transcript.key").write_text(reference, encoding="utf-8")
    app.transcript_store = TranscriptStore(protected_secret_store=keychain)
    assert app.transcript_store.decryptable is True
    assert app.transcript_store.probe_content_integrity() is False


def test_mismatched_key_offers_restore_and_refuses_setup(env) -> None:
    client, app, keychain, tmp_path = env
    from whisperstream import history_recovery

    record_id = _save_record(app.transcript_store)
    recovery_key = _setup(client)
    envelope = history_recovery.envelope_path(tmp_path).read_bytes()
    _install_mismatched_key(app, keychain, tmp_path)

    status = client.get("/api/history/recovery/status").json()
    assert status["key_mismatch"] is True
    assert status["needs_setup"] is False
    assert status["has_recovery_key"] is True

    for path in ("setup", "regenerate", "password", "confirm"):
        response = client.post(f"/api/history/recovery/{path}", json={})
        assert response.status_code == 409, path
        assert response.json()["error"] == "history_key_mismatch", path
    assert history_recovery.envelope_path(tmp_path).read_bytes() == envelope

    restored = client.post("/api/history/recovery/unlock", json={"recovery_key": recovery_key})
    assert restored.json() == {"ok": True}
    assert app.transcript_store.probe_content_integrity() is True
    assert app.transcript_store.get(record_id)["raw_text"] == "synthetic transcript"


def test_setup_refuses_to_replace_a_working_recovery_key(env) -> None:
    client, app, _keychain, tmp_path = env
    from whisperstream import history_recovery

    _save_record(app.transcript_store)
    _setup(client)
    envelope = history_recovery.envelope_path(tmp_path).read_bytes()
    response = client.post("/api/history/recovery/setup")
    assert response.status_code == 409
    assert response.json()["error"] == "recovery_already_set_up"
    assert history_recovery.envelope_path(tmp_path).read_bytes() == envelope


# --- Finding 4: Start fresh can't lose a concurrent save --------------------


def test_save_during_start_fresh_lands_under_the_new_key(env) -> None:
    import threading
    import time

    _client, app, keychain, tmp_path = env
    from whisperstream.transcript_crypto import delete_data_key

    store = app.transcript_store
    _save_record(store)
    saved: list[str | None] = []

    def remove_old_keys() -> None:
        # A save arrives mid-reset. It must wait for the new key, not use the
        # old one whose protected copy is being deleted right now.
        worker = threading.Thread(target=lambda: saved.append(_save_record(store)))
        worker.start()
        time.sleep(0.2)
        assert worker.is_alive()
        delete_data_key(tmp_path / "transcript.key", store=keychain)
        remove_old_keys.worker = worker

    assert store.wipe_and_rekey(remove_old_keys) is True
    remove_old_keys.worker.join(timeout=5)
    assert saved and saved[0]
    assert store.record_count() == 1
    assert store.probe_content_integrity() is True
    # The surviving row opens with the key that is now protected on disk.
    from whisperstream.transcript_crypto import load_data_key

    disk_key, _failure = load_data_key(tmp_path / "transcript.key", store=keychain)
    assert disk_key is not None and store.key_opens_history(disk_key)


def test_start_fresh_route_rekeys_atomically(env) -> None:
    client, app, _keychain, tmp_path = env
    from whisperstream import history_recovery

    _save_record(app.transcript_store)
    _setup(client)
    response = client.post("/api/history/reset", json={"confirm": "DELETE"})
    assert response.json() == {"ok": True}
    assert app.transcript_store.record_count() == 0
    assert app.transcript_store.decryptable is True
    assert not history_recovery.envelope_path(tmp_path).exists()


# --- Finding 5: every envelope proves which key it wraps --------------------


def test_lockdown_round_trip_keeps_a_key_check(env) -> None:
    client, app, _keychain, tmp_path = env
    from whisperstream import history_recovery

    _save_record(app.transcript_store)
    _setup(client)
    _enable_lockdown(client)
    blob = (tmp_path / "lockdown.key.json").read_text(encoding="utf-8")
    assert "key_check" in blob
    assert client.post("/api/lockdown/disable", json={"password": LOCKDOWN_PASSWORD}).json()["ok"]
    key = app.transcript_store.data_key_for_recovery()
    assert history_recovery.matches_key(tmp_path, key)


def test_unchecked_envelope_does_not_count_until_it_proves_its_key(env) -> None:
    import json

    client, app, keychain, tmp_path = env
    from whisperstream import history_recovery

    _save_record(app.transcript_store)
    recovery_key = _setup(client)
    path = history_recovery.envelope_path(tmp_path)
    blob = json.loads(path.read_text(encoding="utf-8"))
    del blob["key_check"]
    path.write_text(json.dumps(blob), encoding="utf-8")

    key = app.transcript_store.data_key_for_recovery()
    assert not history_recovery.matches_key(tmp_path, key)
    assert client.get("/api/history/recovery/status").json()["needs_setup"] is True

    _lose_keychain_key(app, keychain, tmp_path)
    assert client.post("/api/history/recovery/unlock", json={"recovery_key": recovery_key}).json() == {"ok": True}
    assert history_recovery.matches_key(tmp_path, key)


# --- Finding 6: confirmation is bound to the key the client showed ----------


def test_stale_confirmation_cannot_promote_another_key(env) -> None:
    client, app, keychain, tmp_path = env
    _save_record(app.transcript_store)
    old = _setup(client)
    first = client.post("/api/history/recovery/regenerate").json()
    second = client.post("/api/history/recovery/regenerate").json()

    stale = client.post("/api/history/recovery/confirm", json={"generation": first["generation"]})
    assert stale.status_code == 409 and stale.json()["error"] == "recovery_generation_stale"
    missing = client.post("/api/history/recovery/confirm", json={})
    assert missing.status_code == 409

    # Nothing was promoted: the confirmed key still works.
    _lose_keychain_key(app, keychain, tmp_path)
    assert client.post("/api/history/recovery/unlock", json={"recovery_key": old}).json() == {"ok": True}
    assert second["generation"] != first["generation"]


def test_stale_setup_confirmation_is_rejected(env) -> None:
    client, app, _keychain, _tmp = env
    _save_record(app.transcript_store)
    first = client.post("/api/history/recovery/setup").json()
    second = client.post("/api/history/recovery/setup").json()
    stale = client.post("/api/history/recovery/confirm", json={"generation": first["generation"]})
    assert stale.status_code == 409
    ok = client.post("/api/history/recovery/confirm", json={"generation": second["generation"]})
    assert ok.json() == {"ok": True}


# --- Finding 2: crashes mid-transition leave no platform-only path ----------


class _ReservingKeychain:
    """Keychain double with the Darwin adapter's reserved-identity extension."""

    def __init__(self) -> None:
        import uuid

        from whisperstream.protected_secret_contract import MACOS_KEYCHAIN_PREFIX

        self._uuid = uuid
        self._prefix = MACOS_KEYCHAIN_PREFIX
        self.items: dict[str, bytes] = {}

    def reserve_reference(self) -> str:
        return f"{self._prefix}{self._uuid.uuid4()}"

    def store_reserved(self, reference: str, secret: bytes, *, purpose) -> None:
        del purpose
        self.items[reference] = bytes(secret)

    def store(self, secret: bytes, *, purpose) -> str:
        reference = self.reserve_reference()
        self.store_reserved(reference, secret, purpose=purpose)
        return reference

    def load(self, reference: str) -> bytes:
        from whisperstream.protected_secret_contract import (
            ProtectedSecretError,
            ProtectedSecretFailure,
            ProtectedSecretOperation,
        )

        if reference not in self.items:
            raise ProtectedSecretError(ProtectedSecretOperation.LOAD, ProtectedSecretFailure.NOT_FOUND)
        return self.items[reference]

    def delete(self, reference: str) -> None:
        from whisperstream.protected_secret_contract import (
            ProtectedSecretError,
            ProtectedSecretFailure,
            ProtectedSecretOperation,
        )

        if reference not in self.items:
            raise ProtectedSecretError(ProtectedSecretOperation.DELETE, ProtectedSecretFailure.NOT_FOUND)
        del self.items[reference]


class _Crash(BaseException):
    """Stands in for the process dying: no ``except Exception`` handler runs."""


def test_interrupted_enable_is_completed_at_startup(tmp_path) -> None:
    from whisperstream import history_recovery, lockdown_keystore
    from whisperstream.lockdown_crypto import wrap_key, wrap_key_with_recovery_code
    from whisperstream.transcript_crypto import load_or_create_data_key

    keychain = _ReservingKeychain()
    key = load_or_create_data_key(tmp_path / "transcript.key", store=keychain)
    history_recovery.create(tmp_path, key)
    # Enable wrote the Lockdown envelope, then died before removing the others.
    lockdown_keystore._write_envelopes(
        tmp_path, wrap_key(key, LOCKDOWN_PASSWORD), wrap_key_with_recovery_code(key, "AAAA-BBBB-CCCC-DDDD-EEEE"), key
    )
    assert keychain.items and history_recovery.envelope_path(tmp_path).exists()

    lockdown_keystore.repair_interrupted_transition(tmp_path, store=keychain)

    assert keychain.items == {}
    assert not (tmp_path / "transcript.key").exists()
    assert not history_recovery.envelope_path(tmp_path).exists()
    assert lockdown_keystore.unlock_with_password(tmp_path, LOCKDOWN_PASSWORD) == key


def test_interrupted_disable_is_rolled_back_at_startup(tmp_path) -> None:
    from whisperstream import lockdown_keystore
    from whisperstream.transcript_crypto import load_or_create_data_key

    keychain = _ReservingKeychain()
    key = load_or_create_data_key(tmp_path / "transcript.key", store=keychain)
    lockdown_keystore.enable_lockdown(tmp_path, LOCKDOWN_PASSWORD, store=keychain)
    assert keychain.items == {}
    # Disable staged a protected copy, then died before handing the envelope back.
    assert lockdown_keystore.prepare_disable_lockdown(tmp_path, LOCKDOWN_PASSWORD, store=keychain) == key
    assert keychain.items

    lockdown_keystore.repair_interrupted_transition(tmp_path, store=keychain)

    assert keychain.items == {}
    assert not (tmp_path / "transcript.key").exists()
    assert lockdown_keystore.is_lockdown_enabled(tmp_path)


def test_crash_after_creating_an_item_leaves_no_orphan(tmp_path, monkeypatch) -> None:
    from whisperstream import lockdown_keystore, transcript_crypto

    keychain = _ReservingKeychain()

    def crash(*_args, **_kwargs):
        raise _Crash()

    monkeypatch.setattr(transcript_crypto, "atomic_write_text", crash)
    with pytest.raises(_Crash):
        transcript_crypto.save_data_key(tmp_path / "transcript.key", b"k" * 32, store=keychain)
    monkeypatch.undo()
    # The item exists but nothing references it.
    assert keychain.items and not (tmp_path / "transcript.key").exists()

    lockdown_keystore.repair_interrupted_transition(tmp_path, store=keychain)
    assert keychain.items == {}


def test_crash_while_deleting_an_item_leaves_no_orphan(tmp_path) -> None:
    from whisperstream import lockdown_keystore, transcript_crypto

    keychain = _ReservingKeychain()
    path = tmp_path / "transcript.key"
    transcript_crypto.load_or_create_data_key(path, store=keychain)
    real_delete = keychain.delete

    def crash(_reference):
        raise _Crash()

    keychain.delete = crash
    with pytest.raises(_Crash):
        transcript_crypto.delete_data_key(path, store=keychain)
    keychain.delete = real_delete
    assert keychain.items and not path.exists()

    lockdown_keystore.repair_interrupted_transition(tmp_path, store=keychain)
    assert keychain.items == {}


def test_repair_keeps_the_live_standard_key(tmp_path) -> None:
    from whisperstream import lockdown_keystore
    from whisperstream.transcript_crypto import load_data_key, load_or_create_data_key

    keychain = _ReservingKeychain()
    key = load_or_create_data_key(tmp_path / "transcript.key", store=keychain)
    lockdown_keystore.repair_interrupted_transition(tmp_path, store=keychain)
    assert load_data_key(tmp_path / "transcript.key", store=keychain) == (key, None)
    assert not (tmp_path / "transcript.key.cleanup").exists()


# --- Re-review round 2 -------------------------------------------------------


def test_password_free_enable_cannot_reset_the_backoff(env) -> None:
    client, _app, _keychain, _tmp = env
    _enable_lockdown(client)
    _lock_out(client)
    response = client.post("/api/lockdown/enable", json={})
    assert response.status_code == 409
    assert response.json()["error_code"] == "lockdown_already_enabled"
    assert client.get("/api/lockdown/status").json()["lockout_remaining_seconds"] > 0


def test_repair_keeps_key_copies_beside_a_damaged_lockdown_file(tmp_path) -> None:
    from whisperstream import history_recovery, lockdown_keystore
    from whisperstream.transcript_crypto import load_data_key, load_or_create_data_key

    keychain = _ReservingKeychain()
    key = load_or_create_data_key(tmp_path / "transcript.key", store=keychain)
    history_recovery.create(tmp_path, key)
    (tmp_path / "lockdown.key.json").write_text("{not json", encoding="utf-8")

    lockdown_keystore.repair_interrupted_transition(tmp_path, store=keychain)

    assert load_data_key(tmp_path / "transcript.key", store=keychain) == (key, None)
    assert history_recovery.envelope_path(tmp_path).exists()


def test_repair_removes_other_copies_even_when_the_platform_key_differs(tmp_path) -> None:
    # An envelope's key check is unauthenticated, so it can't exempt any copy.
    from whisperstream import history_recovery, lockdown_keystore
    from whisperstream.lockdown_crypto import wrap_key, wrap_key_with_recovery_code
    from whisperstream.transcript_crypto import load_or_create_data_key

    keychain = _ReservingKeychain()
    load_or_create_data_key(tmp_path / "transcript.key", store=keychain)
    other = bytes(range(32))
    history_recovery.create(tmp_path, other)
    lockdown_keystore._write_envelopes(
        tmp_path, wrap_key(other, LOCKDOWN_PASSWORD), wrap_key_with_recovery_code(other, "AAAA-BBBB-CCCC-DDDD-EEEE"), other
    )
    lockdown_keystore.repair_interrupted_transition(tmp_path, store=keychain)
    assert not history_recovery.envelope_path(tmp_path).exists()
    assert not (tmp_path / "transcript.key").exists()
    assert keychain.items == {}


def test_repair_keeps_copies_beside_empty_lockdown_wrappers(tmp_path) -> None:
    from whisperstream import history_recovery, lockdown_keystore
    from whisperstream.transcript_crypto import load_or_create_data_key

    keychain = _ReservingKeychain()
    key = load_or_create_data_key(tmp_path / "transcript.key", store=keychain)
    history_recovery.create(tmp_path, key)
    (tmp_path / "lockdown.key.json").write_text('{"password": {}, "recovery": {}}', encoding="utf-8")
    lockdown_keystore.repair_interrupted_transition(tmp_path, store=keychain)
    history_recovery.discard_if_lockdown(tmp_path)
    assert (tmp_path / "transcript.key").exists()
    assert history_recovery.envelope_path(tmp_path).exists()


def test_legacy_staged_key_without_a_check_must_be_recreated(tmp_path) -> None:
    import json

    from whisperstream import history_recovery

    key = bytes(range(32))
    history_recovery.create(tmp_path, key)
    history_recovery.regenerate(tmp_path, key)
    path = history_recovery.envelope_path(tmp_path)
    blob = json.loads(path.read_text(encoding="utf-8"))
    del blob["pending_key_check"]
    path.write_text(json.dumps(blob), encoding="utf-8")
    generation = history_recovery.pending_generation(tmp_path)
    assert not history_recovery.confirm(tmp_path, key, generation=generation)


def test_inline_dpapi_references_are_never_copied_into_the_ledger(tmp_path) -> None:
    from whisperstream import transcript_crypto
    from whisperstream.protected_secret_contract import WINDOWS_DPAPI_PREFIX

    transcript_crypto._ledger_add(tmp_path / "transcript.key", f"{WINDOWS_DPAPI_PREFIX}AAAA")
    assert not (tmp_path / "transcript.key.cleanup").exists()


def test_enable_refuses_while_an_orphaned_copy_survives(tmp_path) -> None:
    from whisperstream import lockdown_keystore, transcript_crypto
    from whisperstream.protected_secret_contract import (
        ProtectedSecretError,
        ProtectedSecretFailure,
        ProtectedSecretOperation,
    )

    keychain = _ReservingKeychain()
    path = tmp_path / "transcript.key"
    transcript_crypto.load_or_create_data_key(path, store=keychain)
    orphan = keychain.store(b"x" * 44, purpose=None)
    transcript_crypto._ledger_add(path, orphan)

    def refuse(_reference):
        raise ProtectedSecretError(ProtectedSecretOperation.DELETE, ProtectedSecretFailure.ACCESS_DENIED)

    keychain.delete = refuse
    with pytest.raises(RuntimeError):
        lockdown_keystore.enable_lockdown(tmp_path, LOCKDOWN_PASSWORD, store=keychain)
    assert not lockdown_keystore.is_lockdown_enabled(tmp_path)
    assert path.exists()


def test_setup_over_an_unchecked_envelope_keeps_the_saved_key_until_confirmed(env) -> None:
    import json

    client, app, keychain, tmp_path = env
    from whisperstream import history_recovery

    _save_record(app.transcript_store)
    saved_key = _setup(client)
    path = history_recovery.envelope_path(tmp_path)
    blob = json.loads(path.read_text(encoding="utf-8"))
    del blob["key_check"]
    path.write_text(json.dumps(blob), encoding="utf-8")

    staged = client.post("/api/history/recovery/setup").json()
    assert staged["ok"] is True
    # Not confirmed yet: the saved key still restores.
    _lose_keychain_key(app, keychain, tmp_path)
    assert client.post("/api/history/recovery/unlock", json={"recovery_key": saved_key}).json() == {"ok": True}


def test_pending_setup_key_restores_a_mismatched_key(env) -> None:
    client, app, keychain, tmp_path = env
    _save_record(app.transcript_store)
    pending = client.post("/api/history/recovery/setup").json()["recovery_key"]
    _install_mismatched_key(app, keychain, tmp_path)
    assert client.get("/api/history/recovery/status").json()["key_mismatch"] is True
    assert client.post("/api/history/recovery/unlock", json={"recovery_key": pending}).json() == {"ok": True}


def test_completed_confirmation_does_not_relabel_a_stale_envelope(env) -> None:
    client, app, keychain, tmp_path = env
    from whisperstream import history_recovery
    from whisperstream.transcript_store import TranscriptStore

    setup = client.post("/api/history/recovery/setup").json()
    assert client.post("/api/history/recovery/confirm", json={"generation": setup["generation"]}).json()["ok"]
    # The protected key disappears while history is empty, so a new key is minted.
    app.transcript_store.close()
    keychain.items.clear()
    (tmp_path / "transcript.key").unlink()
    app.transcript_store = TranscriptStore(protected_secret_store=keychain)
    live = app.transcript_store.data_key_for_recovery()
    assert not history_recovery.matches_key(tmp_path, live)

    retry = client.post("/api/history/recovery/confirm", json={"generation": setup["generation"]})
    assert retry.json() == {"ok": True}
    assert not history_recovery.matches_key(tmp_path, live)
    assert client.get("/api/history/recovery/status").json()["needs_setup"] is True


def test_superseded_confirmation_fails_after_a_newer_one_completes(env) -> None:
    client, app, _keychain, _tmp = env
    _save_record(app.transcript_store)
    _setup(client)
    first = client.post("/api/history/recovery/regenerate").json()
    second = client.post("/api/history/recovery/regenerate").json()
    assert client.post("/api/history/recovery/confirm", json={"generation": second["generation"]}).json()["ok"]
    late = client.post("/api/history/recovery/confirm", json={"generation": first["generation"]})
    assert late.status_code == 409 and late.json()["error"] == "recovery_generation_stale"


def test_damaged_content_is_not_offered_as_a_key_restore(env) -> None:
    client, app, _keychain, tmp_path = env
    from whisperstream.transcript_store import TranscriptStore

    store = app.transcript_store
    store.save(
        raw_text="synthetic transcript", enhanced_text=None, duration_ms=5,
        model="synthetic", source="dictation", audio_bytes=b"RIFFsynthetic",
    )
    _save_record(store)
    _setup(client)
    for wav in (tmp_path / "recordings").glob("*.wav"):
        wav.unlink()
    store.close()
    app.transcript_store = TranscriptStore(protected_secret_store=_keychain)
    status = client.get("/api/history/recovery/status").json()
    assert status["key_mismatch"] is False
    assert status["key_missing"] is False
    # Recovery settings still work: the key itself is right.
    assert client.post("/api/history/recovery/regenerate").json()["ok"] is True
