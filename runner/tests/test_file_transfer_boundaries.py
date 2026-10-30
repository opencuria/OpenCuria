"""Keep file-transfer state and framing below the WebSocket control adapter."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

RUNNER_ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    """Resolve module dependencies, including function-local relative imports."""
    package = ".".join(path.relative_to(RUNNER_ROOT).parts[:-1])
    imports = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(resolve_name("." * node.level + (node.module or ""), package))
    return imports


def test_file_transfers_never_import_control_or_workspace_execution() -> None:
    """Transfer buffers need encoding primitives and an injected event sink only."""
    imports = _imports(RUNNER_ROOT / "src/interfaces/file_transfers.py")
    violations = {
        module
        for module in imports
        if module.split(".")[0] in {"aiohttp", "socketio"}
        or module.startswith("src.")
        and module != "src.chunking"
    }
    assert not violations, (
        "File transfers own protocol state, without workspace services, runtime "
        f"or control adapter dependencies: {sorted(violations)}"
    )


def test_control_adapter_never_owns_file_buffers_or_chunk_framing() -> None:
    """Only the protocol component assembles buffers and frames base64 chunks."""
    path = RUNNER_ROOT / "src/interfaces/websocket.py"
    assert "src.chunking" not in _imports(path), (
        "Delegate file framing and upload limits to interfaces.file_transfers"
    )
    violations = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and node.value in (
            "chunks",
            "buffered_chars",
            "max_chars",
        ):
            violations.append(f"{node.lineno}: buffer field {node.value}")
        if (
            isinstance(node, (ast.Attribute, ast.Subscript))
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "_uploads"
            and (
                isinstance(node, ast.Subscript)
                or node.attr.startswith("_")
                or node.attr in {"get", "pop", "items", "values"}
            )
        ):
            violations.append(f"{node.lineno}: direct upload buffer access")
    assert not violations, (
        "WebSocketInterface routes file requests through UploadTransfers; "
        "buffer storage is private: " + ", ".join(violations)
    )
