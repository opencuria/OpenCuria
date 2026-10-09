"""Opt-in, Docker-free live OpenCuria Claude Agent E2E fixture.

Uses the production Django chat API, WebSocket frontend bridge, Claude engine,
pinned Claude Agent SDK/native CLI, encrypted personal connection, and Vue UI.
Only model inference and workspace runner RPC are synthetic and local. The
Anthropic-compatible SSE server returns a deterministic tool-use script, so no
API key, remote provider or inference request is used. Each CLI is launched in
an isolated mount+network namespace: the host /workspace is hidden beneath a
private tmpfs mount containing only this run's workspace/config, and there is no
network interface/route except loopback. A separate proxy denies unexpected
outbound provider requests from the backend process.

All run state is stored under /tmp/opencuria-claude-live with a private marker.
The runner DB row/workspace is synthetic and cannot spawn host commands; the
WorkspaceAccessor implements only managed-directory setup, isolated CLI startup,
read-only synthetic files and no-op desktop lease lifecycle. Docker, QEMU,
Redis, native runner, real credentials and remote inference are not required.

Run from /workspace/OpenCuria:
  backend/.venv/bin/python e2e/integration/claude-agent-live.py serve --reset
  npm --prefix webapp run dev -- --host 127.0.0.1 --config vite.claude-agent-live.config.mjs
  E2E_CLAUDE_AGENT_LIVE=1 npx --prefix e2e playwright test -c e2e/claude-agent-live.config.ts
  backend/.venv/bin/python e2e/integration/claude-agent-live.py cleanup
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from e2e.integration.claude_live_support import (
    FakeProcessStream,
    LocalSseWorkspaceAccessor,
    encode_text_json,
    encode_text_stream,
    encode_tool_json,
    encode_tool_stream,
)

BACKEND = REPO / "backend"
DEFAULT_STATE_DIR = Path("/tmp/opencuria-claude-live")
MANIFEST = ".claude-agent-live-owner.json"
API_HOST = "127.0.0.1"
API_PORT = 18081
SSE_PORT = 18082
PROXY_PORT = 18083
NETNS = f"ocl{os.getpid()}"
HOST_LINK = f"oclh{os.getpid()}"
GUEST_LINK = "ocl0"


def _network_slot(pid: int) -> int:
    """Derive a PID-specific /30 in RFC 2544 benchmarking space."""
    return (pid * 65_537 + 48_271) % 131_072


def _network_addresses(pid: int) -> tuple[str, str, str]:
    slot = _network_slot(pid)
    octet_2 = 18 + slot // 65_536
    octet_3 = (slot // 256) % 256
    octet_4 = (slot % 256) & 0xFC
    cidr = f"198.{octet_2}.{octet_3}.{octet_4}/30"
    return (
        cidr,
        f"198.{octet_2}.{octet_3}.{octet_4 + 1}",
        f"198.{octet_2}.{octet_3}.{octet_4 + 2}",
    )


_NET_SLOT = _network_slot(os.getpid())
NET_CIDR, HOST_NS_IP, GUEST_NS_IP = _network_addresses(os.getpid())
TOKEN = "sk-ant-live-fixture-only-not-a-secret"
EMAIL = "claude-live@localhost.test"
PASSWORD = "OpenCuria-claude-live-2026!"
SDK_VERSION = "0.2.164"
CLI_VERSION = "2.1.292"
WORKSPACE_NAME = "e2e-claude-live-workspace"
QUESTION_TEXT = "Should the local fixture finish this run?"
FINAL_TEXT = (
    "## Local Claude Agent — E2E passed\n\n"
    "The pinned Claude Agent SDK and native CLI completed this run against "
    "the isolated local API fixture. OpenCuria streamed the tool result and "
    "the user answered its question gate. No remote inference was used."
)
QUESTION = {
    "questions": [
        {
            "header": "LIVE E2E",
            "question": QUESTION_TEXT,
            "options": [
                {"label": "Continue", "description": "Resume the local fixture."},
                {"label": "Stop", "description": "Do not continue."},
            ],
            "multiple": False,
        }
    ]
}
AGENT_TASK = {
    "description": "Inspect the synthetic workspace README",
    "prompt": "Read /workspace/README.md and return its heading.",
    # The pinned CLI exposes Explore as a named Agent subagent.
    "subagent_type": "explore",
}


def _run(*argv: str) -> None:
    """Run one checked, fixture-owned namespace/firewall command."""
    result = subprocess.run(argv, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({' '.join(argv)}): "
            f"{(result.stderr or result.stdout)[-1000:]}"
        )


def _netns_exists() -> bool:
    result = subprocess.run(
        ["ip", "netns", "list"], text=True, capture_output=True, check=False
    )
    return result.returncode == 0 and any(
        row.split() and row.split()[0] == NETNS for row in result.stdout.splitlines()
    )


def setup_cli_network_namespace() -> None:
    """Create a veth with only two local fixture TCP ports reachable."""
    capability = subprocess.run(
        ["unshare", "--net", "--", "true"], capture_output=True, check=False
    )
    if capability.returncode:
        raise RuntimeError("LIVE E2E requires Linux network namespace privileges")
    cleanup_cli_network_namespace()
    _run("ip", "netns", "add", NETNS)
    _run("ip", "link", "add", HOST_LINK, "type", "veth", "peer", "name", GUEST_LINK)
    _run("ip", "link", "set", GUEST_LINK, "netns", NETNS)
    _run("ip", "addr", "add", f"{HOST_NS_IP}/30", "dev", HOST_LINK)
    _run("ip", "link", "set", HOST_LINK, "up")
    _run(
        "ip",
        "netns",
        "exec",
        NETNS,
        "ip",
        "addr",
        "add",
        f"{GUEST_NS_IP}/30",
        "dev",
        GUEST_LINK,
    )
    _run("ip", "netns", "exec", NETNS, "ip", "link", "set", "lo", "up")
    _run("ip", "netns", "exec", NETNS, "ip", "link", "set", GUEST_LINK, "up")
    _run(
        "ip",
        "netns",
        "exec",
        NETNS,
        "ip",
        "route",
        "add",
        f"{HOST_NS_IP}/32",
        "dev",
        GUEST_LINK,
    )
    host_addresses = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", HOST_LINK],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    guest_addresses = subprocess.run(
        [
            "ip",
            "netns",
            "exec",
            NETNS,
            "ip",
            "-4",
            "-o",
            "addr",
            "show",
            "dev",
            GUEST_LINK,
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    if (
        f"{HOST_NS_IP}/30" not in host_addresses
        or f"{GUEST_NS_IP}/30" not in guest_addresses
    ):
        raise RuntimeError(
            "CLI namespace veth did not receive its unique host .1/30 and guest .2/30"
        )
    rules = (
        (
            "INPUT",
            [
                "-i",
                HOST_LINK,
                "-s",
                GUEST_NS_IP,
                "-d",
                HOST_NS_IP,
                "-p",
                "tcp",
                "--dport",
                str(SSE_PORT),
                "-j",
                "ACCEPT",
            ],
        ),
        (
            "INPUT",
            [
                "-i",
                HOST_LINK,
                "-s",
                GUEST_NS_IP,
                "-d",
                HOST_NS_IP,
                "-p",
                "tcp",
                "--dport",
                str(PROXY_PORT),
                "-j",
                "ACCEPT",
            ],
        ),
    )
    _run("iptables", "-I", "INPUT", "1", *rules[0][1])
    _run("iptables", "-I", "INPUT", "2", *rules[1][1])
    _run("iptables", "-I", "INPUT", "3", "-i", HOST_LINK, "-j", "DROP")
    _run(
        "iptables",
        "-I",
        "OUTPUT",
        "1",
        "-o",
        HOST_LINK,
        "-s",
        HOST_NS_IP,
        "-d",
        GUEST_NS_IP,
        "-p",
        "tcp",
        "--sport",
        str(SSE_PORT),
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED,RELATED",
        "-j",
        "ACCEPT",
    )
    _run(
        "iptables",
        "-I",
        "OUTPUT",
        "2",
        "-o",
        HOST_LINK,
        "-s",
        HOST_NS_IP,
        "-d",
        GUEST_NS_IP,
        "-p",
        "tcp",
        "--sport",
        str(PROXY_PORT),
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED,RELATED",
        "-j",
        "ACCEPT",
    )
    _run("iptables", "-I", "OUTPUT", "3", "-o", HOST_LINK, "-j", "DROP")


def cleanup_cli_network_namespace() -> None:
    """Remove only this invocation's exact veth, rules, and network namespace."""
    rules = (
        (
            "INPUT",
            [
                "-i",
                HOST_LINK,
                "-s",
                GUEST_NS_IP,
                "-d",
                HOST_NS_IP,
                "-p",
                "tcp",
                "--dport",
                str(SSE_PORT),
                "-j",
                "ACCEPT",
            ],
        ),
        (
            "INPUT",
            [
                "-i",
                HOST_LINK,
                "-s",
                GUEST_NS_IP,
                "-d",
                HOST_NS_IP,
                "-p",
                "tcp",
                "--dport",
                str(PROXY_PORT),
                "-j",
                "ACCEPT",
            ],
        ),
        ("INPUT", ["-i", HOST_LINK, "-j", "DROP"]),
        (
            "OUTPUT",
            [
                "-o",
                HOST_LINK,
                "-s",
                HOST_NS_IP,
                "-d",
                GUEST_NS_IP,
                "-p",
                "tcp",
                "--sport",
                str(SSE_PORT),
                "-m",
                "conntrack",
                "--ctstate",
                "ESTABLISHED,RELATED",
                "-j",
                "ACCEPT",
            ],
        ),
        (
            "OUTPUT",
            [
                "-o",
                HOST_LINK,
                "-s",
                HOST_NS_IP,
                "-d",
                GUEST_NS_IP,
                "-p",
                "tcp",
                "--sport",
                str(PROXY_PORT),
                "-m",
                "conntrack",
                "--ctstate",
                "ESTABLISHED,RELATED",
                "-j",
                "ACCEPT",
            ],
        ),
        ("OUTPUT", ["-o", HOST_LINK, "-j", "DROP"]),
    )
    for chain, rule in rules:
        if (
            subprocess.run(
                ["iptables", "-C", chain, *rule], capture_output=True, check=False
            ).returncode
            == 0
        ):
            subprocess.run(
                ["iptables", "-D", chain, *rule], capture_output=True, check=False
            )
    subprocess.run(["ip", "link", "del", HOST_LINK], capture_output=True, check=False)
    if _netns_exists():
        subprocess.run(["ip", "netns", "del", NETNS], capture_output=True, check=False)


