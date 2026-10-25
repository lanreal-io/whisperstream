"""Exact-grant and per-client delivery checks across queued socket work."""

import asyncio
import logging
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect

from whisperstream.server.websocket import WebSocketDelivery
from tests.test_eula_server_boundary import ACCEPTED, BLOCKED, _make_server


def _delivery(current=None, **kwargs) -> WebSocketDelivery:
    current = current if current is not None else [ACCEPTED]
    return WebSocketDelivery(
        logger=logging.getLogger(__name__),
        acceptance_is_current=lambda grant: grant == current[0] and grant.allowed,
        bakeoff_is_current=lambda grant: True,
        **kwargs,
    )


def _socket(side_effect=None) -> MagicMock:
    socket = MagicMock()
    socket.send_text = AsyncMock(side_effect=side_effect)
    socket.close = AsyncMock()
    return socket


async def _drain() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def _run_fan_out(delivery: WebSocketDelivery, events, grant=ACCEPTED, before=None):
    """Publish through a recording loop, then run the queued callbacks."""

    loop = MagicMock()
    for event in events:
        delivery.publish(event, loop=loop, acceptance_grant=grant)
    callbacks = [call.args for call in loop.call_soon_threadsafe.call_args_list]

    async def scenario():
        if before is not None:
            before()
        for callback, *args in callbacks:
            callback(*args)
        await _drain()

    asyncio.run(scenario())
    return callbacks


@pytest.mark.parametrize("later_grant", [BLOCKED, replace(ACCEPTED, evidence_sha256="c" * 64)])
def test_queued_content_does_not_inherit_later_acceptance(later_grant):
    current = [ACCEPTED]
    delivery = _delivery(current)
    socket = _socket()
    delivery.register(socket)

    def revoke():
        current[0] = later_grant

    _run_fan_out(
        delivery,
        [{"type": "transcription.result", "text": "synthetic"}],
        before=revoke,
    )
    socket.send_text.assert_not_awaited()


def test_revocation_during_send_stops_remaining_clients():
    current = [ACCEPTED]
    delivery = _delivery(current)
    delivered = []

    async def send_and_revoke(payload):
        delivered.append(payload)
        current[0] = BLOCKED

    for _ in range(2):
        delivery.register(_socket(send_and_revoke))
    _run_fan_out(delivery, [{"type": "transcription.result", "text": "synthetic"}])
    assert len(delivered) == 1


def test_admitted_transcription_is_accounted_without_socket_clients():
    server, _app, _current = _make_server(ACCEPTED)
    server.stats.record_transcription = MagicMock()
    assert not server._socket_delivery.clients
    server.broadcast({"type": "transcription.result", "text": "two words", "duration_ms": 10})
    server.stats.record_transcription.assert_called_once_with("two words", 10)


def test_publish_schedules_one_callback_and_no_task_per_event():
    delivery = _delivery()
    delivery.register(_socket())
    loop = MagicMock()
    for index in range(50):
        delivery.publish({"type": "license.status", "i": index}, loop=loop, acceptance_grant=None)
    assert loop.call_soon_threadsafe.call_count == 50
    assert all(
        not asyncio.iscoroutine(arg)
        for call in loop.call_soon_threadsafe.call_args_list
        for arg in call.args
    )
    assert loop.create_task.call_count == 0


def test_stalled_client_does_not_delay_healthy_clients_and_is_dropped_on_timeout():
    delivery = _delivery(send_timeout_seconds=0.2)
    never = asyncio.Event()

    async def stall(_payload):
        await never.wait()

    stalled = _socket(stall)
    healthy = _socket()
    delivery.register(stalled)
    delivery.register(healthy)

    async def scenario():
        loop = asyncio.get_running_loop()
        for index in range(3):
            delivery.publish({"type": "license.status", "i": index}, loop=loop, acceptance_grant=None)
        await _drain()
        # Healthy peer got every frame while the stalled send is still pending.
        assert healthy.send_text.await_count == 3
        assert stalled.send_text.await_count == 1
        assert delivery.pending_frames(stalled) == 2
        await asyncio.sleep(0.4)
        await _drain()

    asyncio.run(scenario())
    assert delivery.clients == {healthy}
    assert stalled.send_text.await_count == 1  # one writer, never concurrent sends
    stalled.close.assert_awaited_once_with(code=1011)
    assert delivery.pending_frames(stalled) == 0


def test_queue_overflow_drops_only_that_client():
    delivery = _delivery(max_pending_frames=4, send_timeout_seconds=30)
    never = asyncio.Event()

    async def stall(_payload):
        await never.wait()

    stalled = _socket(stall)
    healthy = _socket()
    delivery.register(stalled)
    delivery.register(healthy)

    async def scenario():
        loop = asyncio.get_running_loop()
        for index in range(10):
            delivery.publish({"type": "license.status", "i": index}, loop=loop, acceptance_grant=None)
            await _drain()
        await _drain()

    asyncio.run(scenario())
    assert delivery.clients == {healthy}
    assert healthy.send_text.await_count == 10
    stalled.close.assert_awaited_once_with(code=1011)
    assert delivery._channels.keys() == {healthy}


def test_direct_send_is_ordered_with_broadcasts_and_raises_when_dropped():
    delivery = _delivery(send_timeout_seconds=0.1)
    sent: list[str] = []

    async def record(payload):
        sent.append(payload)

    ws = _socket(record)
    delivery.register(ws)

    async def scenario():
        loop = asyncio.get_running_loop()
        delivery.publish({"type": "license.status"}, loop=loop, acceptance_grant=None)
        await asyncio.sleep(0)
        await delivery.send(ws, '{"type": "direct"}')
        assert sent == ['{"type": "license.status"}', '{"type": "direct"}']

        never = asyncio.Event()

        async def stall(_payload):
            await never.wait()

        ws.send_text.side_effect = stall
        with pytest.raises(WebSocketDisconnect):
            await delivery.send(ws, '{"type": "late"}')

    asyncio.run(scenario())
    assert ws not in delivery.clients


def test_direct_send_to_unregistered_socket_is_bounded():
    delivery = _delivery(send_timeout_seconds=0.05)
    never = asyncio.Event()

    async def stall(_payload):
        await never.wait()

    ws = _socket(stall)

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await delivery.send(ws, "{}")

    asyncio.run(scenario())


def test_unregister_stops_the_writer_task():
    delivery = _delivery()
    ws = _socket()
    delivery.register(ws)

    async def scenario():
        loop = asyncio.get_running_loop()
        delivery.publish({"type": "license.status"}, loop=loop, acceptance_grant=None)
        await _drain()
        task = delivery._channels[ws].task
        delivery.unregister(ws)
        await _drain()
        assert task.done()
        assert delivery._channels == {}

    asyncio.run(scenario())
