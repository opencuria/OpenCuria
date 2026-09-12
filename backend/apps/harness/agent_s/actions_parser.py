"""Typed action model + safe ``ast`` parser (no ``eval``/``exec``).

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.

The planner must emit exactly one ``agent.method(...)`` line inside a fenced
code block. Agent-S materializes it with ``eval(code)``; this port instead
parses the extracted line with :mod:`ast` and accepts only whitelisted
literal arguments (strings, numbers, booleans, None, lists, dicts — plus
unary ``-``/``+`` numbers such as ``scroll(..., -3)``). Names, attributes,
calls, lambdas, comprehensions, tuples/sets, arithmetic, conditionals and
``**``-unpacking are all rejected.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Literal

ACTION_NAMES: tuple[str, ...] = (
    "click",
    "switch_applications",
    "open",
    "type",
    "save_to_knowledge",
    "drag_and_drop",
    "highlight_text_span",
    "set_cell_values",
    "call_code_agent",
    "scroll",
    "hotkey",
    "hold_and_press",
    "wait",
    "done",
    "fail",
)

# Canonical ``dir()`` order (alphabetical) used in the worker prompt.
ACTION_ORDER: tuple[str, ...] = tuple(sorted(ACTION_NAMES))

TerminalSignal = Literal["DONE", "FAIL", "WAIT"]


@dataclass(frozen=True)
class ParsedAction:
    """A safely parsed ``agent.<method>(...)`` call."""

    method: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)

    def call_repr(self) -> str:
        """Canonical ``agent.method(...)`` representation (for tests/logs)."""
        parts = [repr(a) for a in self.args]
        parts += [f"{k}={v!r}" for k, v in self.kwargs.items()]
        return f"agent.{self.method}({', '.join(parts)})"


class ActionParseError(ValueError):
    """Raised when the grounded-action line is not a valid action call."""


def _ensure_literal(node: ast.AST) -> None:
    """Whitelist check: only plain literal data (no names/calls/attrs).

    Accepts ``Constant`` (str/int/float/bool/None), unary ``-``/``+`` on
    numbers (e.g. ``-3`` for scroll clicks), ``List`` and ``Dict`` (nested).
    Everything else — names, attributes, calls, lambdas, comprehensions,
    tuples, sets, arithmetic, ``if``-expressions, dict ``**``-unpacking —
    raises :class:`ActionParseError`.
    """
    if isinstance(node, ast.Constant):
        if node.value is None or isinstance(node.value, (str, int, float, bool)):
            return
        raise ActionParseError(
            f"non-literal argument: {type(node.value).__name__} not allowed"
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = node.operand
        if (
            isinstance(operand, ast.Constant)
            and isinstance(operand.value, (int, float))
            and not isinstance(operand.value, bool)
        ):
            return
        raise ActionParseError("non-literal argument: unary expression not allowed")
    if isinstance(node, ast.List):
        for elt in node.elts:
            _ensure_literal(elt)
        return
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if key is None:
                raise ActionParseError("**kwargs are not allowed")
            _ensure_literal(key)
            _ensure_literal(value)
        return
    raise ActionParseError(f"non-literal argument: {type(node).__name__} not allowed")


def parse_action_line(code: str) -> ParsedAction:
    """Parse one ``agent.method(...)`` line into a :class:`ParsedAction`.

    Only literal arguments are accepted (strings, numbers, booleans, None,
    lists, dicts, unary numeric signs). Anything else — attribute targets,
    non-``agent`` receivers, unknown methods, multiple statements, tuples,
    non-literal args — raises :class:`ActionParseError`.
    """
    text = code.strip()
    if not text:
        raise ActionParseError("empty action code")
    try:
        module = ast.parse(text, mode="exec")
    except SyntaxError as exc:
        raise ActionParseError(f"invalid python: {exc}") from exc
    if len(module.body) != 1 or not isinstance(module.body[0], ast.Expr):
        raise ActionParseError("action must be a single expression statement")
    expr = module.body[0].value
    if not isinstance(expr, ast.Call):
        raise ActionParseError("action must be a call")
    func = expr.func
    if not (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "agent"
    ):
        raise ActionParseError("action must be a call on 'agent'")
    if func.attr not in ACTION_NAMES:
        raise ActionParseError(f"unknown agent action: {func.attr}")
    if expr.starred if hasattr(expr, "starred") else False:  # pragma: no cover
        raise ActionParseError("starred calls are not allowed")
    args: list[Any] = []
    for node in expr.args:
        if isinstance(node, ast.Starred):
            raise ActionParseError("starred arguments are not allowed")
        _ensure_literal(node)
        try:
            args.append(ast.literal_eval(node))
        except Exception as exc:
            raise ActionParseError(f"non-literal argument: {exc}") from exc
    kwargs: dict[str, Any] = {}
    seen_kwargs: set[str] = set()
    for keyword in expr.keywords:
        if keyword.arg is None:
            raise ActionParseError("**kwargs are not allowed")
        if keyword.arg in seen_kwargs:
            raise ActionParseError(f"duplicate keyword argument: {keyword.arg}")
        seen_kwargs.add(keyword.arg)
        _ensure_literal(keyword.value)
        try:
            kwargs[keyword.arg] = ast.literal_eval(keyword.value)
        except Exception as exc:
            raise ActionParseError(f"non-literal argument: {exc}") from exc
    return ParsedAction(method=func.attr, args=tuple(args), kwargs=kwargs)


def coerce_action(action: ParsedAction, signature_binders) -> ParsedAction:
    """Bind parsed args to the action signature, filling defaults.

    ``signature_binders`` maps method name -> ``inspect.Signature``-like with
    ``bind``/``apply_defaults`` (normally from
    :class:`AgentActionSurface <apps.harness.agent_s.actions.AgentActionSurface>`).
    Raises :class:`ActionParseError` on arity/type mismatch, mirroring the
    ``eval`` failure Agent-S would surface as ``CODE_VALID`` feedback.
    """
    binder = signature_binders.get(action.method)
    if binder is None:
        raise ActionParseError(f"unknown agent action: {action.method}")
    try:
        bound = binder.bind(None, *action.args, **action.kwargs)
    except TypeError as exc:
        raise ActionParseError(str(exc)) from exc
    bound.apply_defaults()
    params = list(binder.parameters)[1:]
    ordered = {name: bound.arguments[name] for name in params}
    return ParsedAction(method=action.method, args=(), kwargs=ordered)
