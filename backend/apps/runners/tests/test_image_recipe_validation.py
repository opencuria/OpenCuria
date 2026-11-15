"""Desktop recipe checks for application installation and accessibility setup."""

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


@pytest.mark.parametrize("failed_property", [None, "IsEnabled", "ScreenReaderEnabled"])
def test_desktop_enables_accessibility_before_starting_apps(tmp_path, failed_property):
    recipe = RECIPE.read_text()
    session = recipe.split("<<'DESKTOP_SESSION'\n", 1)[1].split(
        "\nDESKTOP_SESSION", 1
    )[0]
    calls = tmp_path / "calls"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gdbus = fake_bin / "gdbus"
    gdbus.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >>"$CALLS"\n'
        'if [ -n "$FAILED_PROPERTY" ] && '
        '[[ "$*" == *" $FAILED_PROPERTY "* ]]; then exit 1; fi\n'
    )
    gdbus.chmod(0o755)
    xfce = fake_bin / "startxfce4"
    xfce.write_text('#!/bin/bash\necho startxfce4 >>"$CALLS"\n')
    xfce.chmod(0o755)
    touch = fake_bin / "touch"
    touch.write_text('#!/bin/bash\necho accessibility-ready >>"$CALLS"\n')
    touch.chmod(0o755)
    completed = subprocess.run(
        ["/bin/bash", "-c", session, "session", "--session-bus"],
        env={
            "PATH": str(fake_bin),
            "CALLS": str(calls),
            "FAILED_PROPERTY": failed_property or "",
        },
        capture_output=True,
        text=True,
    )
    recorded = calls.read_text().splitlines()
    expected = [
        "call --session --timeout 5 --dest org.a11y.Bus --object-path /org/a11y/bus "
        "--method org.freedesktop.DBus.Properties.Set "
        f"org.a11y.Status {property} <true>"
        for property in ("IsEnabled", "ScreenReaderEnabled")
    ]
    if failed_property:
        assert completed.returncode != 0
        assert "startxfce4" not in recorded
        assert recorded == expected[: (1 if failed_property == "IsEnabled" else 2)]
    else:
        assert completed.returncode == 0, completed.stderr
        assert recorded == [*expected, "accessibility-ready", "startxfce4"]


def test_desktop_launchers_inherit_toolkit_accessibility_settings(tmp_path):
    recipe = RECIPE.read_text()
    startup = recipe.split("<<'DESKTOP_SESSION'\n", 1)[1].split(
        "\nDESKTOP_SESSION", 1
    )[0]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("mkdir", "chmod", "grep"):
        path = fake_bin / name
        path.write_text("#!/bin/bash\nexit 0\n")
        path.chmod(0o755)
    launcher = fake_bin / "dbus-launch"
    launcher.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" "$NO_AT_BRIDGE" '
        '"$QT_LINUX_ACCESSIBILITY_ALWAYS_ON" "$QT_ACCESSIBILITY"\n'
    )
    launcher.chmod(0o755)
    script = tmp_path / "session"
    script.write_text(startup)
    completed = subprocess.run(
        ["/bin/bash", str(script)],
        env={"PATH": str(fake_bin), "XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        f"--exit-with-session {script} --session-bus",
        "0",
        "1",
        "1",
    ]
    assert 'gtk-modules="gail:atk-bridge"' in recipe
    assert "libatk-adaptor libgail-common" in recipe
