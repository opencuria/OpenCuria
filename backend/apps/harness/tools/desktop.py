"""Backend-owned desktop lifecycle tool exposed by the Computer Use plugin."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..agent_desktop import AgentDesktop
from .base import Tool, ToolContext, ToolError, ToolResult


class DesktopArgs(BaseModel):
    """Explicit actions are idempotent; a toggle would be unsafe to retry."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "stop", "status"] = Field(
        description="Start or release this agent's desktop claim, or inspect status."
    )


class DesktopTool(Tool):
    """Use the same run claim as managed Computer Use and headed Playwright."""

    name = "mcp_computer-use_cua_repl_desktop"
    permission_key = name
    original_name = "desktop"
    native_mcp_server = "opencuria_computer_use_cua_repl"
    omit_tools_from = ("code_mode", "deferred")
    execution_tool = True
    annotations = None
    args_schema = DesktopArgs
    description = (
        "Manage this agent's workspace desktop: start, stop, or status. "
        "Start waits for readiness and acquires your own claim even if another "
        "agent or viewer already keeps the display running. Computer Use and "
        "managed headed Playwright share this claim. Before launching a GUI "
        "program with Bash, call start; DISPLAY and XAUTHORITY are already "
        "configured for agent shell commands. Stop releases only your claim; "
        "the desktop stays on while others hold it. Actually stopping the "
        "display closes its GUI applications. Status reports active, own_claim, "
        "own_claim_state, held_by_others, other_claim_count, and needs_start. "
        "If needs_start is true, call start before relying on the display. "
        "The claim ends automatically when this agent run finishes."
    )

    def __init__(self, desktop: AgentDesktop) -> None:
        self.desktop = desktop

    def title(self, args: BaseModel) -> str:
        """Describe the concrete action for approvals and the timeline."""
        return f"Desktop: {getattr(args, 'action', 'status')}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Operate on the bound run identity, never caller-supplied lease IDs."""
        validated = self.coerce_args(args)
        assert isinstance(validated, DesktopArgs)
        try:
            result = await self.desktop.action(validated.action)
        except Exception as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        return ToolResult(output=json.dumps(result), metadata=result)
