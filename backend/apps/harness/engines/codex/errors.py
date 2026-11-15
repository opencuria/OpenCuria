"""Fixed public recovery guidance, separate from secret-bearing protocol errors."""

from __future__ import annotations


class CodexRecoveryError(RuntimeError):
    """A safe actionable failure; native workspace side effects are not retried."""
