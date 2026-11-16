"""Product context shared by the OpenCuria, Codex, and Claude harnesses.

Keep coding behavior, role policy, tools, project instructions, and skills in
their owning harness. This module only describes the OpenCuria environment
and chat surface; it does not grant permissions.
"""

from __future__ import annotations

from ..access.guest import HARNESS_WORKSPACE_ROOT, GuestPlatform
from ..agents.definitions import AgentDefinition

WORKSPACE_POLICY = (
    "Preserve existing user work. "
    "Workspace isolation does not extend to external or production services."
)

WORKSPACE_SETUP_INSTRUCTIONS = (
    "When permitted by the current task, mode, and permissions, carry out "
    "routine workspace-local setup and verification yourself, including "
    "starting development servers, installing dependencies, running migrations "
    "against workspace-local test data, and testing relevant flows in the "
    "running app."
)

CHAT_MEDIA_INSTRUCTIONS = (
    "OpenCuria chat renders workspace images and videos inline using Markdown "
    "image syntax: "
    f"![Screenshot]({HARNESS_WORKSPACE_ROOT}/screenshots/result.png) or "
    f"![Recording]({HARNESS_WORKSPACE_ROOT}/recordings/demo.mp4). "
    "Use ordinary Markdown links for other workspace files, e.g. "
    f"[Report]({HARNESS_WORKSPACE_ROOT}/reports/report.pdf). "
    f"Relative chat paths resolve from {HARNESS_WORKSPACE_ROOT}, regardless of "
    "the shell's working directory. Keep artifacts intended for sharing under "
    f"{HARNESS_WORKSPACE_ROOT}. Embed relevant images or recordings when they "
    "help explain the result."
)


def opencuria_context(agent: AgentDefinition, guest: GuestPlatform) -> str:
    """Return product context, omitting setup guidance for read-only helpers.

    The environment sentences come from the workspace's guest OS.
    """
    if agent.mode == "hidden":
        return ""
    sections = [f"{guest.prompt_context} {WORKSPACE_POLICY}"]
    if agent.name != "explore":
        sections.append(WORKSPACE_SETUP_INSTRUCTIONS)
    sections.append(CHAT_MEDIA_INSTRUCTIONS)
    return "\n\n".join(sections)
