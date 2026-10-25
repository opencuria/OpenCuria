"""Stateless Streamable HTTP endpoint for the management MCP server (``/mcp``).

Every POST is authenticated on its own with an API key
(``Authorization: Bearer kai_…`` or ``X-API-Key``) that holds ``mcp:access``.
``X-Organization-Id`` optionally selects the organization; otherwise the
user's first organization is used. The server keeps no session state, so
revoked keys stop working immediately and any worker can serve any request.
GET (server-initiated streams) and DELETE (session teardown) are not offered.
"""

from __future__ import annotations

import anyio
from anyio.abc import TaskStatus
from asgiref.sync import sync_to_async
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from apps.accounts.models import APIKeyPermission
from apps.accounts.services import APIKeyService

from .context import ContextError, ToolContext, resolve_context
from .server import create_mcp_server

MCP_PATH = "/mcp"


def _bearer_token(request: Request) -> str:
    """Return the API key from ``Authorization`` or ``X-API-Key``."""
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _authenticate(token: str, requested_org_id: str | None) -> ToolContext | Response:
    """Return the caller context or the HTTP error response to send."""
    api_key = APIKeyService().authenticate(token)
    if api_key is None:
        return JSONResponse(
            {"error": "Invalid or missing API key."},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not api_key.has_permission(APIKeyPermission.MCP_ACCESS):
        return JSONResponse(
            {"error": "This API key lacks the mcp:access permission."},
            status_code=403,
        )
    try:
        return resolve_context(api_key, requested_org_id)
    except ContextError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)


async def _serve(ctx: ToolContext, scope: Scope, receive: Receive, send: Send) -> None:
    """Answer one JSON-RPC POST with a fresh, sessionless server instance."""
    server = create_mcp_server(ctx)
    transport = StreamableHTTPServerTransport(
        mcp_session_id=None, is_json_response_enabled=True
    )

    async def run_server(*, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED):
        async with transport.connect() as (read_stream, write_stream):
            task_status.started()
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
                stateless=True,
            )

    async with anyio.create_task_group() as task_group:
        await task_group.start(run_server)
        try:
            await transport.handle_request(scope, receive, send)
        finally:
            await transport.terminate()


async def mcp_asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
    """ASGI entry point mounted by ``config/asgi.py`` at ``/mcp``."""
    if scope["type"] != "http":
        return
    request = Request(scope, receive)
    if request.method != "POST":
        response = Response(status_code=405, headers={"Allow": "POST"})
        await response(scope, receive, send)
        return
    outcome = await sync_to_async(_authenticate)(
        _bearer_token(request), request.headers.get("x-organization-id")
    )
    if isinstance(outcome, Response):
        await outcome(scope, receive, send)
        return
    await _serve(outcome, scope, receive, send)
