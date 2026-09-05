"""Static agent definitions package."""

from .definitions import (
    AGENT_DEFINITIONS,
    EXPLORE_BASH_RULES,
    PLAN_BASH_RULES,
    READ_ONLY_BASH_RULES,
    SMALL_MODEL,
    VALID_MODES,
    AgentDefinition,
    AgentMode,
    get_agent,
    list_agents,
    subagent_descriptions,
)

__all__ = [
    "AGENT_DEFINITIONS",
    "EXPLORE_BASH_RULES",
    "PLAN_BASH_RULES",
    "READ_ONLY_BASH_RULES",
    "SMALL_MODEL",
    "VALID_MODES",
    "AgentDefinition",
    "AgentMode",
    "get_agent",
    "list_agents",
    "subagent_descriptions",
]
