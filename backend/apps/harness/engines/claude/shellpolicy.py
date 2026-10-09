"""Conservative Bash mutation classification for Claude policy decisions."""

from __future__ import annotations

import shlex

_SHELL_WRAPPERS = frozenset(
    {
        "command",
        "env",
        "exec",
        "nohup",
        "nice",
        "stdbuf",
        "sudo",
        "time",
        "timeout",
        "xargs",
    }
)
_READ_ONLY_COMMANDS = frozenset(
    {
        "cat",
        "cd",
        "find",
        "git",
        "grep",
        "head",
        "ls",
        "pwd",
        "rg",
        "sed",
        "stat",
        "tail",
        "test",
        "which",
        "wc",
        "pytest",
        "python",
        "python3",
        "ruff",
        "mypy",
        "pnpm",
        "yarn",
        "cargo",
        "go",
    }
)
_MUTATING_COMMANDS = frozenset(
    {
        "chmod",
        "chown",
        "cp",
        "dd",
        "install",
        "mkdir",
        "mv",
        "rm",
        "rmdir",
        "tee",
        "touch",
        "truncate",
        "unlink",
        "writefile",
    }
)
_GIT_BRANCH_LIST_FLAGS = frozenset(
    {
        "-a",
        "--all",
        "-r",
        "--remotes",
        "-l",
        "--list",
        "--merged",
        "--no-merged",
        "--contains",
        "--no-contains",
        "--points-at",
        "--format",
        "--sort",
        "-v",
        "-vv",
    }
)
_GIT_BRANCH_VALUE_FLAGS = frozenset(
    {"--merged", "--no-merged", "--contains", "--no-contains", "--points-at"}
)
_GIT_BRANCH_REQUIRED_VALUE_FLAGS = frozenset({"--format", "--sort"})
_GIT_READ_ONLY = frozenset(
    {"diff", "grep", "log", "ls-files", "remote", "rev-parse", "show", "status"}
)
_GIT_CONFIG_READ_FLAGS = frozenset(
    {"--get", "--get-all", "--get-regexp", "--list", "--get-color"}
)


def _git_branch_may_mutate(arguments: list[str]) -> bool:
    """Treat branch creation/deletion/move/copy options as writes."""
    if not arguments:
        return False
    if any(
        arg
        in {
            "-d",
            "-D",
            "--delete",
            "-m",
            "-M",
            "--move",
            "-c",
            "-C",
            "--copy",
            "-f",
            "--force",
        }
        for arg in arguments
    ):
        return True

    listing = False
    positional: list[str] = []
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        if arg in _GIT_BRANCH_LIST_FLAGS:
            listing = True
            if arg in _GIT_BRANCH_REQUIRED_VALUE_FLAGS:
                if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                    return True
                index += 2
                continue
            if arg in _GIT_BRANCH_VALUE_FLAGS and index + 1 < len(arguments):
                if not arguments[index + 1].startswith("-"):
                    index += 2
                    continue
        elif arg.startswith("-"):
            return True
        else:
            positional.append(arg)
        index += 1

    # A positional name normally creates a branch. With an explicit listing
    # selector, operands are commit-ish filters or patterns instead.
    return bool(positional) and not listing


def _git_may_mutate(arguments: list[str]) -> bool:
    """Classify Git commands with subcommand-specific write flags."""
    if not arguments:
        return True
    subcommand, *rest = arguments
    subcommand = subcommand.lower()
    if subcommand == "branch":
        return _git_branch_may_mutate(rest)
    if subcommand == "config":
        return not any(arg in _GIT_CONFIG_READ_FLAGS for arg in rest)
    if subcommand in _GIT_READ_ONLY:
        return any(arg == "--output" or arg.startswith("--output=") for arg in rest)
    return True


def shell_command_may_mutate(command: str) -> bool:
    """Return whether a Bash command may change files or hide its command.

    Unknown commands and wrappers fail closed. This is a policy classifier,
    not an attempt to emulate Bash's complete command grammar.
    """
    if not command.strip():
        return False
    if any(char in command for char in (">", "<", "|", ";", "&", "`", "$", "\\", "\n")):
        return True
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return True
    if not tokens:
        return False

    command_name = tokens[0].rsplit("/", 1)[-1].lower()
    if command_name in _SHELL_WRAPPERS:
        # These wrappers can obscure the child or alter the process environment.
        return not (
            command_name == "command" and len(tokens) > 1 and tokens[1] in {"-v", "-V"}
        )

    lowered = [token.rsplit("/", 1)[-1].lower() for token in tokens]
    if command_name in _MUTATING_COMMANDS or any(
        token in {"-delete", "-exec", "-execdir"} for token in lowered
    ):
        return True
    if command_name in {"python", "python3", "bash", "sh", "node", "ruby", "perl"}:
        return len(tokens) > 1
    if command_name in {
        "grep",
        "rg",
        "cat",
        "head",
        "tail",
        "find",
        "ls",
        "pwd",
        "wc",
        "stat",
        "which",
        "test",
    }:
        return False
    if command_name == "git":
        return _git_may_mutate(lowered[1:])
    if command_name == "sed":
        return any(token == "-i" or token.startswith("--in-place") for token in lowered)
    if command_name == "npm":
        if len(lowered) < 2:
            return True
        if lowered[1] in {"--version", "-v", "version", "root"}:
            return False
        # Package scripts can mutate regardless of a friendly script name.
        return lowered[1] != "test"
    if command_name == "make":
        return not any(
            token in {"-n", "--dry-run", "--just-print", "--what-if"}
            for token in lowered[1:]
        )
    return command_name not in _READ_ONLY_COMMANDS


__all__ = ["shell_command_may_mutate"]
