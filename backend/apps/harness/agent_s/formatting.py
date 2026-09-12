"""Format checkers: SINGLE_ACTION / CODE_VALID (with double-materialization).

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.

Reference (``gui_agents/s3/utils/formatters.py``):

- ``SINGLE_ACTION`` passes iff exactly one ``agent.xxx(...)`` call is found
  in the last fenced code block (greedy ``extract_agent_functions`` regex).
- ``CODE_VALID`` *materializes* the action (``eval`` in Agent-S, injected
  async materializer here) and passes iff materialization succeeds. Because
  the accepted plan is materialized again afterwards, grounding/notes/code
  side effects may happen twice — this quirk is preserved intentionally.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from . import parsing as _parsing
from . import prompts as _prompts
from .actions_parser import ActionParseError, ParsedAction, parse_action_line


def single_action_check(response: str) -> tuple[bool, str]:
    ok = (
        len(_parsing.extract_agent_functions(_parsing.parse_code_from_string(response)))
        == 1
    )
    return ok, _prompts.SINGLE_ACTION_ERROR_MSG


async def code_valid_check_async(
    response: str,
    materialize: Callable[[ParsedAction], Awaitable[Any]],
) -> tuple[bool, str]:
    """Async CODE_VALID check; materializes once (quirk preserved)."""
    try:
        action = parse_action_line(_parsing.parse_code_from_string(response))
    except ActionParseError:
        return False, _prompts.CODE_VALID_ERROR_MSG
    try:
        await materialize(action)
    except Exception:
        return False, _prompts.CODE_VALID_ERROR_MSG
    return True, _prompts.CODE_VALID_ERROR_MSG


def make_format_checkers(
    materialize,
) -> list:
    """Return ``[SINGLE_ACTION, CODE_VALID]`` checkers in Agent-S order."""

    async def _code_valid(response: str) -> tuple[bool, str]:
        return await code_valid_check_async(response, materialize)

    return [single_action_check, _code_valid]
