"""Tests for small-model commit subject suggestions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.harness.agents.definitions import get_agent
from apps.harness.commit_messages import (
    MAX_DIFF_CHARS,
    build_commit_message_prompt,
    normalize_commit_subject,
)
from apps.harness.harness_service import HarnessService
from apps.harness.providers.base import Delta, ProviderAdapter, Usage
from apps.harness.services import ProviderConfigService
from common.exceptions import ConflictError

REPO = "/workspace/repo"


def _file(path: str, *lines: tuple[str, str], status: str = "M") -> dict:
    return {
        "old_path": path,
        "new_path": path,
        "status": status,
        "additions": sum(1 for kind, _ in lines if kind == "add"),
        "deletions": sum(1 for kind, _ in lines if kind == "del"),
        "binary": False,
        "truncated": False,
        "has_textual_diff": bool(lines),
        "diff": [
            {
                "header": "@@ -1,2 +1,2 @@",
                "old_start": 1,
                "new_start": 1,
                "lines": [{"type": kind, "content": text} for kind, text in lines],
            }
        ]
        if lines
        else [],
    }


STAGED = _file(
    "src/auth.py",
    ("context", "def login():"),
    ("del", "  return 1"),
    ("add", "  return 2"),
)
UNSTAGED = _file("README.md", ("add", "Docs"))


class CommitProvider(ProviderAdapter):
    """Answers the commit_message agent and records its prompt."""

    name = "fake"

    def __init__(self, reply: str = "fix(auth): return the right login code") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        """Return the configured reply and remember the request."""
        self.systems.append(str(messages[0].content or ""))
        self.prompts.append(str(messages[-1].content or ""))
        assert tools in (None, [])
        yield Delta(text=self.reply, usage=Usage(1, 1, 2))


def _service(provider: ProviderAdapter) -> HarnessService:
    return HarnessService(provider_factory=lambda _org: provider)


def _fake_git(monkeypatch, *, diff: dict, history: dict | None = None):
    calls: list[tuple[str, dict]] = []

    async def run_git_operation(workspace_id, operation, *, repo_path=None, args=None):
        calls.append((operation, {"repo_path": repo_path, **(args or {})}))
        if operation == "working_diff":
            return diff
        return history or {"ok": True, "commits": []}

    fake = SimpleNamespace(run_git_operation=AsyncMock(side_effect=run_git_operation))
    monkeypatch.setattr("apps.harness.harness_service.get_runner_service", lambda: fake)
    return calls


def _configure_small_model(workspace) -> None:
    ProviderConfigService().save_config(
        organization_id=workspace.runner.organization_id,
        default_model="big-model",
        small_model="small-model",
    )


def test_commit_message_agent_is_text_only_on_the_small_model() -> None:
    """The hidden agent offers no tools and resolves to the small model."""
    agent = get_agent("commit_message")
    assert agent.mode == "hidden"
    assert agent.permissions == {"*": "deny", "question": "deny"}
    assert agent.model_override == "small"
    assert "subject line only" in agent.system_prompt


def test_prompt_prefers_staged_changes_and_lists_style_subjects() -> None:
    """Staged files are described with their patch; style subjects come first."""
    prompt = build_commit_message_prompt(
        {"staged": [STAGED], "unstaged": [UNSTAGED]},
        ["feat(api): add login", "  ", "fix: typo"],
    )

    assert prompt.startswith(
        "Recent commit subjects:\n- feat(api): add login\n- fix: typo"
    )
    assert "Staged changes (1 files):\nM src/auth.py (+1 -1)" in prompt
    assert "-  return 1\n+  return 2" in prompt
    assert "README.md" not in prompt


def test_prompt_falls_back_to_unstaged_and_is_empty_without_changes() -> None:
    """Without staged files all working-tree changes are described."""
    prompt = build_commit_message_prompt({"staged": [], "unstaged": [UNSTAGED]})
    assert prompt.startswith("Changes (1 files):\nM README.md (+1 -0)")
    assert build_commit_message_prompt({"staged": [], "unstaged": []}) == ""
    assert build_commit_message_prompt(None) == ""


def test_prompt_bounds_large_diffs_but_keeps_every_file_header() -> None:
    """Huge patches are clipped while all file names stay visible."""
    big = [_file(f"f{i}.txt", *[("add", "x" * 200)] * 200) for i in range(20)]
    prompt = build_commit_message_prompt({"staged": big})

    assert all(f"M f{i}.txt (+200 -0)" in prompt for i in range(20))
    assert "[remaining diffs omitted]" in prompt
    assert "[diff truncated]" in prompt
    assert len(prompt) < MAX_DIFF_CHARS + 5_000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("fix(auth): return the right code\n", "fix(auth): return the right code"),
        ('"Add login retry"', "Add login retry"),
        ("```\nfeat: add retry\n```", "feat: add retry"),
        ("Commit message: **Add retry**", "Add retry"),
        ("`fix: handle null ids`", "fix: handle null ids"),
        ("fix: handle `null` ids", "fix: handle `null` ids"),
        ('fix: rename "foo"', 'fix: rename "foo"'),
        ("\n\n", ""),
    ],
)
def test_normalize_commit_subject(raw: str, expected: str) -> None:
    """Model output is reduced to one plain subject line."""
    assert normalize_commit_subject(raw) == expected


def test_normalize_commit_subject_clamps_at_a_word_boundary() -> None:
    """Overlong subjects are cut at a word boundary."""
    result = normalize_commit_subject("word " * 40)
    assert len(result) <= 100
    assert result.endswith("word")


@pytest.mark.django_db(transaction=True)
async def test_generate_commit_message_uses_diff_and_recent_subjects(
    harness_workspace, monkeypatch
) -> None:
    """The small model sees the staged diff plus recent subjects."""
    _configure_small_model(harness_workspace)
    calls = _fake_git(
        monkeypatch,
        diff={"ok": True, "diff": {"staged": [STAGED], "unstaged": [UNSTAGED]}},
        history={"ok": True, "commits": [{"message": "feat(api): add login"}]},
    )
    provider = CommitProvider()

    message = await _service(provider).generate_commit_message(
        workspace_id=harness_workspace.id,
        repo_path=REPO,
        organization_id=harness_workspace.runner.organization_id,
        user_id=harness_workspace.created_by_id,
    )

    assert message == "fix(auth): return the right login code"
    assert calls == [
        ("working_diff", {"repo_path": REPO}),
        ("repo_history", {"repo_path": REPO, "history_limit": 10}),
    ]
    assert "subject line only" in provider.systems[0]
    assert "- feat(api): add login" in provider.prompts[0]
    assert "src/auth.py" in provider.prompts[0]


@pytest.mark.django_db(transaction=True)
async def test_generate_commit_message_needs_a_small_model(
    harness_workspace, monkeypatch
) -> None:
    """Without a small model nothing is read or prompted."""
    calls = _fake_git(monkeypatch, diff={"ok": True, "diff": {"staged": [STAGED]}})
    provider = CommitProvider()

    with pytest.raises(ConflictError) as exc:
        await _service(provider).generate_commit_message(
            workspace_id=harness_workspace.id,
            repo_path=REPO,
            organization_id=harness_workspace.runner.organization_id,
        )

    assert exc.value.code == "small_model_missing"
    assert calls == []
    assert provider.prompts == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("diff", "reply", "code"),
    [
        ({"ok": True, "diff": {"staged": [], "unstaged": []}}, "unused", "no_changes"),
        (
            {"ok": False, "code": "not_a_repo", "message": "Not a repo"},
            "unused",
            "not_a_repo",
        ),
        (
            {"ok": True, "diff": {"staged": [STAGED]}},
            "```\n```",
            "empty_commit_message",
        ),
    ],
)
async def test_generate_commit_message_failures(
    harness_workspace, monkeypatch, diff, reply, code
) -> None:
    """Empty changes, runner errors and empty replies raise a coded conflict."""
    _configure_small_model(harness_workspace)
    _fake_git(monkeypatch, diff=diff)

    with pytest.raises(ConflictError) as exc:
        await _service(CommitProvider(reply)).generate_commit_message(
            workspace_id=harness_workspace.id,
            repo_path=REPO,
            organization_id=harness_workspace.runner.organization_id,
        )

    assert exc.value.code == code
