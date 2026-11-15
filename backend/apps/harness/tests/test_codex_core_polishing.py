"""Regression contracts for shared dispatch, stable tool ceilings and PDF input."""

from __future__ import annotations

import asyncio
import base64
import io
import uuid
from typing import Any

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from apps.harness.agents.definitions import get_agent
from apps.harness.engines.base import RunOptions
from apps.harness.engines.codex import media
from apps.harness.engines.codex.session_store import CodexSessionStore, home_for
from apps.harness.engines.codex.tools import ManagedToolInvoker
from apps.harness.engines.repositories import HarnessEngineRepository
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.scopes import permission_scope_matches
from apps.harness.runner import HarnessRunner
from apps.harness.tests.test_codex_core_safety import snapshot
from apps.harness.tools import default_tool_registry
from apps.harness.tools.base import ToolContext, ToolResult
from apps.harness.tools.invocation import AuthorizedToolDispatcher
from apps.harness.tools.subagents import TaskTool


def pdf_bytes(
    text: str = "Hello PDF", *, pages: int = 1, encrypted: bool = False
) -> bytes:
    """Generate a genuine text PDF with a font and content stream, no external files."""
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for _ in range(pages):
        page = writer.add_blank_page(width=300, height=300)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                )
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 10 20 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("private-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def file_part(payload: bytes) -> dict[str, Any]:
    """Build the canonical inline PDF part used by attachments and user history."""
    return {
        "type": "file",
        "mime": "application/pdf",
        "filename": "local.pdf",
        "url": "data:application/pdf;base64," + base64.b64encode(payload).decode(),
    }


async def drop_event(event: dict[str, Any]) -> None:
    """Ignore lifecycle events in dispatch-only tests."""


@pytest.mark.asyncio
async def test_pdf_text_and_dynamic_tool_conversion() -> None:
    """Real PDF text is supported and truthfully labelled as text-only."""
    result = await media.dynamic_content([file_part(pdf_bytes())])
    assert result[0]["type"] == "inputText"
    assert "Hello PDF" in result[0]["text"]
    assert "images and layout are not preserved" in result[0]["text"]
    block = media.data_url_block(file_part(pdf_bytes())["url"])
    assert (await media.convert([block]))[0]["type"] == "text"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,match",
    [
        (b"%PDF-invalid", "malformed"),
        (pdf_bytes(encrypted=True), "Encrypted"),
        (pdf_bytes(text=""), "no extractable text"),
        (pdf_bytes(pages=101), "100-page"),
        (pdf_bytes(text="A" * (512 * 1024 + 1)), "512 KiB"),
    ],
    ids=["malformed", "encrypted", "image-only", "page-limit", "text-limit"],
)
async def test_pdf_rejects_invalid_encrypted_empty_and_oversized(
    payload: bytes,
    match: str,
) -> None:
    """Unsupported documents fail visibly instead of yielding silent empty content."""
    with pytest.raises(ValueError, match=match):
        await media.dynamic_content([file_part(payload)])


@pytest.mark.asyncio
async def test_pdf_never_opens_arbitrary_file_uri() -> None:
    """Only checked inline content or authorized no-follow media is accepted."""
    part = file_part(b"unused")
    part["url"] = "file:///etc/passwd"
    with pytest.raises(ValueError, match="data URL"):
        await media.dynamic_content([part])
    part = file_part(b"%PDF-" + b"x" * (20 * 1024 * 1024))
    with pytest.raises(ValueError, match="input limit"):
        await media.dynamic_content([part])


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_pdf_timeout_and_cancellation_reap_parser(
    tmp_path,
    monkeypatch,
    cancel: bool,
) -> None:
    """Cancellation and wall timeouts kill and reap a parser, not a lingering thread."""
    worker = tmp_path / "slow_worker.py"
    worker.write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(media, "_PDF_WORKER", worker)
    monkeypatch.setattr(media, "PDF_TIMEOUT", 0.1 if not cancel else 10)
    original = asyncio.create_subprocess_exec
    created = asyncio.Event()
    children = []

    async def start(*args, **kwargs):
        process = await original(*args, **kwargs)
        children.append(process)
        created.set()
        return process

    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", start)
    task = asyncio.create_task(media.dynamic_content([file_part(pdf_bytes())]))
    await created.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ValueError):
        await task
    assert children[0].returncode is not None


