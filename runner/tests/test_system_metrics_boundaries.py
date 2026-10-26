"""Keep resource sampling behind a transport-independent service boundary."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

RUNNER_ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    """Resolve imports, including aliases and function-local relative imports."""
    package = ".".join(path.relative_to(RUNNER_ROOT).parts[:-1])
    result = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            result.add(module)
            result.update(f"{module}.{alias.name}" for alias in node.names)
    return result


def test_metrics_collector_never_imports_transport_or_workspace_orchestration() -> None:
    """Only configuration may couple the resource collector to other runner code."""
    imports = _imports(RUNNER_ROOT / "src/services/system_metrics.py")
    forbidden = {"aiohttp", "socketio"}
    violations = {
        module
        for module in imports
        if module.split(".")[0] in forbidden
        or module.startswith("src.")
        and module not in {"src.config", "src.config.RunnerSettings"}
    }
    assert not violations, (
        "Supply VM observations to SystemMetricsCollector; it must not import "
        f"transport or workspace orchestration: {sorted(violations)}"
    )


def test_interfaces_delegate_resource_sampling_to_the_collector() -> None:
    """Control adapters schedule and send observations without probing the host."""
    violations = {
        str(path.relative_to(RUNNER_ROOT))
        for path in (RUNNER_ROOT / "src/interfaces").rglob("*.py")
        if any(module.split(".")[0] == "psutil" for module in _imports(path))
    }
    assert not violations, (
        "Host resource probes belong to SystemMetricsCollector, not adapters: "
        f"{sorted(violations)}"
    )
