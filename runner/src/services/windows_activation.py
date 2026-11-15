"""Windows license activation inside a running guest.

The control plane decides which license a Windows workspace uses and sends it
with create, clone and resume. The runner only applies it: the product key
travels to the guest on stdin, never in a command line, a file or a log.
Activation never fails a lifecycle operation; its outcome is reported as
license status.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from ..models import WorkspaceInfo

logger = structlog.get_logger(__name__)

_SCRIPT = Path(__file__).resolve().parent.parent / "runtime" / "windows" / "activate.ps1"
_REMOTE_DIR = "/tmp"
_KEY = re.compile(r"[A-Z0-9]{5}(?:-[A-Z0-9]{5}){4}")
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?")
_STATUSES = {"activated", "failed", "not_activated"}
#: Online activation may wait for a slow licensing service.
ACTIVATION_TIMEOUT = 180.0


def validate_activation(config: object) -> dict[str, str]:
    """Return a validated activation request or raise ``ValueError``."""
    if not isinstance(config, dict):
        raise ValueError("Invalid Windows activation request")
    mode = config.get("mode")
    if mode == "key":
        key = config.get("product_key")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("Invalid Windows product key")
        return {"mode": "key", "product_key": key}
    if mode == "kms":
        host = config.get("kms_host") or ""
        if not isinstance(host, str) or (host and not _HOST.fullmatch(host)):
            raise ValueError("Invalid KMS host")
        return {"mode": "kms", "kms_host": host}
    if mode == "none":
        return {"mode": "none"}
    raise ValueError("Invalid Windows activation mode")


class WindowsActivationService:
    """Apply a workspace's Windows license through the guest licensing service."""

    capture_fence = None

    def __init__(self, get_cached: Callable[[uuid.UUID], WorkspaceInfo], get_runtime) -> None:
        self._get_cached = get_cached
        self._get_runtime = get_runtime

    async def activate(self, workspace_id: uuid.UUID, config: Any) -> dict[str, str] | None:
        """Activate a Windows workspace; ``None`` when no license was requested."""
        if config is None:
            return None
        log = logger.bind(workspace_id=str(workspace_id))
        try:
            request = validate_activation(config)
        except ValueError:
            log.warning("windows_activation_rejected")
            return {"status": "failed", "code": "invalid_request"}
        try:
            result = await asyncio.wait_for(self._run(workspace_id, request), ACTIVATION_TIMEOUT)
        except asyncio.TimeoutError:
            result = {"status": "failed", "code": "timeout"}
        except Exception:
            log.warning("windows_activation_failed", exc_info=True)
            result = {"status": "failed", "code": "runner_error"}
        log.info("windows_activation", status=result["status"], code=result["code"])
        return result

    async def _run(self, workspace_id: uuid.UUID, request: dict[str, str]) -> dict[str, str]:
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        name = f"opencuria-activate-{uuid.uuid4().hex}.ps1"
        script = _SCRIPT.read_bytes()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            entry = tarfile.TarInfo(name=name)
            entry.mode = 0o600
            entry.size = len(script)
            tar.addfile(entry, io.BytesIO(script))
        await runtime.put_archive(info.instance_id, _REMOTE_DIR, archive.getvalue())
        remote = f"{_REMOTE_DIR}/{name}"
        command = (
            "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
            f'-File "$(cygpath -w {remote})"; status=$?; rm -f {remote}; exit $status'
        )
        code, stdout, _ = await runtime.exec_with_input(
            info.instance_id,
            ["sh", "-c", command],
            json.dumps(request).encode(),
        )
        return _parse_result(code, stdout)


def _parse_result(exit_code: int, stdout: str) -> dict[str, str]:
    """Read the script's last JSON line; anything else is a failure."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            result = json.loads(line)
        except ValueError:
            break
        status = result.get("status") if isinstance(result, dict) else None
        if status in _STATUSES:
            code = result.get("code") or ""
            return {"status": status, "code": str(code)[:32]}
        break
    return {"status": "failed", "code": f"exit_{exit_code}"}
