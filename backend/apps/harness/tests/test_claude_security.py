"""Security-boundary tests for Claude redaction and Bash classification."""

from __future__ import annotations

import pytest

from apps.harness.engines.claude.security import StreamingRedactor, redact_text
from apps.harness.engines.claude.shellpolicy import shell_command_may_mutate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ANTHROPIC_API_KEY=secret-value", "ANTHROPIC_API_KEY=[redacted]"),
        ("CLAUDE_CODE_OAUTH_TOKEN=secret-value", "CLAUDE_CODE_OAUTH_TOKEN=[redacted]"),
        ("MY_SECRET: 'quoted value'", "MY_SECRET: [redacted]"),
        ("Authorization: Bearer opaque-value", "Authorization: Bearer [redacted]"),
        ("token=opaque-value", "token=[redacted]"),
        (
            "export ANTHROPIC_API_KEY='secret value'",
            "export ANTHROPIC_API_KEY=[redacted]",
        ),
    ],
)
def test_secret_assignment_names_are_redacted(raw: str, expected: str) -> None:
    assert redact_text(raw) == expected


def _stream_in_chunks(
    text: str, sizes: list[int], *, secrets: tuple[str, ...] = ()
) -> str:
    """Stream text through fresh buffers with the specified chunk lengths."""
    redactor = StreamingRedactor(secrets)
    chunks: list[str] = []
    offset = 0
    for size in sizes:
        chunks.append(redactor.push(text[offset : offset + size]))
        offset += size
    chunks.append(redactor.push(text[offset:]))
    chunks.append(redactor.finish())
    return "".join(chunks)


def test_known_secret_redaction_survives_every_two_chunk_split() -> None:
    secret = "sk-ant-auth-fixture-0123456789abcdefghijklmnopqrstuv"
    text = f"Safe prefix: {secret} : safe suffix"
    for offset in range(1, len(secret)):
        redactor = StreamingRedactor([secret])
        output = "".join(
            (
                redactor.push(text[: len("Safe prefix: ") + offset]),
                redactor.push(text[len("Safe prefix: ") + offset :]),
                redactor.finish(),
            )
        )
        assert secret not in output
        assert output == "Safe prefix: [redacted] : safe suffix"


def test_known_secret_redaction_survives_single_character_chunks() -> None:
    secret = "sk-ant-single-character-fixture-abcdef0123456789"
    text = f"Before {secret} after"
    redactor = StreamingRedactor([secret])
    output = "".join(redactor.push(char) for char in text)
    output += redactor.push("")
    output += redactor.finish()
    assert secret not in output
    assert output == "Before [redacted] after"


def test_known_secret_redaction_handles_empty_chunks_and_adjacent_matches() -> None:
    secret = "anthropic-api-key-fixture-value-0123456789"
    redactor = StreamingRedactor([secret])
    output = redactor.push("Before " + secret[:1])
    output += redactor.push("")
    output += redactor.push(secret[1:] + " " + secret)
    output += redactor.push(" after")
    output += redactor.finish()
    assert secret not in output
    assert output == "Before [redacted] [redacted] after"


def test_streaming_assignment_redaction_handles_every_key_and_value_split() -> None:
    text = "safe ANTHROPIC_API_KEY=split-fixture-secret more safe text"
    expected = "safe ANTHROPIC_API_KEY=[redacted] more safe text"
    for offset in range(1, len(text)):
        redactor = StreamingRedactor()
        output = redactor.push(text[:offset])
        output += redactor.push(text[offset:])
        output += redactor.finish()
        assert output == expected, f"split at {offset}: {output!r}"


def test_streaming_assignment_redaction_handles_single_character_chunks() -> None:
    text = "prefix CLAUDE_CODE_OAUTH_TOKEN=oauth-fixture-value suffix"
    redactor = StreamingRedactor()
    output = "".join(redactor.push(character) for character in text)
    output += redactor.finish()
    assert output == "prefix CLAUDE_CODE_OAUTH_TOKEN=[redacted] suffix"


def test_streaming_redactor_is_fresh_per_content_block() -> None:
    first = StreamingRedactor(["sk-ant-reset-fixture-value"])
    first_output = first.push("safe prefix") + first.finish()
    second = StreamingRedactor(["sk-ant-reset-fixture-value"])
    second_output = second.push("safe suffix") + second.finish()
    assert first_output == "safe prefix"
    assert second_output == "safe suffix"


@pytest.mark.parametrize(
    "command",
    [
        "git branch -D feature",
        "git branch -d feature",
        "git branch --delete feature",
        "git branch -m old new",
        "git branch -M old new",
        "git branch --move old new",
        "git branch -c old copy",
        "git branch -C old copy",
        "git branch -f feature",
        "git branch feature",
        "git branch --sort=-committerdate feature",
        "git checkout -b feature",
        "git config user.name example",
    ],
)
def test_git_mutation_commands_require_plan_approval(command: str) -> None:
    assert shell_command_may_mutate(command), command


@pytest.mark.parametrize(
    "command",
    [
        "git branch",
        "git branch --list",
        "git branch -a",
        "git branch --merged",
        "git branch --contains HEAD",
        "git branch --contains HEAD --list",
        "git status --short",
        "git config --list",
        "git config --get user.name",
        "git diff",
    ],
)
def test_git_read_only_inspection_commands_remain_available(command: str) -> None:
    assert not shell_command_may_mutate(command), command


@pytest.mark.parametrize(
    "command",
    [
        'env python -c \'open("x", "w")\'',
        "timeout 1 rm -f x",
        "xargs rm",
        "command sh -c 'touch x'",
        "make clean",
        "npm run lint",
        "npm exec eslint",
        "git status > result.txt",
    ],
)
def test_shell_wrappers_and_script_commands_fail_closed(command: str) -> None:
    assert shell_command_may_mutate(command), command


@pytest.mark.parametrize(
    "command", ["pytest -q", "grep -R foo .", "git status", "make -n clean"]
)
def test_known_inspection_and_explicit_dry_run_commands_are_read_only(
    command: str,
) -> None:
    assert not shell_command_may_mutate(command), command


def test_numeric_usage_metadata_is_not_redacted_as_a_token_secret() -> None:
    from apps.harness.engines.claude.security import redact_value

    event = {
        "type": "step_finish",
        "tokens": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
        "cost": 0.01,
    }
    assert redact_value(event, ("private-fixture-token",)) == event
    assert redact_value({"tokens": "private-fixture-token"}) == {"tokens": "[redacted]"}
    assert redact_value({"tokens": {"access_token": "private-fixture-token"}}) == {
        "tokens": "[redacted]"
    }
