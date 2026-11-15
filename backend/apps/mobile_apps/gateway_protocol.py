"""Contract between the OpenCuria app's network interceptor and the gateway.

A published app reaches a process of its workspace by name:
``http://<process>.opencuria.local[:<port>]/<path>``. The OpenCuria mobile app
rewrites such requests (only those) to
``<server>/ws/app-backends/<app_id>/<label>/<port>/<path>`` and adds the app
session token in :data:`SESSION_HEADER`. The gateway forwards them to the
process inside the workspace the app was published from.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

#: Domain whose subdomains name processes of the app's workspace.
BACKEND_DOMAIN = "opencuria.local"
#: Path prefix of the gateway on the OpenCuria server.
GATEWAY_PREFIX = "/ws/app-backends"
#: Request header carrying the app session token (stripped before forwarding).
SESSION_HEADER = "x-opencuria-app-session"
#: URL ports that mean "the port the process listens on" (scheme defaults).
AUTO_PORTS = frozenset({80, 443})

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class BackendAddress:
    """A backend as the app addresses it: process label and URL port."""

    label: str
    #: The URL's port; :data:`AUTO_PORTS` select the process's own port.
    port: int

    @property
    def explicit_port(self) -> int | None:
        """The requested workspace port, or ``None`` to detect it."""
        return None if self.port in AUTO_PORTS else self.port

    @property
    def host(self) -> str:
        """``<label>.opencuria.local[:port]``, the host the app called."""
        host = f"{self.label}.{BACKEND_DOMAIN}"
        return host if self.explicit_port is None else f"{host}:{self.port}"


def is_label(value: str) -> bool:
    """Whether *value* is a valid process label (one lowercase DNS label)."""
    return bool(_LABEL.match(value))


def parse_address(label: str, port: str) -> BackendAddress:
    """Validate the label and port path segments of a gateway URL.

    Raises:
        ValueError: For labels that are no DNS label or invalid ports.
    """
    clean = label.lower()
    if not is_label(clean):
        raise ValueError(f"Invalid backend host {label!r}.")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"Invalid backend port {port!r}.")
    return BackendAddress(label=clean, port=int(port))


def gateway_path(app_id: uuid.UUID | str) -> str:
    """Gateway path of one app; clients append ``/<label>/<port>/<path>``."""
    return f"{GATEWAY_PREFIX}/{app_id}"


def backend_path(app_id: uuid.UUID | str, address: BackendAddress) -> str:
    """Gateway path that maps to ``http://<address.host>/``."""
    return f"{gateway_path(app_id)}/{address.label}/{address.port}"
