"""Production ASGI WebSocket frame size stays aligned with Socket.IO."""

from __future__ import annotations

from pathlib import Path


def test_entrypoint_uses_single_uvicorn_worker_with_large_websocket_cap() -> None:
    """ASGI scheduler and harness share the single Uvicorn event loop."""
    entrypoint = Path(__file__).resolve().parents[3] / "entrypoint.sh"
    text = entrypoint.read_text(encoding="utf-8")
    assert "exec uvicorn config.asgi:application" in text
    assert "--ws websockets-sansio" in text
    assert "--ws-max-size $((200 * 1024 * 1024))" in text
    assert "--workers 1" in text