@pytest.mark.asyncio
async def test_stable_ceiling_dispatch_uses_live_role_depth_and_policy() -> None:
    """Stored primary signatures cannot bypass role, depth or global decisions."""
    invoker = ManagedToolInvoker(default_tool_registry(), None, drop_event)
    opts = RunOptions()
    signatures = invoker.schemas(get_agent("plan"), "plan", opts)
    assert signatures == invoker.schemas(get_agent("build"), "build", opts)
    assert signatures == invoker.schemas(get_agent("plan"), "plan", opts)
    assert issubclass(HarnessRunner, AuthorizedToolDispatcher)
    allowed = {x["name"] for x in signatures}
    requests = []

    async def permission(**kwargs):
        requests.append(kwargs)
        return "deny"

    opts.on_permission = permission
    result = await invoker.invoke(
        {
            "tool": "write",
            "callId": "primary-plan",
            "arguments": {"path": "/workspace/a", "content": "x"},
        },
        agent=get_agent("build"),
        mode="plan",
        opts=opts,
        model="model",
        allowed=allowed,
    )
    assert result["success"] is False and requests == []
    # Default general policy must stay YOLO even beneath a plan parent.
    assert (
        invoker._decide(get_agent("general"), "write", "/workspace/a", "plan")
        == "allow"
    )
    opts.depth, opts.max_depth = 1, 1
    opts.run_subagent = permission
    for name, args in [("task", {}), ("todowrite", {})]:
        result = await invoker.invoke(
            {"tool": name, "callId": name, "arguments": args},
            agent=get_agent("general"),
            mode="plan",
            opts=opts,
            model="model",
            allowed=allowed,
        )
        assert result["success"] is False
    invoker.evaluator = PermissionEvaluator(global_rules={"read": "deny"})
    result = await invoker.invoke(
        {"tool": "read", "callId": "global", "arguments": {"path": "/workspace/a"}},
        agent=get_agent("build"),
        mode="build",
        opts=opts,
        model="model",
        allowed=allowed,
    )
    assert result["success"] is False


@pytest.mark.asyncio
async def test_permission_secret_projection_keeps_exact_action_scope() -> None:
    """Refresh-aware secrets are removed before callbacks but identity stays exact."""
    secrets = ["current-private-token"]
    invoker = ManagedToolInvoker(
        default_tool_registry(),
        None,
        drop_event,
        secrets=lambda: secrets,
    )
    requests = []

    async def permission(**kwargs):
        requests.append(kwargs)
        return "once"

    opts = RunOptions(on_permission=permission)
    for secret in [secrets[0], "refreshed-private-token"]:
        secrets.append(secret)
        action = f"echo {secret}"
        assert await invoker._resolve_ask(
            tool_name="bash",
            action=action,
            title=action,
            call_id="call",
            step=1,
            doom_loop=False,
            opts=opts,
        )
    assert len(requests) == 2
    assert not any(secret in str(requests) for secret in secrets)
    assert requests[0]["action"] == requests[1]["action"]
    assert not permission_scope_matches(
        requests[0]["permission_action"],
        requests[1]["permission_action"],
    )


@pytest.mark.asyncio
async def test_task_external_callback_needs_no_native_resolver() -> None:
    """External delegation can run with no fake provider/model resolver."""

    async def delegate(args, ctx, task_id):
        return ToolResult(output="external child result")

    ctx = ToolContext(
        session_id="session",
        workspace_id="workspace",
        accessor=None,
        registry=default_tool_registry(),
        run_subagent=delegate,
    )
    result = await TaskTool().execute(
        {
            "description": "Inspect",
            "prompt": "Inspect only",
            "subagent_type": "general",
        },
        ctx,
    )
    assert result.output == "external child result"


