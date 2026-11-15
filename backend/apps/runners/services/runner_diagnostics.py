"""User-facing wording for runner lifecycle failures.

Runners report failures as stable ``diagnostic_code`` values with sanitized
generic text. This module is the single place translating them into short,
actionable messages for toasts, operations and workspace diagnostics.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Cause and next step for every code a runner reports with a specific meaning.
DIAGNOSTICS: dict[str, tuple[str, str]] = {
    "invalid_resources": (
        "The runner rejected the resource settings.",
        "Use positive CPU, memory and disk values.",
    ),
    "disk_shrink_unsupported": (
        "The disk cannot be made smaller.",
        "Keep at least the current disk size.",
    ),
    "disk_below_image_minimum": (
        "The disk is smaller than the image requires.",
        "Choose a larger disk size.",
    ),
    "workspace_exists": (
        "The runner already has resources for this workspace.",
        "Delete the workspace and create a new one.",
    ),
    "image_unavailable": (
        "The image is missing on the runner.",
        "Rebuild the image or choose another one.",
    ),
    "image_invalid": (
        "The image on the runner is incomplete or damaged.",
        "Rebuild the image or choose another one.",
    ),
    "credential_scrub_unverified": (
        "The workspace was stopped outside OpenCuria, so credential removal "
        "is unverified.",
        "Start it, then stop it again.",
    ),
    "build_queue_timeout": (
        "No image build slot became free in time.",
        "Start the build again later.",
    ),
    "build_script_failed": (
        "The image setup script failed.",
        "Check the build log, fix the script and build again.",
    ),
    "build_script_timeout": (
        "The image setup script exceeded its time limit.",
        "Shorten the script, then build again.",
    ),
    "insufficient_storage": (
        "The runner is out of disk space.",
        "Free up storage on the runner, then try again.",
    ),
}

FAILED = (
    "The runner could not complete the operation. Try again; if it keeps "
    "failing, check the runner logs."
)
INTERRUPTED = "Operation interrupted; automatic recovery will verify the runner state."
RECOVERY_PENDING = "Automatic recovery is checking the runner state."
RECOVERED = (
    "Execution was interrupted. The runner verified that it has ended; "
    "existing resources were kept. Retry the action if needed."
)


def _diagnostic(result: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """Return the cause and next step of a coded runner result, if any."""
    if not isinstance(result, Mapping):
        return None
    code = result.get("diagnostic_code")
    return DIAGNOSTICS.get(code) if isinstance(code, str) else None


def failure_message(result: Mapping[str, Any]) -> str:
    """Describe a terminal runner failure; unknown outcomes await recovery."""
    diagnostic = _diagnostic(result)
    if result.get("outcome_known") is False:
        return f"{diagnostic[0]} {RECOVERY_PENDING}" if diagnostic else INTERRUPTED
    return " ".join(diagnostic) if diagnostic else FAILED


def recovered_message(result: Mapping[str, Any] | None) -> str:
    """Describe an interrupted operation after recovery proved it has ended."""
    diagnostic = _diagnostic(result)
    return " ".join(diagnostic) if diagnostic else RECOVERED
