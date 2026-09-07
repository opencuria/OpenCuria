"""System-prompt composer for the agent harness.

Assembles the system prompt from the agent definition, project context
files (``AGENTS.md`` walk-up with ``CLAUDE.md`` fallback), environment
facts (date, working directory, mode), the available tools, and the
subagent descriptions used by the ``task`` tool.

Truncation limits (documented here so callers can reason about them):

- ``MAX_CONTEXT_FILES`` (3): at most this many context files are
  inlined, nearest-first.
- ``MAX_CONTEXT_FILE_BYTES`` (32 KiB): per-file read cap passed to the
  accessor (files above it are skipped, not partially inlined).
- ``MAX_CONTEXT_TOTAL_CHARS`` (64 KiB): total inlined context budget;
  the walk stops once the budget is exhausted.

Skills/references hook: :func:`compose_system_prompt` accepts
``skills`` and ``references`` lists (default empty). They are an
explicit extension point for M5/M7 (skill loading, ``@file`` mentions);
today they are only rendered as a section when non-empty. No loading
logic lives here on purpose.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

from ..access.base import HARNESS_WORKSPACE_ROOT
from ..agents.definitions import AgentDefinition

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for typing only
    from ..access.base import WorkspaceAccessor
    from ..providers.base import ToolSchema

log = structlog.get_logger(__name__)

#: Max number of context files inlined into the system prompt.
MAX_CONTEXT_FILES = 3

#: Per-file read cap for context files (bytes).
MAX_CONTEXT_FILE_BYTES = 32 * 1024

#: Total inlined context budget (chars).
MAX_CONTEXT_TOTAL_CHARS = 64 * 1024

_CONTEXT_FILENAMES = ("AGENTS.md", "CLAUDE.md")


@dataclass
class LoadedContextFile:
    """One project context file inlined into the system prompt."""

    path: str
    content: str
    truncated: bool = False


@dataclass
class ComposedPrompt:
    """Result of composing a system prompt."""

    system: str
    sources: list[str] = field(default_factory=list)
    truncated: bool = False


def _walk_up_directories(cwd: str) -> list[str]:
    """Return *cwd* and each ancestor down to the workspace root."""
    normalized = (cwd or HARNESS_WORKSPACE_ROOT).rstrip("/") or "/"
    if not normalized.startswith("/"):
        normalized = f"{HARNESS_WORKSPACE_ROOT}/{normalized}"
    chain: list[str] = [normalized]
    while normalized not in ("", "/") and normalized != HARNESS_WORKSPACE_ROOT:
        parent = normalized.rsplit("/", 1)[0] or "/"
        chain.append(parent)
        normalized = parent
        if len(chain) > 32:  # pragma: no cover - pathological depth guard
            break
    if HARNESS_WORKSPACE_ROOT not in chain:
        chain.append(HARNESS_WORKSPACE_ROOT)
    return chain


async def load_project_context(
    accessor: WorkspaceAccessor | None,
    cwd: str,
) -> tuple[list[LoadedContextFile], bool]:
    """Walk up from *cwd*, loading ``AGENTS.md`` (fallback ``CLAUDE.md``).

    At each directory (nearest first) ``AGENTS.md`` is tried before
    ``CLAUDE.md``; the first hit per directory level wins and the walk
    stops after :data:`MAX_CONTEXT_FILES` files or when the total char
    budget is exhausted. Accessor errors (missing files, runner
    failures, oversized files) are swallowed per file and logged — a
    broken context file must never break the run.

    Returns:
        A ``(files, truncated)`` pair; ``truncated`` is True when a
        limit or an error cut the context short.
    """
    if accessor is None:
        return [], False
    found: list[LoadedContextFile] = []
    truncated = False
    total_chars = 0
    for directory in _walk_up_directories(cwd):
        if len(found) >= MAX_CONTEXT_FILES:
            truncated = True
            break
        for filename in _CONTEXT_FILENAMES:
            candidate = f"{directory.rstrip('/')}/{filename}"
            try:
                stored = await accessor.read_file(
                    candidate, max_size=MAX_CONTEXT_FILE_BYTES
                )
            except Exception as exc:
                log.debug("context_file_unreadable", path=candidate, error=str(exc))
                continue
            if getattr(stored, "truncated", False):
                log.debug("context_file_too_large", path=candidate)
                truncated = True
                continue
            try:
                text = stored.content.decode("utf-8")
            except UnicodeDecodeError:
                log.debug("context_file_not_utf8", path=candidate)
                continue
            if total_chars + len(text) > MAX_CONTEXT_TOTAL_CHARS:
                log.debug("context_budget_exhausted", path=candidate)
                truncated = True
                break
            found.append(LoadedContextFile(path=candidate, content=text))
            total_chars += len(text)
            break  # first hit per directory wins
    return found, truncated


async def compose_system_prompt(
    *,
    agent: AgentDefinition,
    mode: str,
    tools: list[ToolSchema] | None = None,
    subagents: dict[str, str] | None = None,
    accessor: WorkspaceAccessor | None = None,
    cwd: str = HARNESS_WORKSPACE_ROOT,
    now: datetime | None = None,
    skills: list[str] | None = None,
    references: list[str] | None = None,
) -> ComposedPrompt:
    """Compose the full system prompt for an agent run.

    Args:
        agent: Static agent definition (prompt + permissions).
        mode: Active mode (``plan`` or ``build``).
        tools: Filtered tool schemas offered to the model.
        subagents: Name -> description for the ``task`` tool.
        accessor: Workspace accessor for the ``AGENTS.md`` walk-up
            (None skips project context).
        cwd: Working directory the walk-up starts from.
        now: Timestamp for the environment section (defaults to UTC).
        skills: Extension point (M5/M7): skill names/blobs to append.
        references: Extension point (M5/M7): ``@file``-style references.

    Returns:
        The composed prompt plus loaded sources and a truncated flag.
    """
    if mode not in ("plan", "build"):
        raise ValueError(f"Invalid mode '{mode}'; expected plan|build")
    moment = now or datetime.now(timezone.utc)
    sections: list[str] = [agent.system_prompt.strip()]
    sources: list[str] = []
    truncated = False

    context_files, context_truncated = await load_project_context(accessor, cwd)
    truncated = truncated or context_truncated
    if context_files:
        lines = ["# Project context"]
        for loaded in context_files:
            sources.append(loaded.path)
            lines.append(f"## {loaded.path}\n{loaded.content.strip()}")
        sections.append("\n\n".join(lines))

    env_lines = [
        "# Environment",
        f"Date: {moment.strftime('%Y-%m-%d')}",
        f"Platform: {platform.system()} {platform.machine()}",
        f"Workspace root: {HARNESS_WORKSPACE_ROOT}",
        f"Working directory: {cwd}",
        f"Mode: {mode} (plan = propose, do not mutate without approval; "
        "build = implement Hunted changes directly)",
    ]
    if mode == "plan":
        env_lines.append(
            "Plan mode: investigate read-only; "
            "ask before edits/mutating commands."
        )
    sections.append("\n".join(env_lines))

    if tools:
        tool_lines = ["# Available tools"]
        for schema in tools:
            tool_lines.append(f"- {schema.name}: {schema.description}")
        if any(schema.name == "process_start" for schema in tools):
            tool_lines.append(
                "Background servers via process_start; read logs from the "
                "returned log_path."
            )
        tool_lines.append(
            "Independent tool calls in one assistant message run in parallel. "
            "Issue multiple tool calls in a single step for independent work; "
            "wait for results before dependent follow-ups."
        )
        sections.append("\n".join(tool_lines))

    if subagents:
        sub_lines = [
            "# Subagents (via the task tool)",
            *[
                f"- {name}: {description}"
                for name, description in sorted(subagents.items())
            ],
            "Launch independent subagents in one message with multiple task "
            "tool calls so they run in parallel.",
        ]
        sections.append("\n".join(sub_lines))

    skills = skills or []
    references = references or []
    if skills:
        sections.append("# Skills\n" + "\n".join(f"- {skill}" for skill in skills))
    if references:
        sections.append(
            "# References\n" + "\n".join(f"- {ref}" for ref in references)
        )

    return ComposedPrompt(system="\n\n".join(sections), sources=sources,
                           truncated=truncated)


def describe_tools_for_prompt(tools: list[ToolSchema] | None) -> list[str]:
    """Return one-line tool summaries (helper for tests/callers)."""
    return [f"{schema.name}: {schema.description}" for schema in (tools or [])]


__all__: list[str] | Any = [
    "MAX_CONTEXT_FILES",
    "MAX_CONTEXT_FILE_BYTES",
    "MAX_CONTEXT_TOTAL_CHARS",
    "ComposedPrompt",
    "LoadedContextFile",
    "compose_system_prompt",
    "describe_tools_for_prompt",
    "load_project_context",
]
