"""Minimal OpenCuria identity appended to otherwise native CLI instructions."""

from __future__ import annotations

from ..agents.definitions import AgentDefinition

# Keep this wording shared with the OpenCuria harness, not a second policy.
PROACTIVE_WORKSPACE_INSTRUCTIONS = (
    "In this disposable environment, take initiative: start/restart "
    "development servers, install missing packages, run migrations "
    "on workspace-local test data, and test relevant flows in the "
    "running app. "
    "Do routine local setup and testing yourself, without "
    "unnecessary confirmation. Investigate failures yourself; "
    "preserve existing user work."
)


def native_prompt_extension(agent: AgentDefinition) -> str:
    """Leave tools, project context and execution policy to the native harness."""
    identity = f"You are opencuria {agent.name}, working inside an OpenCuria workspace."
    if agent.name == "explore":
        return identity
    return f"{identity}\n\n{PROACTIVE_WORKSPACE_INSTRUCTIONS}"
