"""Real process matching for desktop cleanup, without signalling host services."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from src.runtime.managed_process import MANAGED_CODE
from src.services.sessions.desktop import DESKTOP_STOP_COMMAND, DESKTOP_STOPPED_PROBE

ROOT = Path(__file__).resolve().parents[2]


def test_provisioned_stop_script_matches_runner() -> None:
    """Generated image helpers retain the runner's canonical stop implementation."""
    source = (ROOT / "backend/apps/runners/scripts/qemu_desktop_session.sh").read_text()
    script = source.split("<<'DESKSTOP'\n", 1)[1].split("\nDESKSTOP\n", 1)[0] + "\n"
    assert script == DESKTOP_STOP_COMMAND


async def named_process(name: str) -> asyncio.subprocess.Process:
    """Launch a harmless process with the argv used by a desktop executable."""
    process = await asyncio.create_subprocess_exec(
        "bash", "-c", 'exec -a "$1" sleep 30', "desktop-test", name
    )

    async def ready() -> None:
        while (
            not Path(f"/proc/{process.pid}/cmdline")
            .read_bytes()
            .startswith(name.encode() + b"\0")
        ):
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(ready(), 2)
    except BaseException:
        process.terminate()
        await process.wait()
        raise
    return process


@pytest.mark.asyncio
async def test_desktop_cleanup_preserves_agent_wrappers_and_other_displays(
    tmp_path: Path,
) -> None:
    """Actual pgrep kills :1 desktop processes, preserving argv containing profile."""
    pgrep = shutil.which("pgrep")
    if not pgrep:
        pytest.skip("pgrep is required for the desktop process regression")
    processes: list[asyncio.subprocess.Process] = []
    try:
        agent = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            MANAGED_CODE,
        )
        processes.append(agent)
        for name in [
            "rofi",
            "/usr/bin/Xvnc :1",
            "/usr/bin/openbox",
            "/usr/bin/Xvnc :10",
        ]:
            processes.append(await named_process(name))

        # Use real pgrep semantics but restrict every signal to our fixtures.
        # A fake rm keeps the production script away from host X11 lock files.
        commands = tmp_path / "bin"
        commands.mkdir()
        filtered_pgrep = commands / "pgrep"
        filtered_pgrep.write_text(
            "#!/bin/sh\n"
            "desktop_no_match=1\n"
            f'for pid in $({shlex.quote(pgrep)} "$@"); do\n'
            '  case ":$DESKTOP_TEST_PIDS:" in\n'
            '    *":$pid:"*) printf "%s\\n" "$pid"; desktop_no_match=0;;\n'
            "  esac\n"
            "done\n"
            'exit "$desktop_no_match"\n'
        )
        filtered_pgrep.chmod(0o755)
        (commands / "rm").write_text("#!/bin/sh\nexit 0\n")
        (commands / "rm").chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{commands}:/usr/bin:/bin",
            "DESKTOP_TEST_PIDS": ":".join(str(p.pid) for p in processes),
        }

        # Establish the original failure against the real managed wrapper text.
        probe = await asyncio.create_subprocess_exec(
            str(filtered_pgrep),
            "-f",
            "rofi",
            env=env,
            stdout=asyncio.subprocess.PIPE,
        )
        matched, _ = await probe.communicate()
        assert str(agent.pid) in matched.decode().splitlines()

        stop = await asyncio.create_subprocess_exec(
            "bash", "-c", DESKTOP_STOP_COMMAND, env=env
        )
        assert await asyncio.wait_for(stop.wait(), 2) == 0
        for target in processes[1:-1]:
            assert await asyncio.wait_for(target.wait(), 2) == -15
        assert agent.returncode is None
        assert processes[-1].returncode is None
        confirm = await asyncio.create_subprocess_exec(
            "sh", "-c", DESKTOP_STOPPED_PROBE, env=env
        )
        assert await asyncio.wait_for(confirm.wait(), 2) == 0
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
        await asyncio.gather(*(p.wait() for p in processes))
