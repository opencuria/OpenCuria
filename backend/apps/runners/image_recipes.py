"""Render image recipes into the init script a runner runs during a build.

Every guest OS shares the recipe fields (packages, environment variables and a
custom script) and the build flow; one small renderer per guest OS turns them
into the guest's script language. Linux renders bash (apt, ``/etc/profile.d``,
then the desktop session), Windows renders PowerShell (winget, machine
environment variables). The runner receives the result as ``init_script`` and
runs it with the guest's shell.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from .exceptions import ImageRecipeValidationError
from .guest_os import LINUX, WINDOWS, distro_guest_os, guest_os_for_distro

#: winget package identifiers, e.g. ``Git.Git`` or ``OpenJS.NodeJS.LTS``.
WINGET_PACKAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*")
#: Environment variable names every guest shell accepts.
ENV_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: Characters PowerShell treats as single quotes inside a verbatim string.
_POWERSHELL_QUOTES = "'‘’‚‛"
_DESKTOP_SESSION_SCRIPT = (
    Path(__file__).resolve().parent / "scripts" / "qemu_desktop_session.sh"
)


def _recipe_packages(definition: Any) -> list[str]:
    return [p.strip() for p in list(definition.packages or []) if p.strip()]


def validate_recipe(
    base_distro: str, packages: Iterable[str], env_vars: Mapping[str, Any]
) -> None:
    """Reject Windows recipe values that are not winget IDs or variable names.

    Linux recipes keep their historical, unvalidated package and variable
    values so existing recipes stay editable.
    """
    if distro_guest_os(base_distro) != WINDOWS:
        return
    for package in packages:
        if package.strip() and not WINGET_PACKAGE_ID.fullmatch(package.strip()):
            raise ImageRecipeValidationError(
                f"Invalid winget package ID {package!r}, e.g. use Git.Git",
                code="invalid_package_id",
            )
    for name in env_vars:
        if not ENV_VAR_NAME.fullmatch(name):
            raise ImageRecipeValidationError(
                f"Invalid environment variable name {name!r}; use letters, "
                "digits and underscores and do not start with a digit",
                code="invalid_env_var_name",
            )


def desktop_session_init_script_block() -> str:
    """Return shell script lines that install the QEMU KasmVNC desktop."""
    return "\n" + _DESKTOP_SESSION_SCRIPT.read_text(encoding="utf-8").strip() + "\n"


def render_linux_init_script(definition: Any) -> str:
    """Bash: apt packages, ``/etc/profile.d`` environment, custom steps, desktop."""
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        "",
    ]

    packages = _recipe_packages(definition)
    if packages:
        lines += [
            "export DEBIAN_FRONTEND=noninteractive",
            "apt-get update",
            f"apt-get install -y {' '.join(packages)}",
            "rm -rf /var/lib/apt/lists/*",
            "",
        ]

    env_vars = dict(definition.env_vars or {})
    if env_vars:
        lines += [
            "cat >/etc/profile.d/opencuria-image-env.sh <<'EOF'",
            "#!/bin/sh",
        ]
        for key, value in env_vars.items():
            if key:
                escaped = str(value).replace('"', '\\"')
                lines.append(f'export {key}="{escaped}"')
        lines += [
            "EOF",
            "chmod 644 /etc/profile.d/opencuria-image-env.sh",
            "",
        ]

    custom_script = (definition.custom_init_script or "").strip()
    if custom_script:
        lines += [
            "# Custom image definition steps",
            custom_script,
            "",
        ]

    lines += [desktop_session_init_script_block(), ""]

    return "\n".join(lines).strip() + "\n"


def powershell_literal(value: object) -> str:
    """Quote ``value`` as a verbatim PowerShell string (no expansion)."""
    text = str(value)
    for quote in _POWERSHELL_QUOTES:
        text = text.replace(quote, quote * 2)
    return f"'{text}'"


def render_windows_init_script(definition: Any) -> str:
    """PowerShell: winget packages, machine environment, custom steps.

    The Windows desktop is provided by the runner, so no desktop block follows.
    """
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        "",
    ]

    packages = _recipe_packages(definition)
    for package in packages:
        package_id = powershell_literal(package)
        lines += [
            f"winget install --id {package_id} --exact --silent --scope machine "
            "--accept-package-agreements --accept-source-agreements "
            "--disable-interactivity",
            "if ($LASTEXITCODE -ne 0) { throw ('winget install ' + "
            f"{package_id} + ' failed with exit code ' + $LASTEXITCODE) }}",
        ]
    if packages:
        # Later steps of this build see the commands the packages installed.
        lines += [
            "$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') "
            "+ ';' + [Environment]::GetEnvironmentVariable('Path', 'User')",
            "",
        ]

    env_vars = dict(definition.env_vars or {})
    if env_vars:
        for key, value in env_vars.items():
            if key:
                lines.append(
                    "[Environment]::SetEnvironmentVariable("
                    f"{powershell_literal(key)}, {powershell_literal(value)}, "
                    "'Machine')"
                )
        lines.append("")

    custom_script = (definition.custom_init_script or "").strip()
    if custom_script:
        lines += [
            "# Custom image definition steps",
            custom_script,
            "",
        ]

    return "\n".join(lines).strip() + "\n"


_RENDERERS: dict[str, Callable[[Any], str]] = {
    LINUX: render_linux_init_script,
    WINDOWS: render_windows_init_script,
}


def render_init_script(definition: Any) -> str:
    """Render a definition with the renderer of the guest OS it builds."""
    return _RENDERERS[guest_os_for_distro(definition.base_distro)](definition)
