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
_PLAN_NPM = frozenset(
    {
        ("test",),
        ("t",),
        ("dev",),
        ("build",),
        ("serve",),
        ("start",),
        ("preview",),
        ("typecheck",),
        ("run", "test"),
        ("run", "test:unit"),
        ("run", "test:integration"),
        ("run", "test:e2e"),
        ("run", "typecheck"),
        ("run", "build"),
        ("run", "dev"),
        ("run", "serve"),
        ("run", "start"),
        ("run", "preview"),
    }
)
_PLAN_SERVERS = frozenset(
    {"flask", "gunicorn", "http-server", "next", "uvicorn", "vite", "webpack"}
)
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


def _safe_output_destination(path: str) -> bool:
    """Accept a safe report file or a dedicated artifact directory."""
    parsed = _output_parts(path)
    if parsed is None:
        return False
    _parts, suffix = parsed
    if suffix:
        return _safe_output_path(path)
    return _safe_output_path(path, directory=True)


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


def _go_outputs_safe(tokens: list[str]) -> bool:
    """Validate Go coverage profile output destinations."""
    index = 1
    while index < len(tokens):
        if tokens[index].startswith("-coverprofile="):
            if not _safe_output_path(tokens[index].split("=", 1)[1]):
                return False
        elif tokens[index] == "-coverprofile":
            value = _option_value(tokens, index)
            if value is None or not _safe_output_path(value[0]):
                return False
            index = value[1]
        index += 1
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


def _npm_script_workflow(tokens: list[str]) -> bool:
    """Recognize a narrow set of project workflows and validate output flags."""
    if not tokens or _base(tokens[0]) not in {"npm", "pnpm", "yarn"}:
        return False
    args = [_base(token) for token in tokens[1:]]
    if not args:
        return False
    if args[0] == "run" and len(args) > 1:
        script = ("run", args[1])
    else:
        script = (args[0],)
    return script in _PLAN_NPM and _generic_outputs_safe(tokens)


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


def _workflow_flags_are_safe(tokens: list[str]) -> bool:
    """Reject config overrides and external execution wrappers in workflows."""
    names = [_base(token) for token in tokens]
    name = names[0]
    if name == "go" and any(
        token == "-exec" or token.startswith("-exec=") for token in names[1:]
    ):
        return False
    if name == "cargo" and any(
        token.startswith("--config") for token in names[1:]
    ):
        return False
    if name in {"gunicorn", "uvicorn"} and any(
        token == "-c" or token.startswith(("--config", "--log-config"))
        for token in names[1:]
    ):
        return False
    if name == "git" and _git_may_mutate(names[1:]):
        return False
    return True


def _plan_workflow(tokens: list[str]) -> bool:
    """Recognize bounded test/build workflows and conventional dev servers."""
    if not tokens or not _trusted_executable(tokens[0]):
        return False
    if not _workflow_flags_are_safe(tokens):
        return False
    names = [_base(token) for token in tokens]
    name, args = names[0], names[1:]
    if name in {"pytest", "py.test"}:
        return not any(arg in {"-c", "--cov", "--cache-clear"} for arg in args) and _pytest_outputs_safe(tokens)
    if name in {"tox", "nox"}:
        return not any(arg.startswith(("--conf", "--sitepackages", "--installpkg")) for arg in args)
    if name in _PLAN_SERVERS:
        return not any(arg in {"--help", "--version"} for arg in args) and _generic_outputs_safe(tokens)
    if name in {"python", "python3"}:
        if len(args) > 1 and args[0] == "-m":
            if args[1] == "pytest":
                pytest_command = ["pytest", *tokens[3:]]
                return _plan_workflow(pytest_command)
            if args[1] in {"http.server", "uvicorn", "unittest"}:
                return _generic_outputs_safe(tokens)
        if len(args) > 1 and args[0].endswith("manage.py") and args[1] in {"runserver", "test"}:
            if args[1] == "test":
                return _plan_workflow(["pytest", *args[2:]])
            return _generic_outputs_safe(tokens)
    if name == "uv" and len(args) > 1 and args[0] == "run":
        return _plan_workflow(tokens[2:])
    if name in {"npm", "pnpm", "yarn"}:
        return _npm_script_workflow(tokens)
    if name in {"jest", "vitest", "mocha", "playwright", "cypress"}:
        return any(arg in {"test", "run", "open"} for arg in args) and _generic_outputs_safe(tokens)
    if name == "coverage":
        return _coverage_outputs_safe(tokens)
    if name == "ruff":
        return (
            len(args) > 0
            and args[0] == "check"
            and not shell_command_may_mutate(shlex.join(tokens))
            and _ruff_outputs_safe(tokens)
        )
    if name == "make":
        return len(args) == 1 and args[0] in {"test", "build", "check", "serve", "dev"}
    if name == "cargo":
        return len(args) == 1 and args[0] in {"test", "build", "check"} and _generic_outputs_safe(tokens)
    if name == "go":
        return (
            bool(args)
            and args[0] == "test"
            and not any(arg == "-exec" or arg.startswith("-exec=") for arg in args)
            and _go_outputs_safe(tokens)
        )
    return False


