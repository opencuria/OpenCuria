"""Stateless commit suggestions from workspace git changes and a small model."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from apps.runners.services import RunnerService, get_runner_service
from common.exceptions import ConflictError

from .commit_messages import (
    STYLE_SUBJECTS,
    build_commit_message_prompt,
    normalize_commit_subject,
)
from .providers.base import ProviderAdapter
from .runner import RunOptions
from .small_model_service import build_small_model_runner


class CommitMessageService:
    """Suggest git commit subjects independently of chat session orchestration."""

    def __init__(
        self,
        *,
        git: RunnerService | None = None,
        provider_factory: Callable[[uuid.UUID], ProviderAdapter] | None = None,
    ) -> None:
        self._git = git
        self._provider_factory = provider_factory

    async def generate(
        self,
        *,
        workspace_id: uuid.UUID,
        repo_path: str,
        organization_id: uuid.UUID,
        user_id: int | None = None,
    ) -> str:
        """Suggest a subject for staged changes, else all working-tree changes.

        Recent subjects steer format and language. Raise ``ConflictError``
        when the small model is unset, git cannot read the changes, nothing
        changed, or the model returns no subject.
        """
        small = await build_small_model_runner(
            organization_id, user_id=user_id, provider_factory=self._provider_factory
        )
        if small is None:
            raise ConflictError(
                "Set a small model in the provider settings to generate "
                "commit messages",
                code="small_model_missing",
            )
        runner, small_model = small
        git = self._git or get_runner_service()
        diff = await git.run_git_operation(
            workspace_id, "working_diff", repo_path=repo_path, args={}
        )
        if not diff.get("ok"):
            raise ConflictError(
                str(diff.get("message") or "Could not read the changes"),
                code=str(diff.get("code") or "git_failed"),
            )
        history = await git.run_git_operation(
            workspace_id,
            "repo_history",
            repo_path=repo_path,
            args={"history_limit": STYLE_SUBJECTS},
        )
        subjects = [
            str(commit.get("message") or "")
            for commit in (history.get("commits") or [] if history.get("ok") else [])
            if isinstance(commit, dict)
        ]
        prompt = build_commit_message_prompt(diff.get("diff"), subjects)
        if not prompt:
            raise ConflictError("There are no changes to describe", code="no_changes")
        result = await runner.run(
            prompt,
            "commit_message",
            small_model,
            "build",
            RunOptions(auto_approve=True),
        )
        subject = normalize_commit_subject(result.output or "")
        if not subject:
            raise ConflictError(
                "The model returned no commit message", code="empty_commit_message"
            )
        return subject
