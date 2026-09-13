"""Daphne and Socket.IO WebSocket payload limits stay aligned.

Daphne 4.2.2+ defaults message/frame size to 1 MiB. Computer-use PNG
screenshots exceed that and drop the runner connection unless both
Daphne caps match Socket.IO's 200 MiB buffer.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings

WEBSOCKET_MAX_BYTES = 200 * 1024 * 1024


def test_daphne_websocket_limits_match_socketio_buffer() -> None:
    """runserver honors these settings (daphne>=4.2.3)."""
    assert settings.DAPHNE_WEBSOCKET_MAX_MESSAGE_SIZE == WEBSOCKET_MAX_BYTES
    assert settings.DAPHNE_WEBSOCKET_MAX_FRAME_SIZE == WEBSOCKET_MAX_BYTES


def test_entrypoint_raises_daphne_websocket_limits() -> None:
    """Production daphne CLI must raise both message and frame caps."""
    entrypoint = Path(__file__).resolve().parents[3] / "entrypoint.sh"
    text = entrypoint.read_text(encoding="utf-8")
    assert "WS_MAX=$((200 * 1024 * 1024))" in text
    assert '--websocket-max-message-size "$WS_MAX"' in text
    assert '--websocket-max-frame-size "$WS_MAX"' in text
