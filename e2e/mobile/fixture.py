"""Isolated live-Django/ASGI/Socket.IO fixture for OpenCuria mobile E2E.

Uses real backend auth, tenant access, HarnessService and Socket.IO handlers.
Provider and workspace accessor are deterministic local adapters; nothing
contacts the Internet, Redis, an external LLM, the production runner or port
8000.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shlex
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
# Django apps are imported as `apps.*`; keep this backend's package first.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

RUN_DIR = Path(os.environ.get("MOBILE_E2E_RUN_DIR", "")).resolve()
SAFE_ROOT = Path("/tmp/opencuria-mobile-e2e").resolve()
if not RUN_DIR.is_relative_to(SAFE_ROOT) or RUN_DIR == SAFE_ROOT:
    raise RuntimeError("Use a private run directory below /tmp/opencuria-mobile-e2e")
RUN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(RUN_DIR, 0o700)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "e2e.mobile.settings")

import django

django.setup()

import socketio
from apps.harness.access.base import (
    DirEntry,
    ExecChunk,
    ExecResult,
    FileContent,
    FileStat,
    WorkspaceAccessor,
)
from apps.harness.harness_service import (
    HarnessService,
    reset_default_harness_service,
)
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import Delta, ProviderAdapter, Usage
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
)
from apps.organizations.models import Membership, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.runners.sio_server import (
    _register_event_handlers,
    _register_frontend_handlers,
    get_runner_service,
)
from common.utils import hash_token
from django.contrib.auth import get_user_model
from django.db import connections
from socketio import AsyncClient, AsyncManager

WORKSPACE_ROOT = RUN_DIR / "workspace-root"
RUNNER_ID: str = ""


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Persist private fixture data atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, default=str))
    os.chmod(temp, 0o600)
    temp.replace(path)
    os.chmod(path, 0o600)


def manifest_path() -> Path:
    return RUN_DIR / "manifest.json"


def load_manifest() -> dict[str, Any]:
    """Load private run manifest or fail clearly before contacting a server."""
    if not manifest_path().is_file():
        raise RuntimeError("Mobile E2E fixture is not seeded")
    return json.loads(manifest_path().read_text())


def _vpath(workspace_id: str, path: str, *, allow_external: bool = False) -> Path:
    """Resolve a virtual workspace path under exactly one private fixture root."""
    workspace_uuid = str(uuid.UUID(str(workspace_id)))
    requested = Path(path or "/workspace")
    if not requested.is_absolute():
        requested = Path("/workspace") / requested
    if not allow_external and requested.parts[1:2] != ("workspace",):
        raise ValueError("Path must remain below /workspace")
    if requested.parts[:2] == ("/", "workspace"):
        relative = Path(*requested.parts[2:])
    else:
        relative = Path(*requested.parts[1:])
    root = (WORKSPACE_ROOT / workspace_uuid).resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Workspace path escapes its isolated root")
    return resolved


class MobileFakeProvider(ProviderAdapter):
    """Deterministic stream provider which exercises native harness tool/gate flow."""

    name = "mobile-e2e-local"

    @staticmethod
    def _assistant_has_tool_result(messages: list[Any], tool: str) -> bool:
        """Return true only after this provider's matching tool call resolves."""
        call_ids = [
            str(call.get("id", ""))
            for message in messages
            if getattr(message, "role", "") == "assistant"
            for call in getattr(message, "tool_calls", [])
            if call.get("name") == tool and call.get("id")
        ]
        return bool(call_ids) and any(
            getattr(message, "role", "") == "tool"
            and str(getattr(message, "tool_call_id", "")) in call_ids
            for message in messages
        )

    @staticmethod
    def _latest_user_prompt(messages: list[Any]) -> str:
        """Extract user text from ordinary and multimodal provider messages."""
        for message in reversed(messages):
            if getattr(message, "role", "") != "user":
                continue
            content = getattr(message, "content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict)
                )
            return json.dumps(content)
        return ""

    async def chat_stream(
        self, model: str, messages: list[Any], tools: list[Any], opts: Any = None
    ) -> AsyncIterator[Delta]:
        """Yield canned chunks, a real question/edit tool call, or abortable text."""
        prompt = self._latest_user_prompt(messages).lower()
        live_markers = (
            "mobile-e2e:question",
            "mobile-e2e:permission",
            "mobile-e2e:abort",
            "mobile-e2e:edit",
            "mobile-e2e:fork",
        )
        is_live_prompt = any(marker in prompt for marker in live_markers)
        call: dict[str, Any] | None = None
        if "mobile-e2e:question" in prompt and not self._assistant_has_tool_result(
            messages, "question"
        ):
            call = {
                "index": 0,
                "id": "mobile-question-1",
                "name": "question",
                "arguments": json.dumps(
                    {
                        "questions": [
                            {
                                "header": "Mobile fixture",
                                "question": "Which color should the fixture choose?",
                                "options": [
                                    {"label": "Blue", "description": "Use blue"},
                                    {"label": "Green", "description": "Use green"},
                                ],
                                "multiple": False,
                            }
                        ]
                    }
                ),
            }
        elif "mobile-e2e:permission" in prompt and not self._assistant_has_tool_result(
            messages, "edit"
        ):
            call = {
                "index": 0,
                "id": "mobile-permission-1",
                "name": "edit",
                "arguments": json.dumps(
                    {
                        "path": "/workspace/README.md",
                        "old_string": "# Mobile Fixture",
                        "new_string": "# Mobile Fixture — approved edit",
                    }
                ),
            }
        if is_live_prompt and call is not None:
            yield Delta(text="Checking the requested interactive flow. ")
            yield Delta(tool_calls=(call,))
            yield Delta(usage=Usage(4, 3, 7, 0.0), finish_reason="tool_calls")
            return

        if is_live_prompt and "mobile-e2e:abort" in prompt:
            chunks = ("This run is", " still active", " for abort.")
            for index, chunk in enumerate(chunks):
                await asyncio.sleep(2)
                usage = Usage(1, 1, 2) if index == len(chunks) - 1 else None
                yield Delta(text=chunk, usage=usage)
            return

        if is_live_prompt and "mobile-e2e:edit" in prompt:
            text = "Edited mobile fixture turn complete."
        elif is_live_prompt and "mobile-e2e:fork" in prompt:
            text = "Forked mobile fixture response complete."
        elif is_live_prompt and "mobile-e2e:question" in prompt:
            text = "Thanks, the selected color was recorded."
        elif is_live_prompt and "mobile-e2e:permission" in prompt:
            text = "Permission approved; the local file edit completed."
        elif is_live_prompt and "mobile-e2e:abort" in prompt:
            text = "The local stream finished before a stop request."
        else:
            text = (
                "Mobile fixture stream is live.\n\n"
                "| Check | Result |\n|---|---|\n| Django API | **real** |\n"
                "| Socket.IO | real |\n\n"
                "[Fixture README](/workspace/README.md)\n\n"
                "![Fixture preview](/workspace/preview.png)\n\n"
                "Upload a small text or image file to exercise the isolated workspace."
            )
        for offset in range(0, len(text), 18):
            yield Delta(text=text[offset : offset + 18])
            await asyncio.sleep(0.025)
        completion_tokens = max(1, len(text) // 4)
        yield Delta(usage=Usage(7, completion_tokens, 7 + completion_tokens))


class LocalWorkspaceAccessor(WorkspaceAccessor):
    """Typed WorkspaceAccessor confined to a run-owned virtual workspace."""

    def _root(self) -> Path:
        return _vpath(self.workspace_id, "/workspace")

    async def exec_stream(
        self,
        command: list[str] | str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[ExecChunk]:
        """Stream deterministic stdout without leaving the fixture directory."""
        result = await self.exec_wait(command, workdir, env, timeout)
        if result.stdout:
            yield ExecChunk(stream="stdout", data=result.stdout)
        if result.stderr:
            yield ExecChunk(stream="stderr", data=result.stderr)
        yield ExecChunk(stream="", exit_code=result.exit_code, done=True)

    async def exec_wait(
        self,
        command: list[str] | str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """Run a small whitelisted command vocabulary, never an arbitrary shell."""
        args = (
            shlex.split(command)
            if isinstance(command, str)
            else [str(arg) for arg in command]
        )
        cwd = _vpath(self.workspace_id, workdir, allow_external=True)
        if not cwd.is_dir():
            return ExecResult(1, stderr=f"workdir does not exist: {workdir}")
        try:
            if args[:2] == ["echo", "mobile-e2e-permission-ok"]:
                return ExecResult(0, "mobile-e2e-permission-ok\n")
            if args and args[0] == "pwd":
                relative = str(cwd.relative_to(self._root()))
                path = "/workspace" + (f"/{relative}" if relative != "." else "")
                return ExecResult(0, path + "\n")
            if args and args[0] == "ls":
                target_path = args[-1] if len(args) > 1 else "/workspace"
                target = _vpath(self.workspace_id, target_path)
                names = sorted(item.name for item in target.iterdir())
                return ExecResult(0, "\n".join(names) + "\n")
            if args and args[0] == "cat":
                target = _vpath(self.workspace_id, args[-1])
                return ExecResult(0, target.read_text())
            if args and args[:2] == ["sh", "-c"] and len(args) > 2:
                return await self.exec_wait(args[2], workdir, env, timeout)
        except (OSError, UnicodeError, ValueError) as exc:
            return ExecResult(1, stderr=str(exc))
        command_name = args[0] if args else ""
        return ExecResult(
            126,
            stderr=f"Command is not available in the isolated fixture: {command_name}",
        )

    async def read_file(self, path: str, max_size: int | None = None) -> FileContent:
        """Read local fixture bytes using production typed file metadata."""
        target = _vpath(self.workspace_id, path)
        content = target.read_bytes()
        limit = int(max_size) if max_size is not None else 5 * 1024 * 1024
        if limit <= 0:
            raise ValueError("max_size must be positive")
        return FileContent(
            content[:limit], len(content), len(content) > limit, self._mime(path)
        )

    async def write_file(self, path: str, content: bytes, mode: int = 0o644) -> None:
        """Write a file only under this fixture's workspace root."""
        target = _vpath(self.workspace_id, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(content))
        target.chmod(mode)

    async def list_dir(self, path: str) -> list[DirEntry]:
        """Return typed entries for the selected local directory."""
        target = _vpath(self.workspace_id, path)
        entries = []
        for item in sorted(target.iterdir()):
            virtual_path = "/workspace/" + str(item.relative_to(self._root()))
            size = item.stat().st_size if item.is_file() else 0
            entries.append(DirEntry(item.name, virtual_path, item.is_dir(), size))
        return entries

    async def stat(self, path: str) -> FileStat:
        """Stat a workspace-local path."""
        target = _vpath(self.workspace_id, path)
        size = target.stat().st_size if target.is_file() else 0
        return FileStat(path, target.is_dir(), size, self._mime(path))

    async def desktop_action(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("Desktop access is not part of the mobile chat fixture")

    async def process_start(
        self,
        command: str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        name: str = "",
        *,
        session_id: str | None = None,
        kind: str = "persistent",
    ) -> dict[str, Any]:
        raise RuntimeError(
            "Background processes are not part of the mobile chat fixture"
        )

    async def process_list(
        self, *, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    async def process_get(
        self, process_id: str, *, session_id: str | None = None
    ) -> dict[str, Any]:
        raise FileNotFoundError(process_id)

    async def process_stop(
        self, process_id: str, *, session_id: str | None = None
    ) -> dict[str, Any]:
        raise FileNotFoundError(process_id)

    async def process_restart(
        self, process_id: str, *, session_id: str | None = None
    ) -> dict[str, Any]:
        raise FileNotFoundError(process_id)

    async def process_delete(self, process_id: str) -> dict[str, Any]:
        raise FileNotFoundError(process_id)

    async def ensure_runtime_artifact(
        self, artifact_id: str, version: str
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "Runtime artifacts are unavailable in the isolated fixture",
        }

    async def open_process(
        self,
        command: list[str],
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        *,
        owner: dict[str, str] | None = None,
    ):
        raise RuntimeError("Process streams are unavailable in the isolated fixture")

    async def open_tcp(
        self,
        host: str,
        port: int,
        tls: bool = False,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ):
        raise RuntimeError("TCP streams are unavailable in the isolated fixture")

    @staticmethod
    def _mime(path: str) -> str:
        import mimetypes

        return mimetypes.guess_type(path)[0] or "application/octet-stream"


async def _make_accessor(workspace_id: str) -> LocalWorkspaceAccessor:
    """Match the production async accessor-factory contract."""
    return LocalWorkspaceAccessor(workspace_id)


class LocalHarnessService(HarnessService):
    """Real persistent harness service, using only explicit local adapters."""

    def __init__(self, emit):
        self._fixture_provider = MobileFakeProvider()
        evaluator = PermissionEvaluator(
            global_rules={
                "*": "allow",
                "edit": {"/workspace/README.md": "ask"},
                "read": {"/workspace/README.md": "allow"},
                "bash": {"echo mobile-e2e-permission-ok": "ask"},
            }
        )

        def runner_factory(**kwargs):
            from apps.harness.runner import HarnessRunner

            return HarnessRunner(evaluator=evaluator, **kwargs)

        super().__init__(
            permissions=PermissionService(evaluator=evaluator),
            emit=emit,
            provider_factory=lambda _org: self._fixture_provider,
            accessor_factory=_make_accessor,
            runner_factory=runner_factory,
            process_cleanup=lambda _workspace_id, _session_id, **_kwargs: None,
        )


def _manager_isolation_guard() -> None:
    """Refuse reuse of the fixed-channel Redis manager in a mobile fixture."""
    from apps.runners import sio_server

    existing = sio_server._sio
    if existing is not None:
        if existing.manager.__class__.__name__ == "AsyncRedisManager":
            raise RuntimeError(
                "Refusing to reuse fixed-channel Redis Socket.IO manager"
            )
        if getattr(existing, "_mobile_e2e_handlers_wrapped", False):
            return
        runner_connect = existing.handlers["/"]["connect"]
        runner_disconnect = existing.handlers["/"]["disconnect"]

        async def fixture_runner_connect(sid, environ, auth=None):
            await runner_connect(sid, environ, auth)
            connection = await existing.get_session(sid)
            if str(connection.get("runner_id", "")) == RUNNER_ID:
                existing._mobile_e2e_runner_sid = sid

        async def fixture_runner_disconnect(sid):
            await runner_disconnect(sid)
            if getattr(existing, "_mobile_e2e_runner_sid", "") == sid:
                existing._mobile_e2e_runner_sid = ""

        existing.on("connect", fixture_runner_connect)
        existing.on("disconnect", fixture_runner_disconnect)
        existing._mobile_e2e_handlers_wrapped = True
        return
    server = socketio.AsyncServer(
        async_mode="asgi",
        client_manager=AsyncManager(),
        cors_allowed_origins="*",
        logger=False,
        engineio_logger=False,
        max_http_buffer_size=200 * 1024 * 1024,
        monitor_clients=True,
    )
    _register_event_handlers(server)
    _register_frontend_handlers(server)

    runner_connect = server.handlers["/"]["connect"]
    runner_disconnect = server.handlers["/"]["disconnect"]

    async def fixture_runner_connect(sid, environ, auth=None):
        await runner_connect(sid, environ, auth)
        connection = await server.get_session(sid)
        if str(connection.get("runner_id", "")) == RUNNER_ID:
            server._mobile_e2e_runner_sid = sid

    async def fixture_runner_disconnect(sid):
        await runner_disconnect(sid)
        if getattr(server, "_mobile_e2e_runner_sid", "") == sid:
            server._mobile_e2e_runner_sid = ""

    server.on("connect", fixture_runner_connect)
    server.on("disconnect", fixture_runner_disconnect)
    sio_server._sio = server


def _install_fixture_runner_service() -> None:
    """Use the real RunnerService facade with only its dispatcher wrapped."""
    from apps.runners import sio_server
    from apps.runners.services import RunnerService
    from apps.runners.services.domains.file_transfer import FileTransferMixin
    from apps.runners.services.infra.ownership import OwnershipMixin

    server = sio_server._sio
    if server is None:
        raise RuntimeError("The isolated process-local Socket.IO server is missing")
    if getattr(server, "_mobile_e2e_service_patched", False):
        return
    server._mobile_e2e_service_patched = True

    original_emit = RunnerService._emit_to_runner
    original_get_runner_service = sio_server.get_runner_service

    def fixture_get_runner_service():
        """Return the fixture process's runner facade across async handler tasks."""
        sio_server._runner_service = None
        service = original_get_runner_service()
        if getattr(service, "_mobile_e2e_transport_patched", False):
            return service
        emit_base = service._emit_to_runner
        available_base = service._ensure_workspace_available

        async def emit_fixture_events(runner, event, data):
            if str(runner.id) != RUNNER_ID:
                return await emit_base(runner, event, data)
            sid = getattr(server, "_mobile_e2e_runner_sid", "")
            if not sid:
                from apps.runners.exceptions import RunnerOfflineError

                raise RunnerOfflineError(str(runner.id))
            await server.emit(event, data, to=sid, namespace="/")

        def ensure_fixture_workspace(workspace):
            if (
                str(workspace.runner_id) == RUNNER_ID
                and workspace.status == WorkspaceStatus.RUNNING
            ):
                return
            available_base(workspace)

        service._emit_to_runner = emit_fixture_events
        service._ensure_workspace_available = ensure_fixture_workspace
        service._mobile_e2e_transport_patched = True
        return service

    sio_server.get_runner_service = fixture_get_runner_service
    original_available = OwnershipMixin._ensure_workspace_available
    original_validate_upload_start = FileTransferMixin._validate_upload_start
    original_validate_upload_relay = FileTransferMixin._validate_upload_relay

    def fixture_ensure_available(self, workspace):
        original_available(self, workspace)
        if workspace.status == WorkspaceStatus.RUNNING:
            return
        if str(workspace.runner_id) != RUNNER_ID:
            return
        raise ValueError(
            f"Fixture file RPC expects a running fixture workspace ({workspace.id})"
        )

    def fixture_validate_upload_start(data):
        if isinstance(data, dict) and data.get("request_id", "").startswith(
            "mobile-upload-"
        ):
            try:
                return 0 < int(data.get("total_chunks", 0)) <= 64
            except (TypeError, ValueError):
                return False
        return original_validate_upload_start(data)

    def fixture_validate_upload_relay(event, data):
        if (
            isinstance(data, dict)
            and data.get("request_id", "").startswith("mobile-upload-")
            and event == "files:upload_finish"
        ):
            return True
        return original_validate_upload_relay(event, data)

    OwnershipMixin._ensure_workspace_available = fixture_ensure_available
    FileTransferMixin._validate_upload_start = staticmethod(
        fixture_validate_upload_start
    )
    FileTransferMixin._validate_upload_relay = staticmethod(
        fixture_validate_upload_relay
    )

    # RunnerService's facade is created lazily on the first runner connection.
    # The getter wrapper above patches it then, without touching unrelated runners.
    del original_emit


def configure_runtime() -> None:
    """Install isolated ports; use the persisted run runner id after seeding."""
    global RUNNER_ID
    if not RUNNER_ID and manifest_path().exists():
        try:
            RUNNER_ID = str(load_manifest()["runner_id"])
        except (KeyError, OSError, ValueError):
            RUNNER_ID = ""
    from apps.harness import harness_service as harness_module
    from apps.harness.providers import models_catalog
    from apps.runners import sio_server

    models_catalog.openai_compatible_models = _fake_catalog_for_fixture
    _manager_isolation_guard()
    _install_fixture_runner_service()
    reset_default_harness_service()
    sio_server._runner_service = get_runner_service()
    harness_module._default_harness_service = None
    harness_module.get_harness_service = lambda: SERVICE


def _emit_factory():
    """Fan out real harness events to production-authenticated subscribers."""
    from apps.runners.sio_server import emit_to_frontend

    async def emit(event: str, data: dict[str, Any]) -> None:
        workspace_id = str(data.get("workspace_id", ""))
        await emit_to_frontend(event, data, workspace_id)

    return emit


def _fake_catalog_for_fixture(model_ids: list[str]):
    """Return one tool-capable picker entry and make all other fixture IDs absent."""
    from apps.harness.providers.models_catalog import ProviderModel

    if "fake" not in model_ids:
        return []
    return [
        ProviderModel(
            id="openai-compatible/fake",
            name="Local deterministic fixture",
            reasoning_efforts=(),
            default_effort="",
            supports_tools=True,
            context_length=32768,
            max_output_tokens=4096,
            provider="openai-compatible",
        )
    ]


def seed() -> None:
    """Migrate and seed deterministic tenant, workspaces and conversation history."""
    global RUNNER_ID
    from apps.harness.models import ProviderConnection
    from apps.harness.repositories import HarnessSessionRepository
    from django.core.management import call_command

    if manifest_path().exists():
        raise RuntimeError(
            "Existing mobile fixture manifest; choose a fresh run directory"
        )
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "e2e.mobile.settings":
        raise RuntimeError("Refusing to seed outside e2e.mobile.settings")
    connection = connections["default"]
    db_name = Path(connection.settings_dict["NAME"]).resolve()
    if not db_name.is_relative_to(RUN_DIR):
        raise RuntimeError("Refusing to migrate outside this run's SQLite DB")
    call_command("migrate", interactive=False, run_syncdb=True, verbosity=0)

    password = os.environ.get("MOBILE_E2E_PASSWORD", "")
    if len(password) < 12:
        raise RuntimeError(
            "Set MOBILE_E2E_PASSWORD to a disposable >=12-character password"
        )
    user_model = get_user_model()
    user = user_model.objects.create_user("mobile@example.test", password=password)
    stranger = user_model.objects.create_user(
        email=f"mobile-stranger-{uuid.uuid4().hex[:8]}@example.test",
        password=password,
    )
    org = Organization.objects.create(
        name="Mobile Fixture Org",
        slug=f"mobile-e2e-{uuid.uuid4().hex[:10]}",
    )
    Membership.objects.create(user=user, organization=org, role="admin")
    from apps.skills.models import Skill

    Skill.objects.create(
        user=user,
        created_by=user,
        name="Mobile review",
        body="Review the workspace carefully and explain the result briefly.",
    )
    from apps.harness.providers import models_catalog as catalog_module
    from apps.harness.providers.models_catalog import _catalog_cache
    from common.utils import encrypt_value

    ProviderConnection.objects.create(
        organization=org,
        provider="openai-compatible",
        credentials_encrypted=encrypt_value(
            json.dumps({"api_key": "mobile-fixture-not-a-provider-key"})
        ),
        config={
            "base_url": "http://127.0.0.1:9/fixture-disabled/v1",
            "models": ["fake"],
        },
    )
    catalog_module.openai_compatible_models = _fake_catalog_for_fixture
    _catalog_cache.set(str(org.id), _fake_catalog_for_fixture(["fake"]))

    from apps.accounts.models import APIKey, APIKeyPermission
    from common.utils import generate_api_token

    read_only_token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="mobile-fixture-read-only",
        key_hash=hash_token(read_only_token),
        key_prefix=read_only_token[:12],
        permissions=[APIKeyPermission.HARNESS_READ.value],
    )
    _atomic_json(
        RUN_DIR / "api-key-secret.json",
        {"read_only_api_key": read_only_token},
    )

    runner_token = "mobile-fixture-runner-" + uuid.uuid4().hex
    foreign_org = Organization.objects.create(
        name="Mobile Foreign Tenant",
        slug=f"mobile-foreign-{uuid.uuid4().hex[:10]}",
    )
    Membership.objects.create(user=stranger, organization=foreign_org, role="member")
    runner = Runner.objects.create(
        name="Local mobile fixture runner",
        api_token_hash=hash_token(runner_token),
        status=RunnerStatus.ONLINE,
        sid="mobile-fixture-synthetic-runner",
        organization=org,
        available_runtimes=["docker"],
    )
    runner_id = str(runner.id)
    RUNNER_ID = runner_id
    foreign_runner = Runner.objects.create(
        name="Foreign tenant runner",
        api_token_hash=hash_token("mobile-fixture-foreign-" + uuid.uuid4().hex),
        status=RunnerStatus.OFFLINE,
        organization=foreign_org,
        available_runtimes=["docker"],
    )
    foreign_workspace = Workspace.objects.create(
        runner=foreign_runner,
        name="Foreign tenant private workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=stranger,
    )
    foreign_session = HarnessSessionRepository.create(
        workspace_id=foreign_workspace.id,
        organization_id=foreign_org.id,
        title="Foreign private conversation",
        mode="build",
        agent_name="build",
        model="openai-compatible/fake",
        created_by_id=stranger.id,
    )

    workspace_seeds = (
        ("Mobile Playground", WorkspaceStatus.RUNNING),
        ("Mobile Starter", WorkspaceStatus.RUNNING),
        ("Mobile Stopped", WorkspaceStatus.STOPPED),
    )
    workspace_ids: list[str] = []
    for name, status in workspace_seeds:
        workspace = Workspace.objects.create(
            runner=runner, name=name, status=status, created_by=user
        )
        workspace_ids.append(str(workspace.id))
        root = _vpath(str(workspace.id), "/workspace")
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "README.md").write_text(
            "# Mobile Fixture\n\nWorkspace files stay under "
            "/tmp/opencuria-mobile-e2e.\n"
        )
        (root / "src" / "hello.py").write_text('print("hello from workspace")\n')
        (root / "sample.pdf").write_bytes(
            b"%PDF-1.4\n% mobile fixture placeholder\n%%EOF\n"
        )
        (root / "preview.png").write_bytes(
            (Path(__file__).parent / "assets" / "preview.png").read_bytes()
        )

    primary, second, stopped = [
        Workspace.objects.get(pk=item) for item in workspace_ids
    ]
    seeded_session_ids: list[str] = []
    history = HarnessSessionRepository.create(
        workspace_id=primary.id,
        organization_id=org.id,
        title="Example chat — history",
        mode="build",
        agent_name="build",
        model="openai-compatible/fake",
        created_by_id=user.id,
    )
    permission_session = HarnessSessionRepository.create(
        workspace_id=primary.id,
        organization_id=org.id,
        title="Permission gate demo",
        mode="build",
        agent_name="build",
        model="openai-compatible/fake",
        created_by_id=user.id,
    )
    question_session = HarnessSessionRepository.create(
        workspace_id=primary.id,
        organization_id=org.id,
        title="Question gate demo",
        mode="build",
        agent_name="build",
        model="openai-compatible/fake",
        created_by_id=user.id,
    )
    user_message = HarnessMessageRepository.create(
        session_id=history.id,
        role="user",
        content="Summarize the sample files.",
    )
    HarnessPartRepository.create(
        message_id=user_message.id,
        type="text",
        state="completed",
        output=user_message.content,
    )
    assistant = HarnessMessageRepository.create(
        session_id=history.id,
        role="assistant",
        content="",
        model="openai-compatible/fake",
        provider="mobile-e2e-local",
    )
    assistant.tokens = {"prompt": 9, "completion": 24, "total": 33}
    assistant.save(update_fields=["tokens"])
    HarnessPartRepository.create(
        message_id=assistant.id,
        type="text",
        state="completed",
        output=(
            "The fixture keeps files private and tenant-scoped.\n\n"
            "| File | Type |\n|---|---|\n| README.md | Markdown |\n"
            "| preview.png | Image |\n\n"
            "[Fixture link](https://example.test/fixture)\n\n"
            "![Fixture image](/workspace/preview.png)"
        ),
    )
    pdf_data = base64.b64encode(b"%PDF-1.4\\n%%EOF").decode()
    HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        state="completed",
        call_id="fixture-read-pdf",
        title="Read sample.pdf",
        input={"tool": "read", "arguments": '{"path":"/workspace/sample.pdf"}'},
        output="Read sample.pdf (application/pdf)",
        meta={
            "attachments": [
                {
                    "type": "file",
                    "mime": "application/pdf",
                    "filename": "sample.pdf",
                    "url": "data:application/pdf;base64," + pdf_data,
                }
            ]
        },
    )
    for index in range(80):
        role = "user" if index % 2 == 0 else "assistant"
        content = f"History row {index + 1}: stable mobile transcript seed."
        message = HarnessMessageRepository.create(
            session_id=history.id,
            role=role,
            content="" if role == "assistant" else content,
        )
        HarnessPartRepository.create(
            message_id=message.id,
            type="text",
            state="completed",
            output=content,
        )
    seeded_session_ids.append(str(history.id))

    secondary_history = (
        (second, "Starter history", 4),
        (stopped, "Stopped workspace history", 2),
    )
    for workspace, title, count in secondary_history:
        session = HarnessSessionRepository.create(
            workspace_id=workspace.id,
            organization_id=org.id,
            title=title,
            mode="build",
            agent_name="build",
            model="openai-compatible/fake",
            created_by_id=user.id,
        )
        for index in range(count):
            role = "user" if index % 2 == 0 else "assistant"
            message = HarnessMessageRepository.create(
                session_id=session.id,
                role=role,
                content=f"{title}: message {index + 1}",
            )
            HarnessPartRepository.create(
                message_id=message.id,
                type="text",
                state="completed",
                output=message.content,
            )
        seeded_session_ids.append(str(session.id))

    child = HarnessSessionRepository.create(
        workspace_id=primary.id,
        organization_id=org.id,
        title="Explore the sample files",
        mode="plan",
        agent_name="explore",
        model="openai-compatible/fake",
        parent_id=history.id,
        created_by_id=user.id,
    )
    child_user = HarnessMessageRepository.create(
        session_id=child.id,
        role="user",
        content="Inspect README.md",
    )
    HarnessPartRepository.create(
        message_id=child_user.id,
        type="text",
        state="completed",
        output=child_user.content,
    )
    child_assistant = HarnessMessageRepository.create(
        session_id=child.id,
        role="assistant",
        content="Found README.md and src/hello.py.",
        model="openai-compatible/fake",
    )
    HarnessPartRepository.create(
        message_id=child_assistant.id,
        type="text",
        state="completed",
        output=child_assistant.content,
    )

    seeded_session_ids.extend([str(permission_session.id), str(question_session.id)])

    editable_session = HarnessSessionRepository.create(
        workspace_id=primary.id,
        organization_id=org.id,
        title="Editable mobile test",
        mode="build",
        agent_name="build",
        model="openai-compatible/fake",
        created_by_id=user.id,
    )
    editable_prompt = "Original message for the mobile edit flow."
    editable_user = HarnessMessageRepository.create(
        session_id=editable_session.id,
        role="user",
        content=editable_prompt,
    )
    HarnessPartRepository.create(
        message_id=editable_user.id,
        type="text",
        state="completed",
        output=editable_prompt,
    )
    editable_answer = HarnessMessageRepository.create(
        session_id=editable_session.id,
        role="assistant",
        content="The seeded edit conversation is ready.",
        model="openai-compatible/fake",
    )
    HarnessPartRepository.create(
        message_id=editable_answer.id,
        type="text",
        state="completed",
        output=editable_answer.content,
    )
    seeded_session_ids.append(str(editable_session.id))

    abort_session = HarnessSessionRepository.create(
        workspace_id=second.id,
        organization_id=org.id,
        title="Abort mobile test",
        mode="build",
        agent_name="build",
        model="openai-compatible/fake",
        created_by_id=user.id,
    )
    seeded_session_ids.append(str(abort_session.id))

    _atomic_json(
        manifest_path(),
        {
            "run_id": RUN_DIR.name,
            "org_id": str(org.id),
            "user_id": user.id,
            "runner_id": str(runner.id),
            "workspace_ids": workspace_ids,
            "session_ids": seeded_session_ids,
            "active_session_ids": [],
            "permission_session_id": str(permission_session.id),
            "question_session_id": str(question_session.id),
            "abort_session_id": str(abort_session.id),
            "primary_workspace_id": str(primary.id),
            "seeded_session_id": str(history.id),
            "editable_session_id": str(editable_session.id),
            "editable_message_id": str(editable_user.id),
            "email": user.email,
            "read_only_key": read_only_token,
            "foreign_workspace_id": str(foreign_workspace.id),
            "foreign_session_id": str(foreign_session.id),
            "password_env": "MOBILE_E2E_PASSWORD",
            "http_url": os.environ.get("MOBILE_E2E_HTTP_URL", "http://127.0.0.1:8022"),
            "socket_url": os.environ.get(
                "MOBILE_E2E_SOCKET_URL", "http://127.0.0.1:8022"
            ),
            "socket_path": "/ws/runner",
            "workspace_root": str(WORKSPACE_ROOT),
            "fixture_kind": (
                "real Django HTTP API/JWT/authz/ASGI Socket.IO + local deterministic "
                "ProviderAdapter and typed WorkspaceAccessor; no live external LLM "
                "or runner"
            ),
        },
    )
    _atomic_json(RUN_DIR / "runner-secret.json", {"runner_token": runner_token})
    print(f"Seeded isolated mobile fixture: {RUN_DIR}")


async def _http_lifecycle_smoke(
    base: str, headers: dict[str, str], manifest: dict[str, Any]
) -> dict[str, Any]:
    """Exercise actual rename, fork, unread, edit, busy, abort, and delete APIs."""

    async def request(method: str, url: str, **kwargs: Any):
        return await asyncio.to_thread(requests.request, method, url, **kwargs)

    async def timeline_until_idle(session_id: str) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + 35
        last = None
        while asyncio.get_running_loop().time() < deadline:
            last = await request(
                "GET",
                f"{base}/api/v1/harness/sessions/{session_id}/timeline",
                headers=headers,
                timeout=5,
            )
            if last.status_code == 200 and last.json()["session"]["status"] == "idle":
                return last.json()
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"Session {session_id} failed to become idle: "
            f"{last.status_code if last else 'no response'}"
        )

    session_id = manifest["editable_session_id"]
    message_id = manifest["editable_message_id"]
    session_url = f"{base}/api/v1/harness/sessions/{session_id}"

    # Rename and both unread transitions are persisted in the production API.
    renamed = await request(
        "PATCH",
        session_url,
        headers=headers,
        json={"title": "Mobile fixture renamed"},
        timeout=10,
    )
    assert renamed.status_code == 200, renamed.text[:500]
    assert renamed.json()["title"] == "Mobile fixture renamed"
    marked_unread = await request(
        "POST", session_url + "/unread", headers=headers, timeout=10
    )
    assert marked_unread.status_code == 204, marked_unread.text[:300]
    timeline = await request(
        "GET", session_url + "/timeline", headers=headers, timeout=10
    )
    assert timeline.status_code == 200
    assert timeline.json()["session"]["manual_unread"] is True
    marked_read = await request(
        "POST", session_url + "/read", headers=headers, timeout=10
    )
    assert marked_read.status_code == 204, marked_read.text[:300]
    timeline = await request(
        "GET", session_url + "/timeline", headers=headers, timeout=10
    )
    assert timeline.status_code == 200
    assert timeline.json()["session"]["manual_unread"] is False

    # Native edit/rerun creates new message identities; always re-read the
    # current cutoff rather than reusing the seed manifest after another run.
    message_id = next(
        row["id"] for row in timeline.json()["messages"] if row["role"] == "user"
    )

    # The cutoff message belongs to this session. Include it to prove the
    # backend copies persisted messages rather than just allocating an ID.
    forked = await request(
        "POST",
        session_url + "/fork",
        headers=headers,
        json={"message_id": message_id, "include_message": True},
        timeout=10,
    )
    assert forked.status_code == 201, forked.text[:500]
    fork_id = str(forked.json()["id"])
    assert fork_id != session_id and forked.json()["parent_id"] is None
    fork_timeline = await request(
        "GET",
        f"{base}/api/v1/harness/sessions/{fork_id}/timeline",
        headers=headers,
        timeout=10,
    )
    assert fork_timeline.status_code == 200, fork_timeline.text[:300]
    original_message = next(
        row for row in timeline.json()["messages"] if row["id"] == message_id
    )
    assert any(
        row["role"] == original_message["role"]
        and row["content"] == original_message["content"]
        for row in fork_timeline.json()["messages"]
    ), fork_timeline.json()["messages"]
    fork_deleted = await request(
        "DELETE",
        f"{base}/api/v1/harness/sessions/{fork_id}",
        headers=headers,
        timeout=10,
    )
    assert fork_deleted.status_code == 204, fork_deleted.text[:300]
    missing_fork = await request(
        "GET",
        f"{base}/api/v1/harness/sessions/{fork_id}/timeline",
        headers=headers,
        timeout=10,
    )
    assert missing_fork.status_code == 404, missing_fork.text[:300]

    # Edit preserves the session, replaces the user message and starts a real
    # HarnessService run. Its initial response is busy; the fake provider is
    # deliberately streamed so this state is observable before polling idle.
    edited = await request(
        "POST",
        f"{base}/api/v1/harness/sessions/{session_id}/messages/{message_id}/edit",
        headers=headers,
        json={"prompt": "mobile-e2e:edit"},
        timeout=10,
    )
    assert edited.status_code == 202, edited.text[:500]
    assert edited.json()["status"] == "busy", edited.json()
    edited_timeline = await timeline_until_idle(session_id)
    assert any(
        row["role"] == "user" and row["content"] == "mobile-e2e:edit"
        for row in edited_timeline["messages"]
    )
    assert any(
        part.get("type") == "text"
        and "Edited mobile fixture turn complete." in part.get("output", "")
        for message in edited_timeline["messages"]
        if message["role"] == "assistant"
        for part in message["parts"]
    )
    manifest["editable_message_id"] = next(
        row["id"]
        for row in edited_timeline["messages"]
        if row["role"] == "user" and row["content"] == "mobile-e2e:edit"
    )
    _atomic_json(manifest_path(), manifest)
    restored_title = await request(
        "PATCH",
        session_url,
        headers=headers,
        json={"title": "Editable mobile test"},
        timeout=10,
    )
    assert restored_title.status_code == 200, restored_title.text[:300]

    # Transient busy state is visible in both the create response and workspace
    # session list; concurrent follow-up conflicts; abort settles; delete cleans.
    workspace_id = manifest["workspace_ids"][1]
    abort_created = await request(
        "POST",
        f"{base}/api/v1/workspaces/{workspace_id}/harness/sessions/",
        headers=headers,
        json={
            "prompt": "mobile-e2e:abort",
            "agent_name": "build",
            "mode": "build",
            "harness_id": "native",
            "model": "openai-compatible/fake",
        },
        timeout=10,
    )
    assert abort_created.status_code == 201, abort_created.text[:500]
    transient_id = str(abort_created.json()["id"])
    assert abort_created.json()["status"] == "busy", abort_created.json()
    busy_sessions = await request(
        "GET",
        f"{base}/api/v1/workspaces/{workspace_id}/harness/sessions/",
        headers=headers,
        timeout=10,
    )
    assert busy_sessions.status_code == 200, busy_sessions.text[:300]
    listed_busy = next(row for row in busy_sessions.json() if row["id"] == transient_id)
    assert listed_busy["status"] == "busy"
    duplicate = await request(
        "POST",
        f"{base}/api/v1/harness/sessions/{transient_id}/message",
        headers=headers,
        json={"prompt": "mobile-e2e:abort"},
        timeout=10,
    )
    assert duplicate.status_code == 409, duplicate.text[:400]
    aborted = await request(
        "POST",
        f"{base}/api/v1/harness/sessions/{transient_id}/abort",
        headers=headers,
        timeout=10,
    )
    assert aborted.status_code == 200, aborted.text[:400]
    assert aborted.json()["status"] == "idle"
    await timeline_until_idle(transient_id)
    deleted = await request(
        "DELETE",
        f"{base}/api/v1/harness/sessions/{transient_id}",
        headers=headers,
        timeout=10,
    )
    assert deleted.status_code == 204, deleted.text[:300]
    missing = await request(
        "GET",
        f"{base}/api/v1/harness/sessions/{transient_id}/timeline",
        headers=headers,
        timeout=10,
    )
    assert missing.status_code == 404, missing.text[:300]
    return {
        "rename_status": renamed.status_code,
        "manual_unread_round_trip": True,
        "fork_status": forked.status_code,
        "fork_delete_status": fork_deleted.status_code,
        "edit_rerun_status": edited.status_code,
        "edited_run_idle": True,
        "transient_create_status": abort_created.status_code,
        "transient_busy_status": True,
        "busy_duplicate_status": duplicate.status_code,
        "abort_status": aborted.status_code,
        "transient_delete_status": deleted.status_code,
    }


def _run_http_smoke() -> None:
    """Validate JWT, cross-tenant authz, API sessions/catalog and Socket.IO login."""
    manifest = load_manifest()
    base = manifest["http_url"].rstrip("/")
    password = os.environ["MOBILE_E2E_PASSWORD"]
    bad_login = requests.post(
        base + "/api/v1/auth/login/",
        json={"email": manifest["email"], "password": "invalid"},
        timeout=5,
    )
    assert bad_login.status_code == 401, bad_login.text[:500]
    stranger = get_user_model().objects.get(email__startswith="mobile-stranger-")
    no_org_token = requests.post(
        base + "/api/v1/auth/login/",
        json={"email": stranger.email, "password": password},
        timeout=5,
    ).json()["access_token"]
    foreign_org = Organization.objects.get(name="Mobile Foreign Tenant")
    foreign_profile = requests.get(
        base + "/api/v1/auth/me/",
        headers={
            "Authorization": "Bearer " + no_org_token,
            "X-Organization-Id": manifest["org_id"],
        },
        timeout=5,
    )
    assert foreign_profile.status_code == 200
    foreign_org_ids = {
        row["id"] for row in foreign_profile.json().get("organizations", [])
    }
    assert foreign_org_ids == {str(foreign_org.id)}
    login = requests.post(
        base + "/api/v1/auth/login/",
        json={"email": manifest["email"], "password": password},
        timeout=5,
    )
    assert login.status_code == 200, login.text[:500]
    tokens = login.json()
    headers = {
        "Authorization": "Bearer " + tokens["access_token"],
        "X-Organization-Id": manifest["org_id"],
    }
    profile = requests.get(base + "/api/v1/auth/me/", headers=headers, timeout=5)
    assert profile.status_code == 200, profile.text[:500]
    workspaces = requests.get(base + "/api/v1/workspaces/", headers=headers, timeout=5)
    assert workspaces.status_code == 200, workspaces.text[:500]
    owned_workspaces = workspaces.json()
    assert len(owned_workspaces) == 3
    assert all(row["runner_online"] for row in owned_workspaces)
    sessions_url = (
        base
        + f"/api/v1/workspaces/{manifest['primary_workspace_id']}/harness/sessions/"
    )
    sessions = requests.get(sessions_url, headers=headers, timeout=5)
    assert sessions.status_code == 200, sessions.text[:500]
    sessions_body = sessions.json()
    assert any(row["id"] == manifest["seeded_session_id"] for row in sessions_body)
    assert all(row["status"] == "idle" for row in sessions_body)
    assert {"Permission gate demo", "Question gate demo"} <= {
        row["title"] for row in sessions_body
    }
    secret_file = RUN_DIR / "api-key-secret.json"
    if secret_file.stat().st_mode & 0o077:
        raise RuntimeError("Read-only API key artifact is not private")
    read_only_token = json.loads(secret_file.read_text())["read_only_api_key"]
    read_headers = {
        "X-API-Key": read_only_token,
        "X-Organization-Id": manifest["org_id"],
    }
    read_only_sessions = requests.get(sessions_url, headers=read_headers, timeout=5)
    assert read_only_sessions.status_code == 200, read_only_sessions.text[:500]
    write_denied = requests.post(
        f"{base}/api/v1/workspaces/{manifest['primary_workspace_id']}/harness/sessions/",
        headers=read_headers,
        json={"prompt": "This must fail for a read-only API key."},
        timeout=5,
    )
    assert write_denied.status_code == 403, write_denied.text[:500]
    models = requests.get(
        base + "/api/v1/provider-config/models/", headers=headers, timeout=5
    )
    assert models.status_code == 200, models.text[:500]
    assert [row["id"] for row in models.json()] == ["openai-compatible/fake"]

    stranger_login = requests.post(
        base + "/api/v1/auth/login/",
        json={"email": stranger.email, "password": password},
        timeout=5,
    )
    assert stranger_login.status_code == 200, stranger_login.text[:300]
    foreign_headers = {
        "Authorization": "Bearer " + stranger_login.json()["access_token"],
        "X-Organization-Id": str(foreign_org.id),
    }
    foreign_access = requests.get(sessions_url, headers=foreign_headers, timeout=5)
    assert foreign_access.status_code == 404, foreign_access.text[:500]
    wrong_org = requests.get(
        sessions_url,
        headers={
            "Authorization": headers["Authorization"],
            "X-Organization-Id": str(foreign_org.id),
        },
        timeout=5,
    )
    assert wrong_org.status_code == 500, wrong_org.text[:500]
    foreign_workspace_id = manifest["foreign_workspace_id"]
    foreign_workspace_access = requests.get(
        base + f"/api/v1/workspaces/{foreign_workspace_id}/harness/sessions/",
        headers=foreign_headers,
        timeout=5,
    )
    assert foreign_workspace_access.status_code == 200, foreign_workspace_access.text[
        :500
    ]
    assert any(
        row["id"] == manifest["foreign_session_id"]
        for row in foreign_workspace_access.json()
    )
    owned_foreign = requests.get(
        base + f"/api/v1/workspaces/{foreign_workspace_id}/harness/sessions/",
        headers=headers,
        timeout=5,
    )
    assert owned_foreign.status_code == 404, owned_foreign.text[:500]

    async def verify_frontend_socket() -> None:
        """Authenticate frontend JWT and subscribe to an owned workspace."""
        client = AsyncClient(reconnection=False)
        await client.connect(
            base,
            socketio_path="ws/runner",
            namespaces=["/frontend"],
            transports=["websocket"],
            auth={"token": tokens["access_token"]},
            wait_timeout=8,
        )
        await client.emit(
            "frontend:subscribe_workspace",
            {"workspace_id": manifest["primary_workspace_id"]},
            namespace="/frontend",
        )
        await client.disconnect()

    asyncio.run(verify_frontend_socket())
    lifecycle = asyncio.run(_http_lifecycle_smoke(base, headers, manifest))
    unauthorized = requests.get(base + "/api/v1/workspaces/", timeout=5)
    assert unauthorized.status_code == 401, unauthorized.text[:500]
    result = {
        "login_status": login.status_code,
        "profile_status": profile.status_code,
        "workspace_status": workspaces.status_code,
        "harness_sessions_status": sessions.status_code,
        "read_only_key_sessions_status": read_only_sessions.status_code,
        "read_only_key_create_session_status": write_denied.status_code,
        "fake_model_catalog_status": models.status_code,
        "foreign_tenant_workspace_status": foreign_access.status_code,
        "wrong_organization_header_status": wrong_org.status_code,
        "foreign_workspace_owner_status": foreign_workspace_access.status_code,
        "foreign_owner_cross_user_status": owned_foreign.status_code,
        "socket_frontend_handshake": True,
        **lifecycle,
        "unauthenticated_status": unauthorized.status_code,
        "organization_ids": [
            org["id"] for org in profile.json().get("organizations", [])
        ],
        "workspace_count": len(workspaces.json()),
        "jwt_pair_received": bool(
            tokens.get("access_token") and tokens.get("refresh_token")
        ),
    }
    _atomic_json(RUN_DIR / "http-smoke.json", result)
    print(json.dumps(result, indent=2))


def main() -> None:
    """Seed or verify an explicitly selected private fixture run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "smoke", "manifest"))
    args = parser.parse_args()
    if args.action == "seed":
        seed()
    elif args.action == "smoke":
        _run_http_smoke()
    else:
        print(json.dumps(load_manifest(), indent=2))


SERVICE = LocalHarnessService(_emit_factory())
configure_runtime()

if __name__ == "__main__":
    main()