def prepare_state_dir(path: Path, *, reset: bool) -> Path:
    """Create a private state directory; reset only a marked fixture tree."""
    root = path.expanduser().resolve()
    if root == Path("/") or root == REPO or root.is_relative_to(REPO):
        raise ValueError("Fixture state must stay outside the project")
    if root.exists() and any(root.iterdir()):
        marker = root / MANIFEST
        if not reset:
            raise FileExistsError(
                f"Refusing to reuse non-empty state path: {root}; use --reset "
                "only if this is the fixture's disposable directory"
            )
        # A first interrupted attempt can leave only the empty isolated SQLite
        # file before Django has created the durable owner marker. Allow the
        # exact, empty bootstrap layout and no arbitrary user data.
        bootstrap = {
            "opencuria.sqlite3",
            "opencuria.sqlite3-wal",
            "opencuria.sqlite3-shm",
        }
        entries = {item.name for item in root.iterdir()}
        if not marker.is_file():
            bootstrap_layout = entries.issubset(bootstrap | {"workspace"})
            workspace = root / "workspace"
            if not bootstrap_layout or (
                workspace.exists()
                and (
                    not workspace.is_dir()
                    or {item.name for item in workspace.iterdir()} - {"README.md"}
                )
            ):
                raise ValueError(f"Refusing to reset unowned state path: {root}")
            for entry in root.iterdir():
                if entry.is_file() and entry.stat().st_size == 0:
                    entry.unlink()
                elif entry.is_dir() and entry.name == "workspace":
                    shutil.rmtree(entry)
                else:
                    raise ValueError(
                        f"Refusing to remove unowned bootstrap data: {entry}"
                    )
        else:
            if (
                json.loads(marker.read_text()).get("fixture")
                != "opencuria-claude-agent-live"
            ):
                raise ValueError("Fixture ownership marker does not match")
            shutil.rmtree(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "README.md").write_text(
        "# Synthetic Claude LIVE workspace\n\n"
        "This local fixture contains no project secrets or remote code.\n"
    )
    return root


def configure_environment(root: Path) -> None:
    """Select the dedicated development settings and isolated SQLite database."""
    os.environ.update(
        DJANGO_SETTINGS_MODULE="config.settings",
        DJANGO_ENV="development",
        SQLITE_PATH=str(root / "opencuria.sqlite3"),
        DJANGO_SECRET_KEY="opencuria-live-e2e-isolated-key-not-for-deployment",
        DJANGO_ALLOW_ASYNC_UNSAFE="true",
        REDIS_URL=f"redis://{API_HOST}:1/0",
        HTTP_PROXY=f"http://{API_HOST}:{PROXY_PORT}",
        HTTPS_PROXY=f"http://{API_HOST}:{PROXY_PORT}",
        ALL_PROXY=f"http://{API_HOST}:{PROXY_PORT}",
        http_proxy=f"http://{API_HOST}:{PROXY_PORT}",
        https_proxy=f"http://{API_HOST}:{PROXY_PORT}",
        all_proxy=f"http://{API_HOST}:{PROXY_PORT}",
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
    )
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))


