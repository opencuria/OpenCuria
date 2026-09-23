"""Image definitions, builds and artifacts (Phase 3 domain mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self.image_definitions``,
``self.build_jobs``, ``self.image_instances``, ``self.workspaces``,
``self.tasks`` plus sibling helpers (``_emit_to_runner`` via
``RunnerTransportMixin``, ``_forward_*`` via ``FrontendBusMixin``,
``_dispatch_workspace_task`` via ``TaskDispatchMixin``). Lazy
``from ...models`` / ``django`` imports keep their semantics; the
``Path(__file__)`` lookup in ``_desktop_session_init_script_block`` uses
one extra ``.parent`` for the deeper ``domains/`` location (same
``runners/scripts/`` target).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import timedelta
from pathlib import Path

from asgiref.sync import sync_to_async
from django.utils import timezone

from common.exceptions import ConflictError
from common.utils import generate_uuid

from ...enums import RuntimeType, TaskStatus, TaskType, WorkspaceStatus
from ...exceptions import (
    RunnerOfflineError,
    TaskNotFoundError,
    WorkspaceNotFoundError,
    WorkspaceStateError,
)

logger = logging.getLogger(__name__)


class ImageLifecycleMixin:
    """Image definition/build/artifact flows shared by RunnerService."""

    @staticmethod
    def _build_package_install_block(base_distro: str, packages: list[str]) -> str:
        """Generate package installation block based on distro family."""
        clean_packages = [p.strip() for p in packages if p.strip()]
        if not clean_packages:
            return ""

        distro = (base_distro or "").lower()
        if "alpine" in distro:
            return "RUN apk add --no-cache " + " ".join(clean_packages)

        # Default to apt for ubuntu/debian and unknown distros.
        return (
            "RUN apt-get update && apt-get install -y \\\n"
            f"    {' '.join(clean_packages)} \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )

    @staticmethod
    def _validate_qemu_base_distro(base_distro: str) -> None:
        """Ensure QEMU image definitions use a supported distro source."""
        distro = (base_distro or "").strip().lower()
        if distro.startswith("ubuntu:"):
            return
        raise ConflictError(
            "QEMU image definitions currently require an ubuntu:<version> base distro"
        )

    @staticmethod
    def _desktop_session_dockerfile_block() -> str:
        """Return Dockerfile lines that install KasmVNC desktop session support.

        Includes the Agent-S computer-use workspace dependencies for all
        non-Alpine desktop images (PyAutoGUI/pyperclip, tesseract OCR,
        wmctrl, xclip/xsel, sudo/iproute2 ``ss``, LibreOffice Calc +
        python3-uno). ``python3-pyautogui`` has no Ubuntu 22.04/24.04 apt
        package, so it falls back to a minimal pip install into the system
        python (``python3 -m pip`` keeps the distribution visible to
        apt-Python). pip on 24.04 requires ``--break-system-packages``
        (PEP 668); pip on 22.04 does not know the flag, so retry without
        it. ``python3-pyperclip`` from apt already provides
        ``import pyperclip`` (no pip needed). ``python3-tk`` is a pure
        runtime dep: pyautogui imports mouseinfo, which imports tkinter —
        without it every execute snippet dies at import time with
        "You must install tkinter on Linux to use MouseInfo". Existing
        images are never modified in place: rebuild the image definition
        after this block changes.
        """
        return """# --- KasmVNC desktop session support ---
