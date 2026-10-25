"""Expected tool failures reported back to the calling model."""

from __future__ import annotations


class ToolError(Exception):
    """A tool call that cannot complete, with an actionable message.

    The message is shown to the model verbatim, so it should say what went
    wrong and which tool or argument fixes it.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
