"""ASGI transports for the MCP server.

Mounted by ``config/asgi.py`` at ``/mcp``: the primary streamable HTTP
endpoint (``/mcp``) and the legacy SSE endpoints (``/mcp/sse``,
``/mcp/messages/``). Both authenticate the API key
(``Authorization: Bearer kai_...`` or ``X-API-Key``) and serve one
:func:`~apps.mcp_app.server.create_mcp_server` instance per session.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from asgiref.sync import sync_to_async
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount

from .auth import authenticate_api_key
from .server import create_mcp_server


class _BufferedReceive:
    """Replay a buffered HTTP request body for downstream ASGI handlers."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._sent = False

    async def __call__(self) -> dict[str, object]:
        if not self._sent:
            self._sent = True
            return {
                "type": "http.request",
                "body": self._body,
                "more_body": False,
            }
        return {"type": "http.disconnect"}


def _make_buffered_receive(body: bytes):
    return _BufferedReceive(body)


def _load_json_body(body: bytes):
    """Decode a buffered JSON body once so session routing can inspect it."""
    try:
        return json.loads(body), None
    except json.JSONDecodeError as exc:
        return None, exc


class _StreamableHTTPSession:
    """Owns one MCP streamable HTTP session and its background server task."""

    def __init__(self, session_id: str, api_key) -> None:
        self.session_id = session_id
        self.api_key = api_key
        self.transport = StreamableHTTPServerTransport(
            session_id,
            is_json_response_enabled=True,
        )
        self.server = create_mcp_server(api_key)
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._run(),
            name=f"opencuria-mcp-{self.session_id}",
        )
        await self._ready.wait()

    async def _run(self) -> None:
        try:
            async with self.transport.connect() as streams:
                self._ready.set()
                await self.server.run(
                    streams[0],
                    streams[1],
                    self.server.create_initialization_options(),
                )
        finally:
            self._ready.set()

    def add_done_callback(self, callback) -> None:
        if self._task is not None:
            self._task.add_done_callback(callback)

    def is_finished(self) -> bool:
        return self._task is not None and self._task.done()


class _StreamableHTTPSessionManager:
    """Tracks active MCP streamable HTTP sessions for authenticated clients."""

    def __init__(self) -> None:
        self._sessions: dict[str, _StreamableHTTPSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, api_key) -> _StreamableHTTPSession:
        async with self._lock:
            session_id = uuid.uuid4().hex
            session = _StreamableHTTPSession(session_id=session_id, api_key=api_key)
            await session.start()
            self._sessions[session_id] = session
            session.add_done_callback(
                lambda _task: asyncio.create_task(
                    self._remove_if_current(session_id, session)
                )
            )
            return session

    async def get_session(
        self, session_id: str | None
    ) -> _StreamableHTTPSession | None:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.is_finished():
            await self._remove_if_current(session_id, session)
            return None
        return session

    async def _remove_if_current(
        self,
        session_id: str,
        session: _StreamableHTTPSession,
    ) -> None:
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is session:
                self._sessions.pop(session_id, None)


async def _authenticate_request(request: Request):
    token = request.headers.get("authorization", "").removeprefix(
        "Bearer "
    ).strip() or request.headers.get("x-api-key", "")

    return await sync_to_async(authenticate_api_key)(token)


def build_mcp_app() -> Starlette:
    """Build and return the Starlette ASGI app for MCP transports."""

    sse_transport = SseServerTransport("/mcp/messages/")
    session_manager = _StreamableHTTPSessionManager()

    async def handle_streamable_http(scope, receive, send) -> None:
        """Primary MCP endpoint implementing streamable HTTP on /mcp."""
        request = Request(scope, receive)
        api_key = await _authenticate_request(request)
        if api_key is None:
            response = JSONResponse(
                {
                    "error": (
                        "Invalid or missing API key. Ensure the key has the "
                        "mcp:access permission."
                    )
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        session_id = request.headers.get("mcp-session-id")
        request_receive = request.receive

        if request.method == "POST" and session_id is None:
            body = await request.body()
            request_receive = _make_buffered_receive(body)
            payload, parse_error = _load_json_body(body)

            if parse_error is not None:
                response = JSONResponse(
                    {"error": f"Invalid JSON-RPC body: {parse_error.msg}"},
                    status_code=400,
                )
                await response(scope, receive, send)
                return

            if not (
                isinstance(payload, dict)
                and payload.get("jsonrpc") == "2.0"
                and payload.get("method") == "initialize"
            ):
                response = JSONResponse(
                    {
                        "error": (
                            "Missing MCP session. Send initialize without "
                            "Mcp-Session-Id to start a new session."
                        )
                    },
                    status_code=400,
                )
                await response(scope, receive, send)
                return

            session = await session_manager.create_session(api_key)
        else:
            session = await session_manager.get_session(session_id)
            if session is None:
                if request.method == "GET" and session_id is None:
                    response = Response(status_code=405)
                    await response(scope, receive, send)
                    return

                response = JSONResponse(
                    {"error": "Invalid or expired MCP session."},
                    status_code=404 if session_id else 400,
                )
                await response(scope, receive, send)
                return

        await session.transport.handle_request(
            scope,
            request_receive,
            send,
        )

    async def handle_sse(scope, receive, send) -> None:
        """Legacy SSE endpoint kept for older MCP clients."""
        request = Request(scope, receive)
        api_key = await _authenticate_request(request)
        if api_key is None:
            response = JSONResponse(
                {
                    "error": "Invalid or missing API key. Ensure the key has the mcp:access permission."
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        mcp_server = create_mcp_server(api_key)

        async with sse_transport.connect_sse(scope, request.receive, send) as streams:
            await mcp_server.run(
                streams[0],
                streams[1],
                mcp_server.create_initialization_options(),
            )

    return Starlette(
        routes=[
            Mount("/mcp/sse", app=handle_sse),
            Mount("/mcp/messages/", app=sse_transport.handle_post_message),
            Mount("/mcp", app=handle_streamable_http),
        ],
    )


# Singleton — built lazily on first import from asgi.py
_mcp_app: Starlette | None = None


def get_mcp_app() -> Starlette:
    """Return (and lazily create) the singleton MCP Starlette application."""
    global _mcp_app
    if _mcp_app is None:
        _mcp_app = build_mcp_app()
    return _mcp_app
