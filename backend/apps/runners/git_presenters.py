"""Git service error presentation shared by workspace REST adapters."""

from __future__ import annotations

from common.exceptions import ConflictError, NotFoundError
from common.schemas import ErrorOut

from .exceptions import RunnerOfflineError, RunnerTimeoutError, WorkspaceStateError


def git_error_to_response(exc: Exception) -> tuple[int, ErrorOut]:
    """Map git service errors to (status, ErrorOut) tuples."""
    if isinstance(exc, NotFoundError):
        return 404, ErrorOut(detail=exc.message, code=exc.code)
    if isinstance(exc, RunnerTimeoutError):
        return 504, ErrorOut(detail=exc.message, code="runner_timeout")
    if isinstance(exc, RunnerOfflineError):
        return 409, ErrorOut(detail=str(exc), code="runner_offline")
    if isinstance(exc, WorkspaceStateError):
        return 409, ErrorOut(detail=str(exc), code="workspace_conflict")
    if isinstance(exc, ConflictError):
        return 409, ErrorOut(detail=exc.message, code=exc.code)
    if isinstance(exc, ValueError):
        return 400, ErrorOut(detail=str(exc), code="validation_error")
    if isinstance(exc, RuntimeError):
        message = str(exc)
        lowered = message.lower()
        if any(marker in lowered for marker in ("timed out", "timeout")):
            return 504, ErrorOut(detail=message, code="runner_timeout")
        return 502, ErrorOut(detail=message, code="runner_call_failed")
    return 502, ErrorOut(detail=str(exc), code="runner_call_failed")