RUN apt-get update && apt-get install -y \\
    xfonts-base openbox dbus-x11 x11-xserver-utils ffmpeg xdotool \\
    libnss3 libatk-bridge2.0-0 libcups2 libdrm2 \\
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \\
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \\
    wget ca-certificates \\
    tesseract-ocr wmctrl xclip xsel libreoffice-calc python3-uno \\
    python3-pyperclip python3-tk sudo iproute2 python3-pip \\
    && (apt-get install -y libasound2t64 || apt-get install -y libasound2) \\
    && (apt-get install -y python3-pyautogui \\
        || python3 -m pip install --break-system-packages pyautogui \\
        || python3 -m pip install pyautogui) \\
    && wget -q -O /tmp/kasmvnc.deb \\
       "https://github.com/kasmtech/KasmVNC/releases/download/v1.3.3/kasmvncserver_jammy_1.3.3_amd64.deb" \\
    && apt-get install -y /tmp/kasmvnc.deb || true \\
    && apt-get install -f -y \\
    && rm -f /tmp/kasmvnc.deb \\
    && wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \\
    && (apt-get install -y /tmp/google-chrome.deb || apt-get install -f -y) \\
    && rm -f /tmp/google-chrome.deb \\
    && rm -rf /var/lib/apt/lists/*

# Pre-configure KasmVNC (skip interactive wizard).
# python-Xlib opens $XAUTHORITY/~/.Xauthority unconditionally, even
# against the auth-less Xvnc (-SecurityTypes None): bake an empty file
# so PyAutoGUI/mouseinfo connect out of the box (the runner also
# touches it at every start and before each execute for self-healing).
RUN mkdir -p /root/.vnc \\
    && touch /root/.vnc/.de-was-selected /root/.Xauthority \\
    && printf "password\\npassword\\n" | vncpasswd -u root -w -r 2>/dev/null || true \\
    && printf 'desktop:\\n  resolution:\\n    width: 1920\\n    height: 1080\\n  allow_resize: false\\nnetwork:\\n  protocol: http\\n  interface: 0.0.0.0\\n  websocket_port: 6901\\n  ssl:\\n    require_ssl: false\\n    pem_certificate:\\n    pem_key:\\n' > /root/.vnc/kasmvnc.yaml \\
    && printf '#!/bin/bash\\nset -eu\\nfor browser in google-chrome-stable google-chrome chromium chromium-browser /usr/lib/chromium/chromium; do\\n  if [ \"${browser#/}\" != \"$browser\" ]; then\\n    if [ -x \"$browser\" ]; then\\n      exec \"$browser\" --no-sandbox --disable-gpu --start-maximized --disable-dev-shm-usage --no-first-run\\n    fi\\n    continue\\n  fi\\n  if command -v \"$browser\" >/dev/null 2>&1; then\\n    if [ \"$browser\" = \"chromium-browser\" ] && ! chromium-browser --version >/dev/null 2>&1; then\\n      continue\\n    fi\\n    exec \"$browser\" --no-sandbox --disable-gpu --start-maximized --disable-dev-shm-usage --no-first-run\\n  fi\\ndone\\necho \"No supported browser binary found for desktop session\" >&2\\n' > /usr/local/bin/opencuria-desktop-browser \\
    && printf '#!/bin/bash\\nexport DISPLAY=:1\\nexport HOME=/root\\nopenbox-session &\\nsleep 1\\n/usr/local/bin/opencuria-desktop-browser >/root/.vnc/browser.log 2>&1 &\\nwait\\n' > /root/.vnc/xstartup \\
    && chmod +x /root/.vnc/xstartup /usr/local/bin/opencuria-desktop-browser

# Desktop start/stop scripts (use Xvnc directly to avoid KasmVNC perl wrapper prompts)
RUN printf '#!/bin/bash\\nset -e\\nexport DISPLAY=:1\\nexport HOME=/root\\nGEOMETRY="${OPENCURIA_DESKTOP_GEOMETRY:-1920x1080}"\\n/usr/local/bin/opencuria-desktop-stop 2>/dev/null || true\\nmkdir -p /root/.vnc\\nrm -f /root/.vnc/.xstartup-started\\nrm -f /tmp/.X1-lock /tmp/.X11-unix/X1\\n/usr/bin/Xvnc :1 -geometry "$GEOMETRY" -depth 24 -rfbport 5901 -SecurityTypes None -disableBasicAuth -websocketPort 6901 -httpd /usr/share/kasmvnc/www -interface 0.0.0.0 -AlwaysShared -AcceptKeyEvents -AcceptPointerEvents -SendCutText -AcceptCutText -AcceptSetDesktopSize=0 >>/root/.vnc/server.log 2>&1 &\\nfor _ in $(seq 1 120); do\\n  if [ -e /tmp/.X11-unix/X1 ] && [ ! -f /root/.vnc/.xstartup-started ]; then\\n    touch /root/.vnc/.xstartup-started\\n    /root/.vnc/xstartup >>/root/.vnc/xstartup.log 2>&1 &\\n  fi\\n  if [ -e /tmp/.X11-unix/X1 ] && (echo >/dev/tcp/127.0.0.1/6901) >/dev/null 2>&1; then\\n    echo \"Desktop session started on :1 (ws port 6901)\"\\n    exit 0\\n  fi\\n  sleep 0.25\\ndone\\necho \"Desktop session failed to start\" >&2\\nexit 1\\n' > /usr/local/bin/opencuria-desktop-start \
    && printf '#!/bin/bash\\nfor pid in $(pgrep -f "^(/usr/bin/)?Xvnc :1" 2>/dev/null); do kill "$pid" 2>/dev/null || true; done\\nfor pid in $(pgrep -f "openbox" 2>/dev/null); do kill "$pid" 2>/dev/null || true; done\\nrm -f /tmp/.X1-lock /tmp/.X11-unix/X1\\n' > /usr/local/bin/opencuria-desktop-stop \
    && chmod +x /usr/local/bin/opencuria-desktop-start /usr/local/bin/opencuria-desktop-stop
"""

    @staticmethod
    def _desktop_session_init_script_block() -> str:
        """Return shell script lines that install the QEMU KasmVNC desktop."""
        script_path = (
            Path(__file__).resolve().parent.parent.parent
            / "scripts"
            / "qemu_desktop_session.sh"
        )
        return "\n" + script_path.read_text(encoding="utf-8").strip() + "\n"

    @classmethod
    def _build_qemu_init_script_content(cls, definition) -> str:
        """Build a shell init script for QEMU image definitions."""
        lines = [
            "#!/bin/bash",
            "set -euo pipefail",
            "",
        ]

        packages = [p.strip() for p in list(definition.packages or []) if p.strip()]
        distro = (definition.base_distro or "").lower()
        if packages:
            if "alpine" in distro:
                lines += [
                    f"apk add --no-cache {' '.join(packages)}",
                    "",
                ]
            else:
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

        # Always include KasmVNC desktop session support (non-Alpine only)
        distro_check = (definition.base_distro or "").lower()
        if "alpine" not in distro_check:
            lines += [cls._desktop_session_init_script_block(), ""]

        return "\n".join(lines).strip() + "\n"

    @classmethod
    def _generate_dockerfile_content(cls, definition) -> str:
        """Build Dockerfile content from an image definition record."""
        lines = [f"FROM {definition.base_distro}", ""]

        if "alpine" not in (definition.base_distro or "").lower():
            lines += ["ENV DEBIAN_FRONTEND=noninteractive", ""]

        install_block = cls._build_package_install_block(
            definition.base_distro, list(definition.packages or [])
        )
        if install_block:
            lines += [install_block, ""]

        for key, value in dict(definition.env_vars or {}).items():
            if key:
                lines.append(f"ENV {key}={value}")
        if definition.env_vars:
            lines.append("")

        if definition.custom_dockerfile:
            lines += [definition.custom_dockerfile.strip(), ""]

        # Always include KasmVNC desktop session support (non-Alpine only)
        if "alpine" not in (definition.base_distro or "").lower():
            lines += [cls._desktop_session_dockerfile_block(), ""]

        lines += [
            'CMD ["tail", "-f", "/dev/null"]',
        ]
        return "\n".join(lines).strip() + "\n"

    def list_image_definitions(self, organization_id: uuid.UUID) -> list:
        """List image definitions for an organization."""
        self.timeout_stale_image_operations()
        return list(self.image_definitions.list_by_org(organization_id))

    def list_build_jobs(
        self,
        image_definition_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> list:
        """List runner build records for an image definition."""
        self.timeout_stale_image_operations()
        return list(
            self.build_jobs.list_for_definition(
                image_definition_id,
                organization_id=organization_id,
            )
        )

    def timeout_stale_image_operations(self, *, timeout_hours: int = 1) -> None:
        """Fail hung builds and stuck deletions so the UI can retry."""
        from ...models import ImageDefinition, ImageInstance

        cutoff = timezone.now() - timedelta(hours=timeout_hours)
        stale_message = f"Timed out after {timeout_hours}h without progress"

        def _instance_or_none(build):
            try:
                return build.image_instance
            except ImageInstance.DoesNotExist:
                return None

        for build in self.build_jobs.list_stale_builds(cutoff=cutoff):
            self.build_jobs.mark_failed(build.id, error=stale_message)
            instance = _instance_or_none(build)
            if instance is not None and instance.status in {
                ImageInstance.Status.BUILDING,
                ImageInstance.Status.CAPTURING,
            }:
                self.image_instances.mark_failed(instance.id)

        for build in self.build_jobs.list_stale_deletes(cutoff=cutoff):
            self.build_jobs.mark_delete_failed(build.id, error=stale_message)
            instance = _instance_or_none(build)
            if instance is not None and instance.status in {
                ImageInstance.Status.PENDING_DELETION,
                ImageInstance.Status.DELETING,
            }:
                self.image_instances.mark_delete_failed(
                    instance.id, error=stale_message
                )
            self._mark_definition_delete_failed(
                build.image_definition_id,
                error=stale_message,
            )

        deleting_definitions = ImageDefinition.objects.filter(
            status__in=[
                ImageDefinition.Status.PENDING_DELETION,
                ImageDefinition.Status.DELETING,
            ]
        )
        for definition in deleting_definitions:
            self._check_definition_deletion_complete(definition.id)
            definition.refresh_from_db()
            if definition.status not in {
                ImageDefinition.Status.PENDING_DELETION,
                ImageDefinition.Status.DELETING,
            }:
                continue
            if self.build_jobs.list_in_progress_deletes_for_definition(
                definition.id
            ).exists():
                continue
            self._mark_definition_delete_failed(
                definition.id,
                error=stale_message,
            )

    def _ensure_definition_mutable(self, definition) -> None:
        """Reject runner mutations while a definition is deleted or being removed."""
        from ...models import ImageDefinition

        if definition.status in {
            ImageDefinition.Status.PENDING_DELETION,
            ImageDefinition.Status.DELETING,
            ImageDefinition.Status.DELETED,
            ImageDefinition.Status.DELETE_FAILED,
        }:
            raise ConflictError(
                f"Cannot modify image definition in state '{definition.status}'"
            )

    def ensure_definition_mutable(self, definition) -> None:
        """Public alias for :meth:`_ensure_definition_mutable`.

        Phase 4 (leak closure): external callers (``apps/runners/api.py``,
        ``apps/mcp_app/server.py``) use this public name. The private name
        stays canonical with the logic so tests that mock
        ``_ensure_definition_mutable`` with a lambda keep working; this
        method delegates to it (same signature, same effect).
        """
        self._ensure_definition_mutable(definition)

    # -- Phase-4 public repository delegation (leak closure) ----------------
    # Thin sync delegations over ``self.image_instances`` /
    # ``self.image_definitions`` so production callers no longer reach
    # into ``service.<repository>`` directly. No logic, no behaviour
    # change; async callers keep wrapping them in ``sync_to_async``.

    def get_image_artifact(self, artifact_id: uuid.UUID):
        """Return an image artifact by ID or None."""
        return self.image_instances.get_by_id(artifact_id)

    def rename_image_artifact(self, artifact_id: uuid.UUID, name: str) -> bool:
        """Rename an image artifact. Returns True if updated."""
        return self.image_instances.update_name(artifact_id, name)

    def timeout_stale_image_artifacts(self, *, timeout_hours: int = 1) -> int:
        """Mark stale creating image artifacts as failed; return count."""
        return self.image_instances.timeout_stale(timeout_hours=timeout_hours)

    def get_image_definition(self, definition_id: uuid.UUID):
        """Return an image definition by ID or None."""
        return self.image_definitions.get_by_id(definition_id)

    async def activate_build_job(self, build, *, created_by=None):
        """Make an existing runner image selectable, or build it if none exists."""
        from ...models import ImageBuildJob, ImageInstance

        self._ensure_definition_mutable(build.image_definition)
        instance = await sync_to_async(self.image_instances.get_by_build_job_id)(
            build.id
        )
        has_ready_image = (
            build.built_at is not None
            and instance is not None
            and instance.status
            in {ImageInstance.Status.READY, ImageInstance.Status.RETIRED}
            and bool(instance.runner_ref)
        )
        if not has_ready_image:
            return await self.trigger_build_job(
                image_definition=build.image_definition,
                runner=build.runner,
                activate=True,
                created_by=created_by,
            )

        build.status = ImageBuildJob.Status.ACTIVE
        build.deactivated_at = None
        await sync_to_async(build.save)(
            update_fields=["status", "deactivated_at", "updated_at"]
        )
        if instance.status == ImageInstance.Status.RETIRED:
            await sync_to_async(self.image_instances.mark_ready_from_retired)(
                instance.id
            )
        return build

    async def trigger_build_job(
        self,
        *,
        image_definition,
        runner,
        activate: bool = True,
        created_by=None,
    ):
        """Create/update runner build record and dispatch task:build_image."""
        from ...models import ImageInstance, ImageBuildJob

        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=image_definition.runtime_type,
        )
        self._ensure_definition_mutable(image_definition)

        existing = await sync_to_async(self.build_jobs.get)(
            image_definition.id, runner.id
        )
        if existing is not None and existing.status in {
            ImageBuildJob.Status.PENDING_DELETION,
            ImageBuildJob.Status.DELETING,
        }:
            raise ConflictError(
                f"Build job is already in deletion state '{existing.status}'"
            )

        if existing is None:
            build = await sync_to_async(ImageBuildJob.objects.create)(
                image_definition=image_definition,
                runner=runner,
                status=ImageBuildJob.Status.PENDING,
            )
        else:
            if existing.status in {
                ImageBuildJob.Status.PENDING,
                ImageBuildJob.Status.BUILDING,
            } and existing.build_task_id is not None:
                prior_task = await sync_to_async(self.tasks.get_by_id)(
                    existing.build_task_id
                )
                if prior_task is not None and prior_task.status in {
                    TaskStatus.PENDING,
                    TaskStatus.IN_PROGRESS,
                }:
                    raise ConflictError(
                        f"Build job is already '{existing.status}' "
                        f"(task {existing.build_task_id})"
                    )
            build = existing
            build.status = (
                ImageBuildJob.Status.DEACTIVATED
                if not activate
                else ImageBuildJob.Status.PENDING
            )
            build.build_task = None
            # A rebuild/retry starts from a clean slate: the old (possibly
            # truncated) tail must not pollute the new run's log, and
            # clearing it here also frees the TEXT payload immediately
            # instead of growing it further.
            build.build_log = ""
            build.deleting_task_id = None
            build.delete_requested_at = None
            build.delete_started_at = None
            build.delete_confirmed_at = None
            build.delete_last_error = ""
            await sync_to_async(build.save)(
                update_fields=[
                    "status",
                    "build_task",
                    "build_log",
                    "deleting_task_id",
                    "delete_requested_at",
                    "delete_started_at",
                    "delete_confirmed_at",
                    "delete_last_error",
                    "updated_at",
                ]
            )

        if not activate:
            existing_image = await sync_to_async(
                self.image_instances.get_by_build_job_id
            )(build.id)
            if existing_image is not None:
                await sync_to_async(self.image_instances.mark_retired)(
                    existing_image.id
                )
            return build

        if not runner.sid:
            logger.info(
                "Runner %s is offline; leaving image build %s pending",
                runner.id,
                build.id,
            )
            return build

        if image_definition.runtime_type == RuntimeType.QEMU:
            self._validate_qemu_base_distro(image_definition.base_distro)

        task = await sync_to_async(self.tasks.create)(
            task_id=generate_uuid(),
            runner=runner,
            task_type=TaskType.BUILD_IMAGE,
        )
        build_runner_ref = (
            f"opencuria/custom/{re.sub(r'[^a-z0-9-]+', '-', image_definition.name.lower())}:{build.id}"
            if image_definition.runtime_type == RuntimeType.DOCKER
            else f"/var/lib/opencuria/base-images/{build.id}.qcow2"
        )
        await sync_to_async(ImageBuildJob.objects.filter(id=build.id).update)(
            build_task=task,
            status=ImageBuildJob.Status.PENDING,
        )
        image = await sync_to_async(self.image_instances.get_by_build_job_id)(build.id)
        image_name = f"{image_definition.name} ({runner.name})"
        if image is None:
            await sync_to_async(self.image_instances.create_pending)(
                runner=runner,
                runtime_type=image_definition.runtime_type,
                origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
                origin_definition=image_definition,
                name=image_name,
                creating_task_id=str(task.id),
                build_job=build,
                created_by=created_by,
            )
        else:
            image.name = image_name
            image.status = ImageInstance.Status.BUILDING
            image.created_by = created_by
            image.creating_task_id = str(task.id)
            image.size_bytes = 0
            image.origin_definition = image_definition
            image.runner = runner
            image.runtime_type = image_definition.runtime_type
            await sync_to_async(image.save)(
                update_fields=[
                    "name",
                    "status",
                    "created_by",
                    "creating_task_id",
                    "size_bytes",
                    "origin_definition",
                    "runner",
                    "runtime_type",
                ]
            )

        build = await sync_to_async(
            ImageBuildJob.objects.select_related(
                "image_definition", "runner", "build_task"
            ).get
        )(id=build.id)

        payload = {
            "task_id": str(task.id),
            "build_job_id": str(build.id),
            "runtime_type": image_definition.runtime_type,
        }
        if image_definition.runtime_type == RuntimeType.DOCKER:
            payload["dockerfile_content"] = self._generate_dockerfile_content(
                image_definition
            )
            payload["image_tag"] = build_runner_ref
        else:
            payload["base_distro"] = image_definition.base_distro
            payload["init_script"] = self._build_qemu_init_script_content(
                image_definition
            )
            payload["image_path"] = build_runner_ref

        await self._emit_to_runner(runner, "task:build_image", payload)
        await sync_to_async(self.tasks.mark_in_progress)(task)
        return build

    #: Maximum build_log size kept per runner image build (chars). The log is
    #: a rolling tail: new lines are appended atomically in the DB and the
    #: stored value is trimmed to the last BUILD_LOG_MAX_CHARS characters.
    #: This bounds the per-line SQL size and the per-poll response size so
    #: long image builds (10k+ lines) cannot grow process memory without
    #: bound. The list endpoint additionally omits build_log entirely (see
    #: ImageBuildJobListOut and the dedicated /log/ endpoint).
    BUILD_LOG_MAX_CHARS = 200_000

    def handle_image_build_progress(
        self, build_job_id: str, line: str, runner_id: str | None = None
    ) -> None:
        """Append one build log line for a runner image build.

        Uses a single atomic UPDATE (Concat + Right in the database) so the
        full log is never read into Python and re-written. Per-query memory
        therefore stays proportional to the line, not to the total log.
        Unknown build jobs are ignored; runner_id mismatches are rejected.
        """
        from django.db import connection
        from django.db.models import Value
        from django.db.models.functions import Concat, Right

        from ...models import ImageBuildJob

        cleaned = (line or "").rstrip("\n")
        if "\x00" in cleaned:
            cleaned = cleaned.replace("\x00", "")
        if len(cleaned) > 8000:
            cleaned = cleaned[:8000] + "… [line truncated]"
        suffix = cleaned + "\n"
        try:
            job_id = uuid.UUID(str(build_job_id))
        except (TypeError, ValueError):
            return
        if runner_id is not None:
            exists = ImageBuildJob.objects.filter(
                id=job_id, runner_id=runner_id
            ).exists()
            if not exists:
                return
        ImageBuildJob.objects.filter(id=job_id).update(
            status=ImageBuildJob.Status.BUILDING,
            build_log=Right(
                Concat("build_log", Value(suffix)),
                self.BUILD_LOG_MAX_CHARS,
            ),
            updated_at=timezone.now(),
        )
        # Cap each logged query's retained size so the DEBUG query log
        # cannot accumulate gigabytes of UPDATE statements (DEBUG stays on).
        # Also bound the number of retained queries: this handler can run
        # thousands of times per build and shares the thread-local query
        # log with all other handlers on this connection.
        try:
            logged = connection.queries_log
            if logged:
                last = logged[-1]
                sql = last.get("sql", "")
                if isinstance(sql, str) and len(sql) > 2048:
                    last["sql"] = sql[:2048] + "… [query truncated]"
            while len(logged) > 200:
                logged.popleft()
        except Exception:
            pass

    def handle_image_built(
        self,
        *,
        task_id: str,
        build_job_id: str,
        image_tag: str = "",
        image_path: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Mark a runner image build as active and complete its task."""
        from django.utils import timezone
        from ...models import ImageInstance, ImageBuildJob

        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            raise TaskNotFoundError(task_id)
        if not self._validate_task_runner(task, runner_id):
            return

        build = ImageBuildJob.objects.get(id=build_job_id)
        build.status = ImageBuildJob.Status.ACTIVE
        build.built_at = timezone.now()
        build.save(update_fields=["status", "built_at", "updated_at"])
        image = self.image_instances.get_by_build_job_id(uuid.UUID(build_job_id))
        runner_ref = image_tag or image_path
        image_name = f"{build.image_definition.name} ({build.runner.name})"
        if image is None:
            self.image_instances.create(
                runner=build.runner,
                runtime_type=build.image_definition.runtime_type,
                origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
                origin_definition=build.image_definition,
                runner_ref=runner_ref,
                name=image_name,
                size_bytes=0,
                build_job=build,
            )
        else:
            image.name = image_name
            image.status = ImageInstance.Status.READY
            image.runner_ref = runner_ref
            image.size_bytes = 0
            image.creating_task_id = None
            image.deleted_at = None
            image.save(
                update_fields=[
                    "name",
                    "status",
                    "runner_ref",
                    "size_bytes",
                    "creating_task_id",
                    "deleted_at",
                ]
            )
        self.tasks.complete(task)

    def handle_image_build_failed(
        self,
        *,
        task_id: str,
        build_job_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Mark a runner image build as failed and fail the correlated task."""
        from ...models import ImageBuildJob

        task = self.tasks.get_by_id(uuid.UUID(task_id)) if task_id else None
        if task is not None and not self._validate_task_runner(task, runner_id):
            return

        ImageBuildJob.objects.filter(id=build_job_id).update(
            status=ImageBuildJob.Status.FAILED
        )
        image = self.image_instances.get_by_build_job_id(uuid.UUID(build_job_id))
        if image is not None:
            self.image_instances.mark_failed(image.id)
        if task is not None:
            self.tasks.fail(task, error)

    async def create_image_artifact(
        self,
        workspace_id: uuid.UUID,
        name: str,
        organization_id: uuid.UUID | None = None,
    ) -> tuple["Workspace", "Task"]:
        """Dispatch image artifact creation to the runner.

        Creates a pending image instance immediately so the UI can show
        progress. The record is updated to 'ready' when the runner completes.
        """
        from ...models import ImageInstance

        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        if organization_id and workspace.runner.organization_id != organization_id:
            raise WorkspaceNotFoundError(str(workspace_id))
        self._ensure_workspace_available(workspace)

        if workspace.status not in (
            WorkspaceStatus.RUNNING,
            WorkspaceStatus.STOPPED,
        ):
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                "must be running or stopped to capture an image"
            )
        if workspace.credentials_present:
            raise ConflictError(
                "Workspace still has credentials on disk and cannot be captured. "
                "Stop the workspace to remove them first. If it was stopped "
                "externally, resume it and stop it again."
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        # Verify runtime supports image artifact capture
        if workspace.runtime_type not in (runner.available_runtimes or []):
            raise ValueError(
                f"Runner does not support runtime '{workspace.runtime_type}'"
            )

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.CREATE_IMAGE_ARTIFACT,
            workspace=workspace,
        )

        # Create the artifact record upfront so the UI can immediately show the
        # 'creating' state.
        created_by = await sync_to_async(lambda: workspace.created_by)()
        image = await sync_to_async(self.image_instances.create_pending)(
            runner=runner,
            runtime_type=workspace.runtime_type,
            origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
            origin_workspace=workspace,
            name=name,
            creating_task_id=str(task_id),
            created_by=created_by,
        )

        await self._dispatch_workspace_task(
            runner=runner,
            event="task:create_image_artifact",
            task=task,
            workspace=workspace,
            operation=self._task_workspace_operation(TaskType.CREATE_IMAGE_ARTIFACT),
            payload={
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "name": name,
            },
        )
        logger.info(
            "Dispatched create_image_artifact (workspace=%s, task=%s, image=%s)",
            workspace_id,
            task_id,
            image.id,
        )
        return workspace, task

    def handle_image_artifact_created(
        self,
        task_id: str,
        workspace_id: str,
        artifact_id: str,
        name: str,
        size_bytes: int = 0,
        runner_id: str | None = None,
    ) -> None:
        """Handle image_artifact:created from runner and mark the image ready."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            raise TaskNotFoundError(task_id)

        if not self._validate_task_runner(task, runner_id):
            return

        image = self.image_instances.get_by_task_id(task_id)
        if image is not None:
            self.image_instances.mark_ready(
                image.id,
                runner_ref=artifact_id,
                size_bytes=size_bytes,
            )
        else:
            workspace = self.workspaces.get_by_id(uuid.UUID(workspace_id))
            if workspace is None:
                raise WorkspaceNotFoundError(workspace_id)
            self.image_instances.create(
                runner=workspace.runner,
                runtime_type=workspace.runtime_type,
                origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
                origin_workspace=workspace,
                runner_ref=artifact_id,
                name=name,
                size_bytes=size_bytes,
                created_by=task.workspace.created_by if task.workspace else None,
            )

        if task.workspace:
            self.workspaces.update_active_operation(task.workspace, None)
        self.tasks.complete(task)
        logger.info(
            "Image artifact created: workspace=%s, artifact=%s",
            workspace_id,
            artifact_id,
        )

        self._forward_to_frontend(
            "image_artifact:created",
            {
                "workspace_id": workspace_id,
                "image_artifact_id": artifact_id,
                "name": name,
                "size_bytes": size_bytes,
            },
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    def handle_image_artifact_failed(
        self,
        task_id: str,
        workspace_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Handle image_artifact:failed by marking the pending image as failed."""
        task = self.tasks.get_by_id(uuid.UUID(task_id)) if task_id else None
        if task is not None and not self._validate_task_runner(task, runner_id):
            return

        self.image_instances.mark_failed_by_task_id(task_id)

        if task is not None:
            if task.workspace:
                self.workspaces.update_active_operation(task.workspace, None)
            self.tasks.fail(task, error)

        logger.warning(
            "Image artifact creation failed: workspace=%s, task=%s, error=%s",
            workspace_id,
            task_id,
            error,
        )

        self._forward_to_frontend(
            "image_artifact:failed",
            {"workspace_id": workspace_id, "task_id": task_id, "error": error},
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    def list_image_artifacts_for_workspace(self, workspace_id: uuid.UUID) -> list:
        """Return all artifacts captured from a workspace."""
        return list(self.image_instances.list_by_workspace(workspace_id))

    def list_image_artifacts_for_user(self, user) -> list:
        """Return all artifacts created by a specific user."""
        return list(self.image_instances.list_by_user(user))

    async def delete_image_artifact(
        self,
        image_artifact_id: uuid.UUID,
    ) -> None:
        """Delete an image instance safely and dispatch cleanup to the runner if needed."""
        from ...models import ImageInstance

        image = await sync_to_async(self.image_instances.get_by_id)(image_artifact_id)
        if image is None:
            raise ValueError(f"Image artifact '{image_artifact_id}' not found")

        if image.status in (
            ImageInstance.Status.PENDING_DELETION,
            ImageInstance.Status.DELETING,
            ImageInstance.Status.DELETED,
        ):
            raise ConflictError(
                f"Image artifact '{image_artifact_id}' is already in deletion state '{image.status}'"
            )

        dependent_workspaces = await sync_to_async(
            lambda: list(self.workspaces.list_by_base_image_instance(image_artifact_id))
        )()
        if dependent_workspaces:
            raise ConflictError(
                f"Image artifact '{image_artifact_id}' is still used by {len(dependent_workspaces)} workspace(s)"
            )

        # Built images can only be deleted via their build job
        if (
            image.origin_type == ImageInstance.OriginType.DEFINITION_BUILD
            and image.build_job_id
        ):
            raise ConflictError(
                f"Built image artifact '{image_artifact_id}' can only be deleted via its runner build job"
            )

        runner = image.runner
        if not image.runner_ref:
            if image.status in (
                ImageInstance.Status.BUILDING,
                ImageInstance.Status.CAPTURING,
            ):
                raise ConflictError(
                    f"Image artifact '{image_artifact_id}' cannot be deleted while it is still {image.status}"
                )
            await sync_to_async(self.image_instances.mark_deleted)(image_artifact_id)
            logger.info(
                "Image artifact deleted without runner cleanup: %s", image_artifact_id
            )
            return

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.DELETE_IMAGE,
        )

        if runner.is_online:
            await sync_to_async(self.image_instances.mark_deleting)(
                image_artifact_id,
                deleting_task_id=str(task.id),
            )
            await self._emit_to_runner(
                runner,
                "task:delete_image_artifact",
                {
                    "task_id": str(task.id),
                    "image_instance_id": str(image.id),
                    "runtime_type": image.runtime_type,
                    "image_artifact_id": image.runner_ref,
                },
            )
            await sync_to_async(self.tasks.mark_in_progress)(task)
        else:
            await sync_to_async(self.image_instances.mark_pending_deletion)(
                image_artifact_id
            )

        logger.info("Image artifact marked for deletion: %s", image_artifact_id)

    def handle_image_artifact_deleted(
        self,
        task_id: str,
        image_instance_id: str = "",
        runner_ref: str = "",
        result: str = "deleted",
        runner_id: str | None = None,
    ) -> None:
        """Mark an image instance deleted after runner cleanup confirms it."""
        task = self.tasks.get_by_id(uuid.UUID(task_id)) if task_id else None
        if task is not None and not self._validate_task_runner(task, runner_id):
            return

        if result not in {"deleted", "already_absent"}:
            self.handle_image_artifact_delete_failed(
                task_id=task_id,
                error=f"Delete was not confirmed: {result}",
                runner_id=runner_id,
            )
            return

        image = None
        if image_instance_id:
            image = self.image_instances.get_by_id(uuid.UUID(image_instance_id))
        if image is None and task_id:
            image = self.image_instances.get_by_task_id(task_id)
        if image is None and runner_ref and runner_id:
            pending = list(
                self.image_instances.list_pending_delete_for_runner(
                    uuid.UUID(runner_id)
                )
            )
            image = next(
                (item for item in pending if item.runner_ref == runner_ref), None
            )

        if image is not None:
            self.image_instances.mark_deleted(image.id)
        if task is not None:
            self.tasks.complete(task)

    def handle_image_artifact_delete_failed(
        self,
        task_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Handle image artifact deletion failure from runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id)) if task_id else None
        if task is not None and not self._validate_task_runner(task, runner_id):
            return

        image = self.image_instances.get_by_task_id(task_id) if task_id else None
        if image is not None:
            self.image_instances.mark_delete_failed(image.id, error=error)
            if image.build_job_id:
                self.build_jobs.mark_delete_failed(image.build_job_id, error=error)
                self._mark_definition_delete_failed(
                    image.origin_definition_id,
                    error=error or "Runner build cleanup failed",
                )
        elif task_id:
            build = self._get_build_by_delete_task(task_id)
            if build is not None:
                self.build_jobs.mark_delete_failed(build.id, error=error)
                self._mark_definition_delete_failed(
                    build.image_definition_id,
                    error=error or "Runner build cleanup failed",
                )
        if task is not None:
            self.tasks.fail(task, error=error or "Delete failed on runner")
        logger.warning("Image artifact delete failed: task=%s error=%s", task_id, error)

    async def delete_build_job(self, build_job_id: uuid.UUID) -> None:
        """Delete a runner image build and its associated artifact.

        Checks workspace dependencies before allowing deletion.
        If runner is offline, queues as pending_deletion.
        """
        from ...models import ImageBuildJob

        build = await sync_to_async(self.build_jobs.get_by_id)(build_job_id)
        if build is None:
            raise ValueError(f"Build job '{build_job_id}' not found")

        if build.status in (
            ImageBuildJob.Status.PENDING_DELETION,
            ImageBuildJob.Status.DELETING,
            ImageBuildJob.Status.DELETED,
        ):
            raise ConflictError(
                f"Build job '{build_job_id}' is already in deletion state '{build.status}'"
            )

        has_deps, dep_count = await sync_to_async(
            self.build_jobs.has_dependent_workspaces
        )(build_job_id)
        if has_deps:
            raise ConflictError(
                f"Build job '{build_job_id}' is still used by {dep_count} workspace(s)"
            )

        runner = build.runner
        instance = await sync_to_async(lambda: getattr(build, "image_instance", None))()

        if instance and instance.runner_ref and runner.is_online:
            task_id = generate_uuid()
            task = await sync_to_async(self.tasks.create)(
                task_id=task_id,
                runner=runner,
                task_type=TaskType.DELETE_IMAGE,
            )
            await sync_to_async(self._mark_definition_deleting_if_needed)(
                build.image_definition_id
            )
            await sync_to_async(self.build_jobs.mark_deleting)(
                build_job_id, deleting_task_id=str(task.id)
            )
            if instance:
                await sync_to_async(self.image_instances.mark_deleting)(
                    instance.id, deleting_task_id=str(task.id)
                )
            await self._emit_to_runner(
                runner,
                "task:delete_image_artifact",
                {
                    "task_id": str(task.id),
                    "image_instance_id": str(instance.id) if instance else "",
                    "runtime_type": build.image_definition.runtime_type,
                    "image_artifact_id": instance.runner_ref if instance else "",
                },
            )
            await sync_to_async(self.tasks.mark_in_progress)(task)
        elif instance and instance.runner_ref:
            # Runner offline
            await sync_to_async(self.build_jobs.mark_pending_deletion)(build_job_id)
            if instance:
                await sync_to_async(self.image_instances.mark_pending_deletion)(
                    instance.id
                )
        else:
            # No physical artifact to clean up
            await sync_to_async(self.build_jobs.mark_deleted)(build_job_id)
            if instance:
                await sync_to_async(self.image_instances.mark_deleted)(instance.id)

        logger.info("Build job marked for deletion: %s", build_job_id)

    def handle_build_job_deleted(
        self,
        task_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle successful build job deletion from runner.

        Marks both the build job and its image instance as deleted.
        Also checks if the parent definition can be marked deleted.
        """
        task = self.tasks.get_by_id(uuid.UUID(task_id)) if task_id else None
        if task is not None and not self._validate_task_runner(task, runner_id):
            return

        # Find build job by task_id
        build = self._get_build_by_delete_task(task_id)

        if build is not None:
            self.build_jobs.mark_deleted(build.id)
            instance = getattr(build, "image_instance", None)
            if instance:
                self.image_instances.mark_deleted(instance.id)
            # Check if parent definition can be marked deleted
            self._check_definition_deletion_complete(build.image_definition_id)

        if task is not None:
            self.tasks.complete(task)

    def _check_definition_deletion_complete(self, definition_id: uuid.UUID) -> None:
        """Check if all builds for a definition are deleted and finalize."""
        from ...models import ImageDefinition

        definition = self.image_definitions.get_by_id(definition_id)
        if definition is None:
            return
        if definition.status not in (
            ImageDefinition.Status.PENDING_DELETION,
            ImageDefinition.Status.DELETING,
        ):
            return

        remaining = self.build_jobs.list_non_deleted_for_definition(definition_id)
        if not remaining.exists():
            self.image_definitions.mark_deleted(definition_id)
            logger.info("Definition fully deleted: %s", definition_id)

    def _get_build_by_delete_task(self, task_id: str):
        """Return the build job currently linked to a delete task, if any."""
        from ...models import ImageBuildJob

        return (
            ImageBuildJob.objects.filter(deleting_task_id=task_id)
            .select_related("image_definition", "image_instance")
            .first()
        )

    def _mark_definition_delete_failed(
        self,
        definition_id: uuid.UUID | None,
        *,
        error: str,
    ) -> None:
        """Move a definition delete flow into DELETE_FAILED when a child cleanup fails."""
        if definition_id is None:
            return
        definition = self.image_definitions.get_by_id(definition_id)
        if definition is None:
            return
        if definition.status not in {
            definition.Status.PENDING_DELETION,
            definition.Status.DELETING,
            definition.Status.DELETE_FAILED,
        }:
            return
        self.image_definitions.mark_delete_failed(definition_id, error=error)

    def _mark_definition_deleting_if_needed(
        self, definition_id: uuid.UUID | None
    ) -> None:
        """Promote a pending definition delete to deleting once runner cleanup starts."""
        if definition_id is None:
            return
        definition = self.image_definitions.get_by_id(definition_id)
        if definition is None:
            return
        if definition.status in {
            definition.Status.PENDING_DELETION,
            definition.Status.DELETE_FAILED,
        }:
            self.image_definitions.mark_deleting(definition_id)

    async def deactivate_image_definition(self, definition_id: uuid.UUID) -> None:
        """Deactivate a definition — immediately not selectable for new workspaces."""
        definition = await sync_to_async(self.image_definitions.get_by_id)(
            definition_id
        )
        if definition is None:
            raise ValueError(f"Image definition '{definition_id}' not found")
        await sync_to_async(self.image_definitions.deactivate)(definition_id)
        logger.info("Image definition deactivated: %s", definition_id)

    async def activate_image_definition(self, definition_id: uuid.UUID) -> None:
        """Re-activate a deactivated definition."""
        from ...models import ImageDefinition

        definition = await sync_to_async(self.image_definitions.get_by_id)(
            definition_id
        )
        if definition is None:
            raise ValueError(f"Image definition '{definition_id}' not found")
        if definition.status not in (
            ImageDefinition.Status.DEACTIVATED,
            ImageDefinition.Status.ACTIVE,
            ImageDefinition.Status.DELETE_FAILED,
        ):
            raise ConflictError(
                f"Cannot activate definition in state '{definition.status}'"
            )
        if definition.status == ImageDefinition.Status.DELETE_FAILED:
            in_progress = await sync_to_async(
                lambda: self.build_jobs.list_in_progress_deletes_for_definition(
                    definition_id
                ).exists()
            )()
            if in_progress:
                raise ConflictError(
                    "Cannot restore while runner image removal is still in progress"
                )
        await sync_to_async(self.image_definitions.activate)(definition_id)
        logger.info("Image definition activated: %s", definition_id)

    async def delete_image_definition(self, definition_id: uuid.UUID) -> None:
        """Orchestrated two-step definition delete.

        Step 1: Immediately deactivate.
        Step 2: Initiate deletion of all runner builds.
        Definition itself is only marked deleted when all build deletes are confirmed.
        """
        from ...models import ImageDefinition, ImageBuildJob

        definition = await sync_to_async(self.image_definitions.get_by_id)(
            definition_id
        )
        if definition is None:
            raise ValueError(f"Image definition '{definition_id}' not found")

        if definition.status == ImageDefinition.Status.DELETED:
            raise ConflictError("Definition is already deleted")
        if definition.status == ImageDefinition.Status.DELETING:
            raise ConflictError("Definition deletion is already in progress")

        # Step 1: Deactivate
        await sync_to_async(self.image_definitions.deactivate)(definition_id)

        # Get all non-deleted builds for this definition
        builds = await sync_to_async(
            lambda: list(self.build_jobs.list_non_deleted_for_definition(definition_id))
        )()

        if not builds:
            # No builds -> mark definition deleted directly
            await sync_to_async(self.image_definitions.mark_deleted)(definition_id)
            logger.info("Definition deleted (no builds): %s", definition_id)
            return

        # Step 2: Mark definition as pending deletion and initiate build deletes
        await sync_to_async(self.image_definitions.mark_pending_deletion)(definition_id)

        initiation_errors: list[str] = []
        for build in builds:
            if build.status == ImageBuildJob.Status.DELETED:
                continue
            try:
                await self.delete_build_job(build.id)
            except (ConflictError, ValueError) as e:
                initiation_errors.append(str(e))
                logger.warning(
                    "Could not initiate build job deletion %s: %s", build.id, e
                )

        if initiation_errors:
            error = "; ".join(initiation_errors)
            await sync_to_async(self.image_definitions.mark_delete_failed)(
                definition_id,
                error=error,
            )
            raise ConflictError(error)

        # Check if all are already done
        await sync_to_async(self._check_definition_deletion_complete)(definition_id)

    async def dispatch_pending_build_job_deletions(self, runner: "Runner") -> list:
        """Dispatch pending build job deletions that accumulated while runner was offline."""
        from ...models import ImageBuildJob, ImageInstance

        pending = await sync_to_async(
            lambda: list(self.build_jobs.list_pending_delete_for_runner(runner.id))
        )()

        dispatched = []
        for build in pending:
            instance = await sync_to_async(
                lambda: getattr(build, "image_instance", None)
            )()
            if not instance or not instance.runner_ref:
                continue
            try:
                reused_active_task = False
                if not build.deleting_task_id:
                    task = None
                else:
                    existing_task = await sync_to_async(self.tasks.get_by_id)(
                        uuid.UUID(build.deleting_task_id)
                    )
                    if existing_task and existing_task.status in {
                        TaskStatus.PENDING,
                        TaskStatus.IN_PROGRESS,
                    }:
                        task = existing_task
                        reused_active_task = (
                            build.status == ImageBuildJob.Status.DELETING
                            and instance.status == ImageInstance.Status.DELETING
                        )
                    else:
                        task = None
                if task is None:
                    task_id = generate_uuid()
                    task = await sync_to_async(self.tasks.create)(
                        task_id=task_id,
                        runner=runner,
                        task_type=TaskType.DELETE_IMAGE,
                    )

                await sync_to_async(self._mark_definition_deleting_if_needed)(
                    build.image_definition_id
                )
                if not reused_active_task:
                    await sync_to_async(self.build_jobs.mark_deleting)(
                        build.id, deleting_task_id=str(task.id)
                    )
                    await sync_to_async(self.image_instances.mark_deleting)(
                        instance.id, deleting_task_id=str(task.id)
                    )
                await self._emit_to_runner(
                    runner,
                    "task:delete_image_artifact",
                    {
                        "task_id": str(task.id),
                        "image_instance_id": str(instance.id),
                        "runtime_type": build.image_definition.runtime_type,
                        "image_artifact_id": instance.runner_ref,
                    },
                )
                await sync_to_async(self.tasks.mark_in_progress)(task)
                dispatched.append(build)
            except Exception:
                logger.exception(
                    "Failed to dispatch pending build deletion %s for runner %s",
                    build.id,
                    runner.id,
                )
        return dispatched
