"""Production ASGI WebSocket frame size stays aligned with Socket.IO."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings

WEBSOCKET_MAX_BYTES = 200 * 1024 * 1024


def test_websocket_limit_matches_socketio_buffer() -> None:
    """Settings and Socket.IO allow computer-use screenshots up to 200 MiB."""
    assert settings.DAPHNE_WEBSOCKET_MAX_MESSAGE_SIZE == WEBSOCKET_MAX_BYTES
    assert settings.DAPHNE_WEBSOCKET_MAX_FRAME_SIZE == WEBSOCKET_MAX_BYTES


def test_entrypoint_uses_single_uvicorn_worker_with_large_websocket_cap() -> None:
    """ASGI scheduler and harness share the single Uvicorn event loop."""
    entrypoint = Path(__file__).resolve().parents[3] / "entrypoint.sh"
    text = entrypoint.read_text(encoding="utf-8")
    assert "exec uvicorn config.asgi:application" in text
    assert "--ws websockets-sansio" in text
    assert "--ws-max-size $((200 * 1024 * 1024))" in text
    assert "--workers 1" in text
