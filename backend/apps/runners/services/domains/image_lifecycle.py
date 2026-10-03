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
import uuid
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.utils import timezone

from common.exceptions import ConflictError
from common.utils import generate_uuid

from ...enums import RuntimeType, TaskStatus, TaskType, WorkspaceStatus
from ...exceptions import (
    RunnerOfflineError,
    WorkspaceNotFoundError,
    WorkspaceStateError,
)

if TYPE_CHECKING:
    from ...models import Runner, Task, Workspace

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
        return list(self.image_definitions.list_by_org(organization_id))

    def list_build_jobs(
        self,
        image_definition_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> list:
        """List runner build records for an image definition."""
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
                return build.pending_generation or build.current_generation
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

    def create_image_definition(self, **fields):
        """Create a recipe without requesting a build."""
        return self.image_definitions.create(**fields)

    def update_image_recipe(self, definition_id, values):
        """Persist recipe edits through the repository transaction."""
        return self.image_definitions.update_recipe(definition_id, values)

    def image_definition_copy_name(self, base_name, organization_id):
        """Resolve an organization-scoped duplicate name."""
        return self.image_definitions.copy_name(base_name, organization_id)

    def get_visible_image_definition(self, definition_id, organization_id):
        """Resolve a recipe visible to an organization."""
        return self.image_definitions.get_by_id_and_org(definition_id, organization_id)

    def deactivate_runner_image(self, build):
        """Disable assignment selection without mutating any generation."""
        self._ensure_definition_mutable(build.image_definition)
        return self.build_jobs.ensure_inactive(build.image_definition, build.runner)

    def get_image_definition(self, definition_id: uuid.UUID):
        """Return an image definition by ID or None."""
        return self.image_definitions.get_by_id(definition_id)

    async def activate_build_job(self, build, *, created_by=None):
        """Make an existing runner image selectable, or build it if none exists."""
        from ...models import ImageInstance

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

        build = await sync_to_async(self.build_jobs.activate)(build.id)
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

        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=image_definition.runtime_type,
        )
        self._ensure_definition_mutable(image_definition)

        from ...repositories import ImageGenerationRepository

        if not activate:
            return await sync_to_async(self.build_jobs.ensure_inactive)(
                image_definition, runner
            )
        if image_definition.runtime_type == RuntimeType.QEMU:
            self._validate_qemu_base_distro(image_definition.base_distro)
            rendered = {
                "base_distro": image_definition.base_distro,
                "init_script": self._build_qemu_init_script_content(image_definition),
            }
        else:
            rendered = {
                "dockerfile_content": self._generate_dockerfile_content(
                    image_definition
                )
            }
        build, image, task = await sync_to_async(ImageGenerationRepository.request)(
            definition=image_definition,
            runner=runner,
            rendered_input=rendered,
            created_by=created_by,
        )
        payload = {
            **rendered,
            "task_id": str(task.id),
            "build_job_id": str(build.id),
            "image_instance_id": str(image.id),
            "runtime_type": image.runtime_type,
        }
        payload[
            "image_tag" if image.runtime_type == RuntimeType.DOCKER else "image_path"
        ] = image.runner_ref
        await self._emit_to_runner(runner, "task:build_image", payload)
        if runner.sid:
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
        self,
        build_job_id: str,
        line: str,
        runner_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """Append one build log line for a runner image build.

        Uses a single atomic UPDATE (Concat + Right in the database) so the
        full log is never read into Python and re-written. Per-query memory
        therefore stays proportional to the line, not to the total log.
        Unknown build jobs are ignored; runner_id mismatches are rejected.
        """
        from ...repositories import ImageGenerationRepository

        ImageGenerationRepository.progress(
            build_job_id=build_job_id,
            task_id=task_id,
            runner_id=runner_id,
            line=line,
            max_chars=self.BUILD_LOG_MAX_CHARS,
        )

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

        from ...repositories import ImageGenerationRepository

        ImageGenerationRepository.finish(
            task_id=task_id,
            build_job_id=build_job_id,
            runner_id=runner_id,
            runner_ref=image_tag or image_path,
        )

    def handle_image_build_failed(
        self,
        *,
        task_id: str,
        build_job_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Fail only the correlated generation; retain the current image."""
        from ...repositories import ImageGenerationRepository

        ImageGenerationRepository.finish(
            task_id=task_id,
            build_job_id=build_job_id,
            runner_id=runner_id,
            error=error,
        )

    async def create_image_artifact(
        self, workspace_id: uuid.UUID, name: str,
        organization_id: uuid.UUID | None = None,
        stop_and_restart: bool = False,
    ) -> tuple["Workspace", "Task"]:
        """Persist approved discrete QEMU capture phases; worker owns progression."""
        from ...capture_repository import CaptureRepository
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None or (organization_id and
                workspace.runner.organization_id != organization_id):
            raise WorkspaceNotFoundError(str(workspace_id))
        return await sync_to_async(CaptureRepository.allocate)(
            workspace_id, name, stop_and_restart)

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
        from ...repositories import ImageGenerationRepository

        if not ImageGenerationRepository.capture_result(
            task_id=task_id,
            workspace_id=workspace_id,
            runner_id=runner_id,
            runner_ref=artifact_id,
            size_bytes=size_bytes,
        ):
            return

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
        from ...repositories import ImageGenerationRepository

        if not ImageGenerationRepository.capture_result(
            task_id=task_id,
            workspace_id=workspace_id,
            runner_id=runner_id,
            error=error,
        ):
            return

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
        from ...deletion_repository import DeletionRepository
        obj = await sync_to_async(self.image_instances.get_by_id)(image_artifact_id)
        if obj is None:
            raise ValueError('Deletion target not found')
        if not obj.runner.organization_id:
            raise ConflictError('Global recipes are read-only')
        return await sync_to_async(DeletionRepository.request)(
            obj.runner.organization_id, None, 'image', obj.id)

    def handle_image_artifact_deleted(
        self,
        task_id: str,
        image_instance_id: str = "",
        runner_ref: str = "",
        result: str = "deleted",
        runner_id: str | None = None,
    ) -> None:
        """Mark an image instance deleted after runner cleanup confirms it."""
        from ...repositories import ImageGenerationRepository

        if result not in {"deleted", "already_absent"}:
            self.handle_image_artifact_delete_failed(
                task_id=task_id,
                error=f"Delete was not confirmed: {result}",
                runner_id=runner_id,
            )
            return
        image = ImageGenerationRepository.delete_result(
            task_id=task_id,
            runner_id=runner_id,
            image_id=image_instance_id,
            runner_ref=runner_ref,
        )
        # Coordinator finalizes assignments after all generation confirmations.

    def handle_image_artifact_delete_failed(
        self,
        task_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Fail only the exact live deletion attempt."""
        from ...repositories import ImageGenerationRepository

        image = ImageGenerationRepository.delete_result(
            task_id=task_id,
            runner_id=runner_id,
            error=error,
        )
        if image and image.build_job_id:
            self.build_jobs.mark_delete_failed(image.build_job_id, error=error)
            self._mark_definition_delete_failed(image.origin_definition_id, error=error)

    async def delete_build_job(self, build_job_id: uuid.UUID) -> None:
        """Delete a runner image build and its associated artifact.

        Checks workspace dependencies before allowing deletion.
        If runner is offline, queues as pending_deletion.
        """
        from ...deletion_repository import DeletionRepository
        obj = await sync_to_async(self.build_jobs.get_by_id)(build_job_id)
        if obj is None:
            raise ValueError('Deletion target not found')
        if not obj.runner.organization_id:
            raise ConflictError('Global recipes are read-only')
        return await sync_to_async(DeletionRepository.request)(
            obj.runner.organization_id, None, 'assignment', obj.id)

    def handle_build_job_deleted(
        self,
        task_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle successful build job deletion from runner.

        Marks both the build job and its image instance as deleted.
        Also checks if the parent definition can be marked deleted.
        """
        self.handle_image_artifact_deleted(task_id=task_id, runner_id=runner_id)

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
        return self.build_jobs.get_by_delete_task(task_id)

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
        from ...deletion_repository import DeletionRepository
        obj = await sync_to_async(self.image_definitions.get_by_id)(definition_id)
        if obj is None:
            raise ValueError('Deletion target not found')
        if not obj.organization_id:
            raise ConflictError('Global recipes are read-only')
        return await sync_to_async(DeletionRepository.request)(
            obj.organization_id, None, 'definition', obj.id)

    async def dispatch_pending_build_job_deletions(self, runner: "Runner") -> list:
        """Dispatch pending build job deletions that accumulated while runner was offline."""
        return []  # Independent coordinator owns release.
