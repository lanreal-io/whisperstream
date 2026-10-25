"""Regression coverage for Windows async test socket usage."""

from __future__ import annotations

import asyncio
import os
import socket

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient


@pytest.mark.skipif(os.name != "nt", reason="Windows socket-capacity regression")
def test_event_loops_do_not_create_tcp_socket_pairs(monkeypatch) -> None:
    def fail_socketpair(*args, **kwargs):
        raise AssertionError("Windows test event loop created a TCP socket pair")

    monkeypatch.setattr(socket, "socketpair", fail_socketpair)

    for _ in range(3):
        assert asyncio.run(asyncio.sleep(0, result="ok")) == "ok"


@pytest.mark.skipif(os.name != "nt", reason="Windows socket-capacity regression")
def test_context_free_clients_reuse_the_session_portal() -> None:
    app = FastAPI()
    request_loops: list[asyncio.AbstractEventLoop] = []

    @app.get("/")
    async def root() -> dict[str, bool]:
        request_loops.append(asyncio.get_running_loop())
        return {"ok": True}

    client = TestClient(app)
    try:
        for _ in range(3):
            assert client.get("/").json() == {"ok": True}
    finally:
        client.close()

    assert len(request_loops) == 3
    assert all(loop is request_loops[0] for loop in request_loops)
