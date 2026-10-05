"""Guest-side fenced stream supervisor; no commands or secrets in control records."""

from __future__ import annotations

import json

TOKEN_PROBE_CODE = """import json
boot = open('/proc/sys/kernel/random/boot_id').read().strip()
start = open('/proc/1/stat').read().rsplit(')', 1)[1].split()[19]
print(json.dumps({'boot_id': boot, 'init_starttime': start}))
"""


def validate_guest_token(token: object) -> dict[str, str]:
    """Validate non-secret physical guest incarnation evidence."""
    if (
        not isinstance(token, dict)
        or set(token) != {"boot_id", "init_starttime"}
        or not all(isinstance(v, str) and 0 < len(v) <= 128 for v in token.values())
        or not token["init_starttime"].isdigit()
    ):
        raise ValueError("Invalid managed guest token")
    return dict(token)


# Executed inside the guest. flock serializes publish/fork with durable close.
MANAGED_CODE = r"""
import fcntl, json, os, signal, subprocess, sys, time
path, action = sys.argv[1:3]
os.umask(0o077)
os.makedirs(path, mode=0o700, exist_ok=True)
def save(name, value):
    temp = path + '/' + name + '.tmp'
    with open(temp, 'w') as f:
        json.dump(value, f); f.flush(); os.fsync(f.fileno())
    os.replace(temp, path + '/' + name)
    fd = os.open(path, os.O_DIRECTORY); os.fsync(fd); os.close(fd)
def stat(pid):
    try:
        text = open('/proc/%s/stat' % pid).read().rsplit(')', 1)[1].split()
        return {'state': text[0], 'pgid': int(text[2]),
                'sid': int(text[3]), 'starttime': text[19]}
    except (FileNotFoundError, ProcessLookupError):
        return None
def members(pgid):
    result = []
    for pid in os.listdir('/proc'):
        if pid.isdigit():
            s = stat(pid)
            if s and s['pgid'] == pgid and s['state'] != 'Z':
                result.append(int(pid))
    return result
boot = open('/proc/sys/kernel/random/boot_id').read().strip()
lock = open(path + '/lock', 'a')
fcntl.flock(lock, fcntl.LOCK_EX)
if action == 'start':
    config = json.loads(sys.argv[3])
    token = {'boot_id': boot, 'init_starttime': stat(1)['starttime']}
    if config.get('expected_token') != token:
        sys.exit(125)
    if os.path.exists(path + '/closed') or os.path.exists(path + '/identity'):
        sys.exit(125)
    if os.getsid(0) != os.getpid(): os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pid = os.getpid(); identity = stat(pid)
    identity.update(pid=pid, boot_id=boot, init_starttime=token['init_starttime'])
    save('identity', identity)
    # Child inherits the anchored group, but not the supervisor's TERM handler.
    child = subprocess.Popen(sys.argv[4:], cwd=config['workdir'] or None,
                             env=dict(os.environ, **config['env']),
                             preexec_fn=lambda: signal.signal(
                                 signal.SIGTERM, signal.SIG_DFL))
    fcntl.flock(lock, fcntl.LOCK_UN)
    # Only the child needs stdin. Keeping it open here delays producer EOF/
    # SIGPIPE after the child dies, especially while descendants remain.
    os.close(0)
    code = child.wait()
    # Keep the identity anchor alive while any descendants still own the group.
    while True:
        while any(p != pid for p in members(pid)):
            time.sleep(.05)
        # Serialize natural anchor exit with close's identity-check/signals.
        # Without this gate the PGID could be reused during close's TERM grace.
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not any(p != pid for p in members(pid)):
            sys.exit(code)
        fcntl.flock(lock, fcntl.LOCK_UN)
elif action == 'close':
    save('closed', True)
    if not os.path.exists(path + '/identity'):
        # No publish is possible after this tombstone, even for a late transport.
        sys.exit(0)
    identity = json.load(open(path + '/identity'))
    if (identity['boot_id'] != boot or
            identity.get('init_starttime', stat(1)['starttime'])
            != stat(1)['starttime']):
        sys.exit(0)
    pid = identity['pid']; current = stat(pid)
    group = members(identity['pgid'])
    if not group:
        sys.exit(0)
    if not current or any(
            current[k] != identity[k] for k in ('starttime','sid','pgid')):
        sys.exit(126)  # Identity uncertain: never kill a reused PID/group.
    os.killpg(identity['pgid'], signal.SIGTERM)
    time.sleep(.2)
    # The anchor ignores TERM and still owns the group identity until KILL.
    current = stat(pid)
    if current and all(current[k] == identity[k] for k in ('starttime','sid','pgid')):
        try: os.killpg(identity['pgid'], signal.SIGKILL)
        except ProcessLookupError: pass
    elif members(identity['pgid']):
        sys.exit(126)
    for _ in range(100):
        if not members(identity['pgid']): sys.exit(0)
        time.sleep(.05)
    sys.exit(126)
else:
    sys.exit(127)
"""


def managed_argv(
    control_path: str,
    workdir: str | None,
    env: dict[str, str] | None,
    command: list[str],
    *,
    expected_token: dict[str, str],
) -> list[str]:
    """Build an opaque argv launch with the predetermined durable control path."""
    return [
        "setsid",
        "--wait",
        "python3",
        "-u",
        "-c",
        MANAGED_CODE,
        control_path,
        "start",
        json.dumps(
            {
                "workdir": workdir,
                "env": env or {},
                "expected_token": validate_guest_token(expected_token),
            }
        ),
        *command,
    ]


def managed_close_argv(control_path: str) -> list[str]:
    """Return the fenced close command; nonzero means closure is unverified."""
    return ["python3", "-u", "-c", MANAGED_CODE, control_path, "close"]