def seed_database(root: Path) -> dict[str, Any]:
    """Migrate and seed an independent local org/user/runner/workspace."""
    configure_environment(root)
    import django

    django.setup()
    from apps.credentials.models import CredentialService
    from apps.credentials.repositories import OrgCredentialServiceActivationRepository
    from apps.harness.engines.connections import EngineConnectionService
    from apps.organizations.models import Membership, MembershipRole, Organization
    from apps.runners.enums import RunnerStatus, WorkspaceStatus
    from apps.runners.models import Runner, Workspace
    from common.utils import hash_token
    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    call_command("migrate", verbosity=0, interactive=False)
    organization = Organization.objects.create(
        name="Claude LIVE E2E", slug=f"claude-live-{uuid.uuid4().hex[:10]}"
    )
    user = get_user_model().objects.create_user(
        email=EMAIL,
        password=PASSWORD,
        first_name="Claude",
        last_name="LIVE",
        is_staff=True,
        is_superuser=True,
    )
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.ADMIN
    )
    services = list(
        CredentialService.objects.filter(
            slug__in=("claude-agent-api-token", "claude-agent-subscription-token"),
            organization=None,
        )
    )
    if {service.slug for service in services} != {
        "claude-agent-api-token",
        "claude-agent-subscription-token",
    }:
        raise RuntimeError("Claude API and subscription token services must be seeded")
    OrgCredentialServiceActivationRepository.ensure_activated(
        organization.id, [service.id for service in services]
    )
    connection = EngineConnectionService().save_connection(
        organization_id=organization.id,
        user=user,
        auth_type="api_token",
        token=TOKEN,
        label="Local Claude LIVE (synthetic)",
    )
    runner = Runner.objects.create(
        name="e2e-claude-live-synthetic-runner",
        api_token_hash=hash_token(f"e2e-live-{uuid.uuid4().hex}"),
        available_runtimes=["docker"],
        status=RunnerStatus.ONLINE,
        sid="e2e-claude-live-synthetic-sid",
        organization=organization,
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name=WORKSPACE_NAME,
        status=WorkspaceStatus.RUNNING,
        runtime_type="docker",
        created_by=user,
    )
    state = {
        "fixture": "opencuria-claude-agent-live",
        "state_dir": str(root),
        "database": str(root / "opencuria.sqlite3"),
        "organization_id": str(organization.id),
        "user_id": user.id,
        "email": EMAIL,
        "password": PASSWORD,
        "runner_id": str(runner.id),
        "workspace_id": str(workspace.id),
        "workspace_name": workspace.name,
        "connection_id": str(connection.id),
        "model": "sonnet",
        "sdk_version": SDK_VERSION,
        "cli_version": CLI_VERSION,
        "sse_requests": 0,
        "proxy_requests": 0,
        "result": None,
    }
    write_manifest(state)
    return state


def write_manifest(state: dict[str, Any]) -> None:
    """Atomically persist private fixture metadata, not credential plaintext."""
    path = Path(state["state_dir"]) / MANIFEST
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, default=str))
    temp.chmod(0o600)
    temp.replace(path)


