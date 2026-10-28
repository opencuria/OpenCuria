"""Representative workspace list data shared by query tests and benchmarks."""

from __future__ import annotations

import uuid
from typing import Any

from apps.credentials.models import Credential, CredentialService
from apps.plugins.models import Plugin, WorkspacePluginActivation
from apps.runners.models import (
    CapturedImage,
    ImageBuildJob,
    ImageDefinition,
    ImageInstance,
    ImageRevision,
    Runner,
    Workspace,
)


def workspace_read_cases(
    runner: Runner, user: Any, count: int, *, with_metadata: bool
) -> list[Workspace]:
    """Create distinct image lines, shared attachments and legacy/empty cases."""
    case_id = uuid.uuid4().hex[:8]
    credentials, plugins = [], []
    if with_metadata:
        for kind in ("env", "provider_secret", "mcp_oauth"):
            service = CredentialService.objects.create(
                name=kind, slug=uuid.uuid4().hex, credential_type=kind
            )
            credentials.append(
                Credential.objects.create(
                    service=service,
                    user=user,
                    name=kind,
                    encrypted_value="opaque-secret-that-must-not-be-returned",
                )
            )
        plugins = [
            Plugin.objects.create(name=name, slug=uuid.uuid4().hex, enabled=False)
            for name in ("Z first selected", "A last selected")
        ]
    workspaces = []
    for index in range(count):
        base, pending = None, None
        if with_metadata:
            image_fields: dict[str, Any] = {}
            if index % 3 == 0:
                line = CapturedImage.objects.create(
                    runner=runner,
                    organization=runner.organization,
                    created_by=user,
                    name=f"Capture {index}",
                )
                image_fields["captured_image"] = line
            elif index % 3 == 1:
                definition = ImageDefinition.objects.create(
                    name=f"Recipe {index} {case_id}", organization=runner.organization
                )
                job = ImageBuildJob.objects.create(
                    runner=runner, image_definition=definition, status="active"
                )
                image_fields.update(
                    build_job=job,
                    origin_definition=definition,
                    origin_type="definition_build",
                    revision=ImageRevision.objects.create(
                        definition=definition,
                        digest=uuid.uuid4().hex,
                        recipe={},
                        rendered_input={},
                    ),
                )
            else:
                image_fields["is_legacy"] = True
            base = ImageInstance.objects.create(
                runner=runner,
                name=f"Base {index}",
                generation=1,
                runner_ref=f"base-{index}",
                **image_fields,
            )
            if index % 3 != 2:
                pending = ImageInstance.objects.create(
                    runner=runner,
                    name=f"Pending {index}",
                    generation=2,
                    runner_ref=f"pending-{index}",
                    **image_fields,
                )
                if index % 3 == 1:
                    job.current_generation = pending
                    job.save(update_fields=["current_generation"])
        workspace = Workspace.objects.create(
            runner=runner,
            created_by=user,
            name=f"Workspace {index}",
            status="running",
            base_image_instance=base,
            pending_base_image_instance=pending,
        )
        workspace.credentials.set(credentials)
        for plugin in plugins:
            WorkspacePluginActivation.objects.create(
                workspace=workspace, plugin=plugin, enabled_by=user
            )
        workspaces.append(workspace)
    return workspaces
