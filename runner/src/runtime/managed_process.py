"""Guest-side fenced stream supervisor; no commands or secrets in control records."""

from __future__ import annotations

import json
import re

TOKEN_PROBE_CODE = """import json
boot = open('/proc/sys/kernel/random/boot_id').read().strip()
start = open('/proc/1/stat').read().rsplit(')', 1)[1].split()[19]
print(json.dumps({'boot_id': boot, 'init_starttime': start}))
"""

ISOLATED_AGENT_PATH = "/usr/local/bin:/usr/bin:/bin"
ISOLATED_AGENT_CONFIG_ROOT = "/workspace/.opencuria/harness/claude"
ISOLATED_AGENT_ENV_MAX_SIZE = 256 * 1024
_AGENT_AUTH_ENV = frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"})
_AGENT_FIXED_ENV = {
    "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
    "CLAUDE_AGENT_SDK_VERSION": "0.2.164",
    "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
    "DISABLE_UPDATES": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}
_AGENT_FLAG_ENV = frozenset(
    {
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_DISABLE_NON_ESSENTIAL_MODEL_CALLS",
        "CLAUDE_CODE_DISABLE_NON_ESSENTIAL_TRAFFIC",
        "ENABLE_CLAUDEAI_MCP_SERVERS",
        "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "DISABLE_UPDATES",
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
    }
)
_AGENT_LIMIT_ENV = frozenset(
    {
        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
        "MAX_CONCURRENT_SUBAGENTS",
    }
)
_AGENT_CONFIG_SESSION_RE = re.compile(
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
    r"[A-Za-z0-9_-]{1,40}-[0-9a-f]{12})"
)
_AGENT_CONFIG_RESUME_RE = re.compile(r"resume-[0-9a-f]{16}")
_AGENT_ENV_ALLOWLIST = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_CODE_SDK_READS_SESSION_STATE",
        "DISABLE_UPDATES",
        "DISABLE_AUTOUPDATER",
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "CLAUDE_CODE_DISABLE_NON_ESSENTIAL_MODEL_CALLS",
        "CLAUDE_CODE_DISABLE_NON_ESSENTIAL_TRAFFIC",
        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
        "MAX_CONCURRENT_SUBAGENTS",
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
        "ENABLE_CLAUDEAI_MCP_SERVERS",
        "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING",
    }
)


def _validate_config_root(config_root: object) -> str:
    """Require a clean absolute root; production callers use the fixed root."""
    if (
        not isinstance(config_root, str)
        or not config_root.startswith("/")
        or "\x00" in config_root
        or any(part in {"", ".", ".."} for part in config_root.split("/")[1:])
        or not all(
            char.isascii() and (char.isalnum() or char in "/_.-")
            for char in config_root
        )
    ):
        raise ValueError("Invalid managed Claude config root")
    return config_root


def _validate_agent_config_dir(value: object, config_root: str) -> str:
    """Require the engine's absolute, session-scoped config directory."""
    config_root = _validate_config_root(config_root)
    prefix = config_root.rstrip("/") + "/"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("Invalid Claude config directory")
    parts = value[len(prefix) :].split("/")
    if (
        len(parts) not in {2, 3}
        or not _AGENT_CONFIG_SESSION_RE.fullmatch(parts[0])
        or parts[1] != "config"
        or (len(parts) == 3 and not _AGENT_CONFIG_RESUME_RE.fullmatch(parts[2]))
    ):
        raise ValueError("Invalid Claude config directory")
    return value


def _config_root_for_env(
    env: dict[str, str] | None, config_root: str = ISOLATED_AGENT_CONFIG_ROOT
) -> str:
    """Require the explicit config dir to be below the runner-selected root."""
    config_root = _validate_config_root(config_root)
    config_dir = (env or {}).get("CLAUDE_CONFIG_DIR")
    _validate_agent_config_dir(config_dir, config_root)
    return config_root