def make_live_accessor(root: Path, sse_url: str):
    """Use the fixture-owned accessor under an isolated CLI mount/network ns."""

    class LiveLocalSseWorkspaceAccessor(LocalSseWorkspaceAccessor):
        """Synthetic workspace accessor with no shell/process mutations."""

        def __init__(self) -> None:
            super().__init__(
                root,
                {
                    "HOME": str(root),
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    "CLAUDE_CONFIG_DIR": (
                        f"/workspace/.opencuria/harness/claude/{uuid.uuid4()}/config"
                    ),
                    "ANTHROPIC_BASE_URL": f"http://{HOST_NS_IP}:{SSE_PORT}",
                    "MCP_TIMEOUT": "20000",
                    "HTTP_PROXY": f"http://{HOST_NS_IP}:{PROXY_PORT}",
                    "HTTPS_PROXY": f"http://{HOST_NS_IP}:{PROXY_PORT}",
                    "ALL_PROXY": f"http://{HOST_NS_IP}:{PROXY_PORT}",
                    "http_proxy": f"http://{HOST_NS_IP}:{PROXY_PORT}",
                    "https_proxy": f"http://{HOST_NS_IP}:{PROXY_PORT}",
                    "all_proxy": f"http://{HOST_NS_IP}:{PROXY_PORT}",
                    "NO_PROXY": f"127.0.0.1,localhost,{HOST_NS_IP}",
                    "no_proxy": f"127.0.0.1,localhost,{HOST_NS_IP}",
                },
            )
            self.workspace_root = root / "workspace"
            self.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._live_processes: dict[int, asyncio.subprocess.Process] = {}

        async def ensure_runtime_artifact(
            self, artifact_id: str, version: str
        ) -> dict[str, Any]:
            if (artifact_id, version) != ("claude-agent", CLI_VERSION):
                raise RuntimeError("Synthetic runtime rejected an unpinned CLI")
            return {
                "ok": True,
                "path": f"/opt/opencuria/runtimes/claude-agent/{version}/claude",
                "version": version,
            }

        async def exec_wait(
            self, command, workdir="/workspace", env=None, timeout=None
        ):
            """Implement only the engine's safe managed-directory Python guard."""
            from apps.harness.access.base import ExecResult

            argv = [str(item) for item in command]
            self.commands.append(argv)
            if argv[:2] != ["python3", "-c"] or len(argv) < 5:
                raise RuntimeError("Synthetic runner rejected unexpected exec")
            script, base_guest, target_guest = argv[2], argv[-2], argv[-1]
            if "os.path.commonpath((root,path))!=root" not in script:
                raise RuntimeError("Synthetic runner rejected unrecognized Python")
            base = self.guest_path(base_guest)
            target = self.guest_path(target_guest)
            if target != base and base not in target.parents:
                return ExecResult(exit_code=2)
            current = base
            current.mkdir(mode=0o700, parents=True, exist_ok=True)
            current.chmod(0o700)
            for part in target.relative_to(base).parts:
                current = current / part
                if current.is_symlink():
                    return ExecResult(exit_code=3)
                current.mkdir(mode=0o700, exist_ok=True)
                current.chmod(0o700)
            return ExecResult(exit_code=0)

        async def desktop_action(self, action, args=None, timeout=None):
            """Track runner lease states without creating any desktop process."""
            if action == "binding":
                return {"ok": True, "epoch": self.lease_epoch}
            if action == "reserve":
                return {
                    "ok": True,
                    "epoch": self.lease_epoch,
                    "lease_state": "reserved",
                }
            if action in {"renew", "hold"}:
                return {
                    "ok": True,
                    "epoch": self.lease_epoch,
                    "lease_state": "held" if action == "hold" else "reserved",
                }
            if action == "release":
                return {
                    "ok": True,
                    "epoch": self.lease_epoch,
                    "lease_state": "released",
                }
            raise RuntimeError(f"Synthetic runner rejected desktop action {action!r}")

        async def process_list(
            self, *, session_id: str | None = None
        ) -> list[dict[str, Any]]:
            return []

        async def read_file(self, path: str, max_size: int | None = None):
            """Only synthetic /workspace files are readable through the boundary."""
            if path.startswith("/workspace"):
                return await super().read_file(path, max_size)
            if path in {"/AGENTS.md", "/CLAUDE.md"}:
                raise FileNotFoundError(path)
            raise RuntimeError("Synthetic runner denied external host file read")

        async def open_process(
            self,
            command: list[str],
            workdir: str = "/workspace",
            env: dict[str, str] | None = None,
            timeout: float | None = None,
            *,
            owner: dict[str, str] | None = None,
        ):
            """Launch only the bundled CLI in isolated Linux net+mount namespaces."""
            import claude_agent_sdk

            if claude_agent_sdk.__version__ != SDK_VERSION:
                raise RuntimeError("Pinned Claude SDK is unavailable")
            if (
                command[0]
                != f"/opt/opencuria/runtimes/claude-agent/{CLI_VERSION}/claude"
            ):
                raise RuntimeError("Synthetic runner rejected an unknown CLI")
            if str(workdir or "/workspace") != "/workspace":
                raise RuntimeError("Synthetic runner permits only /workspace cwd")
            if (env or {}).get("ANTHROPIC_API_KEY") != TOKEN:
                raise RuntimeError("Synthetic runner rejected the dummy token")
            bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
            if not bundled.is_file():
                raise RuntimeError("Pinned Claude Code executable is missing")
            guest_config = str((env or {}).get("CLAUDE_CONFIG_DIR") or "")
            local_config = self.guest_path(guest_config)
            guest_prompt = next(
                (arg for arg in command if arg.endswith("/system-prompt.md")), ""
            )
            local_prompt = self.guest_path(guest_prompt)
            if not local_config.is_dir() or not local_prompt.is_file():
                raise RuntimeError("Engine did not provision its managed state")

            view = self.root / "cli-views" / uuid.uuid4().hex
            view.mkdir(mode=0o700, parents=True)
            bundled_copy = view / ".claude-live-runtime" / "claude"
            bundled_copy.parent.mkdir(mode=0o700, parents=True)
            shutil.copy2(bundled, bundled_copy)
            bundled_copy.chmod(0o700)
            config_relative = local_config.relative_to(self.workspace_root)
            prompt_relative = local_prompt.relative_to(self.workspace_root)
            shutil.copytree(local_config, view / config_relative, dirs_exist_ok=True)
            shutil.copy2(local_prompt, view / prompt_relative)
            (view / ".opencuria/harness/claude").mkdir(
                mode=0o700, parents=True, exist_ok=True
            )
            (view / "README.md").write_bytes(
                (self.workspace_root / "README.md").read_bytes()
            )
            (view / ".tmp").mkdir(mode=0o700)
            guest_prefix = f"/opt/opencuria/runtimes/claude-agent/{CLI_VERSION}/claude"
            args = [
                str(bundled)
                if arg == guest_prefix
                else str(self.guest_path(arg))
                if arg.startswith("/workspace/")
                else arg
                for arg in command
            ]
            scratch = Path("/workspace/.tmp")
            local_env = {
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "HOME": "/workspace",
                "TMPDIR": str(scratch),
                "PWD": "/workspace",
                "LANG": "C.UTF-8",
                "TERM": "xterm-256color",
                "ANTHROPIC_API_KEY": TOKEN,
                "ANTHROPIC_BASE_URL": f"http://{HOST_NS_IP}:{SSE_PORT}",
                "CLAUDE_CONFIG_DIR": "/workspace/" + str(config_relative),
                "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
                "CLAUDE_AGENT_SDK_VERSION": SDK_VERSION,
                "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "DISABLE_UPDATES": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
                "MCP_TIMEOUT": "20000",
                "HTTP_PROXY": f"http://{HOST_NS_IP}:{PROXY_PORT}",
                "HTTPS_PROXY": f"http://{HOST_NS_IP}:{PROXY_PORT}",
                "ALL_PROXY": f"http://{HOST_NS_IP}:{PROXY_PORT}",
                "http_proxy": f"http://{HOST_NS_IP}:{PROXY_PORT}",
                "https_proxy": f"http://{HOST_NS_IP}:{PROXY_PORT}",
                "all_proxy": f"http://{HOST_NS_IP}:{PROXY_PORT}",
                "NO_PROXY": f"127.0.0.1,localhost,{HOST_NS_IP}",
                "no_proxy": f"127.0.0.1,localhost,{HOST_NS_IP}",
                "LIVE_VIEW": str(view),
                "LIVE_CLAUDE": "/workspace/.claude-live-runtime/claude",
            }
            launcher = (
                "import os,subprocess,sys; os.unshare(os.CLONE_NEWNS); "
                "subprocess.run(['mount','--make-rprivate','/'],check=True); "
                "os.execvp('sh',['sh','-c',sys.argv[0],*sys.argv[1:]])"
            )
            shell = (
                "set -eu; "
                "ip link set lo up; "
                "ip route del default 2>/dev/null || true; "
                "mount -t tmpfs -o size=512m,mode=0755 tmpfs /workspace; "
                'cp -a "$LIVE_VIEW"/. /workspace/; '
                'cd /workspace && exec "$LIVE_CLAUDE" "$@"'
            )
            process = await asyncio.create_subprocess_exec(
                "ip",
                "netns",
                "exec",
                NETNS,
                "unshare",
                "--mount",
                "--fork",
                "--",
                "python3",
                "-c",
                launcher,
                shell,
                *args,
                cwd=self.workspace_root,
                env=local_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            await asyncio.sleep(0.05)
            if process.returncode is not None:
                stderr = await process.stderr.read() if process.stderr else b""
                shutil.rmtree(view, ignore_errors=True)
                raise RuntimeError(
                    "Namespace-isolated CLI failed to start: "
                    + stderr.decode(errors="replace")[-1200:]
                )
            stream = FakeProcessStream(
                process,
                guest_root=self.root,
                remote_config_dir=guest_config,
                local_config_dir=str(local_config),
            )
            self._live_processes[int(process.pid)] = process
            self.opened.append(
                {
                    "pid": process.pid,
                    "cwd": "/workspace",
                    "owner": owner or {},
                    "isolated": True,
                    "view": str(view),
                }
            )
            return stream

        async def aclose_all(self) -> None:
            streams = list(self._live_processes.values())
            for proc in streams:
                if proc.returncode is None:
                    try:
                        os.killpg(proc.pid, 15)
                    except ProcessLookupError:
                        pass
            for proc in streams:
                if proc.returncode is None:
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        try:
                            os.killpg(proc.pid, 9)
                        except ProcessLookupError:
                            pass
                        await proc.wait()
            self._live_processes.clear()
            shutil.rmtree(self.root / "cli-views", ignore_errors=True)

    return LiveLocalSseWorkspaceAccessor()


class SyntheticAnthropicAPI:
    """Deterministic local Messages API: Agent → child Read → question → answer."""

    def __init__(self) -> None:
        self.request_count = 0
        self.request_metadata: list[dict[str, Any]] = []
        self.response_metadata: list[dict[str, Any]] = []
        self.root_agent_sent = False
        self.root_question_sent = False
        self.child_read_sent = False
        self.child_read_reply_sent = False
        self.pending_agent_name = ""
        self.pending_agent_call_id = ""
        self.pending_read_call_id = ""
        self.pending_read_result_received = False
        self.pending_question_name = ""
        self.pending_question_call_id = ""
        self.unexpected: list[str] = []

    async def messages(self, request):  # type: ignore[no-untyped-def]
        from aiohttp import web

        if request.path != "/v1/messages":
            self.unexpected.append(f"path:{request.path}")
            return web.json_response({"error": "fixture path denied"}, status=404)
        if request.headers.get("x-api-key") != TOKEN:
            return web.json_response({"error": "fixture token rejected"}, status=401)
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            # The CLI issues one metadata/ping request as JSON, then begins
            # streaming requests. Return a valid non-stream result for that
            # short metadata turn instead of an empty event stream.
            self.unexpected.append("non-json-preface")
            return web.json_response({"status": "ok"})
        if not isinstance(payload, dict):
            return web.json_response({"error": "bad payload"}, status=400)
        self.request_count += 1
        tools = payload.get("tools") or []
        names = [str(row.get("name") or "") for row in tools if isinstance(row, dict)]
        child_agent_id = str(request.headers.get("x-claude-code-agent-id") or "")
        child = bool(child_agent_id)
        stream = bool(payload.get("stream"))
        model = str(payload.get("model") or "claude-sonnet-4-5")
        self.request_metadata.append(
            {
                "stream": stream,
                "model": model,
                "tool_names": names,
                "has_child_agent_header": child,
            }
        )
        if self.request_count == 1 or self.request_count % 5 == 0:
            print(
                "Claude LIVE local Messages request "
                + json.dumps(self.request_metadata[-1], sort_keys=True),
                flush=True,
            )
        # Advance only when the exact tool call's result appears in history.
        # If the CLI retries the same prompt with stream:false, repeat the same
        # pending tool response rather than turning it into a synthetic success.
        history = json.dumps(payload.get("messages") or [], ensure_ascii=False)
        if (
            self.pending_agent_call_id
            and self.pending_agent_call_id in history
            and "tool_result" in history
        ):
            self.pending_agent_result_received = True
        else:
            self.pending_agent_result_received = getattr(
                self, "pending_agent_result_received", False
            )
        agent_done = self.pending_agent_result_received
        self.pending_read_result_received = self.pending_read_result_received or (
            self.pending_read_call_id in history and "tool_result" in history
        )
        child_done = self.pending_read_result_received
        if (
            self.pending_question_call_id
            and self.pending_question_call_id in history
            and "tool_result" in history
        ):
            self.pending_question_result_received = True
        else:
            self.pending_question_result_received = getattr(
                self, "pending_question_result_received", False
            )
        question_done = self.pending_question_result_received
        if child and not self.child_read_sent and "Read" in names:
            self.pending_read_call_id = f"read-{self.request_count}"
            self.pending_read_result_received = False
            stage = (
                "tool",
                "Read",
                self.pending_read_call_id,
                {"file_path": "/workspace/README.md"},
            )
            self.child_read_sent = True
            self.child_read_reply_sent = False
        elif child and self.child_read_sent and not child_done:
            if not self.child_read_reply_sent:
                stage = (
                    "tool",
                    "Read",
                    self.pending_read_call_id,
                    {"file_path": "/workspace/README.md"},
                )
                self.child_read_reply_sent = True
            else:
                stage = (
                    "text",
                    f"child-wait-{self.request_count}",
                    "I am waiting for the native Read tool result before continuing.",
                )
        elif child and child_done:
            stage = (
                "text",
                f"child-final-{self.request_count}",
                "The synthetic README heading is Synthetic Claude LIVE workspace.",
            )
        elif (
            not child
            and not self.root_agent_sent
            and any(name in {"Agent", "Task"} for name in names)
        ):
            tool_name = "Agent" if "Agent" in names else "Task"
            self.pending_agent_name = tool_name
            self.pending_agent_call_id = f"agent-{self.request_count}"
            self.pending_agent_result_received = False
            stage = (
                "tool",
                tool_name,
                self.pending_agent_call_id,
                AGENT_TASK,
            )
            self.root_agent_sent = True
        elif not child and self.root_agent_sent and not agent_done:
            stage = (
                "tool",
                self.pending_agent_name,
                self.pending_agent_call_id,
                AGENT_TASK,
            )
        elif (
            not child
            and agent_done
            and not self.root_question_sent
            and any(name.endswith("__question") or name == "question" for name in names)
        ):
            question_name = next(
                name
                for name in names
                if name.endswith("__question") or name == "question"
            )
            self.pending_question_name = question_name
            self.pending_question_call_id = f"question-{self.request_count}"
            self.pending_question_result_received = False
            stage = (
                "tool",
                question_name,
                self.pending_question_call_id,
                {"questions": QUESTION["questions"]},
            )
            self.root_question_sent = True
        elif not child and self.root_question_sent and not question_done:
            stage = (
                "tool",
                self.pending_question_name,
                self.pending_question_call_id,
                {"questions": QUESTION["questions"]},
            )
        elif not child and question_done:
            stage = ("text", f"final-{self.request_count}", FINAL_TEXT)
        else:
            # Earlier state-query turns aren't success; the UI must show the
            # SDK-created child task, native Read result and question gate.
            stage = (
                "text",
                f"intermediate-{self.request_count}",
                "I will inspect the synthetic workspace before asking the fixture question.",
            )

        if stage[0] == "tool":
            _, tool_name, call_id, arguments = stage
            self.response_metadata.append(
                {"kind": "tool_use", "name": tool_name, "stream": stream}
            )
            if stream:
                body = encode_tool_stream(tool_name, call_id, arguments, model)
                response = web.StreamResponse(
                    status=200,
                    headers={
                        "content-type": "text/event-stream",
                        "cache-control": "no-cache",
                    },
                )
                await response.prepare(request)
                await response.write(body)
                await response.write_eof()
                return response
            return encode_tool_json(call_id, tool_name, call_id, arguments, model)

        _, message_id, text = stage
        self.response_metadata.append({"kind": "text", "stream": stream})
        if stream:
            response = web.StreamResponse(
                status=200,
                headers={
                    "content-type": "text/event-stream",
                    "cache-control": "no-cache",
                },
            )
            await response.prepare(request)
            await response.write(encode_text_stream(message_id, text, model))
            await response.write_eof()
            return response
        return encode_text_json(message_id, text, model)


def _install_in_memory_socketio() -> None:
    """Keep the production websocket handlers, but replace Redis pub/sub locally."""
    import socketio

    socketio.AsyncRedisManager = lambda *_args, **_kwargs: socketio.AsyncManager()


def _install_synthetic_runner_boundary(state: dict[str, Any]) -> None:
    """Supply deterministic read-only files and stream ACKs; no host runner I/O."""
    from apps.runners.sio_server import (
        emit_to_frontend,
        get_runner_service,
        get_sio_server,
    )

    sio = get_sio_server()
    service = get_runner_service()

    async def dispatch(_runner, event: str, payload: dict[str, Any]) -> None:
        workspace_id = str(payload.get("workspace_id") or "")
        request_id = str(payload.get("request_id") or "")
        path = str(payload.get("path") or "/workspace")
        if workspace_id != state["workspace_id"]:
            raise RuntimeError("Unknown synthetic workspace")
        if event == "files:list":
            entries = (
                [
                    {
                        "name": "README.md",
                        "path": "/workspace/README.md",
                        "type": "file",
                        "size": 93,
                    }
                ]
                if path == "/workspace"
                else []
            )
            await emit_to_frontend(
                "files:list_result",
                {
                    "workspace_id": workspace_id,
                    "request_id": request_id,
                    "path": path,
                    "entries": entries,
                },
                workspace_id,
            )
            return
        if event == "files:find":
            await emit_to_frontend(
                "files:find_result",
                {
                    "workspace_id": workspace_id,
                    "request_id": request_id,
                    "query": payload.get("query", ""),
                    "paths": [],
                    "truncated": False,
                },
                workspace_id,
            )
            return
        if event == "files:read":
            text = "# Synthetic Claude LIVE workspace\n\nThis local fixture contains no project secrets or remote code.\n"
            await emit_to_frontend(
                "files:content_result",
                {
                    "workspace_id": workspace_id,
                    "request_id": request_id,
                    "path": path,
                    "content": text,
                    "size": len(text),
                    "truncated": False,
                    "mime_type": "text/markdown",
                },
                workspace_id,
            )
            return
        raise RuntimeError(f"Synthetic file boundary refused {event}")

    async def call_stream_event(
        _runner, event: str, payload: dict[str, Any], timeout=None
    ) -> dict[str, Any]:
        del timeout
        if str(payload.get("workspace_id") or "") != state["workspace_id"]:
            return {"ok": False, "error": "unknown synthetic workspace"}
        if event == "workspace:stream_start":
            connection_id = str(payload.get("connection_id") or "")
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.create_task(
                    sio.emit(
                        "workspace:stream_output",
                        {
                            "connection_id": connection_id,
                            "workspace_id": state["workspace_id"],
                            "stream": "stdout",
                            "data": "",
                            "started": True,
                        },
                    )
                )
            )
            return {"ok": True, "connection_id": connection_id}
        if event in {"workspace:stream_input", "workspace:stream_close"}:
            return {"ok": True, "closed": event == "workspace:stream_close"}
        return {"ok": False, "error": f"unsupported synthetic stream {event}"}

    service._emit_to_runner = dispatch
    service.call_stream_event = call_stream_event
    state["socket_manager"] = type(sio.manager).__name__


