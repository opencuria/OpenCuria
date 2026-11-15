"""Which port a workspace process listens on, from ``ss`` and ``ps`` output."""

from __future__ import annotations

from apps.mobile_apps.listening_ports import LISTENING_COMMAND, parse_listening

SS = """\
LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=501,fd=14))
LISTEN 0 128      0.0.0.0:22   0.0.0.0:* users:(("sshd",pid=800,fd=3))
LISTEN 0 511      0.0.0.0:9229 0.0.0.0:* users:(("node",pid=1201,fd=21))
LISTEN 0 511            *:3000       *:* users:(("node",pid=1201,fd=19))
LISTEN 0 511  127.0.0.1:41235  0.0.0.0:* users:(("node",pid=1202,fd=20))
LISTEN 0 4096        [::]:8080    [::]:* users:(("docker-proxy",pid=950,fd=4))
"""
PS = """\
    1     1
  800   800
 1200  1200
 1201  1200
 1202  1200
  950   950
"""


def snapshot(sockets: str = SS, processes: str = PS, session: int = 1200):
    return parse_listening(f"{sockets}--opencuria-sessions--\n{processes}", session)


def test_ports_of_the_process_session_and_the_workspace():
    listening = snapshot()
    assert listening.alive is True
    # The session listens on its server, a debugger and a worker port.
    assert listening.owned == (3000, 9229, 41235)
    assert listening.ports == {53, 22, 9229, 3000, 41235, 8080}


def test_lowest_own_port_wins_without_an_explicit_port():
    assert snapshot().port_for(None) == 3000


def test_explicit_port_only_needs_to_listen_somewhere():
    # A container proxy serves 8080 for the process: it is still reachable.
    assert snapshot().port_for(8080) == 8080
    assert snapshot().port_for(5000) is None


def test_process_without_sockets_is_alive_but_has_no_port():
    listening = snapshot(session=1)
    assert listening.alive is True
    assert listening.owned == ()
    assert listening.port_for(None) is None


def test_vanished_session_and_missing_ps():
    assert snapshot(session=4242).alive is False
    assert snapshot(processes="").alive is None


def test_command_runs_ss_and_ps_in_one_shell():
    assert LISTENING_COMMAND[:2] == ["sh", "-c"]
    assert "ss -Hltnp" in LISTENING_COMMAND[2]
    assert "ps -e -o pid=,sid=" in LISTENING_COMMAND[2]
