"""Find the TCP ports a workspace background process listens on.

Background processes start in their own session (``setsid``), so the
session id of every descendant equals the process's pid. One command lists
listening sockets with their owners (``ss``) and every process's session
(``ps``); pure parsing keeps the decision testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEPARATOR = "--opencuria-sessions--"

#: Run inside the workspace (as root, so ``ss`` sees every owner).
LISTENING_COMMAND = [
    "sh",
    "-c",
    f"ss -Hltnp 2>/dev/null; echo {_SEPARATOR}; ps -e -o pid=,sid= 2>/dev/null",
]

_PID = re.compile(r"pid=(\d+)")


@dataclass(frozen=True)
class ListeningSnapshot:
    """Listening TCP ports in a workspace, seen from one process session."""

    #: Whether a process of the session still exists (``None``: unknown).
    alive: bool | None
    #: Ports the session listens on, ascending.
    owned: tuple[int, ...]
    #: Every listening TCP port in the workspace.
    ports: frozenset[int]

    def port_for(self, requested: int | None) -> int | None:
        """The port to forward to: *requested*, or the session's lowest one.

        An explicit port only has to be listening (it may belong to a child
        such as a container proxy); without one the process's own lowest
        port is used, so debug or HMR side ports do not win.
        """
        if requested is not None:
            return requested if requested in self.ports else None
        return self.owned[0] if self.owned else None


def _local_port(address: str) -> int | None:
    port = address.rsplit(":", 1)[-1]
    return int(port) if port.isdigit() and 0 < int(port) <= 65535 else None


def parse_listening(output: str, session_id: int) -> ListeningSnapshot:
    """Parse :data:`LISTENING_COMMAND` output for the session *session_id*."""
    sockets, _, processes = output.partition(_SEPARATOR)
    members: set[int] = set()
    seen_processes = False
    for line in processes.splitlines():
        fields = line.split()
        if len(fields) != 2 or not all(field.isdigit() for field in fields):
            continue
        seen_processes = True
        pid, sid = int(fields[0]), int(fields[1])
        if sid == session_id:
            members.add(pid)

    owned: set[int] = set()
    ports: set[int] = set()
    for line in sockets.splitlines():
        fields = line.split()
        # State Recv-Q Send-Q Local:Port Peer:Port [users:((...))]
        if len(fields) < 5 or fields[0] != "LISTEN":
            continue
        port = _local_port(fields[3])
        if port is None:
            continue
        ports.add(port)
        owners = {int(pid) for pid in _PID.findall(" ".join(fields[5:]))}
        if owners & members:
            owned.add(port)
    return ListeningSnapshot(
        alive=bool(members) if seen_processes else None,
        owned=tuple(sorted(owned)),
        ports=frozenset(ports),
    )