def _isolated_agent_env(
    env: dict[str, str] | None,
    *,
    config_root: str = ISOLATED_AGENT_CONFIG_ROOT,
    auth_required: bool = True,
) -> dict[str, str]:
    """Validate the engine's child environment without accepting HOME/PATH."""
    if env is not None and not isinstance(env, dict):
        raise TypeError("Invalid isolated agent environment")
    explicit = env or {}
    if not isinstance(config_root, str) or not isinstance(explicit, dict):
        raise TypeError("Invalid isolated agent environment")
    if any(not isinstance(key, str) for key in explicit):
        raise TypeError("Invalid isolated agent environment")
    if set(explicit) - _AGENT_ENV_ALLOWLIST:
        raise ValueError("Unsupported environment variable for isolated agent")
    for key, value in explicit.items():
        if (
            not isinstance(value, str)
            or len(value) > 8192
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key)
            or "\x00" in value
        ):
            raise ValueError("Invalid isolated agent environment")
    auth_keys = set(explicit) & _AGENT_AUTH_ENV
    if auth_required and len(auth_keys) != 1:
        raise ValueError("Exactly one Claude authentication variable is required")
    if any(not explicit.get(key, "").strip() for key in auth_keys):
        raise ValueError("Claude authentication value must not be empty")
    if "CLAUDE_CONFIG_DIR" not in explicit:
        raise ValueError("Claude config directory is required")
    _validate_agent_config_dir(explicit["CLAUDE_CONFIG_DIR"], config_root)
    if any(explicit.get(key) != value for key, value in _AGENT_FIXED_ENV.items()):
        raise ValueError("Invalid fixed Claude SDK environment")
    result: dict[str, str] = {}
    for key, value in explicit.items():
        if (
            key in _AGENT_FLAG_ENV
            and key not in _AGENT_FIXED_ENV
            and value not in {"0", "1"}
        ):
            raise ValueError("Invalid Claude feature flag")
        if key in _AGENT_LIMIT_ENV and (
            not value.isdigit() or len(value) > 3 or int(value) > 128
        ):
            raise ValueError("Invalid Claude agent limit")
        result[key] = value
    return result


