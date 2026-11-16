"""Image recipe rendering per guest OS and recipe validation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.runners.exceptions import ImageRecipeValidationError
from apps.runners.image_recipes import (
    powershell_literal,
    render_init_script,
    validate_recipe,
)
from apps.runners.models import ImageDefinition, ImageInstance
from apps.runners.services import RunnerService


def recipe(base_distro: str, **fields) -> SimpleNamespace:
    values = {"packages": [], "env_vars": {}, "custom_init_script": ""}
    values.update(fields)
    return SimpleNamespace(base_distro=base_distro, **values)


def test_linux_recipe_renders_bash_with_the_desktop_block():
    script = render_init_script(
        recipe(
            "ubuntu:24.04",
            packages=["git", " python3 ", ""],
            env_vars={"A": 'x"y'},
            custom_init_script="echo hi\n",
        )
    )

    assert script.startswith(
        "#!/bin/bash\nset -euo pipefail\n\n"
        "export DEBIAN_FRONTEND=noninteractive\napt-get update\n"
        "apt-get install -y git python3\nrm -rf /var/lib/apt/lists/*\n\n"
        "cat >/etc/profile.d/opencuria-image-env.sh <<'EOF'\n#!/bin/sh\n"
        'export A="x\\"y"\nEOF\n'
        "chmod 644 /etc/profile.d/opencuria-image-env.sh\n\n"
        "# Custom image definition steps\necho hi\n\n"
    )
    assert "startxfce4" in script
    assert "winget" not in script


def test_windows_recipe_renders_powershell_without_a_desktop_block():
    script = render_init_script(
        recipe(
            "windows:11-enterprise",
            packages=["OpenJS.NodeJS.LTS", " Git.Git "],
            env_vars={"NODE_ENV": "it's $HOME"},
            custom_init_script="Write-Host 'done'\n",
        )
    )

    install = (
        "winget install --id '{id}' --exact --silent --scope machine "
        "--accept-package-agreements --accept-source-agreements "
        "--disable-interactivity\n"
        "if ($LASTEXITCODE -ne 0) {{ throw ('winget install ' + '{id}' + "
        "' failed with exit code ' + $LASTEXITCODE) }}\n"
    )
    assert script == (
        "$ErrorActionPreference = 'Stop'\n"
        "$ProgressPreference = 'SilentlyContinue'\n\n"
        + install.format(id="OpenJS.NodeJS.LTS")
        + install.format(id="Git.Git")
        + "$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') "
        "+ ';' + [Environment]::GetEnvironmentVariable('Path', 'User')\n\n"
        "[Environment]::SetEnvironmentVariable('NODE_ENV', 'it''s $HOME', "
        "'Machine')\n\n"
        "# Custom image definition steps\nWrite-Host 'done'\n"
    )
    assert "#!/bin/bash" not in script and "startxfce4" not in script


def test_empty_windows_recipe_only_sets_the_error_policy():
    assert render_init_script(recipe("windows:11")) == (
        "$ErrorActionPreference = 'Stop'\n$ProgressPreference = 'SilentlyContinue'\n"
    )


def test_powershell_literals_never_expand_or_end_early():
    assert powershell_literal("a'b") == "'a''b'"
    # PowerShell also ends verbatim strings at typographic single quotes.
    assert powershell_literal("‘x’ ‚y‛") == "'‘‘x’’ ‚‚y‛‛'"
    assert powershell_literal("$env:X `n") == "'$env:X `n'"


@pytest.mark.parametrize(
    ("base_distro", "packages", "env_vars", "code"),
    [
        ("windows:11", ["Git.Git; rm"], {}, "invalid_package_id"),
        ("windows:11", ["-Git"], {}, "invalid_package_id"),
        ("windows:11", ["Git.Git"], {"BAD-NAME": "1"}, "invalid_env_var_name"),
        ("windows:11", [], {"1ST": "1"}, "invalid_env_var_name"),
        ("ubuntu:24.04", ["libc6:amd64"], {"MY-VAR": "1"}, None),
        ("windows:11", ["Microsoft.VisualStudioCode", "Python.Python.3+12"], {}, None),
    ],
)
def test_recipe_validation(base_distro, packages, env_vars, code):
    if code is None:
        validate_recipe(base_distro, packages, env_vars)
        return
    with pytest.raises(ImageRecipeValidationError) as caught:
        validate_recipe(base_distro, packages, env_vars)
    assert caught.value.code == code


@pytest.mark.django_db(transaction=True)
async def test_windows_build_sends_the_powershell_script(runner, user):
    sio = AsyncMock()
    service = RunnerService(sio_server=sio)
    definition = await ImageDefinition.objects.acreate(
        organization=runner.organization,
        created_by=user,
        name="Windows build",
        base_distro="windows:11",
        packages=["Git.Git"],
    )

    await service.image_lifecycle.trigger_build_job(
        image_definition=definition, runner=runner, created_by=user
    )

    event, payload = sio.emit.await_args.args[:2]
    assert event == "task:build_image"
    assert payload["base_distro"] == "windows:11"
    assert payload["init_script"].startswith("$ErrorActionPreference = 'Stop'")
    assert "--id 'Git.Git'" in payload["init_script"]
    image = await ImageInstance.objects.aget(id=payload["image_instance_id"])
    assert image.guest_os == "windows"
