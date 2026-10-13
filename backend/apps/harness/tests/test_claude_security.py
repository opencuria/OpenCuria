"""Security-boundary tests for Claude redaction and Bash classification."""

from __future__ import annotations

import pytest

from apps.harness.engines.claude.security import StreamingRedactor, redact_text
from apps.harness.engines.claude.shellpolicy import (
    shell_command_is_explore_safe,
    shell_command_may_mutate,
)


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
        "git config alias.cleanup '!rm -rf .'",
        "git diff --ext-diff",
        "git diff --textconv",
        "git grep --open-files-in-pager=sh pattern",
        "git status --open-files-in-pager sh",
        "git status --config-env=alias.x=GIT_CONFIG_COUNT",
        "git status --config-env alias.x=GIT_CONFIG_COUNT",
        "git status -c alias.x=sh",
    ],
)
def test_git_mutation_commands_are_classified_as_mutating(command: str) -> None:
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
        "git config --get alias.cleanup rm",
        "git diff",
        "git branch --list",
        "/usr/bin/git status --short",
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
        "git remote add origin https://example.invalid/repo.git",
        "git remote set-url origin https://example.invalid/repo.git",
        "sed -i s/a/b/ file.txt",
        "sed -n '1,4p' file.txt -i.bak",
        "sed -n '1,4p' file.txt --in-place=backup",
        "sed -n 'w output.txt' file.txt",
        "rg --pre rm pattern .",
        "rg --pre=rm pattern .",
        "rg --pre=rm=argument pattern .",
        "find . -fprint0 result.txt",
        "find . -fprintf result.txt %p",
        "find . -fls result.txt",
        "ruff check --fix .",
        "ruff format .",
        "pip install package",
        "npm install package",
        "pnpm add package",
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


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff",
        "git remote -v",
        "sed -n '1,4p' file.txt",
        "git log --oneline -5",
        "npm --version",
        "ruff --version",
    ],
)
def test_strict_explore_read_only_command_allowlist(command: str) -> None:
    assert shell_command_is_explore_safe(command), command


@pytest.mark.parametrize(
    "command",
    [
        "git remote add origin URL",
        "sed -n 'w output.txt' file.txt",
        "git status; touch file",
        "git status $(touch file)",
        "pytest -q > report.txt",
        "pytest -q || touch src/config.py",
        "pytest -q > report.txt && pytest -q",
        "pytest -q >> src/config.py",
        "pytest -q 2>& src/config.py",
        "pytest -q &> src/config.py",
        "pytest -q <<< payload",
        "pytest -q; touch src/config.py",
        "rg --pre rm pattern .",
        "rg --pre=rm pattern .",
        "rg --pre rm=arg pattern .",
        "rg --pre=rm=arg pattern .",
        "python -c 'print(1)'",
        "some-unknown-tool --help",
        "./git status",
        "./pytest -q",
        "./rg pattern .",
        "/workspace/bin/git status",
        "/tmp/pytest -q",
        "ruff check --fix .",
        "ruff check --config=/workspace/ruff.toml --no-cache .",
        "ruff check --output-format=json --no-cache .",
        "ruff check --no-cache --fix .",
        "ruff check --no-cache .",
        "npm test",
        "npm run test:unit",
        "git branch -lv",
        "git status --open-files-in-pager=sh",
        "git diff --ext-diff",
        "git diff --textconv",
        "git config --config-env=alias.x=GIT_CONFIG_COUNT",
        "git config --list --add user.name x",
        "go env -w GOPROXY=example",
        "go test -exec ./test-wrapper",
        "go test -exec=/workspace/bin/wrapper",
        "go test -exec /workspace/bin/wrapper",
        "go test -coverprofile=coverage.out -exec=/workspace/bin/wrapper",
        "gunicorn app:app -c /workspace/config.py",
        "gunicorn app:app --config /workspace/config.py",
        "uvicorn app:app --config=/workspace/config.yml",
        "uvicorn app:app --log-config /workspace/logging.ini",
        "find . -fprint results.txt",
        "find . -fprint0 results.txt",
        "find . -fprintf results.txt %p",
        "find . -fls results.txt",
        "find . -fprint0=results.txt",
        "find . -fprintfx results.txt %p",
        "find . -flsx results.txt",
        "sed -n '1w out.txt' file.txt",
        "sed -n '1,4p' file.txt -i.bak",
        "sed -n '1,4p' file.txt --in-place=backup",
        "sed -n 's/a/b/w out.txt' file.txt",
    ],
)
def test_strict_explore_rejects_unknown_or_mutating_commands(command: str) -> None:
    assert not shell_command_is_explore_safe(command), command


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q --junitxml=src/generated.py",
        "pytest -q --junitxml=src/config.json",
        "pytest -q --junitxml=src/config.yml",
        "pytest -q --junitxml=src/config.toml",
        "pytest -q --junitxml=src/notes.md",
        "pytest -q --junitxml=src/component.vue",
        "pytest -q --junitxml=src/component.ts",
        "pytest -q --junitxml=artifacts/config.json",
        "pytest -q --junitxml=src/generated.xml",
        "pytest -q --result-json=src/result.json",
        "pytest -q --basetemp=src/tmp",
        "pytest -q --cov-report=xml:src/coverage.xml",
        "pytest -q --cov-report=html:src/htmlcov",
        "ruff check --no-cache --output-file=src/config.json .",
        "ruff check --no-cache --output-file=reports/output.json .",
        "coverage xml -o src/coverage.json",
        "coverage html -d src/htmlcov",
        "npm run build -- --output-path=src/generated.vue",
        "npm run build -- --output-path=src/config.toml",
        "npm run build -- --config=artifacts/vite.config.ts",
        "npm run build -- --config=src/vite.config.ts",
        "pytest -q $(touch generated.py)",
        "pytest -q `touch generated.py`",
        "git remote add origin URL",
        "ruff check --fix .",
        "sed -i s/a/b/ file.txt",
        "npm install package",
        "python script.py",
        "echo report > out.txt",
        "npm run custom-task",
        "git remote add origin URL",
        "ruff check .",
        "ruff check --no-cache --fix .",
        "sed -n 'w out.txt' file.txt",
    ],
)
def test_generic_mutation_classifier_marks_unrecognized_writes(command: str) -> None:
    assert shell_command_may_mutate(command), command


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
