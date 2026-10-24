"""Request and response schemas for small-model commit suggestions."""

from __future__ import annotations

from ninja import Schema

from apps.runners.schemas import GitRepoQuery


class GitCommitMessageIn(GitRepoQuery):
    """Body for POST /git/commit-message/ (small-model commit subject)."""


class GitCommitMessageOut(Schema):
    """Suggested commit subject for the selected repo."""

    message: str