@pytest.mark.django_db(transaction=True)
def test_repository_checkpoint_rollback_closing_and_missing_generation(
    harness_workspace,
    monkeypatch,
) -> None:
    """Atomic repository writes preserve the last generation on encryption failure."""
    from asgiref.sync import async_to_sync

    from apps.harness.models import HarnessMessage, HarnessRun, HarnessRunStatus
    from apps.harness.repositories import HarnessSessionRepository

    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        model="openrouter/test/model",
    )
    run = HarnessRun.objects.create(
        session=session,
        assistant_message=HarnessMessage.objects.create(
            session=session, role="assistant"
        ),
        organization_id=session.organization_id,
        harness_id="codex",
        status=HarnessRunStatus.CLOSING,
    )
    store = CodexSessionStore(
        session_id=session.id, run_id=run.id, owner_token=run.owner_token
    )
    external, generation = str(uuid.uuid4()), str(uuid.uuid4())
    async_to_sync(store.save)(external, generation, snapshot(session.id))

    def fail_encrypt(value):
        raise RuntimeError("encryption unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("apps.harness.engines.repositories.encrypt_value", fail_encrypt)
        with pytest.raises(RuntimeError, match="encryption unavailable"):
            async_to_sync(store.save)(external, str(uuid.uuid4()), snapshot(session.id))
    assert (
        async_to_sync(store.load)(external, home_for(str(session.id)))["generation"]
        == generation
    )
    with pytest.raises(ValueError):
        HarnessEngineRepository.replace_owned_checkpoint(
            session.id,
            run_id=run.id,
            owner_token=run.owner_token,
            external_session_id=external,
            subpath="codex/checkpoint-v1",
            generation="",
            snapshot=snapshot(session.id),
        )
    assert (
        async_to_sync(store.load)(external, home_for(str(session.id)))["generation"]
        == generation
    )


@pytest.mark.asyncio
async def test_changed_registry_requires_fork_before_launch() -> None:
    """Missing plugins or changed signatures never reuse an old thread silently."""
    from apps.harness.engines.codex.engine import CodexEngine
    from apps.harness.tests.test_codex_core_safety import auth

    engine = CodexEngine(
        tools=default_tool_registry(),
        accessor=None,
        auth=auth("openrouter"),
        external_session_id=str(uuid.uuid4()),
        engine_state={"codex_tools": "old-plugin-schema-fingerprint"},
    )
    with pytest.raises(RuntimeError, match="signatures changed; fork"):
        await engine.run(
            "Continue",
            "build",
            "openrouter/test/model",
            "build",
            RunOptions(session_id=str(uuid.uuid4())),
        )


@pytest.mark.asyncio
async def test_pdf_workspace_read_is_authorized_and_no_follow(tmp_path) -> None:
    """Codex PDF references use the same checked workspace loader as images."""
    from apps.harness.tests.test_claude_media import MediaAccessor

    accessor = MediaAccessor(tmp_path)
    accessor.files["/workspace/local.pdf"] = pdf_bytes()
    authorized = []

    async def authorize(path: str) -> bool:
        authorized.append(path)
        return True

    async def unsafe_read(*args, **kwargs):
        raise AssertionError("ordinary read must not be used")

    # Existing fake's no-follow delegates to read_file; retain the bound fake
    # implementation explicitly so any accidental normal read fails this test.
    checked_read = accessor.read_file
    accessor.read_file_no_follow = checked_read
    accessor.read_file = unsafe_read
    content = await media.user_input(
        "Read @file:/workspace/local.pdf",
        accessor=accessor,
        opts=RunOptions(authorize_file_read=authorize),
        include_history=False,
    )
    assert authorized == ["/workspace/local.pdf"]
    assert len(accessor.reads) == 1
    assert "Hello PDF" in str(content)


@pytest.mark.asyncio
async def test_explore_never_exposes_duplicate_managed_bash() -> None:
    """Even a forced allowed name cannot execute duplicate managed shell tools."""
    from apps.harness.access.base import ExecResult

    class ReadAccessor:
        async def exec_wait(self, command, **kwargs):
            assert command == "ls /workspace"
            return ExecResult(exit_code=0, stdout="source.py\n", stderr="")

    invoker = ManagedToolInvoker(default_tool_registry(), ReadAccessor(), drop_event)
    result = await invoker.invoke(
        {
            "tool": "bash",
            "callId": "safe-shell",
            "arguments": {"command": "ls /workspace"},
        },
        agent=get_agent("explore"),
        mode="plan",
        opts=RunOptions(),
        model="model",
        allowed={"bash"},
    )
    assert result["success"] is False
    assert "not authorized" in str(result)


@pytest.mark.asyncio
async def test_large_checkpoint_restore_respects_runner_write_cap(tmp_path) -> None:
    """A valid >10 MiB serialized checkpoint restores through bounded file RPCs."""
    import hashlib

    from apps.harness.engines.codex.session_store import restore_snapshot
    from apps.harness.tests.test_codex_core import LocalAccessor

    accessor = LocalAccessor(tmp_path / "workspace")
    uploaded = []
    write = accessor.write_file

    async def bounded_write(path, content, mode=0o644):
        assert len(content) <= 4 * 1024 * 1024
        uploaded.append(len(content))
        await write(path, content, mode)

    accessor.write_file = bounded_write
    session_id = str(uuid.uuid4())
    home = home_for(session_id)
    payload = b'{"type":"event","text":"' + b"a" * (8 * 1024 * 1024) + b'"}\n'
    name = "sessions/2026/10/20/rollout-large.jsonl"
    record = {
        "version": 1,
        "home": home,
        "files": [
            {
                "path": name,
                "data": base64.b64encode(payload).decode(),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    await restore_snapshot(accessor, home, record)
    assert len(uploaded) == 3
    assert accessor.path(home + "/" + name).read_bytes() == payload
    assert not list(accessor.path(home).parent.glob("restore*"))
