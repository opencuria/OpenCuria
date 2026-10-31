"""Desktop .deb dependency repair must never publish a missing application."""

import subprocess
from pathlib import Path

import pytest

RECIPE = Path(__file__).parents[1] / "scripts" / "qemu_desktop_session.sh"


@pytest.mark.parametrize("application", ["kasmvnc", "google-chrome"])
@pytest.mark.parametrize("failure", [None, "removed-package", "missing-command"])
def test_repaired_desktop_install_requires_installed_package_and_binary(
    tmp_path, application, failure
):
    recipe = RECIPE.read_text()
    helpers = recipe.split("require_installed_package() {", 1)[1].split(
        "apt-get update", 1
    )[0]
    helpers = "require_installed_package() {" + helpers
    archive = (
        "/tmp/kasmvnc.deb" if application == "kasmvnc" else "/tmp/google-chrome.deb"
    )
    install = recipe.split(f"apt-get install -y {archive}", 1)[1].split(
        f"rm -f {archive}", 1
    )[0]
    install = f"apt-get install -y {archive}" + install
    binaries = (
        ["Xvnc", "vncserver"] if application == "kasmvnc" else ["google-chrome-stable"]
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    def executable(name, body):
        path = fake_bin / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    # The first install fails; dependency repair reports success after removing
    # the intended application. This is apt's valid but unusable repair outcome.
    executable(
        "apt-get", 'if [ "$1" = install ] && [ "$2" = -f ]; then exit 0; fi\nexit 1'
    )
    status = (
        "deinstall ok config-files"
        if failure == "removed-package"
        else "install ok installed"
    )
    executable("dpkg-query", f"printf '%s' '{status}'")
    if failure != "missing-command":
        for binary in binaries:
            executable(binary, "printf '%s\\n' 'application probe succeeded'")
    completed = subprocess.run(
        ["/bin/bash", "-c", "set -euo pipefail\n" + helpers + install],
        env={"PATH": str(fake_bin)},
        capture_output=True,
        text=True,
    )
    if failure is None:
        assert completed.returncode == 0, completed.stderr
    else:
        assert completed.returncode != 0
        assert (
            "Required image package" in completed.stderr
            or "Missing required image command" in completed.stderr
        )