def _generic_outputs_safe(tokens: list[str]) -> bool:
    """Validate common output-file and directory options on plan workflows."""
    destination_flags = {
        "-o",
        "--output",
        "--out",
        "--output-file",
        "--output-path",
        "--outfile",
        "--file",
        "--junitxml",
        "--html",
        "--output-dir",
        "--output-directory",
        "--out-dir",
        "--outdir",
        "--target-dir",
        "--dist-dir",
        "--dir",
        "--dest",
    }
    directory_flags = {
        "--output-path",
        "--output-dir",
        "--output-directory",
        "--out-dir",
        "--outdir",
        "--target-dir",
        "--dist-dir",
        "--dir",
        "--dest",
    }
    config_flags = {"--config", "--config-file", "--webpack-config"}
    index = 1
    while index < len(tokens):
        flag = tokens[index].lower().split("=", 1)[0]
        if flag in {"-exec", "--config-env", "--config-file", "-c"} or flag.startswith(
            ("--config-env=", "--config-file=")
        ):
            return False
        if flag in config_flags or flag.startswith("--config") or flag.startswith(
            "--webpack-config"
        ):
            # Build-system config overrides may redirect or enable generated
            # outputs; require approval rather than infer config semantics.
            return False
        if flag in destination_flags:
            value = _option_value(tokens, index)
            if value is None:
                return False
            safe = (
                _safe_output_path(value[0], directory=True)
                if flag in directory_flags
                else _safe_output_destination(value[0])
            )
            if not safe:
                return False
            index = value[1] + 1
            continue
        if flag.startswith(
            (
                "--config=",
                "--config-file=",
                "--webpack-config=",
                "--output",
                "--out-dir",
                "--outdir",
                "--outfile",
                "--target-dir",
                "--dist-dir",
                "--junitxml",
                "--html",
                "--config",
                "--config-file",
                "--webpack-config",
            )
        ):
            return False
        index += 1
    return True


def _workflow_outputs_are_safe(tokens: list[str]) -> bool:
    """Validate explicit output destinations for a plan workflow."""
    if not _plan_workflow(tokens):
        return False
    name = _base(tokens[0])
    if name in {"npm", "pnpm", "yarn"}:
        return _npm_script_workflow(tokens)
    if name in {"pytest", "py.test", "tox", "nox"}:
        return _pytest_outputs_safe(tokens)
    if name == "ruff":
        return _ruff_outputs_safe(tokens)
    if name == "coverage":
        return _coverage_outputs_safe(tokens)
    return _generic_outputs_safe(tokens)


def shell_command_is_plan_workflow(command: str) -> bool:
    """Return True when every shell-free && segment is a supported workflow."""
    segments = _shell_segments(command)
    return segments is not None and all(
        (len(segment) > 1 and _base(segment[0]) == "cd") or _plan_workflow(segment)
        for segment in segments
    )


def shell_command_requires_plan_approval(command: str) -> bool:
    """Ask before mutations outside safe workflows and harmless reads."""
    segments = _shell_segments(command)
    if segments is None:
        return bool(command.strip())
    for segment in segments:
        if len(segment) > 1 and _base(segment[0]) == "cd":
            continue
        if _workflow_outputs_are_safe(segment):
            continue
        if not shell_command_may_mutate(shlex.join(segment)):
            continue
        return True
    return False


__all__ = [
    "shell_command_is_explore_safe",
    "shell_command_is_plan_workflow",
    "shell_command_may_mutate",
    "shell_command_requires_plan_approval",
]
