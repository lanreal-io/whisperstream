"""Browser requests must be admitted before body parsing or side effects."""

import logging

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from whisperstream.server.middleware import install_middleware


def make_client():
    api = FastAPI()
    calls = []
    install_middleware(
        api, logger=logging.getLogger(__name__),
        current_acceptance_grant=lambda: None,
        acceptance_is_current=lambda grant: True,
        content_blocked_response=lambda path: JSONResponse({}, status_code=403),
        history_startup_busy_response=lambda: None,
        experimental_enabled=lambda: False,
    )

    @api.api_route("/api/action", methods=["GET", "POST"])
    async def action(request: Request):
        calls.append(await request.body())
        return {"ok": True}

    @api.websocket("/ws")
    async def socket(ws: WebSocket):
        await ws.accept()
        calls.append("socket")
        await ws.send_json({"ok": True})
        await ws.close()

    return TestClient(api, base_url="http://127.0.0.1:38420"), calls


@pytest.mark.parametrize("host", ["attacker.example:38420", "localhost.attacker.example", "user@localhost", "localhost:99999", "[::ffff:127.0.0.1]"])
def test_rebinding_host_rejected_before_handler(host):
    client, calls = make_client()
    assert client.post("/api/action", headers={"host": host}).status_code == 403
    assert calls == []


@pytest.mark.parametrize("origin", ["https://attacker.example", "null", "http://localhost.attacker.example", "http://user@localhost", "http://localhost/path", "http://testserver"])
def test_simple_cross_site_post_cannot_mutate(origin):
    client, calls = make_client()
    assert client.post("/api/action", headers={"origin": origin, "content-type": "application/x-www-form-urlencoded"}, content=b"").status_code == 403
    assert calls == []


@pytest.mark.parametrize("origin", [None, "tauri://localhost", "https://tauri.localhost", "http://localhost:5173", "http://127.0.0.1:5173", "http://[::1]:5173"])
def test_local_tools_and_desktop_allowed(origin):
    client, calls = make_client()
    assert client.post("/api/action", headers={} if origin is None else {"origin": origin}).status_code == 200
    assert calls == [b""]


def test_originless_cross_site_browser_request_rejected():
    client, calls = make_client()
    assert client.get("/api/action", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert calls == []


def test_duplicate_admission_headers_rejected():
    client, calls = make_client()
    assert client.post("/api/action", headers=[("host", "localhost"), ("host", "attacker.example")]).status_code == 403
    assert client.post("/api/action", headers=[("origin", "http://localhost"), ("origin", "https://attacker.example")]).status_code == 403
    assert calls == []


def test_websocket_rebinding_host_rejected():
    client, calls = make_client()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"host": "attacker.example:38420", "origin": "http://localhost:5173"}):
            pass
    assert calls == []
