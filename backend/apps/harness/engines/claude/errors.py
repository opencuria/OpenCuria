"""Safe, actionable Claude session recovery errors."""


class ClaudeTranscriptError(RuntimeError):
    """A bound Claude session cannot be resumed from its stored transcript."""

    def __init__(self, reason: str) -> None:
        """Describe recovery without exposing opaque transcript contents."""
        super().__init__(
            f"Claude transcript {reason}. Fork this conversation or start a new "
            "session to continue; the existing conversation has not been reset."
        )