def _install_live_harness_service(state: dict[str, Any], sse_url: str):
    """Use production HarnessService with local-SSE real SDK and fake runtime."""
    from apps.harness import harness_service as harness_module
    from apps.harness.harness_service import HarnessService

    accessor = make_live_accessor(Path(state["state_dir"]), sse_url)

    async def accessor_factory(workspace_id: str):
        if workspace_id != state["workspace_id"]:
            raise RuntimeError("Refused workspace outside fixture scope")
        return accessor

    service = HarnessService(accessor_factory=accessor_factory)
    harness_module._default_harness_service = service
    return service, accessor


async def deny_proxy(_request):  # type: ignore[no-untyped-def]
    """Reject every proxied HTTP request without attempting remote DNS."""
    from aiohttp import web

    return web.json_response(
        {"error": "outbound network disabled in LIVE E2E"}, status=403
    )


async def wait_http(url: str, timeout: float = 40) -> None:
    """Wait for one local API health endpoint to accept a TCP/HTTP request."""
    import aiohttp

    deadline = asyncio.get_running_loop().time() + timeout
    async with aiohttp.ClientSession() as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with client.get(url) as response:
                    if response.status == 200:
                        return
            except (aiohttp.ClientError, OSError):
                pass
            await asyncio.sleep(0.2)
    raise TimeoutError(f"Timed out waiting for local service {url}")


