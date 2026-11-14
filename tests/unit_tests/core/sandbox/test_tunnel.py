import asyncio
from types import SimpleNamespace

import pytest
from websockets.asyncio.server import serve

from core.sandbox.tunnel import BrowserTunnel


@pytest.fixture
async def fake_service():
    """A stand-in for daiv-sandbox's CDP endpoint: echoes binary frames reversed, recording each connection."""
    state = SimpleNamespace(headers=[], paths=[], connections=[])

    async def handler(websocket):
        state.headers.append(websocket.request.headers.get("X-API-KEY"))
        state.paths.append(websocket.request.path)
        state.connections.append(websocket)
        async for message in websocket:
            await websocket.send(bytes(reversed(message)))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        state.client = SimpleNamespace(
            browser_cdp_url=lambda session_id: f"ws://127.0.0.1:{port}/session/{session_id}/browser/cdp",
            auth_headers=lambda: {"X-API-KEY": "fake-key"},
        )
        yield state


async def _roundtrip(tunnel: BrowserTunnel, payload: bytes) -> tuple[bytes, asyncio.StreamWriter]:
    port = int(tunnel.http_url.rsplit(":", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    return await asyncio.wait_for(reader.readexactly(len(payload)), 5), writer


async def test_bytes_round_trip_through_the_local_port(fake_service):
    tunnel = await BrowserTunnel(fake_service.client, "sid").aopen()
    try:
        assert tunnel.http_url.startswith("http://127.0.0.1:")
        echoed, writer = await _roundtrip(tunnel, b"GET /json/version")
        writer.close()
    finally:
        await tunnel.aclose()

    assert echoed == bytes(reversed(b"GET /json/version"))


async def test_each_connection_opens_its_own_authenticated_websocket(fake_service):
    tunnel = await BrowserTunnel(fake_service.client, "sid").aopen()
    try:
        _, first = await _roundtrip(tunnel, b"one")
        _, second = await _roundtrip(tunnel, b"two")
        first.close()
        second.close()
    finally:
        await tunnel.aclose()

    assert len(fake_service.headers) == 2
    assert fake_service.headers == ["fake-key", "fake-key"]
    assert fake_service.paths == ["/session/sid/browser/cdp"] * 2


async def test_aclose_closes_the_listener_and_every_open_bridge(fake_service):
    tunnel = await BrowserTunnel(fake_service.client, "sid").aopen()
    port = int(tunnel.http_url.rsplit(":", 1)[1])
    _, writer = await _roundtrip(tunnel, b"open")

    await tunnel.aclose()

    await asyncio.wait_for(fake_service.connections[0].wait_closed(), 5)
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    writer.close()


def test_http_url_needs_an_open_tunnel(fake_service):
    with pytest.raises(RuntimeError):
        _ = BrowserTunnel(fake_service.client, "sid").http_url
