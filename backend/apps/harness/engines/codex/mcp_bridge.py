"""Native Codex MCP exposure with OpenCuria's ordinary authorized dispatcher.

The workspace relay only transports bounded JSON lines over a private Unix
socket. Discovery, tool execution, permissions and lifecycle remain backend-owned.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ...access.base import WorkspaceAccessor

MAX_LINE = 8 * 1024 * 1024

# A disposable workspace process, owned by the same runner lease as Codex.
# No project file, listening network port or provider credential is required.
RELAY_SERVER = r"""
import asyncio,json,os,shutil,sys,tempfile,uuid
async def main():
 directory=tempfile.mkdtemp(prefix="opencuria-mcp-")
 path=directory+"/rpc"
 pending={}
 clients=set()
 def emit(value):
  sys.stdout.write(json.dumps(value)+"\n");sys.stdout.flush()
 async def accept(reader,writer):
  clients.add(writer)
  try:
   while line:=await reader.readline():
    request=json.loads(line)
    key=uuid.uuid4().hex
    future=asyncio.get_running_loop().create_future()
    pending[key]=future
    emit({"id":key,"request":request})
    try:
     response=await future
     if response is not None:
      writer.write((json.dumps(response)+"\n").encode());await writer.drain()
    finally:pending.pop(key,None)
  finally:
   clients.discard(writer);writer.close();await writer.wait_closed()
 server=await asyncio.start_unix_server(accept,path,limit=8388608)
 emit({"ready":path})
 try:
  while line:=await asyncio.to_thread(sys.stdin.buffer.readline,8388609):
   if len(line)>8388608:raise ValueError("oversized response")
   reply=json.loads(line)
   future=pending.get(reply["id"])
   if future is not None and not future.done():future.set_result(reply.get("response"))
 finally:
  server.close();await server.wait_closed()
  for writer in clients:writer.close()
  for future in pending.values():future.cancel()
  shutil.rmtree(directory)
asyncio.run(main())
"""

RELAY_CLIENT = r"""
import os,socket,sys,threading
connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
connection.connect(sys.argv[1])
def send():
 try:
  while data:=os.read(0,65536):connection.sendall(data)
 finally:
  try:connection.shutdown(socket.SHUT_WR)
  except OSError:pass
threading.Thread(target=send,daemon=True).start()
while data:=connection.recv(65536):
 sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
"""


class ManagedMcpBridge:
    """Expose one managed server directly, retaining all dispatch gates."""

    def __init__(
        self,
        schemas: list[dict[str, Any]],
        invoke: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        names: dict[str, str] | None = None,
    ) -> None:
        self.schemas = schemas
        self.invoke = invoke
        self.names = names or {item["name"]: item["name"] for item in schemas}
        self.stream = None
        self.task: asyncio.Task | None = None
        self.tail = bytearray()

    async def _receive(self) -> dict[str, Any]:
        """Read exactly one bounded relay message without losing buffered data."""
        while b"\n" not in self.tail:
            chunk = await self.stream.receive()
            if not chunk:
                raise RuntimeError("Managed MCP relay disconnected")
            self.tail.extend(chunk)
            if len(self.tail) > MAX_LINE:
                raise ValueError("Managed MCP relay response exceeds limit")
        line, _, remainder = self.tail.partition(b"\n")
        self.tail[:] = remainder
        return json.loads(line)

    async def start(
        self, accessor: WorkspaceAccessor, cwd: str, owner: dict[str, Any]
    ) -> dict[str, Any]:
        """Start the owned relay and return the native Codex MCP configuration."""
        self.stream = await accessor.open_process(
            ["/usr/bin/python3", "-u", "-c", RELAY_SERVER], cwd, {}, owner=owner
        )
        ready = await asyncio.wait_for(self._receive(), 10)
        path = ready.get("ready", "")
        if not isinstance(path, str) or not path.startswith("/tmp/opencuria-mcp-"):
            raise ValueError("Managed MCP relay returned an invalid socket path")
        self.task = asyncio.create_task(self._serve())
        return {
            "command": "/usr/bin/python3",
            "args": ["-u", "-c", RELAY_CLIENT, path],
            "omit_tools_from": ["code_mode", "deferred"],
            "startup_timeout_sec": 15,
            "tool_timeout_sec": 2_147_500,
            # The shared OpenCuria dispatcher owns approvals, including plan mode.
            "default_tools_approval_mode": "approve",
            "tools": {
                item["name"]: {"approval_mode": "approve", "output_token_limit": 25000}
                for item in self.schemas
            },
        }

    async def _serve(self) -> None:
        """Process discovery and invoke requests under the backend's live policy."""
        while True:
            envelope = await self._receive()
            request = envelope["request"]
            identifier = request.get("id")
            response = None
            if identifier is not None:
                try:
                    result = await self._request(request)
                    response = {"jsonrpc": "2.0", "id": identifier, "result": result}
                except asyncio.CancelledError:
                    raise
                except Exception:
                    response = {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "error": {
                            "code": -32603,
                            "message": "Managed MCP request failed",
                        },
                    }
            data = (
                json.dumps({"id": envelope["id"], "response": response}) + "\n"
            ).encode()
            if len(data) > MAX_LINE:
                raise ValueError("Managed MCP result exceeds limit")
            for offset in range(0, len(data), 64 * 1024):
                await self.stream.send(data[offset : offset + 64 * 1024])

    async def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Keep the native MCP surface small; never expose internal tools."""
        method = request.get("method")
        if method == "initialize":
            return {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "opencuria-managed-tools", "version": "1"},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.schemas}
        if method == "tools/call":
            params = request.get("params") or {}
            if params.get("name") not in {item["name"] for item in self.schemas}:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": "Unknown tool"}],
                }
            return await self.invoke(
                {
                    "tool": self.names[params["name"]],
                    "arguments": params.get("arguments", {}),
                    "callId": "mcp_" + uuid.uuid4().hex,
                }
            )
        raise ValueError("Unsupported managed MCP method")

    async def close(self) -> None:
        """Cancel pending dispatch before closing the runner-owned relay stream."""
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.stream:
            try:
                await self.stream.send_eof()
                await asyncio.wait_for(self.stream.wait_closed(), 3)
            finally:
                await asyncio.wait_for(self.stream.aclose(), 10)
