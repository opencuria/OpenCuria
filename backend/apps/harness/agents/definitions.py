"""Static agent definitions for the harness (OpenCode-style, code not DB).

Agent knowledge lives in code instead of database records: each
:class:`AgentDefinition` pins a name, mode, permission rules, system
prompt, optional step budget, and an optional model override.

Permission design (OpenCode-like defaults):

- ``build`` (primary): ``{"*": "allow"}`` — full read/write/run access.
- ``plan`` (primary): edits ask for approval (``ask``, deliberately not
  ``deny`` so a planner can still apply a one-off fix after the user
  approves it). ``bash`` asks by default but allows a read-only
  allowlist without prompting: ``git status/log/diff/branch``, ``ls``,
  ``cat``, ``head``, ``tail``, ``pwd``, ``echo``. Everything else
  (``rm``, ``sudo``, build commands, …) goes through the ask gate.
- ``general`` (subagent): ``{"*": "allow"}`` plus ``question: deny``
  — delegated subtasks may use every tool (M5 wires up child sessions)
  but never prompt the user directly.
- ``explore`` (subagent, read-only): ``edit``/``write`` are ``deny``.
  ``bash`` is ``allow`` (OpenCode-like, so research commands such as
  ``find``/``rg`` do not block on a hidden child ask) with a
  last-match deny list for destructive commands (``rm``, ``sudo``,
  ``mv``, …). Pure research tools (``read``, ``glob``, ``grep``,
  ``list``, ``webfetch``) stay allowed; ``question`` is ``deny``.
- ``title`` / ``compaction`` (hidden): ``{"*": "deny"}`` so the loop
  offers no tools at all and answers text-only. ``title`` uses the
  ``"small"`` sentinel resolved to ``ProviderConfig.small_model`` at
  runtime; ``compaction`` uses the session model (see ``runner``).
- ``computeruse`` (subagent): ``{"*": "allow"}`` plus ``question: deny``
  (same rationale as ``general``; ``ask_user`` still pauses via its
  own desktop flow, not the ``question`` tool).
- Global (``permissions.evaluator.DEFAULT_GLOBAL_RULES``): ``read``
  stays ``allow`` except ``*.env`` / ``*.env.*`` which ``ask``
  (OpenCode parity); ``*.env.example`` stays ``allow``. Combined with
  deny > ask > allow precedence so agent ``* allow`` cannot override.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AgentMode = Literal["primary", "subagent", "hidden"]

#: Sentinel for ``model_override``: use the org's small/cheap model.
SMALL_MODEL = "small"

VALID_MODES: tuple[str, ...] = ("primary", "subagent", "hidden")

#: Read-only shell commands allowed by ``plan`` without prompting.
#: Matched with fnmatch against the full command line (``*`` matches
#: any suffix, including the empty string, so ``"ls*"`` allows ``ls``).
READ_ONLY_BASH_RULES: dict[str, str] = {
    "git status*": "allow",
    "git log*": "allow",
    "git diff*": "allow",
    "git branch*": "allow",
    "ls*": "allow",
    "cat *": "allow",
    "head *": "allow",
    "tail *": "allow",
    "pwd": "allow",
    "echo *": "allow",
}

#: ``plan`` bash rules: read-only commands allowed, rest asks.
#: NOTE: catch-all first — granular matching is last-match-wins, so
#: specific allows must come after ``"*"`` to take effect.
PLAN_BASH_RULES: dict[str, str] = {"*": "ask", **READ_ONLY_BASH_RULES}

#: ``explore`` bash rules: research commands allowed, destructive
#: denied. Order matters (last-match-wins): catch-all first, denies last.
EXPLORE_BASH_RULES: dict[str, str] = {
    "*": "allow",
    "rm *": "deny",
    "rmdir *": "deny",
    "sudo *": "deny",
    "mv *": "deny",
    "cp *": "deny",
    "chmod *": "deny",
    "chown *": "deny",
    "dd *": "deny",
}

#: Subagent/hidden agents never prompt the user directly: pending
#: question gates of child sessions surface on the parent composer.
QUESTION_DENY: dict[str, str] = {"question": "deny"}


@dataclass(frozen=True)
class AgentDefinition:
    """Static definition of one harness agent."""

    name: str
    mode: str
    description: str
    system_prompt: str
    permissions: dict[str, Any] = field(default_factory=dict)
    model_override: str | None = None
    steps: int | None = None
    color: str = "gray"

    def __post_init__(self) -> None:
        """Validate mode and step budget."""
        if self.mode not in VALID_MODES:
            raise ValueError(
                f"Invalid agent mode '{self.mode}' for '{self.name}'; "
                f"expected one of {VALID_MODES}"
            )
        if self.steps is not None and self.steps <= 0:
            raise ValueError(
                f"Agent '{self.name}' steps must be positive, got {self.steps}"
            )


AGENT_DEFINITIONS: dict[str, AgentDefinition] = {
    "build": AgentDefinition(
        name="build",
        mode="primary",
        description="Primary coding agent: full read/write/run access.",
        system_prompt=(
            "You are opencuria build, a senior software engineer working "
            "inside the workspace. Read the relevant code before changing "
            "it, make minimal focused edits, run the relevant checks, and "
            "summarize what you changed. Follow existing project "
            "conventions and keep responses concise."
        ),
        permissions={"*": "allow"},
        color="blue",
    ),
    "plan": AgentDefinition(
        name="plan",
        mode="primary",
        description=(
            "Primary planning agent: investigates and proposes plans; "
            "edits and mutating shell commands need approval."
        ),
        system_prompt=(
            "You are opencuria plan, a staff engineer scoping a task. "
            "Investigate the codebase with read-only tools (read, glob, "
            "grep, list, and harmless shell commands such as git "
            "status/log/diff, ls, cat). Do NOT edit files or run mutating "
            "commands without explicit user approval. End with a concise "
            "step-by-step plan and wait for confirmation."
        ),
        permissions={
            "*": "allow",
            "edit": "ask",
            "write": "ask",
            "process": "ask",
            "bash": dict(PLAN_BASH_RULES),
        },
        color="amber",
    ),
    "general": AgentDefinition(
        name="general",
        mode="subagent",
        description=(
            "General-purpose subagent for delegated subtasks. Use this "
            "agent to execute multiple units of work in parallel."
        ),
        system_prompt=(
            "You are opencuria general, a helpful subagent. Complete the "
            "delegated subtask and return a concise result with file paths "
            "and key facts. Keep tool use focused and avoid unrelated "
            "changes."
        ),
        permissions={"*": "allow", **QUESTION_DENY},
        color="cyan",
    ),
    "explore": AgentDefinition(
        name="explore",
        mode="subagent",
        description=(
            "Read-only research subagent: searches and summarizes, "
            "never mutates the workspace."
        ),
        system_prompt=(
            "You are opencuria explore, a read-only research subagent. Use "
            "read, glob, grep, list, and harmless shell commands only. "
            "Never edit or write files and never run mutating commands. "
            "Return file paths, matching snippets, and a short summary."
        ),
        permissions={
            "*": "allow",
            "edit": "deny",
            "write": "deny",
            "process": "deny",
            "bash": dict(EXPLORE_BASH_RULES),
            **QUESTION_DENY,
        },
        color="green",
    ),
    "title": AgentDefinition(
        name="title",
        mode="hidden",
        description="Hidden: generates short session titles (small model).",
        system_prompt=(
            "Generate a short title (at most 8 words) for the conversation. "
            "Reply with the title only, no tools, no extra text."
        ),
        permissions={"*": "deny", **QUESTION_DENY},
        model_override=SMALL_MODEL,
        color="gray",
    ),
    "compaction": AgentDefinition(
        name="compaction",
        mode="hidden",
        description="Hidden: summarizes context for compaction.",
        system_prompt=(
            "You are a context summarization agent. You are given a "
            "conversation between a user and an agent. Your goal is to "
            "produce a structured summary matching the format specified "
            "so another coding agent can continue the work.\n\n"
            "Always follow the exact output structure requested by the user "
            "prompt. Keep every section, preserve exact file paths and "
            "identifiers when known, and prefer terse bullets over "
            "paragraphs.\n\n"
            "Do not continue the conversation. Do not respond to any "
            "questions in the conversation. Only output the structured "
            "summary in the exact format requested by the user prompt. "
            "Respond in the same language as the conversation."
        ),
        permissions={"*": "deny", **QUESTION_DENY},
        color="magenta",
    ),
    "computeruse": AgentDefinition(
        name="computeruse",
        mode="subagent",
        description=(
            "Computer-use subagent for desktop automation via screen, mouse, "
            "and keyboard inside the workspace."
        ),
        system_prompt=(
            "You are opencuria computeruse, a careful and efficient "
            "computer-use assistant running on the workspace desktop.\n\n"
            "Operating principles:\n"
            "- Follow the delegated subtask exactly. Do not expand its scope, "
            "make unrelated improvements, or interact with unrelated "
            "applications.\n"
            "- Prefer the smallest number of deliberate actions that can "
            "complete the request.\n"
            "- Never repeat an action that the verification image shows has "
            "already succeeded. If the same approach fails twice, inspect more "
            "closely with view_region or ask_user instead of looping.\n"
            "- Do not bundle actions when a later action depends on the visual "
            "result of an earlier one. Inspect the returned verification frame "
            "first.\n"
            "- Treat approval for one action as approval only for that action.\n\n"
            "Desktop operation:\n"
            "- Every returned screen image uses a normalized 1000x1000 "
            "coordinate grid, regardless of actual pixel dimensions.\n"
            "- Use normalized x/y values from 0 through 1000 relative to the "
            "most recent returned image: top-left=(0,0), center=(500,500), "
            "bottom-right=(1000,1000). Aim at the visual center of the "
            "intended control.\n"
            "- Use view_region to zoom into a small, crowded, or ambiguous "
            "target before clicking it.\n"
            "- Mouse and keyboard actions return a fresh verification "
            "screenshot. Inspect it before continuing and do not reuse old "
            "coordinates after the UI or returned image changes.\n"
            "- Session video is recorded automatically by the harness; you do "
            "not need to start or stop recording.\n"
            "- Never claim an action succeeded unless the tool result or "
            "verification frame confirms it.\n"
            "- Use ask_user to pause and ask one short, specific question "
            "whenever missing information could change the target or result. "
            "Do this before taking actions based on a guess.\n"
            "- Keep the final response short and say what was actually "
            "completed."
        ),
        permissions={"*": "allow", **QUESTION_DENY},
        color="purple",
    ),
}


def get_agent(name: str) -> AgentDefinition:
    """Return the agent definition for *name*.

    Raises:
        KeyError: If no agent called *name* is defined.
    """
    try:
        return AGENT_DEFINITIONS[name.strip().lower()]
    except KeyError as exc:
        known = ", ".join(sorted(AGENT_DEFINITIONS))
        raise KeyError(f"Unknown agent '{name}' (known: {known})") from exc


def list_agents(*, mode: str | None = None) -> list[AgentDefinition]:
    """Return all agent definitions, optionally filtered by *mode*."""
    agents = list(AGENT_DEFINITIONS.values())
    if mode is None:
        return agents
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid agent mode '{mode}'; expected {VALID_MODES}")
    return [agent for agent in agents if agent.mode == mode]


def subagent_descriptions() -> dict[str, str]:
    """Return name -> description for subagent-mode agents (task tool)."""
    return {agent.name: agent.description for agent in list_agents(mode="subagent")}
