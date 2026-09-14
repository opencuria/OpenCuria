"""Secure process-tree wrapper for generic workspace stream processes.

Both Docker and QEMU runtimes launch MCP stdio servers (and the
workspace-local TCP relay) through this static wrapper so that:

- ``command`` + ``args`` travel exclusively as positional argv
  (``argv[0]`` is the executable, the rest are positional parameters) —
  no shell interpolation of user values happens anywhere;
- the child runs in its own session/process group (``setsid``) with a
  runner-generated pidfile, so ``process_close`` can TERM/KILL the
  whole tree (shell wrappers included) without touching other
  workspace processes;
- ``cwd``/``env`` are applied by the wrapper itself from validated
  values (single ``chdir``; env allowlist-filtered by the caller).

The wrapper script is a static constant: only the pidfile path,
workdir, env assignments (already shell-quoted by the caller via
``shlex.quote``), and the ``"$@"`` passthrough vary.  Callers build
the final argv as::

    wrapper_argv(pidfile, workdir, env) + [command, *args]

Docker passes it via ``exec_create(cmd=[...])`` (no shell involved).
QEMU passes a single-quoted argv string built with the same quoting
helper used elsewhere (``_shell_quote``), keeping user values opaque.
"""

from __future__ import annotations

import shlex

#: Static wrapper body. Positional params: $1 = pidfile, $2 = workdir,
#: $3..$N-2 = "KEY=VALUE" env assignments (already shlex-quoted by the
#: caller), the last two sentinel-separated params are the real command.
#: Implemented with a sentinel ("--") so env values containing spaces
#: survive intact: everything before the first "--" after $2 is env.
#:
#: PID discipline: ``echo $$`` runs in the *inner* shell *after* setsid
#: so the pidfile always references the new session leader — even when
#: GNU setsid forks (``--wait`` variant).  ``command`` travels exclusively
#: via positional ``"$@"`` and is exec'd, never interpolated.
_STREAM_WRAPPER_BODY = """\
pidfile="$1"; shift
workdir="$1"; shift
if [ -n "$workdir" ]; then cd "$workdir" || exit 127; fi
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
  export "$1"; shift
done
if [ "$1" = "--" ]; then shift; fi
if [ "$#" -eq 0 ]; then echo "opencuria-stream: no command" >&2; exit 127; fi
exec setsid sh -c 'echo $$ > "$1"; shift; exec "$@"' opencuria-stream-inner "$pidfile" "$@"
"""


def stream_wrapper_prefix() -> list[str]:
    """Return the static wrapper argv prefix (``sh -c <body> opencuria-stream``)."""
    return ["sh", "-c", _STREAM_WRAPPER_BODY, "opencuria-stream"]


def stream_wrapper_argv(
    pidfile: str,
    workdir: str | None,
    env: dict[str, str] | None,
    command: list[str],
) -> list[str]:
    """Build the full argv for a stream process (no shell interpolation).

    ``command`` is appended verbatim as positional argv — values are never
    joined, quoted, or interpreted.  ``pidfile``/``workdir``/env keys and
    values are validated by the caller; env assignments are shlex-quoted
    here so the wrapper's ``export "$1"`` receives exactly one
    ``KEY=VALUE`` word per entry.
    """
    if not command:
        raise ValueError("command must not be empty")
    argv = [*stream_wrapper_prefix(), pidfile, workdir or ""]
    for key, value in (env or {}).items():
        argv.append(f"{key}={value}")
    argv.append("--")
    argv.extend(command)
    return argv


def shell_quote(value: str) -> str:
    """Quote one argv word for ``sh -c`` transport (QEMU/SSH only)."""
    return shlex.quote(value)


def shell_quote_argv(argv: list[str]) -> str:
    """Quote a full argv list into one shell-safe command string."""
    return " ".join(shlex.quote(part) for part in argv)
