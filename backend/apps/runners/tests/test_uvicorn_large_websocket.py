"""Exercise the production Uvicorn WebSocket backend above its 1 MiB default."""

from __future__ import annotations

import asyncio
import socket

import pytest
import uvicorn
import websockets

WEBSOCKET_MAX_BYTES = 200 * 1024 * 1024


async def echo_asgi(scope, receive, send):
    if scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.accept"})
        message = await receive()
        await send({"type": "websocket.send", "text": message["text"]})
        await receive()


@pytest.mark.asyncio
async def test_uvicorn_websockets_backend_accepts_message_over_one_mib():
    """A single Socket.IO-like text frame survives production-size limits."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        echo_asgi,
        host="127.0.0.1",
        port=port,
        ws="websockets-sansio",
        ws_max_size=WEBSOCKET_MAX_BYTES,
        lifespan="off",
        log_level="critical",
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        payload = "x" * (1024 * 1024 + 1)
        async with websockets.connect(
            f"ws://127.0.0.1:{port}", max_size=WEBSOCKET_MAX_BYTES
        ) as websocket:
            await websocket.send(payload)
            assert await websocket.recv() == payload
    finally:
        server.should_exit = True
        await server_task
