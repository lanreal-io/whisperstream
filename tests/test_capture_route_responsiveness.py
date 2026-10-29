"""Capture waits must not occupy the ASGI event loop."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch
import httpx
import pytest
from tests._platform_test_support import _make_server
from whisperstream.config_manager import WhisperstreamConfig


@pytest.mark.parametrize('route', ['/api/config', '/api/devices'])
def test_capture_route_wait_does_not_block_unrelated_http(route):
    server = _make_server(config=WhisperstreamConfig())
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(3)
        return []
    # Registration resolves the bound collaborator when constructing the server;
    # the real config reconciliation will wait on this recording lock instead.
    lock = threading.Lock()
    class ObservedLock:
        def __enter__(self):
            entered.set()
            lock.acquire()
        def __exit__(self, *args):
            lock.release()
    server._app._lock = ObservedLock()
    server._app._recording = False
    server._app.audio_capture = SimpleNamespace(capture_available=True)
    async def run():
        delivered = asyncio.Event()
        class Socket:
            async def send_text(self, text):
                delivered.set()
        socket = Socket()
        server._loop = asyncio.get_running_loop()
        server._socket_delivery.register(socket)
        transport = httpx.ASGITransport(app=server.fastapi)
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
            pending = asyncio.create_task(client.get(route))
            assert await asyncio.to_thread(entered.wait, 1)
            response = await asyncio.wait_for(client.get('/api/health'), .5)
            assert response.status_code == 200
            server.broadcast({'type': 'state.recording', 'recording': False})
            await asyncio.wait_for(delivered.wait(), .5)
            server._socket_delivery.unregister(socket)
            assert not pending.done()
            release.set()
            if lock.locked():
                lock.release()
            await asyncio.wait_for(pending, 2)
    lock.acquire()
    try:
        with patch('whisperstream.server.app.platform.system', return_value='Darwin'), patch('whisperstream.capture.list_input_devices', side_effect=blocked):
            asyncio.run(run())
    finally:
        release.set()
        if lock.locked():
            lock.release()


def test_quarantine_config_and_devices_never_query_native_inventory():
    from fastapi.testclient import TestClient
    server = _make_server(config=WhisperstreamConfig())
    server._app.audio_capture = SimpleNamespace(capture_available=False)
    with patch('whisperstream.server.app.platform.system', return_value='Darwin'), patch('whisperstream.capture.list_input_devices') as query:
        with TestClient(server.fastapi) as client:
            assert client.get('/api/config').status_code == 200
            assert client.get('/api/devices').status_code == 503
        query.assert_not_called()
