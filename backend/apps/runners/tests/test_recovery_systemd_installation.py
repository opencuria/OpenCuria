"""Preview installation is safe and renders the deployed worker supervision."""

import os
import subprocess
from pathlib import Path


def test_installer_preview_renders_watchdog_and_backend_dependency(tmp_path):
    backend = Path(__file__).resolve().parents[3]
    env_file = tmp_path / "backend % example.env"
    env_file.write_text("DJANGO_ENV=development\n")
    marker = tmp_path / "systemctl-called"
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    systemctl = executable_dir / "systemctl"
    systemctl.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n")
    systemctl.chmod(0o755)
    environment = {**os.environ, "PATH": f"{executable_dir}:{os.environ['PATH']}"}
    output = tmp_path / "units"
    subprocess.run(
        [
            "bash",
            str(backend / "systemd/install-recovery-service.sh"),
            "--no-activate",
            "--restart-runner",
            "opencuria-runner.service",
            "--env-file",
            str(env_file),
            "--output-dir",
            str(output),
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not marker.exists()
    unit = (output / "opencuria-recover-lifecycle.service").read_text()
    assert "Type=notify" in unit and "WatchdogSec=90" in unit
    assert "PartOf=opencuria-backend.service" in unit
    assert f"WorkingDirectory={backend}" in unit
    assert f"EnvironmentFile={str(env_file).replace('%', '%%')}" in unit
    assert "__" not in unit
    dependency = output / "opencuria-backend.service.d/20-lifecycle-recovery.conf"
    assert (
        dependency.read_text()
        == "[Unit]\nWants=opencuria-recover-lifecycle.service opencuria-execution.service\n"
    )

    execution = (output / "opencuria-execution.service").read_text()
    assert "manage.py execute_harness" in execution
    assert "PartOf=" not in execution
    assert "Requires=" not in execution
    assert "Restart=on-failure" in execution
    assert f"WorkingDirectory={backend}" in execution
    assert f"EnvironmentFile={str(env_file).replace('%', '%%')}" in execution
    assert "__" not in execution


def test_installer_rejects_execution_unit_collisions_and_invalid_names(tmp_path):
    backend = Path(__file__).resolve().parents[3]
    for name, expected in (
        ("opencuria-backend.service", "distinct units"),
        ("../outside.service", "Invalid systemd service name"),
    ):
        result = subprocess.run(
            [
                "bash",
                str(backend / "systemd/install-recovery-service.sh"),
                "--no-activate",
                "--execution-service",
                name,
                "--output-dir",
                str(tmp_path / "units"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert expected in result.stderr
        assert not (tmp_path / "units").exists()
