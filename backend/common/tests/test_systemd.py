"""Systemd notifications support filesystem and abstract sockets safely."""

from unittest.mock import MagicMock, Mock

import pytest

from common import systemd


@pytest.mark.parametrize("address", ["/run/notify.sock", "@notify.sock"])
def test_notify_systemd_sends_to_supervisors_socket(monkeypatch, address):
    monkeypatch.setenv("NOTIFY_SOCKET", address)
    factory = MagicMock()
    monkeypatch.setattr(systemd.socket, "socket", factory)
    systemd.notify_systemd("READY=1\nWATCHDOG=1")
    client = factory.return_value.__enter__.return_value
    client.connect.assert_called_once_with(address.replace("@", "\0", 1))
    client.sendall.assert_called_once_with(b"READY=1\nWATCHDOG=1")
    factory.return_value.__exit__.assert_called_once()


def test_no_supervisor_does_not_open_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    factory = Mock()
    monkeypatch.setattr(systemd.socket, "socket", factory)
    systemd.notify_systemd("READY=1")
    factory.assert_not_called()


def test_unavailable_supervisor_does_not_stop_recovery(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/missing.sock")
    monkeypatch.setattr(systemd.socket, "socket", Mock(side_effect=OSError("gone")))
    systemd.notify_systemd("WATCHDOG=1")
