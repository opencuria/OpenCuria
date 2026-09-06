"""Permission evaluation after OpenCode semantics.

Rules map a tool name (or ``*`` for all tools) to a decision
(``allow``/``ask``/``deny``). Granular per-tool rules use nested dicts
whose keys are path/command patterns::

    {"*": "ask", "bash": "allow", "edit": {"src/**": "allow", "*": "ask"}}

Matching rules:

- ``*`` matches any sequence, ``?`` matches a single character.
- The action string for path-scoped tools is the target path; for
  command-scoped tools (``bash``) it is the command line.
- LAST-MATCH-WINS within one rule dict (insertion order).
- Merge order across layers: global -> agent -> mode. ``deny``
  in any layer beats a merged allow; ``ask`` beats ``allow``
  (see ``_combine_decisions`` in ``runner``).
- Default when nothing matches: ``allow``, except the reserved keys
  ``external_directory`` and ``doom_loop`` which default to ``ask``.
- Filename-only secret matching: granular ``read`` rules are matched
  against both the full action path and its basename, so ``*.env``
  fires for ``/workspace/.env`` (OpenCode parity).

Phase 3 decision (WIRE, not remove): ``HarnessRunner._decide`` and
``_filtered_schemas`` forward ``mode`` to :meth:`evaluate`, so
``mode_rules`` take effect wherever the runner is used. Production
wiring currently passes ``mode_rules=None`` (no per-mode overrides),
which is a documented no-op; the constructor signature stays stable
so future per-mode rules need no signature change.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

log = structlog.get_logger(__name__)

Decision = Literal["allow", "ask", "deny"]

ALLOW: Decision = "allow"
ASK: Decision = "ask"
DENY: Decision = "deny"

VALID_DECISIONS = (ALLOW, ASK, DENY)

#: Reserved permission keys with an ``ask`` default instead of ``allow``.
ASK_BY_DEFAULT_KEYS = ("external_directory", "doom_loop")

#: Global baseline rules (OpenCode parity): secret files ask for
#: approval while everything else reads freely. Order matters
#: (last-match-wins): the ``*.env.example`` allow must come after the
#: ``*.env``/``*.env.*`` asks so the shipped example stays readable.
#: Agent layers with ``{"*": "allow"}`` (build/general) do NOT override
#: this: the runner combines the base and agent evaluators with
#: deny > ask > allow precedence, so a base ``ask`` always survives.
DEFAULT_GLOBAL_RULES: dict[str, Any] = {
    "read": {
        "*": "allow",
        "*.env": "ask",
        "*.env.*": "ask",
        "*.env.example": "allow",
    }
}

# Tools whose action string is a workspace path (pattern-matched).
PATH_SCOPED_KEYS = ("read", "edit", "glob", "grep", "list")


@dataclass
class PermissionRule:
    """One compiled rule: tool pattern -> decision or granular mapping."""

    tool_pattern: str
    decision: Decision | dict[str, Decision]
    order: int


@dataclass
class Evaluation:
    """Outcome of evaluating one tool invocation."""

    decision: Decision
    matched_rule: str | None = None
    matched_pattern: str | None = None
    layer: str | None = None


@dataclass
class PermissionContext:
    """Inputs the loop passes to the evaluator for one tool call."""

    tool: str
    action: str = ""
    external_directory: bool = False
    doom_loop: bool = False
    mode: str = ""
    agent_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _normalize_pattern(pattern: str) -> str:
    """Normalize a match pattern for fnmatch (case-sensitive)."""
    return pattern.strip()


def _matches(pattern: str, value: str) -> bool:
    """Return True when fnmatch *pattern* matches *value*.

    ``**`` behaves like ``*`` (matches across ``/`` too, since
    fnmatch already treats ``/`` as an ordinary character).
    """
    normalized = _normalize_pattern(pattern)
    if normalized == "*":
        return True
    return fnmatch.fnmatchcase(value, normalized)


def _validate_decision(decision: str, *, where: str) -> Decision:
    """Validate a decision string."""
    if decision not in VALID_DECISIONS:
        raise ValueError(f"Invalid permission '{decision}' in {where}")
    return decision  # type: ignore[return-value]


def compile_rules(rules: dict[str, Any] | None) -> list[PermissionRule]:
    """Compile a raw rule dict into ordered PermissionRule entries."""
    compiled: list[PermissionRule] = []
    for order, (tool_pattern, raw) in enumerate((rules or {}).items()):
        if isinstance(raw, dict):
            granular = {
                pattern: _validate_decision(
                    decision, where=f"rule '{tool_pattern}/{pattern}'"
                )
                for pattern, decision in raw.items()
            }
            compiled.append(PermissionRule(tool_pattern, granular, order))
        else:
            compiled.append(
                PermissionRule(
                    tool_pattern,
                    _validate_decision(raw, where=f"rule '{tool_pattern}'"),
                    order,
                )
            )
    return compiled


class PermissionEvaluator:
    """Evaluate tool permissions across global/agent/mode layers.

    Phase 3 decision: mode layer is WIRED (not removed). Pass the
    active run mode via ``mode`` (``"plan"``/``"build"``) so the
    matching ``mode_rules`` slice applies. ``mode_rules=None`` (the
    production default) is a documented no-op.

    Args:
        global_rules: Baseline org/workspace rules.
        agent_rules: Per-agent overrides (M4 supplies definitions).
        mode_rules: Per-mode overrides keyed by mode name; pass the
            active mode via ``mode`` so only that slice applies.
    """

    def __init__(
        self,
        global_rules: dict[str, Any] | None = None,
        agent_rules: dict[str, Any] | None = None,
        mode_rules: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._global = compile_rules(global_rules)
        self._agent = compile_rules(agent_rules)
        self._modes = {
            mode: compile_rules(rules) for mode, rules in (mode_rules or {}).items()
        }

    def evaluate(
        self,
        tool: str,
        action: str = "",
        *,
        mode: str = "",
        external_directory: bool = False,
        doom_loop: bool = False,
    ) -> Decision:
        """Evaluate (*tool*, *action*) and return allow/ask/deny."""
        ctx = PermissionContext(
            tool=tool,
            action=action or "",
            mode=mode,
            external_directory=external_directory,
            doom_loop=doom_loop,
        )
        return self.evaluate_context(ctx).decision

    def evaluate_context(self, ctx: PermissionContext) -> Evaluation:
        """Evaluate a full PermissionContext with reserved-key handling."""
        if ctx.doom_loop:
            decision = self._lookup("doom_loop", "")
            if decision is not None:
                return decision
            return Evaluation(decision=ASK, matched_rule="doom_loop")
        if ctx.external_directory:
            decision = self._lookup("external_directory", "")
            if decision is not None:
                return decision
            return Evaluation(decision=ASK, matched_rule="external_directory")
        layers: list[tuple[str, list[PermissionRule]]] = [
            ("global", self._global),
            ("agent", self._agent),
        ]
        if ctx.mode and ctx.mode in self._modes:
            layers.append((f"mode:{ctx.mode}", self._modes[ctx.mode]))
        merged: Evaluation | None = None
        for layer_name, rules in layers:
            found = self._match_layer(rules, ctx.tool, ctx.action, layer_name)
            if found is not None:
                if found.decision == DENY:
                    return found
                merged = found
        if merged is not None:
            return merged
        log.debug("permission_default_allow", tool=ctx.tool, action=ctx.action)
        return Evaluation(decision=ALLOW)

    def _lookup(self, key: str, action: str) -> Evaluation | None:
        """Look up a reserved key across all layers."""
        merged: Evaluation | None = None
        layers: list[tuple[str, list[PermissionRule]]] = [
            ("global", self._global),
            ("agent", self._agent),
            *((f"mode:{mode}", rules) for mode, rules in self._modes.items()),
        ]
        for layer_name, rules in layers:
            found = self._match_layer(rules, key, action, layer_name)
            if found is not None:
                if found.decision == DENY:
                    return found
                merged = found
        return merged

    @staticmethod
    def _match_layer(
        rules: list[PermissionRule], tool: str, action: str, layer: str
    ) -> Evaluation | None:
        """Apply last-match-wins within one compiled rule layer."""
        matched: Evaluation | None = None
        for rule in rules:
            if not _matches(rule.tool_pattern, tool):
                # A bare "*" rule also applies when no tool-specific
                # rule exists for path/command-scoped narrowing.
                continue
            if isinstance(rule.decision, dict):
                granular = PermissionEvaluator._match_granular(rule.decision, action)
                if granular is None:
                    continue
                decision, pattern = granular
                matched = Evaluation(
                    decision=decision,
                    matched_rule=rule.tool_pattern,
                    matched_pattern=pattern,
                    layer=layer,
                )
            else:
                matched = Evaluation(
                    decision=rule.decision,
                    matched_rule=rule.tool_pattern,
                    matched_pattern=None,
                    layer=layer,
                )
        return matched

    @staticmethod
    def _match_granular(
        granular: dict[str, Decision], action: str
    ) -> tuple[Decision, str] | None:
        """Apply last-match-wins across granular pattern keys.

        ``read``-style actions are full paths while secret patterns
        (``*.env``) are basename-scoped, so both the full action and
        its basename are tried (OpenCode parity: ``.env`` matches
        ``/workspace/.env``).
        """
        matched: tuple[Decision, str] | None = None
        candidates = [action]
        basename = action.rsplit("/", 1)[-1]
        if basename and basename != action:
            candidates.append(basename)
        for pattern, decision in granular.items():
            if any(_matches(pattern, candidate) for candidate in candidates):
                matched = (decision, pattern)
        return matched