async def stop_fixture_cli_processes(accessor: Any) -> None:
    """Reap namespace-isolated CLI launcher/child pairs after ASGI cancellation."""
    active_groups = set(accessor._live_processes)
    for pid in _namespace_pids(NETNS):
        if pid not in active_groups and not _is_fixture_cli_process(pid):
            continue
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    await asyncio.sleep(0.2)
    for pid in _namespace_pids(NETNS):
        if pid not in active_groups and not _is_fixture_cli_process(pid):
            continue
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _namespace_pids(namespace: str) -> set[int]:
    """Return process IDs actually resident in one exact fixture namespace."""
    result = subprocess.run(
        ["ip", "netns", "pids", namespace], text=True, capture_output=True, check=False
    )
    if result.returncode:
        return set()
    return {int(pid) for pid in result.stdout.split() if pid.isdigit()}


def _is_fixture_cli_process(pid: int) -> bool:
    """Match only this fixture's pinned Claude executable by its guest path."""
    try:
        executable = os.readlink(f"/proc/{pid}/exe")
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\\0", b" ")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    return (
        executable == "/workspace/.claude-live-runtime/claude"
        and b"--session-mirror" in command
    )


async def verify_nonstream_stub(sse: SyntheticAnthropicAPI, url: str) -> None:
    """Exercise the HTTP JSON branch with an explicit ``stream: false`` probe."""
    import aiohttp

    payload = {
        "model": "claude-fixture-preflight",
        "max_tokens": 8,
        "stream": False,
        "tools": [],
        "messages": [],
    }
    timeout = aiohttp.ClientTimeout(total=5)
    async with (
        aiohttp.ClientSession(timeout=timeout, trust_env=False) as client,
        client.post(
            url,
            headers={"x-api-key": TOKEN, "content-type": "application/json"},
            json=payload,
        ) as response,
    ):
        if response.status != 200 or response.content_type != "application/json":
            raise RuntimeError("Non-stream Anthropic preflight was not JSON")
        message = await response.json()
    if (
        message.get("type") != "message"
        or message.get("role") != "assistant"
        or message.get("stop_reason") != "end_turn"
        or not isinstance(message.get("content"), list)
    ):
        raise RuntimeError("Non-stream Anthropic preflight returned an invalid Message")
    if not sse.request_metadata or sse.request_metadata[-1].get("stream") is not False:
        raise RuntimeError("Non-stream Anthropic preflight was not recorded")


