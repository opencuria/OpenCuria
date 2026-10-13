"""Conservative Bash policy classification for Claude Code hooks.

These command rules block common escape patterns, but are not a shell sandbox.
"""

from __future__ import annotations

import posixpath
import re
import shlex

_SHELL_META = frozenset({"<", ">", "|", ";", "&", "`", "$", "\\", "\n"})
_SHELL_WRAPPERS = frozenset(
    {"command", "env", "exec", "nohup", "nice", "stdbuf", "sudo", "time", "timeout", "xargs"}
)
_MUTATING_COMMANDS = frozenset(
    {"chmod", "chown", "cp", "dd", "install", "mkdir", "mv", "rm", "rmdir", "tee", "touch", "truncate", "unlink", "writefile"}
)
_READ_ONLY_COMMANDS = frozenset(
    {"cat", "cd", "grep", "head", "ls", "pwd", "rg", "stat", "tail", "test", "which", "wc"}
)
_READ_GIT = frozenset({"diff", "grep", "log", "ls-files", "rev-parse", "show", "status"})
_CONFIG_READ = frozenset({"--get", "--get-all", "--get-regexp", "--list", "--get-color"})
_ARTIFACT_DIRS = frozenset(
    {".pytest_cache", "artifacts", "build", "coverage", "dist", "reports", "tmp"}
)
_ARTIFACT_SUFFIXES = frozenset({".html", ".log", ".xml"})
_SOURCE_SUFFIXES = frozenset(
    {
        ".css",
        ".js",
        ".json",
        ".md",
        ".py",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".vue",
        ".yaml",
        ".yml",
    }
)


def _base(token: str) -> str:
    return token.rsplit("/", 1)[-1].lower()


def _trusted_executable(token: str) -> bool:
    """Allow builtins/PATH executables and common system bin paths, not cwd files."""
    if not token:
        return False
    if "/" not in token:
        return True
    normalized = posixpath.normpath(token)
    return normalized.startswith(("/bin/", "/usr/bin/", "/usr/local/bin/"))


def _shell_segments(command: str) -> list[list[str]] | None:
    """Split only on a standalone &&; reject all other shell operators."""
    if not command.strip() or any(char in command for char in ("$", "`", "\\", "\n")):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens:
        return []

    segments: list[list[str]] = [[]]
    for token in tokens:
        if token == "&&":
            if not segments[-1]:
                return None
            segments.append([])
        elif any(operator in token for operator in (";", "&", "|", "<", ">")):
            return None
        else:
            segments[-1].append(token)
    return segments if segments[-1] else None


