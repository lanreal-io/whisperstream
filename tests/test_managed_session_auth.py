"""Tests for the managed-session per-run capability token (managed_session_auth.py)."""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from starlette.testclient import TestClient

from whisperstream.managed_session_auth import (
    HEADER_NAME,
    TOKEN_FILENAME,
    ManagedSessionAuth,
)


def _make_app(auth: ManagedSessionAuth) -> FastAPI:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(auth.require_capability_token)])
    def protected():
        return {"ok": True}

    return app


def test_mint_writes_token_file_and_sets_current_token(tmp_path):
    auth = ManagedSessionAuth()
    token = auth.mint(tmp_path)

    assert token
    assert auth.token == token

    path = tmp_path / TOKEN_FILENAME
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["token"] == token


def test_token_is_256_bit_url_safe(tmp_path):
    auth = ManagedSessionAuth()
    token = auth.mint(tmp_path)

    # secrets.token_urlsafe(32) encodes 32 bytes (256 bits) of entropy.
    # URL-safe base64 without padding: ceil(32 * 8 / 6) = 43 characters.
    assert len(token) == 43
    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    assert set(token) <= allowed


def test_rewrite_on_restart_invalidates_old_token(tmp_path):
    auth = ManagedSessionAuth()
    old_token = auth.mint(tmp_path)
    assert auth.is_valid(old_token) is True

    new_token = auth.mint(tmp_path)

    assert new_token != old_token
    assert auth.is_valid(old_token) is False
    assert auth.is_valid(new_token) is True

    # A brand new process (fresh instance) reading only the token file also
    # only ever sees the latest value -- the stale in-memory value from a
    # prior "run" never lingers on disk either.
    on_disk = json.loads((tmp_path / TOKEN_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["token"] == new_token


def test_clear_invalidates_token_and_removes_file(tmp_path):
    auth = ManagedSessionAuth()
    token = auth.mint(tmp_path)
    assert (tmp_path / TOKEN_FILENAME).exists()

    auth.clear(tmp_path)

    assert auth.token is None
    assert auth.is_valid(token) is False
    assert not (tmp_path / TOKEN_FILENAME).exists()


def test_clear_is_a_noop_when_file_already_missing(tmp_path):
    auth = ManagedSessionAuth()
    # Never minted -> never wrote a file. Must not raise.
    auth.clear(tmp_path)
    assert auth.token is None


def test_is_valid_false_before_any_mint():
    auth = ManagedSessionAuth()
    assert auth.is_valid("anything") is False
    assert auth.is_valid(None) is False
    assert auth.is_valid("") is False


def test_is_valid_uses_constant_time_compare(tmp_path):
    auth = ManagedSessionAuth()
    token = auth.mint(tmp_path)

    with patch(
        "whisperstream.managed_session_auth.hmac.compare_digest",
        wraps=__import__("hmac").compare_digest,
    ) as spy:
        assert auth.is_valid(token) is True
        spy.assert_called_once_with(token, token)


def test_route_401_on_missing_header(tmp_path):
    auth = ManagedSessionAuth()
    auth.mint(tmp_path)
    client = TestClient(_make_app(auth))

    resp = client.get("/protected")

    assert resp.status_code == 401


def test_route_401_on_wrong_token(tmp_path):
    auth = ManagedSessionAuth()
    auth.mint(tmp_path)
    client = TestClient(_make_app(auth))

    resp = client.get("/protected", headers={HEADER_NAME: "not-the-token"})

    assert resp.status_code == 401


def test_route_200_on_correct_token(tmp_path):
    auth = ManagedSessionAuth()
    token = auth.mint(tmp_path)
    client = TestClient(_make_app(auth))

    resp = client.get("/protected", headers={HEADER_NAME: token})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_route_401_after_clear(tmp_path):
    auth = ManagedSessionAuth()
    token = auth.mint(tmp_path)
    client = TestClient(_make_app(auth))
    auth.clear(tmp_path)

    resp = client.get("/protected", headers={HEADER_NAME: token})

    assert resp.status_code == 401


def test_no_token_value_ever_logged(tmp_path, caplog):
    auth = ManagedSessionAuth()
    with caplog.at_level(logging.DEBUG):
        token = auth.mint(tmp_path)
        auth.is_valid("some-wrong-guess")
        auth.is_valid(token)
        auth.clear(tmp_path)

    for record in caplog.records:
        assert token not in record.getMessage()