# Executed inside the guest. flock serializes publish/fork with durable close.
# An isolated agent gets a minimal HOME/PATH and its explicit environment over
# stdin. Values never appear in argv, durable control JSON, or logs.
MANAGED_CODE = r"""
import fcntl, json, os, re, shutil, signal, subprocess, sys, time
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
def cleanup_home(identity):
    home = identity.get('isolated_home')
    if (isinstance(home, str) and
            re.fullmatch(r'/tmp/opencuria-agent-home-[0-9a-f]{32}', home) and
            os.path.isdir(home) and not os.path.islink(home)):
        shutil.rmtree(home)
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
    isolated = config.get('isolated_agent')
    if isolated:
        if os.environ.get('OPENCURIA_ISOLATED_AGENT') != '1': sys.exit(125)
        os.environ.pop('OPENCURIA_ISOLATED_AGENT', None)
        home = isolated.get('home')
        if (not isinstance(home, str) or
                not re.fullmatch(r'/tmp/opencuria-agent-home-[0-9a-f]{32}', home)):
            sys.exit(125)
        if config.get('env') != {}: sys.exit(125)
        config_root = isolated.get('config_root')
        if (
            not isinstance(config_root, str)
            or not config_root.startswith('/')
            or '\x00' in config_root
            or any(part in ('', '.', '..') for part in config_root.split('/')[1:])
            or not all(c.isascii() and (c.isalnum() or c in '/_.-') for c in config_root)
        ):
            sys.exit(125)
        header = bytearray()
        while len(header) < 4:
            chunk = os.read(0, 4 - len(header))
            if not chunk: sys.exit(125)
            header.extend(chunk)
        length = int.from_bytes(header, 'big')
        if length == 0 or length > 262144: sys.exit(125)
        raw = bytearray()
        while len(raw) < length:
            chunk = os.read(0, min(65536, length - len(raw)))
            if not chunk: sys.exit(125)
            raw.extend(chunk)
        explicit = json.loads(bytes(raw).decode('utf-8'))
        if not isinstance(explicit, dict): sys.exit(125)
        allowed = {
            'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CONFIG_DIR',
            'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_AGENT_SDK_VERSION',
            'CLAUDE_CODE_SDK_READS_SESSION_STATE', 'DISABLE_UPDATES',
            'DISABLE_TELEMETRY', 'DISABLE_ERROR_REPORTING',
            'CLAUDE_CODE_DISABLE_AUTO_MEMORY', 'DISABLE_AUTOUPDATER',
            'CLAUDE_CODE_DISABLE_NON_ESSENTIAL_MODEL_CALLS',
            'CLAUDE_CODE_DISABLE_NON_ESSENTIAL_TRAFFIC',
            'ENABLE_CLAUDEAI_MCP_SERVERS',
            'CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING',
            'CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH',
            'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS', 'MAX_CONCURRENT_SUBAGENTS',
        }
        if any(not isinstance(k, str) or k not in allowed or
               not isinstance(v, str) or '\x00' in v
               for k, v in explicit.items()): sys.exit(125)
        auth_keys = set(explicit) & {'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'}
        if len(auth_keys) != 1 or any(not explicit[key].strip() for key in auth_keys):
            sys.exit(125)
        prefix = config_root.rstrip('/') + '/'
        config_dir = explicit.get('CLAUDE_CONFIG_DIR')
        if not config_dir.startswith(prefix): sys.exit(125)
        parts = config_dir[len(prefix):].split('/')
        session_re = r'(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[A-Za-z0-9_-]{1,40}-[0-9a-f]{12})'
        if (len(parts) not in (2, 3) or not re.fullmatch(session_re, parts[0]) or
                parts[1] != 'config' or
                (len(parts) == 3 and not re.fullmatch(r'resume-[0-9a-f]{16}', parts[2]))):
            sys.exit(125)
        fixed = {
            'CLAUDE_CODE_ENTRYPOINT': 'sdk-py',
            'CLAUDE_AGENT_SDK_VERSION': '0.2.164',
            'CLAUDE_CODE_SDK_READS_SESSION_STATE': '1',
            'DISABLE_UPDATES': '1', 'DISABLE_TELEMETRY': '1',
            'DISABLE_ERROR_REPORTING': '1', 'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1',
        }
        if any(explicit.get(key) != value for key, value in fixed.items()): sys.exit(125)
        flags = ('DISABLE_AUTOUPDATER', 'CLAUDE_CODE_DISABLE_NON_ESSENTIAL_MODEL_CALLS',
                 'CLAUDE_CODE_DISABLE_NON_ESSENTIAL_TRAFFIC', 'ENABLE_CLAUDEAI_MCP_SERVERS',
                 'CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING')
        if any(explicit.get(key) not in ('0', '1') for key in flags if key in explicit):
            sys.exit(125)
        limits = ('CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH',
                  'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS', 'MAX_CONCURRENT_SUBAGENTS')
        if any(not explicit[key].isdigit() or len(explicit[key]) > 3 or
               int(explicit[key]) > 128 for key in limits if key in explicit):
            sys.exit(125)
        current = '/'
        for component in config_root.strip('/').split('/') + parts:
            current = os.path.join(current, component)
            try: os.lstat(current)
            except OSError: sys.exit(125)
            if os.path.islink(current) or not os.path.isdir(current): sys.exit(125)
        try: os.mkdir(home, 0o700)
        except FileExistsError: pass
        if os.path.islink(home) or not os.path.isdir(home): sys.exit(125)
        os.chmod(home, 0o700)
        child_env = {'PATH': isolated['path'], 'HOME': home,
                     'CLAUDE_CONFIG_DIR': config_dir}
        child_env.update(explicit)
    else:
        child_env = dict(os.environ, **config['env'])
    pid = os.getpid(); identity = stat(pid)
    identity.update(pid=pid, boot_id=boot, init_starttime=token['init_starttime'])
    if isolated: identity['isolated_home'] = home
    save('identity', identity)
    # Child inherits the anchored group, but not the supervisor's TERM handler.
    child = subprocess.Popen(sys.argv[4:], cwd=config['workdir'] or None,
                             env=child_env,
                             preexec_fn=lambda: signal.signal(
                                 signal.SIGTERM, signal.SIG_DFL))
    fcntl.flock(lock, fcntl.LOCK_UN)
    if not isolated:
        # Legacy managed processes are not stdin-driven; agent SDK streams are.
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
            cleanup_home(identity)
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
        cleanup_home(identity)
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
        if not members(identity['pgid']):
            cleanup_home(identity)
            sys.exit(0)
        time.sleep(.05)
    sys.exit(126)
else:
    sys.exit(127)
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


def isolated_agent_env_preamble(
    env: dict[str, str] | None,
    *,
    config_root: str = ISOLATED_AGENT_CONFIG_ROOT,
) -> bytes:
    """Encode explicit credentials into a bounded, length-framed stdin payload."""
    import struct

    payload = json.dumps(
        _isolated_agent_env(env, config_root=config_root),
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > ISOLATED_AGENT_ENV_MAX_SIZE:
        raise ValueError("Isolated agent environment is too large")
    return struct.pack("!I", len(payload)) + payload


def managed_argv(
    control_path: str,
    workdir: str | None,
    env: dict[str, str] | None,
    command: list[str],
    *,
    expected_token: dict[str, str],
    isolated_env: bool = False,
    isolated_home: str | None = None,
    config_root: str = ISOLATED_AGENT_CONFIG_ROOT,
) -> list[str]:
    """Build an opaque argv launch with the predetermined durable control path."""
    if isolated_env:
        if isolated_home is None:
            raise ValueError("Invalid isolated agent home")
        return managed_isolated_argv(
            control_path,
            workdir,
            command,
            expected_token=expected_token,
            isolated_home=isolated_home,
            config_root=_config_root_for_env(env, config_root),
        )
    child_env = env or {}
    isolated = None
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
                "env": child_env,
                "isolated_agent": isolated,
                "expected_token": validate_guest_token(expected_token),
            }
        ),
        *command,
    ]


def managed_isolated_argv(
    control_path: str,
    workdir: str | None,
    command: list[str],
    *,
    expected_token: dict[str, str],
    isolated_home: str,
    config_root: str = ISOLATED_AGENT_CONFIG_ROOT,
) -> list[str]:
    """Build the isolated agent supervisor without secret values in argv."""
    if (
        not isinstance(isolated_home, str)
        or not re.fullmatch(r"/tmp/opencuria-agent-home-[0-9a-f]{32}", isolated_home)
    ):
        raise ValueError("Invalid isolated agent home")
    if not isinstance(command, list) or not command or any(
        not isinstance(argument, str) or not argument or "\x00" in argument
        for argument in command
    ):
        raise ValueError("Invalid isolated agent command")
    if (
        not isinstance(workdir, str)
        or not workdir.startswith("/")
        or "\x00" in workdir
    ):
        raise ValueError("Invalid isolated agent workdir")
    config_root = _validate_config_root(config_root)
    config = {
        "workdir": workdir,
        "env": {},
        "isolated_agent": {
            "home": isolated_home,
            "path": ISOLATED_AGENT_PATH,
            "config_root": config_root,
        },
        "expected_token": validate_guest_token(expected_token),
    }
    return [
        "env",
        "OPENCURIA_ISOLATED_AGENT=1",
        "setsid",
        "--wait",
        "python3",
        "-u",
        "-c",
        MANAGED_CODE,
        control_path,
        "start",
        json.dumps(config),
        *command,
    ]


def managed_close_argv(control_path: str) -> list[str]:
    """Return the fenced close command; nonzero means closure is unverified."""
    return ["python3", "-u", "-c", MANAGED_CODE, control_path, "close"]
