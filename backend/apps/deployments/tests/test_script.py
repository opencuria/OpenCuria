"""Fail-safe rollout shell contracts; Docker calls are deliberately faked."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "deploy-production.sh"


def test_deploy_script_requires_arguments_before_docker(tmp_path) -> None:
    """Invalid invocations stop before touching infrastructure."""
    env = {**os.environ, "OPENCURIA_DEPLOY_LOCK": str(tmp_path / "lock")}
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Usage:" in result.stderr


def test_deploy_script_fake_docker_keeps_old_upstream_when_candidate_unready(
    tmp_path,
) -> None:
    """Readiness failure aborts without routing or stopping the old generation."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    lines = [
        "#!/usr/bin/env bash",
        'printf \'%s\n\' "$*" >> "$FAKE_DOCKER_LOG"',
        'if [[ "$1 $2" == "container inspect" && "$3" == *blue-worker-1 ]]; '
        "then exit 1; fi",
        'if [[ "$1" == inspect && "$2" == --format ]]; then echo starting; fi',
        "exit 0",
    ]
    docker.write_text("\n".join(lines) + "\n", encoding="utf-8")
    docker.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("DB_USER=test\nDB_NAME=test\n")
    (project / ".env.operations").write_text("DB_USER=test\nDB_NAME=test\n")
    (project / "release.json").write_text(
        '{"version":"candidate-2","protocol_min":1,"protocol_max":1,'
        '"database_schema":2,"database_schema_min":1,"database_schema_max":2}'
    )
    (project / "scripts").mkdir()
    copied_script = project / "scripts" / SCRIPT.name
    copied_script.write_bytes(SCRIPT.read_bytes())
    copied_script.chmod(0o755)
    (project / "deploy").mkdir()
    (project / "deploy" / "upstreams.conf").write_text("old-upstream\n")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "OPENCURIA_DEPLOY_LOCK": str(tmp_path / "lock"),
        "OPENCURIA_UPGRADE_TIMEOUT": "1",
    }
    result = subprocess.run(
        ["bash", str(copied_script), "candidate-2", "blue"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Candidate not ready" in result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "nginx -s reload" not in commands
    assert "docker stop" not in commands
    assert (project / "deploy" / "upstreams.conf").read_text() == "old-upstream\n"
