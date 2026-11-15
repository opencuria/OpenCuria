"""Keep desktop forwarding independent of runner control and orchestration."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

RUNNER_ROOT = Path(__file__).resolve().parents[1]


def test_desktop_proxy_imports_only_its_runtime_and_admission_contracts() -> None:
    """A proxy receives desktop lookup and events through narrow injected ports."""
    path = RUNNER_ROOT / "src/interfaces/desktop_proxy.py"
    imports = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name(
                "." * node.level + (node.module or ""), "src.interfaces"
            )
            imports.add(module)
            imports.update(f"{module}.{alias.name}" for alias in node.names)
    allowed = {
        "src.models",
        "src.models.DesktopSession",
        "src.services.capture_fence",
        "src.services.capture_fence.CaptureFence",
    }
    violations = {
        module
        for module in imports
        if module == "socketio"
        or module.startswith("socketio.")
        or module.startswith("src.")
        and module not in allowed
    }
    assert not violations, (
        "Inject desktop observations and event delivery; desktop forwarding "
        f"must not import control adapters or orchestration: {sorted(violations)}"
    )


def test_control_adapter_delegates_desktop_connections_and_frame_forwarding() -> None:
    """The control adapter cannot own proxy resources or reach private tunnels."""
    path = RUNNER_ROOT / "src/interfaces/websocket.py"
    violations = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Attribute) and node.attr in {
            "ClientSession",
            "ClientWebSocketResponse",
            "ws_connect",
            "_tunnels",
        }:
            violations.append(f"{node.lineno}: {node.attr}")
        if isinstance(node, ast.Constant) and node.value in (
            "desktop:proxy_ws_frame",
            "desktop:proxy_ws_closed",
        ):
            violations.append(f"{node.lineno}: {node.value}")
    assert not violations, (
        "DesktopProxy owns HTTP/WS connections, readers and tunnel state; "
        "WebSocketInterface only routes control requests: " + ", ".join(violations)
    )


def test_capture_admission_has_no_knowledge_of_desktop_tunnel_storage() -> None:
    """The proxy resolves its own workspace before entering capture admission."""
    tree = ast.parse((RUNNER_ROOT / "src/services/capture_fence.py").read_text())
    storage_names = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not storage_names & {"tunnel_id", "_tunnels", "_desktop_proxy_tunnels"}, (
        "CaptureFence must not inspect desktop proxy state; pass the workspace ID"
    )