async def start_servers(state: dict[str, Any], *, port: int) -> None:
    """Start local SSE/proxy and the production ASGI app with isolated settings."""
    import socketio
    import uvicorn
    from aiohttp import web

    configure_environment(Path(state["state_dir"]))
    _install_in_memory_socketio()
    asgi_module = importlib.import_module("config.asgi")
    from apps.runners.sio_server import get_sio_server

    if not isinstance(get_sio_server().manager, socketio.AsyncManager):
        raise TypeError("Fixture did not replace Redis with a local memory manager")
    sse = SyntheticAnthropicAPI()
    sse_app = web.Application()
    sse_app.router.add_post("/v1/messages", sse.messages)
    sse_app.router.add_route("*", "/{tail:.*}", sse.messages)
    sse_runner = web.AppRunner(sse_app, access_log=None)
    await sse_runner.setup()
    await web.TCPSite(sse_runner, HOST_NS_IP, SSE_PORT).start()
    await verify_nonstream_stub(sse, f"http://{HOST_NS_IP}:{SSE_PORT}/v1/messages")

    proxy_count = 0

    async def deny(_request):  # type: ignore[no-untyped-def]
        nonlocal proxy_count
        proxy_count += 1
        return web.json_response({"error": "outbound inference disabled"}, status=403)

    proxy_app = web.Application()
    proxy_app.router.add_route("*", "/{tail:.*}", deny)
    proxy_runner = web.AppRunner(proxy_app, access_log=None)
    await proxy_runner.setup()
    await web.TCPSite(proxy_runner, HOST_NS_IP, PROXY_PORT).start()

    service, accessor = _install_live_harness_service(
        state, f"http://{HOST_NS_IP}:{SSE_PORT}"
    )
    _install_synthetic_runner_boundary(state)
    from apps.harness import api as harness_api
    from apps.harness.providers.models_catalog import ProviderModel, clear_models_cache

    def local_live_models(_organization_id):
        return [
            ProviderModel(
                id="fixture-local-sonnet",
                name="Local Claude fixture",
                provider="fixture-local",
                reasoning_efforts=["low", "medium", "high"],
                default_effort="high",
                supports_tools=True,
                context_length=100_000,
                max_output_tokens=4_000,
            )
        ]

    # The Claude Agent model list is already supplied by the production
    # Claude engine catalog. The unrelated generic-provider catalog is empty
    # by design, so satisfy the shared model picker with local metadata only.
    # No provider adapter, credentials or HTTP endpoint is instantiated.
    harness_api._list_org_provider_models = local_live_models
    clear_models_cache()
    config = uvicorn.Config(
        asgi_module.application,
        host=API_HOST,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
        loop="asyncio",
        ws="websockets-sansio",
        ws_max_size=209715200,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, setattr, server, "should_exit", True)
    api_task = asyncio.create_task(server.serve(), name="claude-live-api")
    try:
        await wait_http(f"http://{API_HOST}:{port}/api/v1/health/")
        state.update(
            api_url=f"http://{API_HOST}:{port}",
            sse_url=f"http://{API_HOST}:{SSE_PORT}",
            proxy_url=f"http://{API_HOST}:{PROXY_PORT}",
            socket_manager="AsyncManager(in-memory)",
            runner_boundary="synthetic local WorkspaceAccessor; no Docker/QEMU/runner",
        )
        write_manifest(state)
        print(f"Claude LIVE backend ready: {state['api_url']}", flush=True)
        print(
            f"Synthetic workspace: {WORKSPACE_NAME} ({state['workspace_id']})",
            flush=True,
        )
        print(f"Browser login: {EMAIL} / {PASSWORD}", flush=True)
        await api_task
    finally:
        server.should_exit = True
        if not api_task.done():
            api_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await api_task
        await sse_runner.cleanup()
        await proxy_runner.cleanup()
        await stop_fixture_cli_processes(accessor)
        cleanup_cli_network_namespace()
        await accessor.aclose_all()
        service._tasks.clear()
        state["sse_requests"] = sse.request_count
        state["sse_stream_requests"] = sum(
            item["stream"] for item in sse.request_metadata
        )
        state["sse_nonstream_requests"] = sum(
            not item["stream"] for item in sse.request_metadata
        )
        state["sse_request_metadata"] = sse.request_metadata
        state["sse_response_metadata"] = sse.response_metadata
        state["proxy_requests"] = proxy_count
        state["unexpected_sse_paths"] = list(sse.unexpected)
        state["cli_process_count"] = len(accessor.opened)
        state["child_read_result_received"] = sse.pending_read_result_received
        write_manifest(state)


def cleanup_state(path: Path) -> None:
    """Remove only a marked private run state directory."""
    root = path.expanduser().resolve()
    if root == Path("/") or root == REPO or root.is_relative_to(REPO):
        raise ValueError("Refusing unsafe live fixture cleanup")
    marker = root / MANIFEST
    if not marker.is_file():
        raise FileNotFoundError(f"Refusing cleanup without fixture marker: {marker}")
    if json.loads(marker.read_text()).get("fixture") != "opencuria-claude-agent-live":
        raise ValueError("Fixture ownership marker does not match")
    shutil.rmtree(root)


