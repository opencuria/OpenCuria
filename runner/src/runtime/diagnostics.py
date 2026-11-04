"""Stable lifecycle failure codes reported to the control plane.

The runner reports *what* failed as a code; the backend owns the wording shown
to users. Exception messages stay in runner logs and never leave the host.
"""

from __future__ import annotations

#: Interactive (non-lifecycle) failures caused by the guest transport, e.g. an
#: exhausted SSH session limit or a dropped connection, not by the command.
WORKSPACE_UNREACHABLE = "workspace_unreachable"

INVALID_RESOURCES = "invalid_resources"
DISK_SHRINK_UNSUPPORTED = "disk_shrink_unsupported"
DISK_BELOW_IMAGE_MINIMUM = "disk_below_image_minimum"
WORKSPACE_EXISTS = "workspace_exists"
IMAGE_UNAVAILABLE = "image_unavailable"
IMAGE_INVALID = "image_invalid"
CREDENTIAL_SCRUB_UNVERIFIED = "credential_scrub_unverified"
BUILD_QUEUE_TIMEOUT = "build_queue_timeout"
BUILD_SCRIPT_FAILED = "build_script_failed"
BUILD_SCRIPT_TIMEOUT = "build_script_timeout"
INSUFFICIENT_STORAGE = "insufficient_storage"
INTERRUPTED = "interrupted"
HANDLER_FAILED = "handler_failed"

#: Every code a terminal operation result may carry.
CODES = frozenset(
    {
        INVALID_RESOURCES,
        DISK_SHRINK_UNSUPPORTED,
        DISK_BELOW_IMAGE_MINIMUM,
        WORKSPACE_EXISTS,
        IMAGE_UNAVAILABLE,
        IMAGE_INVALID,
        CREDENTIAL_SCRUB_UNVERIFIED,
        BUILD_QUEUE_TIMEOUT,
        BUILD_SCRIPT_FAILED,
        BUILD_SCRIPT_TIMEOUT,
        INSUFFICIENT_STORAGE,
        INTERRUPTED,
        HANDLER_FAILED,
    }
)


class OperationError(RuntimeError):
    """Lifecycle failure with a stable code; runtime effects may have happened."""

    def __init__(self, code: str, message: str) -> None:
        if code not in CODES:
            raise ValueError(f"Unknown diagnostic code: {code}")
        super().__init__(message)
        self.code = code


class OperationRejectedError(OperationError):
    """Refusal raised before the requested operation changed any runtime state.

    Raise it only ahead of the operation's first effect: callers report it as a
    known failure, without recovery or automatic retry.
    """


def failure_code(error: BaseException | str) -> str:
    """Classify a failure for the backend without exposing its message."""
    seen: set[int] = set()
    current: BaseException | str | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OperationError):
            return current.code
        if "no space left" in str(current).lower():
            return INSUFFICIENT_STORAGE
        if not isinstance(current, BaseException):
            break
        current = current.__cause__ or current.__context__
    return HANDLER_FAILED
