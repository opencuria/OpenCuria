"""Read-only Bash allowlist for Explore tools and provider hooks.

These command rules block common escape patterns, but are not a shell sandbox.
"""

from __future__ import annotations

import posixpath
import re
import shlex

_SHELL_META = frozenset({"<", ">", "|", ";", "&", "`", "$", "\\", "\n"})
_READ_ONLY_COMMANDS = frozenset(
    {
        "cat",
        "cd",
        "grep",
        "head",
        "ls",
        "pwd",
        "rg",
        "stat",
        "tail",
        "test",
        "which",
        "wc",
    }
)
_READ_GIT = frozenset(
    {"diff", "grep", "log", "ls-files", "rev-parse", "show", "status"}
)
_CONFIG_READ = frozenset(
    {"--get", "--get-all", "--get-regexp", "--list", "--get-color"}
)


def _base(token: str) -> str:
    """Normalize a command basename for conservative policy classification."""
    return token.rsplit("/", 1)[-1].lower()


def _trusted_executable(token: str) -> bool:
    """Allow builtins/PATH executables and common system bin paths, not cwd files."""
    if not token:
        return False
    if "/" not in token:
        return True
    normalized = posixpath.normpath(token)
    return normalized.startswith(("/bin/", "/usr/bin/", "/usr/local/bin/"))


def _git_branch_may_mutate(arguments: list[str]) -> bool:
    """Reject branch writes, unknown flags, and combined flags."""
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
    list_flags = {
        "-l",
        "--list",
        "-a",
        "--all",
        "-r",
        "--remotes",
        "-v",
        "-vv",
        "--merged",
        "--no-merged",
        "--contains",
        "--no-contains",
        "--points-at",
        "--format",
        "--sort",
    }
    value_flags = {
        "--merged",
        "--no-merged",
        "--contains",
        "--no-contains",
        "--points-at",
    }
    listing = False
    positional: list[str] = []
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        if arg in list_flags:
            listing = True
            if arg in {"--format", "--sort"}:
                if index + 1 >= len(arguments):
                    return True
                index += 1
            elif (
                arg in value_flags
                and index + 1 < len(arguments)
                and not arguments[index + 1].startswith("-")
            ):
                index += 1
        elif arg.startswith("-"):
            return True
        else:
            positional.append(arg)
        index += 1
    return bool(positional) and not listing


def _git_may_mutate(arguments: list[str]) -> bool:
    """Classify known Git reads; fail closed for all unknown commands."""
    if not arguments:
        return True
    command, *args = arguments
    command = command.lower()
    if command == "branch":
        return _git_branch_may_mutate(args)
    if command == "remote":
        return any(arg not in {"-v", "--verbose"} for arg in args)
    if command == "config":
        write_flags = {
            "--add",
            "--append",
            "--replace-all",
            "--unset",
            "--unset-all",
            "--remove-section",
            "--rename-section",
            "--edit",
            "-e",
        }
        return (
            any(arg in write_flags for arg in args)
            or not any(arg in _CONFIG_READ for arg in args)
            or any(arg.startswith("--config-env") for arg in args)
        )
    if command in _READ_GIT:
        execution_options = {
            "-o",
            "-c",
            "--ext-diff",
            "--textconv",
            "--open-files-in-pager",
            "--config-env",
        }
        return any(
            arg in execution_options
            or arg == "--output"
            or arg.startswith(("--output=", "--open-files-in-pager=", "--config-env"))
            for arg in args
        )
    return True


def _find_may_mutate(arguments: list[str]) -> bool:
    """Detect find command execution and every file-output option variant."""
    for token in arguments:
        flag = token.lower()
        if flag in {"-delete", "-exec", "-execdir", "-ok", "-okdir"}:
            return True
        if flag.startswith(("-fprint", "-fprintf", "-fls")):
            return True
    return False


def _sed_is_print_only(tokens: list[str]) -> bool:
    """Allow a small print-only sed grammar and reject in-place switches."""
    script: str | None = None
    read_switches = {"-n", "--quiet", "--silent", "-E", "-r", "--regexp-extended"}
    for token in tokens[1:]:
        flag = token.lower()
        # GNU sed accepts -iSUFFIX and --in-place=SUFFIX. Inspect the entire
        # argument list, including tokens after the script and input files.
        if flag.startswith("-i") or flag.startswith("--in-place"):
            return False
        if script is None and token in read_switches:
            continue
        if token.startswith("-"):
            return False
        if script is None:
            script = token
    return bool(
        script and re.fullmatch(r"(?:\d+(?:,\d+)?p|p)(?:;(?:\d+(?:,\d+)?p|p))*", script)
    )


def _safe_package_query(name: str, tokens: list[str]) -> bool:
    """Allow only recognized package-manager information queries."""
    args = tokens[1:]
    if name == "pip" or re.fullmatch(r"pip3(?:\.\d+)?", name):
        if not args:
            return False
        return args[0] in {
            "--version",
            "-V",
            "--help",
            "help",
            "list",
            "show",
            "freeze",
            "check",
        }
    if name in {"npm", "pnpm", "yarn"}:
        if not args:
            return False
        return args[0] in {
            "--version",
            "-v",
            "--help",
            "help",
            "root",
            "list",
            "ls",
            "view",
            "info",
            "show",
            "outdated",
            "explain",
            "why",
        }
    return False


def shell_command_is_explore_safe(command: str) -> bool:
    """Allow simple read research only; deny tests, lint and preprocessors."""
    if not command.strip():
        return False
    if any(char in command for char in _SHELL_META):
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False
    if not _trusted_executable(tokens[0]):
        return False
    names = [_base(token) for token in tokens]
    name = names[0]
    if name == "rg" and any(
        token == "--pre" or token.startswith("--pre=") for token in names[1:]
    ):
        return False
    if name in _READ_ONLY_COMMANDS:
        return True
    if name == "find":
        return not _find_may_mutate(tokens[1:])
    if name == "git":
        return not _git_may_mutate(names[1:])
    if name == "sed":
        return _sed_is_print_only(tokens)
    if name == "pip" or re.fullmatch(r"pip3(?:\.\d+)?", name):
        return _safe_package_query(name, names)
    if name in {"npm", "pnpm", "yarn"}:
        return _safe_package_query(name, names)
    if name == "ruff":
        return len(names) > 1 and names[1] in {"--version", "-v"}
    if name == "command":
        return len(tokens) > 1 and tokens[1] in {"-v", "-V"}
    return False


__all__ = ["shell_command_is_explore_safe"]
