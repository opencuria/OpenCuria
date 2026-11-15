"""Optional systemd readiness and progress notifications without dependencies."""

import os
import socket

import structlog

logger = structlog.get_logger(__name__)


def notify_systemd(message: str) -> None:
    """Notify the supervising unit, or do nothing outside systemd."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.connect(address)
            client.sendall(message.encode("utf-8"))
    except OSError:
        logger.exception("systemd_notification_failed")