def _git_branch_may_mutate(arguments: list[str]) -> bool:
    """Reject branch writes, unknown flags, and combined flags."""
    if not arguments:
        return False
    if any(
        arg in {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force"}
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
    value_flags = {"--merged", "--no-merged", "--contains", "--no-contains", "--points-at"}
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
            elif arg in value_flags and index + 1 < len(arguments) and not arguments[index + 1].startswith("-"):
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
        return any(arg in write_flags for arg in args) or not any(
            arg in _CONFIG_READ for arg in args
        ) or any(arg.startswith("--config-env") for arg in args)
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
        script
        and re.fullmatch(r"(?:\d+(?:,\d+)?p|p)(?:;(?:\d+(?:,\d+)?p|p))*", script)
    )


def _output_parts(path: str) -> tuple[list[str], str] | None:
    """Normalize a literal destination and return its path parts/suffix."""
    value = path.strip().strip("'\"")
    if not value or any(char in value for char in "$`\\\n"):
        return None
    normalized = value.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or ".." in parts:
        return None
    if parts[0].lower() == "workspace":
        parts = parts[1:]
    if not parts:
        return None
    suffix = "." + parts[-1].rsplit(".", 1)[-1].lower() if "." in parts[-1] else ""
    return parts, suffix


def _safe_output_path(path: str, *, directory: bool = False) -> bool:
    """Allow only clearly named reports below dedicated artifact roots."""
    parsed = _output_parts(path)
    if parsed is None:
        return False
    parts, suffix = parsed
    normalized = path.strip().strip("'\"").replace("\\", "/")
    in_tmp = normalized == "/tmp" or normalized.startswith("/tmp/")
    if parts[0].lower() == "tmp":
        artifact_root = True
    else:
        artifact_root = parts[0].lower() in _ARTIFACT_DIRS
    if not artifact_root and not in_tmp:
        return False
    if directory:
        # A source-like suffix is never treated as a directory name.
        return suffix not in _SOURCE_SUFFIXES
    return suffix in _ARTIFACT_SUFFIXES


def _option_value(tokens: list[str], index: int) -> tuple[str, int] | None:
    if "=" in tokens[index]:
        return tokens[index].split("=", 1)[1], index
    if index + 1 < len(tokens):
        return tokens[index + 1], index + 1
    return None


def _pytest_outputs_safe(tokens: list[str]) -> bool:
    """Validate pytest report, temporary-directory, and config output flags."""
    file_options = {"--junitxml", "--junit-xml", "--html", "--log-file", "--result-json"}
    directory_options = {"--basetemp", "--cache-dir"}
    index = 1
    while index < len(tokens):
        flag = tokens[index].lower().split("=", 1)[0]
        if flag.startswith(("-c", "--cache-clear", "--cov")):
            return False
        if flag in file_options | directory_options:
            value = _option_value(tokens, index)
            if value is None or not _safe_output_path(
                value[0], directory=flag in directory_options
            ):
                return False
            index = value[1] + 1
            continue
        if flag == "--cov-report":
            value = _option_value(tokens, index)
            if value is None:
                return False
            report = value[0].split(":", 1)
            report_type = report[0].lower()
            if report_type in {"term", "term-missing"} and len(report) == 1:
                index = value[1] + 1
                continue
            if (
                len(report) != 2
                or report_type not in {"html", "xml", "json", "lcov"}
                or not _safe_output_path(report[1], directory=report_type == "html")
            ):
                return False
            index = value[1] + 1
            continue
        if flag == "-o":
            value = _option_value(tokens, index)
            if value is None:
                return False
            setting = value[0].split("=", 1)
            if len(setting) != 2 or setting[0].lower() not in {
                "junit_xml",
                "cache_dir",
                "basetemp",
                "log_file",
            }:
                return False
            key = setting[0].lower()
            if not _safe_output_path(
                setting[1], directory=key in {"cache_dir", "basetemp"}
            ):
                return False
            index = value[1] + 1
            continue
        if flag.startswith(
            (
                "-o",
                "--override-ini",
                "--junitxml",
                "--junit-xml",
                "--html",
                "--log-file",
                "--result-json",
                "--basetemp",
                "--cache-dir",
                "--cov-report",
            )
        ):
            return False
        index += 1
    return True


def _ruff_outputs_safe(tokens: list[str]) -> bool:
    """Validate Ruff output-file destinations."""
    index = 1
    while index < len(tokens):
        flag = tokens[index].lower().split("=", 1)[0]
        if flag in {"--output-file", "-o"}:
            value = _option_value(tokens, index)
            if value is None or not _safe_output_path(value[0]):
                return False
            index = value[1] + 1
            continue
        if flag.startswith("--output-file"):
            return False
        index += 1
    return True


def _coverage_outputs_safe(tokens: list[str]) -> bool:
    """Validate coverage report and data destinations."""
    if len(tokens) < 2 or tokens[1].split("=", 1)[0] not in {"run", "report", "xml", "html", "json", "lcov"}:
        return False
    operation = tokens[1].split("=", 1)[0]
    index = 2
    while index < len(tokens):
        flag = tokens[index].lower().split("=", 1)[0]
        if flag.startswith("--data-file") and flag != "--data-file":
            return False
        if flag not in {"--data-file", "-o", "--output", "-d", "--directory"}:
            index += 1
            continue
        value = _option_value(tokens, index)
        if value is None or not _safe_output_path(
            value[0], directory=flag in {"-d", "--directory"} or operation == "html"
        ):
            return False
        index = value[1] + 1
    return True


def _safe_package_query(name: str, tokens: list[str]) -> bool:
    """Allow only recognized package-manager information queries."""
    args = tokens[1:]
    if name == "pip" or re.fullmatch(r"pip3(?:\.\d+)?", name):
        if not args:
            return False
        return args[0] in {"--version", "-V", "--help", "help", "list", "show", "freeze", "check"}
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


def shell_command_may_mutate(command: str) -> bool:
    """Return True for commands that may write or conceal writes."""
    if not command.strip():
        return False
    if any(char in command for char in _SHELL_META):
        segments = _shell_segments(command)
        if segments is None:
            return True
        if len(segments) > 1:
            return any(shell_command_may_mutate(shlex.join(segment)) for segment in segments)
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return True
    if not tokens:
        return False
    names = [_base(token) for token in tokens]
    name = names[0]
    if name in _SHELL_WRAPPERS:
        return not (name == "command" and len(tokens) > 1 and tokens[1] in {"-v", "-V"})
    if not _trusted_executable(tokens[0]):
        return True
    if name in _MUTATING_COMMANDS:
        return True
    if name == "find":
        return _find_may_mutate(tokens[1:])
    if name == "rg" and any(
        token == "--pre" or token.startswith("--pre=") for token in names[1:]
    ):
        return True
    if name in {"python", "python3"} and len(tokens) > 1:
        return True
    if name in {"gunicorn", "uvicorn"} and any(
        arg == "-c" or arg.startswith(("--config", "--log-config"))
        for arg in names[1:]
    ):
        return True
    if name in {"bash", "sh", "node", "ruby", "perl"}:
        return len(tokens) > 1
    if name == "pip" or re.fullmatch(r"pip3(?:\.\d+)?", name):
        return not _safe_package_query(name, names)
    if name == "git":
        return _git_may_mutate(names[1:])
    if name == "sed":
        return not _sed_is_print_only(tokens)
    if name in {"ruff"}:
        if len(names) > 1 and names[1] in {"--version", "-v"}:
            return False
        return not (
            len(names) > 2
            and names[1] == "check"
            and _ruff_flags_are_safe(names[2:])
            and "--no-cache" in names[2:]
            and not any(arg in {"--fix", "--fix-only", "--unsafe-fixes"} for arg in names[2:])
            and _ruff_outputs_safe(tokens)
        )
    if name in {"pytest", "py.test"}:
        return not _pytest_outputs_safe(tokens)
    if name in {"tox", "nox"}:
        return True
    if name == "coverage":
        return not _coverage_outputs_safe(tokens)
    if name in {"npm", "pnpm", "yarn"}:
        return not _safe_package_query(name, names)
    if name == "cargo":
        return len(names) < 2 or names[1] not in {"-v", "--version", "metadata", "tree"}
    if name == "go":
        if len(names) < 2:
            return True
        if names[1] == "env":
            return "-w" in names or (len(names) > 2 and "-json" not in names)
        return names[1] not in {"version", "list"}
    if name == "make":
        return not any(arg in {"-n", "--dry-run", "--just-print", "--what-if"} for arg in names[1:])
    return name not in _READ_ONLY_COMMANDS


def shell_command_is_explore_safe(command: str) -> bool:
    """Allow simple read research only; deny tests, lint and preprocessors."""
    if not command.strip():
        return False
    if any(char in command for char in _SHELL_META):
        if _shell_segments(command) is None:
            return False
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
    if name in {"gunicorn", "uvicorn"} and any(
        arg == "-c" or arg.startswith("--config") for arg in names[1:]
    ):
        return False
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


def _ruff_flags_are_safe(tokens: list[str]) -> bool:
    """Reject arbitrary Ruff config and alternative output destinations."""
    index = 1
    while index < len(tokens):
        flag = tokens[index].split("=", 1)[0]
        if flag in {"--config", "--config-file"} or flag.startswith(
            ("--config=", "--config-file=")
        ):
            return False
        if flag in {"--output-format", "--statistics"}:
            return False
        if flag == "--output-file":
            value = _option_value(tokens, index)
            if value is None or not _safe_output_path(value[0]):
                return False
            index = value[1] + 1
            continue
        if flag.startswith("--output-file="):
            value = tokens[index].split("=", 1)[1]
            if not _safe_output_path(value):
                return False
        elif flag.startswith("--output-file"):
            return False
        index += 1
    return True


__all__ = [
    "shell_command_is_explore_safe",
    "shell_command_may_mutate",
]
