"""Build and boot the complete standard desktop recipe on an isolated real VM.

V074/V075 expectations live in docs/testing/lifecycle-vm-cases.md. Every
allocated guest and image is removed after testing; screenshots and small JSON
evidence remain in the output directory. No deployed service is contacted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))

from src.runtime.base import WorkspaceConfig
from verify_workspace_lifecycle import runtime_at


async def verify(root: Path, cloud: Path, distro: str, recipe: Path) -> None:
    """Check physical recipe effects, publication, desktop, browser and storage."""
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o755)
    if any(not parent.stat().st_mode & 0o001 for parent in root.parents):
        raise PermissionError("Choose an output parent traversable by libvirt-qemu")
    runtime, _ = runtime_at(root)
    runtime._settings.qemu_disk_size_gb = 20
    runtime._settings.qemu_build_memory_mb = 2048
    runtime._settings.qemu_memory_mb = 2048
    runtime._resolve_build_base_image = lambda _: cloud
    operation, image, guest_id = (str(uuid.uuid4()) for _ in range(3))
    target = runtime._build_image_dir / f"{image}.qcow2"
    report: dict[str, Any] = {
        "case": "V074" if distro == "22.04" else "V075",
        "distro": distro,
        "checks": [],
        "passed": False,
        "cleanup_errors": [],
        "recipe_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
    }

    def save() -> None:
        (root / "report.json").write_text(json.dumps(report, indent=2))

    def passed(name: str, **evidence: Any) -> None:
        report["checks"].append({"name": name, "status": "PASS", **evidence})
        save()

    async def guest(script: str, timeout: float = 120) -> str:
        code, output = await asyncio.wait_for(
            runtime.exec_command_wait(guest_id, ["bash", "-lc", script]),
            timeout,
        )
        if code:
            raise RuntimeError(f"Guest check failed ({code}): {output[-6000:]}")
        return output.strip()

    async def progress(line: str) -> None:
        with (root / "build.log").open("a") as stream:
            stream.write(line + "\n")

    try:
        assert shutil.disk_usage(root).free > 10 * 1024**3
        started = time.monotonic()
        result = await runtime.build_image(
            base_distro=f"ubuntu:{distro}",
            init_script=recipe.read_text(),
            image_path=str(target),
            operation_id=operation,
            image_instance_id=image,
            progress_callback=progress,
        )
        passed(
            "full standard recipe builds",
            seconds=time.monotonic() - started,
            image_result=result,
        )
        info = await runtime._image_info(target)
        manifest = json.loads(target.with_suffix(".manifest.json").read_text())
        assert not info.get("backing-filename")
        assert manifest["operation_id"] == operation
        assert manifest["sha256"] == await asyncio.to_thread(
            runtime._publication_digest,
            target,
        )
        passed(
            "standalone publication identity, hash and disk floor", manifest=manifest
        )
        await runtime.create_workspace(
            WorkspaceConfig(
                workspace_id=guest_id,
                image=str(target),
                qemu_vcpus=1,
                qemu_memory_mb=2048,
                qemu_disk_size_gb=20,
            )
        )
        native = await guest("""set -euo pipefail
for executable in Xvnc vncserver xfce4-session xfce4-panel xfwm4 thunar plank rofi google-chrome-stable libreoffice xclip wmctrl xdotool; do command -v "$executable"; done
/usr/bin/python3 - <<'PY'
import Xlib, PIL, structlog, gi, uno
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi
print('Native dependencies imported')
PY
test -d /usr/share/themes/WhiteSur-Light
test -d /usr/share/icons/WhiteSur
test -s /usr/share/backgrounds/opencuria/Ventura-light.jpg
if ldd "$(command -v Xvnc)" | grep 'not found'; then exit 1; fi
""")
        passed("published image boots with native desktop dependencies", output=native)
        desktop = await guest("""set -euo pipefail
