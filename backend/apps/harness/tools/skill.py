"""Skill tool: load the instructions of a plugin skill on demand.

The system prompt lists the name and description of every skill of the
plugins enabled in the workspace; the agent loads a skill's Markdown with
this tool when a task matches (progressive disclosure). The catalog is
shared with the plugin reloader, so skills added mid-run are loadable at
once.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from apps.plugins.runtime_snapshot import SkillEntry

from .base import Tool, ToolContext, ToolError, ToolResult


class SkillCatalog:
    """The plugin skills an agent can load in one run (mutable)."""

    def __init__(self, entries: list[SkillEntry] | None = None) -> None:
        self._entries: dict[str, SkillEntry] = {}
        self.replace(entries or [])

    def replace(self, entries: list[SkillEntry]) -> tuple[list[str], list[str]]:
        """Swap in *entries*; return the added and removed identifiers."""
        before = set(self._entries)
        self._entries = {entry.identifier: entry for entry in entries}
        after = set(self._entries)
        return sorted(after - before), sorted(before - after)

    def get(self, identifier: str) -> SkillEntry | None:
        """Return the entry addressed by *identifier* (case-insensitive)."""
        wanted = (identifier or "").strip().lower()
        return self._entries.get(wanted) or next(
            (
                entry
                for entry in self._entries.values()
                if entry.name.strip().lower() == wanted
            ),
            None,
        )

    def entries(self) -> list[SkillEntry]:
        """All entries in catalog order."""
        return list(self._entries.values())

    def __bool__(self) -> bool:
        return bool(self._entries)


class SkillArgs(BaseModel):
    """Arguments for the skill tool."""

    name: str = Field(description="The name of a skill from the available skills list.")


class SkillTool(Tool):
    """Return the full instructions of one available plugin skill."""

    name = "skill"
    description = (
        "Load a skill: its specialized instructions for a task. Use it when the "
        "task at hand matches one of the available skills listed in the system "
        "prompt, before you start the task. The name must match an available "
        "skill."
    )
    args_schema: type[BaseModel] = SkillArgs
    permission_key = "skill"

    def __init__(self, catalog: SkillCatalog) -> None:
        self.catalog = catalog

    def title(self, args: BaseModel) -> str:
        """Return a short title for a skill invocation."""
        assert isinstance(args, SkillArgs)
        return f"Skill {args.name}"[:120]

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Return the skill's Markdown wrapped in a skill block."""
        validated = self.coerce_args(args)
        assert isinstance(validated, SkillArgs)
        entry = self.catalog.get(validated.name)
        if entry is None:
            available = ", ".join(e.identifier for e in self.catalog.entries())
            raise ToolError(
                f"Unknown skill '{validated.name}'. Available: {available or 'none'}",
                tool=self.name,
            )
        return ToolResult(
            output=(
                f'<skill name="{entry.identifier}" plugin="{entry.plugin_name}">\n'
                f"{entry.body.strip()}\n</skill>"
            ),
            metadata={"skill": entry.identifier, "plugin": entry.plugin_name},
        )