def cleanup_stale_orphan_legacy_rules() -> int:
    """Delete only stale fixture OUTPUT permits for absent PID-owned legacy veths."""
    listing = subprocess.run(
        ["ip", "netns", "list"], text=True, capture_output=True, check=False
    )
    links_result = subprocess.run(
        ["ip", "-o", "link", "show"], text=True, capture_output=True, check=False
    )
    rules_result = subprocess.run(
        ["iptables", "-S", "OUTPUT"], text=True, capture_output=True, check=False
    )
    if listing.returncode or links_result.returncode or rules_result.returncode:
        raise RuntimeError("Cannot inventory stale Claude fixture OUTPUT rules")
    namespaces = {row.split()[0] for row in listing.stdout.splitlines() if row.split()}
    links = {
        row.split(":", 2)[1].strip().split("@", 1)[0]
        for row in links_result.stdout.splitlines()
        if ":" in row
    }
    allowed_pairs = {("10.203.77.1", "10.203.77.2")}
    removed = 0
    for line in rules_result.stdout.splitlines():
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if len(tokens) < 4 or tokens[:2] != ["-A", "OUTPUT"]:
            continue
        values = dict(zip(tokens[2::2], tokens[3::2]))
        device = values.get("-o", "")
        match = re.fullmatch(r"oclh([0-9]+)", device)
        if not match or device in links:
            continue
        pid = int(match.group(1))
        if f"ocl{pid}" in namespaces:
            continue
        old_slot = pid % 16_384
        allowed_pairs.add(
            (
                f"10.{old_slot // 256}.{old_slot % 256}.1",
                f"10.{old_slot // 256}.{old_slot % 256}.2",
            )
        )
        salted = (pid * 65_537 + 48_271) % 4_194_304
        octet_2 = 64 + salted // 65_536
        octet_3 = (salted // 256) % 256
        octet_4 = (salted % 256) & 0xFC
        allowed_pairs.add(
            (
                f"10.{octet_2}.{octet_3}.{octet_4 + 1}",
                f"10.{octet_2}.{octet_3}.{octet_4 + 2}",
            )
        )
        source = values.get("-s", "").removesuffix("/32")
        target = values.get("-d", "").removesuffix("/32")
        if (source, target) not in allowed_pairs:
            continue
        if "-m" not in tokens or tokens[tokens.index("-m") + 1] != "conntrack":
            continue
        states = set(values.get("--ctstate", "").split(","))
        if tokens[-1] != "ACCEPT" or states != {"ESTABLISHED", "RELATED"}:
            continue
        _run("iptables", "-D", "OUTPUT", *tokens[2:])
        removed += 1
    return removed


def cleanup_stale_legacy_networks() -> int:
    """Remove only stale CLI namespaces whose PID/address/rules match this fixture."""
    listing = subprocess.run(
        ["ip", "netns", "list"], text=True, capture_output=True, check=False
    )
    if listing.returncode:
        raise RuntimeError("Cannot inventory stale Claude fixture namespaces")
    links_result = subprocess.run(
        ["ip", "-o", "link", "show"], text=True, capture_output=True, check=False
    )
    if links_result.returncode:
        raise RuntimeError("Cannot inventory fixture veth links")
    host_links = {
        row.split(":", 2)[1].strip().split("@", 1)[0]
        for row in links_result.stdout.splitlines()
        if ":" in row
    }
    removed = 0
    for row in listing.stdout.splitlines():
        match = re.fullmatch(r"(ocl([0-9]+))(?: \(id: [0-9]+\))?", row.strip())
        if not match:
            continue
        namespace, pid_text = match.groups()
        pid = int(pid_text)
        if pid == os.getpid():
            continue
        host_link = f"oclh{pid}"
        if host_link not in host_links:
            continue
        host_probe = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", host_link],
            text=True,
            capture_output=True,
            check=False,
        )
        guest_probe = subprocess.run(
            [
                "ip",
                "netns",
                "exec",
                namespace,
                "ip",
                "-4",
                "-o",
                "addr",
                "show",
                "dev",
                GUEST_LINK,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if host_probe.returncode or guest_probe.returncode:
            continue
        candidates = [_network_addresses(pid)[1:]]
        # Earlier attempts used either the PID-only 10.0/16 mapping or its
        # salted dynamic 10.64/10 mapping. Recognize only those exact /30s.
        old_slot = pid % 16_384
        candidates.append(
            (
                f"10.{old_slot // 256}.{old_slot % 256}.1",
                f"10.{old_slot // 256}.{old_slot % 256}.2",
            )
        )
        salted_slot = (pid * 65_537 + 48_271) % 4_194_304
        octet_2 = 64 + salted_slot // 65_536
        octet_3 = (salted_slot // 256) % 256
        octet_4 = (salted_slot % 256) & 0xFC
        candidates.append(
            (
                f"10.{octet_2}.{octet_3}.{octet_4 + 1}",
                f"10.{octet_2}.{octet_3}.{octet_4 + 2}",
            )
        )
        candidates.append(("10.203.77.1", "10.203.77.2"))
        addresses = next(
            (
                pair
                for pair in candidates
                if f"{pair[0]}/30" in host_probe.stdout
                and f"{pair[1]}/30" in guest_probe.stdout
            ),
            None,
        )
        if addresses is None:
            continue
        guest_ip, host_ip = addresses[1], addresses[0]
        pids = subprocess.run(
            ["ip", "netns", "pids", namespace],
            text=True,
            capture_output=True,
            check=False,
        )
        if pids.returncode or pids.stdout.strip():
            continue
        rules = (
            (
                "INPUT",
                [
                    "-i",
                    host_link,
                    "-s",
                    guest_ip,
                    "-d",
                    host_ip,
                    "-p",
                    "tcp",
                    "--dport",
                    str(SSE_PORT),
                    "-j",
                    "ACCEPT",
                ],
            ),
            (
                "INPUT",
                [
                    "-i",
                    host_link,
                    "-s",
                    guest_ip,
                    "-d",
                    host_ip,
                    "-p",
                    "tcp",
                    "--dport",
                    str(PROXY_PORT),
                    "-j",
                    "ACCEPT",
                ],
            ),
            ("INPUT", ["-i", host_link, "-j", "DROP"]),
            (
                "OUTPUT",
                [
                    "-o",
                    host_link,
                    "-s",
                    host_ip,
                    "-d",
                    guest_ip,
                    "-p",
                    "tcp",
                    "--sport",
                    str(SSE_PORT),
                    "-m",
                    "conntrack",
                    "--ctstate",
                    "ESTABLISHED,RELATED",
                    "-j",
                    "ACCEPT",
                ],
            ),
            (
                "OUTPUT",
                [
                    "-o",
                    host_link,
                    "-s",
                    host_ip,
                    "-d",
                    guest_ip,
                    "-p",
                    "tcp",
                    "--sport",
                    str(PROXY_PORT),
                    "-m",
                    "conntrack",
                    "--ctstate",
                    "ESTABLISHED,RELATED",
                    "-j",
                    "ACCEPT",
                ],
            ),
            ("OUTPUT", ["-o", host_link, "-j", "DROP"]),
        )
        matching = [
            (chain, rule)
            for chain, rule in rules
            if subprocess.run(
                ["iptables", "-C", chain, *rule], capture_output=True, check=False
            ).returncode
            == 0
        ]
        if len(matching) < 3:
            continue
        for chain, rule in rules:
            while (
                subprocess.run(
                    ["iptables", "-C", chain, *rule], capture_output=True, check=False
                ).returncode
                == 0
            ):
                subprocess.run(
                    ["iptables", "-D", chain, *rule], capture_output=True, check=True
                )
        subprocess.run(
            ["ip", "link", "del", host_link], check=True, capture_output=True
        )
        subprocess.run(
            ["ip", "netns", "del", namespace], check=True, capture_output=True
        )
        removed += 1
    return removed


def cleanup_stale_legacy_views() -> int:
    """Remove abandoned old CLI staging dirs only when fixture contents match."""
    removed = 0
    expected_readme = "# Synthetic Claude LIVE workspace\n\nThis local fixture contains no project secrets or remote code.\n"
    for view in Path("/tmp").glob("oc-claude-live-view-*"):
        if not view.is_dir() or view.is_symlink():
            continue
        try:
            if view.stat().st_mode & 0o777 != 0o700:
                continue
            if any(item.is_symlink() for item in view.rglob("*")):
                continue
            top = {item.name for item in view.iterdir()}
            if top - {".tmp", ".claude-live-runtime", ".opencuria", "README.md"}:
                continue
            readme = view / "README.md"
            prompt_files = list(
                (view / ".opencuria/harness/claude").glob("*/system-prompt.md")
            )
            binary = view / ".claude-live-runtime/claude"
            if (
                not readme.is_file()
                or readme.read_text() != expected_readme
                or len(prompt_files) != 1
                or binary.exists()
                and binary.stat().st_size != 251_456_696
            ):
                continue
            files = {item.name for item in view.rglob("*") if item.is_file()}
            if files - {"README.md", "system-prompt.md", "claude"}:
                continue
        except OSError:
            continue
        shutil.rmtree(view)
        removed += 1
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve", help="Start local API, SSE stub and deny proxy"
    )
    serve.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    serve.add_argument("--port", type=int, default=API_PORT)
    serve.add_argument("--reset", action="store_true")
    cleanup = commands.add_parser(
        "cleanup", help="Remove this fixture's private run dir"
    )
    cleanup.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = parser.parse_args()
    if args.command == "cleanup":
        cleanup_state(args.state_dir)
        return
    stale_rules = cleanup_stale_orphan_legacy_rules()
    stale_namespaces = cleanup_stale_legacy_networks()
    stale_views = cleanup_stale_legacy_views()
    if stale_rules or stale_namespaces or stale_views:
        print(
            "Removed stale Claude fixture resources: "
            f"{stale_rules} orphan rules, {stale_namespaces} legacy network namespaces, {stale_views} legacy CLI views",
            flush=True,
        )
    root = prepare_state_dir(args.state_dir, reset=args.reset)
    setup_cli_network_namespace()
    try:
        state = seed_database(root)
        asyncio.run(start_servers(state, port=args.port))
    finally:
        cleanup_cli_network_namespace()
        cli_dir = root / "cli-views"
        if cli_dir.exists():
            shutil.rmtree(cli_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