OPENCURIA_DESKTOP_GEOMETRY=1280x720 /usr/local/bin/opencuria-desktop-start
export DISPLAY=:1
for _ in $(seq 1 120); do
 if pgrep -x xfce4-session >/dev/null && wmctrl -m | grep -qi xfwm; then break; fi
 sleep 0.25
done
wmctrl -m | grep -i xfwm
xdotool getmouselocation
""")
        passed("real KasmVNC and Xfce session", output=desktop)
        browser = await guest("""set -euo pipefail
printf '<html><head><title>OpenCuria lifecycle audit</title></head><body style="font:28px sans-serif;padding:64px;background:#f4f7fc"><h1>OpenCuria workspace</h1><p>Full desktop image verification passed.</p></body></html>' >/tmp/audit-page.html
google-chrome-stable --no-sandbox --headless --disable-gpu --disable-dev-shm-usage --no-first-run --dump-dom file:///tmp/audit-page.html 2>/tmp/audit-headless.log | grep 'Full desktop image verification passed.'
export DISPLAY=:1
nohup google-chrome-stable --no-sandbox --disable-gpu --disable-dev-shm-usage --no-first-run --force-renderer-accessibility --user-data-dir=/tmp/audit-profile --new-window file:///tmp/audit-page.html >/tmp/audit-browser.log 2>&1 </dev/null &
for _ in $(seq 1 120); do
 if wmctrl -l | grep -q 'OpenCuria lifecycle audit'; then break; fi
 sleep 0.25
done
wmctrl -l | grep 'OpenCuria lifecycle audit'
wmctrl -r 'OpenCuria lifecycle audit' -b add,maximized_vert,maximized_horz
/usr/bin/python3 - <<'PY'
from Xlib import display, X
from PIL import Image
d = display.Display(':1')
s = d.screen()
raw = s.root.get_image(0, 0, s.width_in_pixels, s.height_in_pixels, X.ZPixmap, 0xffffffff)
Image.frombytes('RGB', (s.width_in_pixels, s.height_in_pixels), raw.data, 'raw', 'BGRX').save('/tmp/audit-desktop.png')
d.close()
PY
""")
        ssh = await runtime._get_ssh(guest_id)
        async with ssh.start_sftp_client() as sftp:
            await sftp.get("/tmp/audit-desktop.png", str(root / "desktop.png"))
        passed(
            "Chrome headless and visible GUI rendering",
            output=browser,
            screenshot=str(root / "desktop.png"),
        )
        token = str(uuid.uuid4())
        await guest(f"printf '%s' '{token}' >/workspace/audit-persist; sync")
        await runtime.stop_workspace(guest_id)
        await runtime.start_workspace(guest_id)
        assert await guest("cat /workspace/audit-persist") == token
        passed("persistent disk survives stop and start")
        report["passed"] = True
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        for instance in (guest_id, runtime._build_instance_id(operation)):
            try:
                await runtime.remove_workspace(instance)
            except Exception as error:  # noqa: BLE001 - clean every allocated guest
                report["cleanup_errors"].append(f"{instance}: {error}")
        if target.exists():
            try:
                await runtime.delete_image_artifact(str(target))
            except Exception as error:  # noqa: BLE001 - preserve cleanup evidence
                report["cleanup_errors"].append(f"publication: {error}")
        if report["cleanup_errors"]:
            report["passed"] = False
        save()
        if report["cleanup_errors"]:
            raise RuntimeError(f"Cleanup failed: {report['cleanup_errors']}")


def main() -> None:
    """Parse explicit local fixtures and a fresh writable output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cloud-image", required=True, type=Path)
    parser.add_argument("--init-script", required=True, type=Path)
    parser.add_argument("--distro", choices=("22.04", "24.04"), required=True)
    args = parser.parse_args()
    asyncio.run(
        verify(
            args.output.resolve(),
            args.cloud_image.resolve(),
            args.distro,
            args.init_script.resolve(),
        )
    )


if __name__ == "__main__":
    main()
